// Endless Legend macOS Fullscreen Fix 1.0.0 — MIT License
// Exact Unity build offsets are documented in docs/HOW_IT_WORKS.md.
#import <AppKit/AppKit.h>
#import <OpenGL/OpenGL.h>
#import <OpenGL/gl.h>
#import <OpenGL/glext.h>
#import <objc/runtime.h>
#import <mach-o/dyld.h>
#include <stdatomic.h>

static void (*originalSetContext)(id, SEL, CGLContextObj);
static Ivar contextIvar;
static Class playerViewClass;
static intptr_t executableSlide;
static __weak NSView *playerView;
static atomic_int drawableWidth, drawableHeight;
static NSRect (*originalContentRect)(id, SEL, NSRect);
typedef struct { int width, height, refresh; } Resolution;
typedef struct { Resolution *begin, *end, *capacity; } ResolutionVector;
typedef BOOL (*SetResolutionFunction)(void *, int, int, bool, int);
static SetResolutionFunction originalResolution;
static Resolution (*originalSystemResolution)(void *);
static void **clonedTable;
static _Thread_local struct { BOOL active, fullscreen; int width, height; } request;

static void Log(const char *message)
{
    FILE *file = fopen("/tmp/ELContextFix-native.log", "a");
    if (file) { fprintf(file, "%s\n", message); fclose(file); }
}

static NSSize PixelSize(void)
{
    NSView *view = playerView;
    if (view.window && (view.window.styleMask & NSWindowStyleMaskFullScreen)) {
        NSSize size = view.bounds.size;
        CGFloat scale = view.window.backingScaleFactor ?: 1;
        return NSMakeSize(size.width * scale, size.height * scale);
    }
    NSScreen *screen = view.window.screen ?: NSScreen.mainScreen;
    NSSize size = screen.frame.size;
    CGFloat scale = screen.backingScaleFactor ?: 1;
    return NSMakeSize(size.width * scale, size.height * scale);
}

static void UpdateDrawableSize(void)
{
    NSView *view = playerView;
    if (!view) return;
    NSSize size = [view convertRectToBacking:view.bounds].size;
    atomic_store(&drawableWidth, (int)llround(size.width));
    atomic_store(&drawableHeight, (int)llround(size.height));
}

static BOOL SetResolution(void *manager, int width, int height, bool fullscreen, int refresh)
{
    static BOOL firstFullscreen = YES;
    if (fullscreen && firstFullscreen) {
        firstFullscreen = NO;
        NSSize native = PixelSize();
        CGFloat scale = playerView.window.backingScaleFactor ?: 1;
        if (scale > 1 && width == (int)llround(native.width / scale) &&
            height == (int)llround(native.height / scale)) {
            width = (int)llround(native.width); height = (int)llround(native.height);
            Log("Retina: promoted legacy desktop-point default to native pixels");
        }
    }
    typeof(request) previous = request;
    request.active = YES; request.fullscreen = fullscreen;
    request.width = width; request.height = height;
    BOOL result = originalResolution(manager, width, height, fullscreen, refresh);
    request = previous;
    UpdateDrawableSize();
    NSLog(@"[EL Context Fix Retina] render=%dx%d drawable=%dx%d fullscreen=%d",
          width, height, atomic_load(&drawableWidth), atomic_load(&drawableHeight), fullscreen);
    return result;
}

static NSRect ContentRect(id window, SEL selector, NSRect frame)
{
    NSRect result = originalContentRect(window, selector, frame);
    uintptr_t caller = (uintptr_t)__builtin_return_address(0) - executableSlide;
    // Only Unity's SetResolutionImmediate readback: AppKit and mouse mapping
    // continue receiving ordinary point coordinates.
    if (caller == 0x101088055 && request.active && request.fullscreen &&
        [((NSWindow *)window).contentView isKindOfClass:playerViewClass]) {
        result.size = NSMakeSize(request.width, request.height);
    }
    return result;
}

