import json
import unittest

from autoterm_flow5.controller import Controller
from autoterm_flow5.fuel import FuelMeter
from autoterm_flow5.mqtt_bridge import apply_command, collect_state, discovery_messages, mqtt_state_updates
from autoterm_flow5.protocol import decode_extended
from autoterm_flow5.settings import Settings


class MqttTests(unittest.TestCase):
    def test_discovery_covers_faults_fuel_and_limits(self):
        blob = "\n".join(payload for _topic, payload in discovery_messages())
        self.assertIn("Fault text", blob)
        self.assertIn("Fault code", blob)
        self.assertIn("Fuel consumption", blob)
        self.assertIn("Coolant limit", blob)
        decoded = [json.loads(payload) for _topic, payload in discovery_messages() if payload]
        names = {item.get("name") for item in decoded}
        self.assertIn("Fault text", names)
        self.assertIn("Fuel consumption", names)
        self.assertIn("Coolant limit", names)
        self.assertIn("Fuel total", names)
        self.assertIn("Fuel hour average", names)
        self.assertIn("Fuel level", names)
        self.assertIn("Fuel time remaining", names)
        self.assertIn("Tank filled", names)
        self.assertIn("Debug log", names)
        self.assertIn("Adaptive coolant control", names)
        self.assertIn("Reset adaptive learning", names)
        self.assertIn("Adaptive control status", names)
        self.assertFalse(any(item.get("name", "").startswith(("Status byte", "Extended byte")) for item in decoded))
        self.assertNotIn("Extended frame hex", names)
        removed = {topic for topic, payload in discovery_messages() if payload == ""}
        self.assertIn("homeassistant/sensor/autoterm_flow5/ext_byte_7/config", removed)
        self.assertIn("homeassistant/sensor/autoterm_flow5/ext_hex/config", removed)
        self.assertIn("autoterm_flow5/state/ext_byte_7", removed)

    def test_raw_bytes_are_not_published(self):
        updates = mqtt_state_updates({"ext_byte_7": "176", "ext_hex": "0001", "status_byte_5": "1", "room": "16.10"})
        self.assertEqual(updates, {"room": "16.10"})

    def test_coolant_limit_command_is_clamped_and_kept(self):
        settings = Settings()
        controller = Controller(settings)
        apply_command(controller, settings, "autoterm_flow5/set/debug", "ON", 0)
        self.assertTrue(settings.debug)
        apply_command(controller, settings, "autoterm_flow5/set/debug", "OFF", 1)
        self.assertFalse(settings.debug)
        apply_command(controller, settings, "autoterm_flow5/set/coolant_limit", "70", 0)
        self.assertEqual(settings.coolant_limit, 70)
        self.assertEqual(controller.settings.coolant_limit, 70)
        apply_command(controller, settings, "autoterm_flow5/set/coolant_limit", "120", 1)
        self.assertEqual(settings.coolant_limit, 95)

    def test_the_card_keeps_heating_on_across_a_restart(self):
        import tempfile
        from pathlib import Path

        settings = Settings()
        controller = Controller(settings)
        apply_command(controller, settings, "autoterm_flow5/set/climate_mode", "heat", 0)
        self.assertTrue(settings.heat_on)
        self.assertEqual(settings.control_mode, "thermostat")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            settings.save(path)
            self.assertTrue(Settings.load(path).heat_on)
            apply_command(controller, settings, "autoterm_flow5/set/climate_mode", "off", 1)
            settings.save(path)
            self.assertFalse(Settings.load(path).heat_on)

    def test_the_card_shows_heating_idle_and_off(self):
        from test_controller import status

        settings = Settings()
        controller = Controller(settings)

        def action():
            return collect_state(
                None,
                None,
                fuel=FuelMeter(),
                settings=settings,
                controller=controller,
                room_c=None,
                online=True,
                telemetry_stale=False,
                fan_on=False,
                frequency_hz=0,
            )["climate_action"]

        self.assertEqual(action(), "off")
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 15, 0)
        self.assertEqual(action(), "idle")
        controller.tick(5, status(3, 2, coolant=50), None, 15, 0)
        self.assertEqual(action(), "heating")
        decoded = [json.loads(payload) for _topic, payload in discovery_messages() if payload]
        climate = next(item for item in decoded if item.get("unique_id", "").endswith("_climate"))
        self.assertEqual(climate["action_topic"], "autoterm_flow5/state/climate_action")

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

    def test_coolant_follows_the_liquid_sensor_when_status_is_absent(self):
        raw = bytearray(58)
        raw[19] = 42
        extended = decode_extended(bytes(raw))
        settings = Settings()
        values = collect_state(
            None,
            extended,
            fuel=FuelMeter(),
            settings=settings,
            controller=Controller(settings),
            room_c=6.8,
            online=True,
            telemetry_stale=False,
            fan_on=False,
            frequency_hz=4.2,
        )
        self.assertEqual(values["coolant"], "42.00")
        self.assertEqual(values["liquid"], "42")
        self.assertEqual(values["coolant_a"], "")
        published: dict[str, str] = {}
        first = mqtt_state_updates(values, published)
        self.assertEqual(first["coolant"], "42.00")
        self.assertNotIn("coolant_a", first)
        self.assertEqual(mqtt_state_updates(values, published), {})


if __name__ == "__main__":
    unittest.main()
