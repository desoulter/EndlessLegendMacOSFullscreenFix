#!/usr/bin/env python3
"""Record temporary rendering diagnostics, restoring the observer after the run."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
EXE = "EndlessLegend.app/Contents/MacOS/EndlessLegend"
LIBRARY = "EndlessLegend.app/Contents/Plugins/libELRenderProbe.dylib"
STATE = "ContextFix/render-probe/state.json"
BACKUP = "ContextFix/render-probe/executable.before-probe"
CONFIG = "ContextFix/render-probe/config.json"
DEPENDENCY = b"@executable_path/../Plugins/libELRenderProbe.dylib\0"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


fix = module("fullscreen_fix_for_probe", REPO / "fullscreen_fix.py")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def add_dependency(data):
    """Add only a load command to the already validated Intel player slice."""
    require(data[:4] == b"\xca\xfe\xba\xbe", "Expected the supported fat Mach-O player")
    count = struct.unpack_from(">I", data, 4)[0]
    require(0 < count <= 8, "Invalid Mach-O slice count")
    slices = [struct.unpack_from(">5I", data, 8 + 20 * i) for i in range(count)]
    matches = [item for item in slices if item[0] == 0x01000007]
    require(len(matches) == 1, "Expected one x86_64 slice")
    _, _, base, length, _ = matches[0]
    require(base + length <= len(data), "Truncated player slice")
    require(struct.unpack_from("<I", data, base)[0] == 0xfeedfacf, "Expected Mach-O 64")
    commands, size = struct.unpack_from("<2I", data, base + 16)
    cursor, first = base + 32, length
    require(cursor + size <= base + length, "Invalid load-command range")
    for _ in range(commands):
        kind, n = struct.unpack_from("<2I", data, cursor)
        require(n >= 8 and cursor + n <= base + 32 + size, "Invalid load command")
        if kind in (0xc, 0x80000018, 0x8000001f, 0x20, 0x80000023):
            require(DEPENDENCY.rstrip(b"\0") not in data[cursor:cursor + n], "Observer dependency already exists")
        if kind == 0x19:
            require(n >= 72, "Invalid segment")
            sections = struct.unpack_from("<I", data, cursor + 64)[0]
            require(72 + 80 * sections <= n, "Invalid section table")
            for index in range(sections):
                offset = struct.unpack_from("<I", data, cursor + 72 + index * 80 + 48)[0]
                if offset:
                    first = min(first, offset)
        cursor += n
    require(cursor == base + 32 + size, "Inconsistent command size")
    extra = (24 + len(DEPENDENCY) + 7) & ~7
    require(32 + size + extra <= first, "Insufficient empty Mach-O header space")
    require(data[cursor:cursor + extra] == bytes(extra), "Mach-O header space is not empty")
    command = struct.pack("<6I", 0xc, extra, 24, 0, 0x10000, 0x10000) + DEPENDENCY
    result = bytearray(data)
    result[cursor:cursor + extra] = command.ljust(extra, b"\0")
    struct.pack_into("<2I", result, base + 16, commands + 1, size + extra)
    return bytes(result)


def base_mode(root):
    data = (root / EXE).read_bytes()
    if sha(data) == fix.BASE_EXE_SHA and not (root / fix.STATE).exists() and not (root / fix.NATIVE).exists():
        return "original"
    state = fix.load_state(root)
    require(fix.verify_installed(root, state), "Fullscreen installation is not intact")
    original = (root / fix.backup_name(EXE)).read_bytes()
    require(sha(original) == fix.BASE_EXE_SHA and fix.patch_executable(original) == data,
            "Unsupported player instructions; refusing to use Unity diagnostic offsets")
    return "patched"


def build():
    require(sys.platform == "darwin", "The observer requires macOS")
    folder = REPO / ".build"
    folder.mkdir(exist_ok=True)
    target = folder / "libELRenderProbe.dylib"
    temporary = target.with_name(target.name + ".tmp-" + str(os.getpid()))
    try:
        subprocess.run(["xcrun", "--sdk", "macosx", "clang", "-arch", "x86_64", "-O2", "-fobjc-arc",
                        "-fno-omit-frame-pointer", "-dynamiclib", "-Wall", "-Wextra", "-Werror",
                        "-Wno-deprecated-declarations", "-mmacosx-version-min=10.13",
                        "-install_name", DEPENDENCY.rstrip(b"\0").decode(),
                        "-framework", "AppKit", "-framework", "OpenGL",
                        str(REPO / "tools/render_probe.m"), "-o", str(temporary)], check=True)
        data = temporary.read_bytes()
        fix.validate_native(data)
        os.replace(temporary, target)
        print("Built rendering observer: " + str(target))
        return data
    finally:
        temporary.unlink(missing_ok=True)


def install(root, native, msaa_cap=None, fps_cap=0, measurement_seconds=None, warmup_seconds=30):
    require(not (root / STATE).exists(), "Rendering observer already installed; use remove first")
    require(not (root / LIBRARY).exists(), "Unregistered rendering observer exists")
    require(msaa_cap in (None, 0, 2, 4), "MSAA cap must be 0, 2 or 4")
    require(fps_cap == 0 or 15 <= fps_cap <= 240, "FPS cap must be 0 or 15–240")
    require(measurement_seconds is None or 30 <= measurement_seconds <= 3600,
            "Measurement duration must be 30–3600 seconds")
    require(0 <= warmup_seconds <= 300, "Warmup must be 0–300 seconds")
    mode = base_mode(root)
    require(msaa_cap is None or mode == "patched", "MSAA experiment requires the fullscreen patch")
    fix.validate_native(native)
    before = (root / EXE).read_bytes()
    after = add_dependency(before)
    permission = (root / EXE).stat().st_mode & 0o7777
    config = dict(supported_player=True, patched_mode=mode == "patched", msaa_cap=msaa_cap, fps_cap=fps_cap)
    if measurement_seconds is not None:
        config.update(measurement_seconds=measurement_seconds, warmup_seconds=warmup_seconds)
    state = dict(schema=1, mode=mode, original_executable_sha256=sha(before),
                 installed_executable_sha256=sha(after), library_sha256=sha(native),
                 executable_mode=permission, config=config)
    encode = lambda value: (json.dumps(value, indent=2) + "\n").encode()
    # Activate the dependency last. This manifest is separate from the release
    # manifest; removing the observer restores the exact pre-probe player bytes.
    fix.write_transaction(root, {BACKUP: before, CONFIG: encode(config), LIBRARY: native,
                                STATE: encode(state), EXE: after}, {EXE: permission, LIBRARY: 0o755})
    print("Rendering observer installed over " + mode + "; configuration: " + json.dumps(config))


def verify(root):
    state = json.loads((root / STATE).read_text())
    require(state.get("schema") == 1 and state.get("mode") in ("original", "patched"), "Invalid observer state")
    for relative, key in [(EXE, "installed_executable_sha256"), (BACKUP, "original_executable_sha256"),
                          (LIBRARY, "library_sha256")]:
        require((root / relative).is_file() and sha((root / relative).read_bytes()) == state.get(key),
                "Observer file changed; refusing to overwrite: " + relative)
    require(json.loads((root / CONFIG).read_text()) == state.get("config"), "Observer configuration changed")
    return state


def remove(root):
    state = verify(root)
    permission = state.get("executable_mode")
    require(type(permission) is int and 0 <= permission <= 0o7777, "Invalid original permission")
    fix.write_transaction(root, {EXE: (root / BACKUP).read_bytes(), LIBRARY: None, STATE: None,
                                CONFIG: None}, {EXE: permission})
    print("Rendering observer removed; exact pre-probe player restored. Diagnostic logs retained.")


def record(args):
    bench = module("gpu_benchmark_for_probe", REPO / "tools/gpu_benchmark.py")
    # NSWorkspace knows the actual application path and avoids a system ps scan.
    check = argparse.Namespace(game_dir=args.game_dir, interval=1, sudo_sensors=False)
    result = subprocess.run(bench.helper_command(check, 1), capture_output=True, text=True, timeout=45, check=True)
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    sample = next(item for item in rows if item.get("kind") == "sample")
    require(not sample.get("game_pid"), "Quit Endless Legend before changing diagnostic mode")
    require(not (args.game_dir / STATE).exists(), "Observer from a previous session exists; use remove first")
    current = base_mode(args.game_dir)
    native = build()
    if args.mode != current:
        if args.mode == "original":
            fix.uninstall(args.game_dir)
        else:
            fix.install(args.game_dir)
    installed = False
    try:
        install(args.game_dir, native, args.msaa_cap, args.fps_cap, args.measure, args.warmup)
        installed = True
        label = args.mode + "-probe"
        if args.msaa_cap is not None:
            label += "-aa" + str(args.msaa_cap)
        if args.fps_cap:
            label += "-fps" + str(args.fps_cap)
        if args.measure:
            label += "-tutorial"
        recording = argparse.Namespace(label=label, interval=1, game_dir=args.game_dir,
                                      sudo_sensors=args.sudo_sensors, power=args.power, duration=args.duration,
                                      output=None, notes="Render probe: " + json.dumps(dict(msaa_cap=args.msaa_cap,
                                      fps_cap=args.fps_cap)), keep_running=False, allow_missing_temperature=False,
                                      measure=args.measure, warmup=args.warmup)
        bench.record(recording)
    finally:
        if installed:
            # Do not remove the loaded dependency while a failed recording may
            # have left the game running. The explicit remove command recovers it.
            result = subprocess.run(bench.helper_command(check, 1), capture_output=True, text=True, timeout=45)
            samples = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            last = next((item for item in samples if item.get("kind") == "sample"), None)
            if result.returncode == 0 and last and not last.get("game_pid"):
                remove(args.game_dir)
            else:
                print("Observer retained because the game may still be running. Quit it, then run "
                      "'python3 tools/render_probe.py remove'.", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "record", "status", "remove"))
    parser.add_argument("--game-dir", type=Path, default=REPO.parent)
    parser.add_argument("--mode", choices=("original", "patched"), default="patched")
    parser.add_argument("--msaa-cap", type=int, choices=(0, 2, 4), help="Explicit experiment; default preserves MSAA")
    parser.add_argument("--fps-cap", type=int, default=0, help="Explicit pacing experiment; 0 observes unchanged")
    parser.add_argument("--measure", type=int, help="Mark a tutorial measurement with F8 in game; duration in seconds")
    parser.add_argument("--warmup", type=int, default=30, help="Seconds ignored after F8 before measuring (default 30)")
    parser.add_argument("--power", action="store_true")
    parser.add_argument("--sudo-sensors", action="store_true")
    parser.add_argument("--duration", type=float, default=1200)
    args = parser.parse_args(argv)
    args.game_dir = args.game_dir.expanduser().resolve()
    try:
        require(args.fps_cap == 0 or 15 <= args.fps_cap <= 240, "FPS cap must be 0 or 15–240")
        require(args.msaa_cap is None or args.mode == "patched", "MSAA experiment requires --mode patched")
        require(args.duration > 0, "Duration must be positive")
        require(args.measure is None or 30 <= args.measure <= 3600, "Measurement duration must be 30–3600 seconds")
        require(0 <= args.warmup <= 300, "Warmup must be 0–300 seconds")
        if args.action == "build":
            build()
        elif args.action == "record":
            record(args)
        elif args.action == "status":
            print(json.dumps(verify(args.game_dir), indent=2))
        else:
            remove(args.game_dir)
    except (ValueError, OSError, KeyError, StopIteration, struct.error, subprocess.SubprocessError) as error:
        parser.exit(1, "Error: " + str(error) + "\n")


if __name__ == "__main__":
    main()
