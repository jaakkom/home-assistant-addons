"""Unit tests for adaptive coolant-target control."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autoterm_flow5.adaptive import (
    FREQ_HIGH,
    FREQ_LOW,
    FREQ_MID,
    P_MIN,
    AdaptiveConfig,
    AdaptiveController,
    classify_step,
    default_c,
    equilibrium_temp,
    is_burning,
    nearest_step,
    power_kw,
)
from autoterm_flow5.controller import Controller
from autoterm_flow5.protocol import (
    FACTORY_SETTINGS,
    decode_heater_settings,
    decode_status,
    requested_temperature,
    stop_command,
)
from autoterm_flow5.settings import Settings

IDLE = (0, 1)
GLOW = (2, 1)
IGNITION = (2, 3)
CHAMBER = (2, 6)
LOW = (3, 0)
MIDDLE = (3, 8)
HIGH = (3, 2)
WAITING = (3, 5)
COOLDOWN = (4, 0)

PHASES = {IDLE: "idle", GLOW: "starting", IGNITION: "starting", CHAMBER: "starting",
          LOW: "running", MIDDLE: "running", HIGH: "running", WAITING: "running",
          COOLDOWN: "cooldown"}


def status(state=0, substate=1, coolant=40):
    raw = bytearray(18)
    raw[0] = state
    raw[1] = substate
    raw[3] = coolant
    raw[4] = coolant
    raw[12] = 0xFF if state in (2, 3) else 0
    return decode_status(bytes(raw))


class AdaptiveMathTests(unittest.TestCase):
    def test_power_scales_with_frequency(self):
        self.assertAlmostEqual(power_kw(4.4), 5.0)
        self.assertAlmostEqual(power_kw(1.8), P_MIN)
        self.assertEqual(power_kw(0), 0.0)

    def test_steps_follow_the_state_name_then_the_frequency(self):
        self.assertEqual(classify_step(MIDDLE, 2.6), FREQ_MID)
        self.assertEqual(classify_step(LOW, 2.6), FREQ_LOW)
        self.assertEqual(classify_step((3, 9), 3.1), FREQ_MID)
        self.assertEqual(nearest_step(4.2), FREQ_HIGH)

    def test_startup_and_waiting_are_not_burning(self):
        for state in (GLOW, IGNITION, CHAMBER, WAITING, COOLDOWN, IDLE):
            self.assertFalse(is_burning(state), state)
        for state in (LOW, MIDDLE, HIGH):
            self.assertTrue(is_burning(state), state)

    def test_default_c_gives_about_50_c_at_2_kw(self):
        self.assertAlmostEqual(equilibrium_temp(2.0, 20.0, default_c()), 50.0, places=0)


class AdaptiveControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "adaptive.json"
        self.config = AdaptiveConfig(enabled=True, startup_grace_s=0)
        self.adaptive = AdaptiveController(self.path, self.config)
        self.now = 0.0
        self.confirmed = 60.0

    def tearDown(self):
        self.tmp.cleanup()

    def tick(self, state, seconds=1.0, **kwargs):
        self.now += seconds
        values = dict(
            state=state,
            phase=PHASES[state],
            fault=0,
            heat_enabled=True,
            mode="thermostat",
            room_c=19.5,
            outdoor_c=-5.0,
            coolant_c=40.0,
            pump_hz=0.0,
            room_target=20.0,
            hysteresis_c=2.0,
            confirmed_coolant=self.confirmed,
        )
        values.update(kwargs)
        return self.adaptive.tick(self.now, **values)

    def run_for(self, state, seconds, step=10.0, **kwargs):
        actions = []
        end = self.now + seconds
        while self.now < end:
            actions.append(self.tick(state, step, **kwargs))
        return actions

    def ignite(self, **kwargs):
        self.tick(IDLE, **kwargs)
        self.adaptive._verify = None
        self.adaptive._wrote_target_this_rest = True
        self.tick(GLOW, **kwargs)

    def test_disabled_never_writes_holds_or_stops(self):
        self.adaptive.config.enabled = False
        self.adaptive._enabled_was = False
        actions = [self.tick(IDLE)] + self.run_for(MIDDLE, 3600, pump_hz=3.0)
        self.assertFalse(any(a.stop or a.coolant_limit is not None or a.hold_start for a in actions))

    def test_middle_stage_at_cold_liquid_is_not_standby(self):
        # The log: Middle at 25 °C liquid with a 40 °C target was taken as standby.
        self.confirmed = 40.0
        self.ignite(room_c=11.2, room_target=14)
        self.run_for(CHAMBER, 120, pump_hz=1.8, coolant_c=22, room_c=11.2, room_target=14)
        actions = self.run_for(MIDDLE, 600, pump_hz=3.0, coolant_c=25, room_c=11.2, room_target=14)
        self.assertFalse(any(a.stop for a in actions))
        self.assertFalse(self.adaptive._cycle.standby_seen)

    def test_startup_time_is_not_in_the_shares(self):
        self.ignite()
        self.run_for(CHAMBER, 300, pump_hz=1.8, coolant_c=30)
        self.run_for(HIGH, 600, pump_hz=4.4, coolant_c=45)
        cycle = self.adaptive._cycle
        self.assertAlmostEqual(cycle.burn_s, 600, delta=10)
        self.assertEqual(cycle.time_low, 0.0)
        self.tick(COOLDOWN, coolant_c=45)
        self.tick(IDLE, coolant_c=45)
        entry = self.adaptive.model.cycle_log[-1]
        self.assertEqual(entry["share_1_8"], 0.0)
        self.assertAlmostEqual(entry["share_4_4"], 1.0, places=2)

    def test_standby_needs_hot_liquid_zero_pump_and_a_wait_state(self):
        self.ignite()
        self.run_for(LOW, 600, pump_hz=1.8, coolant_c=55)
        self.tick(WAITING, pump_hz=0.0, coolant_c=50)
        self.assertFalse(self.adaptive._cycle.standby_seen)
        self.tick(WAITING, pump_hz=0.0, coolant_c=59)
        self.assertTrue(self.adaptive._cycle.standby_seen)

    def test_no_stop_before_twenty_minutes_of_burn(self):
        self.ignite()
        self.run_for(LOW, 600, pump_hz=1.8, coolant_c=58)
        actions = self.run_for(WAITING, 60, pump_hz=0.0, coolant_c=60)
        self.assertFalse(any(a.stop for a in actions))

    def test_standby_after_long_burn_stops_once_and_raises(self):
        self.ignite()
        self.run_for(LOW, 1300, pump_hz=1.8, coolant_c=58)
        actions = self.run_for(WAITING, 30, pump_hz=0.0, coolant_c=60)
        stops = [a for a in actions if a.stop]
        self.assertEqual(len(stops), 1)
        self.assertGreater(self.adaptive._pending_target, 60.0)

    def test_stops_share_one_lockout_and_a_six_hour_quota(self):
        self.adaptive.model.active_stops.extend([self.now - 100])
        self.assertFalse(self.adaptive._stop_allowed(self.now))
        self.adaptive.model.active_stops.clear()
        self.adaptive.model.active_stops.extend([self.now - 5 * 3600, self.now - 2 * 3600])
        with self.assertLogs("autoterm_flow5.adaptive", "WARNING"):
            self.assertFalse(self.adaptive._stop_allowed(self.now))
        self.adaptive.model.active_stops.clear()
        self.adaptive.model.active_stops.append(self.now - 2 * 3600)
        self.assertTrue(self.adaptive._stop_allowed(self.now))

    def test_a_room_far_below_target_bypasses_the_quota_with_a_shorter_lockout(self):
        self.adaptive.model.active_stops.extend([self.now - 5 * 3600, self.now - 2 * 3600])
        with self.assertLogs("autoterm_flow5.adaptive", "WARNING"):
            self.assertTrue(self.adaptive._stop_allowed(self.now, urgent=True))
        self.adaptive.model.active_stops.append(self.now - 30 * 60)
        self.assertFalse(self.adaptive._stop_allowed(self.now, urgent=True))
        self.adaptive.model.active_stops[-1] = self.now - 50 * 60
        with self.assertLogs("autoterm_flow5.adaptive", "WARNING"):
            self.assertTrue(self.adaptive._stop_allowed(self.now, urgent=True))

    def test_a_cold_room_at_low_power_is_raised_even_when_ua_says_enough(self):
        self.adaptive.model.ua = 0.01
        self.ignite()
        cold = dict(room_c=16.5, room_target=20.0)
        actions = self.run_for(LOW, 1200 + 15 * 60 + 60, pump_hz=1.8, coolant_c=50, **cold)
        self.assertEqual(sum(1 for a in actions if a.stop), 1)
        self.assertEqual(self.adaptive._pending_action, "power_raise")
        self.assertEqual(self.adaptive._pending_target, self.config.rise_target_c)

    def test_standby_in_a_cold_room_stops_even_with_the_quota_used(self):
        self.adaptive.model.active_stops.extend([self.now - 5 * 3600, self.now - 2 * 3600])
        self.ignite()
        self.run_for(LOW, 1300, pump_hz=1.8, coolant_c=50)
        with self.assertLogs("autoterm_flow5.adaptive", "WARNING"):
            actions = self.run_for(WAITING, 30, pump_hz=0.0, coolant_c=60, room_c=16.5)
        self.assertEqual(sum(1 for a in actions if a.stop), 1)
        self.assertEqual(self.adaptive._pending_target, self.config.rise_target_c)

    def test_standby_in_a_mildly_cool_room_respects_the_quota(self):
        self.adaptive.model.active_stops.extend([self.now - 5 * 3600, self.now - 2 * 3600])
        self.ignite()
        self.run_for(LOW, 1300, pump_hz=1.8, coolant_c=50)
        actions = self.run_for(WAITING, 30, pump_hz=0.0, coolant_c=60, room_c=18.5)
        self.assertFalse(any(a.stop for a in actions))

    def test_a_mildly_cool_room_still_trusts_ua(self):
        self.adaptive.model.ua = 0.01
        cool = dict(room_c=17.5, room_target=20.0)
        self.ignite()
        actions = self.run_for(LOW, 1200 + 40 * 60, pump_hz=1.8, coolant_c=50, **cool)
        self.assertFalse(any(a.stop for a in actions))

    def test_rest_writes_holds_and_releases_after_read_back(self):
        self.confirmed = 40.0
        first = self.tick(IDLE)
        self.assertIsNotNone(first.coolant_limit)
        self.assertTrue(first.hold_start)
        target = first.coolant_limit
        waiting = self.tick(IDLE, confirmed_coolant=None)
        self.assertTrue(waiting.hold_start)
        done = self.tick(IDLE, confirmed_coolant=target)
        self.assertFalse(done.hold_start)

    def test_a_fractional_target_is_written_and_checked_as_whole_degrees(self):
        self.confirmed = 51.0
        self.adaptive._rest_target = lambda *args: 46.68097244113454
        first = self.tick(IDLE)
        self.assertEqual(first.coolant_limit, 47.0)
        done = self.tick(IDLE, confirmed_coolant=47.0)
        self.assertFalse(done.hold_start)

    def test_a_rejected_write_is_retried_three_times_then_started(self):
        self.confirmed = 40.0
        first = self.tick(IDLE)
        self.assertIsNotNone(first.coolant_limit)
        retries = [self.tick(IDLE, confirmed_coolant=40.0) for _ in range(3)]
        self.assertTrue(all(a.coolant_limit is not None for a in retries))
        with self.assertLogs("autoterm_flow5.adaptive", "ERROR"):
            last = self.tick(IDLE, confirmed_coolant=40.0)
        self.assertFalse(last.hold_start)
        self.assertTrue(self.adaptive._next_cycle_unlearned)
        self.tick(GLOW, confirmed_coolant=40.0)
        self.assertFalse(self.adaptive._cycle.learned)
        self.assertEqual(self.adaptive._cycle.coolant_target, 40.0)

    def test_adaptive_restart_waits_for_the_read_back(self):
        self.ignite()
        self.run_for(LOW, 1300, pump_hz=1.8, coolant_c=58)
        self.run_for(WAITING, 30, pump_hz=0.0, coolant_c=60)
        target = self.adaptive._pending_target
        self.tick(COOLDOWN, coolant_c=58)
        write = self.tick(IDLE, coolant_c=55)
        self.assertEqual(write.coolant_limit, target)
        self.assertFalse(write.start)
        ready = self.tick(IDLE, coolant_c=55, confirmed_coolant=target)
        self.assertTrue(ready.start)

    def test_raise_never_lowers(self):
        self.adaptive._actual_coolant = 54.0
        self.adaptive.model.last_target = 54.0
        target = self.adaptive._raise_target("liquid_standby_raise", 6.4, 11.0, 14.0)
        self.assertGreater(target, 54.0)

    def test_raise_at_the_limit_is_skipped(self):
        self.adaptive._actual_coolant = 85.0
        self.assertIsNone(self.adaptive._raise_target("power_raise", -5.0, 18.0, 20.0))

    def test_rise_mode_when_room_is_far_below_target(self):
        actions = self.tick(IDLE, room_c=11.2, room_target=14.0, outdoor_c=6.4)
        self.assertEqual(self.adaptive.model.last_mode, "rise")
        self.assertEqual(actions.coolant_limit, 75.0)

    def test_rise_mode_ends_near_target(self):
        self.tick(IDLE, room_c=11.2, room_target=14.0)
        self.assertTrue(self.adaptive._rise_active)
        self.tick(LOW, room_c=13.8, room_target=14.0, pump_hz=1.8)
        self.assertFalse(self.adaptive._rise_active)

    def test_rise_mode_makes_no_corrections(self):
        self.confirmed = 75.0
        self.tick(IDLE, room_c=11.0, room_target=14.0)
        self.tick(GLOW, room_c=11.0, room_target=14.0)
        self.run_for(LOW, 1300, pump_hz=1.8, coolant_c=74, room_c=11.0, room_target=14.0)
        actions = self.run_for(WAITING, 60, pump_hz=0.0, coolant_c=76, room_c=11.0, room_target=14.0)
        self.assertFalse(any(a.stop for a in actions))

    def test_enabling_during_a_burn_neither_stops_nor_learns(self):
        self.adaptive.config.enabled = False
        self.adaptive._enabled_was = False
        self.ignite()
        self.run_for(LOW, 300, pump_hz=1.8, coolant_c=50)
        self.adaptive.on_enabled_changed(True, 60.0)
        actions = self.run_for(LOW, 1300, pump_hz=1.8, coolant_c=58)
        actions += self.run_for(WAITING, 60, pump_hz=0.0, coolant_c=60)
        self.assertFalse(any(a.stop for a in actions))
        self.assertTrue(self.adaptive._cycle.observe_only)

    def test_safe_coolant_waits_for_the_startup_grace(self):
        adaptive = AdaptiveController(self.path, AdaptiveConfig(enabled=True, startup_grace_s=30))
        common = dict(phase="unknown", fault=0, heat_enabled=True, mode="thermostat", room_c=19.0,
                      outdoor_c=0.0, coolant_c=None, pump_hz=0.0, room_target=20.0,
                      hysteresis_c=2.0, confirmed_coolant=None)
        with self.assertNoLogs("autoterm_flow5.adaptive", "WARNING"):
            adaptive.tick(0.0, state=None, **common)
            adaptive.tick(10.0, state=None, **common)
        with self.assertLogs("autoterm_flow5.adaptive", "WARNING"):
            adaptive.tick(31.0, state=None, **common)

    def test_fault_writes_safe_coolant_at_idle(self):
        actions = self.tick(IDLE, fault=14)
        self.assertEqual(actions.coolant_limit, self.config.safe_coolant_c)

    def test_disable_restores_manual_on_idle(self):
        self.adaptive._manual_coolant = 72.0
        self.adaptive.on_enabled_changed(False, 60.0)
        self.assertEqual(self.tick(IDLE).coolant_limit, 72.0)

    def test_model_persists_and_resets(self):
        self.adaptive.model.ua = 0.22
        self.adaptive.model.k_bins["-2"] = 1.5
        self.adaptive.model.active_stops.append(123.0)
        self.adaptive.save()
        loaded = AdaptiveController(self.path, self.config)
        self.assertAlmostEqual(loaded.model.ua, 0.22)
        self.assertEqual(list(loaded.model.active_stops), [123.0])
        loaded.reset_learned()
        self.assertAlmostEqual(loaded.model.ua, 0.15)
        self.assertEqual(loaded.model.k_bins, {})


class UaLearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adaptive = AdaptiveController(Path(self.tmp.name) / "m.json", AdaptiveConfig(enabled=False))
        self.adaptive._enabled_was = False

    def tearDown(self):
        self.tmp.cleanup()

    def simulate_house(self, hours, *, ua, mass_kwh_per_c, outdoor, room, pump_hz=3.0, start=0.0, dropout=None):
        """Thermostat-driven house with stored heat; the heater burns between 19.7 and 20.3 °C.

        dropout=(period_s, length_s) loses telemetry for length_s at the start of every period.
        """
        now, step_s, burning, ignition_left = start, 10.0, False, 0.0
        heat_kw = power_kw(pump_hz)
        while now < start + hours * 3600:
            if not burning and ignition_left <= 0 and room <= 19.7:
                ignition_left = 60.0
            if ignition_left > 0:
                state, hz = GLOW, 0.0
                ignition_left -= step_s
                if ignition_left <= 0:
                    burning = True
            elif burning:
                state, hz = MIDDLE, pump_hz
                if room >= 20.3:
                    burning = False
                    state, hz = IDLE, 0.0
            else:
                state, hz = IDLE, 0.0
            delivered = heat_kw if state == MIDDLE else 0.0
            room += (delivered - ua * (room - outdoor)) * (step_s / 3600.0) / mass_kwh_per_c
            now += step_s
            lost = dropout is not None and (now % dropout[0]) < dropout[1]
            self.adaptive.tick(
                now, state=None if lost else state, phase=PHASES[state], fault=0, heat_enabled=True,
                mode="thermostat", room_c=room, outdoor_c=outdoor, coolant_c=None if lost else 60.0,
                pump_hz=hz, room_target=20.0, hysteresis_c=0.6, confirmed_coolant=60.0,
            )
        return now, room

    def test_short_telemetry_dropouts_keep_the_ua_window(self):
        self.simulate_house(
            36, ua=0.18, mass_kwh_per_c=3.0, outdoor=0.0, room=12.0, pump_hz=FREQ_HIGH, dropout=(1800, 120)
        )
        self.assertGreaterEqual(self.adaptive.model.ua_samples, 2)
        self.assertAlmostEqual(self.adaptive.model.ua, 0.18, delta=0.03)

    def test_the_ua_window_survives_a_short_add_on_restart(self):
        house = dict(ua=0.18, mass_kwh_per_c=3.0, outdoor=0.0, pump_hz=FREQ_HIGH)
        now, room = self.simulate_house(2, room=19.8, **house)
        self.assertIsNotNone(self.adaptive._ua_totals)
        collected = self.adaptive._ua_totals.seconds
        first = self.adaptive._ua_anchors[0].at
        self.adaptive.save()
        self.adaptive = AdaptiveController(Path(self.tmp.name) / "m.json", AdaptiveConfig(enabled=False))
        self.adaptive._enabled_was = False
        self.simulate_house(0.01, room=room, start=now + 300, **house)
        self.assertIsNotNone(self.adaptive._ua_totals)
        self.assertGreater(self.adaptive._ua_totals.seconds, collected + 300)
        self.assertEqual(self.adaptive._ua_anchors[0].at, first)

    def test_a_long_add_on_outage_drops_the_ua_window(self):
        house = dict(ua=0.18, mass_kwh_per_c=3.0, outdoor=0.0, pump_hz=FREQ_HIGH)
        now, room = self.simulate_house(2, room=19.8, **house)
        self.adaptive.save()
        self.adaptive = AdaptiveController(Path(self.tmp.name) / "m.json", AdaptiveConfig(enabled=False))
        self.adaptive._enabled_was = False
        self.simulate_house(0.01, room=room, start=now + 3600, **house)
        self.assertTrue(all(anchor.at >= now + 3600 for anchor in self.adaptive._ua_anchors))

    def advance(self, hours, *, ua=0.15, difference=10.0):
        totals = self.adaptive._ua_totals
        totals.energy_kwh += ua * difference * hours
        totals.delta_ch += difference * hours
        totals.seconds += hours * 3600

    def test_a_first_window_at_another_room_temperature_does_not_block_learning(self):
        # Field log: heating resumed after a restart at 15.4 °C, the add-on
        # thermostat then lit the heater at 15.0 °C every time.
        a = self.adaptive
        a._ua_checkpoint(0.0, 15.4, 45.0)
        self.advance(1.2, ua=0.12)
        a._ua_checkpoint(1.2 * 3600, 15.0, 45.0)
        for hour in (2.4, 3.6):
            self.advance(1.2, ua=0.12)
            a._ua_checkpoint(hour * 3600, 15.0, 45.0)
        self.assertEqual(a.model.ua_samples, 0)
        self.advance(1.2, ua=0.12)
        a._ua_checkpoint(4.8 * 3600, 15.1, 45.0)
        self.assertEqual(a.model.ua_samples, 1)
        self.assertAlmostEqual(a.model.ua, 0.12, places=3)
        self.assertEqual(len(a._ua_anchors), 1)

    def test_a_room_exactly_three_tenths_away_matches(self):
        a = self.adaptive
        a._ua_checkpoint(0.0, 15.4, 45.0)
        self.advance(3.1, ua=0.12)
        a._ua_checkpoint(3.1 * 3600, 15.1, 45.0)
        self.assertEqual(a.model.ua_samples, 1)

    def test_a_window_saved_by_0_5_7_is_resumed(self):
        from autoterm_flow5.adaptive import AdaptiveModel

        path = Path(self.tmp.name) / "old.json"
        model = AdaptiveModel()
        model.ua_window = {
            "started_at": 0.0, "room_c": 15.0, "coolant_c": 45.0,
            "energy_kwh": 4.0, "delta_ch": 25.0, "seconds": 9000.0, "saved_at": 9000.0,
        }
        path.write_text(json.dumps(model.to_dict()))
        adaptive = AdaptiveController(path, AdaptiveConfig(enabled=False))
        adaptive._restore_ua_window(9100.0)
        self.assertEqual(adaptive._ua_totals.seconds, 9000.0)
        self.assertEqual(adaptive._ua_anchors[0].room_c, 15.0)

    def test_warm_up_from_cold_does_not_inflate_ua(self):
        self.simulate_house(36, ua=0.18, mass_kwh_per_c=3.0, outdoor=0.0, room=12.0, pump_hz=FREQ_HIGH)
        self.assertGreaterEqual(self.adaptive.model.ua_samples, 2)
        self.assertAlmostEqual(self.adaptive.model.ua, 0.18, delta=0.02)

    def test_mild_weather_leaves_ua_alone(self):
        self.simulate_house(36, ua=0.18, mass_kwh_per_c=3.0, outdoor=15.0, room=19.0)
        self.assertEqual(self.adaptive.model.ua_samples, 0)
        self.assertAlmostEqual(self.adaptive.model.ua, 0.15)

    def test_version_one_ua_is_discarded(self):
        from autoterm_flow5.adaptive import AdaptiveModel

        model = AdaptiveModel.from_dict({"ua": 0.27, "c": 0.03, "reliable": True})
        self.assertAlmostEqual(model.ua, 0.15)
        self.assertFalse(model.reliable)
        self.assertAlmostEqual(model.c, 0.03)
        kept = AdaptiveModel.from_dict(model.to_dict() | {"ua": 0.2})
        self.assertAlmostEqual(kept.ua, 0.2)


class ControllerHoldTests(unittest.TestCase):
    def controller(self):
        controller = Controller(Settings(room_target=14))
        controller.accept_device_settings(decode_heater_settings(FACTORY_SETTINGS), 0.0, "idle")
        return controller

    def test_adaptive_stop_does_not_resend_the_start(self):
        # The log: "Sent heater stop" was followed by a thermostat start one second later.
        controller = self.controller()
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 11, 0)
        controller.tick(1, status(3, 8, coolant=25), None, 11, 0)
        controller.adaptive_stop(2)
        stopped = controller.tick(2, status(3, 8, coolant=25), None, 11, 0)
        self.assertIn(stop_command(), stopped.commands)
        after = controller.tick(3, status(3, 8, coolant=25), None, 11, 0)
        self.assertNotIn(requested_temperature(14), after.commands)
        idle = controller.tick(10, status(), None, 11, 0)
        self.assertNotIn(requested_temperature(14), idle.commands)
        controller.adaptive_resume(11)
        resumed = controller.tick(11, status(), None, 11, 0)
        self.assertIn(requested_temperature(14), resumed.commands)

    def test_a_held_start_waits(self):
        controller = self.controller()
        controller.set_heat(True, 0)
        controller.start_blocked = True
        held = controller.tick(0, status(), None, 11, 0)
        self.assertNotIn(requested_temperature(14), held.commands)
        controller.start_blocked = False
        started = controller.tick(1, status(), None, 11, 0)
        self.assertIn(requested_temperature(14), started.commands)

    def test_a_lower_target_reaches_a_running_heater_during_the_hold(self):
        controller = self.controller()
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 13, 0)
        controller.tick(1, status(3, 8, coolant=50), None, 13, 0)
        controller.start_blocked = True
        controller.settings.room_target = 12
        lowered = controller.tick(2, status(3, 8, coolant=50), None, 13, 0)
        self.assertIn(requested_temperature(12), lowered.commands)

    def test_a_target_change_at_idle_waits_for_the_hold(self):
        controller = self.controller()
        controller.set_heat(True, 0)
        controller.tick(0, status(), None, 11, 0)
        controller.tick(1, status(3, 8, coolant=50), None, 11, 0)
        controller.start_blocked = True
        controller.settings.room_target = 17
        held = controller.tick(2, status(), None, 11, 0)
        self.assertNotIn(requested_temperature(17), held.commands)
        controller.start_blocked = False
        sent = controller.tick(3, status(), None, 11, 0)
        self.assertIn(requested_temperature(17), sent.commands)

    def test_confirmed_standby_is_hidden_while_a_write_is_open(self):
        controller = self.controller()
        self.assertEqual(controller.confirmed_standby(), 88.0)
        controller.settings.coolant_limit = 54
        controller.note_device_setting("coolant_limit")
        self.assertIsNone(controller.confirmed_standby())


if __name__ == "__main__":
    unittest.main()
