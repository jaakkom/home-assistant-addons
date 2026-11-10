"""Adaptive liquid-temperature control for Autoterm Flow 5.

Learns a small physical model of the house and radiator network, then
chooses the coolant standby temperature so the heater runs as long as
possible with as few ignitions as possible. Only the coolant target is
changed; thermostat hysteresis stays under user control.
"""

from __future__ import annotations

import json
import logging
import math
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

LOG = logging.getLogger("autoterm_flow5.adaptive")

FREQ_LOW = 1.8
FREQ_MID = 2.8
FREQ_HIGH = 4.4
P_MAX = 5.0
P_MIN = P_MAX * FREQ_LOW / FREQ_HIGH
P_MID = P_MAX * FREQ_MID / FREQ_HIGH

DEFAULT_UA = 0.15
DEFAULT_ROOM_FOR_C = 20.0
DEFAULT_T_EQ_AT_2KW = 50.0
DEFAULT_COOLANT_DROP = 18.0
DEFAULT_FIRST_TARGET = 60.0
SAFE_COOLANT = 65.0
SOOT_TARGET = 80.0
K_BIN_WIDTH = 3.0
UA_WINDOW_S = 4 * 3600.0
UA_FAST_TAU_S = 3600.0
UA_SLOW_TAU_S = 18 * 3600.0
C_STABLE_S = 600.0
C_STABLE_DELTA = 0.5
MODEL_MIN_C_SAMPLES = 3
MODEL_MIN_UA_S = 3 * 3600.0
OUTDOOR_RAPID_C_PER_H = 3.0

MODES = ("continuous", "intermittent", "maximum")
END_THERMOSTAT = "thermostat"
END_LIQUID_STANDBY = "liquid_standby"
END_ADAPTIVE = "adaptive"
END_FAULT = "fault"
END_MANUAL = "manual"
END_EXTERNAL = "external"


def power_kw(frequency_hz: float) -> float:
    if frequency_hz <= 0:
        return 0.0
    return P_MAX * max(0.0, frequency_hz) / FREQ_HIGH


def nearest_step(frequency_hz: float) -> float:
    if frequency_hz <= 0:
        return 0.0
    return min((FREQ_LOW, FREQ_MID, FREQ_HIGH), key=lambda step: abs(step - frequency_hz))


def step_label(frequency_hz: float) -> str:
    step = nearest_step(frequency_hz)
    if step == FREQ_LOW:
        return "1.8"
    if step == FREQ_MID:
        return "2.8"
    if step == FREQ_HIGH:
        return "4.4"
    return "0"


def default_c() -> float:
    """Radiator coefficient so T_eq(2 kW) ≈ 50 °C at 20 °C room."""
    delta = DEFAULT_T_EQ_AT_2KW - DEFAULT_ROOM_FOR_C
    return 2.0 / (delta ** 1.3)


def equilibrium_temp(power: float, room_c: float, coefficient: float) -> float:
    if coefficient <= 0 or power <= 0:
        return room_c
    return room_c + (power / coefficient) ** (1.0 / 1.3)


def outdoor_bin(outdoor_c: float) -> int:
    return int(math.floor(outdoor_c / K_BIN_WIDTH))


@dataclass
class AdaptiveConfig:
    enabled: bool = False
    coolant_min: float = 45.0
    coolant_max: float = 85.0
    margin_c: float = 3.0
    base_step_c: float = 2.0
    step_up_ratio: float = 0.6
    power_confirm_s: float = 30 * 60.0
    power_lockout_s: float = 90 * 60.0
    soot_interval_s: float = 24 * 3600.0
    soot_target_c: float = SOOT_TARGET
    safe_coolant_c: float = SAFE_COOLANT
    first_target_c: float = DEFAULT_FIRST_TARGET
    room_hysteresis_c: float = 2.0
    coolant_drop_c: float = DEFAULT_COOLANT_DROP


@dataclass
class AdaptiveActions:
    """Side effects requested by one adaptive tick."""

    stop: bool = False
    start: bool = False
    coolant_limit: float | None = None
    save_model: bool = False
    save_settings: bool = False
    log_line: str | None = None


@dataclass
class CycleStats:
    started_at: float = 0.0
    ended_at: float = 0.0
    outdoor_c: float | None = None
    room_c: float | None = None
    room_target: float = 20.0
    coolant_target: float = 60.0
    hysteresis_c: float = 2.0
    run_s: float = 0.0
    pause_s: float = 0.0
    time_1_8: float = 0.0
    time_2_8: float = 0.0
    time_4_4: float = 0.0
    coolant_max: float | None = None
    end_reason: str = END_EXTERNAL
    corrections: list[str] = field(default_factory=list)
    mode: str = ""
    learned: bool = True


