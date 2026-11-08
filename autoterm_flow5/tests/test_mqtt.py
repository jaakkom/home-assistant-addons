import json
import unittest

from autoterm_flow5.controller import Controller
from autoterm_flow5.fuel import FuelMeter
from autoterm_flow5.mqtt_bridge import apply_command, collect_state, discovery_messages
from autoterm_flow5.settings import Settings


class MqttTests(unittest.TestCase):
    def test_discovery_covers_faults_fuel_and_limits(self):
        blob = "\n".join(payload for _topic, payload in discovery_messages())
        self.assertIn("Fault text", blob)
        self.assertIn("Fault code", blob)
        self.assertIn("Fuel consumption", blob)
        self.assertIn("Coolant limit", blob)
        decoded = [json.loads(payload) for _topic, payload in discovery_messages()]
        names = {item.get("name") for item in decoded}
        self.assertIn("Fault text", names)
        self.assertIn("Fuel consumption", names)
        self.assertIn("Coolant limit", names)
        self.assertIn("Fuel total", names)
        self.assertIn("Fuel hour average", names)
        self.assertIn("Fuel level", names)
        self.assertIn("Fuel time remaining", names)
        self.assertIn("Tank filled", names)
        diagnostic = [item for item in decoded if item.get("entity_category") == "diagnostic"]
        self.assertTrue(any(item.get("name", "").startswith("Status byte") for item in diagnostic))
        self.assertTrue(any(item.get("name", "").startswith("Extended byte") for item in diagnostic))

    def test_coolant_limit_command_is_clamped_and_kept(self):
        settings = Settings()
        controller = Controller(settings)
        apply_command(controller, settings, "autoterm_flow5/set/coolant_limit", "70", 0)
        self.assertEqual(settings.coolant_limit, 70)
        self.assertEqual(controller.settings.coolant_limit, 70)
        apply_command(controller, settings, "autoterm_flow5/set/coolant_limit", "120", 1)
        self.assertEqual(settings.coolant_limit, 95)

    def test_refuel_and_manual_level(self):
        settings = Settings(tank_capacity_l=20)
        controller = Controller(settings)
        fuel = FuelMeter()
        apply_command(controller, settings, "autoterm_flow5/set/fuel_refuel", "", 0, fuel)
        self.assertEqual(fuel.remaining_ml, 20000)
        apply_command(controller, settings, "autoterm_flow5/set/fuel_level", "7.5", 1, fuel)
        self.assertEqual(fuel.remaining_liters, 7.5)
        apply_command(controller, settings, "autoterm_flow5/set/tank_capacity", "5", 2, fuel)
        self.assertEqual(settings.tank_capacity_l, 5)
        self.assertEqual(fuel.remaining_ml, 5000)
        values = collect_state(
            None,
            None,
            fuel=fuel,
            settings=settings,
            controller=controller,
            room_c=None,
            online=True,
            telemetry_stale=False,
            fan_on=False,
            frequency_hz=0,
        )
        self.assertEqual(values["fuel_level_percent"], "100.00")
        self.assertEqual(values["fuel_hours"], "")


if __name__ == "__main__":
    unittest.main()
