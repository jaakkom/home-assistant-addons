"""Add-on process: serial poll, control, MQTT discovery."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .controller import Controller, aggregate
from .faults import ChangeNotifier, LinkNotifier, Notice, fault_name
from .fuel import FuelMeter, liters_per_hour
from .ha import deliver_notice, read_temperature, supervisor_token
from .link import SerialLink, open_port
from .mqtt_bridge import PREFIX, apply_command, collect_state, discovery_messages, state_topic
from .settings import Settings, option_value, settings_from_options
from .simulator import SimulatedHeater

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
        LOG.warning("Could not read the options file")
        return {}


def mqtt_config(options: dict) -> tuple[str, int, str, str]:
    host = option_value(options, "mqtt", "mqtt_host", "") or os.environ.get("MQTT_HOST") or "core-mosquitto"
    port = int(option_value(options, "mqtt", "mqtt_port", "") or os.environ.get("MQTT_PORT") or 1883)
    username = option_value(options, "mqtt", "mqtt_username", "") or os.environ.get("MQTT_USER") or os.environ.get("MQTT_USERNAME") or ""
    password = option_value(options, "mqtt", "mqtt_password", "") or os.environ.get("MQTT_PASSWORD") or ""
    return str(host), port, str(username), str(password)


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    options = load_options()
    settings = Settings.load(SETTINGS_PATH, settings_from_options(options))
    settings.save(SETTINGS_PATH)
    controller = Controller(settings)
    fuel = FuelMeter(FUEL_PATH, settings.pump_dose_ml)
    if fuel.clamp_to_capacity(settings.tank_capacity_l):
        fuel.save()
    faults = ChangeNotifier("autoterm_flow5_fault", "Autoterm Flow 5")
    link_notice = LinkNotifier()
    token = supervisor_token()
    host, port, username, password = mqtt_config(options)
    baud = int(option_value(options, "device", "baud", 2400) or 2400)
    serial_path = str(option_value(options, "device", "serial_port", "/dev/ttyUSB0") or "/dev/ttyUSB0")

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
            LOG.warning("MQTT connection failed (%s), retrying", exc)
            time.sleep(5)
    client.subscribe(f"{PREFIX}/set/#")
    client.loop_start()
    for topic, payload in discovery_messages():
        client.publish(topic, payload, retain=True)
    client.publish(f"{PREFIX}/availability", "online", retain=True)
    if settings.simulate:
        LOG.info("MQTT connected %s:%s, simulation enabled, UART is not used", host, port)
    else:
        LOG.info("MQTT connected %s:%s, serial port %s @ %s", host, port, serial_path, baud)

    link: SerialLink | None = None
    last_room: float | None = None
    last_room_at: float | None = None
    try:
        while True:
            started = time.monotonic()
            now = time.time()
            while pending:
                topic, payload = pending.pop(0)
                action = apply_command(controller, settings, topic, payload, now, fuel)
                if action == "reset-fuel":
                    fuel.reset()
                settings.save(SETTINGS_PATH)
                controller.settings = settings
            room, last_room, last_room_at = _room(settings, token, now, last_room, last_room_at)
            age = None if last_room_at is None else now - last_room_at
            if link is None:
                try:
                    link = open_link(settings, serial_path, baud)
                except Exception as exc:  # serial port missing or busy
                    LOG.warning("Could not open the serial port: %s", exc)
                    _publish_offline(client, controller, fuel, settings, last_room)
                    time.sleep(POLL_S)
                    continue
            link.extended_telemetry = settings.extended_telemetry
            try:
                result = step(now, link, controller, settings, fuel, faults, link_notice, last_room, age)
            except Exception as exc:
                LOG.warning("Heater communication failed: %s", exc)
                if not settings.simulate:
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
            for notice in result.notices:
                deliver_notice(notice, token, HA_API)
                if notice.notification_id.endswith("_fault") and notice.action == "create":
                    client.publish(
                        f"{PREFIX}/event/fault",
                        json.dumps({"code": notice.code, "message": notice.message or fault_name(notice.code or 0)}),
                    )
                if notice.notification_id.endswith("_link"):
                    client.publish(
                        f"{PREFIX}/event/link",
                        json.dumps({"online": result.values.get("link") == "ON", "message": notice.message}),
                    )
            for key, value in result.values.items():
                client.publish(state_topic(key), value, retain=True)
            if result.frequency_hz:
                LOG.debug(
                    "consumption %.3f L/h",
                    liters_per_hour(result.frequency_hz, settings.pump_dose_ml),
                )
            time.sleep(max(0.0, POLL_S - (time.monotonic() - started)))
    finally:
        client.publish(f"{PREFIX}/availability", "offline", retain=True)
        client.loop_stop()
        client.disconnect()


@dataclass
class CycleResult:
    values: dict[str, str]
    notices: list[Notice]
    frequency_hz: float


def open_link(settings: Settings, serial_path: str, baud: int) -> SerialLink:
    """Open the UART, or a simulated heater when simulation is enabled."""
    if settings.simulate:
        return SerialLink(SimulatedHeater(), settings.extended_telemetry)
    return SerialLink(open_port(serial_path, baud), settings.extended_telemetry)


def step(
    now: float,
    link: SerialLink,
    controller: Controller,
    settings: Settings,
    fuel: FuelMeter,
    faults: ChangeNotifier,
    link_notice: LinkNotifier,
    room_c: float | None,
    room_age_s: float | None,
) -> CycleResult:
    """One control cycle against the previous status, then a fresh poll."""
    port = link.port
    if settings.simulate and isinstance(port, SimulatedHeater):
        port.fault = int(settings.simulate_fault)
    output = controller.tick(now, link.status, link.extended, room_c, room_age_s)
    link.poll(now, output.commands)
    frequency = link.extended.pump_hz if link.extended is not None else 0.0
    fuel.update(now, frequency, settings.pump_dose_ml)
    if fuel.pending_save:
        fuel.save()
    online = link.online(now)
    notices: list[Notice] = []
    fault_notice = faults.update(_current_fault(link))
    if fault_notice is not None:
        notices.append(fault_notice)
    link_event = link_notice.update(online)
    if link_event is not None:
        notices.append(link_event)
    coolant = link.status.coolant_c if link.status is not None else None
    fan_on = coolant is not None and coolant >= settings.fan_temperature
    fresh = room_c is not None and room_age_s is not None and room_age_s <= settings.sensor_stale_s
    values = collect_state(
        link.status,
        link.extended,
        fuel=fuel,
        settings=settings,
        controller=controller,
        room_c=room_c if fresh else None,
        online=online,
        telemetry_stale=link.telemetry_stale(now),
        fan_on=fan_on,
        frequency_hz=frequency,
    )
    return CycleResult(values=values, notices=notices, frequency_hz=frequency)


def _room(
    settings: Settings,
    token: str,
    now: float,
    last_room: float | None,
    last_room_at: float | None,
) -> tuple[float | None, float | None, float | None]:
    if settings.simulate and not settings.entity_ids():
        return settings.simulate_room_c, settings.simulate_room_c, now
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
