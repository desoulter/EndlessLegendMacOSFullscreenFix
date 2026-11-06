# Testing and development

## Source-only tests

From the repository directory:

```sh
python3 -m unittest discover -s tests -v
```

The default suite runs **22 tests** with small synthetic Mach-O, Unity asset and
Info.plist fixtures. Two optional real-game tests are skipped unless their local
fixture path is supplied. No game bytes, native binaries, third-party Python
packages or compiler are needed for the default suite.

Coverage includes exact patch scope, untouched machine instructions/i386 slice,
untouched settings bytes and other objects, permissions and original-file
restoration, idempotence, no-write dry runs, unsupported game layouts, compilation
failure, backup collisions, changed installed files/backups, rejected manifest
path redirection, and rollback of failed installation/removal writes.

There is deliberately no version-migration path. A manifest from another version
is rejected before any installed file is changed.

## Compile the actual native helper

On macOS with Apple's developer tools:

```sh
python3 fullscreen_fix.py build
file .build/libELContextFixNative.dylib
otool -L .build/libELContextFixNative.dylib
```

Expect a thin **x86_64** dynamic library linked to macOS frameworks. Build flags
include ARC, the x86_64 target, macOS 10.13 deployment target and a fixed install
name. The random linker UUID is omitted. Compiler/SDK differences may still
produce different binaries; source and binary hashes are recorded in
`.build/build.json` for the local build.

## Optional tests against your game files

Set `EL_FULLSCREEN_GAME_DIR` to the folder containing `EndlessLegend.app`, then
run the suite:

```sh
EL_FULLSCREEN_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Endless Legend" \
  python3 -m unittest discover -s tests -v
```

These tests read the original executable, settings and Info.plist from that
installation, preferring the known original backup files in `ContextFix/backup/`
when present. They compile the real native source and exercise installation,
status and exact removal on **temporary copies**. They also compare every byte
outside the targeted PlayerSettings fields. They do not install into the real
game directory or launch the game.

An unsupported original executable/layout fails this optional test rather than
relaxing the release's compatibility checks. This fixture reader is a test-only
facility, not an update/migration feature in the installer.

## Manual game check

After installation, start the game through Steam and check:

1. Main menu is visible in native fullscreen; text is sharp at the native Retina resolution.
2. Enable the large interface, select 2048×1330, and confirm the resolution dialog.
3. Change to another available resolution and back, including the dialog's cancel/timeout behavior.
4. Cmd-Tab to another application, then return to the game.
5. Check mouse alignment, menu selection, and an actual saved/new game.
6. Check other installed patches, such as movie sound and visual indicators.

The owner confirmed the central Retina/large-UI/2048×1330/Cmd-Tab scenario on the
configuration in the README. Do not interpret that confirmation as testing all
quality levels, gameplay durations or display arrangements.

## Independent drawable/context reproduction

`research/probe.m` reproduces the context-replacement/fullscreen sequence without
Unity, Mono or Steam. Compile it as a small Cocoa app:

```sh
mkdir -p .build/ELGLProbe.app/Contents/MacOS
cp research/Probe-Info.plist .build/ELGLProbe.app/Contents/Info.plist
xcrun --sdk macosx clang -arch x86_64 -fobjc-arc -fblocks \
  -Wno-deprecated-declarations -mmacosx-version-min=10.13 \
  -framework AppKit -framework OpenGL research/probe.m \
  -o .build/ELGLProbe.app/Contents/MacOS/ELGLProbe
mkdir -p /tmp/el-fullscreen-research
cp research/configs/guarded.json /tmp/el-fullscreen-research/probe-config.json
open .build/ELGLProbe.app
```

The guarded case runs for about nine seconds and should finish with a `PASS`
line in `/tmp/el-fullscreen-research/probe-guarded.log`. It detaches the old
drawable before replacing the context. `crashing.json` removes this guard and
is included only to reproduce the observed graphics-driver crash; do not use it
as a normal installation check.

The original investigation observed:

| Sequence | Observed result on the tested system |
| --- | --- |
| Resize only | Completed |
| Replace context without fullscreen | Completed |
| Fullscreen without replacement | Completed |
| Replace context and enter fullscreen | Driver crash at `CGLFlushDrawable` |
| Same sequence with an extra `update` | Same crash |
| Detach old drawable, replace context, enter fullscreen | Completed |

## Independent MSAA allocation reproduction

`research/msaa_probe.c` creates legacy CGL color and depth/stencil renderbuffers
at several game resolutions. It compares an excessive sample request with a
request capped to `GL_MAX_SAMPLES_EXT`:

```sh
mkdir -p .build
xcrun --sdk macosx clang -arch x86_64 -Wno-deprecated-declarations \
  -framework OpenGL research/msaa_probe.c -o .build/el-msaa-probe
.build/el-msaa-probe
```

It returns zero when unsupported requests are rejected and the capped requests
create complete framebuffers. It needs access to a normal macOS graphics
session. In the development command sandbox it compiled but could not obtain a
CoreGraphics connection, so runtime MSAA validation was performed inside the
actual game instead.

In that game, the unguarded request was 8 samples with a driver limit of 4:
both attachments remained 0×0 and framebuffer status was incomplete. Capping
both allocations to 4 produced complete 2048×1330 buffers and visible output.

## Runtime logs

- `~/Library/Logs/Unity/Player.log`: context replacements, render/drawable sizes,
  resolution enumeration and Unity messages.
- `/tmp/ELContextFix-native.log`: early library loading, drawable detachment,
  application activation changes and MSAA clamping.

The release source contains no automated resolution-command reader or pixel
capture. These were temporary investigation tools and are not needed to apply
or build the fix.
