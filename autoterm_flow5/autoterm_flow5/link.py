"""Serial poll loop. Status is requested. The long frame is a stream the heater sends after diagnostic mode is enabled."""

from __future__ import annotations

from typing import BinaryIO

from .protocol import (
    ExtendedFrame,
    Framer,
    StatusFrame,
    classify,
    diagnostic_enable,
    status_request,
)

HANDSHAKE_INTERVAL_S = 60.0
# Until a parameter frame has arrived, repeat the enable command sooner.
HANDSHAKE_RETRY_S = 5.0
# Ask again once the stream has actually stopped, without waiting a full minute.
ENABLE_AGAIN_S = 15.0
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
        self._empty_reads = 0
        self._extended_count = 0
        self._held: list[bytes] = []
        # Last time a parameter frame was on the wire, even with a bad checksum.
        self.last_parameter_at: float | None = None
        self.last_payload_length: int | None = None

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
        queued = self._held + list(commands or [])
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
        if self.framer.pending and self.rx_bytes == 0 and queued:
            self.framer.reset()
        if self.framer.pending:
            self._held = queued
            self.bad_crc = self.framer.bad_crc - bad_before
            return
        if self._needs_handshake(now) and not noisy and not self._stream_busy(now):
            self._write(diagnostic_enable())
            self.last_handshake_at = now
            self._drain(now)
            if self.framer.pending:
                self._held = queued
                self.bad_crc = self.framer.bad_crc - bad_before
                return
        blocked = False
        for index, command in enumerate(queued):
            if self.framer.pending:
                self._held = queued[index:]
                blocked = True
                break
            self._write(command)
            self._drain(now)
            # One command in a gap. A second write, or a status poll, lands in
            # the next parameter frame. Checksums then fail until we stop talking.
            if self.framer.pending or noisy or self._stream_busy(now):
                self._held = queued[index + 1 :]
                blocked = True
                break
        if not blocked and not noisy and not self.framer.pending and self._status_due(now):
            self._write(status_request())
            self._drain(now)
        self.bad_crc = self.framer.bad_crc - bad_before

    def _write(self, data: bytes) -> None:
        self.tx_bytes += data
        self.port.write(data)

    def _note_parameter(self, now: float) -> None:
        if self.last_extended_at == now or any(note.startswith("02/01/") for note in self.framer.bad_frames):
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
        return self.last_parameter_at is not None and now - self.last_parameter_at < ENABLE_AGAIN_S

    def _needs_handshake(self, now: float) -> bool:
        if not self.extended_telemetry:
            return False
        # Bad checksums still mean diagnostic mode is on. Asking again collides.
        if self._stream_busy(now):
            return False
        interval = ENABLE_AGAIN_S if self.last_extended_at is not None else HANDSHAKE_RETRY_S
        if self.last_handshake_at is not None and now - self.last_handshake_at < interval:
            return False
        if self.last_extended_at is None:
            return True
        return now - self.last_extended_at >= ENABLE_AGAIN_S

    def _status_due(self, now: float) -> bool:
        if self._stream_busy(now) and self.last_extended_at != now:
            # A gap with nothing heard can take one status poll. Bytes on the
            # wire mean the parameter frame is still in progress.
            if self.rx_bytes > 0:
                return False
            if self.last_status_at is None:
                return False
            return now - self.last_status_at >= STATUS_PERIOD_S
        if not self.extended_telemetry or self.last_extended_at is None:
            return True
        if now - self.last_extended_at >= ENABLE_AGAIN_S:
            return True
        if self.last_status_at is None:
            return True
        return now - self.last_status_at >= STATUS_PERIOD_S

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
                if self._empty_reads >= 3 and not self._status_is_recent(now):
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
                continue
            self.last_frame_at = now
            if isinstance(parsed, StatusFrame):
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
    import serial

    return serial.Serial(
        path,
        baudrate=baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.3,
        write_timeout=1.0,
        exclusive=True,
    )


class LinkProgress:
    """Decide which link changes are worth one log line."""

    def __init__(self) -> None:
        self.announced_online = False
        self.warned_silent = False
        self.opened_at: float | None = None

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
