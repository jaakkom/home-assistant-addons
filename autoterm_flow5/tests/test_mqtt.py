import json
import unittest

from autoterm_flow5.controller import Controller
from autoterm_flow5.mqtt_bridge import apply_command, discovery_messages
from autoterm_flow5.settings import Settings


class MqttTests(unittest.TestCase):
    def test_discovery_covers_faults_fuel_and_limits(self):
        blob = "\n".join(payload for _topic, payload in discovery_messages())
        self.assertIn("Vikateksti", blob)
        self.assertIn("Vikakoodi", blob)
        self.assertIn("Kulutus", blob)
        self.assertIn("Nesteen yläraja", blob)
        decoded = [json.loads(payload) for _topic, payload in discovery_messages()]
        names = {item.get("name") for item in decoded}
        self.assertIn("Vikateksti", names)
        self.assertIn("Kulutus", names)
        self.assertIn("Nesteen yläraja", names)
        self.assertIn("Polttoainekertymä", names)
        diagnostic = [item for item in decoded if item.get("entity_category") == "diagnostic"]
        self.assertTrue(any(item.get("name", "").startswith("Tilatavu") for item in diagnostic))
        self.assertTrue(any(item.get("name", "").startswith("Laajennettu tavu") for item in diagnostic))

    def test_coolant_limit_command_is_clamped_and_kept(self):
        settings = Settings()
        controller = Controller(settings)
        apply_command(controller, settings, "autoterm_flow5/set/coolant_limit", "70", 0)
        self.assertEqual(settings.coolant_limit, 70)
        self.assertEqual(controller.settings.coolant_limit, 70)
        apply_command(controller, settings, "autoterm_flow5/set/coolant_limit", "120", 1)
        self.assertEqual(settings.coolant_limit, 95)


if __name__ == "__main__":
    unittest.main()
