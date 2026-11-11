"""MQTT discovery payloads and command routing."""

from __future__ import annotations

import json
from typing import Any

from .controller import Controller, _phase_from, phase_of
from .fuel import FuelMeter
from .protocol import EXTENDED_LEN, EXTENDED_MAPPED, ExtendedFrame, StatusFrame
from .settings import Settings
from .faults import fault_name

PREFIX = "autoterm_flow5"
DEVICE = {
    "identifiers": [PREFIX],
    "name": "Autoterm Flow 5",
    "manufacturer": "Autoterm",
    "model": "Flow 5",
}


def state_topic(key: str) -> str:
    return f"{PREFIX}/state/{key}"


def set_topic(key: str) -> str:
    return f"{PREFIX}/set/{key}"


def _sensor(
    key: str,
    name: str,
    *,
    unit: str | None = None,
    device_class: str | None = None,
    state_class: str | None = None,
    diagnostic: bool = False,
    icon: str | None = None,
    precision: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "unique_id": f"{PREFIX}_{key}",
        "state_topic": state_topic(key),
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
    }
    if unit:
        payload["unit_of_measurement"] = unit
    if device_class:
        payload["device_class"] = device_class
    if state_class:
        payload["state_class"] = state_class
    if diagnostic:
        payload["entity_category"] = "diagnostic"
    if icon:
        payload["icon"] = icon
    if precision is not None:
        payload["suggested_display_precision"] = precision
    return {"component": "sensor", "key": key, "payload": payload}


def _binary(key: str, name: str, *, device_class: str | None = None, diagnostic: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "unique_id": f"{PREFIX}_{key}",
        "state_topic": state_topic(key),
        "payload_on": "ON",
        "payload_off": "OFF",
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
    }
    if device_class:
        payload["device_class"] = device_class
    if diagnostic:
        payload["entity_category"] = "diagnostic"
    return {"component": "binary_sensor", "key": key, "payload": payload}


def _number(
    key: str,
    name: str,
    minimum: float,
    maximum: float,
    step: float,
    unit: str | None = None,
    mode: str = "box",
    category: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "unique_id": f"{PREFIX}_{key}",
        "command_topic": set_topic(key),
        "state_topic": state_topic(key),
        "min": minimum,
        "max": maximum,
        "step": step,
        "mode": mode,
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
    }
    if unit:
        payload["unit_of_measurement"] = unit
    if category:
        payload["entity_category"] = category
    return {"component": "number", "key": key, "payload": payload}