static Resolution SystemResolution(void *manager)
{
    Resolution result = originalSystemResolution(manager);
    NSSize pixels = PixelSize();
    result.width = (int)llround(pixels.width); result.height = (int)llround(pixels.height);
    return result;
}

static void GetResolutions(ResolutionVector *out, void *manager, int refresh, bool restricted)
{
    // Keep allocation/deallocation in Unity's own libstdc++ implementation.
    void (*base)(ResolutionVector *, void *, int, bool) = (void *)(0x101099700 + executableSlide);
    void (*insert)(ResolutionVector *, Resolution *, const Resolution *) = (void *)(0x10109a290 + executableSlide);
    base(out, manager, refresh, false);
    NSSize pixels = PixelSize();
    int width = (int)llround(pixels.width), height = (int)llround(pixels.height);
    Resolution *write = out->begin;
    BOOL found = NO;
    for (Resolution *read = out->begin; read != out->end; ++read) {
        if (read->width > width || read->height > height) continue;
        *write++ = *read;
        if (read->width == width && read->height == height) found = YES;
    }
    out->end = write;
    if (!found) {
        Resolution native = {width, height, refresh ?: 60};
        insert(out, out->end, &native);
    }
    NSLog(@"[EL Context Fix Retina] resolutions max=%dx%d count=%ld", width, height, (long)(out->end-out->begin));
}

static void HookScreenManager(void)
{
    void *(*getManager)(void) = (void *)(0x100a4c570 + executableSlide);
    void *manager = getManager();
    if (!manager || clonedTable) return;
    void **table = *(void ***)manager;
    if (table != (void *)(0x1015c0fe0 + executableSlide) ||
        table[0x20/8] != (void *)(0x101087d70 + executableSlide)) {
        Log("Retina: unexpected ScreenManager vtable; skipped"); return;
    }
    // Clone this instance's table instead of modifying read-only engine data.
    clonedTable = malloc(0x130);
    if (!clonedTable) {
        Log("Retina: ScreenManager table allocation failed; skipped"); return;
    }
    memcpy(clonedTable, table-2, 0x130);
    void **replacement = clonedTable+2;
    originalResolution = (void *)table[0x20/8];
    originalSystemResolution = (void *)table[0x38/8];
    replacement[0x20/8] = (void *)SetResolution;
    replacement[0x38/8] = (void *)SystemResolution;
    replacement[0x108/8] = (void *)GetResolutions;
    *(void ***)manager = replacement;
    Log("Retina: installed per-instance ScreenManager hooks");
}

static CGRect DisplayBounds(CGDirectDisplayID display)
{
    CGRect result = CGDisplayBounds(display);
    uintptr_t caller = (uintptr_t)__builtin_return_address(0) - executableSlide;
    if (caller == 0x101087ea6 && request.active && request.fullscreen) {
        NSSize size = PixelSize();
        result.size = CGSizeMake(size.width, size.height);
    } else if (caller == 0x10108843f || caller == 0x10108859e) {
        int width = atomic_load(&drawableWidth), height = atomic_load(&drawableHeight);
        if (width > 0 && height > 0) result.size = CGSizeMake(width, height);
    }
    return result;
}

__attribute__((used)) static struct { const void *replacement; const void *replacee; }
interposition __attribute__((section("__DATA,__interpose"))) = {(void *)DisplayBounds, (void *)CGDisplayBounds};

