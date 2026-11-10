"""Serial poll loop. Status is requested. The long frame is a stream the heater sends after diagnostic mode is enabled."""

from __future__ import annotations

import logging
from typing import BinaryIO

from .protocol import (
    CRC_LEN,
    HEADER_LEN,
    PREAMBLE,
    TYPE_CABIN_TEMP,
    TYPE_PREHEAT_DURATION,
    TYPE_PUMP,
    TYPE_SETTINGS,
    TYPE_START,
    TYPE_STOP,
    ExtendedFrame,
    Framer,
    HeaterSettings,
    StatusFrame,
    classify,
    diagnostic_enable,
    diagnostic_request,
    parameter_pending,
    status_request,
)

HANDSHAKE_INTERVAL_S = 60.0
# Until a parameter frame has arrived, repeat the enable command sooner.
HANDSHAKE_RETRY_S = 5.0
# Ask again once a valid parameter frame has been missing for a while.
ENABLE_AGAIN_S = 15.0
# Official tester timer. It does not start the stream. It is the only write
# while the stream is up, and only on a poll that heard nothing.
KEEPALIVE_S = 8.0
# Diagnostic mode again only after the keepalive has already had its turn.
STREAM_RECOVER_S = 30.0
# One parameter frame does not mean the stream is still on the wire.
# Fifteen seconds of silence is the link timeout, so the heater looks lost
# and a start command has nowhere to land.
STREAM_GAP_S = 2.0
# While the heater is already streaming, a status poll every second lands in
# the next parameter frame. Eight seconds still tracks mode changes.
STATUS_PERIOD_S = 8.0
LINK_TIMEOUT_S = 15.0
# Keep listening after a request. One 0.3 s read misses a slow reply, and the
# next request then collides with it.
REPLY_READS = 8
STATUS_GAP_S = 30.0
# pyserial raises this when select says the port is readable and read
# returns nothing. One such result is an empty poll, not a lost adapter.
_EMPTY_READ = "returned no data"
LOG = logging.getLogger("autoterm_flow5")


