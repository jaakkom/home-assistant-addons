"""Panel replacement: room-temperature demand, coolant limit, minimum times.

The heater has no confirmed power-level command. This loop only sends
start, stop, pump, and the cabin-temperature report.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .faults import LOCKOUT_CODES, SELF_STOP_CODES
from .protocol import (
    ExtendedFrame,
    StatusFrame,
    cabin_temperature,
    start_preheat,
    start_pump,
    start_thermostat,
    stop_command,
)
from .settings import Settings

LOG = logging.getLogger("autoterm_flow5.controller")
MODES = ("thermostat", "preheat", "pump")
ACTIVE = frozenset({"starting", "running"})


def aggregate(values: list[float | None], mode: str) -> float | None:
    numbers = [value for value in values if value is not None]
    if not numbers:
        return None
    if mode == "max":
        return max(numbers)
    if mode == "avg":
        return sum(numbers) / len(numbers)
    return min(numbers)


def phase_of(status: StatusFrame | None) -> str:
    if status is None:
        return "unknown"
    state, sub = status.state, status.substate
    if state == 0:
        return "idle"
    if state == 4 or (state == 1 and sub == 0):
        return "cooldown"
    if state == 3 and sub == 7:
        return "pump"
    if state == 2:
        return "starting"
    if state == 3:
        return "running"
    if state == 1 and sub == 1:
        return "locked"
    return "other"


def update_demand(current: bool, temperature: float, target: float, drop: float, rise: float) -> bool:
    if temperature <= target - drop:
        return True
    if temperature >= target + rise:
        return False
    return current


@dataclass
class ControlOutput:
    commands: list[bytes] = field(default_factory=list)
    demand: bool = False
    heat_enabled: bool = False
    mode: str = "thermostat"
    fan_on: bool = False
    room_c: float | None = None
    phase: str = "unknown"
    lockout: bool = False


class Controller:
    """Decide the next UART commands. Heating stays off until the user asks.

    A new process does not stop a burner that is already running and does
    not send a start of its own.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings.sanitized()
        self.heat_enabled = False
        self.mode = "thermostat"
        self.demand = False
        self.lockout = False
        self._phase = "unknown"
        self._run_started_at: float | None = None
        self._stop_reason: str | None = None
        self._stopped_at: float | None = None
        self._stop_pending = False
        # True only after the user turns heating off. A fresh process must not
        # stop a burner that is already running.
        self._stop_requested = False
        self._start_at: float | None = None
        self._preheat_sent = False
        self._pump_sent = False
        self._voltage_low_since: float | None = None
        self._saw_fault_25 = False
        self._circulation_sent = False
        self._cabin_at: float | None = None
        self._cabin_c: int | None = None

    def set_mode(self, mode: str) -> None:
        if mode in MODES and mode != self.mode:
            self.mode = mode
            self._preheat_sent = False
            self._pump_sent = False

    def set_heat(self, enabled: bool, now: float) -> None:
        if enabled and not self.heat_enabled:
            self.lockout = False
            self.heat_enabled = True
            self._stop_reason = None
            self._stop_pending = False
            self._stop_requested = False
            self._start_at = None
            self._preheat_sent = False
            self._pump_sent = False
        elif not enabled and self.heat_enabled:
            self.heat_enabled = False
            self.demand = False
            self._stop_pending = True
            self._stop_requested = True
            self._stop_reason = "user"
            self._stopped_at = now

    def request_stop(self, now: float) -> None:
        self.set_heat(False, now)

    def tick(
        self,
        now: float,
        status: StatusFrame | None,
        extended: ExtendedFrame | None,
        room_c: float | None,
        room_age_s: float | None,
    ) -> ControlOutput:
        settings = self.settings
        phase = phase_of(status)
        commands: list[bytes] = []
        fresh = room_c is not None and room_age_s is not None and room_age_s <= settings.sensor_stale_s
        fault = _fault(status, extended)
        coolant = _coolant(status, extended)
        voltage = extended.voltage if extended is not None else None

        if fault in LOCKOUT_CODES:
            self.lockout = True
        if fault == 25:
            self._saw_fault_25 = True
        if phase == "idle" and self._phase in ACTIVE | {"cooldown", "pump"}:
            if fault in SELF_STOP_CODES or self._saw_fault_25:
                self._stop_reason = "fault25"
                self._stopped_at = now
                self._saw_fault_25 = False
                self._run_started_at = None
            elif fault == 0 and not self.lockout and self._stop_reason is None and self.heat_enabled:
                self._note_unexpected_stop(now, coolant)

        if fresh and self.heat_enabled and self.mode == "thermostat" and not self.lockout:
            self.demand = update_demand(
                self.demand,
                room_c,  # type: ignore[arg-type]
                settings.room_target,
                settings.room_drop,
                settings.room_rise,
            )
        elif not self.heat_enabled or self.mode != "thermostat":
            self.demand = False

        if fresh and room_c is not None:
            reported = int(round(room_c))
            due = (
                self._cabin_at is None
                or reported != self._cabin_c
                or now - self._cabin_at >= 30
            )
            if due:
                commands.append(cabin_temperature(reported))
                self._cabin_at = now
                self._cabin_c = reported

        if self._stop_pending or (self._stop_requested and phase in ACTIVE):
            commands.append(stop_command())
            self._stop_pending = False
            self._run_started_at = None
            self._start_at = None
        elif self.heat_enabled and not self.lockout:
            if phase in ACTIVE and self._run_started_at is None:
                self._run_started_at = now
            commands.extend(
                self._mode_commands(now, phase, fresh, fault, coolant, voltage, room_age_s)
            )
        elif self.lockout and phase in ACTIVE:
            commands.append(stop_command())
            self._run_started_at = None
            self._start_at = None

        self._phase = phase
        fan_on = coolant is not None and coolant >= settings.fan_temperature
        return ControlOutput(
            commands=commands,
            demand=self.demand,
            heat_enabled=self.heat_enabled,
            mode=self.mode,
            fan_on=fan_on,
            room_c=room_c,
            phase=phase,
            lockout=self.lockout,
        )

    def _mode_commands(
        self,
        now: float,
        phase: str,
        fresh: bool,
        fault: int,
        coolant: float | None,
        voltage: float | None,
        room_age_s: float | None,
    ) -> list[bytes]:
        if self.mode == "preheat":
            return self._preheat(now, phase, coolant, voltage)
        if self.mode == "pump":
            return self._pump(phase)
        return self._thermostat(now, phase, fresh, fault, coolant, voltage, room_age_s)

    def _preheat(
        self,
        now: float,
        phase: str,
        coolant: float | None,
        voltage: float | None,
    ) -> list[bytes]:
        if _safety_stop(self, now, phase, coolant, voltage):
            self._preheat_sent = True
            return [stop_command()]
        if phase == "idle" and not self._preheat_sent:
            self._preheat_sent = True
            self._run_started_at = now
            self._stop_reason = None
            return start_preheat(self.settings.preheat_minutes)
        return []

    def _pump(self, phase: str) -> list[bytes]:
        if phase == "idle" and not self._pump_sent:
            self._pump_sent = True
            return [start_pump()]
        return []

    def _thermostat(
        self,
        now: float,
        phase: str,
        fresh: bool,
        fault: int,
        coolant: float | None,
        voltage: float | None,
        room_age_s: float | None,
    ) -> list[bytes]:
        settings = self.settings
        if not fresh and room_age_s is not None and room_age_s > settings.sensor_stale_s and phase in ACTIVE:
            self._mark_stop(now, "stale")
            return [stop_command()]
        if _safety_stop(self, now, phase, coolant, voltage):
            return [stop_command()]
        if (
            phase in ACTIVE
            and not self.demand
            and self._run_started_at is not None
            and now - self._run_started_at >= settings.min_on_s
        ):
            self._mark_stop(now, "room")
            return [stop_command()]
        if phase == "idle" and self._may_start(now, fresh, fault, coolant):
            self._run_started_at = now
            self._stop_reason = None
            self._start_at = now
            return [start_thermostat()]
        waiting_for_coolant = (
            settings.circulation_pump
            and self.demand
            and phase == "idle"
            and not self._may_start(now, fresh, fault, coolant)
            and self._stop_reason in {"coolant", "fault25"}
        )
        if waiting_for_coolant and not self._circulation_sent:
            self._circulation_sent = True
            return [start_pump()]
        if not waiting_for_coolant:
            self._circulation_sent = False
        return []

    def _may_start(self, now: float, fresh: bool, fault: int, coolant: float | None) -> bool:
        if not fresh or not self.demand or self.lockout:
            return False
        if fault not in (0, 25):
            return False
        if self._start_at is not None and now - self._start_at < 10 and self._stop_reason is None:
            return False
        reason = self._stop_reason
        if reason == "room" and self._stopped_at is not None:
            if now - self._stopped_at < self.settings.min_off_s:
                return False
        if reason == "voltage" and self._stopped_at is not None:
            if now - self._stopped_at < self.settings.min_off_s:
                return False
        if reason == "stale" and self._stopped_at is not None:
            if now - self._stopped_at < self.settings.min_off_s:
                return False
        if reason in {"coolant", "fault25"}:
            if coolant is None:
                return False
            if coolant > self.settings.coolant_limit - self.settings.coolant_drop:
                return False
        return True

    def _note_unexpected_stop(self, now: float, coolant: float | None) -> None:
        """The burner left an active phase with no fault and no stop from us.

        Electromagnetic interference can do this without setting a fault code.
        Start again while heat is still wanted. A coolant temperature that is
        already at the limit waits for the normal drop instead of relighting.
        """
        self._run_started_at = None
        self._start_at = None
        self._stopped_at = now
        if coolant is not None and coolant >= self.settings.coolant_limit:
            self._stop_reason = "coolant"
            LOG.info("Heater stopped at the coolant limit with no fault; waiting for the drop")
            return
        self._stop_reason = "unexpected"
        self._preheat_sent = False
        self._pump_sent = False
        LOG.info("Heater stopped with no fault and no stop command; starting again")

    def _mark_stop(self, now: float, reason: str) -> None:
        self._stop_reason = reason
        self._stopped_at = now
        self._run_started_at = None
        self._start_at = None


