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
# BINAR-5S profile command 32. The panel and Autoterm Test use it to read
# and write the control unit's stored operating settings.
TYPE_SETTINGS = 0x20
TYPE_EXTENDED = 0x01
# Empty device-0x02 query. Autoterm Test sends it, and this heater does not answer it.
TYPE_DIAGNOSTIC = 0x00
# Panel command that makes the heater start sending the parameter block.
TYPE_DIAGNOSTIC_MODE = 0x07

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


_CRC_TABLE = []
for _index in range(256):
    _crc = _index
    for _ in range(8):
        _crc = (_crc >> 1) ^ 0xA001 if _crc & 1 else _crc >> 1
    _CRC_TABLE.append(_crc)
_CRC_REV = {value: index for index, value in enumerate(_CRC_TABLE)}


def _crc_step(crc: int, byte: int) -> int:
    return _CRC_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)


def _crc_solve_byte(crc_in: int, crc_out: int) -> int | None:
    """The one data byte that carries `crc_in` to `crc_out`, if it exists."""
    index = _CRC_REV.get(crc_out ^ (crc_in >> 8))
    if index is None:
        return None
    return index ^ (crc_in & 0xFF)


def _apply_bits(mat: list[int], crc: int) -> int:
    out = 0
    for bit in range(16):
        if crc & (1 << bit):
            out ^= mat[bit]
    return out


def _compose(outer: list[int], inner: list[int]) -> list[int]:
    return [_apply_bits(outer, inner[bit]) for bit in range(16)]


def _invert_matrix(mat: list[int]) -> list[int]:
    aug = [0] * 16
    for row in range(16):
        bits = 0
        for col in range(16):
            if mat[col] & (1 << row):
                bits |= 1 << col
        aug[row] = bits | (1 << (16 + row))
    for col in range(16):
        pivot = next(row for row in range(col, 16) if aug[row] & (1 << col))
        aug[col], aug[pivot] = aug[pivot], aug[col]
        for row in range(16):
            if row != col and aug[row] & (1 << col):
                aug[row] ^= aug[col]
    inverse = [0] * 16
    for row in range(16):
        high = aug[row] >> 16
        for col in range(16):
            if high & (1 << col):
                inverse[col] |= 1 << row
    return inverse


def _crc_powers() -> tuple[list[list[int]], list[list[int]]]:
    step = [_crc_step(1 << bit, 0) for bit in range(16)]
    powers = [[1 << bit for bit in range(16)]]
    for _ in range(80):
        powers.append(_compose(step, powers[-1]))
    return powers, [_invert_matrix(power) for power in powers]


_CRC_POWERS, _CRC_INVERSE = _crc_powers()


