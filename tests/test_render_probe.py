import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import fixtures

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


probe = load("render_probe_tests", ROOT / "tools/render_probe.py")
bench = load("probe_bench_tests", ROOT / "tools/gpu_benchmark.py")


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="el-render-probe-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        settings, player = fixtures.settings()
        self.originals = {probe.EXE: fixtures.executable(), probe.fix.SETTINGS: settings,
                          probe.fix.INFO: fixtures.info()}
        for name, value in self.originals.items():
            probe.fix.atomic_write(self.root / name, value, 0o755 if name == probe.EXE else 0o644)
        constants = patch.multiple(probe.fix, BASE_EXE_SHA=probe.sha(self.originals[probe.EXE]),
                                   PLAYER_SHA=probe.sha(player))
        constants.start()
        self.addCleanup(constants.stop)
        base = patch.object(bench, "BASE_SHA", probe.sha(self.originals[probe.EXE]))
        base.start()
        self.addCleanup(base.stop)
        native = fixtures.native()
        builder = patch.object(probe.fix, "build_native", return_value=(native, dict(
            source_sha256=probe.sha(probe.fix.SOURCE.read_bytes()), binary_sha256=probe.sha(native))))
        builder.start()
        self.addCleanup(builder.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_overlay_preserves_instructions_and_i386_on_both_players(self):
        for before in (self.originals[probe.EXE], probe.fix.patch_executable(self.originals[probe.EXE])):
            after = probe.add_dependency(before)
            self.assertEqual(len(before), len(after))
            self.assertEqual(before[:0x1000], after[:0x1000])
            self.assertEqual(before[0x1200:], after[0x1200:])
            self.assertIn(probe.DEPENDENCY, after[0x1020:0x1200])
            with self.assertRaisesRegex(ValueError, "already exists"):
                probe.add_dependency(after)

    def test_overlay_refuses_nonempty_header_without_writes(self):
        old = bytearray(self.originals[probe.EXE])
        old[0x1000 + 32 + 152] = 1
        with self.assertRaisesRegex(ValueError, "not empty"):
            probe.add_dependency(bytes(old))

    def test_original_overlay_is_recognized_and_restores_exactly(self):
        probe.install(self.root, fixtures.native())
        self.assertEqual(bench.patch_snapshot(self.root)["mode"], "original")
        self.assertTrue(bench.patch_snapshot(self.root)["render_probe"]["valid"])
        self.assertFalse(probe.verify(self.root)["config"]["patched_mode"])
        probe.remove(self.root)
        for path, value in self.originals.items():
            self.assertEqual((self.root / path).read_bytes(), value)
        self.assertEqual((self.root / probe.EXE).stat().st_mode & 0o777, 0o755)

    def test_patched_overlay_keeps_release_manifest_and_restores_verified_installation(self):
        probe.fix.install(self.root)
        before = {n: (self.root / n).read_bytes() for n in (*probe.fix.FILE_ORDER, probe.fix.STATE)}
        probe.install(self.root, fixtures.native(), msaa_cap=0, fps_cap=60)
        state = bench.patch_snapshot(self.root)
        self.assertEqual(state["mode"], "patched")
        self.assertTrue(probe.verify(self.root)["config"]["patched_mode"])
        self.assertTrue(state["manifest_hashes_match"])
        self.assertEqual((self.root / probe.fix.STATE).read_bytes(), before[probe.fix.STATE])
        probe.remove(self.root)
        self.assertTrue(probe.fix.verify_installed(self.root, probe.fix.load_state(self.root)))
        for path, value in before.items():
            self.assertEqual((self.root / path).read_bytes(), value)

    def test_tampering_prevents_removal_and_invalidates_benchmark_state(self):
        probe.install(self.root, fixtures.native())
        (self.root / probe.LIBRARY).write_bytes(b"changed")
        before = self.snapshot()
        self.assertEqual(bench.patch_snapshot(self.root)["mode"], "unknown")
        with self.assertRaisesRegex(ValueError, "changed"):
            probe.remove(self.root)
        self.assertEqual(self.snapshot(), before)

    def test_configuration_tampering_cannot_be_mislabeled_as_observing(self):
        probe.install(self.root, fixtures.native())
        path = self.root / probe.CONFIG
        config = json.loads(path.read_text())
        config["fps_cap"] = 60
        path.write_text(json.dumps(config))
        self.assertEqual(bench.patch_snapshot(self.root)["mode"], "unknown")
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            probe.remove(self.root)

    def test_msaa_experiment_is_not_installed_over_original(self):
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "requires the fullscreen patch"):
            probe.install(self.root, fixtures.native(), msaa_cap=0)
        self.assertEqual(self.snapshot(), before)

    def test_failed_activation_rolls_back_the_original_player(self):
        real_write = probe.fix.atomic_write

        def fail_once(path, data, mode=None):
            if path == self.root / probe.EXE and data != self.originals[probe.EXE]:
                raise OSError("simulated activation failure")
            real_write(path, data, mode)

        before = self.snapshot()
        with patch.object(probe.fix, "atomic_write", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "activation failure"):
                probe.install(self.root, fixtures.native())
        self.assertEqual(self.snapshot(), before)

    def test_reader_handles_partial_lines_stale_data_and_pid_changes(self):
        output = self.root / "run"
        output.mkdir()
        directory = self.root / "ContextFix/render-probe/logs"
        directory.mkdir(parents=True)
        path = directory / "123.jsonl"
        first = json.dumps(dict(kind="context", pid=123, pixel_format_samples=4)) + "\n"
        row = json.dumps(dict(kind="frame", pid=123, timestamp_epoch_s=100, present_fps=60,
                              render_width=2048, render_height=1330, surface_samples=4)) + "\n"
        path.write_text(first + row[:20])
        reader = bench.RenderProbeReader(self.root, output)
        self.assertEqual(reader.sample(123, now=101), {})
        with path.open("a") as f:
            f.write(row[20:])
        self.assertEqual(reader.sample(123, now=101)["present_fps"], 60)
        self.assertNotIn("present_fps", reader.sample(123, now=106))
        self.assertEqual(reader.sample(456, now=101), {})
        self.assertEqual((output / "render-probe-123.jsonl").read_text(), first + row)
        self.assertEqual(reader.frames_seen, 1)

    def test_reader_detects_zero_sized_msaa_attachment_in_a_live_frame(self):
        output = self.root / "run"
        output.mkdir()
        directory = self.root / "ContextFix/render-probe/logs"
        directory.mkdir(parents=True)
        rows = [dict(kind="frame", pid=123, timestamp_epoch_s=100, present_fps=110,
                     msaa_fbo=3, msaa_fbo_width=0, msaa_fbo_height=0),
                dict(kind="frame", pid=123, timestamp_epoch_s=101, present_fps=100,
                     msaa_fbo=3, msaa_fbo_width=2048, msaa_fbo_height=1330),
                dict(kind="frame", pid=123, timestamp_epoch_s=102, present_fps=100, msaa_fbo=0)]
        (directory / "123.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        reader = bench.RenderProbeReader(self.root, output)
        reader.sample(123, now=103)
        self.assertEqual(reader.frames_seen, 3)
        self.assertEqual(reader.invalid_frames, 1)

    def test_reader_keeps_the_in_game_marker_even_before_a_frame_record(self):
        output = self.root / "run"
        output.mkdir()
        directory = self.root / "ContextFix/render-probe/logs"
        directory.mkdir(parents=True)
        marker = dict(kind="measurement_marker", pid=123, timestamp_epoch_s=100,
                      warmup_s=30, duration_s=180)
        (directory / "123.jsonl").write_text(json.dumps(marker) + "\n")
        reader = bench.RenderProbeReader(self.root, output)
        self.assertEqual(reader.sample(123, now=101), {})
        self.assertEqual(reader.markers[123], marker)
        self.assertEqual(reader.frames_seen, 0)

    @unittest.skipUnless(shutil.which("cc"), "C compiler not available")
    def test_native_policy_preserves_release_clamp_when_observer_replaces_it(self):
        # Exercise the same compiled C policy used by the observer. In the
        # failing dyld route Unity's 8-sample request reaches it directly,
        # without first passing through the release helper's 8 -> 4 clamp.
        source = self.root / "msaa-policy.c"
        source.write_text('''#include <assert.h>
#include "render_probe_msaa.h"
int main(void) {
    assert(ELProbeMSAASamples(8, 4, true, true, -1) == 4);
    assert(ELProbeMSAASamples(4, 4, true, true, -1) == 4);
    assert(ELProbeMSAASamples(2, 4, true, true, -1) == 2);
    assert(ELProbeMSAASamples(0, 4, true, true, -1) == 0);
    assert(ELProbeMSAASamples(8, 4, false, true, -1) == 8);
    assert(ELProbeMSAASamples(8, 4, true, false, -1) == 8);
    assert(ELProbeMSAASamples(8, 4, true, true, 0) == 0);
    assert(ELProbeMSAASamples(8, 4, true, true, 2) == 2);
    assert(ELProbeMSAASamples(8, 4, true, true, 4) == 4);
    assert(ELProbeMSAASamples(8, 2, true, true, 4) == 2);
    assert(ELProbeMSAASamples(8, 0, true, true, -1) == 8);
    return 0;
}
''')
        binary = self.root / "msaa-policy"
        subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(ROOT / "tools"),
                        str(source), "-o", str(binary)], check=True, capture_output=True)
        subprocess.run([str(binary)], check=True, capture_output=True)


