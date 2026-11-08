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
        self.bad_crc = 0
        self.other_frames: list[str] = []
        self._empty_reads = 0

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
        """Ask for status. Diagnostic mode is enabled with its own write first.

        Gluing it to the status request makes the heater answer neither: it
        starts replying while the extra bytes are still being sent.
        """
        self.rx_bytes = 0
        self.other_frames = []
        bad_before = self.framer.bad_crc
        if self._needs_handshake(now):
            self.port.write(diagnostic_enable())
            self.last_handshake_at = now
            self._ingest(now)
        payload = bytearray(status_request())
        for command in commands or []:
            payload.extend(command)
        self.port.write(payload)
        self._ingest(now)
        self.bad_crc = self.framer.bad_crc - bad_before

    def _needs_handshake(self, now: float) -> bool:
        if not self.extended_telemetry:
            return False
        interval = HANDSHAKE_INTERVAL_S if self.last_extended_at is not None else HANDSHAKE_RETRY_S
        if self.last_handshake_at is not None and now - self.last_handshake_at < interval:
            return False
        if self.last_extended_at is None:
            return True
        return now - self.last_extended_at >= HANDSHAKE_INTERVAL_S

    def _ingest(self, now: float) -> None:
        """Read until the reply is in, or the line stays quiet.

        A status frame often arrives before the 58-byte frame. Stopping at the
        status frame drops the slower telemetry and the next request collides
        with it.
        """
        quiet = 0
        for _ in range(REPLY_READS):
            data = _read_port(self.port)
            if data is None:
                self._empty_reads += 1
                if self._empty_reads >= 3 and not self._status_is_recent(now):
                    raise ConnectionError("serial port returned no data")
                continue
            elif not data:
                quiet += 1
            else:
                quiet = 0
                self._empty_reads = 0
                self.rx_bytes += len(data)
                self._consume(now, data)
                if self.last_status_at == now and not self._waiting_for_extended(now):
                    return
            if quiet >= 3:
                return

    def _waiting_for_extended(self, now: float) -> bool:
        if not self.extended_telemetry:
            return False
        if self.last_extended_at == now:
            return False
        if self.last_extended_at is None:
            return True
        return self.last_handshake_at == now or now - self.last_extended_at >= HANDSHAKE_INTERVAL_S

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
        return found_status

    def online(self, now: float) -> bool:
        return self.last_status_at is not None and now - self.last_status_at <= LINK_TIMEOUT_S

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

    def update(self, now: float, enabled: bool, link: SerialLink) -> list[tuple[str, str]]:
        if not enabled:
            self._ok = False
            self._missing = False
            self._last_ok_at = None
            self._mode = None
            return []
        lines: list[tuple[str, str]] = []
        mode = link.status.mode_name if link.status is not None else None
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
                    f"58-byte telemetry ok: {frame.mode_name}, {frame.voltage:.2f} V, "
                    f"pump {frame.pump_hz:.2f} Hz, flame {frame.flame_c:.0f} °C",
                )
            )
            return lines
        self._ok = False
        self._last_ok_at = None
        if self._missing and not mode_changed and link.last_handshake_at != now:
            return lines
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


def _heard(link: SerialLink) -> str:
    """What arrived besides a parsed 58-byte frame."""
    parts = [f"heard {link.rx_bytes} bytes"]
    if link.bad_crc:
        parts.append(f"bad crc {link.bad_crc}")
    if link.other_frames:
        parts.append("other " + " ".join(link.other_frames[:4]))
    return ", ".join(parts)


def _read_port(port: BinaryIO) -> bytes | None:
    """Return bytes, empty bytes on timeout, or None for one spurious empty read."""
    try:
        return port.read(1024) or b""
    except Exception as exc:
        if _EMPTY_READ in str(exc).lower():
            return None
        raise