def crc16_modbus(data: bytes) -> int:
    """CRC-16/MODBUS (poly 0xA001, init 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc = _crc_step(crc, byte)
    return crc


def _known_frame(dev: int, msg_type: int, length: int) -> bool:
    """Headers this heater actually sends. Anything else is a slipped byte."""
    if dev == DEV_EXTENDED and msg_type == TYPE_EXTENDED and length in (56, 58, 72):
        return True
    if dev == DEV_HEATER and msg_type == TYPE_STATUS and length == STATUS_LEN:
        return True
    if dev == DEV_HEATER and msg_type == TYPE_DIAGNOSTIC_MODE and length == 1:
        return True
    return False


def parameter_pending(buf: bytes) -> bool:
    """True when `buf` starts a parameter frame that has not been finished."""
    if len(buf) < HEADER_LEN or buf[0] != PREAMBLE:
        return False
    length = int.from_bytes(buf[2:4], "little")
    return buf[1] == DEV_EXTENDED and buf[4] == TYPE_EXTENDED and length in (56, 58, 72)


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
    """Keepalive the official tester writes. This Flow 5 does not answer it."""
    return build_frame(DEV_EXTENDED, TYPE_DIAGNOSTIC)


def diagnostic_enable() -> bytes:
    """Ask the heater to start sending the parameter block on its own.

    Captured from the official diagnostic software and listed in the
    BINAR-5S profile as command 7, payload 1. After the heater accepts it,
    it transmits device 0x02 type 0x01 frames without a further request.
    Payload 0 turns that stream off. This is not the start command.
    """
    return build_frame(DEV_PANEL, TYPE_DIAGNOSTIC_MODE, b"\x01")


def diagnostic_disable() -> bytes:
    return build_frame(DEV_PANEL, TYPE_DIAGNOSTIC_MODE, b"\x00")


def stop_command() -> bytes:
    return build_frame(DEV_PANEL, TYPE_STOP)


# Comfort Control sends a temperature and a run time. The run time is the
# first two bytes of the start payload, in minutes. 0xFFFF is the step past
# 12 hours, shown on the panel as unlimited. Mode 0x02 uses the temperature
# the panel reports (command 0x11) as the thermostat sensor.
MODE_BY_PANEL = 0x02
UNLIMITED_RUN = 0xFFFF


def start_thermostat(celsius: int = 20, minutes: int | None = None) -> bytes:
    """Start in thermostat mode with a temperature and a run time.

    A two-byte type 0x01 value is the run time, not the setpoint. Sending
    8 there ran this heater for eight minutes and then into cooldown, with
    the room either side of 8 °C. The setpoint is its own byte, and the
    run time can be unlimited.
    """
    temp = max(0, min(40, int(celsius)))
    if minutes is None or int(minutes) <= 0:
        run = UNLIMITED_RUN
    else:
        run = max(1, min(720, int(minutes)))
    payload = bytes((
        (run >> 8) & 0xFF,
        run & 0xFF,
        MODE_BY_PANEL,
        temp,
        0,
        0,
    ))
    return build_frame(DEV_PANEL, TYPE_START, payload)


def requested_temperature(celsius: int, minutes: int | None = None) -> bytes:
    """Thermostat start. The temperature is not written into the run-time bytes."""
    return start_thermostat(celsius, minutes)


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


# Byte order is the BINAR-5S profile's a1..a10 block for command 32.
# Temperatures are the Celsius byte. The mode numbers are the ones the
# BINAR-5S SMS settings command writes: auxiliary 0/2/3, and the on/off
# flags 0 or 1.
SETTINGS_LENGTHS = (9, 10)
AUXILIARY_MODES = {"off": 0, "auto": 2, "manual": 3}
AUXILIARY_NAMES = {0: "off", 2: "auto", 3: "manual"}
RELAY_MODES = {"auto": 0, "off": 1}
RELAY_NAMES = {0: "auto", 1: "off"}
PUMP_SIGNALS = {"plain": 0, "pwm": 1}
PUMP_SIGNAL_NAMES = {0: "plain", 1: "pwm"}
# Factory block from the modem's reset reply: standby 88 °C, auxiliary off,
# auxiliary standby 95 °C, cabin relay automatic at 40 °C, plain pump signal,
# pump only with the heater, pump on in standby, alarm channel on.
FACTORY_SETTINGS = bytes((88, 0, 95, 0, 40, 0, 0, 0, 0, 0))


@dataclass
class HeaterSettings:
    """Operating settings stored in the control unit, not in the add-on."""

    raw: bytes
    standby_c: int
    auxiliary_mode: int
    auxiliary_c: int
    relay_mode: int
    relay_c: int
    pump_signal: int
    pump_with_engine: int
    pump_standby: int
    alarm_channel: int


def settings_request() -> bytes:
    """Ask the control unit for its stored settings block."""
    return build_frame(DEV_PANEL, TYPE_SETTINGS)


def settings_write(payload: bytes) -> bytes:
    """Write the stored settings block back to the control unit."""
    return build_frame(DEV_PANEL, TYPE_SETTINGS, payload)


def decode_heater_settings(payload: bytes) -> HeaterSettings | None:
    """Parse a command-32 reply. Anything else is left untouched."""
    if len(payload) not in SETTINGS_LENGTHS:
        return None
    standby, auxiliary_c, relay_c = payload[0], payload[2], payload[4]
    if not (20 <= standby <= 95 and 80 <= auxiliary_c <= 95 and 30 <= relay_c <= 60):
        return None
    return HeaterSettings(
        raw=bytes(payload),
        standby_c=standby,
        auxiliary_mode=payload[1],
        auxiliary_c=auxiliary_c,
        relay_mode=payload[3],
        relay_c=relay_c,
        pump_signal=payload[5],
        pump_with_engine=payload[6],
        pump_standby=payload[7],
        alarm_channel=payload[8],
    )


def _named(table: dict[int, str], value: int) -> str:
    return table.get(value, str(value))


def heater_settings_view(parsed: HeaterSettings) -> dict[str, object]:
    """Values the add-on shows and edits for one settings block."""
    return {
        "coolant_limit": float(parsed.standby_c),
        "auxiliary_mode": _named(AUXILIARY_NAMES, parsed.auxiliary_mode),
        "auxiliary_limit": float(parsed.auxiliary_c),
        "relay_mode": _named(RELAY_NAMES, parsed.relay_mode),
        "fan_temperature": float(parsed.relay_c),
        "pump_signal": _named(PUMP_SIGNAL_NAMES, parsed.pump_signal),
        "pump_with_engine": parsed.pump_with_engine == 1,
        "pump_standby": parsed.pump_standby == 0,
        "alarm_channel": parsed.alarm_channel == 0,
    }


def patch_heater_settings(raw: bytes, changes: dict[str, object]) -> bytes:
    """Change only the named bytes. Every other stored byte stays as read."""
    data = bytearray(raw)
    if "coolant_limit" in changes:
        data[0] = max(20, min(95, int(round(changes["coolant_limit"]))))
    if "auxiliary_mode" in changes and changes["auxiliary_mode"] in AUXILIARY_MODES:
        data[1] = AUXILIARY_MODES[str(changes["auxiliary_mode"])]
    if "auxiliary_limit" in changes:
        data[2] = max(80, min(95, int(round(changes["auxiliary_limit"]))))
    if "relay_mode" in changes and changes["relay_mode"] in RELAY_MODES:
        data[3] = RELAY_MODES[str(changes["relay_mode"])]
    if "fan_temperature" in changes:
        data[4] = max(30, min(60, int(round(changes["fan_temperature"]))))
    if "pump_signal" in changes and changes["pump_signal"] in PUMP_SIGNALS:
        data[5] = PUMP_SIGNALS[str(changes["pump_signal"])]
    if "pump_with_engine" in changes:
        data[6] = 1 if changes["pump_with_engine"] else 0
    if "pump_standby" in changes:
        data[7] = 0 if changes["pump_standby"] else 1
    if "alarm_channel" in changes:
        data[8] = 0 if changes["alarm_channel"] else 1
    return bytes(data)


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
        self.bad_frames: list[str] = []
        self.bad_notes: list[str] = []
        self.lead = b""

    @property
    def pending(self) -> int:
        """Bytes of a frame that has not been completed yet."""
        return len(self._buf)

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
                if not self.lead:
                    self.lead = bytes(self._buf[: min(start, 16)])
                del self._buf[:start]
            if len(self._buf) < HEADER_LEN:
                break
            length = int.from_bytes(self._buf[2:4], "little")
            if length > MAX_PAYLOAD:
                self.resyncs += 1
                del self._buf[:1]
                continue
            total = HEADER_LEN + length + CRC_LEN
            if len(self._buf) >= total and crc_bytes(self._buf[: total - CRC_LEN]) == self._buf[total - CRC_LEN : total]:
                raw = bytes(self._buf[:total])
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
                continue
            nxt = self._next_header(total)
            if nxt == -1:
                # An 0xAA near the end is not yet long enough to classify.
                break
            if nxt is not None and nxt < total:
                restored = None
                if (
                    total - nxt == 1
                    and self._buf[1] == DEV_EXTENDED
                    and self._buf[4] == TYPE_EXTENDED
                ):
                    restored = _restore_one_byte(bytes(self._buf[:nxt]))
                if restored is not None:
                    frames.append(
                        Frame(
                            raw=restored,
                            dev=restored[1],
                            length=length,
                            msg_type=restored[4],
                            payload=restored[HEADER_LEN:-CRC_LEN],
                            crc_ok=True,
                        )
                    )
                    del self._buf[:nxt]
                    continue
                self.bad_crc += 1
                self.resyncs += 1
                if len(self.bad_frames) < 6 and len(self._buf) >= HEADER_LEN:
                    self.bad_frames.append(f"{self._buf[1]:02x}/{self._buf[4]:02x}/{length}")
                if len(self.bad_notes) < 2:
                    self.bad_notes.append(f"short by {total - nxt}, kept the next frame")
                # The declared window already contains the next header. Dropping
                # the whole window would eat that header and fail every frame after it.
                del self._buf[:nxt]
                continue
            if len(self._buf) < total:
                break
            self.bad_crc += 1
            self.resyncs += 1
            if len(self.bad_frames) < 6 and len(self._buf) >= HEADER_LEN:
                self.bad_frames.append(f"{self._buf[1]:02x}/{self._buf[4]:02x}/{length}")
            if len(self.bad_notes) < 2:
                self.bad_notes.append(self._bad_note(length))
            # A known header with a bad checksum is still one whole frame.
            # Dropping a single byte makes the following frame start in the
            # middle, so every later checksum fails too.
            if _known_frame(self._buf[1], self._buf[4], length):
                del self._buf[:total]
            else:
                del self._buf[:1]
        return frames

    def _next_header(self, total: int) -> int | None:
        """Index of the next known header before `total`.

        Returns -1 when an 0xAA in that window still needs more bytes.
        """
        index = 1
        limit = min(total, len(self._buf))
        while index < limit:
            found = self._buf.find(PREAMBLE, index, limit)
            if found < 0:
                return None
            if found + HEADER_LEN > len(self._buf):
                return -1
            length = int.from_bytes(self._buf[found + 2 : found + 4], "little")
            if length <= MAX_PAYLOAD and _known_frame(self._buf[found + 1], self._buf[found + 4], length):
                return found
            index = found + 1
        return None

    def reset(self) -> None:
        """Drop a partial frame that is no longer arriving."""
        self._buf.clear()

    def peek(self, limit: int = 24) -> bytes:
        return bytes(self._buf[:limit])

    def _bad_note(self, length: int) -> str:
        """Hex and checksum of one rejected frame, plus a nearby match if there is one."""
        total = HEADER_LEN + length + CRC_LEN
        raw = bytes(self._buf[:total])
        expect = crc_bytes(raw[:-CRC_LEN])
        parts = [
            f"frame {raw.hex()}",
            f"crc expect {expect.hex()} got {raw[-CRC_LEN:].hex()}",
        ]
        alts = []
        for trial in (56, 58, 72):
            if trial == length:
                continue
            trial_total = HEADER_LEN + trial + CRC_LEN
            if len(self._buf) < trial_total:
                continue
            chunk = bytes(self._buf[:trial_total])
            if crc_bytes(chunk[:-CRC_LEN]) == chunk[-CRC_LEN:]:
                alts.append(str(trial))
        if alts:
            parts.append("crc ok at length " + ",".join(alts))
        nxt = self._buf.find(PREAMBLE, 1)
        if nxt > 0 and len(self._buf) >= nxt + HEADER_LEN:
            nlen = int.from_bytes(self._buf[nxt + 2 : nxt + 4], "little")
            ntotal = HEADER_LEN + nlen + CRC_LEN
            if 0 < nlen <= MAX_PAYLOAD and len(self._buf) >= nxt + ntotal:
                chunk = bytes(self._buf[nxt : nxt + ntotal])
                mark = "ok" if crc_bytes(chunk[:-CRC_LEN]) == chunk[-CRC_LEN:] else "bad"
                parts.append(f"next aa +{nxt} {chunk[1]:02x}/{chunk[4]:02x}/{nlen} {mark}")
            else:
                parts.append(f"next aa +{nxt}")
        return " ".join(parts)


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
    # BINAR-5S formulas use bytes 1..56. Air heaters send 72 bytes in the same stream.
    if len(payload) < 56:
        return None
    raw = payload[:EXTENDED_LEN]
    if len(raw) < EXTENDED_LEN:
        raw = raw.ljust(EXTENDED_LEN, b"\x00")
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


def _plausible_extended(payload: bytes) -> bool:
    parsed = decode_extended(payload)
    if parsed is None:
        return False
    return (
        8.0 <= parsed.voltage <= 36.0
        and -40.0 <= parsed.flame_c <= 600.0
        and 0.0 <= parsed.pump_hz <= 10.0
    )


def _restore_one_byte(received: bytes) -> bytes | None:
    """Rebuild a parameter frame that lost one byte before the next header.

    The last two received bytes stay the checksum. Each earlier position has
    at most one byte that makes that checksum match. The frame is restored
    only when exactly one of those candidates has a real voltage, pump
    frequency, and flame temperature.
    """
    if len(received) < HEADER_LEN + CRC_LEN:
        return None
    data = received[:-CRC_LEN]
    target = int.from_bytes(received[-CRC_LEN:], "big")
    header = received[:HEADER_LEN]
    prefix = 0xFFFF
    prefixes = [prefix]
    for byte in data:
        prefix = _crc_step(prefix, byte)
        prefixes.append(prefix)
    suffix_const = [0] * (len(data) + 1)
    for index in range(len(data) + 1):
        state = 0
        for byte in data[index:]:
            state = _crc_step(state, byte)
        suffix_const[index] = state
    found: list[bytes] = []
    for index in range(len(data) + 1):
        remaining = len(data) - index
        required = _apply_bits(_CRC_INVERSE[remaining], target ^ suffix_const[index])
        value = _crc_solve_byte(prefixes[index], required)
        if value is None:
            continue
        trial = data[:index] + bytes((value,)) + data[index:] + received[-CRC_LEN:]
        if trial[:HEADER_LEN] != header:
            continue
        if crc_bytes(trial[:-CRC_LEN]) != trial[-CRC_LEN:]:
            continue
        if not _plausible_extended(trial[HEADER_LEN:-CRC_LEN]):
            continue
        found.append(trial)
        if len(found) > 1:
            return None
    if not found:
        return None
    return found[0]


def classify(frame: Frame) -> StatusFrame | ExtendedFrame | HeaterSettings | None:
    if not frame.crc_ok:
        return None
    if frame.dev == DEV_HEATER and frame.msg_type == TYPE_STATUS:
        return decode_status(frame.payload)
    if frame.dev == DEV_HEATER and frame.msg_type == TYPE_SETTINGS:
        return decode_heater_settings(frame.payload)
    if (
        frame.dev == DEV_EXTENDED
        and frame.msg_type in (TYPE_EXTENDED, TYPE_DIAGNOSTIC)
        and frame.length >= 56
    ):
        return decode_extended(frame.payload)
    return None
