"""Panel replacement.

Thermostat mode follows the comfort panel. The climate card sets the
requested temperature, and the room sensor is reported as the cabin
temperature the heater hysterises against. Preheat follows coolant
temperature instead. Pump mode still uses the add-on's own limits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .faults import LOCKOUT_CODES, SELF_STOP_CODES
from .protocol import (
    ExtendedFrame,
    HeaterSettings,
    StatusFrame,
    cabin_temperature,
    heater_settings_view,
    patch_heater_settings,
    requested_temperature,
    settings_request,
    settings_write,
    start_preheat,
    start_pump,
    stop_command,
)
from .settings import Settings

LOG = logging.getLogger("autoterm_flow5.controller")
MODES = ("thermostat", "preheat", "pump")
ACTIVE = frozenset({"starting", "running"})
# The control unit drops a run that is not refreshed. A start on every poll
# silences telemetry, so the same request goes out about once a minute.
SETPOINT_REPEAT_S = 60.0
DEVICE_SETTING_KEYS = frozenset({
    "coolant_limit",
    "auxiliary_mode",
    "auxiliary_limit",
    "relay_mode",
    "fan_temperature",
    "pump_signal",
    "pump_with_engine",
    "pump_standby",
    "alarm_channel",
})


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
        # The thermostat request is repeated while heating stays on.
        self._thermostat_on = False
        self._setpoint_c: int | None = None
        self._setpoint_at: float | None = None
        self._device_raw: bytes | None = None
        self._settings_dirty: set[str] = set()
        self._settings_pending = False
        self._settings_confirm = False
        self._settings_wait_idle = False
        self._settings_attempts = 0
        self._settings_asked_at: float | None = None
        self._settings_write_at: float | None = None
        self._settings_seen_at: float | None = None

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
            self._setpoint_c = None
            self._setpoint_at = None
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

    def note_device_setting(self, key: str) -> None:
        """The user changed one stored setting. Write it after the block is known."""
        if key not in DEVICE_SETTING_KEYS:
            return
        self._settings_dirty.add(key)
        self._settings_pending = True
        self._settings_wait_idle = False
        self._settings_attempts = 0

    def accept_device_settings(self, parsed: HeaterSettings, seen_at: float, phase: str) -> bool:
        """Take a settings block the heater just sent.

        A block that arrives before the user edits anything replaces the
        add-on's copy. An edit made first is written on top of that block.
        """
        if seen_at == self._settings_seen_at:
            return False
        self._settings_seen_at = seen_at
        if self._settings_confirm:
            return self._confirm_settings(parsed, phase)
        if self._settings_pending and self._device_raw is None:
            self._device_raw = patch_heater_settings(parsed.raw, self._changes())
            return False
        if self._settings_pending:
            return False
        if self._device_raw == parsed.raw:
            return False
        self._device_raw = parsed.raw
        return self._copy_device_settings(parsed)

    def _confirm_settings(self, parsed: HeaterSettings, phase: str) -> bool:
        self._settings_confirm = False
        if parsed.raw == self._device_raw:
            self._settings_attempts = 0
            self._settings_wait_idle = False
            LOG.info("Heater stored the settings")
            return False
        self._settings_attempts += 1
        if self._settings_attempts < 2 or phase in ACTIVE:
            self._settings_pending = True
            self._settings_wait_idle = phase in ACTIVE
            LOG.info("Heater did not store the settings yet")
            return False
        LOG.warning("Heater kept its own settings")
        self._settings_pending = False
        self._settings_wait_idle = False
        self._settings_dirty.clear()
        self._settings_attempts = 0
        self._device_raw = parsed.raw
        return self._copy_device_settings(parsed)

    def _changes(self) -> dict[str, object]:
        values: dict[str, object] = {}
        for key in self._settings_dirty:
            if key == "coolant_limit":
                values[key] = self.settings.coolant_limit
            elif key == "auxiliary_mode":
                values[key] = self.settings.auxiliary_mode
            elif key == "auxiliary_limit":
                values[key] = self.settings.auxiliary_limit
            elif key == "relay_mode":
                values[key] = self.settings.relay_mode
            elif key == "fan_temperature":
                values[key] = self.settings.fan_temperature
            elif key == "pump_signal":
                values[key] = self.settings.pump_signal
            elif key == "pump_with_engine":
                values[key] = self.settings.pump_with_engine
            elif key == "pump_standby":
                values[key] = self.settings.pump_standby
            elif key == "alarm_channel":
                values[key] = self.settings.alarm_channel
        return values

    def _copy_device_settings(self, parsed: HeaterSettings) -> bool:
        view = heater_settings_view(parsed)
        changed = False
        for key, value in view.items():
            if getattr(self.settings, key) != value:
                setattr(self.settings, key, value)
                changed = True
        if changed:
            LOG.info(
                "Read heater settings: standby %s °C, auxiliary %s °C, fan %s °C",
                parsed.standby_c,
                parsed.auxiliary_c,
                parsed.relay_c,
            )
        return changed

    def _settings_commands(self, now: float, phase: str) -> list[bytes]:
        if self._device_raw is None:
            if self._settings_asked_at is None or now - self._settings_asked_at >= 10:
                self._settings_asked_at = now
                return [settings_request()]
            return []
        if self._settings_pending:
            if self._settings_wait_idle and phase in ACTIVE:
                return []
            self._device_raw = patch_heater_settings(self._device_raw, self._changes())
            self._settings_dirty.clear()
            self._settings_pending = False
            self._settings_confirm = True
            self._settings_write_at = now
            LOG.info("Sending stored settings to the heater")
            return [settings_write(self._device_raw)]
        if (
            self._settings_confirm
            and self._settings_write_at is not None
            and now - self._settings_write_at >= 2
            and (self._settings_asked_at is None or now - self._settings_asked_at >= 10)
        ):
            self._settings_asked_at = now
            return [settings_request()]
        return []

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
            self._setpoint_c = None
            self._setpoint_at = None
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

        commands[0:0] = self._settings_commands(now, phase)
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
        # Comfort-panel mode. The card's requested temperature is the heater
        # setpoint, and the cabin report is the room sensor it hysterises
        # against. Preheat is the other mode: that one follows coolant.
        # The request is sent when heating is switched on, when the card value
        # changes, about once a minute while the request stays on, and again
        # if the burner goes out with no fault. A fault is left alone.
        del fresh, coolant, voltage, room_age_s
        if fault:
            return []
        if phase == "cooldown":
            return []
        target = max(0, min(40, int(round(self.settings.room_target))))
        changed = self._setpoint_c != target
        due = self._setpoint_at is None or now - self._setpoint_at >= SETPOINT_REPEAT_S
        dropped = phase == "idle" and self._phase in ACTIVE | {"cooldown", "pump"}
        if self._thermostat_on and not changed and not due and not dropped:
            return []
        starting = phase == "idle" and not self._thermostat_on
        self._thermostat_on = True
        self._setpoint_c = target
        self._setpoint_at = now
        if starting or dropped:
            self._run_started_at = now
            self._start_at = now
            self._stop_reason = None
        return [requested_temperature(target)]

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
    """Low voltage stops preheat and pump mode even during the minimum run.

    The coolant limit is the heater's own standby temperature. It is stored
    in the control unit, so this loop does not send a separate stop for it.
    """
    del coolant
    if phase not in ACTIVE:
        controller._voltage_low_since = None
        return False
    settings = controller.settings
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
