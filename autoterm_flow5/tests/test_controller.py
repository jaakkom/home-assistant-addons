import unittest

from autoterm_flow5.controller import Controller, aggregate
from autoterm_flow5.protocol import (
    decode_extended,
    decode_status,
    start_preheat,
    start_pump,
    start_thermostat,
    stop_command,
)
from autoterm_flow5.settings import Settings


def status(state=0, substate=1, fault=0, coolant=40):
    raw = bytearray(18)
    raw[0] = state
    raw[1] = substate
    raw[2] = fault
    raw[3] = coolant
    raw[4] = coolant
    raw[12] = 0xFF if state in (2, 3) else 0
    return decode_status(bytes(raw))


def extended(voltage=12.5, fault=0):
    raw = bytearray(58)
    raw[0] = 3
    raw[1] = 2
    whole = int(voltage * 10)
    raw[22] = (whole >> 8) & 0xFF
    raw[23] = whole & 0xFF
    raw[36] = fault
    return decode_extended(bytes(raw))


def commands_of(output):
    return output.commands


class ControllerTests(unittest.TestCase):
    def settings(self, **kwargs):
        base = dict(
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
        )
        base.update(kwargs)
        return Settings(**base)

    def test_boot_does_not_start(self):
        controller = Controller(self.settings())
        output = controller.tick(0, status(), None, 10, 0)
        self.assertNotIn(start_thermostat(), commands_of(output))
        self.assertFalse(output.heat_enabled)

    def test_restart_does_not_stop_or_restart_a_running_heater(self):
        controller = Controller(self.settings())
        running = controller.tick(0, status(3, 2, coolant=50), None, 15, 0)
        self.assertNotIn(stop_command(), commands_of(running))
        self.assertNotIn(start_thermostat(), commands_of(running))
        ignition = controller.tick(1, status(2, 1, coolant=40), None, 15, 0)
        self.assertNotIn(stop_command(), commands_of(ignition))
        self.assertNotIn(start_thermostat(), commands_of(ignition))
        controller.set_heat(True, 2)
        held = controller.tick(2, status(3, 2, coolant=50), None, 15, 0)
        self.assertNotIn(start_thermostat(), commands_of(held))
        self.assertNotIn(stop_command(), commands_of(held))

    def test_missing_sensor_does_not_start(self):
        controller = Controller(self.settings())
        controller.set_heat(True, 0)
        output = controller.tick(0, status(), None, None, None)
        self.assertNotIn(start_thermostat(), commands_of(output))

    def test_coldest_sensor(self):
        self.assertEqual(aggregate([21, 18.5, 19], "min"), 18.5)
        self.assertEqual(aggregate([21, 18.5, 19], "max"), 21)
        self.assertAlmostEqual(aggregate([10, 20], "avg"), 15)

    def test_hysteresis_and_minimum_times(self):
        controller = Controller(self.settings())
        controller.set_heat(True, 0)
        started = controller.tick(0, status(), None, 15, 0)
        self.assertIn(start_thermostat(), commands_of(started))
        self.assertTrue(started.demand)
        held = controller.tick(1, status(3, 2, coolant=50), None, 19, 0)
        self.assertNotIn(stop_command(), commands_of(held))
        early = controller.tick(30, status(3, 2, coolant=50), None, 25, 0)
        self.assertNotIn(stop_command(), commands_of(early))
        self.assertFalse(early.demand)
        stopped = controller.tick(600, status(3, 2, coolant=50), None, 25, 0)
        self.assertIn(stop_command(), commands_of(stopped))
        blocked = controller.tick(601, status(), None, 15, 0)
        self.assertNotIn(start_thermostat(), commands_of(blocked))
        again = controller.tick(1200, status(), None, 15, 0)
        self.assertIn(start_thermostat(), commands_of(again))

    def test_coolant_limit_and_drop(self):
        controller = Controller(self.settings())
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 15, 0)
        limited = controller.tick(1, status(3, 2, coolant=88), None, 15, 0)
        self.assertIn(stop_command(), commands_of(limited))
        waiting = controller.tick(2, status(coolant=80), None, 15, 0)
        self.assertNotIn(start_thermostat(), commands_of(waiting))
        ready = controller.tick(3, status(coolant=70), None, 15, 0)
        self.assertIn(start_thermostat(), commands_of(ready))

    def test_fault_25_restarts_without_minimum_off(self):
        controller = Controller(self.settings())
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 15, 0)
        controller.tick(1, status(3, 2, fault=25, coolant=70), None, 15, 0)
        restarted = controller.tick(2, status(fault=25, coolant=70), None, 15, 0)
        self.assertIn(start_thermostat(), commands_of(restarted))

    def test_overheat_requires_user_toggle(self):
        controller = Controller(self.settings())
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 15, 0)
        tripped = controller.tick(1, status(3, 2, fault=1, coolant=90), None, 15, 0)
        self.assertTrue(tripped.lockout)
        self.assertIn(stop_command(), commands_of(tripped))
        quiet = controller.tick(2, status(fault=0, coolant=40), None, 15, 0)
        self.assertNotIn(start_thermostat(), commands_of(quiet))
        controller.set_heat(False, 3)
        controller.set_heat(True, 4)
        restarted = controller.tick(5, status(coolant=40), None, 15, 0)
        self.assertIn(start_thermostat(), commands_of(restarted))

    def test_stale_sensor_stops_a_run(self):
        controller = Controller(self.settings(sensor_stale_s=900))
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 15, 0)
        stopped = controller.tick(30, status(3, 2, coolant=50), None, 15, 901)
        self.assertIn(stop_command(), commands_of(stopped))

    def test_preheat_and_pump(self):
        controller = Controller(self.settings(preheat_minutes=30))
        controller.set_mode("preheat")
        controller.set_heat(True, 0)
        output = controller.tick(0, status(), None, None, None)
        self.assertEqual(output.commands, start_preheat(30))
        pump = Controller(self.settings())
        pump.set_mode("pump")
        pump.set_heat(True, 0)
        first = pump.tick(0, status(), None, None, None)
        second = pump.tick(1, status(), None, None, None)
        self.assertEqual(first.commands, [start_pump()])
        self.assertEqual(second.commands, [])

    def test_low_voltage_stop(self):
        controller = Controller(self.settings(shutdown_voltage=12, shutdown_voltage_s=30))
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 15, 0)
        controller.tick(1, status(3, 2, coolant=50), extended(11), 15, 0)
        stopped = controller.tick(31, status(3, 2, coolant=50), extended(11), 15, 0)
        self.assertIn(stop_command(), commands_of(stopped))

    def test_unexpected_shutdown_without_a_fault_restarts_immediately(self):
        controller = Controller(self.settings(min_off_s=600))
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 15, 0)
        controller.tick(5, status(3, 2, coolant=50), None, 15, 0)
        restarted = controller.tick(6, status(coolant=50), None, 15, 0)
        self.assertIn(start_thermostat(), commands_of(restarted))
        self.assertTrue(restarted.demand)

    def test_unexpected_stop_at_the_coolant_limit_waits_for_the_drop(self):
        controller = Controller(self.settings())
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 15, 0)
        controller.tick(20, status(3, 2, coolant=90), None, 15, 0)
        waiting = controller.tick(21, status(coolant=90), None, 15, 0)
        self.assertNotIn(start_thermostat(), commands_of(waiting))
        ready = controller.tick(22, status(coolant=70), None, 15, 0)
        self.assertIn(start_thermostat(), commands_of(ready))

    def test_fault_shutdown_is_not_restarted_as_interference(self):
        controller = Controller(self.settings())
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 15, 0)
        controller.tick(20, status(3, 2, fault=14, coolant=50), None, 15, 0)
        idle = controller.tick(21, status(fault=14, coolant=50), None, 15, 0)
        self.assertNotIn(start_thermostat(), commands_of(idle))

    def test_user_stop_is_not_restarted(self):
        controller = Controller(self.settings())
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 15, 0)
        controller.tick(5, status(3, 2, coolant=50), None, 15, 0)
        controller.set_heat(False, 6)
        stopped = controller.tick(6, status(3, 2, coolant=50), None, 15, 0)
        self.assertIn(stop_command(), commands_of(stopped))
        idle = controller.tick(7, status(coolant=50), None, 15, 0)
        self.assertNotIn(start_thermostat(), commands_of(idle))

    def test_preheat_and_pump_restart_after_an_unexpected_shutdown(self):
        preheat = Controller(self.settings(preheat_minutes=30))
        preheat.set_mode("preheat")
        preheat.set_heat(True, 0)
        preheat.tick(0, status(), None, None, None)
        preheat.tick(5, status(3, 2, coolant=50), None, None, None)
        again = preheat.tick(6, status(coolant=50), None, None, None)
        self.assertEqual(again.commands, start_preheat(30))

        pump = Controller(self.settings())
        pump.set_mode("pump")
        pump.set_heat(True, 0)
        pump.tick(0, status(), None, None, None)
        pump.tick(5, status(3, 7, coolant=40), None, None, None)
        pumped = pump.tick(6, status(coolant=40), None, None, None)
        self.assertEqual(pumped.commands, [start_pump()])

    def test_fan_request(self):
        controller = Controller(self.settings(fan_temperature=40))
        output = controller.tick(0, status(coolant=40), None, None, None)
        self.assertTrue(output.fan_on)
        cool = controller.tick(1, status(coolant=39), None, None, None)
        self.assertFalse(cool.fan_on)


if __name__ == "__main__":
    unittest.main()
