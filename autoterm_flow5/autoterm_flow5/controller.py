"""Panel replacement.

In thermostat mode the add-on only switches the heater on and off and
reports the room temperature. The heater starts, stops, and protects
itself from that temperature. Preheat and pump modes still use the
add-on's own limits.
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
    return _phase_from(status.state, status.substate)


def _phase_from(state: int, sub: int) -> str:
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
        self._stop_sent_at: float | None = None
        self._start_at: float | None = None
        self._preheat_sent = False
        self._pump_sent = False
        self._voltage_low_since: float | None = None
        self._saw_fault_25 = False
        self._circulation_sent = False
        self._cabin_at: float | None = None
        self._cabin_c: int | None = None
        # One thermostat start per time the user switches heating on.
        self._thermostat_on = False

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
            self._stop_sent_at = None
            self._start_at = None
            self._preheat_sent = False
            self._pump_sent = False
            self._thermostat_on = False
            self.demand = True
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
        status_at: float | None = None,
        extended_at: float | None = None,
    ) -> ControlOutput:
        settings = self.settings
        phase = _observed_phase(status, extended, status_at, extended_at)
        commands: list[bytes] = []
        fresh = room_c is not None and room_age_s is not None and room_age_s <= settings.sensor_stale_s
        fault = _fault(status, extended)
        coolant = _coolant(status, extended, status_at, extended_at)
        voltage = extended.voltage if extended is not None else None

        if fault in LOCKOUT_CODES and self.mode != "thermostat":
            self.lockout = True
        if fault == 25 and self.mode != "thermostat":
            self._saw_fault_25 = True
        if phase == "idle" and self._phase in ACTIVE | {"cooldown", "pump"}:
            if self.mode == "thermostat":
                # The heater ended the burn from its own sensor or protection.
                self._run_started_at = None
            elif fault in SELF_STOP_CODES or self._saw_fault_25:
                self._stop_reason = "fault25"
                self._stopped_at = now
                self._saw_fault_25 = False
                self._run_started_at = None
            elif fault == 0 and not self.lockout and self._stop_reason is None and self.heat_enabled:
                self._note_unexpected_stop(now, coolant)

        if self.heat_enabled and self.mode == "thermostat":
            self.demand = True
        elif not self.heat_enabled or self.mode != "thermostat":
            self.demand = False

        thermostat = self.mode == "thermostat"
        if phase not in ACTIVE:
            # Cooldown and idle are the heater's own stop. Do not keep sending it.
            self._stop_requested = False
        retry_stop = (
            self._stop_requested
            and phase in ACTIVE
            and (self._stop_sent_at is None or now - self._stop_sent_at >= 5)
        )
        # One stop, then at most one retry every five seconds while the burner
        # is still running. A stop on every poll is what silenced telemetry
        # from 13:26:58 to 13:27:48.
        if self._stop_pending or retry_stop:
            commands.append(stop_command())
            self._stop_pending = False
            self._stop_sent_at = now
            self._run_started_at = None
            self._start_at = None
            self._thermostat_on = False
        elif self.heat_enabled and (thermostat or not self.lockout):
            if phase in ACTIVE and self._run_started_at is None:
                self._run_started_at = now
            commands.extend(
                self._mode_commands(now, phase, fresh, fault, coolant, voltage, room_age_s)
            )
        elif self.lockout and phase in ACTIVE and not thermostat:
            commands.append(stop_command())
            self._run_started_at = None
            self._start_at = None

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
        # The cabin-temperature report is what the heater regulates from.
        # One start enters that mode. The heater then starts, stops, and
        # applies its own limits. A later idle state is its decision.
        del fresh, fault, coolant, voltage, room_age_s
        if phase == "idle" and not self._thermostat_on:
            self._thermostat_on = True
            self._run_started_at = now
            self._start_at = now
            self._stop_reason = None
            return [start_thermostat()]
        return []

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


def _extended_is_newer(status_at: float | None, extended_at: float | None) -> bool:
    return extended_at is not None and (status_at is None or extended_at > status_at)


def _observed_phase(
    status: StatusFrame | None,
    extended: ExtendedFrame | None,
    status_at: float | None,
    extended_at: float | None,
) -> str:
    if extended is not None and _extended_is_newer(status_at, extended_at):
        return _phase_from(extended.state, extended.substate)
    return phase_of(status)


def _coolant(
    status: StatusFrame | None,
    extended: ExtendedFrame | None,
    status_at: float | None = None,
    extended_at: float | None = None,
) -> float | None:
    if extended is not None and _extended_is_newer(status_at, extended_at):
        return float(extended.liquid_c)
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
