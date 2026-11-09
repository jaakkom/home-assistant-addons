import json
import threading
import unittest
import urllib.error
import urllib.request

from autoterm_flow5.controller import Controller
from autoterm_flow5.fuel import FuelMeter
from autoterm_flow5.mqtt_bridge import apply_command
from autoterm_flow5.panel import PanelHub, PanelServer, command_accepted, ingress_prefix, page_language
from autoterm_flow5.settings import Settings


class PanelTests(unittest.TestCase):
    def test_language_and_ingress_prefix(self):
        self.assertEqual(page_language("fi-FI,fi;q=0.9,en;q=0.8"), "fi")
        self.assertEqual(page_language("en-GB,en;q=0.8"), "en")
        self.assertEqual(page_language(""), "en")
        self.assertEqual(ingress_prefix("/api/hassio_ingress/abc"), "/api/hassio_ingress/abc")
        self.assertEqual(ingress_prefix("/api/hassio_ingress/abc/"), "/api/hassio_ingress/abc")
        self.assertEqual(ingress_prefix('"/></script>'), "")
        self.assertEqual(ingress_prefix("https://example.test"), "")

    def test_only_known_commands_are_accepted(self):
        self.assertTrue(command_accepted("room_target", "21.5"))
        self.assertTrue(command_accepted("power", "ON"))
        self.assertTrue(command_accepted("fuel_refuel", "pressed"))
        self.assertFalse(command_accepted("serial_port", "/dev/ttyUSB0"))
        self.assertFalse(command_accepted("mode", "boost"))
        self.assertFalse(command_accepted("room_target", "warm"))

    def test_page_reads_state_and_queues_commands(self):
        hub = PanelHub()
        server = PanelServer(("127.0.0.1", 0), hub)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        port = server.server_address[1]
        hub.publish({"coolant": "70", "power": "OFF", "fault_text": "<hot>"})

        page = urllib.request.urlopen(
            urllib.request.Request(
                f"http://127.0.0.1:{port}/",
                headers={
                    "Accept-Language": "fi",
                    "X-Ingress-Path": "/api/hassio_ingress/abc",
                },
            )
        )
        body = page.read().decode("utf-8")
        self.assertIn('const LANG = "fi"', body)
        self.assertIn('lang="fi"', body)
        self.assertIn('const PREFIX = "/api/hassio_ingress/abc"', body)
        self.assertIn("Tallenna", body)
        self.assertIn("Liekki", body)
        self.assertIn("function panelRoot", body)
        self.assertIn('id="heat"', body)
        self.assertTrue(command_accepted("debug", "ON"))

        state = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state").read().decode("utf-8"))
        self.assertEqual(state["coolant"], "70")

        posted = json.dumps({"commands": [{"key": "room_target", "value": "21"}, {"key": "power", "value": "ON"}]}).encode()
        response = urllib.request.urlopen(
            urllib.request.Request(
                f"http://127.0.0.1:{port}/api/command",
                data=posted,
                headers={"Content-Type": "application/json"},
            )
        )
        self.assertEqual(response.status, 200)
        settings = Settings()
        controller = Controller(settings)
        for topic, payload in hub.drain():
            apply_command(controller, settings, topic, payload, 0, FuelMeter())
        self.assertEqual(settings.room_target, 21)
        self.assertTrue(controller.heat_enabled)

        rejected = json.dumps({"key": "serial_port", "value": "/dev/ttyUSB0"}).encode()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/command",
                    data=rejected,
                    headers={"Content-Type": "application/json"},
                )
            )
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(hub.drain(), [])


if __name__ == "__main__":
    unittest.main()