static void MultisampleStorage(GLenum target, GLsizei samples, GLenum format, GLsizei width, GLsizei height)
{
    uintptr_t caller = (uintptr_t)__builtin_return_address(0) - executableSlide;
    // Only Unity's downscaled-fullscreen color/depth allocations. Its legacy
    // FSAA chooser returns 8 on this driver although GL_MAX_SAMPLES_EXT is 4.
    // Both attachments must use the same supported sample count.
    if (caller == 0x101087c68 || caller == 0x101087cc4) {
        GLint maximum = 0;
        glGetIntegerv(GL_MAX_SAMPLES_EXT, &maximum);
        if (maximum > 0 && samples > maximum) {
            char message[256];
            snprintf(message, sizeof(message),
                     "Retina: clamped unsupported MSAA %d -> %d for %dx%d",
                     samples, maximum, width, height);
            Log(message);
            samples = maximum;
        }
    }
    glRenderbufferStorageMultisampleEXT(target, samples, format, width, height);
}
__attribute__((used)) static struct { const void *replacement; const void *replacee; }
multisampleInterposition __attribute__((section("__DATA,__interpose"))) = {(void *)MultisampleStorage, (void *)glRenderbufferStorageMultisampleEXT};

static void SetContext(id view, SEL selector, CGLContextObj next)
{
    playerView = view;
    HookScreenManager();
    BOOL fullscreen = request.active ? request.fullscreen :
        ((((NSView *)view).window.styleMask & NSWindowStyleMaskFullScreen) != 0);
    // Unity's custom NSView replaces its NSOpenGLContext without detaching the
    // old drawable. On this macOS driver that leaves fullscreen presentation
    // referencing stale state. Detach while the old wrapper is still alive.
    NSOpenGLContext *previous = object_getIvar(view, contextIvar);
    if (previous) {
        [previous clearDrawable];
        Log("detached previous drawable");
        NSLog(@"[EL Context Fix] detached old drawable before setContext (%p -> %p)",
              previous.CGLContextObj, next);
    }
    [view setWantsBestResolutionOpenGLSurface:fullscreen];
    originalSetContext(view, selector, next);
    UpdateDrawableSize();
    NSLog(@"[EL Context Fix Retina] best=%d scale=%.1f points=%.0fx%.0f pixels=%dx%d",
          fullscreen, ((NSView *)view).window.backingScaleFactor,
          ((NSView *)view).bounds.size.width, ((NSView *)view).bounds.size.height,
          atomic_load(&drawableWidth), atomic_load(&drawableHeight));
    Log("setContext completed");
}

__attribute__((visibility("default"))) int ELContextFixInstall(void)
{
    if (originalSetContext) return 1;
    if (![NSThread isMainThread]) return -1;
    Class cls = objc_getClass("PlayerWindowView");
    if (!cls) return -2;
    playerViewClass = cls;
    executableSlide = _dyld_get_image_vmaddr_slide(0);
    contextIvar = class_getInstanceVariable(cls, "m_OpenGLContext");
    Method method = class_getInstanceMethod(cls, sel_registerName("setContext:"));
    if (!contextIvar || !method) return -3;
    originalSetContext = (void *)method_setImplementation(method, (IMP)SetContext);
    Method rectangle = class_getInstanceMethod(NSWindow.class, @selector(contentRectForFrameRect:));
    originalContentRect = (void *)method_setImplementation(rectangle, (IMP)ContentRect);
    [NSNotificationCenter.defaultCenter addObserverForName:NSWindowDidResizeNotification object:nil queue:nil usingBlock:^(NSNotification *notification) {
        if (notification.object == playerView.window) UpdateDrawableSize();
    }];
    for (NSNotificationName name in @[NSApplicationDidBecomeActiveNotification,
                                     NSApplicationDidResignActiveNotification]) {
        [NSNotificationCenter.defaultCenter addObserverForName:name object:nil queue:nil
            usingBlock:^(NSNotification *notification) {
                Log([notification.name isEqualToString:NSApplicationDidBecomeActiveNotification]
                    ? "application became active" : "application resigned active");
            }];
    }
    NSLog(@"[EL Context Fix] installed drawable-detach hook on PlayerWindowView");
    Log("installed drawable-detach hook on PlayerWindowView");
    return 1;
}

__attribute__((constructor)) static void Bootstrap(void)
{
    @autoreleasepool {
        Log("constructor started");
        int result = ELContextFixInstall();
        if (result != 1) Log("constructor: hook installation failed");
    }
}