@unittest.skipUnless(os.environ.get("EL_FULLSCREEN_GAME_DIR"), "Optional real-game fixture path not set")
class RealPlayerProbeTests(unittest.TestCase):
    def test_load_command_overlay_preserves_real_text_and_restores_original(self):
        source = Path(os.environ["EL_FULLSCREEN_GAME_DIR"]).expanduser().resolve()
        backup = source / probe.fix.backup_name(probe.EXE)
        original = (backup if backup.exists() else source / probe.EXE).read_bytes()
        self.assertEqual(probe.sha(original), probe.fix.BASE_EXE_SHA)
        with tempfile.TemporaryDirectory(prefix="el-real-probe-") as temporary:
            game = Path(temporary)
            probe.fix.atomic_write(game / probe.EXE, original, 0o755)
            probe.install(game, fixtures.native())
            self.assertEqual(bench.patch_snapshot(game)["mode"], "original")
            installed = (game / probe.EXE).read_bytes()
            # The executable is unmodified except the existing header padding
            # and ncmds/sizeofcmds; all slices retain their original size.
            count = struct.unpack_from(">I", original, 4)[0]
            slices = [struct.unpack_from(">5I", original, 8 + i * 20) for i in range(count)]
            _, _, base, _, _ = next(item for item in slices if item[0] == 0x01000007)
            commands, size = struct.unpack_from("<2I", original, base + 16)
            end = base + 32 + size
            self.assertEqual(installed[base + 32:end], original[base + 32:end])
            extra = (24 + len(probe.DEPENDENCY) + 7) & ~7
            self.assertEqual(installed[end + extra:], original[end + extra:])
            probe.remove(game)
            self.assertEqual((game / probe.EXE).read_bytes(), original)


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "Requires macOS developer tools")
class NativeInterpositionTests(unittest.TestCase):
    def test_real_observer_allocation_hook_alone_and_with_a_second_interposer(self):
        # Real x86_64 dyld routing, actual observer code and actual backtrace
        # caller recognition. A fake GL driver avoids needing a graphics session.
        with tempfile.TemporaryDirectory(prefix="el-probe-dyld-") as temporary:
            root = Path(temporary)
            backend = root / "backend.c"
            backend.write_text('''#include <OpenGL/gl.h>
#include <OpenGL/glext.h>
#include <OpenGL/OpenGL.h>
#include <math.h>
#include <time.h>
static int samples, width, height, calls;
void GLBackendReset(void) { samples = -1; width = height = calls = 0; }
int GLBackendSamples(void) { return samples; }
int GLBackendWidth(void) { return width; }
int GLBackendCalls(void) { return calls; }
void glGetIntegerv(GLenum name, GLint *value) { *value = name == GL_MAX_SAMPLES_EXT ? 4 : 0; }
void glRenderbufferStorageMultisampleEXT(GLenum t, GLsizei s, GLenum f, GLsizei w, GLsizei h) {
    (void)t; (void)f; samples = s; calls++; width = s > 4 ? 0 : w; height = s > 4 ? 0 : h;
}
void glGetRenderbufferParameterivEXT(GLenum t, GLenum name, GLint *value) {
    (void)t;
    *value = name == GL_RENDERBUFFER_SAMPLES_EXT ? samples :
             name == GL_RENDERBUFFER_WIDTH_EXT ? width : height;
}
CGLContextObj CGLGetCurrentContext(void) { return NULL; }
CGLError CGLFlushDrawable(CGLContextObj ctx) {
    (void)ctx;
    struct timespec now; clock_gettime(CLOCK_MONOTONIC, &now);
    double time = now.tv_sec + now.tv_nsec / 1e9;
    double delay = (floor(time * 120.0) + 1) / 120.0 - time;
    struct timespec wait = {0, (long)(delay * 1e9)}; nanosleep(&wait, NULL);
    return kCGLNoError;
}
''')
            release = root / "release.c"
            release.write_text('''#include <OpenGL/gl.h>
#include <OpenGL/glext.h>
void ELReleaseAnchor(void) {}
static void Clamp(GLenum t, GLsizei s, GLenum f, GLsizei w, GLsizei h) {
    glRenderbufferStorageMultisampleEXT(t, s > 4 ? 4 : s, f, w, h);
}
__attribute__((used)) static struct { const void *newFn, *oldFn; }
hook __attribute__((section("__DATA,__interpose"))) = {
    (void *)Clamp, (void *)glRenderbufferStorageMultisampleEXT
};
''')
            main = root / "main.m"
            main.write_text('''#include <assert.h>
#include <stdio.h>
#include <objc/objc.h>
#include <time.h>
#include <unistd.h>
#import <Foundation/Foundation.h>
#import <AppKit/NSEvent.h>
#include <OpenGL/gl.h>
#include <OpenGL/glext.h>
#include <OpenGL/OpenGL.h>
extern void ELProbeTestConfigure(BOOL, int, void *);
extern void ELProbeTestFrameCap(int);
extern int ELProbeTestPresents(void);
#ifdef HAS_NATIVE
extern void ELContextFixTestFrameCap(int);
#endif
extern void GLBackendReset(void);
extern int GLBackendSamples(void), GLBackendWidth(void), GLBackendCalls(void);
extern BOOL ELProbeTestMeasurementHook(Class);
extern int ELProbeTestSignals(void);
static int forwarded;
@interface FakeApplication : NSObject
- (void)sendEvent:(NSEvent *)event;
@end
@implementation FakeApplication
- (void)sendEvent:(NSEvent *)event { (void)event; forwarded++; }
@end
@interface FakeKey : NSObject { NSEventType kind; unsigned short code; BOOL repeated; }
+ (id)type:(NSEventType)t code:(unsigned short)c repeat:(BOOL)r;
- (NSEventType)type;
- (unsigned short)keyCode;
- (BOOL)isARepeat;
@end
@implementation FakeKey
+ (id)type:(NSEventType)t code:(unsigned short)c repeat:(BOOL)r {
    FakeKey *event = [FakeKey new]; event->kind=t; event->code=c; event->repeated=r; return event;
}
- (NSEventType)type { return kind; }
- (unsigned short)keyCode { return code; }
- (BOOL)isARepeat { return repeated; }
@end
#ifdef HAS_RELEASE
extern void ELReleaseAnchor(void);
#endif
static __attribute__((noinline)) void Allocate(BOOL patched, int cap, int scoped) {
    ELProbeTestConfigure(patched, cap, scoped ? &&returned : NULL);
    glRenderbufferStorageMultisampleEXT(GL_RENDERBUFFER_EXT, 8, GL_RGBA8, 2048, 1330);
returned:
    __asm__ volatile("nop");
}
static void Check(BOOL patched, int cap, int scoped, int expected) {
    GLBackendReset(); Allocate(patched, cap, scoped);
    assert(GLBackendCalls() == 1);
    assert(GLBackendSamples() == expected);
    assert(GLBackendWidth() == (expected > 4 ? 0 : 2048));
}
int main(void) {
#ifdef HAS_RELEASE
    ELReleaseAnchor();
#endif
    Check(YES, -1, 1, 4); Check(YES, 0, 1, 0);
    Check(YES, 2, 1, 2); Check(YES, 4, 1, 4);
#ifndef HAS_RELEASE
    Check(NO, -1, 1, 8); Check(YES, -1, 0, 8);
#endif
    assert(ELProbeTestMeasurementHook([FakeApplication class]));
    FakeApplication *app = [FakeApplication new];
    [app sendEvent:[FakeKey type:NSEventTypeKeyDown code:0 repeat:NO]];
    [app sendEvent:[FakeKey type:NSEventTypeKeyDown code:100 repeat:NO]];
    [app sendEvent:[FakeKey type:NSEventTypeKeyDown code:100 repeat:YES]];
    [app sendEvent:[FakeKey type:NSEventTypeKeyUp code:100 repeat:NO]];
    [app sendEvent:[FakeKey type:NSEventTypeKeyUp code:0 repeat:NO]];
    assert(forwarded == 2); assert(ELProbeTestSignals() == 1);
#ifdef HAS_NATIVE
    ELContextFixTestFrameCap(60);
    ELProbeTestFrameCap(0); /* must inherit the ordinary patch's cap */
#else
    ELProbeTestFrameCap(60);
#endif
    struct timespec began, ended;
    clock_gettime(CLOCK_MONOTONIC, &began);
    for (int i=0; i<75; ++i) {
        usleep(2000); /* simulated frame preparation before a 120 Hz swap */
        CGLFlushDrawable((CGLContextObj)0x1234);
    }
    clock_gettime(CLOCK_MONOTONIC, &ended);
    double elapsed = ended.tv_sec-began.tv_sec + (ended.tv_nsec-began.tv_nsec)/1e9;
    assert(75.0/elapsed > 53 && 75.0/elapsed < 64);
    assert(ELProbeTestPresents() >= 75);
    puts("PASS: native observer preserves clamp and caller scope");
    return 0;
}
''')
            common = ["xcrun", "--sdk", "macosx", "clang", "-arch", "x86_64", "-fno-omit-frame-pointer",
                      "-Wno-deprecated-declarations", "-mmacosx-version-min=10.13"]

            def compile(arguments):
                subprocess.run(common + arguments, check=True, capture_output=True, text=True)

            compile(["-dynamiclib", str(backend), "-install_name", "@rpath/libMockGL.dylib",
                     "-o", str(root / "libMockGL.dylib")])
            links = ["-L" + str(root), "-lMockGL", "-Wl,-rpath," + str(root)]
            compile(["-dynamiclib", str(release), *links, "-install_name", "@rpath/libReleaseMock.dylib",
                     "-o", str(root / "libReleaseMock.dylib")])
            compile(["-dynamiclib", "-O2", "-fobjc-arc", "-DEL_PROBE_INTERPOSITION_TEST",
                     str(ROOT / "tools/render_probe.m"), *links,
                     "-framework", "AppKit", "-framework", "OpenGL",
                     "-install_name", "@rpath/libProbeTest.dylib", "-o", str(root / "libProbeTest.dylib")])
            compile(["-dynamiclib", "-fobjc-arc", "-DEL_FRAME_CAP_TEST", str(ROOT / "src/ELContextFixNative.m"),
                     *links, "-framework", "AppKit", "-framework", "OpenGL",
                     "-install_name", "@rpath/libNativeTest.dylib", "-o", str(root / "libNativeTest.dylib")])
            for variant in ("observer", "mock-release", "native-release"):
                flags = ["-DHAS_RELEASE", "-lReleaseMock"] if variant == "mock-release" else \
                        ["-DHAS_NATIVE", "-lNativeTest"] if variant == "native-release" else []
                binary = root / ("test-" + variant)
                compile(["-O0", str(main), *links, *flags, "-lProbeTest", "-framework", "Foundation",
                         "-framework", "AppKit", "-o", str(binary)])
                result = subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=20)
                self.assertIn("PASS: native observer", result.stdout)
            # Production present limiter alone: no observer, F8 handler or logs.
            standalone = root / "native-only.c"
            standalone.write_text('''#include <assert.h>
#include <time.h>
#include <unistd.h>
#include <OpenGL/OpenGL.h>
extern void ELContextFixTestFrameCap(int);
extern int ELContextFixGetFrameCap(void);
extern void ELContextFixTestReadOptions(void);
int main(void) {
    ELContextFixTestReadOptions(); assert(ELContextFixGetFrameCap() == 60);
    struct timespec a,b; clock_gettime(CLOCK_MONOTONIC,&a);
    for(int i=0;i<75;i++) { usleep(2000); CGLFlushDrawable((CGLContextObj)0x1234); }
    clock_gettime(CLOCK_MONOTONIC,&b);
    double elapsed = b.tv_sec-a.tv_sec+(b.tv_nsec-a.tv_nsec)/1e9;
    assert(75.0/elapsed > 53 && 75.0/elapsed < 64);
    ELContextFixTestFrameCap(0); assert(ELContextFixGetFrameCap() == 0);
    return 0;
}
''')
            app = root / "OrdinaryGame.app"
            (app / "Contents/MacOS").mkdir(parents=True)
            import plistlib
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps(dict(
                CFBundleExecutable="test-native-only", CFBundleIdentifier="test.el.framecap")))
            (root / "ContextFix").mkdir()
            (root / "ContextFix/runtime-options.json").write_text('{"schema":1,"fps_cap":60}')
            binary = app / "Contents/MacOS/test-native-only"
            compile([str(standalone), *links, "-lNativeTest", "-o", str(binary)])
            subprocess.run([str(binary)], check=True, capture_output=True, timeout=20)
