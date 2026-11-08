import unittest

from autoterm_flow5.link import LinkProgress, SerialLink, TelemetryLog
from autoterm_flow5.protocol import PUBR0, build_frame, decode_extended, status_request


class FakePort:
    def __init__(self, response: bytes = b"") -> None:
        self.written = bytearray()
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

    def test_poll_requests_status_and_handshake_once(self):
        port = FakePort()
        link = SerialLink(port, extended_telemetry=True)
        link.poll(0)
        self.assertTrue(bytes(port.written).startswith(status_request()))
        self.assertIn(PUBR0, bytes(port.written))
        port.written.clear()
        link.poll(10)
        self.assertNotIn(PUBR0, bytes(port.written))
        self.assertTrue(bytes(port.written).startswith(status_request()))


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
        self.assertIsNone(progress.update(10, False, link))
        first = progress.update(10, True, link)
        self.assertEqual(first[0], "info")
        self.assertIn("12.50 V", first[1])
        self.assertIn("3.80 Hz", first[1])
        self.assertIn("High", first[1])
        self.assertIsNone(progress.update(20, True, link))
        link.last_extended_at = 70
        self.assertEqual(progress.update(70, True, link)[0], "info")

    def test_debug_warns_once_when_the_frame_never_arrives(self):
        link = SerialLink(FakePort(), extended_telemetry=True)
        progress = TelemetryLog()
        self.assertEqual(progress.update(1, True, link), ("warning", "58-byte telemetry has not arrived"))
        self.assertIsNone(progress.update(2, True, link))
        self.assertIsNone(progress.update(3, False, link))
        self.assertEqual(progress.update(4, True, link)[0], "warning")

    def test_debug_reports_when_a_frame_stops(self):
        link = SerialLink(FakePort(), extended_telemetry=True)
        link.extended = _extended_frame()
        link.last_extended_at = 0
        progress = TelemetryLog()
        self.assertEqual(progress.update(0, True, link)[0], "info")
        stopped = progress.update(60, True, link)
        self.assertEqual(stopped[0], "warning")
        self.assertIn("60 s ago", stopped[1])
        self.assertIsNone(progress.update(61, True, link))


if __name__ == "__main__":
    unittest.main()
