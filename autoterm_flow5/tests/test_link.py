import unittest

from autoterm_flow5.link import LinkProgress, SerialLink, TelemetryLog
from autoterm_flow5.protocol import (
    build_frame,
    decode_extended,
    diagnostic_enable,
    start_thermostat,
    status_request,
)


class FakePort:
    def __init__(self, response: bytes = b"") -> None:
        self.written = bytearray()
        self.writes = 0
        self.response = response

    def write(self, data: bytes) -> None:
        self.written.extend(data)
        self.writes = getattr(self, "writes", 0) + 1

    def read(self, _size: int) -> bytes:
        chunks = getattr(self, "chunks", None)
        if chunks is not None:
            if not chunks:
                return b""
            return chunks.pop(0)
        data = self.response
        self.response = b""
        return data


class EmptyPort(FakePort):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0
        self.closed = False

    def read(self, _size: int) -> bytes:
        self.reads += 1
        raise OSError(
            "device reports readiness to read but returned no data "
            "(device disconnected or multiple access on port?)"
        )

    def close(self) -> None:
        self.closed = True


class LinkProgressTests(unittest.TestCase):
    def test_silence_is_reported_once(self):
        progress = LinkProgress()
        progress.opened(0)
        self.assertIsNone(progress.update(9, False))
        self.assertEqual(progress.update(10, False), "silent")
        self.assertIsNone(progress.update(30, False))

    def test_answer_and_loss(self):
        progress = LinkProgress()
        progress.opened(0)
        self.assertEqual(progress.update(1, True), "answered")
        self.assertIsNone(progress.update(2, True))
        self.assertEqual(progress.update(3, False), "lost")
        self.assertIsNone(progress.update(4, False))