@dataclass
class AdaptiveModel:
    ua: float = DEFAULT_UA
    c: float = field(default_factory=default_c)
    coolant_drop_c: float = DEFAULT_COOLANT_DROP
    thermal_mass_kwh_per_c: float | None = None
    k_bins: dict[str, float] = field(default_factory=dict)
    k_counts: dict[str, int] = field(default_factory=dict)
    k_last_sign: dict[str, int] = field(default_factory=dict)
    step_halves: dict[str, int] = field(default_factory=dict)
    threshold_up: list[float] = field(default_factory=list)
    threshold_down: list[float] = field(default_factory=list)
    c_samples: int = 0
    ua_integral_energy: float = 0.0
    ua_integral_dtdt: float = 0.0
    ua_seconds: float = 0.0
    last_high_power_at: float | None = None
    ignitions: deque = field(default_factory=lambda: deque(maxlen=500))
    run_durations: deque = field(default_factory=lambda: deque(maxlen=100))
    room_errors: deque = field(default_factory=lambda: deque(maxlen=100))
    cycle_log: deque = field(default_factory=lambda: deque(maxlen=50))
    last_target: float = DEFAULT_FIRST_TARGET
    last_mode: str = ""
    reliable: bool = False
    outdoor_history: deque = field(default_factory=lambda: deque(maxlen=120))
    created: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "ua": self.ua,
            "c": self.c,
            "coolant_drop_c": self.coolant_drop_c,
            "thermal_mass_kwh_per_c": self.thermal_mass_kwh_per_c,
            "k_bins": dict(self.k_bins),
            "k_counts": dict(self.k_counts),
            "k_last_sign": dict(self.k_last_sign),
            "step_halves": dict(self.step_halves),
            "threshold_up": list(self.threshold_up)[-40:],
            "threshold_down": list(self.threshold_down)[-40:],
            "c_samples": self.c_samples,
            "ua_integral_energy": self.ua_integral_energy,
            "ua_integral_dtdt": self.ua_integral_dtdt,
            "ua_seconds": self.ua_seconds,
            "last_high_power_at": self.last_high_power_at,
            "ignitions": list(self.ignitions)[-200:],
            "run_durations": list(self.run_durations),
            "room_errors": list(self.room_errors),
            "cycle_log": list(self.cycle_log)[-50:],
            "last_target": self.last_target,
            "last_mode": self.last_mode,
            "reliable": self.reliable,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AdaptiveModel:
        model = cls()
        model.ua = float(raw.get("ua", DEFAULT_UA))
        model.c = float(raw.get("c", default_c()))
        model.coolant_drop_c = float(raw.get("coolant_drop_c", DEFAULT_COOLANT_DROP))
        mass = raw.get("thermal_mass_kwh_per_c")
        model.thermal_mass_kwh_per_c = None if mass is None else float(mass)
        model.k_bins = {str(k): float(v) for k, v in dict(raw.get("k_bins", {})).items()}
        model.k_counts = {str(k): int(v) for k, v in dict(raw.get("k_counts", {})).items()}
        model.k_last_sign = {str(k): int(v) for k, v in dict(raw.get("k_last_sign", {})).items()}
        model.step_halves = {str(k): int(v) for k, v in dict(raw.get("step_halves", {})).items()}
        model.threshold_up = [float(v) for v in raw.get("threshold_up", [])][-40:]
        model.threshold_down = [float(v) for v in raw.get("threshold_down", [])][-40:]
        model.c_samples = int(raw.get("c_samples", 0))
        model.ua_integral_energy = float(raw.get("ua_integral_energy", 0.0))
        model.ua_integral_dtdt = float(raw.get("ua_integral_dtdt", 0.0))
        model.ua_seconds = float(raw.get("ua_seconds", 0.0))
        high = raw.get("last_high_power_at")
        model.last_high_power_at = None if high is None else float(high)
        model.ignitions = deque((float(v) for v in raw.get("ignitions", [])), maxlen=500)
        model.run_durations = deque((float(v) for v in raw.get("run_durations", [])), maxlen=100)
        model.room_errors = deque((float(v) for v in raw.get("room_errors", [])), maxlen=100)
        model.cycle_log = deque(raw.get("cycle_log", [])[-50:], maxlen=50)
        model.last_target = float(raw.get("last_target", DEFAULT_FIRST_TARGET))
        model.last_mode = str(raw.get("last_mode", ""))
        model.reliable = bool(raw.get("reliable", False))
        model.created = False
        return model

    def reset(self) -> None:
        fresh = AdaptiveModel()
        for key, value in asdict(fresh).items():
            if key in {"ignitions", "run_durations", "room_errors", "cycle_log", "outdoor_history"}:
                setattr(self, key, getattr(fresh, key))
            else:
                setattr(self, key, value)
        self.c = default_c()
        self.created = True


@dataclass
class _LiveCycle:
    started_at: float
    outdoor_c: float | None
    room_start: float | None
    room_target: float
    coolant_target: float
    hysteresis_c: float
    mode: str
    time_1_8: float = 0.0
    time_2_8: float = 0.0
    time_4_4: float = 0.0
    coolant_max: float | None = None
    learned: bool = True
    corrections: list[str] = field(default_factory=list)
    last_freq: float = 0.0
    last_sample_at: float | None = None
    liquid_standby_seen: bool = False
    room_at_standby: float | None = None
    coolant_at_standby: float | None = None
    high_power_heat_start: float | None = None
    high_power_coolant_start: float | None = None


@dataclass
class _StableWindow:
    step: float
    since: float
    coolant_start: float
    last_coolant: float


