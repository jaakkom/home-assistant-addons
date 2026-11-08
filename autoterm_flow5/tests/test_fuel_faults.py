import tempfile
import unittest
from pathlib import Path

from autoterm_flow5.faults import ChangeNotifier, fault_name
from autoterm_flow5.fuel import FuelMeter, liters_per_hour
from autoterm_flow5.settings import settings_from_options


class FuelTests(unittest.TestCase):
    def test_flow5_high_rate(self):
        self.assertAlmostEqual(liters_per_hour(3.8, 0.044), 0.602, places=3)

    def test_two_second_integration(self):
        meter = FuelMeter()
        meter.update(0, 2.0, 0.044)
        added = meter.update(2, 2.0, 0.044)
        self.assertAlmostEqual(added, 0.176, places=6)
        self.assertAlmostEqual(meter.total_ml, 0.176, places=6)

    def test_gap_over_five_seconds_adds_nothing(self):
        meter = FuelMeter()
        meter.update(0, 2.0, 0.044)
        added = meter.update(6, 2.0, 0.044)
        self.assertEqual(added, 0)
        self.assertEqual(meter.total_ml, 0)

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fuel.json"
            meter = FuelMeter(path)
            meter.update(0, 2.0, 0.044)
            meter.update(2, 2.0, 0.044)
            meter.save()
            restored = FuelMeter(path)
            self.assertAlmostEqual(restored.total_ml, 0.176, places=6)
            restored.reset()
            self.assertEqual(FuelMeter(path).total_ml, 0)

    def test_tank_level_percent_and_runtime(self):
        meter = FuelMeter()
        self.assertIsNone(meter.level_percent(10))
        self.assertIsNone(meter.hours_remaining())
        self.assertFalse(meter.refuel(0))
        self.assertTrue(meter.refuel(10))
        self.assertEqual(meter.remaining_ml, 10000)
        self.assertEqual(meter.level_percent(10), 100)
        meter.update(0, 2.0, 0.044)
        self.assertIsNone(meter.hours_remaining())
        meter.update(2, 0.0, 0.044)
        self.assertAlmostEqual(meter.remaining_ml, 10000 - 0.176, places=6)
        self.assertAlmostEqual(meter.level_percent(10), 99.99824, places=4)
        self.assertAlmostEqual(meter.average_lph(), liters_per_hour(2, 0.044), places=4)
        self.assertAlmostEqual(meter.hours_remaining(), meter.remaining_liters / liters_per_hour(2, 0.044), places=4)

    def test_gap_does_not_reduce_the_tank(self):
        meter = FuelMeter()
        meter.refuel(10)
        meter.update(0, 2.0, 0.044)
        meter.update(6, 2.0, 0.044)
        self.assertEqual(meter.remaining_ml, 10000)

    def test_partial_fill_and_capacity_cap(self):
        meter = FuelMeter()
        meter.set_level_liters(12, 10)
        self.assertEqual(meter.remaining_ml, 10000)
        meter.set_level_liters(4, 10)
        self.assertEqual(meter.remaining_ml, 4000)
        self.assertTrue(meter.clamp_to_capacity(3))
        self.assertEqual(meter.remaining_ml, 3000)
        self.assertEqual(meter.level_percent(3), 100)

    def test_total_reset_keeps_the_tank_level(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fuel.json"
            meter = FuelMeter(path)
            meter.refuel(10)
            meter.update(0, 2.0, 0.044)
            meter.update(2, 2.0, 0.044)
            meter.reset()
            restored = FuelMeter(path)
            self.assertEqual(restored.total_ml, 0)
            self.assertAlmostEqual(restored.remaining_ml, 10000 - 0.176, places=6)
            self.assertAlmostEqual(restored.average_lph(), liters_per_hour(2, 0.044), places=4)

    def test_hour_average_includes_idle_and_drops_old_use(self):
        meter = FuelMeter()
        meter.refuel(10)
        meter.update(0, 2.0, 0.044)
        meter.update(4, 0.0, 0.044)
        burn = liters_per_hour(2, 0.044)
        self.assertAlmostEqual(meter.average_lph(), burn, places=4)
        meter.update(8, 0.0, 0.044)
        self.assertAlmostEqual(meter.average_lph(), burn / 2, places=4)
        self.assertAlmostEqual(meter.hours_remaining(), meter.remaining_liters / (burn / 2), places=4)
        meter.update(3604, 0.0, 0.044)
        meter.update(3608, 0.0, 0.044)
        self.assertEqual(meter.average_lph(), 0.0)
        self.assertIsNone(meter.hours_remaining())

    def test_old_fuel_file_has_no_level(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fuel.json"
            path.write_text('{"total_ml": 12}', encoding="utf-8")
            restored = FuelMeter(path)
            self.assertEqual(restored.total_ml, 12)
            self.assertIsNone(restored.remaining_ml)

    def test_options_read_tank_capacity(self):
        settings = settings_from_options({"fuel": {"tank_capacity_l": 42}})
        self.assertEqual(settings.tank_capacity_l, 42)


class FaultTests(unittest.TestCase):
    def test_known_names(self):
        self.assertEqual(fault_name(0), "No fault")
        self.assertEqual(fault_name(1), "Overheating")
        self.assertEqual(fault_name(25), "Temperature rising too fast")
        self.assertEqual(fault_name(78), "Check the fuel system")
        self.assertEqual(fault_name(99), "Fault code 99")

    def test_notice_only_when_code_changes(self):
        notifier = ChangeNotifier("autoterm_flow5_fault", "Autoterm Flow 5")
        self.assertIsNone(notifier.update(0))
        first = notifier.update(14)
        self.assertEqual(first.action, "create")
        self.assertIn("pump", first.message.lower())
        self.assertIsNone(notifier.update(14))
        changed = notifier.update(78)
        self.assertEqual(changed.action, "create")
        self.assertEqual(changed.code, 78)
        cleared = notifier.update(0)
        self.assertEqual(cleared.action, "dismiss")
        self.assertIsNone(notifier.update(0))


if __name__ == "__main__":
    unittest.main()
