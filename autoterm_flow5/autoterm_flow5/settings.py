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
    room_drop: float = 1.0
    room_rise: float = 0.5
    room_control: bool = True
    min_on_s: float = 600.0
    min_off_s: float = 300.0
    sensor_stale_s: float = 900.0
    coolant_limit: float = 88.0
    coolant_drop: float = 18.0
    auxiliary_mode: str = "off"
    auxiliary_limit: float = 95.0
    relay_mode: str = "auto"
    pump_signal: str = "plain"
    pump_with_engine: bool = False
    pump_standby: bool = True
    alarm_channel: bool = True
    shutdown_voltage: float = 11.5
    shutdown_voltage_s: float = 30.0
    fan_temperature: float = 40.0
    pump_dose_ml: float = 0.044
    tank_capacity_l: float = 0.0
    preheat_minutes: int = 30
    # 0 is Comfort Control's unlimited run time. A positive value is minutes.
    run_minutes: int = 0
    extended_telemetry: bool = True
    debug: bool = False
    circulation_pump: bool = False
    aggregation: str = "min"
    temperature_entities: str = ""
    outdoor_temperature_entity: str = ""
    adaptive_enabled: bool = False
    adaptive_coolant_min: float = 40.0
    adaptive_coolant_max: float = 85.0
    adaptive_margin_c: float = 3.0
    adaptive_base_step_c: float = 2.0
    adaptive_step_up_ratio: float = 0.6
    adaptive_power_confirm_s: float = 1800.0
    adaptive_power_lockout_s: float = 5400.0
    adaptive_soot_interval_h: float = 24.0
    adaptive_soot_target_c: float = 80.0
    adaptive_safe_coolant_c: float = 65.0
    adaptive_min_burn_min: float = 20.0
    adaptive_max_stops_6h: int = 2
    adaptive_rise_offset_c: float = 1.0
    adaptive_rise_target_c: float = 75.0
    manual_coolant_limit: float = 88.0
    simulate: bool = False
    simulate_room_c: float = 15.0
    simulate_outdoor_c: float = 0.0
    simulate_fault: int = 0
    heat_on: bool = False
    control_mode: str = "thermostat"

    def sanitized(self) -> Settings:
        aggregation = self.aggregation if self.aggregation in {"min", "avg", "max"} else "min"
        coolant_min = _clamp(self.adaptive_coolant_min, 20, 95)
        coolant_max = _clamp(self.adaptive_coolant_max, coolant_min, 95)
        return Settings(
            room_target=_clamp(self.room_target, 0, 40),
            room_drop=_clamp(self.room_drop, 0, 10),
            room_rise=_clamp(self.room_rise, 0, 10),
            room_control=bool(self.room_control),
            heat_on=bool(self.heat_on),
            control_mode=self.control_mode if self.control_mode in {"thermostat", "preheat", "pump"} else "thermostat",
            min_on_s=_clamp(self.min_on_s, 0, 7200),
            min_off_s=_clamp(self.min_off_s, 0, 7200),
            sensor_stale_s=_clamp(self.sensor_stale_s, 60, 7200),
            coolant_limit=float(round(_clamp(self.coolant_limit, 20, 95))),
            coolant_drop=_clamp(self.coolant_drop, 1, 40),
            auxiliary_mode=self.auxiliary_mode if self.auxiliary_mode in {"off", "auto", "manual"} else "off",
            auxiliary_limit=_clamp(self.auxiliary_limit, 80, 95),
            relay_mode=self.relay_mode if self.relay_mode in {"auto", "off"} else "auto",
            pump_signal=self.pump_signal if self.pump_signal in {"plain", "pwm"} else "plain",
            pump_with_engine=bool(self.pump_with_engine),
            pump_standby=bool(self.pump_standby),
            alarm_channel=bool(self.alarm_channel),
            shutdown_voltage=_clamp(self.shutdown_voltage, 9, 30),
            shutdown_voltage_s=_clamp(self.shutdown_voltage_s, 1, 600),
            fan_temperature=_clamp(self.fan_temperature, 30, 60),
            pump_dose_ml=_clamp(self.pump_dose_ml, 0.01, 0.2),
            tank_capacity_l=_clamp(self.tank_capacity_l, 0, 500),
            preheat_minutes=int(_clamp(self.preheat_minutes, 1, 480)),
            run_minutes=int(_clamp(self.run_minutes, 0, 720)),
            extended_telemetry=bool(self.extended_telemetry),
            debug=bool(self.debug),
            circulation_pump=bool(self.circulation_pump),
            aggregation=aggregation,
            temperature_entities=self.temperature_entities.strip(),
            outdoor_temperature_entity=self.outdoor_temperature_entity.strip(),
            adaptive_enabled=bool(self.adaptive_enabled),
            adaptive_coolant_min=coolant_min,
            adaptive_coolant_max=coolant_max,
            adaptive_margin_c=_clamp(self.adaptive_margin_c, 0, 15),
            adaptive_base_step_c=_clamp(self.adaptive_base_step_c, 0.2, 10),
            adaptive_step_up_ratio=_clamp(self.adaptive_step_up_ratio, 0.1, 1.5),
            adaptive_power_confirm_s=_clamp(self.adaptive_power_confirm_s, 60, 7200),
            adaptive_power_lockout_s=_clamp(self.adaptive_power_lockout_s, 60, 14400),
            adaptive_soot_interval_h=_clamp(self.adaptive_soot_interval_h, 1, 168),
            adaptive_soot_target_c=_clamp(self.adaptive_soot_target_c, 60, 95),
            adaptive_safe_coolant_c=_clamp(self.adaptive_safe_coolant_c, 40, 95),
            adaptive_min_burn_min=_clamp(self.adaptive_min_burn_min, 0, 240),
            adaptive_max_stops_6h=int(_clamp(self.adaptive_max_stops_6h, 0, 10)),
            adaptive_rise_offset_c=_clamp(self.adaptive_rise_offset_c, 0.3, 10),
            adaptive_rise_target_c=_clamp(self.adaptive_rise_target_c, 40, 95),
            manual_coolant_limit=_clamp(self.manual_coolant_limit, 20, 95),
            simulate=bool(self.simulate),
            simulate_room_c=_clamp(self.simulate_room_c, 0, 40),
            simulate_outdoor_c=_clamp(self.simulate_outdoor_c, -40, 40),
            simulate_fault=int(_clamp(self.simulate_fault, 0, 255)),
        )

    def entity_ids(self) -> list[str]:
        return [part.strip() for part in self.temperature_entities.split(",") if part.strip()]

    def outdoor_entity_id(self) -> str | None:
        text = self.outdoor_temperature_entity.strip()
        return text or None

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
        # Files saved before the add-on thermostat carry the old defaults,
        # which were never shown to the user as a choice.
        if "room_control" not in raw:
            if raw.get("room_drop") == 2.0 and raw.get("room_rise") == 1.0:
                data["room_drop"] = base.room_drop
                data["room_rise"] = base.room_rise
            if raw.get("min_off_s") == 600.0:
                data["min_off_s"] = base.min_off_s
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
    if option_is_set(options, "temperature", "outdoor_temperature_entity"):
        settings.outdoor_temperature_entity = seeded.outdoor_temperature_entity
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
        room_drop=num("temperature", "room_drop", 1),
        room_rise=num("temperature", "room_rise", 0.5),
        room_control=flag("temperature", "room_control", True),
        min_on_s=num("timing", "min_on_minutes", 10) * 60,
        min_off_s=num("timing", "min_off_minutes", 5) * 60,
        sensor_stale_s=num("timing", "sensor_stale_minutes", 15) * 60,
        coolant_limit=num("protection", "coolant_limit", 88),
        coolant_drop=num("protection", "coolant_drop", 18),
        auxiliary_mode=text("protection", "auxiliary_mode", "off"),
        auxiliary_limit=num("protection", "auxiliary_limit", 95),
        relay_mode=text("protection", "relay_mode", "auto"),
        pump_signal=text("protection", "pump_signal", "plain"),
        pump_with_engine=flag("protection", "pump_with_engine", False),
        pump_standby=flag("protection", "pump_standby", True),
        alarm_channel=flag("protection", "alarm_channel", True),
        shutdown_voltage=num("protection", "shutdown_voltage", 11.5),
        shutdown_voltage_s=num("protection", "shutdown_voltage_seconds", 30),
        fan_temperature=num("protection", "fan_temperature", 40),
        pump_dose_ml=num("fuel", "pump_dose_ml", 0.044),
        tank_capacity_l=num("fuel", "tank_capacity_l", 0),
        preheat_minutes=int(num("timing", "preheat_minutes", 30)),
        run_minutes=int(num("timing", "run_minutes", 0)),
        extended_telemetry=flag("device", "extended_telemetry", True),
        debug=flag("device", "debug", False),
        circulation_pump=flag("timing", "circulation_pump", False),
        aggregation=text("temperature", "aggregation", "min"),
        temperature_entities=text("temperature", "temperature_entities"),
        outdoor_temperature_entity=text("temperature", "outdoor_temperature_entity"),
        adaptive_enabled=flag("temperature", "adaptive_enabled", False),
        adaptive_coolant_min=num("temperature", "adaptive_coolant_min", 40),
        adaptive_coolant_max=num("temperature", "adaptive_coolant_max", 85),
        adaptive_min_burn_min=num("temperature", "adaptive_min_burn_minutes", 20),
        adaptive_max_stops_6h=int(num("temperature", "adaptive_max_stops_6h", 2)),
        adaptive_rise_offset_c=num("temperature", "adaptive_rise_offset", 1.0),
        adaptive_rise_target_c=num("temperature", "adaptive_rise_target", 75),
        simulate=flag("simulation", "simulate", False),
        simulate_room_c=num("simulation", "simulate_room_temperature", 15),
        simulate_outdoor_c=num("simulation", "simulate_outdoor_temperature", 0),
        simulate_fault=int(num("simulation", "simulate_fault_code", 0)),
    ).sanitized()