class AdaptiveController:
    """Observe heater telemetry and optionally steer the coolant target."""

    def __init__(self, path: Path, config: AdaptiveConfig | None = None) -> None:
        self.path = path
        self.config = config or AdaptiveConfig()
        self.model = self._load()
        self._phase = "unknown"
        self._cycle: _LiveCycle | None = None
        self._pause_started_at: float | None = None
        self._last_tick_at: float | None = None
        self._last_room_target: float | None = None
        self._last_hysteresis: float | None = None
        self._last_outdoor: float | None = None
        self._stable: _StableWindow | None = None
        self._power_low_since: float | None = None
        self._power_lock_until: float = 0.0
        self._pending_action: str | None = None
        self._pending_target: float | None = None
        self._awaiting_idle_write = False
        self._awaiting_start = False
        self._soot_burn_active = False
        self._manual_cycle = False
        self._enabled_was = self.config.enabled
        self._faulted = False
        self._restore_manual_pending = False
        self._manual_coolant: float | None = None
        self._wrote_target_this_rest = False
        self._status = "off"

    def _load(self) -> AdaptiveModel:
        if not self.path.exists():
            return AdaptiveModel()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            LOG.warning("Could not read adaptive model; starting fresh")
            return AdaptiveModel()
        if not isinstance(raw, dict):
            return AdaptiveModel()
        return AdaptiveModel.from_dict(raw)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.model.to_dict()), encoding="utf-8")

    def configure(self, config: AdaptiveConfig) -> None:
        self.config = config

    def set_manual_coolant(self, value: float | None) -> None:
        self._manual_coolant = value

    def note_manual_coolant_change(self) -> None:
        """User overrode the coolant target. Do not learn from this cycle."""
        self._manual_cycle = True
        if self._cycle is not None:
            self._cycle.learned = False
            self._cycle.corrections.append("manual_override")

    def reset_learned(self) -> None:
        self.model.reset()
        self._stable = None
        self._power_low_since = None
        self._pending_action = None
        self._pending_target = None
        self._awaiting_idle_write = False
        self._awaiting_start = False
        self._soot_burn_active = False
        self.save()
        LOG.info("Adaptive model reset to defaults")

    def status_values(self) -> dict[str, str]:
        self._refresh_status()
        mode = self.model.last_mode if self.config.enabled else ""
        return {
            "adaptive_enabled": "ON" if self.config.enabled else "OFF",
            "adaptive_status": self._status,
            "adaptive_mode": mode,
            "adaptive_coolant_target": f"{self.model.last_target:.0f}",
            "adaptive_ua": f"{self.model.ua:.4f}",
            "adaptive_c": f"{self.model.c:.5f}",
            "adaptive_coolant_drop": f"{self.model.coolant_drop_c:.1f}",
            "adaptive_ignitions_day": str(self.ignitions_last_day()),
            "adaptive_avg_run_s": _num(self.average_run_s()),
            "adaptive_room_error": _num(self.average_room_error()),
            "adaptive_reliable": "ON" if self.model.reliable else "OFF",
            "adaptive_k_table": json.dumps(self.model.k_bins, sort_keys=True),
            "adaptive_coolant_min": f"{self.config.coolant_min:.0f}",
            "adaptive_coolant_max": f"{self.config.coolant_max:.0f}",
        }

    def ignitions_last_day(self, now: float | None = None) -> int:
        if not self.model.ignitions:
            return 0
        latest = now if now is not None else self.model.ignitions[-1]
        cutoff = latest - 86400.0
        return sum(1 for stamp in self.model.ignitions if stamp >= cutoff)

    def average_run_s(self) -> float | None:
        if not self.model.run_durations:
            return None
        return sum(self.model.run_durations) / len(self.model.run_durations)

    def average_room_error(self) -> float | None:
        if not self.model.room_errors:
            return None
        return sum(self.model.room_errors) / len(self.model.room_errors)

    def on_enabled_changed(self, enabled: bool, current_coolant: float) -> AdaptiveActions:
        actions = AdaptiveActions(save_settings=True, save_model=True)
        if enabled and not self._enabled_was:
            self._manual_coolant = current_coolant
            self._restore_manual_pending = False
            self._pending_action = None
            self._awaiting_idle_write = False
            self._awaiting_start = False
            # New target is written on the next rest, not mid-burn.
            self._wrote_target_this_rest = False
            LOG.info("Adaptive control enabled; manual coolant %.0f °C saved", current_coolant)
        elif not enabled and self._enabled_was:
            self._pending_action = None
            self._power_low_since = None
            if self._awaiting_start:
                actions.start = True
            self._awaiting_idle_write = False
            self._awaiting_start = False
            self._restore_manual_pending = True
            LOG.info("Adaptive control disabled; restoring manual coolant on next idle")
        self._enabled_was = enabled
        self.config.enabled = enabled
        self._refresh_status()
        return actions

    def tick(
        self,
        now: float,
        *,
        phase: str,
        burner_active: bool,
        fault: int,
        heat_enabled: bool,
        mode: str,
        room_c: float | None,
        outdoor_c: float | None,
        coolant_c: float | None,
        pump_hz: float,
        room_target: float,
        hysteresis_c: float,
        current_coolant_limit: float,
        sensors_ok: bool,
    ) -> AdaptiveActions:
        actions = AdaptiveActions()
        dt = 0.0 if self._last_tick_at is None else max(0.0, min(30.0, now - self._last_tick_at))
        self._last_tick_at = now
        self._update_reliability()
        self._refresh_status()

        if outdoor_c is not None:
            self.model.outdoor_history.append((now, outdoor_c))
            self._last_outdoor = outdoor_c

        settings_changed = self._settings_changed(room_target, hysteresis_c)
        outdoor_rapid = self._outdoor_changing_fast(now)
        skip_learning = settings_changed or outdoor_rapid or self._manual_cycle

        liquid_standby = _is_liquid_standby(phase, burner_active)
        active = phase in {"starting", "running"} or (liquid_standby and phase == "cooldown")

        # Passive energy / UA even while adaptive steering is off.
        if sensors_ok and outdoor_c is not None and room_c is not None and dt > 0:
            self._accumulate_ua(dt, room_c, outdoor_c, pump_hz if (burner_active or phase in {"starting", "running"}) else 0.0)
            actions.save_model = True

        if sensors_ok and coolant_c is not None and room_c is not None and burner_active and dt > 0:
            self._observe_frequency(now, pump_hz, coolant_c, room_c, current_coolant_limit)
            self._observe_stability(now, pump_hz, coolant_c, room_c)
            actions.save_model = True

        if fault or not sensors_ok:
            if not self._faulted:
                LOG.warning("Adaptive control entering safe coolant because sensors or heater failed")
            self._faulted = True
            if self.config.enabled:
                if phase == "idle" and not self._wrote_target_this_rest:
                    actions.coolant_limit = self.config.safe_coolant_c
                    self.model.last_target = self.config.safe_coolant_c
                    self._wrote_target_this_rest = True
                    actions.save_model = True
            self._phase = phase
            return actions
        self._faulted = False

        # Cycle edges.
        was_active = self._cycle is not None
        if heat_enabled and mode == "thermostat" and active and not was_active:
            self._begin_cycle(now, outdoor_c, room_c, room_target, current_coolant_limit, hysteresis_c)
            actions.save_model = True
        elif was_active and not active:
            end_reason = self._end_reason(phase, liquid_standby, fault)
            self._finish_cycle(now, room_c, end_reason, skip_learning)
            actions.save_model = True
            self._wrote_target_this_rest = False
            self._manual_cycle = False

        if self._cycle is not None and dt > 0:
            self._sample_cycle(now, dt, pump_hz, coolant_c, room_c, liquid_standby)

        # Passive learning of thresholds continues in the background.
        if was_active and self._cycle is None:
            pass

        if not self.config.enabled:
            if self._restore_manual_pending and phase == "idle":
                restore = self._manual_coolant if self._manual_coolant is not None else current_coolant_limit
                actions.coolant_limit = restore
                self._restore_manual_pending = False
                self._wrote_target_this_rest = True
                actions.save_settings = True
            self._phase = phase
            self._refresh_status()
            return actions

        if mode != "thermostat" or not heat_enabled:
            self._pending_action = None
            self._awaiting_idle_write = False
            self._awaiting_start = False
            self._phase = phase
            return actions

        # Finish a pending adaptive rewrite after afterblow.
        if self._awaiting_idle_write and phase == "idle":
            target = self._pending_target
            if target is None:
                target = self._compute_target(outdoor_c, room_c, room_target)
            actions.coolant_limit = target
            self.model.last_target = target
            self._awaiting_idle_write = False
            self._awaiting_start = True
            self._pending_target = None
            self._pending_action = None
            self._wrote_target_this_rest = True
            actions.save_model = True
            actions.save_settings = True
            LOG.info("Adaptive rewrite complete: coolant target %.0f °C", target)

        if self._awaiting_start and phase == "idle" and not self._awaiting_idle_write:
            actions.start = True
            self._awaiting_start = False

        # Natural rest: choose the next coolant target before the heater restarts.
        if phase == "idle" and not self._wrote_target_this_rest and not self._awaiting_idle_write:
            target = self._compute_target(outdoor_c, room_c, room_target)
            if self._needs_soot_burn(now):
                target = max(target, self.config.soot_target_c)
                self._soot_burn_active = True
                LOG.info("Soot-prevention burn scheduled at %.0f °C", target)
            actions.coolant_limit = target
            self.model.last_target = target
            self._wrote_target_this_rest = True
            actions.save_model = True
            actions.save_settings = True

        # Active corrections while burning / in liquid standby.
        if self._cycle is not None and not skip_learning and not self._awaiting_idle_write:
            correction = self._active_correction(
                now,
                phase=phase,
                liquid_standby=liquid_standby,
                room_c=room_c,
                outdoor_c=outdoor_c,
                coolant_c=coolant_c,
                pump_hz=pump_hz,
                room_target=room_target,
            )
            if correction is not None:
                kind, target = correction
                self._cycle.corrections.append(kind)
                self._pending_action = kind
                self._pending_target = target
                self._awaiting_idle_write = True
                self._awaiting_start = False
                actions.stop = True
                actions.save_model = True
                LOG.info("Adaptive correction %s -> coolant %.0f °C", kind, target)

        if self._soot_burn_active and self._cycle is not None:
            high_s = self._cycle.time_4_4
            if high_s >= 15 * 60:
                self._soot_burn_active = False
                self.model.last_high_power_at = now
                actions.save_model = True

        self._phase = phase
        self._refresh_status()
        return actions

    def _refresh_status(self) -> None:
        if not self.config.enabled:
            self._status = "off"
        elif not self.model.reliable or self._faulted:
            self._status = "learning"
        else:
            self._status = "active"

    def _update_reliability(self) -> None:
        self.model.reliable = (
            self.model.c_samples >= MODEL_MIN_C_SAMPLES
            and self.model.ua_seconds >= MODEL_MIN_UA_S
        )

    def _settings_changed(self, room_target: float, hysteresis_c: float) -> bool:
        changed = False
        if self._last_room_target is not None and abs(self._last_room_target - room_target) > 0.05:
            changed = True
        if self._last_hysteresis is not None and abs(self._last_hysteresis - hysteresis_c) > 0.05:
            changed = True
        self._last_room_target = room_target
        self._last_hysteresis = hysteresis_c
        return changed

    def _outdoor_changing_fast(self, now: float) -> bool:
        history = self.model.outdoor_history
        if len(history) < 2:
            return False
        older = None
        for stamp, value in history:
            if now - stamp >= 3600.0:
                older = (stamp, value)
        if older is None:
            stamp0, value0 = history[0]
            if now - stamp0 < 600:
                return False
            rate = abs(history[-1][1] - value0) / max(now - stamp0, 1.0) * 3600.0
            return rate > OUTDOOR_RAPID_C_PER_H
        return abs(history[-1][1] - older[1]) > OUTDOOR_RAPID_C_PER_H

    def _accumulate_ua(self, dt: float, room_c: float, outdoor_c: float, pump_hz: float) -> None:
        energy = power_kw(pump_hz) * (dt / 3600.0)
        delta = max(0.0, room_c - outdoor_c) * (dt / 3600.0)
        self.model.ua_integral_energy += energy
        self.model.ua_integral_dtdt += delta
        self.model.ua_seconds += dt
        # Keep a sliding window by exponential decay toward the window length.
        if self.model.ua_seconds > UA_WINDOW_S:
            keep = UA_WINDOW_S / self.model.ua_seconds
            self.model.ua_integral_energy *= keep
            self.model.ua_integral_dtdt *= keep
            self.model.ua_seconds = UA_WINDOW_S
        if self.model.ua_integral_dtdt > 0.05:
            measured = self.model.ua_integral_energy / self.model.ua_integral_dtdt
            tau = UA_FAST_TAU_S if self.model.ua_seconds < MODEL_MIN_UA_S else UA_SLOW_TAU_S
            alpha = 1.0 - math.exp(-dt / tau)
            self.model.ua = (1.0 - alpha) * self.model.ua + alpha * measured

    def _observe_frequency(
        self,
        now: float,
        pump_hz: float,
        coolant_c: float,
        room_c: float,
        coolant_limit: float,
    ) -> None:
        del room_c
        step = nearest_step(pump_hz)
        if self._cycle is not None and self._cycle.last_freq > 0 and abs(step - self._cycle.last_freq) >= 0.5:
            gap = coolant_limit - coolant_c
            if step > self._cycle.last_freq:
                self.model.threshold_up.append(gap)
            else:
                self.model.threshold_down.append(gap)
        if step >= FREQ_HIGH - 0.2:
            self.model.last_high_power_at = now
            if self._cycle is not None:
                if self._cycle.high_power_heat_start is None:
                    self._cycle.high_power_heat_start = now
                    self._cycle.high_power_coolant_start = coolant_c
                elif (
                    self._cycle.high_power_coolant_start is not None
                    and now - self._cycle.high_power_heat_start >= 300
                    and coolant_c > self._cycle.high_power_coolant_start + 2
                ):
                    rise = coolant_c - self._cycle.high_power_coolant_start
                    hours = (now - self._cycle.high_power_heat_start) / 3600.0
                    if rise > 0 and hours > 0:
                        self.model.thermal_mass_kwh_per_c = (P_MAX * hours) / rise

    def _observe_stability(self, now: float, pump_hz: float, coolant_c: float, room_c: float) -> None:
        step = nearest_step(pump_hz)
        if step <= 0 or coolant_c <= room_c + 1:
            self._stable = None
            return
        if self._stable is None or abs(self._stable.step - step) > 0.2:
            self._stable = _StableWindow(step=step, since=now, coolant_start=coolant_c, last_coolant=coolant_c)
            return
        self._stable.last_coolant = coolant_c
        elapsed = now - self._stable.since
        if elapsed < C_STABLE_S:
            return
        drift = abs(coolant_c - self._stable.coolant_start)
        if drift > C_STABLE_DELTA:
            self._stable = _StableWindow(step=step, since=now, coolant_start=coolant_c, last_coolant=coolant_c)
            return
        delta = max(1.0, coolant_c - room_c)
        measured = power_kw(step) / (delta ** 1.3)
        self.model.c_samples += 1
        alpha = 1.0 / min(self.model.c_samples, 10)
        self.model.c = (1.0 - alpha) * self.model.c + alpha * measured
        self._stable = _StableWindow(step=step, since=now, coolant_start=coolant_c, last_coolant=coolant_c)
        LOG.info("Adaptive radiator C updated to %.5f from %.1f Hz plateau", self.model.c, step)

    def _begin_cycle(
        self,
        now: float,
        outdoor_c: float | None,
        room_c: float | None,
        room_target: float,
        coolant_target: float,
        hysteresis_c: float,
    ) -> None:
        pause_s = 0.0
        if self._pause_started_at is not None:
            pause_s = max(0.0, now - self._pause_started_at)
        self._pause_started_at = None
        mode = self.model.last_mode or self._select_mode(outdoor_c, room_c, room_target)
        self._cycle = _LiveCycle(
            started_at=now,
            outdoor_c=outdoor_c,
            room_start=room_c,
            room_target=room_target,
            coolant_target=coolant_target,
            hysteresis_c=hysteresis_c,
            mode=mode,
            learned=not self._manual_cycle,
        )
        if pause_s:
            # Attach the preceding pause length to the previous log entry if present.
            if self.model.cycle_log:
                previous = dict(self.model.cycle_log[-1])
                previous["pause_s"] = round(pause_s, 1)
                self.model.cycle_log[-1] = previous
        self.model.ignitions.append(now)
        LOG.info(
            "Adaptive cycle start: mode=%s coolant=%.0f outdoor=%s",
            mode,
            coolant_target,
            "n/a" if outdoor_c is None else f"{outdoor_c:.1f}",
        )

    def _sample_cycle(
        self,
        now: float,
        dt: float,
        pump_hz: float,
        coolant_c: float | None,
        room_c: float | None,
        liquid_standby: bool,
    ) -> None:
        cycle = self._cycle
        if cycle is None:
            return
        step = nearest_step(pump_hz)
        if step == FREQ_LOW:
            cycle.time_1_8 += dt
        elif step == FREQ_MID:
            cycle.time_2_8 += dt
        elif step == FREQ_HIGH:
            cycle.time_4_4 += dt
        if coolant_c is not None:
            cycle.coolant_max = coolant_c if cycle.coolant_max is None else max(cycle.coolant_max, coolant_c)
        if liquid_standby and not cycle.liquid_standby_seen:
            cycle.liquid_standby_seen = True
            cycle.room_at_standby = room_c
            cycle.coolant_at_standby = coolant_c
            if coolant_c is not None and cycle.coolant_target:
                drop = cycle.coolant_target - coolant_c
                # Learn the re-ignition drop once the liquid has fallen after standby.
                if drop > 5:
                    self.model.coolant_drop_c = 0.7 * self.model.coolant_drop_c + 0.3 * drop
        cycle.last_freq = step
        cycle.last_sample_at = now

    def _end_reason(self, phase: str, liquid_standby: bool, fault: int) -> str:
        if fault:
            return END_FAULT
        if self._pending_action:
            return END_ADAPTIVE
        if self._manual_cycle:
            return END_MANUAL
        if liquid_standby or phase == "cooldown":
            return END_LIQUID_STANDBY
        return END_THERMOSTAT

    def _finish_cycle(self, now: float, room_c: float | None, end_reason: str, skip_learning: bool) -> None:
        cycle = self._cycle
        self._cycle = None
        self._stable = None
        self._power_low_since = None
        self._pause_started_at = now
        if cycle is None:
            return
        run_s = max(0.0, now - cycle.started_at)
        self.model.run_durations.append(run_s)
        if room_c is not None:
            self.model.room_errors.append(room_c - cycle.room_target)
        stats = {
            "started_at": cycle.started_at,
            "ended_at": now,
            "outdoor_c": cycle.outdoor_c,
            "room_c": room_c if room_c is not None else cycle.room_start,
            "room_target": cycle.room_target,
            "coolant_target": cycle.coolant_target,
            "hysteresis": cycle.hysteresis_c,
            "run_s": round(run_s, 1),
            "pause_s": 0.0,
            "share_1_8": round(cycle.time_1_8 / run_s, 3) if run_s else 0.0,
            "share_2_8": round(cycle.time_2_8 / run_s, 3) if run_s else 0.0,
            "share_4_4": round(cycle.time_4_4 / run_s, 3) if run_s else 0.0,
            "coolant_max": cycle.coolant_max,
            "end_reason": end_reason,
            "corrections": list(cycle.corrections),
            "mode": cycle.mode,
        }
        self.model.cycle_log.append(stats)
        LOG.info(
            "Adaptive cycle end: reason=%s run=%.0fs shares=%.0f/%.0f/%.0f max_liquid=%s",
            end_reason,
            run_s,
            100 * stats["share_1_8"],
            100 * stats["share_2_8"],
            100 * stats["share_4_4"],
            "n/a" if cycle.coolant_max is None else f"{cycle.coolant_max:.0f}",
        )
        if skip_learning or not cycle.learned or end_reason in {END_FAULT, END_MANUAL, END_ADAPTIVE}:
            return
        self._learn_from_cycle(cycle, stats, room_c)

    def _learn_from_cycle(self, cycle: _LiveCycle, stats: dict[str, Any], room_c: float | None) -> None:
        outdoor = cycle.outdoor_c
        share_high = stats["share_4_4"]
        share_mid = stats["share_2_8"]
        share_low = stats["share_1_8"]
        end_reason = stats["end_reason"]
        corrections: list[str] = []

        if end_reason == END_LIQUID_STANDBY and (
            cycle.room_at_standby is None or cycle.room_at_standby < cycle.room_target
        ):
            step = self._step_size(outdoor, raise_k=True)
            self._adjust_k(outdoor, step, raise_k=True)
            corrections.append(f"k_up_standby:{step:.2f}")
            if not self.model.reliable and cycle.coolant_max is not None:
                # Direct observation before the model is trusted.
                bumped = min(self.config.coolant_max, cycle.coolant_target + step)
                self.model.last_target = bumped
            self._maybe_refresh_physical(cycle, room_c)
        elif end_reason == END_THERMOSTAT:
            if share_high >= 0.8:
                if cycle.coolant_max is not None:
                    direct = max(self.config.coolant_min, cycle.coolant_max - 2.0)
                    lowered = max(direct, cycle.coolant_target - 5.0)
                    lowered = min(lowered, cycle.coolant_target)
                    delta = lowered - cycle.coolant_target
                    if delta < -0.1:
                        self._adjust_k(outdoor, abs(delta), raise_k=False)
                        self.model.last_target = lowered
                        corrections.append(f"target_down_high:{delta:.1f}")
            elif share_mid >= 0.5 and cycle.mode == "continuous":
                step = self._step_size(outdoor, raise_k=False)
                self._adjust_k(outdoor, step, raise_k=False)
                corrections.append(f"k_down_mid:{step:.2f}")
            elif share_low >= 0.5:
                corrections.append("intermittent_hint")
                # House needs less than P_min. Prefer intermittent mode; check UA.
                if outdoor is not None and room_c is not None:
                    need = self.model.ua * max(0.0, cycle.room_target - outdoor)
                    if need > P_MIN * 0.8:
                        # UA may be high; ease it slightly from this observation.
                        self.model.ua = max(0.02, self.model.ua * 0.95)
                        corrections.append("ua_ease")
            else:
                self._bump_confidence(outdoor)
                corrections.append("good_cycle")
        if corrections:
            stats["corrections"] = list(stats.get("corrections", [])) + corrections
            if self.model.cycle_log:
                self.model.cycle_log[-1] = stats

    def _maybe_refresh_physical(self, cycle: _LiveCycle, room_c: float | None) -> None:
        if room_c is None or cycle.outdoor_c is None:
            return
        # Conflict with the model prediction: nudge UA/C, keep K small.
        predicted = self._heat_need(cycle.outdoor_c, room_c if room_c is not None else cycle.room_target, cycle.room_target)
        if predicted > P_MIN and cycle.liquid_standby_seen:
            self.model.c = max(0.005, self.model.c * 0.97)

    def _active_correction(
        self,
        now: float,
        *,
        phase: str,
        liquid_standby: bool,
        room_c: float | None,
        outdoor_c: float | None,
        coolant_c: float | None,
        pump_hz: float,
        room_target: float,
    ) -> tuple[str, float] | None:
        del phase, coolant_c
        if room_c is None:
            self._power_low_since = None
            return None

        # 6.1 Liquid standby while the room is still below the cut-out.
        if liquid_standby and room_c < room_target:
            target = self._compute_target(outdoor_c, room_c, room_target)
            step = self._step_size(outdoor_c, raise_k=True)
            self._adjust_k(outdoor_c, step, raise_k=True)
            target = min(self.config.coolant_max, max(target, (self.model.last_target + step)))
            return "liquid_standby_raise", target

        # 6.2 Power insufficient for 30 minutes.
        step = nearest_step(pump_hz)
        need = self._heat_need(outdoor_c, room_c, room_target)
        room_floor = room_target - self.config.room_hysteresis_c - 0.3
        rising = False
        if self._cycle is not None and self._cycle.room_start is not None:
            elapsed_h = max((now - self._cycle.started_at) / 3600.0, 1e-3)
            rising = (room_c - self._cycle.room_start) / elapsed_h >= 0.2
        low_power = step in {FREQ_LOW, FREQ_MID} and power_kw(step) < need
        if (
            low_power
            and room_c < room_floor
            and not rising
            and now >= self._power_lock_until
            and step < FREQ_HIGH - 0.2
        ):
            if self._power_low_since is None:
                self._power_low_since = now
            elif now - self._power_low_since >= self.config.power_confirm_s:
                step_c = self._step_size(outdoor_c, raise_k=True)
                self._adjust_k(outdoor_c, step_c, raise_k=True)
                target = self._compute_target(outdoor_c, room_c, room_target)
                self._power_lock_until = now + self.config.power_lockout_s
                self._power_low_since = None
                return "power_raise", target
        else:
            self._power_low_since = None
        return None

    def _heat_need(self, outdoor_c: float | None, room_c: float, room_target: float) -> float:
        del room_c
        if outdoor_c is None:
            outdoor_c = self._last_outdoor if self._last_outdoor is not None else room_target - 15.0
        return max(0.0, self.model.ua * (room_target - outdoor_c))

    def _select_mode(self, outdoor_c: float | None, room_c: float | None, room_target: float) -> str:
        room = room_c if room_c is not None else room_target
        need = self._heat_need(outdoor_c, room, room_target)
        if need < P_MIN:
            return "intermittent"
        if need > P_MAX:
            return "maximum"
        return "continuous"

    def _compute_target(
        self,
        outdoor_c: float | None,
        room_c: float | None,
        room_target: float,
    ) -> float:
        room = room_c if room_c is not None else room_target
        need = self._heat_need(outdoor_c, room, room_target)
        mode = self._select_mode(outdoor_c, room_c, room_target)
        k = self._k_for(outdoor_c)
        if mode == "maximum":
            target = self.config.coolant_max
        elif mode == "intermittent":
            target = equilibrium_temp(P_MIN, room, self.model.c) + self.config.margin_c + k
        else:
            target = equilibrium_temp(need, room, self.model.c) + k
            floor = equilibrium_temp(P_MIN, room, self.model.c) + self.config.margin_c
            target = max(target, floor)
        if not self.model.reliable:
            # Lean on the last direct cycle correction before the model is trusted.
            target = 0.5 * target + 0.5 * self.model.last_target
        target = min(self.config.coolant_max, max(self.config.coolant_min, target))
        self.model.last_mode = mode
        self.model.last_target = target
        return target

    def _needs_soot_burn(self, now: float) -> bool:
        last = self.model.last_high_power_at
        if last is None:
            return self.model.ua_seconds > 3600
        return now - last >= self.config.soot_interval_s

    def _k_key(self, outdoor_c: float | None) -> str:
        if outdoor_c is None:
            outdoor_c = self._last_outdoor if self._last_outdoor is not None else 0.0
        return str(outdoor_bin(outdoor_c))

    def _k_for(self, outdoor_c: float | None) -> float:
        return float(self.model.k_bins.get(self._k_key(outdoor_c), 0.0))

    def _step_size(self, outdoor_c: float | None, raise_k: bool) -> float:
        key = self._k_key(outdoor_c)
        n = self.model.k_counts.get(key, 0)
        halves = self.model.step_halves.get(key, 0)
        base = self.config.base_step_c * max(0.3, 1.0 / (1.0 + n / 3.0))
        base /= 2 ** halves
        if raise_k:
            base *= self.config.step_up_ratio
        return base

    def _adjust_k(self, outdoor_c: float | None, step: float, raise_k: bool) -> None:
        key = self._k_key(outdoor_c)
        sign = 1 if raise_k else -1
        previous = self.model.k_last_sign.get(key, 0)
        if previous and previous != sign:
            self.model.step_halves[key] = self.model.step_halves.get(key, 0) + 1
            step = abs(step) / 2.0
        self.model.k_last_sign[key] = sign
        delta = step if raise_k else -step
        self._apply_k(key, delta, weight=1.0)
        # Spread learning to neighbouring outdoor bins.
        try:
            center = int(key)
        except ValueError:
            return
        self._apply_k(str(center - 1), delta, weight=0.5)
        self._apply_k(str(center + 1), delta, weight=0.5)
        self.model.k_counts[key] = self.model.k_counts.get(key, 0) + 1

    def _apply_k(self, key: str, delta: float, weight: float) -> None:
        current = self.model.k_bins.get(key, 0.0)
        self.model.k_bins[key] = current + delta * weight

    def _bump_confidence(self, outdoor_c: float | None) -> None:
        key = self._k_key(outdoor_c)
        self.model.k_counts[key] = self.model.k_counts.get(key, 0) + 1