def _fault(status: StatusFrame | None, extended: ExtendedFrame | None) -> int:
    if status is not None and status.fault:
        return status.fault
    if extended is not None and extended.fault:
        return extended.fault
    if status is not None:
        return status.fault
    if extended is not None:
        return extended.fault
    return 0


def _coolant(status: StatusFrame | None, extended: ExtendedFrame | None) -> float | None:
    if status is not None:
        return status.coolant_c
    if extended is not None:
        return float(extended.liquid_c)
    return None


def _safety_stop(
    controller: Controller,
    now: float,
    phase: str,
    coolant: float | None,
    voltage: float | None,
) -> bool:
    """Coolant limit and low voltage stop even during the minimum run."""
    if phase not in ACTIVE:
        controller._voltage_low_since = None
        return False
    settings = controller.settings
    if coolant is not None and coolant >= settings.coolant_limit:
        controller._mark_stop(now, "coolant")
        return True
    if voltage is not None and voltage < settings.shutdown_voltage:
        if controller._voltage_low_since is None:
            controller._voltage_low_since = now
        elif now - controller._voltage_low_since >= settings.shutdown_voltage_s:
            controller._mark_stop(now, "voltage")
            controller._voltage_low_since = None
            return True
    else:
        controller._voltage_low_since = None
    return False
