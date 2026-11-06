# How the fullscreen fix works

The patch addresses three related failures in the native Intel game: fullscreen
application switching, OpenGL drawable lifetime, and Retina/scaled rendering.
All native addresses below refer to the supported x86_64 Unity **5.3.6p1**
executable, before adding its ASLR slide. The installer requires an exact match
for that executable before adding the helper's load command.

## 1. Load before Unity creates its graphics contexts

The Python installer parses the fat Mach-O file and selects its x86_64 slice.
It inserts one `LC_LOAD_DYLIB` command in existing zero-filled header padding:

```text
@executable_path/../Plugins/libELContextFixNative.dylib
```

Only the x86_64 load-command count/size and the new command change. Section
offsets, executable instructions, file length and the i386 slice remain intact.
The helper is compiled from `src/ELContextFixNative.m` for x86_64, so it can load
into the Intel game even when the host Mac is Apple Silicon.

The library constructor installs the Objective-C hooks before Unity initializes
its graphics contexts. A managed bootstrap in the game's `Application.Awake`
was too late for the original failure. This approach needs no managed DLL patch
or replacement Unity player.

## 2. Use native macOS fullscreen

The installer locates the class-129 `PlayerSettings` object through Unity's
format-15 serialized object table. It verifies the object layout/content against
the inspected build, then changes these fields:

| Offset within PlayerSettings | Field | Installed value |
| --- | --- | --- |
| `0xb0` | `macFullscreenMode`, 32-bit integer | `2` |
| `0xa6` | `resizableWindow`, byte | `1` |
| `0x80` | Threaded renderer, byte | Preserved; must already be `1` |

In the inspected player, mode 2 selects Cocoa fullscreen and permits normal
application switching. The other fullscreen path applies presentation options
that prohibit switching. Changing the mode alone exposed the drawable problem
below, so both pieces are needed on the tested system.

The complete settings file is preserved apart from the targeted fields. The
installer checks the normalized PlayerSettings object rather than hardcoding
its position or requiring an identical whole asset file.

## 3. Detach the old drawable before replacing the context

Unity's `PlayerWindowView` is a custom `NSView`, not an `NSOpenGLView`. Its
`setContext:` implementation releases the previous `NSOpenGLContext` wrapper,
wraps a new CGL context, and attaches it with `setView:`. It does not explicitly
detach the previous drawable first. The old raw CGL context is destroyed later.

The helper exchanges the implementation of `PlayerWindowView.setContext:`.
Before delegating to Unity, it reads the existing `m_OpenGLContext` ivar and
calls `clearDrawable` while that wrapper is still alive. Apple documents this
operation as detaching the context from its drawable.

The failure was reproduced outside the game with a small Cocoa/OpenGL app:
resize a window, replace the context in Unity's order, then enter fullscreen.
Without detachment it crashed in `gldPresentFramebufferData` / `CGLFlushDrawable`;
with detachment it completed. Calling `update` alone did not fix that reproduction.
This supports the workaround without establishing that every involved driver or
Unity component is independently correct.

## 4. Keep Retina pixels separate from display points

Native fullscreen initially worked with a logical 1512×945 drawable, producing
blurred text. The patch enables `NSHighResolutionCapable` in Info.plist and
`wantsBestResolutionOpenGLSurface` on the fullscreen view. On the tested display,
1512×945 points correspond to a 3024×1890 backing surface.

Unity also needs consistent dimensions for its resolution list, requested render
size and final presentation viewport. The helper clones the current
ScreenManager instance's vtable, including its two preceding ABI entries, and
replaces only the relevant function pointers in this heap copy. The original
read-only vtable is not modified.

| Hook | Purpose |
| --- | --- |
| Set-resolution virtual method, vtable byte offset `0x20` | Tracks the request while Unity creates/rebinds its window and context; promotes the first legacy desktop-point default to Retina pixels |
| System-resolution virtual method, offset `0x38` | Reports the fullscreen backing dimensions |
| Resolution enumeration, offset `0x108` | Calls Unity's base enumeration, filters modes above the backing size, and includes the native backing resolution |
| `NSWindow.contentRectForFrameRect:` | Returns the requested pixel dimensions only for Unity's resolution readback caller |
| `CGDisplayBounds` interposition | Supplies pixel dimensions only to the resolution and presentation callers |
| Window resize notification | Refreshes the cached drawable size using `convertRectToBacking:` |

The base resolution enumeration and vector insertion use Unity's own functions,
retaining allocation/deallocation in the engine's C++ runtime. Input-coordinate
callers still receive ordinary display-point bounds. The `NSWindow` method hook
also delegates unchanged for other callers and window views.

