"""Offline checks for acquisition state and stale-record handling."""

import importlib.util
import argparse
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "scope.py"
spec = importlib.util.spec_from_file_location("scope_under_test", SCRIPT)
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)


class FakeInstrument:
    def __init__(self):
        self.writes = []
        self.state = "STOP"
        self.points = 20000
        self.fail_command = None

    def write(self, command):
        self.writes.append(command)
        if command == self.fail_command:
            raise OSError("test write failure")
        if command.startswith("ACQuire:POINts "):
            self.points = int(command.split()[-1])
        elif command == "RUNContinous":
            self.state = "RUN"
        elif command == "STOP":
            self.state = "STOP"

    def query(self, command, timeout=None):
        values = {
            "CHANnel1:STATe?": "ON",
            "CHANnel1:DATA:HEADer?": "0,0.031,32",
            "CHANnel1:DATA?": ",".join(str(i % 2) for i in range(32)),
            "CHANnel1:SCALe?": "0.5",
            "CHANnel1:OFFSet?": "0",
            "TRIGger:A:LEVel?": "0.5",
            "ACQuire:STATe?": self.state,
            "ACQuire:POINts?": str(self.points),
        }
        return values[command]

    def query_int(self, command):
        return int(self.query(command))


class ScopeStateTests(unittest.TestCase):
    def test_model_guard_only_accepts_verified_rtb2000_variants(self):
        self.assertTrue(scope.is_supported_idn(
            "Rohde&Schwarz,RTB2004,1333.1005k04/113121,02.202"))
        self.assertTrue(scope.is_supported_idn("Rohde&Schwarz,RTB2002,serial,firmware"))
        self.assertFalse(scope.is_supported_idn("Rohde&Schwarz,RTO2044,serial,firmware"))
        self.assertFalse(scope.is_supported_idn("Other,RTB2004,serial,firmware"))

    def test_public_capture_requires_ten_stable_periods(self):
        parser = scope.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["csv", "--stable-periods", "9"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["watch", "--stable-periods", "0"])
        self.assertEqual(parser.parse_args(["csv", "--stable-periods", "10"]).stable_periods, 10)

    def test_direct_capture_rejects_short_stability_before_instrument_commands(self):
        inst = FakeInstrument()
        args = argparse.Namespace(stable_periods=9, already_captured=False)
        with self.assertRaisesRegex(scope.ScpiError, "至少保持 10"):
            scope.cmd_csv(args, inst)
        with self.assertRaisesRegex(scope.ScpiError, "至少保持 10"):
            scope.cmd_watch(args, inst)
        self.assertEqual(inst.writes, [])

    def test_trigger_wait_arms_normal_without_auto_capture(self):
        inst = FakeInstrument()
        args = type("Args", (), {"trig_slope": "rise", "min_vpp": 0.005,
                                   "duration": 0, "trig_level": 1.0,
                                   "interval": 0.5})()
        old_read, old_sleep = scope.meas_read_one, scope.time.sleep
        scope.meas_read_one = lambda _inst, slot: {3: 2.0, 4: 1.5, 2: 1000.0}[slot]
        scope.time.sleep = lambda _: None
        try:
            scope.monitor_for_signal(inst, 1, 1.0, args, 0.0)
            self.assertIn("TRIGger:A:MODE NORMal", inst.writes)
            self.assertNotIn("TRIGger:A:MODE AUTO", inst.writes)
            self.assertLess(inst.writes.index("TRIGger:A:MODE NORMal"),
                            inst.writes.index("RUN"))
        finally:
            scope.meas_read_one, scope.time.sleep = old_read, old_sleep

    def test_reading_frozen_waveform_never_rearms(self):
        inst = FakeInstrument()
        wave = scope.read_channel(inst, 1)
        self.assertEqual(wave["points"], 32)
        self.assertEqual(inst.writes, [])

    def test_uncompleted_single_capture_cannot_export_stale_record(self):
        inst = FakeInstrument()
        args = type("Args", (), {"trig_slope": "rise", "trig_mode": "normal",
                                   "trig_level": 0.5, "trig_source": "CH1",
                                   "trig_wait": 1.0})()
        old_sleep = scope.time.sleep
        scope.time.sleep = lambda _: None
        try:
            with self.assertRaisesRegex(scope.ScpiError, "未完成"):
                scope.capture_trigger(inst, args)
        finally:
            scope.time.sleep = old_sleep

    def test_completed_capture_without_edge_cannot_export(self):
        class NoEdgeInstrument(FakeInstrument):
            def query(self, command, timeout=None):
                if command == "CHANnel1:DATA?":
                    return ",".join("0" for _ in range(32))
                return super().query(command, timeout)

        inst = NoEdgeInstrument()
        inst.state = "COMP"
        args = type("Args", (), {"trig_slope": "rise", "trig_mode": "normal",
                                   "trig_level": 0.5, "trig_source": "CH1",
                                   "trig_wait": 1.0})()
        old_sleep = scope.time.sleep
        scope.time.sleep = lambda _: None
        try:
            with self.assertRaisesRegex(scope.ScpiError, "未跨越"):
                scope.capture_trigger(inst, args)
        finally:
            scope.time.sleep = old_sleep

    def test_restore_reapplies_depth_and_running_state(self):
        inst = FakeInstrument()
        inst.points = 10000
        snap = {"acquire_points": "20000", "state": "RUN"}
        scope.restore_acq(inst, snap)
        self.assertEqual(inst.points, 20000)
        self.assertEqual(inst.state, "RUN")

    def test_restore_accepts_bre_as_frozen_state(self):
        class BreInstrument(FakeInstrument):
            def query(self, command, timeout=None):
                if command == "ACQuire:STATe?":
                    return "BRE"
                return super().query(command, timeout)

        scope.restore_acq(BreInstrument(), {"state": "STOP"})

    def test_continuous_restore_keeps_data_tuned_axes_and_runs_auto(self):
        inst = FakeInstrument()
        snap = {"state": "RUN", "trig_mode": "NORM", "timebase": "0.00074",
                "position": "0.0005", "ch1_scale": "50", "ch1_offset": "1",
                "acquire_points": "20000"}
        scope.restore_acq(inst, snap, preserve_continuous_display=True)
        self.assertIn("TRIGger:A:MODE AUTO", inst.writes)
        self.assertEqual(inst.writes[-1], "RUNContinous")
        self.assertFalse(any(command.startswith(("TIMebase:", "CHANnel1:SCALe ",
                                                 "CHANnel1:OFFSet "))
                             for command in inst.writes))

    def test_restore_failure_is_reported(self):
        inst = FakeInstrument()
        inst.fail_command = "ACQuire:POINts 20000"
        with self.assertRaisesRegex(scope.ScpiError, "还原示波器设置失败"):
            scope.restore_acq(inst, {"acquire_points": "20000", "state": "RUN"})

    def test_stability_gate_waits_ten_periods(self):
        inst = FakeInstrument()
        values = [float((i // 50) % 2) for i in range(600)]
        wave = {"values": values, "dt_s": 0.001, "t_start_s": 0.0,
                "t_stop_s": 0.599, "vpp_v": 1.0, "mean_v": 0.5,
                "min_v": 0.0, "max_v": 1.0}
        clock = [0.0]
        old_sleep, old_monotonic, old_read = (scope.time.sleep,
                                               scope.time.monotonic,
                                               scope.read_channel)
        scope.time.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        scope.time.monotonic = lambda: clock[0]
        scope.read_channel = lambda _inst, _ch: wave
        args = type("Args", (), {"stable_periods": 10})()
        try:
            result = scope.stabilize_waveform(inst, 1, 0.5, "rise", args)
            self.assertIs(result, wave)
            self.assertGreaterEqual(clock[0], 1.0)
        finally:
            scope.time.sleep = old_sleep
            scope.time.monotonic = old_monotonic
            scope.read_channel = old_read

    def test_continuous_stability_never_enables_normal_trigger(self):
        inst = FakeInstrument()
        values = [float((i // 50) % 2) for i in range(600)]
        wave = {"values": values, "dt_s": 0.001, "t_start_s": 0.0,
                "t_stop_s": 0.599, "vpp_v": 1.0, "mean_v": 0.5,
                "min_v": 0.0, "max_v": 1.0}
        clock = [0.0]
        old_sleep, old_monotonic, old_read = (scope.time.sleep,
                                               scope.time.monotonic,
                                               scope.read_channel)
        scope.time.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        scope.time.monotonic = lambda: clock[0]
        scope.read_channel = lambda _inst, _ch: wave
        args = type("Args", (), {"stable_periods": 10, "settle": 1.0})()
        try:
            result = scope.stabilize_waveform(
                inst, 1, 0.5, "rise", args, acquisition_mode="continuous")
            self.assertIs(result, wave)
            self.assertIn("TRIGger:A:MODE AUTO", inst.writes)
            self.assertIn("RUNContinous", inst.writes)
            self.assertFalse(any("NORM" in command or "TRIGger:A:LEVel " in command
                                 for command in inst.writes))
        finally:
            scope.time.sleep = old_sleep
            scope.time.monotonic = old_monotonic
            scope.read_channel = old_read

    def test_observation_detects_delayed_mean_jump_from_numbers(self):
        rows = [
            {"elapsed_s": 4.8, "mean_v": 1.0, "vpp_v": 2.0, "frequency_hz": 1000.0},
            {"elapsed_s": 5.3, "mean_v": 1.5, "vpp_v": 2.0, "frequency_hz": 1000.0},
        ]
        self.assertEqual(scope.observation_events(rows), [
            {"between_s": [4.8, 5.3], "changed": ["mean_v"]}
        ])

    def test_stability_rejects_mean_jump_with_same_frequency_and_amplitude(self):
        before = {"cross": True, "freq": 1000.0, "n_edges": 10,
                  "vpp": 2.0, "mean": 1.0, "phase": None}
        after = dict(before, mean=1.5)
        self.assertFalse(scope.signature_is_stable(after, before, require_phase=False))

    def test_optional_screenshot_failure_cannot_block_numeric_export(self):
        args = argparse.Namespace(capture="both", trig_slope="rise", trig_wait=1.0,
                                  settle=0.6, ip="127.0.0.1", port=5025)
        exported = []
        old_shot, old_csv = scope.cmd_shot, scope.cmd_csv
        scope.cmd_shot = lambda *_: (_ for _ in ()).throw(RuntimeError("image failed"))
        scope.cmd_csv = lambda *_: exported.append(True)
        try:
            scope.export_capture(FakeInstrument(), args, 1, "out", "record", 1.0, "COMP")
            self.assertEqual(exported, [True])
        finally:
            scope.cmd_shot, scope.cmd_csv = old_shot, old_csv


if __name__ == "__main__":
    unittest.main()
