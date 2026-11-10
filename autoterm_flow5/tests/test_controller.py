import unittest

from autoterm_flow5.controller import Controller, aggregate
from autoterm_flow5.protocol import (
    FACTORY_SETTINGS,
    decode_extended,
    decode_heater_settings,
    decode_status,
    cabin_temperature,
    requested_temperature,
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

    def test_a_user_change_is_written_onto_the_block_that_was_read(self):
        controller = Controller(self.settings())
        block = decode_heater_settings(FACTORY_SETTINGS)
        controller.accept_device_settings(block, 1.0, "idle")
        controller.settings.coolant_limit = 40
        controller.note_device_setting("coolant_limit")
        written = controller.tick(2, status(), None, None, None)
        self.assertEqual(written.commands[0][5], 40)
        self.assertEqual(written.commands[0][6:-2], FACTORY_SETTINGS[1:])
        stored = bytearray(FACTORY_SETTINGS)
        stored[0] = 40
        self.assertFalse(
            controller.accept_device_settings(decode_heater_settings(bytes(stored)), 3.0, "idle")
        )
        self.assertFalse(controller._settings_pending)
        self.assertFalse(controller._settings_confirm)

    def test_an_unread_heater_replaces_the_local_standby_temperature(self):
        controller = Controller(self.settings(coolant_limit=70))
        block = decode_heater_settings(FACTORY_SETTINGS)
        self.assertTrue(controller.accept_device_settings(block, 1.0, "idle"))
        self.assertEqual(controller.settings.coolant_limit, 88)
        self.assertEqual(controller.settings.fan_temperature, 40)

    def test_preheat_does_not_stop_at_the_standby_temperature(self):
        controller = Controller(self.settings(coolant_limit=40, preheat_minutes=30))
        controller.set_mode("preheat")
        controller.set_heat(True, 0)
        controller.tick(0, status(coolant=30), None, None, None)
        running = controller.tick(1, status(3, 2, coolant=50), None, None, None)
        self.assertNotIn(stop_command(), commands_of(running))

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
        self.assertIn(requested_temperature(20), commands_of(held))
        self.assertNotIn(stop_command(), commands_of(held))
        again = controller.tick(3, status(3, 2, coolant=50), None, 15, 0)
        self.assertNotIn(requested_temperature(20), commands_of(again))

    def test_room_temperature_is_sent_when_heating_is_on(self):
        controller = Controller(self.settings())
        controller.accept_device_settings(decode_heater_settings(FACTORY_SETTINGS), 0.0, "idle")
        controller.set_heat(True, 0)
        output = controller.tick(0, status(), None, 18.4, 0)
        self.assertEqual(commands_of(output)[:2], [start_thermostat(), cabin_temperature(18)])
        held = controller.tick(1, status(3, 2, coolant=50), None, 18.4, 0)
        self.assertNotIn(cabin_temperature(18), commands_of(held))
        again = controller.tick(31, status(3, 2, coolant=50), None, 19.2, 0)
        self.assertEqual(commands_of(again), [cabin_temperature(19)])

    def test_switching_on_starts_before_the_room_reaches_the_drop(self):
        controller = Controller(self.settings())
        controller.set_heat(True, 0)
        output = controller.tick(0, status(), None, 19, 0)
        self.assertIn(start_thermostat(), commands_of(output))
        self.assertTrue(output.demand)

    def test_switching_on_starts_even_when_the_room_is_already_warm(self):
        controller = Controller(self.settings())
        controller.set_heat(True, 0)
        output = controller.tick(0, status(), None, 22, 0)
        self.assertIn(start_thermostat(), commands_of(output))

    def test_missing_sensor_still_starts_thermostat_mode(self):
        controller = Controller(self.settings())
        controller.set_heat(True, 0)
        output = controller.tick(0, status(), None, None, None)
        self.assertIn(start_thermostat(), commands_of(output))

    def test_coldest_sensor(self):
        self.assertEqual(aggregate([21, 18.5, 19], "min"), 18.5)
        self.assertEqual(aggregate([21, 18.5, 19], "max"), 21)
        self.assertAlmostEqual(aggregate([10, 20], "avg"), 15)

    def test_a_warm_room_does_not_stop_thermostat_mode(self):
        controller = Controller(self.settings())
        controller.set_heat(True, 0)
        started = controller.tick(0, status(), None, 15, 0)
        self.assertIn(start_thermostat(), commands_of(started))
        warm = controller.tick(600, status(3, 2, coolant=50), None, 25, 0)
        self.assertNotIn(stop_command(), commands_of(warm))
        idle = controller.tick(601, status(), None, 15, 0)
        self.assertIn(start_thermostat(), commands_of(idle))

    def test_thermostat_mode_does_not_stop_for_coolant_or_voltage(self):
        controller = Controller(self.settings(shutdown_voltage=12, shutdown_voltage_s=0))
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 15, 0)
        hot = controller.tick(1, status(3, 2, coolant=95), extended(11), 15, 0)
        self.assertNotIn(stop_command(), commands_of(hot))

    def test_the_heater_is_not_restarted_after_it_stops_itself(self):
        controller = Controller(self.settings())
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 15, 0)
        controller.tick(5, status(3, 2, coolant=50), None, 15, 0)
        idle = controller.tick(6, status(fault=25, coolant=70), None, 15, 0)
        self.assertNotIn(start_thermostat(), commands_of(idle))
        self.assertNotIn(stop_command(), commands_of(idle))

    def test_overheat_does_not_make_the_add_on_stop_thermostat_mode(self):
        controller = Controller(self.settings())
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 15, 0)
        tripped = controller.tick(1, status(3, 2, fault=1, coolant=90), None, 15, 0)
        self.assertFalse(tripped.lockout)
        self.assertNotIn(stop_command(), commands_of(tripped))

    def test_a_stale_room_sensor_does_not_stop_thermostat_mode(self):
        controller = Controller(self.settings(sensor_stale_s=900))
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 15, 0)
        running = controller.tick(30, status(3, 2, coolant=50), None, 15, 901)
        self.assertNotIn(stop_command(), commands_of(running))

    def test_preheat_and_pump(self):
        controller = Controller(self.settings(preheat_minutes=30))
        controller.accept_device_settings(decode_heater_settings(FACTORY_SETTINGS), 0.0, "idle")
        controller.set_mode("preheat")
        controller.set_heat(True, 0)
        output = controller.tick(0, status(), None, None, None)
        self.assertEqual(output.commands, start_preheat(30))
        pump = Controller(self.settings())
        pump.accept_device_settings(decode_heater_settings(FACTORY_SETTINGS), 0.0, "idle")
        pump.set_mode("pump")
        pump.set_heat(True, 0)
        first = pump.tick(0, status(), None, None, None)
        second = pump.tick(1, status(), None, None, None)
        self.assertEqual(first.commands, [start_pump()])
        self.assertEqual(second.commands, [])

    def test_low_voltage_does_not_stop_thermostat_mode(self):
        controller = Controller(self.settings(shutdown_voltage=12, shutdown_voltage_s=30))
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 15, 0)
        controller.tick(1, status(3, 2, coolant=50), extended(11), 15, 0)
        still = controller.tick(31, status(3, 2, coolant=50), extended(11), 15, 0)
        self.assertNotIn(stop_command(), commands_of(still))

    def test_a_faultless_stop_sends_the_request_again(self):
        controller = Controller(self.settings(min_off_s=600))
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 15, 0)
        controller.tick(5, status(3, 2, coolant=50), None, 15, 0)
        idle = controller.tick(6, status(coolant=50), None, 15, 0)
        self.assertIn(start_thermostat(), commands_of(idle))

    def test_the_request_is_repeated_while_heating_stays_on(self):
        controller = Controller(self.settings())
        controller.set_heat(True, 0)
        controller.tick(0, status(3, 2, coolant=40), None, 15, 0)
        held = controller.tick(30, status(3, 2, coolant=40), None, 15, 0)
        self.assertNotIn(requested_temperature(20), commands_of(held))
        again = controller.tick(60, status(3, 2, coolant=40), None, 15, 0)
        self.assertIn(requested_temperature(20), commands_of(again))

    def test_the_thermostat_card_updates_a_running_heater(self):
        controller = Controller(self.settings(room_target=18))
        controller.set_heat(True, 0)
        running = controller.tick(1, status(3, 2, coolant=50), None, 17, 0)
        self.assertIn(requested_temperature(18), commands_of(running))
        controller.settings.room_target = 22
        changed = controller.tick(2, status(3, 2, coolant=50), None, 17, 0)
        self.assertEqual(commands_of(changed), [requested_temperature(22)])
        held = controller.tick(3, status(3, 2, coolant=50), None, 17, 0)
        self.assertNotIn(requested_temperature(22), commands_of(held))
        controller.settings.room_target = 22
        idle = controller.tick(4, status(), None, 17, 0)
        self.assertIn(requested_temperature(22), commands_of(idle))

    def test_switching_on_again_sends_start(self):
        controller = Controller(self.settings())
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 15, 0)
        controller.tick(5, status(3, 2, coolant=50), None, 15, 0)
        controller.tick(6, status(coolant=50), None, 15, 0)
        controller.set_heat(False, 7)
        controller.set_heat(True, 8)
        again = controller.tick(8, status(coolant=40), None, 15, 0)
        self.assertIn(start_thermostat(), commands_of(again))

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
        held = controller.tick(7, status(3, 2, coolant=50), None, 15, 0)
        self.assertNotIn(stop_command(), commands_of(held))
        retry = controller.tick(11, status(3, 2, coolant=50), None, 15, 0)
        self.assertIn(stop_command(), commands_of(retry))
        idle = controller.tick(12, status(coolant=50), None, 15, 0)
        self.assertNotIn(start_thermostat(), commands_of(idle))
        self.assertNotIn(stop_command(), commands_of(idle))

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
