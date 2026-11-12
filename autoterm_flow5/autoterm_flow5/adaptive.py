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
FREQ_MID = 3.0
FREQ_HIGH = 4.4
P_MAX = 5.0

# Power stages by the heater's own state name: Low, Middle, High.
STAGE_BY_STATE: dict[tuple[int, int], float] = {
    (3, 0): FREQ_LOW,
    (3, 8): FREQ_MID,
    (3, 2): FREQ_HIGH,
}
# States the heater reports while the burner is out but the session goes on.
NOT_BURNING_RUN_STATES = frozenset({4, 5, 6, 7})
STANDBY_STATES = frozenset({(3, 4), (3, 5), (3, 6), (1, 0), (4, 0), (4, 3)})

DEFAULT_UA = 0.15
DEFAULT_ROOM_FOR_C = 20.0
DEFAULT_T_EQ_AT_2KW = 50.0
DEFAULT_COOLANT_DROP = 18.0
DEFAULT_FIRST_TARGET = 60.0
K_BIN_WIDTH = 3.0
UA_MIN_WINDOW_S = 3 * 3600.0
UA_MAX_WINDOW_S = 12 * 3600.0
# Sensors report tenths: 15.4 - 15.1 is 0.3000000000000007 in floating point.
UA_ROOM_MATCH_C = 0.3
UA_MIN_MEAN_DELTA_C = 8.0
UA_MIN_ALPHA = 0.2
UA_LIMITS = (0.02, 1.0)
# A telemetry or sensor dropout shorter than this keeps the UA window.
UA_MAX_GAP_S = 5 * 60.0
# The open window is saved this often, and kept over an add-on restart
# that took at most UA_RESTART_GAP_S since that save.
UA_SAVE_EVERY_S = 5 * 60.0
UA_RESTART_GAP_S = 10 * 60.0
UA_MAX_ANCHORS = 48
LIQUID_MASS_LIMITS = (0.01, 0.1)
MODEL_VERSION = 2
C_STABLE_S = 600.0
C_STABLE_DELTA = 0.5
MODEL_MIN_C_SAMPLES = 3
MODEL_MIN_UA_SAMPLES = 2
OUTDOOR_RAPID_C_PER_H = 3.0
OUTDOOR_MAX_AGE_S = 1800.0
HOLD_MAX_S = 300.0

MODES = ("continuous", "intermittent", "maximum", "rise")
END_THERMOSTAT = "thermostat"
END_LIQUID_STANDBY = "liquid_standby"
END_ADAPTIVE = "adaptive"
END_FAULT = "fault"
END_MANUAL = "manual"
END_NO_BURN = "no_burn"


def power_kw(frequency_hz: float) -> float:
    if frequency_hz <= 0:
        return 0.0
    return P_MAX * frequency_hz / FREQ_HIGH


P_MIN = power_kw(FREQ_LOW)


def nearest_step(frequency_hz: float) -> float:
    if frequency_hz <= 0:
        return 0.0
    return min((FREQ_LOW, FREQ_MID, FREQ_HIGH), key=lambda step: abs(step - frequency_hz))


def is_burning(state: tuple[int, int] | None) -> bool:
    if state is None:
        return False
    if state in STAGE_BY_STATE:
        return True
    return state[0] == 3 and state[1] not in NOT_BURNING_RUN_STATES


def classify_step(state: tuple[int, int] | None, frequency_hz: float) -> float:
    if state in STAGE_BY_STATE:
        return STAGE_BY_STATE[state]
    return nearest_step(frequency_hz)


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
    coolant_min: float = 40.0
    coolant_max: float = 85.0
    margin_c: float = 3.0
    base_step_c: float = 2.0
    step_up_ratio: float = 0.6
    power_confirm_s: float = 30 * 60.0
    stop_lockout_s: float = 90 * 60.0
    max_stops: int = 2
    # Room further than hysteresis + this below target: the stop quota no
    # longer applies, stops may come sooner, and low power is not judged by UA.
    urgent_margin_c: float = 1.0
    urgent_lockout_s: float = 45 * 60.0
    urgent_confirm_s: float = 15 * 60.0
    stops_window_s: float = 6 * 3600.0
    min_burn_s: float = 20 * 60.0
    soot_interval_s: float = 24 * 3600.0
    soot_target_c: float = 80.0
    safe_coolant_c: float = 65.0
    room_hysteresis_c: float = 2.0
    standby_band_c: float = 2.0
    rise_offset_c: float = 1.0
    rise_exit_c: float = 0.3
    rise_target_c: float = 75.0
    # A room target raised during a burn stops the burn when the new coolant
    # target is at least this far above the heater's, once the new room target
    # has held for raise_settle_s (a card slider passes through values).
    raise_stop_min_c: float = 3.0
    raise_settle_s: float = 60.0
    startup_grace_s: float = 30.0
    verify_retries: int = 3
    verify_timeout_s: float = 45.0


@dataclass
class AdaptiveActions:
    """Side effects requested by one adaptive tick."""

    stop: bool = False
    start: bool = False
    coolant_limit: float | None = None
    hold_start: bool | None = None
    save_model: bool = False
    save_settings: bool = False


@dataclass
class _UaTotals:
    """Running energy balance since collection began, without a gap."""

    energy_kwh: float = 0.0
    delta_ch: float = 0.0
    seconds: float = 0.0