def discovery_messages() -> list[tuple[str, str]]:
    entities: list[dict[str, Any]] = [
        _sensor("mode", "Operating mode"),
        _sensor("phase", "Control phase", diagnostic=True),
        _sensor("state_code", "State code", diagnostic=True),
        _sensor("substate_code", "Substate code", diagnostic=True),
        _sensor("fault_code", "Fault code"),
        _sensor("fault_text", "Fault text"),
        _binary("fault_active", "Fault", device_class="problem"),
        _sensor("coolant", "Coolant", unit="°C", device_class="temperature", state_class="measurement"),
        _sensor("coolant_a", "Coolant A", unit="°C", device_class="temperature", diagnostic=True),
        _sensor("coolant_b", "Coolant B", unit="°C", device_class="temperature", diagnostic=True),
        _sensor("voltage_like", "Voltage-like byte", diagnostic=True),
        _sensor("rising_sensor", "Rising sensor", unit="°C", diagnostic=True),
        _sensor("elapsed_minutes", "Elapsed minutes", unit="min", diagnostic=True),
        _sensor("elapsed_seconds", "Elapsed seconds", unit="s", diagnostic=True),
        _binary("burner", "Burner"),
        _sensor("cooldown_counter", "Cooldown counter", diagnostic=True),
        _sensor("status_hex", "Status frame hex", diagnostic=True),
        _sensor("ext_mode", "Extended mode", diagnostic=True),
        _sensor("runtime", "Runtime", unit="s", state_class="measurement", diagnostic=True),
        _sensor("fan_target", "Fan target", diagnostic=True),
        _sensor("fan_actual", "Fan measured", diagnostic=True),
        _binary("glow_plug", "Glow plug", diagnostic=True),
        _sensor("pump_hz", "Pump frequency", unit="Hz", state_class="measurement"),
        _sensor("flame", "Flame temperature", unit="°C", device_class="temperature", state_class="measurement"),
        _sensor("liquid", "Extended liquid temperature", unit="°C", device_class="temperature", diagnostic=True),
        _sensor("overheat", "Overheat sensor", unit="°C", device_class="temperature", state_class="measurement"),
        _sensor("board", "Board temperature", unit="°C", device_class="temperature", diagnostic=True),
        _sensor("voltage", "Voltage", unit="V", device_class="voltage", state_class="measurement"),
        _sensor("ext_fault_code", "Extended fault code", diagnostic=True),
        _sensor("engine_state", "Engine state", diagnostic=True),
        _sensor("relay_state", "Relay", diagnostic=True),
        _sensor("fan_current", "Fan current", unit="mA", diagnostic=True),
        _sensor("ext_hex", "Extended frame hex", diagnostic=True),
        _sensor("fuel_lph", "Fuel consumption", unit="L/h", state_class="measurement", icon="mdi:fuel"),
        _sensor("fuel_avg_lph", "Fuel hour average", unit="L/h", state_class="measurement", icon="mdi:fuel", precision=3),
        _sensor("fuel_ml_min", "Fuel consumption per minute", unit="mL/min", state_class="measurement", diagnostic=True),
        _sensor("fuel_total_l", "Fuel total", unit="L", state_class="total_increasing", icon="mdi:fuel"),
        _sensor("fuel_total_ml", "Fuel total ml", unit="mL", state_class="total_increasing", diagnostic=True),
        _sensor("fuel_level_percent", "Fuel level", unit="%", state_class="measurement", icon="mdi:gauge", precision=0),
        _sensor("fuel_remaining_l", "Fuel remaining", unit="L", state_class="measurement", icon="mdi:fuel", precision=2),
        _sensor(
            "fuel_hours",
            "Fuel time remaining",
            unit="h",
            device_class="duration",
            state_class="measurement",
            icon="mdi:timer-sand",
            precision=1,
        ),
        _sensor("room", "Control temperature", unit="°C", device_class="temperature", state_class="measurement"),
        _binary("demand", "Heat demand"),
        _binary("link", "Link", device_class="connectivity"),
        _binary("telemetry_stale", "Telemetry stale", device_class="problem", diagnostic=True),
        _binary("fan_request", "Interior fan"),
        _binary("lockout", "Lockout", device_class="problem"),
        _number("coolant_limit", "Coolant limit", 20, 95, 1, "°C"),
        _number("auxiliary_limit", "Auxiliary standby temperature", 80, 95, 1, "°C"),
        _number("sensor_stale", "Sensor timeout", 1, 120, 1, "min"),
        _number("shutdown_voltage", "Shutdown voltage", 9, 30, 0.1, "V"),
        _number("shutdown_voltage_seconds", "Shutdown voltage time", 1, 600, 1, "s"),
        _number("fan_temperature", "Fan start temperature", 30, 60, 1, "°C"),
        _number("pump_dose", "Pump dose", 0.01, 0.2, 0.001, "mL"),
        _number("tank_capacity", "Tank capacity", 0, 500, 0.5, "L"),
        _number("fuel_level", "Fuel in tank", 0, 500, 0.1, "L"),
        _number("preheat_minutes", "Preheat duration", 1, 480, 1, "min"),
        _number("run_minutes", "Run time (0 unlimited)", 0, 720, 1, "min"),
        _number("simulate_room", "Simulated room temperature", 0, 40, 0.5, "°C", category="config"),
        _number("simulate_outdoor", "Simulated outdoor temperature", -40, 40, 0.5, "°C", category="config"),
        _number("simulate_fault", "Simulated fault code", 0, 255, 1, category="config"),
        _binary("simulation", "Simulation", diagnostic=True),
        _sensor("outdoor", "Outdoor temperature", unit="°C", device_class="temperature", state_class="measurement"),
        _sensor("adaptive_status", "Adaptive control status"),
        _sensor("adaptive_mode", "Adaptive operating mode", diagnostic=True),
        _sensor("adaptive_coolant_target", "Adaptive coolant target", unit="°C", device_class="temperature", diagnostic=True),
        _sensor("adaptive_actual_coolant", "Confirmed coolant target", unit="°C", device_class="temperature", diagnostic=True),
        _sensor("adaptive_ua", "Adaptive UA", unit="kW/°C", diagnostic=True, precision=4),
        _sensor("adaptive_c", "Adaptive radiator C", diagnostic=True, precision=5),
        _sensor("adaptive_ignitions_day", "Ignitions per day", state_class="measurement", diagnostic=True),
        _sensor("adaptive_avg_run_s", "Average run time", unit="s", state_class="measurement", diagnostic=True),
        _sensor("adaptive_room_error", "Room temperature error", unit="°C", state_class="measurement", diagnostic=True),
        _sensor("adaptive_k_table", "Adaptive K table", diagnostic=True),
        _number("adaptive_coolant_min", "Adaptive coolant minimum", 20, 95, 1, "°C", category="config"),
        _number("adaptive_coolant_max", "Adaptive coolant maximum", 20, 95, 1, "°C", category="config"),
        _number("room_drop", "Room hysteresis", 0, 10, 0.5, "°C", category="config"),
        _number("room_rise", "Stop above target", 0, 10, 0.5, "°C", category="config"),
    ]
    for index in (5, 8, 10, 13, 14, 15, 17):
        entities.append(_sensor(f"status_byte_{index}", f"Status byte {index}", diagnostic=True))
    for index in range(EXTENDED_LEN):
        if index not in EXTENDED_MAPPED:
            entities.append(_sensor(f"ext_byte_{index}", f"Extended byte {index}", diagnostic=True))

    messages = []
    for entity in entities:
        topic = f"homeassistant/{entity['component']}/{PREFIX}/{entity['key']}/config"
        messages.append((topic, json.dumps(entity["payload"], ensure_ascii=False)))

    messages.append((f"homeassistant/switch/{PREFIX}/power/config", json.dumps({
        "name": "Heating",
        "unique_id": f"{PREFIX}_power",
        "command_topic": set_topic("power"),
        "state_topic": state_topic("power"),
        "payload_on": "ON",
        "payload_off": "OFF",
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
    })))
    messages.append((f"homeassistant/button/{PREFIX}/stop/config", json.dumps({
        "name": "Stop",
        "unique_id": f"{PREFIX}_stop",
        "command_topic": set_topic("stop"),
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
        "icon": "mdi:stop",
    })))
    messages.append((f"homeassistant/button/{PREFIX}/fuel_reset/config", json.dumps({
        "name": "Reset fuel total",
        "unique_id": f"{PREFIX}_fuel_reset",
        "command_topic": set_topic("fuel_reset"),
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
        "entity_category": "config",
    })))
    messages.append((f"homeassistant/button/{PREFIX}/fuel_refuel/config", json.dumps({
        "name": "Tank filled",
        "unique_id": f"{PREFIX}_fuel_refuel",
        "command_topic": set_topic("fuel_refuel"),
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
        "icon": "mdi:gas-station",
    })))
    messages.append((f"homeassistant/button/{PREFIX}/adaptive_reset/config", json.dumps({
        "name": "Reset adaptive learning",
        "unique_id": f"{PREFIX}_adaptive_reset",
        "command_topic": set_topic("adaptive_reset"),
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
        "entity_category": "config",
        "icon": "mdi:brain",
    })))
    messages.append((f"homeassistant/switch/{PREFIX}/adaptive_enabled/config", json.dumps({
        "name": "Adaptive coolant control",
        "unique_id": f"{PREFIX}_adaptive_enabled",
        "command_topic": set_topic("adaptive_enabled"),
        "state_topic": state_topic("adaptive_enabled"),
        "payload_on": "ON",
        "payload_off": "OFF",
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
        "icon": "mdi:chart-timeline-variant-shimmer",
    })))
    messages.append((f"homeassistant/switch/{PREFIX}/room_control/config", json.dumps({
        "name": "Add-on thermostat",
        "unique_id": f"{PREFIX}_room_control",
        "command_topic": set_topic("room_control"),
        "state_topic": state_topic("room_control"),
        "payload_on": "ON",
        "payload_off": "OFF",
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
        "entity_category": "config",
        "icon": "mdi:thermostat-auto",
    })))
    messages.append((f"homeassistant/select/{PREFIX}/mode/config", json.dumps({
        "name": "Mode",
        "unique_id": f"{PREFIX}_mode",
        "command_topic": set_topic("mode"),
        "state_topic": state_topic("mode_select"),
        "options": ["thermostat", "preheat", "pump"],
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
    })))
    messages.append((f"homeassistant/select/{PREFIX}/auxiliary_mode/config", json.dumps({
        "name": "Auxiliary heater",
        "unique_id": f"{PREFIX}_auxiliary_mode",
        "command_topic": set_topic("auxiliary_mode"),
        "state_topic": state_topic("auxiliary_mode"),
        "options": ["off", "auto", "manual"],
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
    })))
    messages.append((f"homeassistant/select/{PREFIX}/relay_mode/config", json.dumps({
        "name": "Cabin heater relay",
        "unique_id": f"{PREFIX}_relay_mode",
        "command_topic": set_topic("relay_mode"),
        "state_topic": state_topic("relay_mode"),
        "options": ["auto", "off"],
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
    })))
    messages.append((f"homeassistant/select/{PREFIX}/pump_signal/config", json.dumps({
        "name": "Pump signal",
        "unique_id": f"{PREFIX}_pump_signal",
        "command_topic": set_topic("pump_signal"),
        "state_topic": state_topic("pump_signal"),
        "options": ["plain", "pwm"],
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
    })))
    for key, name in (
        ("pump_with_engine", "Pump with engine"),
        ("pump_standby", "Pump in standby"),
        ("alarm_channel", "Alarm channel"),
    ):
        messages.append((f"homeassistant/switch/{PREFIX}/{key}/config", json.dumps({
            "name": name,
            "unique_id": f"{PREFIX}_{key}",
            "command_topic": set_topic(key),
            "state_topic": state_topic(key),
            "payload_on": "ON",
            "payload_off": "OFF",
            "availability_topic": f"{PREFIX}/availability",
            "device": DEVICE,
        })))
    messages.append((f"homeassistant/select/{PREFIX}/aggregation/config", json.dumps({
        "name": "Temperature aggregation",
        "unique_id": f"{PREFIX}_aggregation",
        "command_topic": set_topic("aggregation"),
        "state_topic": state_topic("aggregation"),
        "options": ["min", "avg", "max"],
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
        "entity_category": "config",
    })))
    messages.append((f"homeassistant/switch/{PREFIX}/debug/config", json.dumps({
        "name": "Debug log",
        "unique_id": f"{PREFIX}_debug",
        "command_topic": set_topic("debug"),
        "state_topic": state_topic("debug"),
        "payload_on": "ON",
        "payload_off": "OFF",
        "icon": "mdi:bug",
        "entity_category": "config",
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
    })))
    messages.append((f"homeassistant/switch/{PREFIX}/circulation/config", json.dumps({
        "name": "Circulation pump while waiting",
        "unique_id": f"{PREFIX}_circulation",
        "command_topic": set_topic("circulation"),
        "state_topic": state_topic("circulation"),
        "payload_on": "ON",
        "payload_off": "OFF",
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
    })))
    messages.append((f"homeassistant/climate/{PREFIX}/climate/config", json.dumps({
        "name": "Autoterm Flow 5",
        "unique_id": f"{PREFIX}_climate",
        "modes": ["off", "heat"],
        "mode_command_topic": set_topic("climate_mode"),
        "mode_state_topic": state_topic("climate_mode"),
        "temperature_command_topic": set_topic("room_target"),
        "temperature_state_topic": state_topic("room_target"),
        "current_temperature_topic": state_topic("room"),
        "action_topic": state_topic("climate_action"),
        "min_temp": 0,
        "max_temp": 40,
        "temp_step": 0.5,
        "precision": 0.1,
        "temperature_unit": "C",
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
    })))
    return messages