class LinkTests(unittest.TestCase):
    def test_one_empty_read_does_not_drop_the_poll(self):
        port = EmptyPort()
        link = SerialLink(port, extended_telemetry=False)
        link.last_status_at = 0
        link.poll(1)
        self.assertGreaterEqual(port.reads, 1)
        self.assertEqual(link.last_status_at, 0)

    def test_repeated_empty_reads_ask_for_a_reopen(self):
        port = EmptyPort()
        link = SerialLink(port, extended_telemetry=False)
        with self.assertRaises(ConnectionError):
            link.poll(0)
        link.close()
        self.assertTrue(port.closed)

    def test_a_recent_status_survives_an_empty_read_glitch(self):
        port = EmptyPort()
        link = SerialLink(port, extended_telemetry=False)
        link.last_status_at = 0
        link.poll(1)
        self.assertEqual(link.last_status_at, 0)

    def test_a_late_status_frame_is_read_before_the_next_request(self):
        payload = bytes.fromhex("02030046460081430001015cff0100000000")
        frame = build_frame(0x04, 0x0F, payload)
        port = FakePort()
        port.chunks = [b"", frame]
        link = SerialLink(port, extended_telemetry=False)
        link.poll(5)
        self.assertEqual(link.last_status_at, 5)
        self.assertEqual(link.status.mode_name, "Ignition 1")
        self.assertEqual(port.writes, 1)

    def test_extended_frame_after_the_status_is_read_in_the_same_poll(self):
        status_payload = bytes.fromhex("02030046460081430001015cff0100000000")
        raw = bytearray(58)
        raw[0] = 3
        raw[1] = 2
        raw[15] = 38
        raw[17], raw[18] = 0x01, 0xF4
        raw[22], raw[23] = 0, 125
        port = FakePort()
        port.chunks = [
            build_frame(0x04, 0x0F, status_payload),
            build_frame(0x02, 0x01, bytes(raw)),
        ]
        link = SerialLink(port, extended_telemetry=True)
        link.last_handshake_at = 4
        link.poll(5)
        self.assertEqual(link.status.mode_name, "Ignition 1")
        self.assertEqual(link.last_extended_at, 5)
        self.assertEqual(link.extended.voltage, 12.5)
        self.assertEqual(port.writes, 0)

    def test_an_incoming_frame_is_finished_before_the_status_request(self):
        raw = bytearray(58)
        raw[22], raw[23] = 0, 125
        frame = build_frame(0x02, 0x01, bytes(raw))
        port = FakePort()
        port.chunks = [frame[:20], frame[20:], b""]
        port.order: list[str] = []
        write = port.write
        read = port.read

        def record_write(data: bytes) -> None:
            port.order.append("w")
            write(data)

        def record_read(size: int) -> bytes:
            port.order.append("r")
            return read(size)

        port.write = record_write
        port.read = record_read
        link = SerialLink(port, extended_telemetry=True)
        link.last_handshake_at = 0
        link.last_extended_at = 1
        link.poll(10)
        self.assertEqual(link.extended.voltage, 12.5)
        self.assertLess(port.order.index("r"), port.order.index("w"))
        self.assertTrue(bytes(port.written).startswith(status_request()))

    def test_poll_requests_status_and_handshake_once(self):
        port = FakePort()
        link = SerialLink(port, extended_telemetry=True)
        link.poll(0)
        written = bytes(port.written)
        self.assertTrue(written.startswith(diagnostic_enable()))
        self.assertIn(status_request(), written)
        self.assertLess(written.index(diagnostic_enable()), written.index(status_request()))
        port.written.clear()
        link.poll(4)
        self.assertNotIn(diagnostic_enable(), bytes(port.written))
        self.assertTrue(bytes(port.written).startswith(status_request()))
        port.written.clear()
        link.poll(5)
        self.assertIn(diagnostic_enable(), bytes(port.written))

    def test_diagnostic_mode_stays_off_while_frames_arrive(self):
        port = FakePort()
        link = SerialLink(port, extended_telemetry=True)
        link.last_extended_at = 0
        link.last_handshake_at = 0
        link.poll(8)
        self.assertNotIn(diagnostic_enable(), bytes(port.written))
        port.written.clear()
        link.poll(60)
        self.assertTrue(bytes(port.written).startswith(diagnostic_enable()))

    def test_status_stays_quiet_while_parameter_frames_arrive(self):
        frame = build_frame(0x02, 0x01, bytes(bytearray(58)))
        port = FakePort()
        port.chunks = [frame]
        link = SerialLink(port, extended_telemetry=True)
        link.last_handshake_at = 0
        link.last_status_at = 9
        link.last_extended_at = 9
        link.poll(10)
        self.assertEqual(link.last_extended_at, 10)
        self.assertEqual(port.writes, 0)

    def test_status_is_asked_again_after_the_quiet_period(self):
        frame = build_frame(0x02, 0x01, bytes(bytearray(58)))
        port = FakePort()
        port.chunks = [frame]
        link = SerialLink(port, extended_telemetry=True)
        link.last_handshake_at = 0
        link.last_status_at = 0
        link.last_extended_at = 0
        link.poll(8)
        self.assertEqual(port.writes, 1)
        self.assertIn(status_request(), bytes(port.written))

    def test_a_command_is_its_own_frame(self):
        port = FakePort()
        link = SerialLink(port, extended_telemetry=False)
        command = bytes.fromhex("aa03000003d84f")
        link.poll(1, [command])
        self.assertEqual(port.writes, 2)
        self.assertTrue(bytes(port.written).startswith(command))
        self.assertIn(status_request(), bytes(port.written))

    def test_a_corrupt_burst_is_not_answered(self):
        port = FakePort()
        port.chunks = [bytes.fromhex("aa02040001000000000000")]
        link = SerialLink(port, extended_telemetry=True)
        link.last_handshake_at = 0
        link.last_extended_at = 0
        link.last_status_at = 0
        link.poll(10)
        self.assertGreater(link.bad_crc, 0)
        self.assertEqual(port.writes, 0)
        self.assertEqual(link.framer.bad_frames, ["02/01/4"])

    def test_a_command_waits_out_a_partial_frame(self):
        frame = build_frame(0x02, 0x01, bytes(bytearray(58)))
        port = FakePort()
        port.chunks = [frame[:30]]
        link = SerialLink(port, extended_telemetry=True)
        link.last_handshake_at = 0
        link.last_extended_at = 0
        link.last_status_at = 0
        command = bytes.fromhex("aa03000003d84f")
        link.poll(10, [command])
        self.assertEqual(port.writes, 0)
        self.assertEqual(link._held, [command])
        port.chunks = [frame[30:]]
        link.poll(11)
        self.assertIn(command, bytes(port.written))

    def test_a_parameter_frame_keeps_the_link_online(self):
        link = SerialLink(FakePort(), extended_telemetry=True)
        link.last_extended_at = 0
        self.assertTrue(link.online(10))
        self.assertFalse(link.online(16))

    def test_a_bad_parameter_frame_does_not_hide_the_next_one(self):
        payload = bytes(56)
        good = build_frame(0x02, 0x01, payload)
        bad = bytearray(good)
        bad[-1] ^= 0xFF
        port = FakePort()
        port.chunks = [bytes(bad) + good]
        link = SerialLink(port, extended_telemetry=True)
        link.last_handshake_at = 0
        link.last_status_at = 0
        link.poll(5)
        self.assertEqual(link.bad_crc, 1)
        self.assertEqual(link.last_extended_at, 5)
        self.assertEqual(port.writes, 0)

    def test_a_broken_parameter_stream_still_carries_a_start_command(self):
        good = build_frame(0x02, 0x01, bytes(56))
        bad = bytearray(good)
        bad[-1] ^= 0xFF
        port = FakePort()
        port.chunks = [bytes(bad)]
        link = SerialLink(port, extended_telemetry=True)
        link.last_handshake_at = 0
        link.last_extended_at = 0
        link.last_status_at = 0
        link.poll(30, [start_thermostat()])
        self.assertEqual(port.writes, 1)
        self.assertTrue(bytes(port.written).startswith(start_thermostat()))
        self.assertNotIn(diagnostic_enable(), bytes(port.written))
        self.assertNotIn(status_request(), bytes(port.written))

    def test_a_stuck_partial_does_not_hold_the_command(self):
        frame = build_frame(0x02, 0x01, bytes(bytearray(58)))
        port = FakePort()
        port.chunks = [frame[:30]]
        link = SerialLink(port, extended_telemetry=True)
        link.last_handshake_at = 0
        link.last_extended_at = 0
        link.last_status_at = 9
        command = bytes.fromhex("aa03000003d84f")
        link.poll(10, [command])
        self.assertEqual(port.writes, 0)
        link.poll(11)
        self.assertIn(command, bytes(port.written))

    def test_debug_names_a_frame_that_failed_the_checksum(self):
        port = FakePort()
        port.chunks = [bytes.fromhex("aa02040001000000000000")]
        link = SerialLink(port, extended_telemetry=True)
        link.last_handshake_at = 0
        link.last_extended_at = 0
        link.poll(70)
        lines = TelemetryLog().update(70, True, link)
        self.assertTrue(lines)
        self.assertIn("bad 02/01/4", lines[0][1])


