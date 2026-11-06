import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import fixtures

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fullscreen_fix", ROOT / "fullscreen_fix.py")
fix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fix)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="el-fullscreen-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        settings, player = fixtures.settings()
        self.originals = {fix.EXE: fixtures.executable(), fix.SETTINGS: settings, fix.INFO: fixtures.info()}
        self.constants = patch.multiple(fix, BASE_EXE_SHA=fix.sha(self.originals[fix.EXE]), PLAYER_SHA=fix.sha(player))
        self.constants.start()
        self.addCleanup(self.constants.stop)
        for relative, data in self.originals.items():
            fix.atomic_write(self.root / relative, data, 0o755 if relative == fix.EXE else 0o644)
        self.marker = self.root / "EndlessLegend.app/Contents/Resources/Data/Managed/existing-patches.dll"
        fix.atomic_write(self.marker, b"Existing audio/visual patch marker")
        native = fixtures.native()
        self.builder = patch.object(fix, "build_native", return_value=(native, dict(
            version=fix.VERSION, source_sha256=fix.sha(fix.SOURCE.read_bytes()), binary_sha256=fix.sha(native))))
        self.build = self.builder.start()
        self.addCleanup(self.builder.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def assert_originals(self):
        for relative, data in self.originals.items():
            self.assertEqual((self.root / relative).read_bytes(), data)
        self.assertEqual(self.marker.read_bytes(), b"Existing audio/visual patch marker")
        self.assertEqual((self.root / fix.EXE).stat().st_mode & 0o777, 0o755)

    def snapshot(self):
        return {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def change_state(self, action):
        path = self.root / fix.STATE
        state = json.loads(path.read_text())
        action(state)
        path.write_text(json.dumps(state))

    def test_executable_only_changes_header_and_preserves_code_and_i386_slice(self):
        old = self.originals[fix.EXE]
        new = fix.patch_executable(old)
        self.assertEqual(len(new), len(old))
        self.assertEqual(new[:0x1000], old[:0x1000])
        self.assertEqual(new[0x1200:], old[0x1200:])
        commands, size = struct.unpack_from("<2I", new, 0x1010)
        self.assertEqual(commands, 2)
        self.assertGreater(size, 152)
        self.assertIn(fix.LIB_NAME, new[0x1020:0x1200])

    def test_settings_only_change_native_fullscreen_and_resize_fields(self):
        old = self.originals[fix.SETTINGS]
        new = fix.patch_settings(old)
        start, _ = fix.player_object(old)
        self.assertEqual({i for i in range(len(old)) if old[i] != new[i]}, {start + 0xa6, start + 0xb0})
        self.assertEqual(new[-4:], b"KEEP")
        self.assertEqual(new[start + 0x80], 1)

    def test_info_preserves_other_keys(self):
        info = plistlib.loads(fix.patch_info(self.originals[fix.INFO]))
        self.assertTrue(info.pop("NSHighResolutionCapable"))
        self.assertEqual(info, plistlib.loads(self.originals[fix.INFO]))

    def test_unknown_executable_is_rejected_before_build_or_backup(self):
        (self.root / fix.EXE).write_bytes(b"unsupported executable")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            fix.install(self.root)
        self.build.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_unknown_player_layout_is_rejected(self):
        data = bytearray(self.originals[fix.SETTINGS])
        start, _ = fix.player_object(data)
        data[start + 0x30] ^= 1
        with self.assertRaises(ValueError):
            fix.patch_settings(bytes(data))

    def test_disabled_threaded_renderer_is_rejected(self):
        data = bytearray(self.originals[fix.SETTINGS])
        start, _ = fix.player_object(data)
        data[start + 0x80] = 0
        with self.assertRaises(ValueError):
            fix.patch_settings(bytes(data))

    def test_install_verify_idempotence_and_exact_uninstall(self):
        fix.install(self.root)
        self.assertTrue(fix.status(self.root))
        before = self.snapshot()
        fix.install(self.root)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.build.call_count, 1)
        fix.uninstall(self.root)
        self.assert_originals()
        self.assertFalse((self.root / fix.NATIVE).exists())
        self.assertFalse((self.root / fix.STATE).exists())

    def test_dry_run_does_not_build_or_modify_any_file(self):
        before = self.snapshot()
        fix.install(self.root, dry_run=True)
        self.build.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "ContextFix").exists())

    def test_removal_restores_each_original_permission_mode(self):
        modes = {fix.EXE: 0o751, fix.SETTINGS: 0o640, fix.INFO: 0o600}
        for relative, mode in modes.items():
            (self.root / relative).chmod(mode)
        fix.install(self.root)
        fix.uninstall(self.root)
        for relative, mode in modes.items():
            self.assertEqual((self.root / relative).stat().st_mode & 0o7777, mode)
            self.assertEqual((self.root / relative).read_bytes(), self.originals[relative])

    def test_build_failure_leaves_game_and_backups_untouched(self):
        self.build.side_effect = ValueError("compiler unavailable")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            fix.install(self.root)
        self.assertEqual(self.snapshot(), before)

    def test_helper_without_uuid_is_rejected_before_backups(self):
        native, metadata = self.build.return_value
        native = fixtures.native(with_uuid=False)
        self.build.return_value = native, dict(metadata, binary_sha256=fix.sha(native))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "LC_UUID"):
            fix.install(self.root)
        self.assertEqual(self.snapshot(), before)

    def test_registered_helper_without_uuid_is_reported_but_can_be_removed(self):
        fix.install(self.root)
        native = fixtures.native(with_uuid=False)
        (self.root / fix.NATIVE).write_bytes(native)
        self.change_state(lambda state: next(item for item in state["files"]
                          if item["path"] == fix.NATIVE).update(installed_sha256=fix.sha(native)))
        before = self.snapshot()
        for operation in (fix.status, fix.install):
            with self.assertRaisesRegex(ValueError, "LC_UUID"):
                operation(self.root)
            self.assertEqual(self.snapshot(), before)
        fix.uninstall(self.root)
        self.assert_originals()

    def test_invalid_uuid_load_commands_are_rejected(self):
        valid = fixtures.native()
        fix.validate_native(valid)
        cases = [valid[:40], valid[:40] + bytes(16)]
        malformed = bytearray(valid)
        struct.pack_into("<I", malformed, 36, 16)
        cases.append(bytes(malformed))
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                fix.validate_native(data)

    def test_conflicting_backup_is_rejected_before_build(self):
        fix.atomic_write(self.root / fix.backup_name(fix.INFO), b"different original")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            fix.install(self.root)
        self.build.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_failed_manifest_write_rolls_back_entire_installation(self):
        write, failed = fix.atomic_write, False

        def fail_once(path, data, mode=None):
            nonlocal failed
            if path == self.root / fix.STATE and not failed:
                failed = True
                raise OSError("simulated disk failure")
            write(path, data, mode)

        with patch.object(fix, "atomic_write", fail_once), self.assertRaises(OSError):
            fix.install(self.root)
        self.assert_originals()
        self.assertFalse((self.root / fix.NATIVE).exists())
        self.assertFalse((self.root / fix.STATE).exists())

    def test_failed_uninstall_rolls_back_installed_state(self):
        fix.install(self.root)
        before = self.snapshot()
        write, failed = fix.atomic_write, False

        def fail_once(path, data, mode=None):
            nonlocal failed
            if path == self.root / fix.INFO and not failed:
                failed = True
                raise OSError("simulated disk failure")
            write(path, data, mode)

        with patch.object(fix, "atomic_write", fail_once), self.assertRaises(OSError):
            fix.uninstall(self.root)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(fix.status(self.root))

    def test_external_modification_prevents_all_writes(self):
        fix.install(self.root)
        native = self.root / fix.NATIVE
        native.write_bytes(native.read_bytes() + b"external modification")
        before = self.snapshot()
        for operation in (fix.install, fix.uninstall):
            with self.assertRaises(ValueError):
                operation(self.root)
            self.assertEqual(self.snapshot(), before)

    def test_corrupt_backup_prevents_all_writes(self):
        fix.install(self.root)
        (self.root / fix.backup_name(fix.INFO)).write_bytes(b"corrupted backup")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            fix.uninstall(self.root)
        self.assertEqual(self.snapshot(), before)

    def test_missing_file_is_reported(self):
        fix.install(self.root)
        (self.root / fix.NATIVE).unlink()
        self.assertFalse(fix.status(self.root))

    def test_manifest_cannot_redirect_backup_paths(self):
        fix.install(self.root)
        self.change_state(lambda state: state["files"][0].update(backup="../outside-file"))
        before = self.snapshot()
        with self.assertRaises(ValueError):
            fix.uninstall(self.root)
        self.assertEqual(self.snapshot(), before)

    def test_manifest_cannot_redirect_installed_paths(self):
        fix.install(self.root)
        self.change_state(lambda state: state["files"][0].update(path="../outside-file"))
        before = self.snapshot()
        with self.assertRaises(ValueError):
            fix.uninstall(self.root)
        self.assertEqual(self.snapshot(), before)

    def test_experimental_version_has_no_migration_path(self):
        fix.install(self.root)
        self.change_state(lambda state: state.update(version="0.2.0"))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "Unsupported installation state"):
            fix.install(self.root)
        self.assertEqual(self.snapshot(), before)

    def test_changed_source_requires_explicit_removal(self):
        fix.install(self.root)
        self.change_state(lambda state: state.update(source_sha256="0" * 64))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "different source"):
            fix.install(self.root)
        self.assertEqual(self.snapshot(), before)

    def test_build_metadata_mismatch_is_rejected_before_backups(self):
        native, metadata = self.build.return_value
        self.build.return_value = native, dict(metadata, binary_sha256="0" * 64)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "Build metadata mismatch"):
            fix.install(self.root)
        self.assertEqual(self.snapshot(), before)

    def test_reinstall_after_removal_reuses_original_backups(self):
        fix.install(self.root)
        fix.uninstall(self.root)
        fix.install(self.root)
        self.assertTrue(fix.status(self.root))
        fix.uninstall(self.root)
        self.assert_originals()


