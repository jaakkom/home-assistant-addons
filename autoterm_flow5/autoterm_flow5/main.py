"""Add-on process: serial poll, control, MQTT discovery."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from .controller import Controller, aggregate
from .faults import ChangeNotifier, LinkNotifier, fault_name
from .fuel import FuelMeter, liters_per_hour
from .ha import deliver_notice, read_temperature, supervisor_token
from .link import SerialLink, open_port
from .mqtt_bridge import PREFIX, apply_command, collect_state, discovery_messages, state_topic
from .settings import Settings, settings_from_options

LOG = logging.getLogger("autoterm_flow5")
OPTIONS = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))
SETTINGS_PATH = Path(os.environ.get("SETTINGS_PATH", "/data/settings.json"))
FUEL_PATH = Path(os.environ.get("FUEL_PATH", "/data/fuel.json"))
HA_API = os.environ.get("HA_API_URL", "http://supervisor/core/api")
POLL_S = 1.0


def load_options() -> dict:
    if not OPTIONS.exists():
        return {}
    try:
        return json.loads(OPTIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        LOG.warning("Asetustiedostoa ei voitu lukea")
        return {}


def mqtt_config(options: dict) -> tuple[str, int, str, str]:
    host = options.get("mqtt_host") or os.environ.get("MQTT_HOST") or "core-mosquitto"
    port = int(options.get("mqtt_port") or os.environ.get("MQTT_PORT") or 1883)
    username = options.get("mqtt_username") or os.environ.get("MQTT_USER") or os.environ.get("MQTT_USERNAME") or ""
    password = options.get("mqtt_password") or os.environ.get("MQTT_PASSWORD") or ""
    return str(host), port, str(username), str(password)


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    options = load_options()
    settings = Settings.load(SETTINGS_PATH, settings_from_options(options))
    settings.save(SETTINGS_PATH)
    controller = Controller(settings)
    fuel = FuelMeter(FUEL_PATH, settings.pump_dose_ml)
    faults = ChangeNotifier("autoterm_flow5_fault", "Autoterm Flow 5")
    link_notice = LinkNotifier()
    token = supervisor_token()
    host, port, username, password = mqtt_config(options)
    baud = int(options.get("baud", 2400))
    serial_path = str(options.get("serial_port", "/dev/ttyUSB0"))

    import paho.mqtt.client as mqtt

    client = mqtt.Client()
    if username:
        client.username_pw_set(username, password)
    pending: list[tuple[str, str]] = []

    def on_message(_client, _userdata, message) -> None:
        pending.append((message.topic, message.payload.decode("utf-8", errors="replace")))

    client.on_message = on_message
    client.will_set(f"{PREFIX}/availability", "offline", retain=True)
    while True:
        try:
            client.connect(host, port, keepalive=60)
            break
        except Exception as exc:
            LOG.warning("MQTT-yhteys epäonnistui (%s), uusi yritys", exc)
            time.sleep(5)
    client.subscribe(f"{PREFIX}/set/#")
    client.loop_start()
    for topic, payload in discovery_messages():
        client.publish(topic, payload, retain=True)
    client.publish(f"{PREFIX}/availability", "online", retain=True)
    LOG.info("MQTT yhdistetty %s:%s, sarjaportti %s @ %s", host, port, serial_path, baud)

    link: SerialLink | None = None
    last_room: float | None = None
    last_room_at: float | None = None
    try:
        while True:
            started = time.monotonic()
            now = time.time()
            while pending:
                topic, payload = pending.pop(0)
                action = apply_command(controller, settings, topic, payload, now)
                if action == "reset-fuel":
                    fuel.reset()
                settings.save(SETTINGS_PATH)
                controller.settings = settings
            room, last_room, last_room_at = _room(settings, token, now, last_room, last_room_at)
            age = None if last_room_at is None else now - last_room_at
            if link is None:
                try:
                    link = SerialLink(open_port(serial_path, baud), settings.extended_telemetry)
                except Exception as exc:  # serial port missing or busy
                    LOG.warning("Sarjaporttia ei saatu auki: %s", exc)
                    _publish_offline(client, controller, fuel, settings, last_room)
                    time.sleep(POLL_S)
                    continue
            link.extended_telemetry = settings.extended_telemetry
            try:
                output = controller.tick(now, link.status, link.extended, last_room, age)
                link.poll(now, output.commands)
            except Exception as exc:
                LOG.warning("Sarjaliikenne epäonnistui: %s", exc)
                link = None
                notice = link_notice.update(False)
                if notice:
                    deliver_notice(notice, token, HA_API)
                    client.publish(
                        f"{PREFIX}/event/link",
                        json.dumps({"online": False, "message": notice.message}),
                    )
                time.sleep(POLL_S)
                continue
            frequency = link.extended.pump_hz if link.extended else 0.0
            fuel.dose_ml = settings.pump_dose_ml
            if fuel.update(now, frequency, settings.pump_dose_ml):
                fuel.save()
            online = link.online(now)
            code = _current_fault(link)
            notice = faults.update(code)
            if notice:
                deliver_notice(notice, token, HA_API)
                if notice.action == "create":
                    client.publish(
                        f"{PREFIX}/event/fault",
                        json.dumps({"code": notice.code, "message": notice.message or fault_name(code)}),
                    )
            link_event = link_notice.update(online)
            if link_event:
                deliver_notice(link_event, token, HA_API)
                client.publish(
                    f"{PREFIX}/event/link",
                    json.dumps({"online": online, "message": link_event.message}),
                )
            values = collect_state(
                link.status,
                link.extended,
                fuel=fuel,
                settings=settings,
                controller=controller,
                room_c=last_room if age is not None and age <= settings.sensor_stale_s else None,
                online=online,
                telemetry_stale=link.telemetry_stale(now),
                fan_on=output.fan_on,
                frequency_hz=frequency,
            )
            for key, value in values.items():
                client.publish(state_topic(key), value, retain=True)
            if frequency:
                LOG.debug("kulutus %.3f l/h", liters_per_hour(frequency, settings.pump_dose_ml))
            time.sleep(max(0.0, POLL_S - (time.monotonic() - started)))
    finally:
        client.publish(f"{PREFIX}/availability", "offline", retain=True)
        client.loop_stop()
        client.disconnect()


def _room(
    settings: Settings,
    token: str,
    now: float,
    last_room: float | None,
    last_room_at: float | None,
) -> tuple[float | None, float | None, float | None]:
    readings = [
        read_temperature(entity_id, token, HA_API)
        for entity_id in settings.entity_ids()
    ]
    current = aggregate(readings, settings.aggregation)
    if current is None:
        return last_room, last_room, last_room_at
    return current, current, now


def _current_fault(link: SerialLink) -> int:
    if link.status is not None and link.status.fault:
        return link.status.fault
    if link.extended is not None:
        return link.extended.fault
    if link.status is not None:
        return link.status.fault
    return 0


def _publish_offline(client, controller, fuel, settings, room) -> None:
    values = collect_state(
        None,
        None,
        fuel=fuel,
        settings=settings,
        controller=controller,
        room_c=room,
        online=False,
        telemetry_stale=True,
        fan_on=False,
        frequency_hz=0,
    )
    for key, value in values.items():
        client.publish(state_topic(key), value, retain=True)


if __name__ == "__main__":
    run()
