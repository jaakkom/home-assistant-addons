import tempfile
import unittest
from pathlib import Path

from autoterm_flow5.faults import ChangeNotifier, fault_name
from autoterm_flow5.fuel import FuelMeter, liters_per_hour


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


class FaultTests(unittest.TestCase):
    def test_known_names(self):
        self.assertEqual(fault_name(0), "Ei vikaa")
        self.assertEqual(fault_name(1), "Ylikuumeneminen")
        self.assertEqual(fault_name(25), "Liian nopea lämmönnousu")
        self.assertEqual(fault_name(78), "Tarkista polttoainejärjestelmä")
        self.assertEqual(fault_name(99), "Vikakoodi 99")

    def test_notice_only_when_code_changes(self):
        notifier = ChangeNotifier("autoterm_flow5_fault", "Autoterm Flow 5")
        self.assertIsNone(notifier.update(0))
        first = notifier.update(14)
        self.assertEqual(first.action, "create")
        self.assertIn("pumppu", first.message.lower())
        self.assertIsNone(notifier.update(14))
        changed = notifier.update(78)
        self.assertEqual(changed.action, "create")
        self.assertEqual(changed.code, 78)
        cleared = notifier.update(0)
        self.assertEqual(cleared.action, "dismiss")
        self.assertIsNone(notifier.update(0))


if __name__ == "__main__":
    unittest.main()