class SerialLink:
    def __init__(self, port: BinaryIO, extended_telemetry: bool = True) -> None:
        self.port = port
        self.extended_telemetry = extended_telemetry
        self.framer = Framer()
        self.status: StatusFrame | None = None
        self.extended: ExtendedFrame | None = None
        self.last_status_at: float | None = None
        self.last_extended_at: float | None = None
        self.last_frame_at: float | None = None
        self.last_handshake_at: float | None = None
        self.rx_bytes = 0
        self.rx_preview = b""
        self.tx_bytes = b""
        self.bad_crc = 0
        self.other_frames: list[str] = []
        self.heater_settings = None
        self.heater_settings_at: float | None = None
        self._empty_reads = 0
        self._extended_count = 0
        self._held: list[bytes] = []
        # Last time a parameter frame was on the wire, even with a bad checksum.
        self.last_parameter_at: float | None = None
        # Last time the port returned any bytes, including a frame we could not classify.
        self.last_rx_at: float | None = None
        self.last_payload_length: int | None = None
        # The poll after start or stop carries the room temperature before
        # another diagnostic enable. Until a parameter frame exists, that
        # enable stays ahead of a routine temperature report.
        self._after_control = False
        self.last_keepalive_at: float | None = None

    def close(self) -> None:
        """Release the port before another open. A second handle makes reads fail."""
        closer = getattr(self.port, "close", None)
        if closer is None:
            return
        try:
            closer()
        except Exception:
            return

    def poll(self, now: float, commands: list[bytes] | None = None) -> None:
        """Listen, then ask. The heater transmits the parameter block on its own.

        A status request written during that frame breaks the CRC. Finish
        whatever is already arriving, write the next byte at the start of the
        gap, and stay quiet when the bytes just heard were already corrupt.
        """
        self.rx_bytes = 0
        self.rx_preview = b""
        self.tx_bytes = b""
        self.other_frames = []
        self.framer.bad_frames.clear()
        self.framer.bad_notes.clear()
        self.framer.lead = b""
        queued = _order_commands(self._held + list(commands or []))
        self._held = []
        bad_before = self.framer.bad_crc
        extended_before = self._extended_count
        # The first listen waits out a frame period. One 0.3 s read misses a
        # burner that is already transmitting, and the enable command then
        # hits that frame.
        self._drain(now, patient=self._first_listen())
        noisy = self.framer.bad_crc > bad_before and self._extended_count == extended_before
        # A partial frame that gained no bytes this poll will not finish.
        # Holding the user's start or stop behind it is what makes the button late.
        # Cabin temperature stays queued: wiping the partial to send it is what
        # cut the next parameter frame in the 11:58 log.
        if (
            self.framer.pending
            and self.rx_bytes == 0
            and any(_control_command(item) for item in queued)
        ):
            self.framer.reset()
        if self.framer.pending:
            self._held = queued
            self.bad_crc = self.framer.bad_crc - bad_before
            return
        # Start and stop own the poll. Diagnostic mode or a status request
        # written behind them is what the heater was ignoring.
        sent_control = False
        rest = queued
        for index, command in enumerate(queued):
            if not _control_command(command):
                rest = queued[index:]
                break
            self._write(command)
            _log_control(command)
            sent_control = True
            self._drain(now)
            rest = queued[index + 1 :]
            break
        else:
            rest = []
        if sent_control and self.extended_telemetry:
            self._after_control = True
            self._held = rest
            self.bad_crc = self.framer.bad_crc - bad_before
            return
        # The heater regulates from the room temperature. It goes out once a
        # parameter frame has been seen, and on the poll after start or stop.
        # Before that, diagnostic mode has to be allowed to start the stream.
        if rest and self._temperature_due(now, noisy):
            command = rest[0]
            self._after_control = False
            self._write(command)
            _log_report(command)
            self._drain(now)
            self._held = rest[1:]
            self.bad_crc = self.framer.bad_crc - bad_before
            return
        if self._needs_handshake(now) and not noisy and not self._stream_busy(now):
            self._write(diagnostic_enable())
            self.last_handshake_at = now
            self._drain(now)
            self._held = rest
            self.bad_crc = self.framer.bad_crc - bad_before
            return
        if self._keepalive_due(now) and not noisy:
            self._write(diagnostic_request())
            self.last_keepalive_at = now
            LOG.info("Sent telemetry keepalive")
            self._drain(now)
            self._held = rest
            self.bad_crc = self.framer.bad_crc - bad_before
            return
        blocked = False
        for index, command in enumerate(rest):
            if self.framer.pending:
                self._held = rest[index:]
                blocked = True
                break
            # Cabin temperature waits for a checksum-valid parameter frame or a
            # quiet line. It is not written on top of a frame that already failed.
            if not _control_command(command) and not self._temperature_due(now, noisy):
                self._held = rest[index:]
                blocked = True
                break
            self._write(command)
            if _control_command(command):
                _log_control(command)
            else:
                self._after_control = False
                _log_report(command)
            self._drain(now)
            # One command in a gap. A second write, or a status poll, lands in
            # the next parameter frame. Checksums then fail until we stop talking.
            if self.framer.pending or noisy or self._stream_busy(now):
                self._held = rest[index + 1 :]
                blocked = True
                break
        else:
            self._held = []
        if not blocked and not noisy and not self.framer.pending and self._status_due(now):
            self._write(status_request())
            self._drain(now)
        self.bad_crc = self.framer.bad_crc - bad_before

    def _write(self, data: bytes) -> None:
        self.tx_bytes += data
        self.port.write(data)

    def _note_parameter(self, now: float) -> None:
        if (
            self.last_extended_at == now
            or any(_parameter_note(note) for note in self.framer.bad_frames)
            or parameter_pending(self.framer.peek(HEADER_LEN))
        ):
            self.last_parameter_at = now

    def _first_listen(self) -> bool:
        return (
            self.extended_telemetry
            and self.last_handshake_at is None
            and self.last_parameter_at is None
            and self.last_extended_at is None
        )

    def _stream_busy(self, now: float) -> bool:
        """Parameter frames are arriving. Another request would be written into them.

        Three seconds is shorter than the retry, so a restart kept asking
        diagnostic mode every five seconds and the checksum never recovered.
        """
        return self.last_parameter_at is not None and now - self.last_parameter_at < STREAM_GAP_S

    def _temperature_due(self, now: float, noisy: bool) -> bool:
        if not self._report_gap(now, noisy):
            return False
        return self.last_extended_at is not None or self._after_control

    def _report_gap(self, now: float, noisy: bool) -> bool:
        """True when the room temperature can be written without cutting a frame.

        A finished status reply is a gap. A parameter frame is a gap only at
        the moment it completes. Bytes still arriving are not.
        """
        if noisy or self.last_handshake_at == now or self.framer.pending:
            return False
        if self.last_extended_at == now:
            return True
        return not self._stream_busy(now)

    def _heard_recently(self, now: float) -> bool:
        return self.last_rx_at is not None and now - self.last_rx_at <= LINK_TIMEOUT_S

    def _needs_handshake(self, now: float) -> bool:
        if not self.extended_telemetry:
            return False
        # A status reply is not the parameter stream. Keep asking until a
        # checksum-valid frame arrives. A single bad header must not pause this.
        # Bytes already on the wire are not a place to write the enable again.
        if self._stream_busy(now) or self.rx_bytes or self.framer.pending:
            return False
        if self.last_extended_at is None:
            if self.last_handshake_at is not None and now - self.last_handshake_at < HANDSHAKE_RETRY_S:
                return False
            return True
        # One frame already started the stream. Asking again fifteen seconds
        # later is what cut the next frame in the 13:20 log. The keepalive
        # owns that gap. Enable is only the recovery after it has failed.
        if now - self.last_extended_at < STREAM_RECOVER_S:
            return False
        if self.last_keepalive_at is None or self.last_keepalive_at < self.last_extended_at:
            return False
        if self.last_handshake_at is not None and now - self.last_handshake_at < STREAM_RECOVER_S:
            return False
        return True

    def _keepalive_due(self, now: float) -> bool:
        """Official 8 s timer, and only when this poll heard nothing."""
        if not self.extended_telemetry or self.last_extended_at is None:
            return False
        if self.rx_bytes or self.framer.pending or self._stream_busy(now):
            return False
        if self.last_handshake_at == now:
            return False
        anchor = self.last_keepalive_at if self.last_keepalive_at is not None else self.last_extended_at
        return now - anchor >= KEEPALIVE_S

    def _telemetry_recent(self, now: float) -> bool:
        """A parameter frame arrived recently. Status would be written into the next one."""
        for stamp in (self.last_extended_at, self.last_parameter_at):
            if stamp is not None and now - stamp < ENABLE_AGAIN_S:
                return True
        return False

    def _status_due(self, now: float) -> bool:
        # A status poll in the gap still overlaps the next parameter frame.
        # Mode, voltage, pump, and flame already come from that frame.
        # Once a checksum-valid frame has arrived, status is not asked again.
        # The 13:20 log got one frame, then a status poll, then the link died.
        if self._stream_busy(now) or self.last_handshake_at == now or self._telemetry_recent(now):
            return False
        if self.extended_telemetry and self.last_extended_at is not None:
            return False
        return True

    def _drain(self, now: float, patient: bool = False) -> None:
        """Read until the line is idle and no half-received frame remains.

        After a whole parameter frame, return at once. Waiting out the read
        timeout spends the gap, and the next request then hits the following frame.
        The first listen uses three quiet reads, about a frame period, so a
        restart can see the stream before it transmits.
        """
        quiet = 0
        idle_reads = 3 if patient else 1
        for _ in range(REPLY_READS):
            data = _read_port(self.port)
            if data is None:
                self._empty_reads += 1
                # Reopening throws away the kernel buffer and pulses the lines.
                # A glitch on the next poll is the same adapter, not a new one,
                # while bytes have arrived within the link timeout.
                if (
                    self._empty_reads >= 3
                    and not self._status_is_recent(now)
                    and not self._heard_recently(now)
                ):
                    raise ConnectionError("serial port returned no data")
                if self._status_is_recent(now):
                    return
                continue
            if not data:
                quiet += 1
                # One quiet read is a gap between frames. A partial frame gets
                # a little longer, in case the rest is still coming.
                if quiet >= 3 or (self.framer.pending == 0 and quiet >= idle_reads):
                    return
                continue
            quiet = 0
            self._empty_reads = 0
            self.last_rx_at = now
            self.rx_bytes += len(data)
            if len(self.rx_preview) < 80:
                self.rx_preview += data[: 80 - len(self.rx_preview)]
            self._consume(now, data)
            self._note_parameter(now)
            if (
                self.framer.pending == 0
                and not _port_waiting(self.port)
                and (self.last_extended_at == now or self.last_parameter_at == now)
            ):
                return

    def _status_is_recent(self, now: float) -> bool:
        return self.last_status_at is not None and now - self.last_status_at <= STATUS_GAP_S

    def _consume(self, now: float, data: bytes) -> bool:
        found_status = False
        for frame in self.framer.feed(data):
            parsed = classify(frame)
            if parsed is None:
                if frame.crc_ok:
                    self.last_frame_at = now
                    self.other_frames.append(f"{frame.dev:02x}/{frame.msg_type:02x}/{frame.length}")
                    if frame.msg_type == TYPE_SETTINGS:
                        LOG.info("Ignored a settings frame: %s", frame.hex)
                continue
            self.last_frame_at = now
            if isinstance(parsed, HeaterSettings):
                self.heater_settings = parsed
                self.heater_settings_at = now
            elif isinstance(parsed, StatusFrame):
                self.status = parsed
                self.last_status_at = now
                found_status = True
            elif isinstance(parsed, ExtendedFrame):
                self.extended = parsed
                self.last_extended_at = now
                self.last_payload_length = frame.length
                self._extended_count += 1
        return found_status

    def online(self, now: float) -> bool:
        heard = self.last_status_at
        for stamp in (self.last_extended_at, self.last_parameter_at):
            if stamp is not None and (heard is None or stamp > heard):
                heard = stamp
        return heard is not None and now - heard <= LINK_TIMEOUT_S

    def telemetry_stale(self, now: float) -> bool:
        if not self.extended_telemetry:
            return False
        if self.last_extended_at is None:
            return True
        return now - self.last_extended_at >= HANDSHAKE_INTERVAL_S


