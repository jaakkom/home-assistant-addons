"""MQTT discovery payloads and command routing."""

from __future__ import annotations

import json
from typing import Any

from .controller import Controller
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
    return {"component": "number", "key": key, "payload": payload}


def discovery_messages() -> list[tuple[str, str]]:
    entities: list[dict[str, Any]] = [
        _sensor("mode", "Toimintatila"),
        _sensor("phase", "Ohjauksen vaihe", diagnostic=True),
        _sensor("state_code", "Tilan koodi", diagnostic=True),
        _sensor("substate_code", "Alitilan koodi", diagnostic=True),
        _sensor("fault_code", "Vikakoodi"),
        _sensor("fault_text", "Vikateksti"),
        _binary("fault_active", "Vika", device_class="problem"),
        _sensor("coolant", "Jäähdytysneste", unit="°C", device_class="temperature", state_class="measurement"),
        _sensor("coolant_a", "Jäähdytysneste A", unit="°C", device_class="temperature", diagnostic=True),
        _sensor("coolant_b", "Jäähdytysneste B", unit="°C", device_class="temperature", diagnostic=True),
        _sensor("voltage_like", "Jännitemainen tavu", diagnostic=True),
        _sensor("rising_sensor", "Nouseva anturi", unit="°C", diagnostic=True),
        _sensor("elapsed_minutes", "Käyntiminuutit", unit="min", diagnostic=True),
        _sensor("elapsed_seconds", "Käyntisekunnit", unit="s", diagnostic=True),
        _binary("burner", "Poltin"),
        _sensor("cooldown_counter", "Jäähtymislaskuri", diagnostic=True),
        _sensor("status_hex", "Tilakehyksen heksa", diagnostic=True),
        _sensor("ext_mode", "Laajennettu tila", diagnostic=True),
        _sensor("runtime", "Käyntiaika", unit="s", state_class="measurement", diagnostic=True),
        _sensor("fan_target", "Puhaltimen tavoite", diagnostic=True),
        _sensor("fan_actual", "Puhaltimen mitattu", diagnostic=True),
        _binary("glow_plug", "Hehkutulppa", diagnostic=True),
        _sensor("pump_hz", "Pumpun taajuus", unit="Hz", state_class="measurement"),
        _sensor("flame", "Liekin lämpö", unit="°C", device_class="temperature", state_class="measurement"),
        _sensor("liquid", "Neste laajennetusta", unit="°C", device_class="temperature", diagnostic=True),
        _sensor("overheat", "Ylikuumenemisanturi", unit="°C", device_class="temperature", state_class="measurement"),
        _sensor("board", "Piirilevyn lämpö", unit="°C", device_class="temperature", diagnostic=True),
        _sensor("voltage", "Jännite", unit="V", device_class="voltage", state_class="measurement"),
        _sensor("ext_fault_code", "Laajennettu vikakoodi", diagnostic=True),
        _sensor("engine_state", "Moottorin tila", diagnostic=True),
        _sensor("relay_state", "Rele", diagnostic=True),
        _sensor("fan_current", "Puhaltimen virta", unit="mA", diagnostic=True),
        _sensor("ext_hex", "Laajennetun kehyksen heksa", diagnostic=True),
        _sensor("fuel_lph", "Kulutus", unit="L/h", state_class="measurement", icon="mdi:fuel"),
        _sensor("fuel_ml_min", "Kulutus minuutissa", unit="mL/min", state_class="measurement", diagnostic=True),
        _sensor("fuel_total_l", "Polttoainekertymä", unit="L", state_class="total_increasing", icon="mdi:fuel"),
        _sensor("fuel_total_ml", "Polttoainekertymä ml", unit="mL", state_class="total_increasing", diagnostic=True),
        _sensor("room", "Ohjauslämpötila", unit="°C", device_class="temperature", state_class="measurement"),
        _binary("demand", "Lämmönpyyntö"),
        _binary("link", "Yhteys", device_class="connectivity"),
        _binary("telemetry_stale", "Telemetria vanhentunut", device_class="problem", diagnostic=True),
        _binary("fan_request", "Sisäpuhallin"),
        _binary("lockout", "Lukitus", device_class="problem"),
        _number("coolant_limit", "Nesteen yläraja", 20, 95, 1, "°C"),
        _number("coolant_drop", "Nesteen pudotus", 1, 40, 1, "°C"),
        _number("room_drop", "Huoneen pudotus", 0, 10, 0.5, "°C"),
        _number("room_rise", "Huoneen nousu", 0, 10, 0.5, "°C"),
        _number("min_on", "Vähimmäiskäynti", 0, 120, 1, "min"),
        _number("min_off", "Vähimmäisseisonta", 0, 120, 1, "min"),
        _number("sensor_stale", "Anturin vanheneminen", 1, 120, 1, "min"),
        _number("shutdown_voltage", "Sammutusjännite", 9, 30, 0.1, "V"),
        _number("shutdown_voltage_seconds", "Sammutusjännitteen aika", 1, 600, 1, "s"),
        _number("fan_temperature", "Puhaltimen käynnistyslämpö", 30, 60, 1, "°C"),
        _number("pump_dose", "Pumpun annos", 0.01, 0.2, 0.001, "mL"),
        _number("preheat_minutes", "Esilämmityksen kesto", 1, 480, 1, "min"),
    ]
    for index in (5, 8, 10, 13, 14, 15, 17):
        entities.append(_sensor(f"status_byte_{index}", f"Tilatavu {index}", diagnostic=True))
    for index in range(EXTENDED_LEN):
        if index not in EXTENDED_MAPPED:
            entities.append(_sensor(f"ext_byte_{index}", f"Laajennettu tavu {index}", diagnostic=True))

    messages = []
    for entity in entities:
        topic = f"homeassistant/{entity['component']}/{PREFIX}/{entity['key']}/config"
        messages.append((topic, json.dumps(entity["payload"], ensure_ascii=False)))

    messages.append((f"homeassistant/switch/{PREFIX}/power/config", json.dumps({
        "name": "Lämmitys",
        "unique_id": f"{PREFIX}_power",
        "command_topic": set_topic("power"),
        "state_topic": state_topic("power"),
        "payload_on": "ON",
        "payload_off": "OFF",
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
    })))
    messages.append((f"homeassistant/button/{PREFIX}/stop/config", json.dumps({
        "name": "Pysäytys",
        "unique_id": f"{PREFIX}_stop",
        "command_topic": set_topic("stop"),
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
        "icon": "mdi:stop",
    })))
    messages.append((f"homeassistant/button/{PREFIX}/fuel_reset/config", json.dumps({
        "name": "Nollaa polttoainekertymä",
        "unique_id": f"{PREFIX}_fuel_reset",
        "command_topic": set_topic("fuel_reset"),
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
        "entity_category": "config",
    })))
    messages.append((f"homeassistant/select/{PREFIX}/mode/config", json.dumps({
        "name": "Tila",
        "unique_id": f"{PREFIX}_mode",
        "command_topic": set_topic("mode"),
        "state_topic": state_topic("mode_select"),
        "options": ["thermostat", "preheat", "pump"],
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
    })))
    messages.append((f"homeassistant/select/{PREFIX}/aggregation/config", json.dumps({
        "name": "Lämpötilan yhdistely",
        "unique_id": f"{PREFIX}_aggregation",
        "command_topic": set_topic("aggregation"),
        "state_topic": state_topic("aggregation"),
        "options": ["min", "avg", "max"],
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
        "entity_category": "config",
    })))
    messages.append((f"homeassistant/switch/{PREFIX}/circulation/config", json.dumps({
        "name": "Kiertovesipumppu odotuksessa",
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
        "min_temp": 0,
        "max_temp": 40,
        "temp_step": 0.5,
        "precision": 0.1,
        "temperature_unit": "C",
        "availability_topic": f"{PREFIX}/availability",
        "device": DEVICE,
    })))
    return messages


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
) -> dict[str, str]:
    from .fuel import liters_per_hour, milliliters_per_minute

    fault = 0
    if status is not None and status.fault:
        fault = status.fault
    elif extended is not None:
        fault = extended.fault
    values: dict[str, Any] = {
        "mode": status.mode_name if status else "Ei yhteyttä",
        "phase": controller._phase,
        "state_code": status.state if status else "",
        "substate_code": status.substate if status else "",
        "fault_code": fault,
        "fault_text": fault_name(fault),
        "fault_active": "ON" if fault else "OFF",
        "coolant": _num(status.coolant_c if status else None),
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
        "fuel_ml_min": f"{milliliters_per_minute(frequency_hz, settings.pump_dose_ml):.4f}",
        "fuel_total_l": f"{fuel.total_liters:.4f}",
        "fuel_total_ml": f"{fuel.total_ml:.4f}",
        "room": _num(room_c),
        "demand": "ON" if controller.demand else "OFF",
        "link": "ON" if online else "OFF",
        "telemetry_stale": "ON" if telemetry_stale else "OFF",
        "fan_request": "ON" if fan_on else "OFF",
        "lockout": "ON" if controller.lockout else "OFF",
        "power": "ON" if controller.heat_enabled else "OFF",
        "mode_select": controller.mode,
        "climate_mode": "heat" if controller.heat_enabled and controller.mode == "thermostat" else "off",
        "aggregation": settings.aggregation,
        "circulation": "ON" if settings.circulation_pump else "OFF",
        "coolant_limit": settings.coolant_limit,
        "coolant_drop": settings.coolant_drop,
        "room_drop": settings.room_drop,
        "room_rise": settings.room_rise,
        "room_target": settings.room_target,
        "min_on": settings.min_on_s / 60,
        "min_off": settings.min_off_s / 60,
        "sensor_stale": settings.sensor_stale_s / 60,
        "shutdown_voltage": settings.shutdown_voltage,
        "shutdown_voltage_seconds": settings.shutdown_voltage_s,
        "fan_temperature": settings.fan_temperature,
        "pump_dose": settings.pump_dose_ml,
        "preheat_minutes": settings.preheat_minutes,
    }
    if status is not None:
        for index, value in status.unmapped.items():
            values[f"status_byte_{index}"] = value
    if extended is not None:
        for index, value in extended.unmapped.items():
            values[f"ext_byte_{index}"] = value
    return {key: "" if value is None else str(value) for key, value in values.items()}