def _is_liquid_standby(phase: str, burner_active: bool) -> bool:
    if phase == "cooldown":
        return True
    if phase == "running" and not burner_active:
        return True
    return False


def config_from_settings(settings: Any) -> AdaptiveConfig:
    """Build AdaptiveConfig from the add-on Settings object."""
    return AdaptiveConfig(
        enabled=bool(getattr(settings, "adaptive_enabled", False)),
        coolant_min=float(getattr(settings, "adaptive_coolant_min", 45.0)),
        coolant_max=float(getattr(settings, "adaptive_coolant_max", 85.0)),
        margin_c=float(getattr(settings, "adaptive_margin_c", 3.0)),
        base_step_c=float(getattr(settings, "adaptive_base_step_c", 2.0)),
        step_up_ratio=float(getattr(settings, "adaptive_step_up_ratio", 0.6)),
        power_confirm_s=float(getattr(settings, "adaptive_power_confirm_s", 1800.0)),
        power_lockout_s=float(getattr(settings, "adaptive_power_lockout_s", 5400.0)),
        soot_interval_s=float(getattr(settings, "adaptive_soot_interval_h", 24.0)) * 3600.0,
        soot_target_c=float(getattr(settings, "adaptive_soot_target_c", 80.0)),
        safe_coolant_c=float(getattr(settings, "adaptive_safe_coolant_c", 65.0)),
        room_hysteresis_c=float(getattr(settings, "room_drop", 2.0)),
        coolant_drop_c=float(getattr(settings, "coolant_drop", DEFAULT_COOLANT_DROP)),
    )


def _num(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}"