def open_port(path: str, baud: int):
    """Open 2400 8N1 with DTR and RTS held on.

    Autoterm Test disables both lines. On this USB-UART that cuts the link:
    the heater stops answering. The same port answers while the lines stay on.
    Hardware flow control stays off, so the lines are levels, not handshake.
    """
    import serial

    port = serial.Serial(
        path,
        baudrate=baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.3,
        write_timeout=1.0,
        exclusive=True,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )
    port.dtr = True
    port.rts = True
    return port


def _parameter_note(note: str) -> bool:
    """True for a rejected 56, 58, or 72 byte parameter header, not a slipped byte."""
    kind, _, length = note.partition("/")
    if kind != "02":
        return False
    msg, _, size = length.partition("/")
    if msg != "01":
        return False
    try:
        return int(size) in (56, 58, 72)
    except ValueError:
        return False


def _log_report(command: bytes) -> None:
    if len(command) >= 6 and command[4] == TYPE_CABIN_TEMP:
        LOG.info("Sent room temperature %s °C", command[5])
    elif len(command) >= HEADER_LEN and command[4] == TYPE_SETTINGS:
        if len(command) == HEADER_LEN + CRC_LEN:
            LOG.info("Asked the heater for its stored settings")
        else:
            LOG.info("Sent stored settings to the heater")


