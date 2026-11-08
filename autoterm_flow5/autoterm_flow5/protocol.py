"""Flow 5 UART frames.

Frame layout, confirmed against captured Flow 5 traffic:

    AA | dev | len_lo | len_hi | type | payload | crc_hi | crc_lo

CRC-16/MODBUS over the whole frame including the leading 0xAA, sent
most-significant byte first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PREAMBLE = 0xAA
DEV_PANEL = 0x03
DEV_HEATER = 0x04
DEV_EXTENDED = 0x02

TYPE_START = 0x01
TYPE_PREHEAT_DURATION = 0x02
TYPE_STOP = 0x03
TYPE_STATUS = 0x0F
TYPE_CABIN_TEMP = 0x11
TYPE_PUMP = 0x21
TYPE_EXTENDED = 0x01
# Autoterm Test asks for the parameter block with device 0x02 and type 0x00.
TYPE_DIAGNOSTIC = 0x00

HEADER_LEN = 5
CRC_LEN = 2
MAX_PAYLOAD = 250

# Fixed diagnostic handshake. Its CRC is least-significant byte first,
# unlike every other frame, so the bytes are stored as captured.
PUBR0 = bytes.fromhex("aa5055425230000000ffffffff0fb0")

STATUS_LEN = 18
EXTENDED_LEN = 58

# state, substate -> English name from the vendor mode table.
STATE_NAMES: dict[tuple[int, int], str] = {
    (0, 0): "Off",
    (0, 1): "Waiting for a command",
    (1, 0): "Waiting for temperature reduction",
    (1, 1): "Locked",
    (2, 0): "Cooling",
    (2, 1): "Glow plug warming up",
    (2, 2): "Preparation for ignition",
    (2, 3): "Ignition 1",
    (2, 4): "Ignition 2",
    (2, 5): "Blowing",
    (2, 6): "Combustion chamber heating",
    (2, 7): "Blowing",
    (3, 0): "Low",
    (3, 2): "High",
    (3, 4): "Blowing",
    (3, 5): "Waiting",
    (3, 6): "Blowing",
    (3, 7): "Pump only",
    (3, 8): "Middle",
    (4, 0): "Cooldown",
    (4, 3): "Shutting down",
}

# 18-byte status indexes that have no confirmed meaning.
STATUS_UNMAPPED = (5, 8, 10, 13, 14, 15, 17)

# 58-byte extended indexes consumed by a named field.
EXTENDED_MAPPED = frozenset(
    {0, 1, 2, 3, 4, 11, 12, 13, 15, 17, 18, 19, 20, 21, 22, 23, 36, 51, 52, 54, 55}
)


def crc16_modbus(data: bytes) -> int:
    """CRC-16/MODBUS (poly 0xA001, init 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def crc_bytes(data: bytes) -> bytes:
    """CRC as it appears on the wire: high byte first."""
    return crc16_modbus(data).to_bytes(2, "big")


def build_frame(dev: int, msg_type: int, payload: bytes = b"") -> bytes:
    body = bytes([PREAMBLE, dev & 0xFF]) + len(payload).to_bytes(2, "little")
    body += bytes([msg_type & 0xFF]) + payload
    return body + crc_bytes(body)


def status_request() -> bytes:
    return build_frame(DEV_PANEL, TYPE_STATUS)


def diagnostic_request() -> bytes:
    """Ask for the 58-byte parameter block.

    Autoterm Test 1.17.3.0 sends this from Timer3 every eight seconds while
    the port is open. It is device 0x02, type 0x00, and an empty payload.
    The status request stays on device 0x03 type 0x0F and only returns the
    18-byte status. This is not the panel start command: that is device
    0x03 type 0x01.
    """
    return build_frame(DEV_EXTENDED, TYPE_DIAGNOSTIC)


def stop_command() -> bytes:
    return build_frame(DEV_PANEL, TYPE_STOP)


def start_thermostat() -> bytes:
    return build_frame(DEV_PANEL, TYPE_START, bytes.fromhex("0022"))


def start_preheat(minutes: int) -> list[bytes]:
    """Preheat start.

    Below 120 minutes the captured panel sends a fixed marker and then the
    duration. From 120 minutes up the duration is the only payload.
    """
    minutes = max(1, min(480, int(minutes)))
    if minutes >= 120:
        return [build_frame(DEV_PANEL, TYPE_START, minutes.to_bytes(2, "big"))]
    marker = build_frame(DEV_PANEL, TYPE_START, bytes.fromhex("001e"))
    duration = build_frame(DEV_PANEL, TYPE_PREHEAT_DURATION, minutes.to_bytes(2, "big"))
    return [marker, duration]


def start_pump() -> bytes:
    return build_frame(DEV_PANEL, TYPE_PUMP, bytes.fromhex("0028"))


def cabin_temperature(celsius: int) -> bytes:
    value = max(0, min(125, int(celsius)))
    return build_frame(DEV_PANEL, TYPE_CABIN_TEMP, bytes([value]))


def state_name(state: int, substate: int) -> str:
    return STATE_NAMES.get((state, substate), f"State {state}.{substate}")


@dataclass
class Frame:
    raw: bytes
    dev: int
    length: int
    msg_type: int
    payload: bytes
    crc_ok: bool

    @property
    def hex(self) -> str:
        return self.raw.hex()


class Framer:
    """Split a byte stream into CRC-checked frames."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.resyncs = 0
        self.bad_crc = 0

    def feed(self, data: bytes) -> list[Frame]:
        self._buf.extend(data)
        frames: list[Frame] = []
        while True:
            start = self._buf.find(PREAMBLE)
            if start < 0:
                self._buf.clear()
                break
            if start:
                self.resyncs += 1
                del self._buf[:start]
            if len(self._buf) < HEADER_LEN:
                break
            length = int.from_bytes(self._buf[2:4], "little")
            if length > MAX_PAYLOAD:
                self.resyncs += 1
                del self._buf[:1]
                continue
            total = HEADER_LEN + length + CRC_LEN
            if len(self._buf) < total:
                break
            raw = bytes(self._buf[:total])
            ok = crc_bytes(raw[:-CRC_LEN]) == raw[-CRC_LEN:]
            if ok:
                frames.append(
                    Frame(
                        raw=raw,
                        dev=raw[1],
                        length=length,
                        msg_type=raw[4],
                        payload=raw[HEADER_LEN:-CRC_LEN],
                        crc_ok=True,
                    )
                )
                del self._buf[:total]
            else:
                self.bad_crc += 1
                self.resyncs += 1
                del self._buf[:1]
        return frames


@dataclass
class StatusFrame:
    """18-byte panel status (dev 0x04, type 0x0F)."""

    state: int
    substate: int
    fault: int
    coolant_a: int
    coolant_b: int
    voltage_like: int
    rising_sensor: int
    elapsed_minutes: int
    elapsed_seconds: int
    burner_active: bool
    cooldown_counter: int
    raw: bytes
    unmapped: dict[int, int] = field(default_factory=dict)

    @property
    def coolant_c(self) -> float:
        return (self.coolant_a + self.coolant_b) / 2

    @property
    def mode_name(self) -> str:
        return state_name(self.state, self.substate)

    @property
    def hex(self) -> str:
        return self.raw.hex()


@dataclass
class ExtendedFrame:
    """58-byte diagnostic telemetry (dev 0x02, type 0x01)."""

    state: int
    substate: int
    running_time_s: int
    fan_target: int
    fan_actual: int
    glow_plug: bool
    pump_hz: float
    flame_c: float
    liquid_c: int
    overheat_c: int
    board_c: int
    voltage: float
    fault: int
    engine_state: int
    relay_state: int
    fan_current: int
    raw: bytes
    unmapped: dict[int, int] = field(default_factory=dict)

    @property
    def mode_name(self) -> str:
        return state_name(self.state, self.substate)

    @property
    def hex(self) -> str:
        return self.raw.hex()


def decode_status(payload: bytes) -> StatusFrame | None:
    if len(payload) < STATUS_LEN:
        return None
    raw = payload[:STATUS_LEN]
    return StatusFrame(
        state=raw[0],
        substate=raw[1],
        fault=raw[2],
        coolant_a=raw[3],
        coolant_b=raw[4],
        voltage_like=raw[6],
        rising_sensor=raw[7],
        elapsed_minutes=raw[9],
        elapsed_seconds=raw[11],
        burner_active=raw[12] == 0xFF,
        cooldown_counter=raw[16],
        raw=raw,
        unmapped={index: raw[index] for index in STATUS_UNMAPPED},
    )


def decode_extended(payload: bytes) -> ExtendedFrame | None:
    if len(payload) < EXTENDED_LEN:
        return None
    raw = payload[:EXTENDED_LEN]
    return ExtendedFrame(
        state=raw[0],
        substate=raw[1],
        running_time_s=raw[2] * 65536 + raw[3] * 256 + raw[4],
        fan_target=raw[11],
        fan_actual=raw[12],
        glow_plug=raw[13] > 0,
        pump_hz=raw[15] / 10,
        flame_c=(raw[17] * 256 + raw[18]) - 273,
        liquid_c=raw[19],
        overheat_c=raw[20],
        board_c=raw[21],
        voltage=(raw[22] * 256 + raw[23]) / 10,
        fault=raw[36],
        engine_state=raw[51],
        relay_state=raw[52],
        fan_current=raw[54] * 256 + raw[55],
        raw=raw,
        unmapped={
            index: raw[index]
            for index in range(EXTENDED_LEN)
            if index not in EXTENDED_MAPPED
        },
    )


def classify(frame: Frame) -> StatusFrame | ExtendedFrame | None:
    if not frame.crc_ok:
        return None
    if frame.dev == DEV_HEATER and frame.msg_type == TYPE_STATUS:
        return decode_status(frame.payload)
    if (
        frame.dev == DEV_EXTENDED
        and frame.msg_type in (TYPE_EXTENDED, TYPE_DIAGNOSTIC)
        and frame.length == EXTENDED_LEN
    ):
        return decode_extended(frame.payload)
    return None