@dataclass
class _UaAnchor:
    """The totals at one ignition. A later ignition at the same room
    temperature closes the window, so heat stored in the house cancels."""

    at: float
    room_c: float
    coolant_c: float
    energy_kwh: float
    delta_ch: float
    seconds: float


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
    ua_samples: int = 0
    last_high_power_at: float | None = None
    ignitions: deque = field(default_factory=lambda: deque(maxlen=500))
    active_stops: deque = field(default_factory=lambda: deque(maxlen=50))
    run_durations: deque = field(default_factory=lambda: deque(maxlen=100))
    room_errors: deque = field(default_factory=lambda: deque(maxlen=100))
    cycle_log: deque = field(default_factory=lambda: deque(maxlen=50))
    last_target: float = DEFAULT_FIRST_TARGET
    last_mode: str = ""
    reliable: bool = False
    outdoor_history: deque = field(default_factory=lambda: deque(maxlen=120))
    ua_window: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": MODEL_VERSION,
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
            "ua_samples": self.ua_samples,
            "last_high_power_at": self.last_high_power_at,
            "ignitions": list(self.ignitions)[-200:],
            "active_stops": list(self.active_stops),
            "run_durations": list(self.run_durations),
            "room_errors": list(self.room_errors),
            "cycle_log": list(self.cycle_log)[-50:],
            "last_target": self.last_target,
            "last_mode": self.last_mode,
            "reliable": self.reliable,
            "ua_window": self.ua_window,
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
        model.ua_samples = int(raw.get("ua_samples", 0))
        high = raw.get("last_high_power_at")
        model.last_high_power_at = None if high is None else float(high)
        model.ignitions = deque((float(v) for v in raw.get("ignitions", [])), maxlen=500)
        model.active_stops = deque((float(v) for v in raw.get("active_stops", [])), maxlen=50)
        model.run_durations = deque((float(v) for v in raw.get("run_durations", [])), maxlen=100)
        model.room_errors = deque((float(v) for v in raw.get("room_errors", [])), maxlen=100)
        model.cycle_log = deque(raw.get("cycle_log", [])[-50:], maxlen=50)
        model.last_target = float(raw.get("last_target", DEFAULT_FIRST_TARGET))
        model.last_mode = str(raw.get("last_mode", ""))
        model.reliable = bool(raw.get("reliable", False))
        window = raw.get("ua_window")
        model.ua_window = window if isinstance(window, dict) else None
        if int(raw.get("version", 1)) < 2:
            # Version 1 counted heat stored while warming the house as loss.
            LOG.info("Adaptive UA %.3f kW/°C from the old estimate discarded; relearning", model.ua)
            model.ua = DEFAULT_UA
            model.ua_samples = 0
            model.reliable = False
        return model


@dataclass
class _LiveCycle:
    started_at: float
    outdoor_c: float | None
    room_start: float | None
    room_target: float
    coolant_target: float | None
    hysteresis_c: float
    mode: str
    learned: bool = True
    observe_only: bool = False
    burn_started_at: float | None = None
    burn_ended_at: float | None = None
    burn_s: float = 0.0
    time_low: float = 0.0
    time_mid: float = 0.0
    time_high: float = 0.0
    coolant_max: float | None = None
    corrections: list[str] = field(default_factory=list)
    last_step: float = 0.0
    standby_seen: bool = False
    standby_peak: float | None = None
    room_at_standby: float | None = None
    high_start_at: float | None = None
    high_start_coolant: float | None = None


@dataclass
class _StableWindow:
    step: float
    since: float
    coolant_start: float


@dataclass
class _Verify:
    target: float
    sent_at: float
    retries: int = 0


