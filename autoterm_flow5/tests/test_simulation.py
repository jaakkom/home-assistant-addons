"""Run the real control loop against the in-process heater."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autoterm_flow5.controller import Controller
from autoterm_flow5.faults import ChangeNotifier, LinkNotifier
from autoterm_flow5.fuel import FuelMeter
from autoterm_flow5.link import SerialLink
from autoterm_flow5.main import _room, open_link, step
from autoterm_flow5.mqtt_bridge import PREFIX, apply_command, discovery_messages
from autoterm_flow5.settings import Settings, load_settings, settings_from_options
from autoterm_flow5.simulator import SimulatedHeater


class OpenLinkTests(unittest.TestCase):
    def test_simulation_does_not_open_a_serial_port(self):
        settings = Settings(simulate=True)
        with mock.patch("autoterm_flow5.main.open_port", side_effect=AssertionError("uart")):
            link = open_link(settings, "/dev/ttyUSB0", 2400)
        self.assertIsInstance(link.port, SimulatedHeater)

    def test_hardware_mode_opens_the_configured_port(self):
        settings = Settings(simulate=False)
        port = mock.Mock()
        with mock.patch("autoterm_flow5.main.open_port", return_value=port) as open_port:
            link = open_link(settings, "/dev/ttyUSB0", 2400)
        open_port.assert_called_once_with("/dev/ttyUSB0", 2400)
        self.assertIs(link.port, port)

    def test_options_seed_the_simulation_settings(self):
        settings = settings_from_options(
            {"simulate": True, "simulate_room_temperature": 18, "simulate_fault_code": 14}
        )
        self.assertTrue(settings.simulate)
        self.assertEqual(settings.simulate_room_c, 18)
        self.assertEqual(settings.simulate_fault, 14)

    def test_grouped_options_override_the_flat_file(self):
        settings = settings_from_options(
            {
                "simulate": False,
                "simulation": {"simulate": True, "simulate_room_temperature": 11, "simulate_fault_code": 0},
                "temperature": {"room_target": 19, "aggregation": "avg"},
                "protection": {"coolant_limit": 80},
            }
        )
        self.assertTrue(settings.simulate)
        self.assertEqual(settings.simulate_room_c, 11)
        self.assertEqual(settings.room_target, 19)
        self.assertEqual(settings.aggregation, "avg")
        self.assertEqual(settings.coolant_limit, 80)

    def test_configuration_page_turns_saved_simulation_off(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            Settings(simulate=True, room_target=22).save(path)
            loaded = load_settings(path, {"simulation": {"simulate": False}})
            self.assertFalse(loaded.simulate)
            self.assertEqual(loaded.room_target, 22)
            loaded.save(path)
            self.assertFalse(Settings.load(path).simulate)

    def test_configuration_page_turns_saved_simulation_on(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            Settings(simulate=False).save(path)
            loaded = load_settings(path, {"simulate": "true"})
            self.assertTrue(loaded.simulate)

    def test_configuration_page_turns_saved_debug_off(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            Settings(debug=True, room_target=22).save(path)
            loaded = load_settings(path, {"device": {"debug": False}})
            self.assertFalse(loaded.debug)
            self.assertEqual(loaded.room_target, 22)

    def test_missing_simulation_option_keeps_the_saved_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            Settings(simulate=True).save(path)
            self.assertTrue(load_settings(path, {}).simulate)

    def test_simulated_room_is_used_when_no_entities_are_set(self):
        settings = Settings(simulate=True, simulate_room_c=16, temperature_entities="")
        room, last, seen_at = _room(settings, "", 1_000, None, None)
        self.assertEqual((room, last, seen_at), (16, 16, 1_000))


class SimulationLoopTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            simulate=True,
            simulate_room_c=15,
            room_target=20,
            room_drop=2,
            room_rise=1,
            min_on_s=600,
            min_off_s=600,
            sensor_stale_s=900,
            coolant_limit=88,
            coolant_drop=18,
            shutdown_voltage=11.5,
            shutdown_voltage_s=30,
            fan_temperature=40,
            pump_dose_ml=0.044,
            extended_telemetry=True,
        )
        self.controller = Controller(self.settings)
        self.heater = SimulatedHeater()
        self.link = SerialLink(self.heater, True)
        self.fuel = FuelMeter(None)
        self.faults = ChangeNotifier("autoterm_flow5_fault", "Autoterm Flow 5")
        self.link_notice = LinkNotifier()
        self.now = 1_700_000_000.0

    def cycle(self, room=None, age=0.0):
        if room is None:
            room = self.settings.simulate_room_c
        result = step(
            self.now,
            self.link,
            self.controller,
            self.settings,
            self.fuel,
            self.faults,
            self.link_notice,
            room,
            age,
        )
        self.now += 1
        return result

    def test_boot_stays_off_until_heat_is_turned_on(self):
        result = None
        for _ in range(3):
            result = self.cycle()
        self.assertNotIn("start", self.heater.events)
        self.assertEqual(result.values["simulation"], "ON")
        self.assertEqual(result.values["link"], "ON")
        self.assertEqual(result.values["power"], "OFF")
        self.assertEqual(result.values["mode"], "Waiting for a command")
        self.assertEqual(result.values["telemetry_stale"], "OFF")

    def test_cold_room_starts_and_publishes_fuel_and_fan(self):
        self.cycle()
        apply_command(self.controller, self.settings, f"{PREFIX}/set/power", "ON", self.now)
        apply_command(self.controller, self.settings, f"{PREFIX}/set/simulate_room", "12", self.now)
        self.assertEqual(self.settings.simulate_room_c, 12)
        result = self.cycle()
        self.assertIn("start", self.heater.events)
        self.assertEqual(result.values["mode"], "High")
        self.assertEqual(result.values["phase"], "running")
        self.assertEqual(result.values["burner"], "ON")
        self.assertEqual(result.values["climate_mode"], "heat")
        self.assertAlmostEqual(float(result.values["pump_hz"]), 3.8)
        self.assertAlmostEqual(float(result.values["fuel_lph"]), 0.6019, places=3)
        self.assertAlmostEqual(float(result.values["voltage"]), 12.6)
        later = self.cycle()
        self.assertGreater(float(later.values["fuel_total_ml"]), 0)
        self.assertEqual(later.values["fan_request"], "ON")

    def test_coolant_limit_does_not_stop_thermostat_mode(self):
        self.controller.set_heat(True, self.now)
        for _ in range(40):
            self.cycle()
        self.assertNotIn("stop", self.heater.events)
        self.assertEqual(self.heater.events.count("start"), 1)
        self.assertIn("cabin", self.heater.events)

    def test_room_temperature_does_not_stop_the_heater(self):
        self.settings.coolant_limit = 95
        self.controller.settings = self.settings
        self.cycle()
        self.controller.set_heat(True, self.now)
        self.cycle()
        for _ in range(3):
            self.cycle(room=30)
        self.assertNotIn("stop", self.heater.events)
        self.now += 600
        result = self.cycle(room=30)
        self.assertNotIn("stop", self.heater.events)
        self.assertEqual(result.values["burner"], "ON")
        self.assertIn("cabin", self.heater.events)

    def test_fault_notice_is_sent_once(self):
        self.controller.set_heat(True, self.now)
        self.cycle()
        self.settings.simulate_fault = 14
        reported = self.cycle()
        self.assertEqual(
            [notice.action for notice in reported.notices if notice.code == 14],
            ["create"],
        )
        self.assertEqual(reported.values["fault_text"], "Coolant pump or circuit")
        self.assertEqual(reported.values["fault_active"], "ON")
        self.assertEqual(self.cycle().notices, [])
        self.settings.simulate_fault = 0
        cleared = self.cycle()
        self.assertEqual([notice.action for notice in cleared.notices], ["dismiss"])
        self.assertEqual(cleared.values["fault_text"], "No fault")

    def test_overheat_is_reported_and_the_heater_is_left_running(self):
        self.settings.coolant_limit = 95
        self.controller.settings = self.settings
        self.controller.set_heat(True, self.now)
        self.cycle()
        self.settings.simulate_fault = 1
        reported = self.cycle()
        self.assertEqual(reported.values["fault_text"], "Overheating")
        locked = self.cycle()
        self.assertEqual(locked.values["lockout"], "OFF")
        self.assertNotIn("stop", self.heater.events)
        self.settings.simulate_fault = 0
        for _ in range(4):
            self.cycle()
        self.assertEqual(self.heater.events.count("start"), 1)
        self.controller.set_heat(False, self.now)
        self.cycle()
        self.assertIn("stop", self.heater.events)
        self.controller.set_heat(True, self.now)
        self.cycle()
        self.assertEqual(self.heater.events.count("start"), 2)

    def test_silent_heater_reports_link_loss_and_recovery(self):
        self.cycle()
        self.heater.silent = True
        notices = []
        offline = None
        for _ in range(20):
            result = self.cycle()
            notices.extend(result.notices)
            if result.values["link"] == "OFF":
                offline = result
                break
        self.assertIsNotNone(offline)
        self.assertTrue(
            any(notice.action == "create" and notice.notification_id.endswith("_link") for notice in notices)
        )
        self.heater.silent = False
        recovered = self.cycle()
        self.assertEqual(recovered.values["link"], "ON")
        self.assertTrue(any(notice.action == "dismiss" for notice in recovered.notices))

    def test_an_idle_return_is_not_started_again(self):
        self.settings.coolant_limit = 95
        self.controller.settings = self.settings
        self.cycle()
        self.controller.set_heat(True, self.now)
        self.cycle()
        self.assertEqual(self.heater.events.count("start"), 1)
        self.heater.burning = False
        idle = self.cycle()
        self.cycle()
        self.assertEqual(self.heater.events.count("start"), 1)
        self.assertEqual(idle.values["fault_code"], "0")
        self.assertEqual(idle.values["burner"], "OFF")

    def test_discovery_advertises_the_simulation_entities(self):
        payloads = "\n".join(payload for _topic, payload in discovery_messages())
        self.assertIn("Simulated room temperature", payloads)
        self.assertIn("Simulated fault code", payloads)
        self.assertIn("Simulation", payloads)