def apply_command(controller: Controller, settings: Settings, topic: str, payload: str, now: float) -> str | None:
    """Apply one retained or live command. Return 'reset-fuel' when asked."""
    key = topic.removeprefix(f"{PREFIX}/set/")
    text = payload.strip()
    if key == "power":
        controller.set_heat(text == "ON", now)
    elif key == "stop":
        controller.request_stop(now)
    elif key == "fuel_reset":
        return "reset-fuel"
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
    else:
        try:
            _apply_number(settings, key, text)
        except ValueError:
            return None
    controller.settings = settings.sanitized()
    for field_name, value in vars(controller.settings).items():
        setattr(settings, field_name, value)
    return None


def _apply_number(settings: Settings, key: str, text: str) -> None:
    if key == "room_target":
        settings.room_target = float(text)
    elif key == "room_drop":
        settings.room_drop = float(text)
    elif key == "room_rise":
        settings.room_rise = float(text)
    elif key == "min_on":
        settings.min_on_s = float(text) * 60
    elif key == "min_off":
        settings.min_off_s = float(text) * 60
    elif key == "sensor_stale":
        settings.sensor_stale_s = float(text) * 60
    elif key == "coolant_limit":
        settings.coolant_limit = float(text)
    elif key == "coolant_drop":
        settings.coolant_drop = float(text)
    elif key == "shutdown_voltage":
        settings.shutdown_voltage = float(text)
    elif key == "shutdown_voltage_seconds":
        settings.shutdown_voltage_s = float(text)
    elif key == "fan_temperature":
        settings.fan_temperature = float(text)
    elif key == "pump_dose":
        settings.pump_dose_ml = float(text)
    elif key == "preheat_minutes":
        settings.preheat_minutes = int(float(text))
    else:
        raise ValueError(key)


def _num(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}"