def _climate_action(controller: Controller) -> str:
    if not controller.heat_enabled or controller.mode != "thermostat":
        return "off"
    if controller.phase in ("starting", "running"):
        return "heating"
    return "idle"


def collect_state(
    status: StatusFrame | None,
    extended: ExtendedFrame | None,
    *,
    fuel: FuelMeter,
    settings: Settings,
    controller: Controller,
    room_c: float | None,
    online: bool,
    telemetry_stale: bool,
    fan_on: bool,
    frequency_hz: float,
    status_at: float | None = None,
    extended_at: float | None = None,
    outdoor_c: float | None = None,
    adaptive: Any | None = None,
) -> dict[str, str]:
    from .fuel import liters_per_hour, milliliters_per_minute

    fault = 0
    if status is not None and status.fault:
        fault = status.fault
    elif extended is not None:
        fault = extended.fault
    newer_extended = (
        extended is not None
        and extended_at is not None
        and (status_at is None or extended_at > status_at)
    )
    if newer_extended:
        mode_name = extended.mode_name
        phase_name = _phase_from(extended.state, extended.substate)
    else:
        mode_name = status.mode_name if status else "No connection"
        phase_name = phase_of(status)
    values: dict[str, Any] = {
        "mode": mode_name,
        "phase": phase_name,
        "state_code": status.state if status else "",
        "substate_code": status.substate if status else "",
        "fault_code": fault,
        "fault_text": fault_name(fault),
        "fault_active": "ON" if fault else "OFF",
        "coolant": _num(_coolant_c(status, extended)),
        "coolant_a": status.coolant_a if status else "",
        "coolant_b": status.coolant_b if status else "",
        "voltage_like": status.voltage_like if status else "",
        "rising_sensor": status.rising_sensor if status else "",
        "elapsed_minutes": status.elapsed_minutes if status else "",
        "elapsed_seconds": status.elapsed_seconds if status else "",
        "burner": "ON" if status and status.burner_active else "OFF",
        "cooldown_counter": status.cooldown_counter if status else "",
        "status_hex": status.hex if status else "",
        "ext_mode": extended.mode_name if extended else "",
        "runtime": extended.running_time_s if extended else "",
        "fan_target": extended.fan_target if extended else "",
        "fan_actual": extended.fan_actual if extended else "",
        "glow_plug": "ON" if extended and extended.glow_plug else "OFF",
        "pump_hz": _num(frequency_hz),
        "flame": _num(extended.flame_c if extended else None),
        "liquid": extended.liquid_c if extended else "",
        "overheat": extended.overheat_c if extended else "",
        "board": extended.board_c if extended else "",
        "voltage": _num(extended.voltage if extended else None),
        "ext_fault_code": extended.fault if extended else "",
        "engine_state": extended.engine_state if extended else "",
        "relay_state": extended.relay_state if extended else "",
        "fan_current": extended.fan_current if extended else "",
        "ext_hex": extended.hex if extended else "",
        "fuel_lph": f"{liters_per_hour(frequency_hz, settings.pump_dose_ml):.4f}",
        "fuel_avg_lph": f"{fuel.average_lph():.4f}",
        "fuel_ml_min": f"{milliliters_per_minute(frequency_hz, settings.pump_dose_ml):.4f}",
        "fuel_total_l": f"{fuel.total_liters:.4f}",
        "fuel_total_ml": f"{fuel.total_ml:.4f}",
        "fuel_level_percent": _num(fuel.level_percent(settings.tank_capacity_l)),
        "fuel_remaining_l": _num(fuel.remaining_liters),
        "fuel_hours": _num(fuel.hours_remaining()),
        "tank_capacity": settings.tank_capacity_l,
        "fuel_level": _num(fuel.remaining_liters),
        "room": _num(room_c),
        "outdoor": _num(outdoor_c),
        "demand": "ON" if controller.demand else "OFF",
        "link": "ON" if online else "OFF",
        "telemetry_stale": "ON" if telemetry_stale else "OFF",
        "fan_request": "ON" if fan_on else "OFF",
        "lockout": "ON" if controller.lockout else "OFF",
        "power": "ON" if controller.heat_enabled else "OFF",
        "mode_select": controller.mode,
        "climate_mode": "heat" if controller.heat_enabled and controller.mode == "thermostat" else "off",
        "climate_action": _climate_action(controller),
        "aggregation": settings.aggregation,
        "circulation": "ON" if settings.circulation_pump else "OFF",
        "coolant_limit": settings.coolant_limit,
        "auxiliary_mode": settings.auxiliary_mode,
        "auxiliary_limit": settings.auxiliary_limit,
        "relay_mode": settings.relay_mode,
        "pump_signal": settings.pump_signal,
        "pump_with_engine": "ON" if settings.pump_with_engine else "OFF",
        "pump_standby": "ON" if settings.pump_standby else "OFF",
        "alarm_channel": "ON" if settings.alarm_channel else "OFF",
        "room_target": settings.room_target,
        "room_drop": settings.room_drop,
        "room_rise": settings.room_rise,
        "room_control": "ON" if settings.room_control else "OFF",
        "sensor_stale": settings.sensor_stale_s / 60,
        "shutdown_voltage": settings.shutdown_voltage,
        "shutdown_voltage_seconds": settings.shutdown_voltage_s,
        "fan_temperature": settings.fan_temperature,
        "pump_dose": settings.pump_dose_ml,
        "preheat_minutes": settings.preheat_minutes,
        "run_minutes": settings.run_minutes,
        "simulate_room": settings.simulate_room_c,
        "simulate_outdoor": settings.simulate_outdoor_c,
        "simulate_fault": settings.simulate_fault,
        "simulation": "ON" if settings.simulate else "OFF",
        "debug": "ON" if settings.debug else "OFF",
        "adaptive_enabled": "ON" if settings.adaptive_enabled else "OFF",
        "adaptive_coolant_min": settings.adaptive_coolant_min,
        "adaptive_coolant_max": settings.adaptive_coolant_max,
    }
    if adaptive is not None:
        values.update(adaptive.status_values())
    else:
        values.setdefault("adaptive_status", "off")
        values.setdefault("adaptive_mode", "")
        values.setdefault("adaptive_coolant_target", "")
        values.setdefault("adaptive_actual_coolant", "")
        values.setdefault("adaptive_ua", "")
        values.setdefault("adaptive_c", "")
        values.setdefault("adaptive_ignitions_day", "")
        values.setdefault("adaptive_avg_run_s", "")
        values.setdefault("adaptive_room_error", "")
        values.setdefault("adaptive_k_table", "")
        values.setdefault("adaptive_reliable", "OFF")
        values.setdefault("adaptive_coolant_drop", "")
    if status is not None:
        for index, value in status.unmapped.items():
            values[f"status_byte_{index}"] = value
    if extended is not None:
        for index, value in extended.unmapped.items():
            values[f"ext_byte_{index}"] = value
    return {key: "" if value is None else str(value) for key, value in values.items()}