def _log_control(command: bytes) -> None:
    if command[4] == TYPE_START and len(command) >= 7:
        value = int.from_bytes(command[5:7], "big")
        if value <= 40:
            LOG.info("Sent requested temperature %s °C", value)
            return
    kind = {
        TYPE_START: "start",
        TYPE_STOP: "stop",
        TYPE_PUMP: "pump",
        TYPE_PREHEAT_DURATION: "preheat",
    }.get(command[4], "command")
    LOG.info("Sent heater %s", kind)


def _control_command(command: bytes) -> bool:
    """Start, stop, and pump. The cabin-temperature report can wait."""
    if len(command) < HEADER_LEN or command[0] != PREAMBLE:
        return False
    return command[4] in (TYPE_START, TYPE_STOP, TYPE_PUMP, TYPE_PREHEAT_DURATION)


def _order_commands(commands: list[bytes]) -> list[bytes]:
    """Start and stop go out before a cabin report that was waiting."""
    control = [command for command in commands if _control_command(command)]
    rest = [command for command in commands if not _control_command(command)]
    return control + rest


class LinkProgress:
    """Decide which link changes are worth one log line."""

    def __init__(self) -> None:
        self.announced_online = False
        self.warned_silent = False
        self.opened_at: float | None = None
        self.last_mode = ""

    def opened(self, now: float) -> None:
        self.opened_at = now
        self.warned_silent = False

    def update(self, now: float, online: bool) -> str | None:
        """Return answered, lost, silent, or None."""
        if online:
            self.warned_silent = False
            if self.announced_online:
                return None
            self.announced_online = True
            return "answered"
        if self.announced_online:
            self.announced_online = False
            self.opened_at = now
            self.warned_silent = False
            return "lost"
        if self.opened_at is not None and not self.warned_silent and now - self.opened_at >= 10:
            self.warned_silent = True
            return "silent"
        return None


