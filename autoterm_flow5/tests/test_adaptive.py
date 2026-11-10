"""Unit tests for adaptive coolant-target control."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autoterm_flow5.adaptive import (
    P_MIN,
    AdaptiveConfig,
    AdaptiveController,
    default_c,
    equilibrium_temp,
    nearest_step,
    power_kw,
)
from autoterm_flow5.controller import Controller
from autoterm_flow5.protocol import (
    FACTORY_SETTINGS,
    decode_heater_settings,
    decode_status,
    stop_command,
)
from autoterm_flow5.settings import Settings


def status(state=0, substate=1, fault=0, coolant=40, burner=None):
    raw = bytearray(18)
    raw[0] = state
    raw[1] = substate
    raw[2] = fault
    raw[3] = coolant
    raw[4] = coolant
    if burner is None:
        burner = state in (2, 3)
    raw[12] = 0xFF if burner else 0
    return decode_status(bytes(raw))


class AdaptiveMathTests(unittest.TestCase):
    def test_power_scales_with_frequency(self):
        self.assertAlmostEqual(power_kw(4.4), 5.0)
        self.assertAlmostEqual(power_kw(1.8), P_MIN)
        self.assertEqual(power_kw(0), 0.0)

    def test_nearest_step(self):
        self.assertEqual(nearest_step(1.7), 1.8)
        self.assertEqual(nearest_step(2.9), 2.8)
        self.assertEqual(nearest_step(4.2), 4.4)

    def test_default_c_gives_about_50_c_at_2_kw(self):
        temp = equilibrium_temp(2.0, 20.0, default_c())
        self.assertAlmostEqual(temp, 50.0, places=0)


class AdaptiveControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "adaptive.json"
        self.config = AdaptiveConfig(
            enabled=True,
            coolant_min=45,
            coolant_max=85,
            margin_c=3,
            base_step_c=2,
            power_confirm_s=60,
            power_lockout_s=120,
            soot_interval_s=24 * 3600,
        )
        self.adaptive = AdaptiveController(self.path, self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def _tick(self, now, **kwargs):
        defaults = dict(
            phase="idle",
            burner_active=False,
            fault=0,
            heat_enabled=True,
            mode="thermostat",
            room_c=19.0,
            outdoor_c=-5.0,
            coolant_c=40.0,
            pump_hz=0.0,
            room_target=20.0,
            hysteresis_c=2.0,
            current_coolant_limit=60.0,
            sensors_ok=True,
        )
        defaults.update(kwargs)
        return self.adaptive.tick(now, **defaults)

    def test_disabled_does_not_write_or_stop(self):
        self.adaptive.config.enabled = False
        self.adaptive._enabled_was = False
        actions = self._tick(1.0, phase="running", burner_active=True, pump_hz=4.4, coolant_c=55)
        self.assertFalse(actions.stop)
        self.assertIsNone(actions.coolant_limit)

    def test_idle_writes_computed_target_when_enabled(self):
        actions = self._tick(10.0, phase="idle", current_coolant_limit=88.0)
        self.assertIsNotNone(actions.coolant_limit)
        self.assertGreaterEqual(actions.coolant_limit, 45.0)
        self.assertLessEqual(actions.coolant_limit, 85.0)

    def test_continuous_target_stays_above_min_power_equilibrium(self):
        # Need = 0.15 * 25 = 3.75 kW, between P_min and P_max.
        self.adaptive.model.ua = 0.15
        self.adaptive.model.c = default_c()
        self.adaptive.model.reliable = True
        target = self.adaptive._compute_target(-5.0, 20.0, 20.0)
        floor = equilibrium_temp(P_MIN, 20.0, self.adaptive.model.c) + 3.0
        self.assertEqual(self.adaptive.model.last_mode, "continuous")
        self.assertGreaterEqual(target, floor - 0.1)

    def test_intermittent_when_heat_need_below_pmin(self):
        self.adaptive.model.ua = 0.05
        self.adaptive.model.reliable = True
        target = self.adaptive._compute_target(10.0, 20.0, 20.0)
        self.assertEqual(self.adaptive.model.last_mode, "intermittent")
        self.assertGreaterEqual(target, 45.0)

    def test_maximum_when_heat_need_above_pmax(self):
        self.adaptive.model.ua = 0.5
        self.adaptive.model.reliable = True
        target = self.adaptive._compute_target(-20.0, 20.0, 20.0)
        self.assertEqual(self.adaptive.model.last_mode, "maximum")
        self.assertEqual(target, 85.0)

    def test_liquid_standby_triggers_stop_and_raise(self):
        # Start a cycle first.
        self._tick(100.0, phase="running", burner_active=True, pump_hz=1.8, coolant_c=58, room_c=18)
        actions = self._tick(
            160.0,
            phase="cooldown",
            burner_active=False,
            pump_hz=1.8,
            coolant_c=60,
            room_c=18,
            current_coolant_limit=58,
        )
        self.assertTrue(actions.stop)
        self.assertTrue(self.adaptive._awaiting_idle_write)

    def test_after_adaptive_stop_idle_writes_and_restarts(self):
        self._tick(100.0, phase="running", burner_active=True, pump_hz=1.8, coolant_c=58, room_c=18)
        self._tick(160.0, phase="cooldown", burner_active=False, pump_hz=1.8, coolant_c=60, room_c=18)
        actions = self._tick(200.0, phase="idle", burner_active=False, pump_hz=0, coolant_c=55, room_c=18)
        self.assertIsNotNone(actions.coolant_limit)
        self.assertTrue(actions.start)

    def test_high_share_cycle_lowers_target(self):
        self.adaptive.model.reliable = True
        self._tick(0.0, phase="running", burner_active=True, pump_hz=4.4, coolant_c=65, room_c=18)
        # Accumulate almost entirely high-frequency time.
        for second in range(1, 100):
            self._tick(
                float(second),
                phase="running",
                burner_active=True,
                pump_hz=4.4,
                coolant_c=65 + second * 0.01,
                room_c=18 + second * 0.01,
            )
        self.assertIsNotNone(self.adaptive._cycle)
        self.adaptive._cycle.coolant_target = 80.0
        self.adaptive._cycle.coolant_max = 68.0
        self.adaptive._finish_cycle(100.0, 20.5, "thermostat", skip_learning=False)
        # Desired is max_liquid-2=66, but the drop is capped at 5 °C per cycle.
        self.assertAlmostEqual(self.adaptive.model.last_target, 75.0)

    def test_model_persists(self):
        self.adaptive.model.ua = 0.22
        self.adaptive.model.k_bins["-2"] = 1.5
        self.adaptive.save()
        loaded = AdaptiveController(self.path, self.config)
        self.assertAlmostEqual(loaded.model.ua, 0.22)
        self.assertAlmostEqual(loaded.model.k_bins["-2"], 1.5)

    def test_reset_learned_restores_defaults(self):
        self.adaptive.model.ua = 0.4
        self.adaptive.model.k_bins["0"] = 5
        self.adaptive.model.c_samples = 9
        self.adaptive.reset_learned()
        self.assertAlmostEqual(self.adaptive.model.ua, 0.15)
        self.assertEqual(self.adaptive.model.k_bins, {})
        self.assertEqual(self.adaptive.model.c_samples, 0)

    def test_enable_saves_manual_coolant(self):
        self.adaptive.config.enabled = False
        self.adaptive._enabled_was = False
        actions = self.adaptive.on_enabled_changed(True, 77.0)
        self.assertEqual(self.adaptive._manual_coolant, 77.0)
        self.assertTrue(actions.save_settings)

    def test_disable_restores_manual_on_idle(self):
        self.adaptive.config.enabled = True
        self.adaptive._enabled_was = True
        self.adaptive._manual_coolant = 72.0
        self.adaptive.on_enabled_changed(False, 60.0)
        actions = self._tick(50.0, phase="idle")
        self.assertEqual(actions.coolant_limit, 72.0)

    def test_safe_coolant_on_fault(self):
        actions = self._tick(10.0, phase="idle", fault=14, sensors_ok=False)
        self.assertEqual(actions.coolant_limit, self.config.safe_coolant_c)

    def test_status_values_include_metrics(self):
        values = self.adaptive.status_values()
        self.assertEqual(values["adaptive_enabled"], "ON")
        self.assertIn(values["adaptive_status"], {"learning", "active"})
        self.assertIn("adaptive_ua", values)


class AdaptiveControllerIntegrationTests(unittest.TestCase):
    def test_adaptive_stop_keeps_heat_enabled(self):
        controller = Controller(Settings())
        controller.accept_device_settings(decode_heater_settings(FACTORY_SETTINGS), 0.0, "idle")
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 15, 0)
        controller.tick(1, status(3, 2, coolant=50), None, 15, 0)
        controller.adaptive_stop(2)
        self.assertTrue(controller.heat_enabled)
        stopped = controller.tick(2, status(3, 2, coolant=50), None, 15, 0)
        self.assertIn(stop_command(), stopped.commands)
        controller.adaptive_resume(10)
        idle = controller.tick(10, status(), None, 15, 0)
        self.assertTrue(any(cmd[4] == 0x01 for cmd in idle.commands))


if __name__ == "__main__":
    unittest.main()
