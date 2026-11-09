"""Runtime settings, seeded from the add-on options and saved in /data."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


@dataclass
class Settings:
    room_target: float = 20.0
    room_drop: float = 2.0
    room_rise: float = 1.0
    min_on_s: float = 600.0
    min_off_s: float = 600.0
    sensor_stale_s: float = 900.0
    coolant_limit: float = 88.0
    coolant_drop: float = 18.0
    shutdown_voltage: float = 11.5
    shutdown_voltage_s: float = 30.0
    fan_temperature: float = 40.0
    pump_dose_ml: float = 0.044
    tank_capacity_l: float = 0.0
    preheat_minutes: int = 30
    extended_telemetry: bool = True
    debug: bool = False
    circulation_pump: bool = False
    aggregation: str = "min"
    temperature_entities: str = ""
    simulate: bool = False
    simulate_room_c: float = 15.0
    simulate_fault: int = 0

    def sanitized(self) -> Settings:
        aggregation = self.aggregation if self.aggregation in {"min", "avg", "max"} else "min"
        return Settings(
            room_target=_clamp(self.room_target, 0, 40),
            room_drop=_clamp(self.room_drop, 0, 10),
            room_rise=_clamp(self.room_rise, 0, 10),
            min_on_s=_clamp(self.min_on_s, 0, 7200),
            min_off_s=_clamp(self.min_off_s, 0, 7200),
            sensor_stale_s=_clamp(self.sensor_stale_s, 60, 7200),
            coolant_limit=_clamp(self.coolant_limit, 20, 95),
            coolant_drop=_clamp(self.coolant_drop, 1, 40),
            shutdown_voltage=_clamp(self.shutdown_voltage, 9, 30),
            shutdown_voltage_s=_clamp(self.shutdown_voltage_s, 1, 600),
            fan_temperature=_clamp(self.fan_temperature, 30, 60),
            pump_dose_ml=_clamp(self.pump_dose_ml, 0.01, 0.2),
            tank_capacity_l=_clamp(self.tank_capacity_l, 0, 500),
            preheat_minutes=int(_clamp(self.preheat_minutes, 1, 480)),
            extended_telemetry=bool(self.extended_telemetry),
            debug=bool(self.debug),
            circulation_pump=bool(self.circulation_pump),
            aggregation=aggregation,
            temperature_entities=self.temperature_entities.strip(),
            simulate=bool(self.simulate),
            simulate_room_c=_clamp(self.simulate_room_c, 0, 40),
            simulate_fault=int(_clamp(self.simulate_fault, 0, 255)),
        )

    def entity_ids(self) -> list[str]:
        return [part.strip() for part in self.temperature_entities.split(",") if part.strip()]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self.sanitized())), encoding="utf-8")

    @classmethod
    def load(cls, path: Path, defaults: Settings | None = None) -> Settings:
        base = defaults or cls()
        if not path.exists():
            return base.sanitized()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return base.sanitized()
        data = asdict(base)
        for key in data:
            if key in raw:
                data[key] = raw[key]
        return cls(**data).sanitized()


def option_value(options: dict, section: str, name: str, default=None):
    """Read one option from a grouped page, or from an older flat file."""
    group = options.get(section)
    if isinstance(group, dict) and name in group and group[name] is not None:
        return group[name]
    if name in options and options[name] is not None and not isinstance(options[name], (dict, list)):
        return options[name]
    return default


def option_is_set(options: dict, section: str, name: str) -> bool:
    """True when the add-on configuration actually contains this option."""
    group = options.get(section)
    if isinstance(group, dict) and name in group and group[name] is not None:
        return True
    value = options.get(name)
    return name in options and value is not None and not isinstance(value, (dict, list))


def load_settings(path: Path, options: dict) -> Settings:
    """Load saved settings, then apply the simulation switch from the add-on page.

    The other values stay in settings.json after the first start. Simulation is
    changed only on the configuration page, so a previously saved true must not
    keep the UART closed after the option is turned off.
    """
    settings = Settings.load(path, settings_from_options(options))
    seeded = settings_from_options(options)
    if option_is_set(options, "simulation", "simulate"):
        settings.simulate = seeded.simulate
    if option_is_set(options, "device", "debug"):
        settings.debug = seeded.debug
    # The entity list has no Home Assistant control. The configuration page
    # is the only place it can be changed, so a later edit must replace the
    # list saved on the first start.
    if option_is_set(options, "temperature", "temperature_entities"):
        settings.temperature_entities = seeded.temperature_entities
    return settings.sanitized()


def settings_from_options(options: dict) -> Settings:
    def num(section: str, name: str, default: float) -> float:
        try:
            return float(option_value(options, section, name, default))
        except (TypeError, ValueError):
            return default

    def flag(section: str, name: str, default: bool) -> bool:
        value = option_value(options, section, name, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def text(section: str, name: str, default: str = "") -> str:
        value = option_value(options, section, name, default)
        return default if value is None else str(value)

    return Settings(
        room_target=num("temperature", "room_target", 20),
        room_drop=num("temperature", "room_drop", 2),
        room_rise=num("temperature", "room_rise", 1),
        min_on_s=num("timing", "min_on_minutes", 10) * 60,
        min_off_s=num("timing", "min_off_minutes", 10) * 60,
        sensor_stale_s=num("timing", "sensor_stale_minutes", 15) * 60,
        coolant_limit=num("protection", "coolant_limit", 88),
        coolant_drop=num("protection", "coolant_drop", 18),
        shutdown_voltage=num("protection", "shutdown_voltage", 11.5),
        shutdown_voltage_s=num("protection", "shutdown_voltage_seconds", 30),
        fan_temperature=num("protection", "fan_temperature", 40),
        pump_dose_ml=num("fuel", "pump_dose_ml", 0.044),
        tank_capacity_l=num("fuel", "tank_capacity_l", 0),
        preheat_minutes=int(num("timing", "preheat_minutes", 30)),
        extended_telemetry=flag("device", "extended_telemetry", True),
        debug=flag("device", "debug", False),
        circulation_pump=flag("timing", "circulation_pump", False),
        aggregation=text("temperature", "aggregation", "min"),
        temperature_entities=text("temperature", "temperature_entities"),
        simulate=flag("simulation", "simulate", False),
        simulate_room_c=num("simulation", "simulate_room_temperature", 15),
        simulate_fault=int(num("simulation", "simulate_fault_code", 0)),
    ).sanitized()
