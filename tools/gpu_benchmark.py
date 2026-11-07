#!/usr/bin/env python3
"""Record and compare manual Endless Legend GPU/temperature runs on macOS."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import select
import signal
import statistics
import subprocess
import sys
import threading
import time

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "tools/telemetry.m"
HELPER = REPO / ".build/el-telemetry"
RESULTS = REPO / "benchmark-results"
EXECUTABLE = "EndlessLegend.app/Contents/MacOS/EndlessLegend"
NATIVE = "EndlessLegend.app/Contents/Plugins/libELContextFixNative.dylib"
BASE_SHA = "588bf58e87809a226a4d1a822d4c4f7b0c509617c7d4e39fd4b828ff6df4f6c5"
PROBE_FIELDS = ["present_fps", "present_flush_ms", "probe_query_ms", "render_width", "render_height",
                "present_viewport_width", "present_viewport_height", "surface_samples", "swap_interval",
                "msaa_fbo_width", "msaa_fbo_height", "msaa_fbo_samples", "quality_msaa", "quality_vsync",
                "target_fps_script", "target_fps_engine", "present_error", "probe_age_s"]
PROBE_FIELDS += ["native_fps_cap", "effective_fps_cap"]
FIELDS = [
    "timestamp_utc", "elapsed_s", "game_elapsed_s", "game_pid", "game_active", "game_cpu_pct",
    "game_rss_mib", "gpu_util_pct", "gpu_renderer_pct", "gpu_tiler_pct", "gpu_memory_mib",
    "gpu_temp_mean_c", "gpu_temp_max_c", "cpu_temp_mean_c", "cpu_temp_max_c",
    "fan0_rpm", "fan1_rpm", "thermal_state", "collection_ms", "power_age_s",
    "gpu_power_w", "cpu_power_w", "gpu_frequency_mhz", "gpu_active_pct",
    "display_pixel_width", "display_pixel_height", "display_refresh_hz",
    "power_source",
    "measurement_elapsed_s", "measurement_phase",
] + PROBE_FIELDS


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def default_game():
    parent = REPO.parent
    return parent if (parent / EXECUTABLE).is_file() else Path.home() / "Library/Application Support/Steam/steamapps/common/Endless Legend"


def build_helper():
    if sys.platform != "darwin":
        raise ValueError("Recording requires macOS on Apple Silicon")
    HELPER.parent.mkdir(exist_ok=True)
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    stamp = HELPER.with_suffix(".sha256")
    if HELPER.is_file() and stamp.is_file() and stamp.read_text().strip() == digest:
        return HELPER
    temporary = HELPER.with_name(HELPER.name + ".tmp-" + str(os.getpid()))
    try:
        command = ["xcrun", "--sdk", "macosx", "clang", "-arch", "arm64", "-O2", "-fobjc-arc", "-Wall", "-Wextra",
                   "-framework", "AppKit", "-framework", "IOKit", "-framework", "CoreGraphics",
                   str(SOURCE), "-o", str(temporary)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise ValueError("Telemetry build failed. Install Apple Command Line Tools.\n" + result.stderr)
        os.replace(temporary, HELPER)
        stamp.write_text(digest + "\n")
    finally:
        temporary.unlink(missing_ok=True)
    return HELPER


def helper_command(args, count=None):
    command = [str(build_helper()), "--interval", str(args.interval), "--game-executable",
               str(args.game_dir.resolve() / EXECUTABLE)]
    if count is not None:
        command += ["--count", str(count)]
    return ["sudo", "-n", "--", *command] if args.sudo_sensors else command


def patch_snapshot(game):
    state_path = game / "ContextFix/install-state.json"
    native_present = (game / NATIVE).is_file()
    executable = game / EXECUTABLE
    result = {"manifest_present": state_path.is_file(), "native_present": native_present}
    result["executable_sha256"] = hashlib.sha256(executable.read_bytes()).hexdigest() if executable.is_file() else None
    effective_sha = result["executable_sha256"]
    probe_state = game / "ContextFix/render-probe/state.json"
    if probe_state.is_file():
        # A verified diagnostic overlay changes only the dependency table. Use
        # its byte-exact backup for the underlying fullscreen-state check.
        try:
            probe = json.loads(probe_state.read_text())
            valid = probe.get("schema") == 1
            for relative, key in [
                (EXECUTABLE, "installed_executable_sha256"),
                ("ContextFix/render-probe/executable.before-probe", "original_executable_sha256"),
                ("EndlessLegend.app/Contents/Plugins/libELRenderProbe.dylib", "library_sha256")]:
                path = game / relative
                valid = valid and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == probe.get(key)
            config = json.loads((game / "ContextFix/render-probe/config.json").read_text())
            valid = valid and config == probe.get("config")
            result["render_probe"] = dict(valid=valid, config=config)
            if valid:
                effective_sha = probe["original_executable_sha256"]
                result["base_executable_sha256"] = effective_sha
            else:
                effective_sha = None
        except (OSError, ValueError, KeyError, TypeError):
            result["render_probe"] = dict(valid=False)
            effective_sha = None
    result["mode"] = "original" if not native_present and effective_sha == BASE_SHA else "unknown"
    options = game / "ContextFix/runtime-options.json"
    if options.is_file():
        try:
            result["runtime_options"] = json.loads(options.read_text())
        except (OSError, ValueError):
            result["runtime_options"] = {"invalid": True}
    if state_path.is_file():
        state = json.loads(state_path.read_text())
        allowed = {EXECUTABLE, NATIVE, "EndlessLegend.app/Contents/Info.plist",
                   "EndlessLegend.app/Contents/Resources/Data/globalgamemanagers"}
        records = state.get("files", [])
        healthy = isinstance(records, list) and len(records) == 4
        if healthy:
            healthy = {item.get("path") for item in records if isinstance(item, dict)} == allowed
        if healthy:
            healthy = all((game / item["path"]).is_file() and
                          (effective_sha if item["path"] == EXECUTABLE else
                           hashlib.sha256((game / item["path"]).read_bytes()).hexdigest()) == item.get("installed_sha256")
                          for item in records)
        result.update(version=state.get("version"), manifest_hashes_match=healthy)
        if healthy and native_present:
            result["mode"] = "patched"
    return result


def video_settings():
    import xml.etree.ElementTree as ET
    home = Path.home()
    for parent in [home / "Library/Application Support/Endless Legend/Users", home / "Documents/Endless Legend/Users"]:
        if not parent.exists():
            continue
        for path in sorted(parent.glob("*/Registry.xml")):
            try:
                root = ET.parse(path).getroot()
                video = root.find("./Settings/Video")
                if video is not None:
                    return {item.tag: item.text for item in video}
            except (OSError, ET.ParseError):
                continue
    return {}


def read_metadata(args):
    result = {"schema": 1, "label": args.label, "started_utc": utc(), "interval_s": args.interval,
              "macos": platform.mac_ver()[0], "architecture": platform.machine(),
              "patch": patch_snapshot(args.game_dir), "video_settings_start": video_settings(),
              "power_requested": args.power, "sudo_sensors": args.sudo_sensors,
              "notes": args.notes, "status": "recording",
              "metrics_scope": "GPU utilization/temperature/fans/power are system-wide, not game-exclusive",
              "temperature_definition": "mean/max of valid SMC Tg* readings; all individual keys are retained"}
    try:
        result["power_configuration"] = subprocess.run(["pmset", "-g", "custom"], capture_output=True,
                                                       text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def stop_process(process):
    if process.poll() is not None:
        return
    try:
        process.send_signal(signal.SIGTERM)
    except PermissionError:
        # A sudo monitor normally forwards the signal. If needed, use the same
        # cached sudo authorization solely to stop the child we just launched.
        subprocess.run(["sudo", "-n", "/bin/kill", "-TERM", str(process.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except PermissionError:
            subprocess.run(["sudo", "-n", "/bin/kill", "-KILL", str(process.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        process.wait(timeout=5)


def parse_power(text):
    result = {}
    for label, key in [("GPU Power", "gpu_power_w"), ("CPU Power", "cpu_power_w")]:
        match = re.search(r"(?m)^\s*" + label + r":\s*([\d.]+)\s*(mW|W)\b", text)
        if match:
            value = float(match[1]) / (1000 if match[2] == "mW" else 1)
            if number(value):
                result[key] = value
    for label, key, unit in [("GPU HW active frequency", "gpu_frequency_mhz", "MHz"),
                             ("GPU HW active residency", "gpu_active_pct", "%")]:
        match = re.search(re.escape(label) + r":\s*([\d.]+)\s*" + unit, text)
        if match and number(float(match[1])):
            result[key] = float(match[1])
    return result


class PowerReader:
    def __init__(self, folder, interval, duration):
        command = ["sudo", "-n", "--", "/usr/bin/powermetrics", "--samplers", "cpu_power,gpu_power,thermal",
                   "-i", str(round(interval * 1000)), "-n", str(math.ceil(duration / interval) + 5),
                   "-f", "text", "-b", "1"]
        self.error = (folder / "powermetrics.stderr.txt").open("w")
        self.raw = (folder / "powermetrics.txt").open("w")
        self.normalized = (folder / "power.jsonl").open("w")
        self.latest = None
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=self.error,
                                        text=True, bufsize=1)
        self.thread = threading.Thread(target=self.read, daemon=True)
        self.thread.start()

    def read(self):
        block = []
        observed = None
        observed_utc = None

        def finish():
            value = parse_power("".join(block))
            if value:
                value.update(observed_monotonic_s=observed or time.monotonic(), timestamp_utc=observed_utc or utc())
                self.latest = value
                self.normalized.write(json.dumps(value) + "\n")
                self.normalized.flush()

        try:
            for line in self.process.stdout:
                self.raw.write(line)
                self.raw.flush()
                if "*** Sampled system activity" in line:
                    finish()
                    block = []
                    observed = time.monotonic()
                    observed_utc = utc()
                block.append(line)
            finish()
        except (OSError, ValueError):
            pass

    def sample(self, interval):
        value = self.latest
        if value is None:
            return {}
        age = time.monotonic() - value["observed_monotonic_s"]
        if age > max(5, interval * 4):
            return {"power_age_s": round(age, 3)}
        return {key: val for key, val in dict(value, power_age_s=round(age, 3)).items()
                if key in FIELDS}

    def close(self):
        stop_process(self.process)
        self.thread.join(timeout=3)
        self.process.stdout.close()
        self.raw.close()
        self.normalized.close()
        self.error.close()


class RenderProbeReader:
    """Read complete observer lines for the actual game PID; never reuse stale FPS."""
    def __init__(self, game, output):
        self.directory = game / "ContextFix/render-probe/logs"
        self.output = output
        self.offsets = {}
        self.latest = {}
        self.frames_seen = 0
        self.invalid_frames = 0
        self.markers = {}

    def sample(self, pid, now=None):
        if not pid:
            return {}
        path = self.directory / (str(pid) + ".jsonl")
        if not path.is_file():
            return {}
        offset = self.offsets.get(pid, 0)
        if path.stat().st_size < offset:
            offset = 0
            self.latest.pop(pid, None)
        with path.open("rb") as stream:
            stream.seek(offset)
            while True:
                before = stream.tell()
                line = stream.readline()
                if not line or not line.endswith(b"\n"):
                    self.offsets[pid] = before
                    break
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                # Keep allocation/context events as well as FPS in the run folder.
                with (self.output / ("render-probe-" + str(pid) + ".jsonl")).open("ab") as saved:
                    saved.write(line)
                if row.get("kind") == "measurement_marker" and row.get("pid") == pid and \
                        number(row.get("timestamp_epoch_s")):
                    self.markers.setdefault(pid, row)
                if row.get("kind") == "frame" and row.get("pid") == pid and number(row.get("timestamp_epoch_s")):
                    self.latest[pid] = row
                    self.frames_seen += 1
                    if row.get("msaa_fbo") and (row.get("msaa_fbo_width", 0) <= 0 or
                                                row.get("msaa_fbo_height", 0) <= 0):
                        self.invalid_frames += 1
        latest = self.latest.get(pid)
        if latest is None:
            return {}
        age = (time.time() if now is None else now) - latest["timestamp_epoch_s"]
        if not -1 <= age <= 4:
            return {"probe_age_s": round(age, 3)}
        return dict({key: latest[key] for key in PROBE_FIELDS if key in latest}, probe_age_s=round(age, 3))


class MeasurementWindow:
    """Align each run to its own in-game marker, independent of loading time."""
    def __init__(self, duration, warmup=30, interval=1):
        self.duration = duration
        self.warmup = warmup
        self.interval = interval
        self.marker = None
        self.last_game_time = None

    def arm(self, marker, row):
        if self.marker is not None or not row.get("game_pid") or not number(row.get("game_elapsed_s")):
            return False
        # The marker and the Python row share wall time. Translate onto this
        # run's game-relative clock; do not mix Python and helper monotonic bases.
        age = datetime.fromisoformat(row["timestamp_utc"]).timestamp() - marker["timestamp_epoch_s"]
        if not -1 <= age <= 5:
            return False
        self.marker = row["game_elapsed_s"] - age
        return True

    def apply(self, row):
        if self.marker is None or not number(row.get("game_elapsed_s")):
            return
        elapsed = row["game_elapsed_s"] - self.marker - self.warmup
        row["measurement_elapsed_s"] = round(elapsed, 3)
        row["measurement_phase"] = "warmup" if elapsed < 0 else "measure" if elapsed < self.duration else "complete"
        if row.get("game_pid"):
            self.last_game_time = elapsed

    def summary(self):
        # One-second polling cannot include the exact endpoint. Permit the final
        # two sample intervals so exiting at the completion beep is not rejected.
        complete = self.last_game_time is not None and self.last_game_time >= self.duration - 2 * self.interval
        return dict(duration_s=self.duration, warmup_s=self.warmup, marker="F8",
                    marker_game_elapsed_s=self.marker,
                    start_game_elapsed_s=None if self.marker is None else self.marker + self.warmup,
                    completed=complete)


def archive_game_logs(folder, started_utc):
    """Keep the scene/loading evidence before the next launch replaces Player.log."""
    home = Path.home()
    candidates = [home / "Library/Logs/Unity/Player.log"]
    temporary = home / "Library/Application Support/Endless Legend/Temporary Files"
    if temporary.is_dir():
        candidates += list(temporary.glob("Diagnostics*.html"))
    cutoff = datetime.fromisoformat(started_utc).timestamp() - 5
    saved = []
    for source in candidates:
        try:
            if source.is_file() and source.stat().st_mtime >= cutoff:
                target = folder / ("Player.log" if source.name == "Player.log" else "game-diagnostics.html")
                # Prefer the newest diagnostics if a startup handoff produced two.
                if target.exists() and source.stat().st_mtime < target.stat().st_mtime:
                    continue
                target.write_bytes(source.read_bytes())
                os.utime(target, (source.stat().st_atime, source.stat().st_mtime))
                saved.append(target.name)
        except OSError:
            continue
    return sorted(set(saved))


def flatten(sample, started, game_started, previous):
    now = sample["monotonic_s"]
    row = {key: sample.get(key) for key in FIELDS}
    row.update(timestamp_utc=utc(), elapsed_s=round(now - started, 3),
               game_elapsed_s=round(now - game_started, 3) if game_started is not None else None)
    if number(sample.get("game_cpu_time_ns")) and previous and sample.get("game_pid") == previous.get("game_pid"):
        dt = now - previous["monotonic_s"]
        delta = sample["game_cpu_time_ns"] - previous.get("game_cpu_time_ns", sample["game_cpu_time_ns"])
        if dt > 0 and delta >= 0:
            row["game_cpu_pct"] = round(delta / (dt * 1e9) * 100, 3)
    for source, target in [("game_rss_bytes", "game_rss_mib"), ("gpu_memory_bytes", "gpu_memory_mib")]:
        if number(sample.get(source)):
            row[target] = round(sample[source] / 1024 ** 2, 3)
    fans = sample.get("fans_rpm", {})
    row["fan0_rpm"], row["fan1_rpm"] = fans.get("F0Ac"), fans.get("F1Ac")
    return row


def record(args):
    command = helper_command(args)
    metadata = read_metadata(args)
    if args.label in ("patched", "original") and metadata["patch"]["mode"] != args.label:
        raise ValueError("Label does not match detected patch state: " + json.dumps(metadata["patch"]) +
                         ". Install/remove the fullscreen patch yourself before this run")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = args.output or RESULTS / (args.label + "-" + stamp)
    folder = folder.expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=False)
    dump(folder / "metadata.json", metadata)
    process = power = None
    started = game_started = exited = previous = None
    count = 0
    deadline = time.monotonic() + args.duration
    error = None
    probe = RenderProbeReader(args.game_dir, folder)
    measure = MeasurementWindow(args.measure, getattr(args, "warmup", 30), args.interval) \
        if getattr(args, "measure", None) else None
    measure_announced = finished_announced = False
    print("Run folder: " + str(folder), flush=True)
    print("Launch the game manually. Quit it to stop logging after 15 seconds, or press Ctrl+C.", flush=True)
    if measure:
        print("Load the same tutorial checkpoint and camera. Press F8 (Fn+F8 on Mac) in the game: "
              "{}s warmup, then {}s measurement. A second beep marks completion; then quit the game.".format(
                  measure.warmup, measure.duration), flush=True)
    try:
        if args.power:
            power = PowerReader(folder, args.interval, args.duration)
        with (folder / "telemetry.stderr.txt").open("w") as errors, \
             (folder / "samples.jsonl").open("w") as raw, \
             (folder / "samples.csv").open("w", newline="") as output, \
             (folder / "events.jsonl").open("w") as events:
            writer = csv.DictWriter(output, fieldnames=FIELDS)
            writer.writeheader()
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, text=True, bufsize=1)

            def event(name, **data):
                item = dict(timestamp_utc=utc(), event=name, **data)
                events.write(json.dumps(item) + "\n")
                events.flush()

            event("logger_started")
            while time.monotonic() < deadline:
                if power and power.process.poll() is not None:
                    raise ValueError("powermetrics stopped. Run 'sudo -v' in Terminal before --power. "
                                     "See powermetrics.stderr.txt")
                ready, _, _ = select.select([process.stdout], [], [], 0.5)
                if not ready:
                    if process.poll() is not None:
                        break
                    continue
                line = process.stdout.readline()
                if not line:
                    break
                sample = json.loads(line)
                if sample.get("kind") == "capabilities":
                    metadata["capabilities"] = sample
                    dump(folder / "metadata.json", metadata)
                    for warning in sample.get("warnings", []):
                        print("NOTE: " + warning, flush=True)
                    continue
                if sample.get("kind") != "sample":
                    continue
                if started is None:
                    started = sample["monotonic_s"]
                    if not number(sample.get("gpu_temp_max_c")) and not args.allow_missing_temperature:
                        raise ValueError("No GPU temperature sensor is readable. Run 'doctor' in a normal Terminal. "
                                         "If necessary, run 'sudo -v' and retry with --sudo-sensors. "
                                         "--allow-missing-temperature is for diagnostic captures only")
                    if not number(sample.get("gpu_util_pct")):
                        raise ValueError("GPU utilization is unavailable; see telemetry.stderr.txt")
                if sample.get("game_pid"):
                    if game_started is None:
                        game_started = sample["monotonic_s"]
                        event("game_started", pid=sample["game_pid"], elapsed_s=sample["monotonic_s"] - started)
                        print("Game detected: recording gameplay interval.", flush=True)
                    exited = None
                elif game_started is not None and exited is None:
                    exited = time.monotonic()
                    event("game_stopped", elapsed_s=sample["monotonic_s"] - started)
                    print("Game closed. Recording 15 seconds of cooldown.", flush=True)
                row = flatten(sample, started, game_started, previous)
                if power:
                    row.update(power.sample(args.interval))
                row.update(probe.sample(sample.get("game_pid")))
                if measure:
                    marker = probe.markers.get(sample.get("game_pid"))
                    if marker and measure.arm(marker, row):
                        event("measurement_marked", game_elapsed_s=measure.marker, warmup_s=measure.warmup,
                              duration_s=measure.duration)
                        print("F8 received. Stabilizing the tutorial scene...", flush=True)
                    measure.apply(row)
                    if row.get("measurement_phase") == "measure" and not measure_announced:
                        event("measurement_started", game_elapsed_s=measure.marker + measure.warmup)
                        measure_announced = True
                        print("MEASUREMENT STARTED: keep the same camera and do not interact.", flush=True)
                    if row.get("measurement_phase") == "complete" and not finished_announced:
                        event("measurement_completed", game_elapsed_s=measure.marker + measure.warmup + measure.duration)
                        finished_announced = True
                        print("MEASUREMENT COMPLETE. You can quit the game.", flush=True)
                raw.write(json.dumps(dict(sample, **row), allow_nan=False) + "\n")
                raw.flush()
                writer.writerow(row)
                output.flush()
                previous = sample
                count += 1
                if count == 1 or count % max(1, round(15 / args.interval)) == 0:
                    print("{:.0f}s  GPU={} %  GPU max={} C  fans={}/{} rpm  game={}".format(
                        row["elapsed_s"], row.get("gpu_util_pct"), row.get("gpu_temp_max_c"),
                        row.get("fan0_rpm"), row.get("fan1_rpm"), row.get("game_pid")), flush=True)
                    if metadata["patch"].get("render_probe", {}).get("valid") and row.get("game_pid"):
                        print("  present={} fps  render={}x{}  MSAA FBO/surface={}/{}  engine target={}".format(
                            round(row["present_fps"], 1) if number(row.get("present_fps")) else "pending",
                            row.get("render_width"), row.get("render_height"), row.get("msaa_fbo_samples"),
                            row.get("surface_samples"), row.get("target_fps_engine")), flush=True)
                if exited is not None and not args.keep_running and time.monotonic() - exited >= 15:
                    break
            if process.poll() not in (None, 0):
                raise ValueError("Telemetry helper stopped unexpectedly; see telemetry.stderr.txt")
            event("logger_stopped", samples=count)
    except KeyboardInterrupt:
        print("\nStopping and saving the run...", flush=True)
    except Exception as exception:
        error = exception
    finally:
        try:
            if process:
                stop_process(process)
                process.stdout.close()
            if power:
                power.close()
        except Exception as exception:
            if error is None:
                error = exception
        finally:
            metadata.update(ended_utc=utc(), samples=count, video_settings_end=video_settings(),
                            status="failed" if error else "complete", game_seen=game_started is not None,
                            render_probe_frames_seen=probe.frames_seen,
                            render_probe_invalid_frames=probe.invalid_frames,
                            comparison_valid=not probe.invalid_frames)
            if measure:
                metadata["measurement"] = measure.summary()
                metadata["comparison_valid"] &= metadata["measurement"]["completed"]
            if game_started is not None and metadata.get("started_utc"):
                metadata["archived_game_logs"] = archive_game_logs(folder, metadata["started_utc"])
            if error:
                metadata["error"] = str(error)
            dump(folder / "metadata.json", metadata)
    if error:
        raise error
    print("Saved {} samples: {}".format(count, folder), flush=True)
    if game_started is None:
        print("NOTE: No game process was detected; this run cannot be used as a gameplay comparison.")
    if metadata["patch"].get("render_probe", {}).get("valid") and not probe.frames_seen:
        print("NOTE: Rendering observer produced no FPS samples; rendering diagnostics are incomplete.")
    if probe.invalid_frames:
        print("INVALID RENDERING: a nonzero MSAA framebuffer had a zero-sized attachment. "
              "This run is excluded from comparisons; its diagnostic logs are retained.")
    if measure and not measure.summary()["completed"]:
        print("INCOMPLETE MEASUREMENT: no F8 marker or the game exited before the selected measurement finished.")


def doctor(args):
    result = subprocess.run(helper_command(args, 1), capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise ValueError("Telemetry helper failed:\n" + result.stderr)
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    capabilities = next(row for row in rows if row["kind"] == "capabilities")
    sample = next(row for row in rows if row["kind"] == "sample")
    print(json.dumps({"patch": patch_snapshot(args.game_dir), "capabilities": capabilities, "sample": sample}, indent=2))
    if not number(sample.get("gpu_util_pct")) or not number(sample.get("gpu_temp_max_c")):
        raise ValueError("Required GPU utilization/temperature is missing. Test in normal Terminal; "
                         "for SMC access errors try 'sudo -v' followed by doctor --sudo-sensors")
    print("READY: GPU utilization and GPU temperatures are available.")


def load_run(folder):
    folder = folder.expanduser().resolve()
    metadata = json.loads((folder / "metadata.json").read_text())
    rows = [json.loads(line) for line in (folder / "samples.jsonl").read_text().splitlines() if line.strip()]
    return {"folder": str(folder), "metadata": metadata, "rows": rows}


def latest_run(label):
    candidates = []
    if RESULTS.exists():
        for folder in RESULTS.iterdir():
            try:
                metadata = json.loads((folder / "metadata.json").read_text())
                matches = metadata.get("label") == label or (
                    metadata.get("label", "").startswith(label + "-probe") and
                    metadata.get("patch", {}).get("mode") == label and
                    metadata.get("patch", {}).get("render_probe", {}).get("valid"))
                if matches and metadata.get("status") == "complete" and metadata.get("game_seen") and \
                        metadata.get("comparison_valid") is not False:
                    candidates.append((metadata["started_utc"], folder))
            except (OSError, ValueError, KeyError):
                continue
    if not candidates:
        raise ValueError("No completed gameplay run for " + label + "; supply both run folders explicitly")
    return max(candidates)[1]


def metric_summary(rows, field, start=0, end=None):
    values = [row[field] for row in rows if row.get("game_pid") and number(row.get("game_elapsed_s")) and
              row["game_elapsed_s"] >= start and (end is None or row["game_elapsed_s"] <= end) and
              row.get("game_active") is not False and number(row.get(field))]
    return {"n": len(values), "mean": statistics.mean(values) if values else None,
            "max": max(values) if values else None}


def compare(args):
    runs = [load_run(args.patched or latest_run("patched")), load_run(args.original or latest_run("original"))]
    for run in runs:
        if run["metadata"].get("comparison_valid") is False:
            raise ValueError("Invalid rendering or incomplete measurement in " + run["folder"] +
                             "; this run cannot be used for comparison")
        if not any(row.get("game_pid") and number(row.get("game_elapsed_s")) for row in run["rows"]):
            raise ValueError("No detected gameplay in " + run["folder"])
    marked = [bool(run["metadata"].get("measurement")) for run in runs]
    if any(marked) and not all(marked):
        raise ValueError("A marked tutorial run must be compared with another marked tutorial run")
    if all(marked):
        for run in runs:
            measurement = run["metadata"]["measurement"]
            if not measurement.get("completed"):
                raise ValueError("Incomplete marked measurement in " + run["folder"])
            run["rows"] = [dict(row, process_game_elapsed_s=row.get("game_elapsed_s"),
                                game_elapsed_s=row["measurement_elapsed_s"])
                           for row in run["rows"] if number(row.get("measurement_elapsed_s")) and
                           0 <= row["measurement_elapsed_s"] < measurement["duration_s"]]
    if args.end is not None:
        end = args.end
    elif all(marked):
        end = min(run["metadata"]["measurement"]["duration_s"] for run in runs)
    else:
        end = min(max(row["game_elapsed_s"] for row in run["rows"] if row.get("game_pid") and
                      number(row.get("game_elapsed_s"))) for run in runs)
    if end <= args.start:
        raise ValueError("The selected window has no overlapping gameplay time")
    output = (args.output or RESULTS / "comparison.html").expanduser().resolve()
    if output.exists() or output.with_suffix(".json").exists():
        raise ValueError("Comparison output already exists; choose another --output path")
    output.parent.mkdir(parents=True, exist_ok=True)
    template = (REPO / "tools/benchmark_report.html").read_text()
    data = json.dumps({"runs": runs, "start": args.start, "end": end,
                       "time_basis": "measurement" if all(marked) else "game"}, ensure_ascii=False, allow_nan=False)
    output.write_text(template.replace("__BENCHMARK_DATA__", data.replace("<", "\\u003c")))
    summaries = {name: {field: metric_summary(run["rows"], field, args.start, end)
                        for field in ["gpu_util_pct", "gpu_temp_mean_c", "gpu_temp_max_c", "fan0_rpm", "fan1_rpm",
                                      "gpu_power_w", "cpu_power_w", "game_cpu_pct", "collection_ms",
                                      "present_fps", "present_flush_ms", "probe_query_ms", "surface_samples",
                                      "msaa_fbo_samples", "target_fps_engine", "gpu_frequency_mhz"]}
                 for name, run in zip(["patched", "original"], runs)}
    dump(output.with_suffix(".json"), {"window_start_s": args.start, "window_end_s": end,
                                     "time_basis": "measurement" if all(marked) else "game",
                                     "summaries": summaries, "run_folders": [run["folder"] for run in runs]})
    print("Comparison: " + str(output))
    print("Machine-readable summary: " + str(output.with_suffix(".json")))
    for name, metrics in summaries.items():
        print(name + ": " + json.dumps(metrics, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("record", "doctor"):
        action = actions.add_parser(name)
        action.add_argument("--game-dir", type=Path, default=default_game())
        action.add_argument("--interval", type=float, default=1)
        action.add_argument("--sudo-sensors", action="store_true", help="Read SMC via a sudo child after sudo -v")
        if name == "record":
            action.add_argument("--label", required=True, help="Use patched or original for the two real runs")
            action.add_argument("--duration", type=float, default=1200, help="Maximum recording duration in seconds")
            action.add_argument("--power", action="store_true", help="Also run powermetrics after sudo -v")
            action.add_argument("--output", type=Path)
            action.add_argument("--notes", default="")
            action.add_argument("--keep-running", action="store_true", help="Do not stop 15 seconds after the game exits")
            action.add_argument("--allow-missing-temperature", action="store_true", help="Diagnostic capture only")
    action = actions.add_parser("compare")
    action.add_argument("patched", nargs="?", type=Path)
    action.add_argument("original", nargs="?", type=Path)
    action.add_argument("--start", type=float, default=0, help="Seconds after game detection")
    action.add_argument("--end", type=float)
    action.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action in ("record", "doctor"):
            if not number(args.interval) or not 0.2 <= args.interval <= 60:
                raise ValueError("Interval must be between 0.2 and 60 seconds")
        if args.action == "record":
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", args.label):
                raise ValueError("Use a short label containing letters, digits, underscores or hyphens")
            if not number(args.duration) or args.duration <= 0:
                raise ValueError("Duration must be positive")
            record(args)
        elif args.action == "doctor":
            doctor(args)
        else:
            if not number(args.start) or args.start < 0 or (args.end is not None and
                    (not number(args.end) or args.end <= args.start)):
                raise ValueError("Invalid comparison time window")
            compare(args)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, "Error: " + str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