class TelemetryLog:
    """While debug is on, say whether the 58-byte frame is arriving."""

    def __init__(self) -> None:
        self._ok = False
        self._missing = False
        self._last_ok_at: float | None = None
        self._mode: str | None = None
        self._last_detail_at: float | None = None

    def update(self, now: float, enabled: bool, link: SerialLink) -> list[tuple[str, str]]:
        if not enabled:
            self._ok = False
            self._missing = False
            self._last_ok_at = None
            self._mode = None
            self._last_detail_at = None
            return []
        lines: list[tuple[str, str]] = []
        mode = _observed_mode(link)
        mode_changed = mode is not None and mode != self._mode
        if mode_changed:
            self._mode = mode
            lines.append(("info", f"heater status: {mode}"))
        fresh = (
            link.extended is not None
            and link.last_extended_at is not None
            and now - link.last_extended_at < HANDSHAKE_INTERVAL_S
        )
        if fresh:
            self._missing = False
            due = self._last_ok_at is None or now - self._last_ok_at >= HANDSHAKE_INTERVAL_S
            if self._ok and not due:
                return lines
            self._ok = True
            self._last_ok_at = now
            frame = link.extended
            assert frame is not None
            lines.append(
                (
                    "info",
                    f"telemetry ok ({link.last_payload_length or 58} B): {frame.mode_name}, "
                    f"{frame.voltage:.2f} V, "
                    f"pump {frame.pump_hz:.2f} Hz, flame {frame.flame_c:.0f} °C",
                )
            )
            return lines
        self._ok = False
        self._last_ok_at = None
        detail_due = bool(link.bad_crc) and (
            self._last_detail_at is None or now - self._last_detail_at >= 15
        )
        if self._missing and not mode_changed and link.last_handshake_at != now and not detail_due:
            return lines
        if detail_due:
            self._last_detail_at = now
        self._missing = True
        if link.last_extended_at is None:
            if link.last_handshake_at == now:
                text = "diagnostic reports requested, no 58-byte frame yet"
            else:
                text = "58-byte telemetry has not arrived"
        else:
            age = now - link.last_extended_at
            text = f"58-byte telemetry stopped, last frame {age:.0f} s ago"
        lines.append(("warning", f"{text}, {_heard(link)}"))
        return lines


def _observed_mode(link: SerialLink) -> str | None:
    """Mode from whichever frame arrived last."""
    if (
        link.extended is not None
        and link.last_extended_at is not None
        and (link.last_status_at is None or link.last_extended_at > link.last_status_at)
    ):
        return link.extended.mode_name
    if link.status is not None:
        return link.status.mode_name
    return None


def _heard(link: SerialLink) -> str:
    """What arrived besides a parsed 58-byte frame."""
    parts = [f"heard {link.rx_bytes} bytes"]
    if link.bad_crc:
        parts.append(f"bad crc {link.bad_crc}")
    if link.framer.bad_frames:
        parts.append("bad " + " ".join(link.framer.bad_frames[:4]))
    if link.other_frames:
        parts.append("other " + " ".join(link.other_frames[:4]))
    if link.bad_crc or link.framer.pending or link.tx_bytes:
        if link.tx_bytes:
            parts.append("wrote " + link.tx_bytes.hex())
        else:
            parts.append("wrote nothing")
    if link.framer.lead:
        parts.append("lead " + link.framer.lead.hex())
    if link.framer.bad_notes:
        parts.append(link.framer.bad_notes[0])
    if link.framer.pending:
        parts.append(f"pending {link.framer.pending} {link.framer.peek().hex()}")
    if link.rx_preview and (link.bad_crc or link.framer.pending):
        parts.append("rx " + link.rx_preview.hex())
    return ", ".join(parts)


def _port_waiting(port: BinaryIO) -> bool:
    waiting = getattr(port, "in_waiting", 0)
    try:
        return int(waiting or 0) > 0
    except (TypeError, ValueError):
        return False


def _read_port(port: BinaryIO) -> bytes | None:
    """Return bytes, empty bytes on timeout, or None for one spurious empty read."""
    try:
        return port.read(1024) or b""
    except Exception as exc:
        if _EMPTY_READ in str(exc).lower():
            return None
        raise