The backing surface stays at its fullscreen pixel size while the selected game
render size can be lower, such as 2048×1330. Unity performs its usual scaling.
The game's large-interface option remains a separate game setting.

## 5. Respect the driver's MSAA limit

The remaining black screen at lower fullscreen resolutions was traced to buffer
allocation. With the Maximum quality preset, Unity's legacy FSAA chooser requested
**8 samples**, while the tested driver reported **`GL_MAX_SAMPLES_EXT = 4`**.

Both color and depth/stencil renderbuffers remained **0×0**. The MSAA framebuffer
was incomplete (`0x8cd6`), and resolving it failed (`0x506`); allocation had also
left `GL_INVALID_OPERATION` (`0x502`) pending. The image existed neither in the
MSAA buffers nor in their resolved destination.

The helper interposes `glRenderbufferStorageMultisampleEXT` only for the two
legacy fullscreen allocation callers and limits `samples` to the driver's
maximum. Both attachments therefore use the same supported sample count.
After the fix, the 2048×1330 attachments had 4 samples, the framebuffer was
complete (`0x8cd5`), and the resolve completed without an OpenGL error.

The final fix uses Unity's original frame presentation. It does not install a
per-frame framebuffer copy or replace the scaling shader. No diagnostic timer,
command-file reader or framebuffer pixel reader is present in the release source.

## Build-specific addresses

| Address / return address | Meaning |
| --- | --- |
| `0x100a4c570` | `GetScreenManager()` |
| `0x1015c0fe0` | Expected ScreenManager vtable pointer |
| `0x101087d70` | Expected set-resolution virtual method |
| `0x101099700` | Base macOS resolution enumeration |
| `0x10109a290` | Unity resolution-vector insertion |
| `0x101088055` | Content-rectangle readback inside resolution switching |
| `0x101087ea6` | Display-bounds readback inside resolution switching |
| `0x10108843f`, `0x10108859e` | Legacy/alternate presentation viewport bounds |
| `0x101087c68`, `0x101087cc4` | Fullscreen MSAA color and depth/stencil allocation return addresses |

The helper obtains the executable's slide with `_dyld_get_image_vmaddr_slide(0)`
and applies it when accessing functions/tables or comparing return addresses.
It also checks the ScreenManager vtable and set-resolution method before cloning
the table. These checks supplement, rather than replace, the installer's exact
executable hash check. Supporting a different build requires inspecting and
validating its layout and native addresses first.

## Installation integrity and scope

The installer builds with an argument list passed to `xcrun`, validates that the
result is an x86_64 Mach-O dynamic library with a valid `LC_UUID` command, and
records source/binary hashes. The linker UUID must be retained: omitting it with
`-Wl,-no_uuid` causes macOS/Rosetta to reject the helper at launch with
`missing LC_UUID load command`, before any patch code runs.
There is no fixed prebuilt-helper checksum to edit after compilation.

Original files and their permissions are recorded in `ContextFix/`. All backups
are checked before removal, and manifest paths must match the four known game
paths and their known backup locations. Install activates the executable load
command last; uninstall restores the executable before removing the helper.
Ordinary Python I/O failures roll back the attempted multi-file operation.

The patch does not rewrite managed assemblies, saves, shaders or movie files,
and does not change the renderer's threading setting. Existing movie-audio and
visual fixes remained intact in the tested installation.

## Evidence and limits

The owner confirmed the working release behavior: Retina, the large interface,
2048×1330, and Cmd-Tab away and back. Native 3024×1890 and 1920×1200 rendering
were also checked. The repository's installation logic was tested against
synthetic fixtures and temporary copies of the actual supported game files.

The Cocoa reproduction identifies a drawable-lifetime workaround. The runtime
diagnostics identify an unsupported MSAA request. Neither establishes behavior
for every macOS version, display arrangement, graphics driver, quality preset or
gameplay duration. See the tested configuration in the main README.

## Primary references

- Apple: [NSOpenGLContext.clearDrawable](https://developer.apple.com/documentation/appkit/nsopenglcontext/cleardrawable())
- Apple: [Working with Rendering Contexts](https://developer.apple.com/library/archive/documentation/GraphicsImaging/Conceptual/OpenGL-MacProgGuide/opengl_contexts/opengl_contexts.html)
- Khronos: [EXT_framebuffer_multisample specification](https://registry.khronos.org/OpenGL/extensions/EXT/EXT_framebuffer_multisample.txt)
