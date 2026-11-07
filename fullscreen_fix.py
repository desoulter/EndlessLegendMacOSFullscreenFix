#!/usr/bin/env python3
"""Build and install Endless Legend's native macOS fullscreen fix from source."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import struct
import subprocess
import sys
import tempfile

VERSION = "1.1.0"
# Both releases use the same file manifest and original-backup layout.
COMPATIBLE_STATE_VERSIONS = ("1.0.0", VERSION)
BASE_EXE_SHA = "588bf58e87809a226a4d1a822d4c4f7b0c509617c7d4e39fd4b828ff6df4f6c5"
PLAYER_SHA = "98102951d913ce37b3be2c1cba0529cf31439cb2e274ecafb5685fe4ecb3d5ef"
LIB_NAME = b"@executable_path/../Plugins/libELContextFixNative.dylib\0"
EXE = "EndlessLegend.app/Contents/MacOS/EndlessLegend"
SETTINGS = "EndlessLegend.app/Contents/Resources/Data/globalgamemanagers"
NATIVE = "EndlessLegend.app/Contents/Plugins/libELContextFixNative.dylib"
INFO = "EndlessLegend.app/Contents/Info.plist"
STATE = "ContextFix/install-state.json"
OPTIONS = "ContextFix/runtime-options.json"
PACKAGE = Path(__file__).resolve().parent
SOURCE = PACKAGE / "src/ELContextFixNative.m"
BUILD = PACKAGE / ".build"
FILE_ORDER = (EXE, SETTINGS, INFO, NATIVE)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def patch_executable(data):
    require(sha(data) == BASE_EXE_SHA, "Unsupported or already modified Unity executable")
    result = bytearray(data)
    require(data[:4] == b"\xca\xfe\xba\xbe", "Expected a fat Mach-O executable")
    count = struct.unpack_from(">I", data, 4)[0]
    slices = [struct.unpack_from(">5I", data, 8 + 20 * i) for i in range(count)]
    matches = [s for s in slices if s[0] == 0x01000007]
    require(len(matches) == 1, "Expected one x86_64 slice")
    _, _, base, length, _ = matches[0]
    require(base + length <= len(data), "Invalid Mach-O slice")
    require(struct.unpack_from("<I", data, base)[0] == 0xfeedfacf, "Expected Mach-O 64")
    ncmds, size = struct.unpack_from("<2I", data, base + 16)
    first_section = length
    cursor = base + 32
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<2I", data, cursor)
        require(cmdsize >= 8 and cursor + cmdsize <= base + 32 + size,
                "Invalid Mach-O load command")
        if cmd == 0x19:  # LC_SEGMENT_64
            sections = struct.unpack_from("<I", data, cursor + 64)[0]
            for index in range(sections):
                offset = struct.unpack_from("<I", data, cursor + 72 + 80 * index + 48)[0]
                if offset:
                    first_section = min(first_section, offset)
        cursor += cmdsize
    require(cursor == base + 32 + size, "Incorrect Mach-O command size")
    command_size = (24 + len(LIB_NAME) + 7) & ~7
    require(32 + size + command_size <= first_section, "Insufficient Mach-O header padding")
    require(data[cursor:cursor + command_size] == bytes(command_size), "Header padding is not empty")
    command = struct.pack("<6I", 0xc, command_size, 24, 0, 0x10000, 0x10000) + LIB_NAME
    command += bytes(command_size - len(command))
    result[cursor:cursor + command_size] = command
    struct.pack_into("<2I", result, base + 16, ncmds + 1, size + command_size)
    # Code, section offsets, and the i386 slice retain their exact bytes.
    return bytes(result)


def player_object(data):
    """Locate PlayerSettings in the native Unity 5.3 format-15 object table."""
    metadata, filesize, version, data_offset = struct.unpack_from(">4I", data)
    require(version == 15 and data[16] == 0 and filesize == len(data), "Unsupported Unity asset header")
    end = 20 + metadata
    require(20 < end <= data_offset <= len(data), "Invalid Unity metadata boundaries")
    cursor = 20

    def take(size):
        nonlocal cursor
        require(0 <= size <= end - cursor, "Truncated Unity metadata")
        value = data[cursor:cursor + size]
        cursor += size
        return value

    def integer():
        return struct.unpack("<i", take(4))[0]

    unity_end = data.index(b"\0", cursor, min(end, cursor + 65))
    require(data[cursor:unity_end].startswith(b"5.3."), "Expected Unity 5.3")
    cursor = unity_end + 1
    require(integer() == 2, "Expected macOS PlayerSettings")
    tree = take(1)[0]
    require(tree == 0, "Unexpected Unity type tree")
    types = integer()
    require(0 <= types <= (end - cursor) // 20, "Invalid Unity type count")
    for _ in range(types):
        cls = integer()
        if cls < 0 or cls == 114:
            take(16)
        take(16)
    objects = integer()
    require(0 <= objects <= (end - cursor) // 25, "Invalid Unity object count")
    matches = []
    for _ in range(objects):
        take((-cursor) % 4)
        path_id, offset, size, _, cls, _, _ = struct.unpack("<qIIihhB", take(25))
        if cls == 129:
            require(path_id == 1 and size == 337 and data_offset + offset + size <= len(data),
                    "Unsupported PlayerSettings object")
            matches.append((data_offset + offset, size))
    require(len(matches) == 1, "Expected one PlayerSettings object")
    return matches[0]


def patch_settings(data):
    start, length = player_object(data)
    normalized = bytearray(data[start:start + length])
    # These three fields were varied during research; all other PlayerSettings
    # bytes must match the inspected Unity build. Preserve threading as supplied.
    normalized[0xb0:0xb4] = bytes(4)
    normalized[0xa6] = 0
    normalized[0x80] = 1
    require(sha(normalized) == PLAYER_SHA, "Unsupported PlayerSettings layout/content")
    require(data[start + 0x80] == 1, "Restore the original threaded-renderer setting first")
    result = bytearray(data)
    result[start + 0xb0:start + 0xb4] = (2).to_bytes(4, "little")
    result[start + 0xa6] = 1
    return bytes(result)


def atomic_write(path, data, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".tmp-", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode if mode is not None else (path.stat().st_mode & 0o777) if path.exists() else 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def patch_info(data):
    info = plistlib.loads(data)
    require(info.get("CFBundleExecutable") == "EndlessLegend" and
            info.get("UnityBuildNumber") == "ef59fdfbd254", "Unexpected game Info.plist")
    info["NSHighResolutionCapable"] = True
    return plistlib.dumps(info, sort_keys=False)


def validate_native(data):
    require(len(data) >= 32 and data[:4] == b"\xcf\xfa\xed\xfe",
            "Expected a thin 64-bit Mach-O native helper")
    cpu, _, filetype = struct.unpack_from("<3I", data, 4)
    require(cpu == 0x01000007 and filetype == 6,
            "The native helper must be an x86_64 dynamic library")
    count, size = struct.unpack_from("<2I", data, 16)
    end = 32 + size
    require(end <= len(data), "Truncated native helper load commands")
    cursor, uuids = 32, 0
    for _ in range(count):
        require(cursor + 8 <= end, "Truncated native helper load command")
        command, length = struct.unpack_from("<2I", data, cursor)
        require(length >= 8 and length % 8 == 0 and cursor + length <= end,
                "Invalid native helper load command")
        if command == 0x1b:  # LC_UUID: required when dyld loads this helper under Rosetta.
            require(length == 24 and any(data[cursor + 8:cursor + 24]),
                    "Invalid native helper LC_UUID")
            uuids += 1
        cursor += length
    require(cursor == end, "Incorrect native helper load command size")
    require(uuids == 1, "Native helper requires one LC_UUID load command for macOS/Rosetta. "
            "Uninstall the existing fix and reinstall it from the corrected source")


def build_native():
    """Compile the tracked Objective-C source; no prebuilt payload is required."""
    require(sys.platform == "darwin", "Building the native helper requires macOS")
    source_hash = sha(SOURCE.read_bytes())
    BUILD.mkdir(parents=True, exist_ok=True)
    flags = ["-arch", "x86_64", "-fobjc-arc", "-dynamiclib", "-Wall", "-Wextra",
             "-Wno-unused-parameter", "-Wno-deprecated-declarations",
             "-mmacosx-version-min=10.13",
             "-install_name", LIB_NAME.rstrip(b"\0").decode(),
             "-framework", "AppKit", "-framework", "OpenGL"]
    fd, name = tempfile.mkstemp(prefix=".native-", suffix=".dylib", dir=str(BUILD))
    os.close(fd)
    temporary = Path(name)
    try:
        result = subprocess.run(
            ["xcrun", "--sdk", "macosx", "clang", *flags,
             "src/ELContextFixNative.m", "-o", str(temporary)],
            cwd=str(PACKAGE), capture_output=True, text=True)
        require(result.returncode == 0,
                "Native build failed. Install Apple's Command Line Tools with "
                "'xcode-select --install'.\n" + result.stdout + result.stderr)
        data = temporary.read_bytes()
        validate_native(data)
        require(sha(SOURCE.read_bytes()) == source_hash,
                "Native source changed during compilation; run the build again")
        metadata = dict(version=VERSION, source_sha256=source_hash,
                        binary_sha256=sha(data), architecture="x86_64", flags=flags)
        atomic_write(BUILD / "libELContextFixNative.dylib", data, 0o755)
        atomic_write(BUILD / "build.json", (json.dumps(metadata, indent=2) + "\n").encode())
        print("Built x86_64 helper from source: " + str(BUILD / "libELContextFixNative.dylib"))
        return data, metadata
    finally:
        temporary.unlink(missing_ok=True)


def backup_name(relative):
    return "ContextFix/backup/" + Path(relative).name


def valid_digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def load_state(root):
    state = json.loads((root / STATE).read_text())
    require(isinstance(state, dict) and state.get("version") in COMPATIBLE_STATE_VERSIONS,
            "Unsupported installation state. Remove experimental or other versions "
            "with the installer that created them before installing this release")
    records = state.get("files")
    require(isinstance(records, list) and len(records) == len(FILE_ORDER) and
            all(isinstance(item, dict) for item in records), "Invalid installation manifest")
    require({item.get("path") for item in records} == set(FILE_ORDER),
            "Unexpected paths in installation manifest")
    for item in records:
        relative = item["path"]
        require(valid_digest(item.get("installed_sha256")), "Invalid installed file checksum")
        require(type(item.get("original_mode")) is int and 0 <= item["original_mode"] <= 0o7777,
                "Invalid original file permissions")
        if relative == NATIVE:
            require(item.get("backup") is None and item.get("original_sha256") is None,
                    "Unexpected native-helper backup")
        else:
            require(item.get("backup") == backup_name(relative) and
                    valid_digest(item.get("original_sha256")), "Unexpected backup path or checksum")
    require(next(item for item in records if item["path"] == EXE)["original_sha256"] == BASE_EXE_SHA,
            "Manifest belongs to an unsupported Unity executable")
    return state


def verify_installed(root, state):
    healthy = True
    for item in state["files"]:
        path = root / item["path"]
        valid = path.is_file() and sha(path.read_bytes()) == item["installed_sha256"]
        print(("OK " if valid else "CHANGED/MISSING ") + item["path"])
        healthy &= valid
        if item["backup"]:
            backup = root / item["backup"]
            valid = backup.is_file() and sha(backup.read_bytes()) == item["original_sha256"]
            if not valid:
                print("CHANGED/MISSING backup: " + item["backup"])
            healthy &= valid
    return healthy


def status(root):
    if not (root / STATE).exists():
        print("No registered Fullscreen Fix installation.")
        return False
    state = load_state(root)
    print("Fullscreen Fix " + state["version"])
    healthy = verify_installed(root, state)
    if healthy:
        validate_native((root / NATIVE).read_bytes())
    print("Frame cap: " + str(read_frame_options(root)["fps_cap"]) + " (0 disables; applied on game startup)")
    return healthy


def frame_options(fps_cap):
    require(type(fps_cap) is int and (fps_cap == 0 or 15 <= fps_cap <= 240),
            "FPS cap must be an integer: 0 disables, or 15–240")
    return dict(schema=1, fps_cap=fps_cap)


def read_frame_options(root):
    path = root / OPTIONS
    if not path.exists():
        return frame_options(0)
    value = json.loads(path.read_text())
    require(isinstance(value, dict) and value.get("schema") == 1 and set(value) == {"schema", "fps_cap"},
            "Unknown runtime options; refusing to replace them")
    return frame_options(value.get("fps_cap"))


def encoded_frame_options(root, fps_cap):
    read_frame_options(root)  # Validate an existing preferences file first.
    return (json.dumps(frame_options(fps_cap), indent=2) + "\n").encode()


def configure(root, fps_cap):
    options = encoded_frame_options(root, fps_cap)
    state = load_state(root)
    require(verify_installed(root, state), "Installed files or backups changed; refusing to configure")
    require(state.get("source_sha256") == sha(SOURCE.read_bytes()),
            "Update the installed helper from this source before configuring the frame cap")
    write_transaction(root, {OPTIONS: options}, {})
    print("Frame cap set to " + str(fps_cap) + ". Restart the game to apply; 0 disables the cap.")


def write_transaction(root, changes, modes):
    """Rollback ordinary write failures, including a failed manifest write."""
    before = {relative: (root / relative).read_bytes() if (root / relative).exists() else None
              for relative in changes}
    permissions = {relative: (root / relative).stat().st_mode & 0o7777
                   for relative in changes if before[relative] is not None}
    try:
        for relative, data in changes.items():
            path = root / relative
            if data is None:
                path.unlink()
            else:
                atomic_write(path, data, modes.get(relative))
    except Exception:
        for relative, data in before.items():
            path = root / relative
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, data, permissions[relative])
        raise


def install(root, dry_run=False, fps_cap=None):
    options = encoded_frame_options(root, fps_cap) if fps_cap is not None else None
    if (root / STATE).exists():
        state = load_state(root)
        require(verify_installed(root, state), "Installed files or backups changed; refusing to overwrite")
        validate_native((root / NATIVE).read_bytes())
        require(state.get("source_sha256") == sha(SOURCE.read_bytes()),
                "Installed helper was built from different source. Run update to rebuild the installed helper")
        if options is not None and not dry_run:
            configure(root, fps_cap)
        print("Fullscreen Fix " + VERSION + " is already installed.")
        return
    require(not (root / NATIVE).exists(), "Unregistered native helper exists; remove its previous installation first")
    originals = {relative: (root / relative).read_bytes() for relative in (EXE, SETTINGS, INFO)}
    changed = {EXE: patch_executable(originals[EXE]),
               SETTINGS: patch_settings(originals[SETTINGS]), INFO: patch_info(originals[INFO])}
    # Check every backup collision before changing or building anything.
    for relative, data in originals.items():
        backup = root / backup_name(relative)
        require(not backup.exists() or backup.read_bytes() == data,
                "Existing original backup differs: " + str(backup))
    if dry_run:
        print("Compatibility validated. Installation would compile the helper, "
              "add its load command, enable native fullscreen/Retina, and save original backups. "
              "No game files changed; compiler availability was not tested.")
        return
    native, metadata = build_native()
    validate_native(native)
    require(metadata.get("source_sha256") == sha(SOURCE.read_bytes()) and
            metadata.get("binary_sha256") == sha(native), "Build metadata mismatch")
    changed[NATIVE] = native
    modes = {relative: (root / relative).stat().st_mode & 0o7777 for relative in originals}
    modes[NATIVE] = 0o755
    records = []
    for relative in FILE_ORDER:
        old = originals.get(relative)
        backup = backup_name(relative) if old is not None else None
        if backup and not (root / backup).exists():
            atomic_write(root / backup, old, modes[relative])
        records.append(dict(path=relative, backup=backup,
                            original_sha256=sha(old) if old is not None else None,
                            installed_sha256=sha(changed[relative]),
                            original_mode=modes.get(relative, 0o755)))
    state = dict(version=VERSION, source_sha256=metadata["source_sha256"], files=records)
    # Load the dependency first; activate the executable's new load command last.
    transaction = {relative: changed[relative] for relative in (NATIVE, INFO, SETTINGS, EXE)}
    if options is not None:
        transaction[OPTIONS] = options
    transaction[STATE] = (json.dumps(state, indent=2) + "\n").encode()
    write_transaction(root, transaction, modes)
    print("Fullscreen Fix " + VERSION + " installed. Start the game through Steam as usual.")


def update(root, fps_cap=None):
    """Update only the helper of an intact, supported installation, atomically."""
    read_frame_options(root)
    options = encoded_frame_options(root, fps_cap) if fps_cap is not None else None
    state = load_state(root)
    require(verify_installed(root, state), "Installed files or backups changed; refusing to update")
    original = (root / backup_name(EXE)).read_bytes()
    require(patch_executable(original) == (root / EXE).read_bytes(), "Unsupported installed player")
    native, metadata = build_native()
    validate_native(native)
    require(metadata.get("source_sha256") == sha(SOURCE.read_bytes()) and
            metadata.get("binary_sha256") == sha(native), "Build metadata mismatch")
    revised = json.loads(json.dumps(state))
    revised["version"] = VERSION
    revised["source_sha256"] = metadata["source_sha256"]
    next(item for item in revised["files"] if item["path"] == NATIVE)["installed_sha256"] = sha(native)
    changes = {NATIVE: native}
    if options is not None:
        changes[OPTIONS] = options
    changes[STATE] = (json.dumps(revised, indent=2) + "\n").encode()
    write_transaction(root, changes, {NATIVE: (root / NATIVE).stat().st_mode & 0o7777})
    print("Fullscreen helper updated; original backups and other game files preserved.")
    print("Frame cap: " + str(read_frame_options(root)["fps_cap"]) + ". Start the game through Steam.")


def uninstall(root):
    require((root / STATE).exists(), "No registered installation")
    state = load_state(root)
    require(verify_installed(root, state), "Installed files or backups changed; refusing to overwrite")
    by_path = {item["path"]: item for item in state["files"]}
    changes, modes = {}, {}
    # Restore the executable before removing the library that it loads.
    for relative in FILE_ORDER:
        item = by_path[relative]
        changes[relative] = None if item["backup"] is None else (root / item["backup"]).read_bytes()
        modes[relative] = item["original_mode"]
    changes[STATE] = None
    write_transaction(root, changes, modes)
    print("Original executable, settings, Info.plist and permissions restored; "
          "native helper removed; original backups retained.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("action", choices=("build", "install", "update", "configure", "status", "uninstall"))
    parser.add_argument("--game-dir", type=Path, help="Folder containing EndlessLegend.app")
    parser.add_argument("--dry-run", action="store_true", help="Validate installation without changing game files")
    parser.add_argument("--fps-cap", type=int, help="Optional frame cap: 0 disables, or 15–240; applies after restart")
    args = parser.parse_args(argv)
    default = PACKAGE.parent if (PACKAGE.parent / "EndlessLegend.app").exists() else Path.home() / "Library/Application Support/Steam/steamapps/common/Endless Legend"
    root = (args.game_dir or default).expanduser().resolve()
    try:
        require(not args.dry_run or args.action == "install", "--dry-run is only supported for install")
        require(args.fps_cap is None or args.action in ("install", "update", "configure"),
                "--fps-cap is supported for install, update and configure")
        if args.action == "build":
            build_native()
        else:
            require((root / EXE).exists(), "EndlessLegend.app not found; provide --game-dir")
            if args.action == "install":
                install(root, args.dry_run, args.fps_cap)
            elif args.action == "update":
                update(root, args.fps_cap)
            elif args.action == "configure":
                require(args.fps_cap is not None, "configure requires --fps-cap")
                configure(root, args.fps_cap)
            elif args.action == "uninstall":
                uninstall(root)
            else:
                return 0 if status(root) else 1
    except (ValueError, OSError, KeyError, TypeError, struct.error, subprocess.SubprocessError) as error:
        parser.exit(1, "Error: " + str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
