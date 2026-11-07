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


class LinkTests(unittest.TestCase):
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
