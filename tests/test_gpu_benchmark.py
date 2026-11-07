import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gpu_benchmark", ROOT / "tools/gpu_benchmark.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="el-benchmark-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def make_run(self, name, values):
        folder = self.root / name
        folder.mkdir()
        bench.dump(folder / "metadata.json", {"label": name, "status": "complete", "game_seen": True,
                   "started_utc": "2026-11-06T12:00:00Z", "interval_s": 1,
                   "notes": "</script><script>alert(1)</script>"})
        rows = [dict(game_elapsed_s=i, game_pid=123, game_active=True, gpu_util_pct=value)
                for i, value in enumerate(values)]
        (folder / "samples.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        return folder

    def compare_args(self, patched, original, start=0):
        return argparse.Namespace(patched=patched, original=original, start=start, end=None,
                                  output=self.root / "comparison.html")

    def test_power_units_frequency_and_residency(self):
        result = bench.parse_power("GPU Power: 2500 mW\nCPU Power: 3.5 W\n"
                                   "GPU HW active frequency: 1234 MHz\nGPU HW active residency: 45.6% (P1: 1%)")
        self.assertEqual(result, dict(gpu_power_w=2.5, cpu_power_w=3.5,
                                     gpu_frequency_mhz=1234, gpu_active_pct=45.6))

    def test_missing_power_is_not_reported_as_zero(self):
        self.assertEqual(bench.parse_power("Thermal pressure: Nominal\n"), {})
        self.assertEqual(bench.parse_power("GPU Power: 0 mW\n"), {"gpu_power_w": 0})

    def test_cpu_delta_and_zero_fan_reading_are_preserved(self):
        old = dict(monotonic_s=10, game_pid=44, game_cpu_time_ns=2_000_000_000)
        sample = dict(monotonic_s=12, game_pid=44, game_cpu_time_ns=5_000_000_000,
                      gpu_memory_bytes=1024 ** 2, fans_rpm={"F0Ac": 0, "F1Ac": 2000})
        row = bench.flatten(sample, 5, 10, old)
        self.assertEqual(row["game_cpu_pct"], 150)
        self.assertEqual(row["game_elapsed_s"], 2)
        self.assertEqual(row["elapsed_s"], 7)
        self.assertEqual(row["gpu_memory_mib"], 1)
        self.assertEqual(row["fan0_rpm"], 0)
        self.assertIsNone(row["gpu_temp_max_c"])

    def test_cpu_counter_does_not_cross_process_restart(self):
        previous = dict(monotonic_s=10, game_pid=44, game_cpu_time_ns=5_000_000_000)
        sample = dict(monotonic_s=11, game_pid=45, game_cpu_time_ns=1_000_000_000)
        self.assertIsNone(bench.flatten(sample, 0, 0, previous)["game_cpu_pct"])

    def test_summary_excludes_idle_background_missing_and_outside_window(self):
        rows = [dict(game_pid=1, game_elapsed_s=1, game_active=True, metric=0),
                dict(game_pid=1, game_elapsed_s=2, game_active=True, metric=40),
                dict(game_pid=1, game_elapsed_s=2, game_active=False, metric=100),
                dict(game_pid=None, game_elapsed_s=2, metric=100),
                dict(game_pid=1, game_elapsed_s=3, metric=None),
                dict(game_pid=1, game_elapsed_s=9, metric=100)]
        self.assertEqual(bench.metric_summary(rows, "metric", 1, 3), dict(n=2, mean=20, max=40))

    def test_comparison_uses_overlapping_window_and_escapes_script_content(self):
        patched = self.make_run("patched", [10, 30, 50, 90])
        original = self.make_run("original", [0, 20, 40])
        args = self.compare_args(patched, original, 1)
        with contextlib.redirect_stdout(io.StringIO()):
            bench.compare(args)
        summary = json.loads(args.output.with_suffix(".json").read_text())
        self.assertEqual(summary["window_end_s"], 2)
        self.assertEqual(summary["summaries"]["patched"]["gpu_util_pct"], dict(n=2, mean=40, max=50))
        self.assertEqual(summary["summaries"]["original"]["gpu_util_pct"], dict(n=2, mean=30, max=40))
        html = args.output.read_text()
        self.assertNotIn("__BENCHMARK_DATA__", html)
        self.assertNotIn("</script><script>alert", html)
        with self.assertRaisesRegex(ValueError, "already exists"):
            bench.compare(args)

    def test_comparison_rejects_a_run_without_a_game(self):
        patched = self.make_run("patched", [20, 30])
        original = self.make_run("original", [10, 20])
        (original / "samples.jsonl").write_text(json.dumps(dict(game_pid=None, elapsed_s=1)) + "\n")
        with self.assertRaisesRegex(ValueError, "No detected gameplay"):
            bench.compare(self.compare_args(patched, original))

    def test_zero_sized_framebuffer_run_is_rejected_and_skipped_by_latest_selection(self):
        patched = self.make_run("patched", [20, 30])
        original = self.make_run("original", [10, 20])
        metadata = json.loads((patched / "metadata.json").read_text())
        metadata["comparison_valid"] = False
        bench.dump(patched / "metadata.json", metadata)
        with self.assertRaisesRegex(ValueError, "Invalid rendering"):
            bench.compare(self.compare_args(patched, original))
        with patch.object(bench, "RESULTS", self.root):
            self.assertEqual(bench.latest_run("original"), original)
            with self.assertRaisesRegex(ValueError, "No completed gameplay run"):
                bench.latest_run("patched")

    def test_patch_state_does_not_follow_manifest_path_redirection(self):
        game = self.root / "game"
        executable = game / bench.EXECUTABLE
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"original")
        with patch.object(bench, "BASE_SHA", hashlib.sha256(b"original").hexdigest()):
            self.assertEqual(bench.patch_snapshot(game)["mode"], "original")
            state = game / "ContextFix/install-state.json"
            state.parent.mkdir()
            bench.dump(state, {"files": [{"path": "../../outside"}]})
            self.assertFalse(bench.patch_snapshot(game)["manifest_hashes_match"])

    def test_marked_window_translates_marker_time_and_rejects_an_early_exit(self):
        phase = bench.MeasurementWindow(180, warmup=30, interval=1)
        row = dict(game_pid=123, game_elapsed_s=100,
                   timestamp_utc=datetime.fromtimestamp(1000, timezone.utc).isoformat())
        self.assertTrue(phase.arm(dict(timestamp_epoch_s=999.5), row))
        phase.apply(row)
        self.assertEqual(row["measurement_elapsed_s"], -29.5)
        self.assertEqual(row["measurement_phase"], "warmup")
        started = dict(game_pid=123, game_elapsed_s=130)
        phase.apply(started)
        self.assertEqual(started["measurement_phase"], "measure")
        self.assertEqual(started["measurement_elapsed_s"], 0.5)
        phase.apply(dict(game_pid=None, game_elapsed_s=1000))
        self.assertFalse(phase.summary()["completed"])
        phase.apply(dict(game_pid=123, game_elapsed_s=309))
        self.assertTrue(phase.summary()["completed"])
        self.assertFalse(phase.arm(dict(timestamp_epoch_s=1000), row))

    def test_marked_comparison_aligns_different_load_times_and_excludes_warmup(self):
        patched = self.make_run("patched", [1])
        original = self.make_run("original", [1])
        for folder, offset, values in [(patched, 100, [10, 30]), (original, 500, [0, 20])]:
            meta = json.loads((folder / "metadata.json").read_text())
            meta["measurement"] = dict(duration_s=2, completed=True)
            bench.dump(folder / "metadata.json", meta)
            rows = [dict(game_pid=123, game_active=True, game_elapsed_s=offset - 10,
                         measurement_elapsed_s=-10, gpu_util_pct=999)]
            rows += [dict(game_pid=123, game_active=True, game_elapsed_s=offset + i,
                          measurement_elapsed_s=i, gpu_util_pct=value) for i, value in enumerate(values)]
            (folder / "samples.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        args = self.compare_args(patched, original)
        with contextlib.redirect_stdout(io.StringIO()):
            bench.compare(args)
        summary = json.loads(args.output.with_suffix(".json").read_text())
        self.assertEqual(summary["time_basis"], "measurement")
        self.assertEqual(summary["summaries"]["patched"]["gpu_util_pct"]["mean"], 20)
        self.assertEqual(summary["summaries"]["original"]["gpu_util_pct"]["mean"], 10)
        meta = json.loads((original / "metadata.json").read_text())
        meta.pop("measurement")
        bench.dump(original / "metadata.json", meta)
        with self.assertRaisesRegex(ValueError, "another marked tutorial run"):
            bench.compare(args)

    def record_args(self):
        return argparse.Namespace(label="test", game_dir=self.root, interval=1, duration=10,
                                  sudo_sensors=False, power=False, output=self.root / "run", notes="",
                                  allow_missing_temperature=False, keep_running=False)

    def test_record_saves_events_and_sensor_data_and_finishes_cleanly(self):
        args = self.record_args()
        rows = [{"kind": "capabilities", "warnings": [], "temperature_keys": ["Tg01"]}]
        for index, pid in enumerate([None, 55, 55, None]):
            rows.append(dict(kind="sample", monotonic_s=100 + index, game_pid=pid,
                             game_active=pid is not None, gpu_temp_max_c=60 + index,
                             gpu_util_pct=index * 10, temperatures_c={"Tg01": 60 + index},
                             game_cpu_time_ns=index * 1_000_000_000))
        program = "import sys; sys.stdout.write(" + repr("".join(json.dumps(r) + "\n" for r in rows)) + ")"
        with patch.object(bench, "helper_command", return_value=[sys.executable, "-u", "-c", program]), \
             patch.object(bench, "read_metadata", return_value={"patch": {"mode": "patched"}}), \
             patch.object(bench, "video_settings", return_value={}), contextlib.redirect_stdout(io.StringIO()):
            bench.record(args)
        metadata = json.loads((args.output / "metadata.json").read_text())
        self.assertEqual(metadata["status"], "complete")
        self.assertTrue(metadata["game_seen"])
        self.assertEqual(metadata["samples"], 4)
        saved = [json.loads(line) for line in (args.output / "samples.jsonl").read_text().splitlines()]
        self.assertEqual(saved[2]["game_elapsed_s"], 1)
        self.assertEqual(saved[2]["game_cpu_pct"], 100)
        events = [json.loads(line)["event"] for line in (args.output / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events, ["logger_started", "game_started", "game_stopped", "logger_stopped"])

    def test_missing_temperature_fails_explicitly_instead_of_recording_zero(self):
        args = self.record_args()
        line = json.dumps(dict(kind="sample", monotonic_s=1, game_pid=None, gpu_util_pct=10,
                               gpu_temp_max_c=None)) + "\n"
        program = "print(" + repr(line) + ", end='')"
        with patch.object(bench, "helper_command", return_value=[sys.executable, "-u", "-c", program]), \
             patch.object(bench, "read_metadata", return_value={"patch": {"mode": "patched"}}), \
             patch.object(bench, "video_settings", return_value={}), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "No GPU temperature"):
                bench.record(args)
        metadata = json.loads((args.output / "metadata.json").read_text())
        self.assertEqual(metadata["status"], "failed")
        self.assertEqual(metadata["samples"], 0)


if __name__ == "__main__":
    unittest.main()
