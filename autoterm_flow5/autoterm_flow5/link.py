"""Serial poll loop. The heater answers; it does not speak first."""

from __future__ import annotations

from typing import BinaryIO

from .protocol import (
    PUBR0,
    ExtendedFrame,
    Framer,
    StatusFrame,
    classify,
    status_request,
)

HANDSHAKE_INTERVAL_S = 60.0
LINK_TIMEOUT_S = 5.0


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

    def poll(self, now: float, commands: list[bytes] | None = None) -> None:
        payload = bytearray(status_request())
        if self._needs_handshake(now):
            payload.extend(PUBR0)
            self.last_handshake_at = now
        for command in commands or []:
            payload.extend(command)
        self.port.write(payload)
        self._ingest(now)

    def _needs_handshake(self, now: float) -> bool:
        if not self.extended_telemetry:
            return False
        if self.last_handshake_at is not None and now - self.last_handshake_at < HANDSHAKE_INTERVAL_S:
            return False
        if self.last_extended_at is None:
            return True
        return now - self.last_extended_at >= HANDSHAKE_INTERVAL_S

    def _ingest(self, now: float) -> None:
        data = self.port.read(1024) or b""
        if not data:
            return
        for frame in self.framer.feed(data):
            parsed = classify(frame)
            if parsed is None:
                if frame.crc_ok:
                    self.last_frame_at = now
                continue
            self.last_frame_at = now
            if isinstance(parsed, StatusFrame):
                self.status = parsed
                self.last_status_at = now
            elif isinstance(parsed, ExtendedFrame):
                self.extended = parsed
                self.last_extended_at = now

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
    )
