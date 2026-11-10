"""Add-on process: serial poll, control, MQTT discovery."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .adaptive import AdaptiveController, config_from_settings
from .controller import Controller, aggregate, phase_of, _coolant, _fault
from .faults import ChangeNotifier, LinkNotifier, Notice, fault_name
from .fuel import FuelMeter, liters_per_hour
from .ha import deliver_notice, read_temperature, supervisor_token
from .link import LinkProgress, SerialLink, TelemetryLog, open_port
from .mqtt_bridge import PREFIX, apply_command, collect_state, discovery_messages, mqtt_state_updates, state_topic
from .panel import PanelHub, start_panel
from .settings import Settings, load_settings, option_value
from .simulator import SimulatedHeater

LOG = logging.getLogger("autoterm_flow5")
OPTIONS = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))
SETTINGS_PATH = Path(os.environ.get("SETTINGS_PATH", "/data/settings.json"))
FUEL_PATH = Path(os.environ.get("FUEL_PATH", "/data/fuel.json"))
ADAPTIVE_PATH = Path(os.environ.get("ADAPTIVE_PATH", "/data/adaptive_model.json"))
HA_API = os.environ.get("HA_API_URL", "http://supervisor/core/api")
POLL_S = 1.0
REOPEN_S = 5.0


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
    settings = load_settings(SETTINGS_PATH, options)
    settings.save(SETTINGS_PATH)
    entities = settings.entity_ids()
    if entities:
        LOG.info("Room temperature sensors: %s", ", ".join(entities))
    else:
        LOG.warning("No room temperature sensor is set. Add it under Temperature entities and restart.")
    controller = Controller(settings)
    adaptive = AdaptiveController(ADAPTIVE_PATH, config_from_settings(settings))
    adaptive.set_manual_coolant(settings.manual_coolant_limit)
    fuel = FuelMeter(FUEL_PATH, settings.pump_dose_ml)
    if fuel.clamp_to_capacity(settings.tank_capacity_l):
        fuel.save()
    panel = PanelHub()
    start_panel(panel, int(os.environ.get("INGRESS_PORT", "8099")))
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
    if settings.debug:
        LOG.info("Debug log on")
    if settings.adaptive_enabled:
        LOG.info("Adaptive coolant control is enabled")

    link: SerialLink | None = None
    progress = LinkProgress()
    telemetry_log = TelemetryLog()
    debug_was_on = settings.debug
    announced_telemetry = False
    last_telemetry_log: float | None = None
    last_telemetry_mode = ""
    last_diag_log: float | None = None
    last_room: float | None = None
    last_room_at: float | None = None
    last_outdoor: float | None = None
    last_outdoor_at: float | None = None
    published: dict[str, str] = {}

    def handle_command(topic: str, payload: str, now: float) -> None:
        previous_adaptive = settings.adaptive_enabled
        previous_coolant = settings.coolant_limit
        action = apply_command(controller, settings, topic, payload, now, fuel, adaptive)
        if action == "reset-fuel":
            fuel.reset()
        if action == "reset-adaptive":
            adaptive.reset_learned()
        if settings.adaptive_enabled != previous_adaptive:
            result = adaptive.on_enabled_changed(settings.adaptive_enabled, previous_coolant)
            if settings.adaptive_enabled:
                settings.manual_coolant_limit = previous_coolant
                adaptive.set_manual_coolant(previous_coolant)
            _apply_adaptive_actions(result, controller, settings, adaptive, now)
        elif topic.endswith("/coolant_limit") and settings.adaptive_enabled:
            # User override while adaptive is on: keep learning paused for this cycle.
            if abs(settings.coolant_limit - previous_coolant) > 0.05:
                adaptive.note_manual_coolant_change()
                settings.manual_coolant_limit = settings.coolant_limit
        adaptive.configure(config_from_settings(settings))
        settings.save(SETTINGS_PATH)
        controller.settings = settings

    try:
        while True:
            started = time.monotonic()
            now = time.time()
            for topic, payload in panel.drain():
                handle_command(topic, payload, now)
            while pending:
                topic, payload = pending.pop(0)
                handle_command(topic, payload, now)
            room, last_room, last_room_at = _room(settings, token, now, last_room, last_room_at)
            outdoor, last_outdoor, last_outdoor_at = _outdoor(
                settings, token, now, last_outdoor, last_outdoor_at
            )
            age = None if last_room_at is None else now - last_room_at
            if link is None:
                try:
                    link = open_link(settings, serial_path, baud)
                    progress.opened(now)
                    if not settings.simulate:
                        LOG.info(
                            "Opened %s @ %s, DTR and RTS on, waiting for a status frame",
                            serial_path,
                            baud,
                        )
                except Exception as exc:  # serial port missing or busy
                    LOG.warning("Could not open the serial port: %s", exc)
                    _publish_offline(
                        client, controller, fuel, settings, last_room, published, panel, adaptive
                    )
                    time.sleep(REOPEN_S)
                    continue
            link.extended_telemetry = settings.extended_telemetry
            try:
                result = step(
                    now,
                    link,
                    controller,
                    settings,
                    fuel,
                    faults,
                    link_notice,
                    last_room,
                    age,
                    outdoor,
                    adaptive,
                )
            except Exception as exc:
                LOG.warning("Heater communication failed: %s", exc)
                if not settings.simulate and link is not None:
                    link.close()
                    link = None
                    progress.announced_online = False
                notice = link_notice.update(False)
                if notice:
                    deliver_notice(notice, token, HA_API)
                    client.publish(
                        f"{PREFIX}/event/link",
                        json.dumps({"online": False, "message": notice.message}),
                    )
                time.sleep(REOPEN_S)
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
            _log_link(progress, now, link.online(now), result.values.get("mode", ""), serial_path)
            if settings.debug and not debug_was_on:
                LOG.info("Debug log on")
            elif debug_was_on and not settings.debug:
                LOG.info("Debug log off")
            debug_was_on = settings.debug
            for level, text in telemetry_log.update(now, settings.debug, link):
                if level == "warning":
                    LOG.warning("Debug: %s", text)
                else:
                    LOG.info("Debug: %s", text)
            if link.last_extended_at == now and link.extended is not None:
                frame = link.extended
                mode_changed = frame.mode_name != last_telemetry_mode
                due = last_telemetry_log is None or now - last_telemetry_log >= 30
                if mode_changed or due or not announced_telemetry:
                    LOG.info(
                        "Telemetry frame: %s, %.2f V, pump %.2f Hz, flame %.0f °C",
                        frame.mode_name,
                        frame.voltage,
                        frame.pump_hz,
                        frame.flame_c,
                    )
                    last_telemetry_log = now
                    last_telemetry_mode = frame.mode_name
                announced_telemetry = True
            elif (
                link.last_handshake_at == now
                and link.last_parameter_at != now
                and (last_diag_log is None or now - last_diag_log >= 15)
            ):
                LOG.info("Diagnostic reports requested, no telemetry frame yet")
                last_diag_log = now
            elif announced_telemetry and link.telemetry_stale(now):
                LOG.warning("Telemetry frame stopped")
                announced_telemetry = False
            if result.settings_changed:
                settings.save(SETTINGS_PATH)
            panel.publish(result.values)
            for key, value in mqtt_state_updates(result.values, published).items():
                client.publish(state_topic(key), value, retain=True)
            if result.frequency_hz:
                LOG.debug(
                    "consumption %.3f L/h",
                    liters_per_hour(result.frequency_hz, settings.pump_dose_ml),
                )
            time.sleep(max(0.0, POLL_S - (time.monotonic() - started)))
    finally:
        if link is not None:
            link.close()
        client.publish(f"{PREFIX}/availability", "offline", retain=True)
        client.loop_stop()
        client.disconnect()


@dataclass
class CycleResult:
    values: dict[str, str]
    notices: list[Notice]
    frequency_hz: float
    settings_changed: bool = False


def _apply_adaptive_actions(
    actions,
    controller: Controller,
    settings: Settings,
    adaptive: AdaptiveController,
    now: float,
) -> bool:
    """Apply stop/start/coolant writes requested by adaptive control."""
    changed = False
    if actions.coolant_limit is not None:
        limit = max(20.0, min(95.0, float(actions.coolant_limit)))
        if abs(settings.coolant_limit - limit) >= 0.05:
            settings.coolant_limit = limit
            controller.settings = settings
            controller.note_device_setting("coolant_limit")
            changed = True
    if actions.stop:
        controller.adaptive_stop(now)
    if actions.start:
        controller.adaptive_resume(now)
    if actions.save_model:
        adaptive.save()
    return changed


def _log_link(progress: LinkProgress, now: float, online: bool, mode: str, serial_path: str) -> None:
    event = progress.update(now, online)
    if event == "answered":
        # Parameter bytes can mark the link up before a status frame exists.
        # "No connection" is that placeholder, not a heater mode.
        if not mode or mode == "No connection":
            progress.announced_online = False
            return
        LOG.info("Heater answered: %s", mode)
        progress.last_mode = mode
    elif event == "lost":
        progress.last_mode = ""
        LOG.warning("Heater stopped answering on %s", serial_path)
    elif online and mode and mode not in ("", "No connection") and mode != progress.last_mode:
        progress.last_mode = mode
        LOG.info("Heater status: %s", mode)
    elif event == "silent":
        LOG.warning("No status frame from the heater on %s", serial_path)


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
    outdoor_c: float | None = None,
    adaptive: AdaptiveController | None = None,
) -> CycleResult:
    """One control cycle against the previous status, then a fresh poll."""
    settings_changed = False
    if link.heater_settings is not None and link.heater_settings_at is not None:
        settings_changed = controller.accept_device_settings(
            link.heater_settings,
            link.heater_settings_at,
            phase_of(link.status),
        )
        if settings_changed:
            for key, value in vars(controller.settings).items():
                setattr(settings, key, value)
    port = link.port
    if settings.simulate and isinstance(port, SimulatedHeater):
        port.fault = int(settings.simulate_fault)

    if adaptive is not None:
        adaptive.configure(config_from_settings(settings))
        phase = phase_of(link.status)
        if link.extended is not None and (
            link.last_extended_at is not None
            and (link.last_status_at is None or link.last_extended_at > link.last_status_at)
        ):
            from .controller import _phase_from

            phase = _phase_from(link.extended.state, link.extended.substate)
        burner = bool(link.status.burner_active) if link.status is not None else False
        if link.extended is not None and phase == "running":
            # Extended frames do not carry burner_active; treat glow-off running
            # with zero pump as standby-like only when status says so.
            pass
        coolant = _coolant(link.status, link.extended, link.last_status_at, link.last_extended_at)
        fault = _fault(link.status, link.extended)
        frequency_pre = link.extended.pump_hz if link.extended is not None else 0.0
        fresh_room = room_c is not None and room_age_s is not None and room_age_s <= settings.sensor_stale_s
        sensors_ok = fresh_room and outdoor_c is not None and coolant is not None and fault == 0
        # Outdoor may be optional for steering when a prior value exists; still
        # require a room reading and no fault before active corrections.
        if outdoor_c is None and adaptive.model.outdoor_history:
            outdoor_c = adaptive.model.outdoor_history[-1][1]
        actions = adaptive.tick(
            now,
            phase=phase,
            burner_active=burner,
            fault=fault,
            heat_enabled=controller.heat_enabled,
            mode=controller.mode,
            room_c=room_c if fresh_room else None,
            outdoor_c=outdoor_c,
            coolant_c=coolant,
            pump_hz=frequency_pre,
            room_target=settings.room_target,
            hysteresis_c=settings.room_drop,
            current_coolant_limit=settings.coolant_limit,
            sensors_ok=sensors_ok or (fresh_room and fault == 0 and coolant is not None),
        )
        if _apply_adaptive_actions(actions, controller, settings, adaptive, now):
            settings_changed = True

    output = controller.tick(
        now,
        link.status,
        link.extended,
        room_c,
        room_age_s,
        link.last_status_at,
        link.last_extended_at,
    )
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
    fan_on = output.fan_on
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
        status_at=link.last_status_at,
        extended_at=link.last_extended_at,
        outdoor_c=outdoor_c,
        adaptive=adaptive,
    )
    return CycleResult(
        values=values,
        notices=notices,
        frequency_hz=frequency,
        settings_changed=settings_changed,
    )


def _room(
    settings: Settings,
    token: str,
    now: float,
    last_room: float | None,
    last_room_at: float | None,
) -> tuple[float | None, float | None, float | None]:
    entities = settings.entity_ids()
    if settings.simulate and not entities:
        return settings.simulate_room_c, settings.simulate_room_c, now
    readings = [read_temperature(entity_id, token, HA_API) for entity_id in entities]
    current = aggregate(readings, settings.aggregation)
    if current is None:
        if entities and (_room_warned_at is None or now - _room_warned_at >= 60):
            LOG.warning("Room temperature sensors did not return a number: %s", ", ".join(entities))
            _remember_room_warning(now)
        return last_room, last_room, last_room_at
    _remember_room_warning(None)
    if last_room is None or round(current, 1) != round(last_room, 1):
        LOG.info("Room temperature %.1f °C from %s", current, ", ".join(entities))
    return current, current, now


def _outdoor(
    settings: Settings,
    token: str,
    now: float,
    last_outdoor: float | None,
    last_outdoor_at: float | None,
) -> tuple[float | None, float | None, float | None]:
    entity = settings.outdoor_entity_id()
    if settings.simulate and entity is None:
        return settings.simulate_outdoor_c, settings.simulate_outdoor_c, now
    if entity is None:
        return last_outdoor, last_outdoor, last_outdoor_at
    current = read_temperature(entity, token, HA_API)
    if current is None:
        if _outdoor_warned_at is None or now - _outdoor_warned_at >= 60:
            LOG.warning("Outdoor temperature sensor did not return a number: %s", entity)
            _remember_outdoor_warning(now)
        return last_outdoor, last_outdoor, last_outdoor_at
    _remember_outdoor_warning(None)
    if last_outdoor is None or round(current, 1) != round(last_outdoor, 1):
        LOG.info("Outdoor temperature %.1f °C from %s", current, entity)
    return current, current, now


_room_warned_at: float | None = None
_outdoor_warned_at: float | None = None


def _remember_room_warning(when: float | None) -> None:
    global _room_warned_at
    _room_warned_at = when


def _remember_outdoor_warning(when: float | None) -> None:
    global _outdoor_warned_at
    _outdoor_warned_at = when


def _current_fault(link: SerialLink) -> int:
    if link.status is not None and link.status.fault:
        return link.status.fault
    if link.extended is not None:
        return link.extended.fault
    if link.status is not None:
        return link.status.fault
    return 0


def _publish_offline(
    client,
    controller,
    fuel,
    settings,
    room,
    published: dict[str, str],
    panel: PanelHub | None = None,
    adaptive: AdaptiveController | None = None,
) -> None:
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
        adaptive=adaptive,
    )
    if panel is not None:
        panel.publish(values)
    for key, value in mqtt_state_updates(values, published).items():
        client.publish(state_topic(key), value, retain=True)


if __name__ == "__main__":
    run()