class AdaptiveController:
    """Observe heater telemetry and optionally steer the coolant target."""

    def __init__(self, path: Path, config: AdaptiveConfig | None = None) -> None:
        self.path = path
        self.config = config or AdaptiveConfig()
        self.model = self._load()
        self._phase = "unknown"
        self._cycle: _LiveCycle | None = None
        self._pause_started_at: float | None = None
        self._first_tick_at: float | None = None
        self._last_tick_at: float | None = None
        self._last_room_target: float | None = None
        self._last_hysteresis: float | None = None
        self._last_outdoor: float | None = None
        self._last_outdoor_at: float | None = None
        self._actual_coolant: float | None = None
        self._stable: _StableWindow | None = None
        self._power_low_since: float | None = None
        self._power_low_room: float | None = None
        self._pending_action: str | None = None
        self._pending_target: float | None = None
        self._verify: _Verify | None = None
        self._awaiting_restart = False
        self._holding_since: float | None = None
        self._rest_cleared = False
        self._ua_totals: _UaTotals | None = None
        self._ua_anchors: list[_UaAnchor] = []
        self._ua_gap_s = 0.0
        self._ua_saved_at: float | None = None
        self._ua_gap_limit_s = UA_MAX_GAP_S
        self._ua_restore = self.model.ua_window
        self._next_cycle_unlearned = False
        self._soot_burn_active = False
        self._rise_active = False
        self._manual_cycle = False
        self._enabled_was = self.config.enabled
        self._faulted = False
        self._quota_warned = False
        self._restore_manual_pending = False
        self._manual_coolant: float | None = None
        self._wrote_target_this_rest = False
        self._retarget_pending = False
        self._room_stopped = False
        self._retarget_from: float | None = None
        self._raise_since: float | None = None
        self._raise_from = 0.0
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
        if self._ua_totals is not None and self._last_tick_at is not None:
            self.model.ua_window = {
                "saved_at": self._last_tick_at,
                "totals": asdict(self._ua_totals),
                "anchors": [asdict(anchor) for anchor in self._ua_anchors],
            }
        elif self._ua_restore is None:
            self.model.ua_window = None
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
        self.model = AdaptiveModel()
        self._stable = None
        self._power_low_since = None
        self._soot_burn_active = False
        self._rise_active = False
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
            "adaptive_actual_coolant": "" if self._actual_coolant is None else f"{self._actual_coolant:.0f}",
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
        return sum(1 for stamp in self.model.ignitions if stamp >= latest - 86400.0)

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
            self._verify = None
            self._awaiting_restart = False
            self._wrote_target_this_rest = False
            if self._cycle is not None:
                # The burn in progress is neither stopped nor analysed.
                self._cycle.observe_only = True
                self._cycle.learned = False
            LOG.info("Adaptive control enabled; manual coolant %.0f °C saved", current_coolant)
        elif not enabled and self._enabled_was:
            self._pending_action = None
            self._power_low_since = None
            self._verify = None
            if self._awaiting_restart:
                actions.start = True
            self._awaiting_restart = False
            self._release_hold(actions)
            actions.hold_start = False
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
        state: tuple[int, int] | None,
        phase: str,
        fault: int,
        heat_enabled: bool,
        mode: str,
        room_c: float | None,
        outdoor_c: float | None,
        coolant_c: float | None,
        pump_hz: float,
        room_target: float,
        hysteresis_c: float,
        confirmed_coolant: float | None,
        room_stopped: bool = False,
    ) -> AdaptiveActions:
        actions = AdaptiveActions()
        self._room_stopped = room_stopped
        if self._first_tick_at is None:
            self._first_tick_at = now
        dt = 0.0 if self._last_tick_at is None else max(0.0, min(30.0, now - self._last_tick_at))
        self._last_tick_at = now
        if self._ua_restore is not None:
            self._restore_ua_window(now)
        if confirmed_coolant is not None:
            self._actual_coolant = confirmed_coolant
        self._update_reliability()

        if outdoor_c is not None:
            self.model.outdoor_history.append((now, outdoor_c))
            self._last_outdoor = outdoor_c
            self._last_outdoor_at = now
        elif self._last_outdoor_at is not None and now - self._last_outdoor_at <= OUTDOOR_MAX_AGE_S:
            outdoor_c = self._last_outdoor

        settings_changed = self._settings_changed(room_target, hysteresis_c)
        skip_learning = settings_changed or self._outdoor_changing_fast(now) or self._manual_cycle
        burning = is_burning(state)
        step = classify_step(state, pump_hz) if burning else 0.0
        telemetry_ok = state is not None and coolant_c is not None

        if telemetry_ok and outdoor_c is not None and room_c is not None:
            if dt > 0 or self._ua_gap_s > 0:
                self._accumulate_ua(dt + self._ua_gap_s, room_c, outdoor_c, pump_hz if burning else 0.0)
            self._ua_gap_s = 0.0
            self._ua_gap_limit_s = UA_MAX_GAP_S
        elif self._ua_totals is not None:
            self._ua_gap_s += dt
            if self._ua_gap_s > self._ua_gap_limit_s:
                LOG.info(
                    "UA windows dropped: telemetry or a temperature was missing for %.0f min",
                    self._ua_gap_s / 60,
                )
                self._ua_totals = None
                self._ua_anchors = []
                self._ua_gap_s = 0.0
        if self._ua_totals is not None and (
            self._ua_saved_at is None or now - self._ua_saved_at >= UA_SAVE_EVERY_S
        ):
            self._ua_saved_at = now
            actions.save_model = True
        if burning and coolant_c is not None and room_c is not None and dt > 0:
            self._observe_stability(now, step, pump_hz, coolant_c, room_c)

        at_rest = phase in {"idle", "unknown"}
        if not heat_enabled or mode != "thermostat" or not at_rest:
            self._rest_cleared = False
        elif self.config.enabled and not self._rest_cleared:
            # The thermostat would otherwise start before this rest's coolant target is settled.
            self._hold(now, actions)

        in_grace = now - self._first_tick_at < self.config.startup_grace_s
        missing = not telemetry_ok or room_c is None or outdoor_c is None
        if fault or (missing and not in_grace):
            self._enter_fault(phase, actions)
            self._phase = phase
            return actions
        if missing:
            self._phase = phase
            return actions
        if self._faulted:
            LOG.info("Adaptive control sensors and heater are back")
        self._faulted = False

        entering_ignition = phase == "starting" and self._phase != "starting"
        if entering_ignition:
            self._ua_checkpoint(now, room_c, coolant_c)
            actions.save_model = True
        if entering_ignition and heat_enabled and mode == "thermostat":
            if self._cycle is not None:
                self._learn_relight_drop(coolant_c)
                self._finish_cycle(now, room_c, self._end_reason(fault), skip_learning)
            self._begin_cycle(now, outdoor_c, room_c, room_target, hysteresis_c)
            actions.save_model = True
        elif self._cycle is not None and phase in {"idle", "locked"}:
            self._finish_cycle(now, room_c, self._end_reason(fault), skip_learning)
            actions.save_model = True
            self._manual_cycle = False

        if self._cycle is not None and dt > 0:
            self._sample_cycle(now, dt, state, step, burning, pump_hz, coolant_c, room_c)

        if not at_rest:
            self._wrote_target_this_rest = False
            if self._verify is not None:
                LOG.warning("Heater left idle before the coolant target was verified")
                self._verify = None
                self._next_cycle_unlearned = True
            self._release_hold(actions)
        if not heat_enabled:
            self._wrote_target_this_rest = False

        if not self.config.enabled:
            if self._restore_manual_pending and phase == "idle":
                restore = self._manual_coolant
                if restore is not None:
                    actions.coolant_limit = restore
                    actions.save_settings = True
                self._restore_manual_pending = False
            self._phase = phase
            return actions

        if mode != "thermostat" or not heat_enabled:
            self._pending_action = None
            self._verify = None
            self._awaiting_restart = False
            self._release_hold(actions)
            self._phase = phase
            return actions

        if self._rise_active and room_c >= room_target - self.config.rise_exit_c:
            self._rise_active = False
            LOG.info("Adaptive RISE mode ended: room %.1f °C near target %.1f °C", room_c, room_target)

        if self._retarget_pending:
            self._retarget_pending = False
            self._retarget(now, phase, room_target)
        if phase not in {"starting", "running"}:
            self._raise_since = None
        elif self._raise_since is not None and self._pending_action is None and not self._awaiting_restart:
            self._raise_stop(now, actions, outdoor_c, room_c, room_target)

        if phase == "idle":
            self._rest(now, actions, outdoor_c, room_c, room_target, confirmed_coolant)

        cycle = self._cycle
        if (
            cycle is not None
            and not cycle.observe_only
            and not skip_learning
            and not self._rise_active
            and self._pending_action is None
        ):
            correction = self._active_correction(
                now,
                cycle=cycle,
                state=state,
                burning=burning,
                step=step,
                room_c=room_c,
                outdoor_c=outdoor_c,
                pump_hz=pump_hz,
                room_target=room_target,
            )
            if correction is not None:
                kind, target = correction
                cycle.corrections.append(f"{kind}:{target:.0f}")
                self._pending_action = kind
                self._pending_target = target
                self._awaiting_restart = True
                self.model.active_stops.append(now)
                actions.stop = True
                actions.save_model = True
                LOG.info(
                    "Adaptive correction %s: stopping to raise coolant %.0f -> %.0f °C (burn %.0f min)",
                    kind,
                    self._actual_coolant if self._actual_coolant is not None else float("nan"),
                    target,
                    cycle.burn_s / 60,
                )

        if self._soot_burn_active and cycle is not None and cycle.time_high >= 15 * 60:
            self._soot_burn_active = False
            actions.save_model = True

        self._phase = phase
        self._refresh_status()
        return actions

    def _enter_fault(self, phase: str, actions: AdaptiveActions) -> None:
        if not self._faulted:
            LOG.warning("Adaptive control entering safe coolant because sensors or heater failed")
        self._faulted = True
        if self._cycle is not None:
            self._cycle.learned = False
        self._verify = None
        self._pending_action = None
        if self._awaiting_restart:
            actions.start = True
            self._awaiting_restart = False
        self._release_hold(actions)
        self._rest_cleared = True
        if self.config.enabled and phase == "idle" and not self._wrote_target_this_rest:
            actions.coolant_limit = self.config.safe_coolant_c
            self.model.last_target = self.config.safe_coolant_c
            self._wrote_target_this_rest = True
            actions.save_model = True

    def _hold(self, now: float, actions: AdaptiveActions) -> None:
        if self._holding_since is None:
            self._holding_since = now
        elif now - self._holding_since > HOLD_MAX_S:
            LOG.warning("Adaptive start hold timed out; letting the heater start")
            self._verify = None
            self._next_cycle_unlearned = True
            self._rest_ready(actions)
            return
        actions.hold_start = True

    def _release_hold(self, actions: AdaptiveActions) -> None:
        if self._holding_since is not None:
            actions.hold_start = False
        self._holding_since = None

    def _retarget(self, now: float, phase: str, room_target: float) -> None:
        """A new room target needs a new coolant target now, not after the next burn."""
        previous, self._retarget_from = self._retarget_from, None
        if self._pending_action is not None or self._awaiting_restart:
            return
        if phase == "idle":
            LOG.info("Room target changed to %.1f °C; recomputing the coolant target", room_target)
            self._verify = None
            self._wrote_target_this_rest = False
            self._rest_cleared = False
            return
        if phase in {"starting", "running"}:
            # This Flow 5 keeps its stored settings while it burns.
            LOG.info(
                "Room target changed to %.1f °C during the burn; the coolant target follows at the next rest",
                room_target,
            )
            if previous is not None and room_target > previous:
                if self._raise_since is None:
                    self._raise_from = previous
                self._raise_since = now
            elif previous is not None and room_target < previous:
                self._raise_since = None

    def _raise_stop(
        self,
        now: float,
        actions: AdaptiveActions,
        outdoor_c: float | None,
        room_c: float,
        room_target: float,
    ) -> None:
        """Stop a burn whose stored coolant target is far below what a raised room target needs.

        The heater only stores a new target at rest, so the burn is stopped,
        the target written and the heater restarted. The minimum burn time
        still applies unless the heater already rests in liquid standby.
        """
        since = self._raise_since
        cycle = self._cycle
        actual = self._actual_coolant
        if since is None or now - since < self.config.raise_settle_s:
            return
        if cycle is None or actual is None:
            self._raise_since = None
            return
        target, mode = self._raise_stop_target(outdoor_c, room_c, room_target)
        before, _ = self._raise_stop_target(outdoor_c, room_c, self._raise_from)
        if target < max(actual, before) + self.config.raise_stop_min_c:
            self._raise_since = None
            return
        resting = cycle.burn_started_at is not None and cycle.burn_ended_at is not None
        if cycle.burn_s < self.config.min_burn_s and not resting:
            return
        if not self._stop_allowed(now, urgent=True):
            return
        self._raise_since = None
        if mode == "rise":
            self._rise_active = True
        self.model.last_mode = mode
        cycle.corrections.append(f"target_raise:{target:.0f}")
        self._pending_action = "target_raise"
        self._pending_target = target
        self._awaiting_restart = True
        self.model.active_stops.append(now)
        actions.stop = True
        actions.save_model = True
        LOG.info(
            "Room target raised to %.1f °C: stopping to raise coolant %.0f -> %.0f °C (burn %.0f min)",
            room_target,
            actual,
            target,
            cycle.burn_s / 60,
        )

    def _rest(
        self,
        now: float,
        actions: AdaptiveActions,
        outdoor_c: float | None,
        room_c: float,
        room_target: float,
        confirmed: float | None,
    ) -> None:
        if self._verify is not None:
            self._check_verify(now, actions, confirmed)
            return
        if self._wrote_target_this_rest:
            return
        self._wrote_target_this_rest = True
        if self._pending_action is not None:
            target = self._pending_target
            self._pending_action = None
        else:
            target = self._rest_target(now, outdoor_c, room_c, room_target)
        # The heater stores whole degrees.
        target = float(round(target))
        actual = self._actual_coolant
        if actual is not None and abs(actual - target) < 0.5:
            self.model.last_target = actual
            self._rest_ready(actions)
            return
        self._verify = _Verify(target=target, sent_at=now)
        actions.coolant_limit = target
        actions.save_settings = True
        actions.save_model = True
        self._hold(now, actions)
        LOG.info(
            "Adaptive rest: writing coolant target %.0f °C (heater has %s), mode=%s",
            target,
            "unknown" if actual is None else f"{actual:.0f} °C",
            self.model.last_mode,
        )

    def _check_verify(self, now: float, actions: AdaptiveActions, confirmed: float | None) -> None:
        verify = self._verify
        if verify is None:
            return
        self._hold(now, actions)
        if confirmed is not None and now > verify.sent_at:
            if abs(confirmed - verify.target) < 0.5:
                LOG.info("Heater confirmed coolant target %.0f °C", confirmed)
                self.model.last_target = confirmed
                self._verify = None
                self._rest_ready(actions)
                return
            self._retry_or_fail(now, actions, f"heater reports {confirmed:.0f} °C")
        elif now - verify.sent_at >= self.config.verify_timeout_s:
            self._retry_or_fail(now, actions, "no read-back")

    def _retry_or_fail(self, now: float, actions: AdaptiveActions, why: str) -> None:
        verify = self._verify
        if verify is None:
            return
        if verify.retries < self.config.verify_retries:
            verify.retries += 1
            verify.sent_at = now
            actions.coolant_limit = verify.target
            LOG.warning(
                "Coolant target %.0f °C not stored (%s); retry %d/%d",
                verify.target,
                why,
                verify.retries,
                self.config.verify_retries,
            )
            return
        LOG.error(
            "Heater did not store coolant target %.0f °C (%s); starting with its own setting, cycle not learned",
            verify.target,
            why,
        )
        self._verify = None
        self._next_cycle_unlearned = True
        self._rest_ready(actions)

    def _rest_ready(self, actions: AdaptiveActions) -> None:
        self._release_hold(actions)
        self._rest_cleared = True
        actions.hold_start = False
        if self._awaiting_restart:
            actions.start = True
            self._awaiting_restart = False

    def _rest_target(self, now: float, outdoor_c: float | None, room_c: float, room_target: float) -> float:
        if room_c < room_target - self.config.rise_offset_c:
            if not self._rise_active:
                LOG.info("Adaptive RISE mode: room %.1f °C is over %.1f °C below target", room_c, self.config.rise_offset_c)
            self._rise_active = True
        if self._rise_active:
            target = self._clamp(self.config.rise_target_c)
            self.model.last_mode = "rise"
        else:
            target, mode = self._predict_target(outdoor_c, room_c, room_target)
            self.model.last_mode = mode
        if self._needs_soot_burn(now):
            target = max(target, self._clamp(self.config.soot_target_c))
            self._soot_burn_active = True
            LOG.info("Soot-prevention burn scheduled at %.0f °C", target)
        self.model.last_target = target
        return target

    def _refresh_status(self) -> None:
        if not self.config.enabled:
            self._status = "off"
        elif not self.model.reliable or self._faulted:
            self._status = "learning"
        else:
            self._status = "active"

    def _update_reliability(self) -> None:
        self.model.reliable = (
            self.model.c_samples >= MODEL_MIN_C_SAMPLES and self.model.ua_samples >= MODEL_MIN_UA_SAMPLES
        )

    def _settings_changed(self, room_target: float, hysteresis_c: float) -> bool:
        changed = False
        if self._last_room_target is not None and abs(self._last_room_target - room_target) > 0.05:
            changed = True
            self._retarget_pending = True
            if self._retarget_from is None:
                self._retarget_from = self._last_room_target
        if self._last_hysteresis is not None and abs(self._last_hysteresis - hysteresis_c) > 0.05:
            changed = True
        self._last_room_target = room_target
        self._last_hysteresis = hysteresis_c
        if changed and self._cycle is not None:
            self._cycle.learned = False
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
        totals = self._ua_totals
        if totals is None:
            return
        totals.energy_kwh += power_kw(pump_hz) * (dt / 3600.0)
        totals.delta_ch += max(0.0, room_c - outdoor_c) * (dt / 3600.0)
        totals.seconds += dt

    def _restore_ua_window(self, now: float) -> None:
        raw = self._ua_restore or {}
        self._ua_restore = None
        self.model.ua_window = None
        saved_at = raw.get("saved_at")
        gap = None if saved_at is None else now - float(saved_at)
        if gap is None or not 0 <= gap <= UA_RESTART_GAP_S:
            LOG.info("UA windows from before the restart dropped: the add-on was down too long")
            return
        try:
            if "totals" in raw:
                totals = _UaTotals(**{k: float(v) for k, v in raw["totals"].items()})
                anchors = [
                    _UaAnchor(**{k: float(v) for k, v in item.items()}) for item in raw.get("anchors", [])
                ]
            else:
                # 0.5.7 saved one window as its own totals since its ignition.
                totals = _UaTotals(
                    energy_kwh=float(raw.get("energy_kwh", 0.0)),
                    delta_ch=float(raw.get("delta_ch", 0.0)),
                    seconds=float(raw.get("seconds", 0.0)),
                )
                anchors = [_UaAnchor(
                    at=float(raw["started_at"]),
                    room_c=float(raw["room_c"]),
                    coolant_c=float(raw["coolant_c"]),
                    energy_kwh=0.0,
                    delta_ch=0.0,
                    seconds=0.0,
                )]
        except (KeyError, TypeError, ValueError):
            return
        self._ua_totals = totals
        self._ua_anchors = anchors
        self._ua_gap_s = gap
        self._ua_gap_limit_s = gap + UA_MAX_GAP_S
        LOG.info("UA windows resumed after the restart: %d open", len(anchors))

    def _ua_checkpoint(self, now: float, room_c: float, coolant_c: float) -> None:
        """Every ignition opens a window; the oldest one at least UA_MIN_WINDOW_S
        old with the room back within UA_ROOM_MATCH_C gives the sample."""
        if self._ua_totals is None:
            self._ua_totals = _UaTotals()
            self._ua_anchors = []
        totals = self._ua_totals
        anchors = [a for a in self._ua_anchors if now - a.at <= UA_MAX_WINDOW_S]
        match = next(
            (
                a
                for a in anchors
                if totals.seconds - a.seconds >= UA_MIN_WINDOW_S and abs(room_c - a.room_c) <= UA_ROOM_MATCH_C + 1e-6
            ),
            None,
        )
        if match is not None:
            self._ua_sample(match, totals, room_c, coolant_c)
            # Later windows overlap the one just used; start fresh from here.
            anchors = []
        elif anchors:
            oldest = anchors[0]
            LOG.info(
                "UA windows open: %d, oldest %.1f h from room %.1f °C; room now %.1f °C "
                "(a sample needs %.0f h and a room within %.1f °C)",
                len(anchors),
                (totals.seconds - oldest.seconds) / 3600,
                oldest.room_c,
                room_c,
                UA_MIN_WINDOW_S / 3600,
                UA_ROOM_MATCH_C,
            )
        else:
            LOG.info(
                "UA window started at ignition, room %.1f °C; a sample needs an ignition %.0f-%.0f h later "
                "with the room within %.1f °C",
                room_c,
                UA_MIN_WINDOW_S / 3600,
                UA_MAX_WINDOW_S / 3600,
                UA_ROOM_MATCH_C,
            )
        anchors.append(_UaAnchor(
            at=now,
            room_c=room_c,
            coolant_c=coolant_c,
            energy_kwh=totals.energy_kwh,
            delta_ch=totals.delta_ch,
            seconds=totals.seconds,
        ))
        self._ua_anchors = anchors[-UA_MAX_ANCHORS:]

    def _ua_sample(self, anchor: _UaAnchor, totals: _UaTotals, room_c: float, coolant_c: float) -> None:
        seconds = totals.seconds - anchor.seconds
        delta_ch = totals.delta_ch - anchor.delta_ch
        hours = seconds / 3600.0
        mean_delta = delta_ch / hours
        if mean_delta < UA_MIN_MEAN_DELTA_C:
            LOG.info(
                "UA window skipped: mean indoor-outdoor difference %.1f °C is too small to measure",
                mean_delta,
            )
            return
        mass = self.model.thermal_mass_kwh_per_c
        liquid_mass = LIQUID_MASS_LIMITS[0] if mass is None else _bounded(mass, *LIQUID_MASS_LIMITS)
        energy = totals.energy_kwh - anchor.energy_kwh - liquid_mass * (coolant_c - anchor.coolant_c)
        measured = _bounded(energy / delta_ch, *UA_LIMITS)
        alpha = max(1.0 / (self.model.ua_samples + 1), UA_MIN_ALPHA)
        previous = self.model.ua
        self.model.ua = (1.0 - alpha) * previous + alpha * measured
        self.model.ua_samples += 1
        LOG.info(
            "UA sample %.3f kW/°C over %.1f h (heat %.1f kWh, mean difference %.1f °C, room %.1f -> %.1f °C); "
            "UA %.3f -> %.3f kW/°C",
            measured,
            hours,
            energy,
            mean_delta,
            anchor.room_c,
            room_c,
            previous,
            self.model.ua,
        )

    def _observe_stability(self, now: float, step: float, pump_hz: float, coolant_c: float, room_c: float) -> None:
        if step <= 0 or coolant_c <= room_c + 1:
            self._stable = None
            return
        if self._stable is None or self._stable.step != step:
            self._stable = _StableWindow(step=step, since=now, coolant_start=coolant_c)
            return
        if now - self._stable.since < C_STABLE_S:
            return
        if abs(coolant_c - self._stable.coolant_start) > C_STABLE_DELTA:
            self._stable = _StableWindow(step=step, since=now, coolant_start=coolant_c)
            return
        measured = power_kw(pump_hz) / (max(1.0, coolant_c - room_c) ** 1.3)
        self.model.c_samples += 1
        alpha = 1.0 / min(self.model.c_samples, 10)
        self.model.c = (1.0 - alpha) * self.model.c + alpha * measured
        self._stable = _StableWindow(step=step, since=now, coolant_start=coolant_c)
        LOG.info("Adaptive radiator C updated to %.5f from %.1f Hz plateau", self.model.c, pump_hz)

    def _begin_cycle(
        self,
        now: float,
        outdoor_c: float | None,
        room_c: float | None,
        room_target: float,
        hysteresis_c: float,
    ) -> None:
        if self._pause_started_at is not None and self.model.cycle_log:
            previous = dict(self.model.cycle_log[-1])
            previous["pause_s"] = round(max(0.0, now - self._pause_started_at), 1)
            self.model.cycle_log[-1] = previous
        self._pause_started_at = None
        unlearned = self._next_cycle_unlearned or self._manual_cycle
        self._next_cycle_unlearned = False
        self._cycle = _LiveCycle(
            started_at=now,
            outdoor_c=outdoor_c,
            room_start=room_c,
            room_target=room_target,
            coolant_target=self._actual_coolant,
            hysteresis_c=hysteresis_c,
            mode=self.model.last_mode or "",
            learned=not unlearned,
            observe_only=not self.config.enabled,
        )
        self.model.ignitions.append(now)
        LOG.info(
            "Adaptive cycle start (ignition): mode=%s heater coolant=%s outdoor=%s%s",
            self._cycle.mode or "-",
            "unknown" if self._actual_coolant is None else f"{self._actual_coolant:.0f}",
            "n/a" if outdoor_c is None else f"{outdoor_c:.1f}",
            "" if self._cycle.learned else " (not learned)",
        )

    def _sample_cycle(
        self,
        now: float,
        dt: float,
        state: tuple[int, int] | None,
        step: float,
        burning: bool,
        pump_hz: float,
        coolant_c: float,
        room_c: float,
    ) -> None:
        cycle = self._cycle
        if cycle is None:
            return
        if burning:
            if cycle.burn_started_at is None:
                cycle.burn_started_at = now
                LOG.info("Adaptive burn phase started at %.1f Hz", pump_hz)
            cycle.burn_ended_at = None
            cycle.burn_s += dt
            if step == FREQ_LOW:
                cycle.time_low += dt
            elif step == FREQ_MID:
                cycle.time_mid += dt
            elif step == FREQ_HIGH:
                cycle.time_high += dt
                self.model.last_high_power_at = now
                self._observe_mass(cycle, now, coolant_c)
            cycle.coolant_max = coolant_c if cycle.coolant_max is None else max(cycle.coolant_max, coolant_c)
            if cycle.last_step and step != cycle.last_step and cycle.coolant_target is not None:
                gap = cycle.coolant_target - coolant_c
                (self.model.threshold_up if step > cycle.last_step else self.model.threshold_down).append(gap)
            cycle.last_step = step
            return
        if cycle.burn_started_at is not None and cycle.burn_ended_at is None:
            cycle.burn_ended_at = now
        if cycle.standby_seen:
            cycle.standby_peak = coolant_c if cycle.standby_peak is None else max(cycle.standby_peak, coolant_c)
            return
        target = cycle.coolant_target
        if (
            # After the add-on thermostat's stop, the heater's cooldown at a
            # liquid near target is not the heater reaching its target.
            not self._room_stopped
            and cycle.burn_started_at is not None
            and state in STANDBY_STATES
            and target is not None
            and coolant_c >= target - self.config.standby_band_c
            and pump_hz <= 0.05
        ):
            cycle.standby_seen = True
            cycle.standby_peak = coolant_c
            cycle.room_at_standby = room_c
            LOG.info(
                "Liquid standby detected: state %s.%s, liquid %.0f °C, target %.0f °C, pump %.2f Hz, room %.1f °C",
                state[0],
                state[1],
                coolant_c,
                target,
                pump_hz,
                room_c,
            )

    def _observe_mass(self, cycle: _LiveCycle, now: float, coolant_c: float) -> None:
        if cycle.high_start_at is None:
            cycle.high_start_at = now
            cycle.high_start_coolant = coolant_c
            return
        if cycle.high_start_coolant is None or now - cycle.high_start_at < 300:
            return
        rise = coolant_c - cycle.high_start_coolant
        if rise > 2:
            hours = (now - cycle.high_start_at) / 3600.0
            self.model.thermal_mass_kwh_per_c = (P_MAX * hours) / rise

    def _learn_relight_drop(self, coolant_c: float) -> None:
        cycle = self._cycle
        if cycle is None or not cycle.standby_seen or cycle.standby_peak is None:
            return
        drop = cycle.standby_peak - coolant_c
        if 5 < drop < 40:
            self.model.coolant_drop_c = 0.7 * self.model.coolant_drop_c + 0.3 * drop
            LOG.info("Liquid standby relight drop %.1f °C (learned %.1f °C)", drop, self.model.coolant_drop_c)

    def _end_reason(self, fault: int) -> str:
        cycle = self._cycle
        if fault:
            return END_FAULT
        if self._pending_action is not None:
            return END_ADAPTIVE
        if self._manual_cycle:
            return END_MANUAL
        if cycle is not None and cycle.burn_started_at is None:
            return END_NO_BURN
        if cycle is not None and cycle.standby_seen:
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
        burn_s = cycle.burn_s
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
            "burn_s": round(burn_s, 1),
            "pause_s": 0.0,
            "share_1_8": round(cycle.time_low / burn_s, 3) if burn_s else 0.0,
            "share_3_0": round(cycle.time_mid / burn_s, 3) if burn_s else 0.0,
            "share_4_4": round(cycle.time_high / burn_s, 3) if burn_s else 0.0,
            "coolant_max": cycle.coolant_max,
            "end_reason": end_reason,
            "corrections": list(cycle.corrections),
            "mode": cycle.mode,
            "learned": cycle.learned and not cycle.observe_only and not skip_learning,
        }
        self.model.cycle_log.append(stats)
        LOG.info(
            "Adaptive cycle end: reason=%s run=%.0fs burn=%.0fs shares=%.0f/%.0f/%.0f max_liquid=%s target=%s%s",
            end_reason,
            run_s,
            burn_s,
            100 * stats["share_1_8"],
            100 * stats["share_3_0"],
            100 * stats["share_4_4"],
            "n/a" if cycle.coolant_max is None else f"{cycle.coolant_max:.0f}",
            "n/a" if cycle.coolant_target is None else f"{cycle.coolant_target:.0f}",
            "" if stats["learned"] else " (not learned)",
        )
        if end_reason == END_THERMOSTAT and self._rise_active:
            self._rise_active = False
            LOG.info("Adaptive RISE mode ended by the thermostat")
        if not stats["learned"] or not self.config.enabled:
            return
        if end_reason in {END_FAULT, END_MANUAL, END_ADAPTIVE, END_NO_BURN}:
            return
        if cycle.coolant_target is None or burn_s < 60:
            return
        self._learn_from_cycle(cycle, stats, room_c)

    def _learn_from_cycle(self, cycle: _LiveCycle, stats: dict[str, Any], room_c: float | None) -> None:
        if cycle.mode == "rise":
            return
        outdoor = cycle.outdoor_c
        actual = float(cycle.coolant_target)
        end_reason = stats["end_reason"]
        corrections: list[str] = []
        if end_reason == END_LIQUID_STANDBY and (
            cycle.room_at_standby is None or cycle.room_at_standby < cycle.room_target
        ):
            step = self._step_size(outdoor, raise_k=True)
            self._adjust_k(outdoor, step, raise_k=True)
            corrections.append(f"k_up_standby:{step:.2f}")
            if not self.model.reliable:
                self.model.last_target = max(actual, self._clamp(actual + step))
        elif end_reason == END_THERMOSTAT:
            if stats["share_4_4"] >= 0.8 and cycle.coolant_max is not None:
                lowered = max(self.config.coolant_min, cycle.coolant_max - 2.0, actual - 5.0)
                lowered = min(lowered, actual)
                if lowered < actual - 0.1:
                    self._adjust_k(outdoor, actual - lowered, raise_k=False)
                    self.model.last_target = lowered
                    corrections.append(f"target_down_high:{lowered - actual:.1f}")
            elif stats["share_3_0"] >= 0.5 and cycle.mode == "continuous":
                step = self._step_size(outdoor, raise_k=False)
                self._adjust_k(outdoor, step, raise_k=False)
                corrections.append(f"k_down_mid:{step:.2f}")
            elif stats["share_1_8"] >= 0.5:
                corrections.append("intermittent_hint")
                if outdoor is not None:
                    need = self.model.ua * max(0.0, cycle.room_target - outdoor)
                    if need > P_MIN * 0.8:
                        self.model.ua = max(0.02, self.model.ua * 0.95)
                        corrections.append("ua_ease")
            else:
                self._bump_confidence(outdoor)
                corrections.append("good_cycle")
        if corrections:
            stats["corrections"] = list(stats.get("corrections", [])) + corrections
            if self.model.cycle_log:
                self.model.cycle_log[-1] = stats

    def _stop_allowed(self, now: float, urgent: bool = False) -> bool:
        window = self.config.stops_window_s
        recent = [stamp for stamp in self.model.active_stops if now - stamp < window]
        lockout = self.config.urgent_lockout_s if urgent else self.config.stop_lockout_s
        if recent and now - recent[-1] < lockout:
            return False
        if len(recent) >= self.config.max_stops and urgent:
            LOG.warning(
                "Adaptive stop quota (%d in %.0f h) bypassed for an urgent raise",
                self.config.max_stops,
                window / 3600,
            )
            return True
        if len(recent) >= self.config.max_stops:
            if not self._quota_warned:
                LOG.warning(
                    "Adaptive control already stopped the heater %d times in %.0f h; active corrections paused",
                    len(recent),
                    window / 3600,
                )
                self._quota_warned = True
            return False
        self._quota_warned = False
        return True

    def _raise_stop_target(self, outdoor_c: float | None, room_c: float, room_target: float) -> tuple[float, str]:
        if room_c < room_target - self.config.rise_offset_c:
            return self._clamp(self.config.rise_target_c), "rise"
        target, mode = self._predict_target(outdoor_c, room_c, room_target)
        return self._clamp(float(math.ceil(target - 0.05))), mode

    def _active_correction(
        self,
        now: float,
        *,
        cycle: _LiveCycle,
        state: tuple[int, int] | None,
        burning: bool,
        step: float,
        room_c: float,
        outdoor_c: float | None,
        pump_hz: float,
        room_target: float,
    ) -> tuple[str, float] | None:
        del state
        if cycle.burn_s < self.config.min_burn_s:
            self._power_low_since = None
            return None

        urgent = room_c < room_target - self.config.room_hysteresis_c - self.config.urgent_margin_c

        # 6.1 Liquid standby while the room is still below the target.
        if cycle.standby_seen and not burning and room_c < room_target:
            if not self._stop_allowed(now, urgent):
                return None
            target = self._raise_target("liquid_standby_raise", outdoor_c, room_c, room_target)
            return None if target is None else ("liquid_standby_raise", self._urgent_target(target, urgent))

        # 6.2 Power insufficient for the confirm time.
        need = self._heat_need(outdoor_c, room_target)
        room_floor = room_target - self.config.room_hysteresis_c - 0.3
        low_power = burning and step in {FREQ_LOW, FREQ_MID} and (urgent or power_kw(pump_hz) < need)
        if not (low_power and room_c < room_floor):
            self._power_low_since = None
            return None
        if self._power_low_since is None:
            self._power_low_since = now
            self._power_low_room = room_c
            return None
        elapsed = now - self._power_low_since
        confirm = self.config.urgent_confirm_s if urgent else self.config.power_confirm_s
        if elapsed < confirm:
            return None
        rate = (room_c - (self._power_low_room or room_c)) / max(elapsed / 3600.0, 1e-3)
        if rate >= 0.2:
            self._power_low_since = now
            self._power_low_room = room_c
            return None
        if not self._stop_allowed(now, urgent):
            return None
        self._power_low_since = None
        target = self._raise_target("power_raise", outdoor_c, room_c, room_target)
        return None if target is None else ("power_raise", self._urgent_target(target, urgent))

    def _urgent_target(self, target: float, urgent: bool) -> float:
        """A room this far below target goes straight to RISE instead of a small step."""
        if not urgent:
            return target
        if not self._rise_active:
            LOG.info("Adaptive RISE mode: room fell over %.1f °C below target during the burn",
                     self.config.room_hysteresis_c + self.config.urgent_margin_c)
        self._rise_active = True
        return max(target, self._clamp(self.config.rise_target_c))

    def _raise_target(self, kind: str, outdoor_c: float | None, room_c: float, room_target: float) -> float | None:
        base = self._actual_coolant if self._actual_coolant is not None else self.model.last_target
        step = self._step_size(outdoor_c, raise_k=True)
        self._adjust_k(outdoor_c, step, raise_k=True)
        predicted, _mode = self._predict_target(outdoor_c, room_c, room_target)
        if predicted <= base + 0.1:
            target = base + max(step, 1.0)
            LOG.info(
                "Adaptive %s: model target %.1f °C is not above the heater's %.1f °C; using direct step to %.1f °C",
                kind,
                predicted,
                base,
                target,
            )
        else:
            target = predicted
        target = self._clamp(float(math.ceil(target - 0.05)))
        if target <= base + 0.4:
            LOG.info("Adaptive %s skipped: coolant target already at the upper limit %.0f °C", kind, base)
            return None
        return target

    def _heat_need(self, outdoor_c: float | None, room_target: float) -> float:
        if outdoor_c is None:
            outdoor_c = self._last_outdoor if self._last_outdoor is not None else room_target - 15.0
        return max(0.0, self.model.ua * (room_target - outdoor_c))

    def _clamp(self, target: float) -> float:
        return min(self.config.coolant_max, max(self.config.coolant_min, target))

    def _predict_target(self, outdoor_c: float | None, room_c: float, room_target: float) -> tuple[float, str]:
        need = self._heat_need(outdoor_c, room_target)
        k = self._k_for(outdoor_c)
        if need > P_MAX:
            return self.config.coolant_max, "maximum"
        if need < P_MIN:
            mode = "intermittent"
            target = equilibrium_temp(P_MIN, room_c, self.model.c) + self.config.margin_c + k
        else:
            mode = "continuous"
            target = equilibrium_temp(need, room_c, self.model.c) + k
            target = max(target, equilibrium_temp(P_MIN, room_c, self.model.c) + self.config.margin_c)
        if not self.model.reliable and self.model.last_mode != "rise":
            # Move halfway from what the heater holds, not from a target that
            # was only computed. RISE's 75 °C is not a starting point.
            anchor = self._actual_coolant if self._actual_coolant is not None else self.model.last_target
            target = 0.5 * target + 0.5 * anchor
        return self._clamp(target), mode

    def _needs_soot_burn(self, now: float) -> bool:
        last = self.model.last_high_power_at
        if last is None:
            return len(self.model.ignitions) >= 3
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
        base = self.config.base_step_c * max(0.3, 1.0 / (1.0 + n / 3.0)) / (2 ** halves)
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
        center = int(key)
        self._apply_k(str(center - 1), delta, weight=0.5)
        self._apply_k(str(center + 1), delta, weight=0.5)
        self.model.k_counts[key] = self.model.k_counts.get(key, 0) + 1

    def _apply_k(self, key: str, delta: float, weight: float) -> None:
        self.model.k_bins[key] = self.model.k_bins.get(key, 0.0) + delta * weight

    def _bump_confidence(self, outdoor_c: float | None) -> None:
        key = self._k_key(outdoor_c)
        self.model.k_counts[key] = self.model.k_counts.get(key, 0) + 1


