import unittest

from autoterm_flow5.protocol import (
    PUBR0,
    classify,
    diagnostic_request,
    Framer,
    build_frame,
    crc16_modbus,
    decode_extended,
    decode_status,
    start_preheat,
    start_thermostat,
    status_request,
    stop_command,
)


class CrcTests(unittest.TestCase):
    def test_status_and_stop_vectors(self):
        self.assertEqual(status_request().hex(), "aa0300000f587c")
        self.assertEqual(diagnostic_request().hex(), "aa02000000a03d")
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

    def test_thermostat_marker(self):
        frame = start_thermostat()
        self.assertEqual(frame[4], 0x01)
        self.assertEqual(frame[5:7].hex(), "0022")


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

    def test_pubr0_literal(self):
        self.assertTrue(PUBR0.startswith(bytes.fromhex("aa5055425230")))
        self.assertEqual(PUBR0[-2:].hex(), "0fb0")


if __name__ == "__main__":
    unittest.main()
