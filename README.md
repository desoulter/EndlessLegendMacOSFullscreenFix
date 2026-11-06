# Endless Legend macOS Fullscreen Fix

Native fullscreen, Cmd-Tab and Retina support for the Intel macOS version of
**Endless Legend**, including selectable fullscreen resolutions and the large UI.

**First release: 1.0.0.** This repository contains the complete patch source and a
Python installer that builds the native helper on your Mac. There are no prebuilt
libraries or game files to download separately.

[Русская инструкция](README.ru.md) · [How it works](docs/HOW_IT_WORKS.md) ·
[Testing and development](docs/TESTING.md) · [Release notes](CHANGELOG.md)

## What this fixes

- Allows switching applications while using the game's native fullscreen mode.
- Avoids the black screen / spinning beachball caused by replacing the OpenGL
  context when entering fullscreen or changing resolution on the tested Mac.
- Preserves Retina rendering instead of limiting the game to logical display points.
- Fixes the black screen at lower fullscreen resolutions when the quality preset
  requests more MSAA samples than the OpenGL driver supports.

The owner confirmed this complete scenario on an **Apple M4 Pro, macOS 26.6.2,
Unity 5.3.6p1, ELCP 2.7.9 S3**: Retina → large interface → **2048×1330** →
Cmd-Tab away and back. Native **3024×1890** and **1920×1200** rendering were also
checked. Steam Overlay was disabled during testing. Other Macs, external displays,
trackpad gestures, Overlay enabled, and long gameplay sessions have not been
separately verified.

## Requirements and compatibility

- The native **Intel macOS** game, not its Windows build under CrossOver.
- Python **3.9 or newer**, available as `python3`.
- Apple's **Command Line Tools** or Xcode, providing `xcrun` and Clang.
- A matching Unity executable and PlayerSettings object, checked before installation.

The native helper is compiled as **x86_64**, including on Apple Silicon, because
it runs inside the Intel game process under Rosetta. The compiler's minimum
deployment target is macOS 10.13; this is not a claim that older macOS versions
were tested.

Supported original executable SHA-256:

```text
588bf58e87809a226a4d1a822d4c4f7b0c509617c7d4e39fd4b828ff6df4f6c5
```

The helper uses offsets from this exact Unity build. An unknown or independently
modified executable is rejected. This is not a generic Unity patch for all versions.
ELCP is the tested configuration, but no ELCP assembly is used or modified by this
patch. The movie-audio and visual fixes can coexist with it.

## Install from source

1. **Quit Endless Legend.** Open Terminal in your local checkout of this repository.
2. Check the prerequisites:

   ```sh
   python3 --version
   xcrun --find clang
   ```

   If the Apple tools are missing, install them and complete the system installer:

   ```sh
   xcode-select --install
   ```

3. Validate the game and install:

   ```sh
   python3 fullscreen_fix.py install --dry-run
   python3 fullscreen_fix.py install
   python3 fullscreen_fix.py status
   ```

`install` compiles `src/ELContextFixNative.m` automatically, saves the originals,
and installs the patch. **No pip packages, Mono, manual binary downloads, checksum
editing, or administrator privileges are needed for a normal writable Steam library.**
The dry run validates game compatibility and backup conflicts; it does not compile
or test compiler availability. To test compilation separately, use `build` below.

The default game directory is:

```text
~/Library/Application Support/Steam/steamapps/common/Endless Legend
```

If this repository is inside a directory containing `EndlessLegend.app`, that
parent directory is detected automatically. For another Steam library, provide
the directory **containing** `EndlessLegend.app`, not the app bundle itself:

```sh
python3 fullscreen_fix.py install --game-dir "/Volumes/Games/Steam/steamapps/common/Endless Legend"
python3 fullscreen_fix.py status --game-dir "/Volumes/Games/Steam/steamapps/common/Endless Legend"
```

Start the game through Steam normally. Enable fullscreen under **Video**, choose
your resolution, and enable the large-interface option under **Interface** if
desired. On the tested driver, a request for 8× fullscreen MSAA is limited to the
supported 4×; other quality settings are preserved.

## Status and removal

Quit the game before removal:

```sh
python3 fullscreen_fix.py status
python3 fullscreen_fix.py uninstall
```

Use the same `--game-dir` argument for a custom library. `status` checks installed
file and backup hashes; it exits nonzero if no registered installation exists or
anything has changed. Repeating a healthy installation from the same native
source does not add another load command or rebuild the helper.

The installer keeps its manifest and original backups inside the game's
`ContextFix/` directory. **Keep this directory until the patch is removed.**
Uninstall restores the original executable, PlayerSettings and Info.plist bytes
and permissions, then removes the helper and manifest. Original backups remain.
Ordinary write failures trigger rollback; abrupt power loss cannot make a
multi-file change atomic.

Removal refuses to overwrite files or backups modified after installation. If
Steam verification or a game update replaced patched files, inspect the `status`
output before doing anything further. Keep the backups; do not delete the helper
manually while the executable still depends on it.

This first release does **not** migrate experimental installations or other
versions. Remove those with the installer that created them before installing
1.0.0.

## Build without installing

```sh
python3 fullscreen_fix.py build
```

This creates `.build/libELContextFixNative.dylib` and `.build/build.json`. The
metadata records the source and binary SHA-256 hashes, target architecture, and
compiler flags. The build directory is ignored by Git. An installation always
builds from the checked-out source rather than trusting an old build artifact.
Status and removal do not require a compiler.

## Files changed in the game

| File inside `EndlessLegend.app/Contents/` | Change |
| --- | --- |
| `MacOS/EndlessLegend` | Adds one native-library load command in existing x86_64 header padding |
| `Resources/Data/globalgamemanagers` | Enables native fullscreen and a resizable window |
| `Info.plist` | Enables `NSHighResolutionCapable` |
| `Plugins/libELContextFixNative.dylib` | Adds the helper compiled from this repository |

The executable's instruction bytes, section offsets and i386 slice are preserved.
The patch leaves managed game DLLs, save files, shaders, movie audio, renderer
threading and system preferences alone. The technical explanation and exact hook
addresses are in [HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md).

## Troubleshooting

| Message / symptom | Action |
| --- | --- |
| `Unsupported or already modified Unity executable` | Use the supported native game build; remove other executable patches with their own installers first |
| `Unsupported PlayerSettings layout/content` | The settings object differs from the inspected build; do not bypass the check |
| `Restore the original threaded-renderer setting first` | Undo the earlier renderer-threading experiment before installation |
| `Native build failed` / missing `xcrun` | Install Apple's Command Line Tools and rerun `build` |
| `Existing original backup differs` | Keep the existing backups and resolve the earlier installation before proceeding |
| `CHANGED/MISSING` | Inspect the named installed file or backup; the installer intentionally refuses to overwrite it |

Runtime context/resolution messages appear in `~/Library/Logs/Unity/Player.log`.
The helper also appends brief lifecycle messages to `/tmp/ELContextFix-native.log`.
These are local files; the helper makes no network requests.

## Tests and license

```sh
python3 -m unittest discover -s tests -v
```

The default suite uses synthetic fixtures and needs no game installation or
compiler. Optional tests against local game files and the independent OpenGL
reproductions are described in [TESTING.md](docs/TESTING.md).

Patch code is available under the [MIT License](LICENSE). Endless Legend, Unity,
and all original game assets remain the property of their respective owners.