@unittest.skipUnless(os.environ.get("EL_FULLSCREEN_GAME_DIR"), "Optional real-game fixture path not set")
class RealGameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        game = Path(os.environ["EL_FULLSCREEN_GAME_DIR"]).expanduser().resolve()
        cls.originals = {}
        for relative in (fix.EXE, fix.SETTINGS, fix.INFO):
            backup = game / fix.backup_name(relative)
            cls.originals[relative] = (backup if backup.exists() else game / relative).read_bytes()
        fix.patch_executable(cls.originals[fix.EXE])
        fix.patch_settings(cls.originals[fix.SETTINGS])
        cls.native, cls.metadata = fix.build_native()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="el-fullscreen-real-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for relative, data in self.originals.items():
            fix.atomic_write(self.root / relative, data, 0o755 if relative == fix.EXE else 0o644)
        marker = self.root / "EndlessLegend.app/Contents/Resources/Data/Managed/existing-patches.dll"
        fix.atomic_write(marker, b"Existing audio patch")
        builder = patch.object(fix, "build_native", return_value=(self.native, self.metadata))
        builder.start()
        self.addCleanup(builder.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def test_real_game_install_status_remove_and_permissions(self):
        fix.install(self.root)
        self.assertTrue(fix.status(self.root))
        self.assertEqual((self.root / fix.NATIVE).read_bytes(), self.native)
        fix.uninstall(self.root)
        for relative, data in self.originals.items():
            self.assertEqual((self.root / relative).read_bytes(), data)
        self.assertEqual((self.root / fix.EXE).stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.root / "EndlessLegend.app/Contents/Resources/Data/Managed/existing-patches.dll").read_bytes(),
                         b"Existing audio patch")

    def test_real_settings_preserve_every_unrelated_byte(self):
        old = self.originals[fix.SETTINGS]
        start, _ = fix.player_object(old)
        new = fix.patch_settings(old)
        permitted = {start + 0xa6, *range(start + 0xb0, start + 0xb4)}
        self.assertLessEqual({i for i in range(len(old)) if old[i] != new[i]}, permitted)
        self.assertEqual(new[start + 0x80], old[start + 0x80])

    def test_real_native_helper_loads_in_x86_64_process(self):
        loader = self.root / "load-native"
        result = subprocess.run(
            ["xcrun", "--sdk", "macosx", "clang", "-arch", "x86_64",
             str(ROOT / "tests/load_native.c"), "-o", str(loader)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = subprocess.run([str(loader), str(fix.BUILD / "libELContextFixNative.dylib")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("x86_64 native helper loaded", result.stdout)


if __name__ == "__main__":
    unittest.main()