def apply_command(
    controller: Controller,
    settings: Settings,
    topic: str,
    payload: str,
    now: float,
    fuel: FuelMeter | None = None,
    adaptive: Any | None = None,
) -> str | None:
    """Apply one retained or live command. Return a special action name when asked."""
    del adaptive
    key = topic.removeprefix(f"{PREFIX}/set/")
    text = payload.strip()
    previous = getattr(settings, key, None)
    if key == "power":
        controller.set_heat(text == "ON", now)
    elif key == "stop":
        controller.request_stop(now)
    elif key == "fuel_reset":
        return "reset-fuel"
    elif key == "adaptive_reset":
        return "reset-adaptive"
    elif key == "fuel_refuel":
        if fuel is not None and fuel.refuel(settings.tank_capacity_l):
            fuel.save()
    elif key == "fuel_level":
        if fuel is None:
            return None
        try:
            fuel.set_level_liters(float(text), settings.tank_capacity_l)
        except ValueError:
            return None
        fuel.save()
    elif key == "mode" and text in {"thermostat", "preheat", "pump"}:
        controller.set_mode(text)
    elif key == "climate_mode":
        if text == "heat":
            controller.set_mode("thermostat")
            controller.set_heat(True, now)
        elif text == "off":
            controller.set_heat(False, now)
    elif key == "aggregation" and text in {"min", "avg", "max"}:
        settings.aggregation = text
    elif key == "circulation":
        settings.circulation_pump = text == "ON"
    elif key == "debug":
        settings.debug = text == "ON"
    elif key == "adaptive_enabled":
        settings.adaptive_enabled = text == "ON"
    elif key == "room_control":
        settings.room_control = text == "ON"
    elif key == "auxiliary_mode" and text in {"off", "auto", "manual"}:
        settings.auxiliary_mode = text
    elif key == "relay_mode" and text in {"auto", "off"}:
        settings.relay_mode = text
    elif key == "pump_signal" and text in {"plain", "pwm"}:
        settings.pump_signal = text
    elif key == "pump_with_engine":
        settings.pump_with_engine = text == "ON"
    elif key == "pump_standby":
        settings.pump_standby = text == "ON"
    elif key == "alarm_channel":
        settings.alarm_channel = text == "ON"
    else:
        try:
            _apply_number(settings, key, text)
        except ValueError:
            return None
    settings.heat_on = controller.heat_enabled
    settings.control_mode = controller.mode
    controller.settings = settings.sanitized()
    for field_name, value in vars(controller.settings).items():
        setattr(settings, field_name, value)
    if getattr(settings, key, None) != previous:
        controller.note_device_setting(key)
    if fuel is not None and fuel.clamp_to_capacity(settings.tank_capacity_l):
        fuel.save()
    return None