def _extended_frame():
    raw = bytearray(58)
    raw[0] = 3
    raw[1] = 2
    raw[15] = 38
    raw[17], raw[18] = 0x01, 0xF4
    raw[22], raw[23] = 0, 125
    return decode_extended(bytes(raw))


class TelemetryLogTests(unittest.TestCase):
    def test_debug_reports_a_frame_once_a_minute(self):
        link = SerialLink(FakePort(), extended_telemetry=True)
        link.extended = _extended_frame()
        link.last_extended_at = 10
        progress = TelemetryLog()
        self.assertEqual(progress.update(10, False, link), [])
        first = progress.update(10, True, link)
        ok = next(text for level, text in first if "12.50 V" in text)
        self.assertEqual(first[0][0], "info")
        self.assertIn("3.80 Hz", ok)
        self.assertIn("High", ok)
        self.assertEqual(progress.update(20, True, link), [])
        link.last_extended_at = 70
        self.assertEqual(progress.update(70, True, link)[0][0], "info")

    def test_debug_warns_once_when_the_frame_never_arrives(self):
        link = SerialLink(FakePort(), extended_telemetry=True)
        progress = TelemetryLog()
        self.assertEqual(
            progress.update(1, True, link),
            [("warning", "58-byte telemetry has not arrived, heard 0 bytes")],
        )
        self.assertEqual(progress.update(2, True, link), [])
        self.assertEqual(progress.update(3, False, link), [])
        self.assertEqual(progress.update(4, True, link)[0][0], "warning")

    def test_debug_repeats_when_the_heater_status_changes(self):
        link = SerialLink(FakePort(), extended_telemetry=True)
        link.status = type("Status", (), {"mode_name": "Waiting for a command"})()
        progress = TelemetryLog()
        first = progress.update(1, True, link)
        self.assertIn(("info", "heater status: Waiting for a command"), first)
        self.assertIn(("warning", "58-byte telemetry has not arrived, heard 0 bytes"), first)
        link.status = type("Status", (), {"mode_name": "Ignition 1"})()
        second = progress.update(2, True, link)
        self.assertIn(("info", "heater status: Ignition 1"), second)
        self.assertTrue(any("has not arrived" in text for _level, text in second))
        link.last_handshake_at = 61
        link.rx_bytes = 25
        third = progress.update(61, True, link)
        self.assertEqual(third, [("warning", "diagnostic reports requested, no 58-byte frame yet, heard 25 bytes")])

    def test_debug_reports_when_a_frame_stops(self):
        link = SerialLink(FakePort(), extended_telemetry=True)
        link.extended = _extended_frame()
        link.last_extended_at = 0
        progress = TelemetryLog()
        self.assertEqual(progress.update(0, True, link)[0][0], "info")
        stopped = progress.update(60, True, link)
        self.assertEqual(stopped[0][0], "warning")
        self.assertIn("60 s ago", stopped[0][1])
        self.assertIn("heard 0 bytes", stopped[0][1])
        self.assertEqual(progress.update(61, True, link), [])


if __name__ == "__main__":
    unittest.main()
