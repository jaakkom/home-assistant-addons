"""In-process Flow 5 heater used when the add-on is set to simulate.

The object behaves as a serial port: the real poll loop writes request
frames and reads status frames back. No UART device is opened.
"""

from __future__ import annotations

from .protocol import (
    DEV_EXTENDED,
    DEV_HEATER,
    DEV_PANEL,
    TYPE_DIAGNOSTIC,
    TYPE_EXTENDED,
    TYPE_STATUS,
    Framer,
    build_frame,
)


class SimulatedHeater:
    """Coolant heater model with the same frames as a Flow 5."""

    def __init__(self) -> None:
        self.burning = False
        self.pump_only = False
        self.coolant = 40.0
        self.fault = 0
        self.voltage = 12.6
        self.extended_enabled = False
        self.events: list[str] = []
        self.silent = False
        self._pending = bytearray()

    def write(self, data: bytes) -> None:
        for frame in Framer().feed(data):
            if not frame.crc_ok:
                continue
            if frame.dev == DEV_EXTENDED and frame.msg_type == TYPE_DIAGNOSTIC and frame.length == 0:
                self.extended_enabled = True
                self.events.append("diagnostic")
                continue
            if frame.dev != DEV_PANEL:
                continue
            if frame.msg_type == TYPE_STATUS:
                self.events.append("status")
            elif frame.msg_type in (0x01, 0x02):
                self.burning = True
                self.pump_only = False
                self.events.append("start")
            elif frame.msg_type == 0x03:
                self.burning = False
                self.pump_only = False
                self.events.append("stop")
            elif frame.msg_type == 0x21:
                self.pump_only = True
                self.burning = False
                self.events.append("pump")
            elif frame.msg_type == 0x11:
                self.events.append("cabin")
        self._advance()
        self._pending.extend(self._status_frame())
        if self.extended_enabled:
            self._pending.extend(self._extended_frame())

    def read(self, _size: int) -> bytes:
        if self.silent:
            self._pending.clear()
            return b""
        data = bytes(self._pending)
        self._pending.clear()
        return data

    def _advance(self) -> None:
        if self.burning:
            self.coolant = min(95.0, self.coolant + 6.0)
        elif self.coolant > 35.0:
            self.coolant = max(35.0, self.coolant - 5.0)

    def _state_bytes(self) -> tuple[int, int, int]:
        if self.burning:
            return 3, 2, 0xFF
        if self.pump_only:
            return 3, 7, 0
        return 0, 1, 0

    def _status_frame(self) -> bytes:
        state, sub, burner = self._state_bytes()
        raw = bytearray(18)
        raw[0] = state
        raw[1] = sub
        raw[2] = self.fault & 0xFF
        temp = int(self.coolant) & 0xFF
        raw[3] = temp
        raw[4] = temp
        raw[12] = burner
        return build_frame(DEV_HEATER, TYPE_STATUS, bytes(raw))

    def _extended_frame(self) -> bytes:
        state, sub, _burner = self._state_bytes()
        raw = bytearray(58)
        raw[0] = state
        raw[1] = sub
        raw[13] = 1 if self.burning else 0
        raw[15] = int((3.8 if self.burning else 0.0) * 10)
        flame = (500 if self.burning else 313)
        raw[17] = (flame >> 8) & 0xFF
        raw[18] = flame & 0xFF
        raw[19] = int(self.coolant) & 0xFF
        raw[20] = min(255, int(self.coolant) + 5)
        raw[21] = 35
        volts = int(self.voltage * 10)
        raw[22] = (volts >> 8) & 0xFF
        raw[23] = volts & 0xFF
        raw[36] = self.fault & 0xFF
        raw[52] = 1 if self.burning else 0
        return build_frame(DEV_EXTENDED, TYPE_EXTENDED, bytes(raw))