def _apply_number(settings: Settings, key: str, text: str) -> None:
    if key == "room_target":
        settings.room_target = float(text)
    elif key == "room_drop":
        settings.room_drop = float(text)
    elif key == "room_rise":
        settings.room_rise = float(text)
    elif key == "sensor_stale":
        settings.sensor_stale_s = float(text) * 60
    elif key == "coolant_limit":
        settings.coolant_limit = float(text)
    elif key == "auxiliary_limit":
        settings.auxiliary_limit = float(text)
    elif key == "shutdown_voltage":
        settings.shutdown_voltage = float(text)
    elif key == "shutdown_voltage_seconds":
        settings.shutdown_voltage_s = float(text)
    elif key == "fan_temperature":
        settings.fan_temperature = float(text)
    elif key == "pump_dose":
        settings.pump_dose_ml = float(text)
    elif key == "tank_capacity":
        settings.tank_capacity_l = float(text)
    elif key == "preheat_minutes":
        settings.preheat_minutes = int(float(text))
    elif key == "run_minutes":
        settings.run_minutes = int(float(text))
    elif key == "simulate_room":
        settings.simulate_room_c = float(text)
    elif key == "simulate_outdoor":
        settings.simulate_outdoor_c = float(text)
    elif key == "simulate_fault":
        settings.simulate_fault = int(float(text))
    elif key == "adaptive_coolant_min":
        settings.adaptive_coolant_min = float(text)
    elif key == "adaptive_coolant_max":
        settings.adaptive_coolant_max = float(text)
    else:
        raise ValueError(key)


def _coolant_c(status: StatusFrame | None, extended: ExtendedFrame | None) -> float | None:
    """Coolant temperature for Home Assistant.

    The short status frame has it, but that frame is not polled once the
    parameter stream is running. The stream's liquid byte is the same
    temperature the diagnostics page shows as the liquid sensor.
    """
    if status is not None:
        return status.coolant_c
    if extended is not None:
        return float(extended.liquid_c)
    return None


def mqtt_state_updates(values: dict[str, str], previous: dict[str, str] | None = None) -> dict[str, str]:
    """States that changed to a real value.

    An empty payload makes a Home Assistant measurement unknown, and
    repeating every topic once a second crowds out the values that did change.
    """
    updates: dict[str, str] = {}
    for key, value in values.items():
        if value == "":
            continue
        if previous is not None and previous.get(key) == value:
            continue
        updates[key] = value
    if previous is not None:
        previous.update(updates)
    return updates


def _num(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}"
