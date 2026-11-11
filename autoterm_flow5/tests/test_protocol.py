import unittest

from autoterm_flow5.protocol import (
    DEV_HEATER,
    FACTORY_SETTINGS,
    PUBR0,
    TYPE_SETTINGS,
    classify,
    decode_heater_settings,
    diagnostic_enable,
    diagnostic_request,
    Framer,
    build_frame,
    crc16_modbus,
    decode_extended,
    decode_status,
    heater_settings_view,
    patch_heater_settings,
    requested_temperature,
    settings_request,
    settings_write,
    start_preheat,
    start_thermostat,
    status_request,
    stop_command,
)


class CrcTests(unittest.TestCase):
    def test_status_and_stop_vectors(self):
        self.assertEqual(status_request().hex(), "aa0300000f587c")
        self.assertEqual(diagnostic_request().hex(), "aa02000000a03d")
        self.assertEqual(diagnostic_enable().hex(), "aa03010007011d9e")
        self.assertEqual(stop_command().hex(), "aa030000035d7c")
        self.assertEqual(crc16_modbus(bytes.fromhex("aa0300000f")), 0x587C)

    def test_preheat_encoding(self):
        short = start_preheat(30)
        self.assertEqual(len(short), 2)
        self.assertEqual(short[0][5:7].hex(), "001e")
        self.assertEqual(short[1][5:7].hex(), "001e")
        long = start_preheat(120)
        self.assertEqual(len(long), 1)
        self.assertEqual(long[0][5:7].hex(), "0078")

    def test_thermostat_separates_temperature_and_run_time(self):
        frame = start_thermostat(8)
        self.assertEqual(frame[4], 0x01)
        self.assertEqual(frame[2], 6)
        # 0xFFFF is unlimited. The 8 °C setpoint is not the run-time word.
        self.assertEqual(frame[5:11].hex(), "ffff02080000")
        self.assertEqual(start_thermostat(8, 45)[5:11].hex(), "002d02080000")
        self.assertEqual(requested_temperature(34), start_thermostat(34))
        self.assertEqual(start_thermostat(80)[8], 40)
        self.assertEqual(start_thermostat(-5)[8], 0)
        self.assertEqual(start_thermostat(20, 0)[5:7].hex(), "ffff")


class FramerRepairTests(unittest.TestCase):
    def _payload(self) -> bytes:
        raw = bytearray(56)
        raw[0] = 3
        raw[1] = 2
        raw[15] = 17
        raw[17], raw[18] = 0x02, 0x10
        raw[19] = 40
        raw[20] = 55
        raw[21] = 30
        raw[22], raw[23] = 0x01, 0x22
        return bytes(raw)

    def test_one_missing_byte_is_restored_and_the_next_frame_remains(self):
        payload = self._payload()
        good = build_frame(0x02, 0x01, payload)
        short = good[:26] + good[27:]
        frames = Framer().feed(short + good)
        self.assertEqual([frame.raw for frame in frames], [good, good])

    def test_two_missing_bytes_do_not_swallow_the_next_frame(self):
        good = build_frame(0x02, 0x01, self._payload())
        short = good[:26] + good[28:]
        frames = Framer().feed(short + good)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].raw, good)

    def test_captured_frame_with_one_dropped_byte(self):
        # 10:38:43, one byte missing, the next header starts at byte 62.
        received = bytes.fromhex(
            "aa0238000100010008b1000584000584000000640000012c"
            "1f21011b0052034c0268004f0268026d0000000000000125"
            "01f201b1ff00000100000001eadc"
        )
        following = bytes.fromhex("aa0238000100010008b1")
        frames = Framer().feed(received + following)
        self.assertEqual(len(frames), 1)
        parsed = decode_extended(frames[0].payload)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.voltage, 28.3)
        self.assertEqual(parsed.pump_hz, 0)
        self.assertEqual(parsed.flame_c, 27)
        framer = Framer()
        framer.feed(received + following)
        self.assertEqual(framer.peek(2), b"\xaa\x02")


