# Endless Legend macOS Fullscreen Fix

Native fullscreen, Cmd-Tab and Retina support for the Intel macOS version of
**Endless Legend**, including selectable fullscreen resolutions and the large UI.

The installer builds the native helper from source and applies the patch to the
local game installation. No game files or prebuilt libraries are included.

[Русская инструкция](README.ru.md) · [How it works](docs/HOW_IT_WORKS.md) ·
[Testing and development](docs/TESTING.md) · [Release notes](CHANGELOG.md)

## What this fixes

- Allows switching applications while using the game's native fullscreen mode.
- Fixes black screens and hangs caused by OpenGL context replacement during
  fullscreen transitions and resolution changes.
- Preserves Retina rendering instead of limiting the game to logical display points.
- Fixes the black screen at lower fullscreen resolutions when the quality preset
  requests more MSAA samples than the OpenGL driver supports.

## Requirements and compatibility

- The native **Intel macOS** game, not its Windows build under CrossOver.
- Python **3.9 or newer**, available as `python3`.
- Apple's **Command Line Tools** or Xcode, providing `xcrun` and Clang.
- A matching Unity executable and PlayerSettings object, checked before installation.

The native helper targets **x86_64** to match the game process. On Apple Silicon,
both the game and helper run through Rosetta. The build uses a macOS 10.13 minimum
deployment target; runtime compatibility has been tested on the configuration below.

Supported original executable SHA-256:

```text
588bf58e87809a226a4d1a822d4c4f7b0c509617c7d4e39fd4b828ff6df4f6c5
```

The helper uses offsets specific to this Unity build. The installer rejects
unknown or independently modified executables and incompatible PlayerSettings
layouts. Remove existing fullscreen patches with their original installers before
installation.

### Tested configuration

| Component | Version / configuration |
| --- | --- |
| Hardware | Apple M4 Pro |
| macOS | 26.6.2 |
| Unity player | 5.3.6p1 |
| ELCP | 2.7.9 S3 |
| Resolutions | 3024×1890, 2048×1330, 1920×1200 |
| Display options | Native fullscreen, Retina, large interface |
| Application switching | Cmd-Tab |
| Steam Overlay | Disabled |

The patch does not modify ELCP assemblies and can be used alongside the separate
movie-audio and visual fixes. See [TESTING.md](docs/TESTING.md) for validation scope
and additional test procedures.

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

`install` compiles `src/ELContextFixNative.m`, backs up the original files, and
applies the patch. It uses Python's standard library and Apple's developer tools;
no pip packages or Mono installation are required. A writable Steam library does
not require administrator privileges.

`--dry-run` checks game compatibility and backup conflicts without changing game
files. Use `build` to check the compiler and build the helper separately.

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
desired. Fullscreen MSAA is capped to the driver's `GL_MAX_SAMPLES_EXT` limit
(4× in the tested configuration). Other quality settings are preserved.

## Optional frame cap and helper update

To update an intact 1.0.0 or 1.1.0 installation, quit the game, use the new release's
source directory, and enable the optional cap:

```sh
python3 fullscreen_fix.py update --fps-cap 60
```

This rebuilds and replaces only the native helper and its installation manifest, and
saves the frame cap in `ContextFix/runtime-options.json`. Start through Steam
normally; no benchmark logger or diagnostic observer is needed. Resolution,
Retina, MSAA and other graphics-quality settings are preserved. The cap is an
upper bound, not a guarantee that the game can sustain exactly that frame rate.

For a fresh installation use `install --fps-cap 60`. Without an explicit setting,
the extra cap is disabled. After updating the helper, change or disable it with:

```sh
python3 fullscreen_fix.py configure --fps-cap 100
python3 fullscreen_fix.py configure --fps-cap 0
```

Restart the game after changing the cap. Runtime preferences are retained on
uninstall but have no effect without the helper. The update command accepts only
an intact supported installation and preserves original backups. Use `update`
without `--fps-cap` to keep the existing cap; on a 1.0.0 installation without
preferences it remains disabled. Unrelated experimental patches are rejected.

In two marked 180-second tutorial runs at 2048×1330 and 4× MSAA, a cap of 60
produced about 54/53 completed presents per second and GPU power of 3.48/3.43 W
for original/patched fullscreen, respectively. The camera positions were
approximately matched; these measurements do not prove identical per-frame
cost in every scene. See [the investigation](docs/POWER-INVESTIGATION.ru.md).

## Status and removal

Quit the game before removal:

```sh
python3 fullscreen_fix.py status
python3 fullscreen_fix.py uninstall
```

Use the same `--game-dir` argument for a custom library. `status` verifies installed
files and backups against their recorded hashes. It exits nonzero if the patch is
not installed or verification fails. Running `install` again with the same source
verifies the existing installation without applying the patch again.

The installer keeps its manifest and original backups inside the game's
`ContextFix/` directory. **Keep this directory until the patch is removed.**
Uninstall restores the original executable, PlayerSettings and Info.plist,
including file permissions, then removes the helper and manifest. Original backups
are retained. File writes use atomic replacement with rollback on I/O errors.

Removal refuses to overwrite files or backups modified after installation. If
Steam verification or a game update replaced patched files, inspect the `status`
output before reinstalling or removing the patch. Keep the backups; do not delete
the helper manually while the executable still depends on it.

## Build without installing

```sh
python3 fullscreen_fix.py build
```

This creates `.build/libELContextFixNative.dylib` and `.build/build.json`. The
metadata records the source and binary SHA-256 hashes, target architecture, and
compiler flags. The build directory is ignored by Git. Each new installation
builds the helper from the current checkout.
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
| `Restore the original threaded-renderer setting first` | Restore the game's original renderer-threading setting before installation |
| `Native build failed` / missing `xcrun` | Install Apple's Command Line Tools and rerun `build` |
| Immediate launch failure with `missing LC_UUID load command` in the crash report | Reinstall using the corrected build instructions below |
| `Existing original backup differs` | Keep the existing backups and resolve the earlier installation before proceeding |
| `CHANGED/MISSING` | Inspect the named installed file or backup; the installer intentionally refuses to overwrite it |

A helper built with the earlier `-Wl,-no_uuid` flag is rejected by macOS/Rosetta
before Unity starts. From a checkout containing the build fix, quit the game and
rebuild the installation:

```sh
python3 fullscreen_fix.py uninstall
python3 fullscreen_fix.py install
python3 fullscreen_fix.py status
```

For a custom Steam library, add the same `--game-dir` to all three commands.
The corrected builder retains and validates `LC_UUID`; removal also works for
the affected installation.

Runtime context/resolution messages appear in `~/Library/Logs/Unity/Player.log`.
The helper also appends lifecycle and MSAA messages to `/tmp/ELContextFix-native.log`.

## Tests and license

For manual GPU/temperature A/B measurements, see the separate read-only
[benchmark logger](docs/BENCHMARK.ru.md). It records CSV/JSON and generates a
local comparison report without changing the game or its graphics settings.

```sh
python3 -m unittest discover -s tests -v
```

The default suite uses synthetic fixtures and needs no game installation or
compiler. Optional tests against local game files and the independent OpenGL
reproductions are described in [TESTING.md](docs/TESTING.md).

Patch code is available under the [MIT License](LICENSE). Endless Legend, Unity,
and all original game assets remain the property of their respective owners.
