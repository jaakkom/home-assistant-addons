import unittest

from autoterm_flow5.link import SerialLink
from autoterm_flow5.protocol import PUBR0, status_request


class FakePort:
    def __init__(self, response: bytes = b"") -> None:
        self.written = bytearray()
        self.response = response

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    def read(self, _size: int) -> bytes:
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


class LinkTests(unittest.TestCase):
    def test_one_empty_read_does_not_drop_the_poll(self):
        port = EmptyPort()
        link = SerialLink(port, extended_telemetry=False)
        link.poll(0)
        link.poll(1)
        self.assertEqual(port.reads, 2)
        self.assertIsNone(link.last_status_at)

    def test_repeated_empty_reads_ask_for_a_reopen(self):
        port = EmptyPort()
        link = SerialLink(port, extended_telemetry=False)
        link.poll(0)
        link.poll(1)
        with self.assertRaises(ConnectionError):
            link.poll(2)
        link.close()
        self.assertTrue(port.closed)

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


if __name__ == "__main__":
    unittest.main()