def config_from_settings(settings: Any) -> AdaptiveConfig:
    """Build AdaptiveConfig from the add-on Settings object."""
    return AdaptiveConfig(
        enabled=bool(getattr(settings, "adaptive_enabled", False)),
        coolant_min=float(getattr(settings, "adaptive_coolant_min", 40.0)),
        coolant_max=float(getattr(settings, "adaptive_coolant_max", 85.0)),
        margin_c=float(getattr(settings, "adaptive_margin_c", 3.0)),
        base_step_c=float(getattr(settings, "adaptive_base_step_c", 2.0)),
        step_up_ratio=float(getattr(settings, "adaptive_step_up_ratio", 0.6)),
        power_confirm_s=float(getattr(settings, "adaptive_power_confirm_s", 1800.0)),
        stop_lockout_s=float(getattr(settings, "adaptive_power_lockout_s", 5400.0)),
        max_stops=int(getattr(settings, "adaptive_max_stops_6h", 2)),
        min_burn_s=float(getattr(settings, "adaptive_min_burn_min", 20.0)) * 60.0,
        soot_interval_s=float(getattr(settings, "adaptive_soot_interval_h", 24.0)) * 3600.0,
        soot_target_c=float(getattr(settings, "adaptive_soot_target_c", 80.0)),
        safe_coolant_c=float(getattr(settings, "adaptive_safe_coolant_c", 65.0)),
        room_hysteresis_c=float(getattr(settings, "room_drop", 1.0)),
        rise_offset_c=float(getattr(settings, "adaptive_rise_offset_c", 1.0)),
        rise_target_c=float(getattr(settings, "adaptive_rise_target_c", 75.0)),
    )


def _bounded(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _num(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}"