class StatusTests(unittest.TestCase):
    def test_captured_status_prefix(self):
        payload = bytes.fromhex("02030046460081430001015cff0100000000")
        self.assertEqual(len(payload), 18)
        frame = build_frame(0x04, 0x0F, payload)
        parsed_frames = Framer().feed(frame)
        self.assertEqual(len(parsed_frames), 1)
        status = decode_status(parsed_frames[0].payload)
        self.assertIsNotNone(status)
        self.assertEqual(status.state, 2)
        self.assertEqual(status.substate, 3)
        self.assertEqual(status.fault, 0)
        self.assertEqual(status.coolant_c, 70)
        self.assertEqual(status.voltage_like, 0x81)
        self.assertEqual(status.rising_sensor, 0x43)
        self.assertEqual(status.elapsed_minutes, 1)
        self.assertEqual(status.elapsed_seconds, 0x5C)
        self.assertTrue(status.burner_active)
        self.assertEqual(status.cooldown_counter, 0)
        self.assertEqual(status.mode_name, "Ignition 1")

    def test_extended_fields(self):
        raw = bytearray(58)
        raw[0] = 3
        raw[1] = 2
        raw[2], raw[3], raw[4] = 0, 1, 44
        raw[11] = 10
        raw[12] = 9
        raw[13] = 1
        raw[15] = 38
        raw[17], raw[18] = 0x01, 0xF4
        raw[19] = 60
        raw[20] = 70
        raw[21] = 40
        raw[22], raw[23] = 0, 125
        raw[36] = 0
        raw[51] = 0
        raw[52] = 1
        raw[54], raw[55] = 0, 100
        extended = decode_extended(bytes(raw))
        self.assertEqual(extended.mode_name, "High")
        self.assertEqual(extended.running_time_s, 300)
        self.assertEqual(extended.fan_target, 10)
        self.assertEqual(extended.fan_actual, 9)
        self.assertTrue(extended.glow_plug)
        self.assertEqual(extended.pump_hz, 3.8)
        self.assertEqual(extended.flame_c, 500 - 273)
        self.assertEqual(extended.liquid_c, 60)
        self.assertEqual(extended.overheat_c, 70)
        self.assertEqual(extended.board_c, 40)
        self.assertEqual(extended.voltage, 12.5)
        self.assertEqual(extended.fault, 0)
        self.assertEqual(extended.relay_state, 1)
        self.assertEqual(extended.fan_current, 100)
        self.assertNotIn(15, extended.unmapped)
        self.assertIn(5, extended.unmapped)
        echoed = build_frame(0x02, 0x00, bytes(raw))
        frame = Framer().feed(echoed)[0]
        self.assertEqual(classify(frame).voltage, 12.5)
        longer = build_frame(0x02, 0x01, bytes(raw) + bytes(14))
        frame = Framer().feed(longer)[0]
        self.assertEqual(frame.length, 72)
        self.assertEqual(classify(frame).pump_hz, 3.8)

    def test_pubr0_literal(self):
        self.assertTrue(PUBR0.startswith(bytes.fromhex("aa5055425230")))
        self.assertEqual(PUBR0[-2:].hex(), "0fb0")


class SettingsBlockTests(unittest.TestCase):
    def test_factory_block_round_trip(self):
        parsed = decode_heater_settings(FACTORY_SETTINGS)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.standby_c, 88)
        self.assertEqual(parsed.auxiliary_c, 95)
        self.assertEqual(parsed.relay_c, 40)
        view = heater_settings_view(parsed)
        self.assertEqual(view["auxiliary_mode"], "off")
        self.assertEqual(view["relay_mode"], "auto")
        self.assertTrue(view["pump_standby"])
        self.assertTrue(view["alarm_channel"])
        self.assertFalse(view["pump_with_engine"])

    def test_a_fractional_standby_is_rounded_not_truncated(self):
        self.assertEqual(patch_heater_settings(FACTORY_SETTINGS, {"coolant_limit": 46.68})[0], 47)
        self.assertEqual(patch_heater_settings(FACTORY_SETTINGS, {"coolant_limit": 46.4})[0], 46)

    def test_a_changed_standby_keeps_the_other_bytes(self):
        updated = patch_heater_settings(FACTORY_SETTINGS, {"coolant_limit": 40})
        self.assertEqual(updated[0], 40)
        self.assertEqual(updated[1:], FACTORY_SETTINGS[1:])
        frame = settings_write(updated)
        self.assertEqual(frame[4], TYPE_SETTINGS)
        self.assertEqual(frame[5], 40)
        request = settings_request()
        self.assertEqual(request[4], TYPE_SETTINGS)
        self.assertEqual(request[2:4], b"\x00\x00")

    def test_the_reply_is_classified_as_settings(self):
        raw = build_frame(DEV_HEATER, TYPE_SETTINGS, FACTORY_SETTINGS)
        parsed = classify(Framer().feed(raw)[0])
        self.assertEqual(parsed.standby_c, 88)

    def test_a_short_or_wild_block_is_ignored(self):
        self.assertIsNone(decode_heater_settings(b"\x58"))
        self.assertIsNone(decode_heater_settings(bytes(9)))


if __name__ == "__main__":
    unittest.main()
