// Temporary observer for the exact Unity 5.3.6p1 player supported by this repo.
// No pixel readbacks, GPU waits, or per-frame file writes. Removed after recording.
#import <AppKit/AppKit.h>
#import <OpenGL/OpenGL.h>
#import <OpenGL/gl.h>
#import <OpenGL/glext.h>
#import <mach-o/dyld.h>
#import <objc/runtime.h>
#include <dlfcn.h>
#include <errno.h>
#include <execinfo.h>
#include <math.h>
#include <pthread.h>
#include <time.h>
#include <unistd.h>
#include "render_probe_msaa.h"

static FILE *output;
static pthread_mutex_t outputLock = PTHREAD_MUTEX_INITIALIZER;
static intptr_t slide;
static BOOL ready;
static BOOL patchedMode;
static int msaaCap = -1, fpsCap;
static int (*nativeFrameCap)(void);
static int measurementSeconds, warmupSeconds;
static BOOL measurementMarked;
static void (*originalSendEvent)(id, SEL, NSEvent *);
#ifdef EL_PROBE_INTERPOSITION_TEST
// Test-only entry point: exercise the actual observer with a fake GL backend,
// real dyld interposition and a supplied caller address, without a graphics session.
static uintptr_t testAllocationCaller;
static int testSignals;
static int testPresents;
__attribute__((visibility("default"))) void ELProbeTestConfigure(BOOL patched, int cap, void *caller)
{
    ready = YES;
    patchedMode = patched;
    msaaCap = cap;
    slide = _dyld_get_image_vmaddr_slide(0);
    testAllocationCaller = (uintptr_t)caller - slide;
}
__attribute__((visibility("default"))) void ELProbeTestFrameCap(int cap)
{
    fpsCap = cap;
    nativeFrameCap = (int (*)(void))dlsym(RTLD_DEFAULT, "ELContextFixGetFrameCap");
}
__attribute__((visibility("default"))) int ELProbeTestPresents(void) { return testPresents; }
#endif

static double Now(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

static void Emit(NSDictionary *record)
{
    if (!output) return;
    @autoreleasepool {
        NSMutableDictionary *row = record.mutableCopy;
        row[@"timestamp_epoch_s"] = @(NSDate.date.timeIntervalSince1970);
        row[@"pid"] = @(getpid());
        NSData *json = [NSJSONSerialization dataWithJSONObject:row options:NSJSONWritingSortedKeys error:nil];
        if (!json) return;
        pthread_mutex_lock(&outputLock);
        fwrite(json.bytes, 1, json.length, output);
        fputc('\n', output);
        fflush(output);
        pthread_mutex_unlock(&outputLock);
    }
}

static void SignalMeasurement(void)
{
#ifdef EL_PROBE_INTERPOSITION_TEST
    testSignals++;
#else
    NSBeep();
#endif
}

static void SendEvent(id app, SEL selector, NSEvent *event)
{
    // F8 is consumed only when the caller explicitly requested a marked run.
    // Observe the game's own sendEvent: implementation: exclusive fullscreen
    // does not need Cmd-Tab, a global keyboard listener or OS input permissions.
    if (event.type == NSEventTypeKeyDown && event.keyCode == 100) {
        if (!event.isARepeat && !measurementMarked) {
            measurementMarked = YES;
            Emit(@{@"kind": @"measurement_marker", @"warmup_s": @(warmupSeconds),
                   @"duration_s": @(measurementSeconds)});
            SignalMeasurement();
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(measurementSeconds + warmupSeconds) * NSEC_PER_SEC),
                           dispatch_get_main_queue(), ^{
                Emit(@{@"kind": @"measurement_finished"});
                SignalMeasurement();
            });
        }
        return;
    }
    if (event.type == NSEventTypeKeyUp && event.keyCode == 100) return;
    originalSendEvent(app, selector, event);
}

static BOOL InstallMeasurementHook(Class application)
{
    Method method = application ? class_getInstanceMethod(application, @selector(sendEvent:)) : NULL;
    if (!method) return NO;
    originalSendEvent = (void *)method_setImplementation(method, (IMP)SendEvent);
    return YES;
}

#ifdef EL_PROBE_INTERPOSITION_TEST
__attribute__((visibility("default"))) BOOL ELProbeTestMeasurementHook(Class application)
{
    measurementSeconds = 180;
    warmupSeconds = 30;
    measurementMarked = NO;
    testSignals = 0;
    return InstallMeasurementHook(application);
}
__attribute__((visibility("default"))) int ELProbeTestSignals(void) { return testSignals; }
#endif

static void *Screen(void)
{
    return ((void *(*)(void))(0x100a4c570 + slide))();
}

static int IntegerAt(void *base, size_t offset)
{
    int value;
    memcpy(&value, (char *)base + offset, sizeof(value));
    return value;
}

static void Snapshot(NSMutableDictionary *row, CGLContextObj ctx)
{
    // Called on Unity's rendering thread, with this context already current.
    GLint swap = -1, viewport[4] = {0}, framebuffer = -1;
    if (CGLGetParameter(ctx, kCGLCPSwapInterval, &swap) == kCGLNoError)
        row[@"swap_interval"] = @(swap);
    glGetIntegerv(GL_VIEWPORT, viewport);
    row[@"present_viewport_width"] = @(viewport[2]);
    row[@"present_viewport_height"] = @(viewport[3]);
    glGetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING_EXT, &framebuffer);
    row[@"present_framebuffer"] = @(framebuffer);
    if (framebuffer == 0) {
        GLint samples = 0;
        glGetIntegerv(GL_SAMPLES, &samples);
        row[@"surface_samples"] = @(samples);
    }
    void *manager = Screen();
    if (manager) {
        row[@"render_width"] = @(IntegerAt(manager, 0x64));
        row[@"render_height"] = @(IntegerAt(manager, 0x68));
        row[@"resolve_fbo"] = @(IntegerAt(manager, 0x148));
        row[@"msaa_fbo"] = @(IntegerAt(manager, 0x154));
        GLuint color = (GLuint)IntegerAt(manager, 0x150);
        if (color && IntegerAt(manager, 0x154)) {
            GLint saved = 0, width = 0, height = 0, samples = 0;
            glGetIntegerv(GL_RENDERBUFFER_BINDING_EXT, &saved);
            glBindRenderbufferEXT(GL_RENDERBUFFER_EXT, color);
            glGetRenderbufferParameterivEXT(GL_RENDERBUFFER_EXT, GL_RENDERBUFFER_WIDTH_EXT, &width);
            glGetRenderbufferParameterivEXT(GL_RENDERBUFFER_EXT, GL_RENDERBUFFER_HEIGHT_EXT, &height);
            glGetRenderbufferParameterivEXT(GL_RENDERBUFFER_EXT, GL_RENDERBUFFER_SAMPLES_EXT, &samples);
            glBindRenderbufferEXT(GL_RENDERBUFFER_EXT, (GLuint)saved);
            row[@"msaa_fbo_width"] = @(width);
            row[@"msaa_fbo_height"] = @(height);
            row[@"msaa_fbo_samples"] = @(samples);
        }
    }
    void *quality = ((void *(*)(void))(0x100a35710 + slide))();
    if (quality) {
        int index = IntegerAt(quality, 0x44);
        void *settings = NULL;
        memcpy(&settings, (char *)quality + 0x28, sizeof(settings));
        if (settings && index >= 0 && index < 32) {
            row[@"quality_msaa"] = @(IntegerAt(settings, index * 0x68 + 0x40));
            row[@"quality_vsync"] = @(IntegerAt(settings, index * 0x68 + 0x44));
        }
    }
    row[@"target_fps_script"] = @(((int (*)(void))(0x100ab08d0 + slide))());
    float actual = ((float (*)(void))(0x100ab08f0 + slide))();
    if (isfinite(actual)) row[@"target_fps_engine"] = @(actual);
}

static CGLError Present(CGLContextObj ctx)
{
    if (!ready) return CGLFlushDrawable(ctx);
#ifdef EL_PROBE_INTERPOSITION_TEST
    testPresents++;
#endif
    static _Thread_local struct {
        CGLContextObj context;
        double began, last, flushTotal;
        unsigned count;
    } stats;
    double before = Now();
    if (stats.context != ctx) {
        memset(&stats, 0, sizeof(stats));
        stats.context = ctx;
        stats.began = before;
    }
    double flushBegan = Now();
    CGLError result = CGLFlushDrawable(ctx);
    double after = Now();
    stats.flushTotal += after - flushBegan;
    // Preserve the ordinary patch's optional cap even when dyld routes this
    // symbol through the observer instead of through the production interposer.
    int inheritedCap = nativeFrameCap ? nativeFrameCap() : 0;
    int effectiveCap = fpsCap;
    if (inheritedCap > 0 && (effectiveCap <= 0 || inheritedCap < effectiveCap)) effectiveCap = inheritedCap;
    // Explicit experiment only. Account for the real swap/VSync wait before
    // sleeping; sleeping before swap can miss a vblank and turn a 60 FPS cap
    // into ~40 FPS on a 120 Hz display. Do not add delay to a slow frame.
    if (effectiveCap > 0 && stats.last > 0) {
        double delay = stats.last + 1.0 / effectiveCap - after;
        if (delay > 0) {
            struct timespec t = {(time_t)delay, (long)((delay - floor(delay)) * 1e9)};
            while (nanosleep(&t, &t) && errno == EINTR) {}
            after = Now();
        }
    }
    stats.last = after;
    stats.count++;
    double duration = after - stats.began;
    if (duration >= 1) {
        @autoreleasepool {
            NSMutableDictionary *row = [@{@"kind": @"frame", @"present_fps": @(stats.count / duration),
                @"present_count": @(stats.count), @"window_s": @(duration),
                @"present_flush_ms": @(1000 * stats.flushTotal / stats.count),
                @"native_fps_cap": @(inheritedCap), @"effective_fps_cap": @(effectiveCap),
                @"context": [NSString stringWithFormat:@"%p", ctx], @"present_error": @(result)} mutableCopy];
            double queryBegan = Now();
            if (CGLGetCurrentContext() == ctx) Snapshot(row, ctx);
            row[@"probe_query_ms"] = @((Now() - queryBegan) * 1000);
            Emit(row);
        }
        stats.count = 0;
        stats.flushTotal = 0;
        stats.began = after;
    }
    return result;
}

// Interposition may be chained through the release helper. Check the stack for
// the two exact Unity allocation return addresses instead of guessing the caller.
static BOOL FullscreenAllocation(void)
{
    void *frames[16];
    int count = backtrace(frames, 16);
    for (int i = 0; i < count; ++i) {
        uintptr_t address = (uintptr_t)frames[i] - slide;
        if (address == 0x101087c68 || address == 0x101087cc4) return YES;
#ifdef EL_PROBE_INTERPOSITION_TEST
        if (testAllocationCaller && address == testAllocationCaller) return YES;
#endif
    }
    return NO;
}

static void Multisample(GLenum target, GLsizei samples, GLenum format, GLsizei width, GLsizei height)
{
    GLsizei requested = samples;
    BOOL fullscreen = ready && patchedMode && FullscreenAllocation();
    GLint maximum = 0;
    if (fullscreen) glGetIntegerv(GL_MAX_SAMPLES_EXT, &maximum);
    // Do not assume two dyld interposers for the same symbol will compose.
    // In patched mode this observer must preserve the production helper's
    // driver-limit clamp even when no MSAA experiment has been requested.
    samples = ELProbeMSAASamples(samples, maximum, patchedMode, fullscreen, msaaCap);
    glRenderbufferStorageMultisampleEXT(target, samples, format, width, height);
    if (ready) {
        GLint actual = -1, actualWidth = 0, actualHeight = 0;
        glGetRenderbufferParameterivEXT(target, GL_RENDERBUFFER_SAMPLES_EXT, &actual);
        glGetRenderbufferParameterivEXT(target, GL_RENDERBUFFER_WIDTH_EXT, &actualWidth);
        glGetRenderbufferParameterivEXT(target, GL_RENDERBUFFER_HEIGHT_EXT, &actualHeight);
        Emit(@{@"kind": @"allocation", @"requested_samples": @(requested), @"passed_samples": @(samples),
            @"actual_samples": @(actual), @"width": @(width), @"height": @(height),
            @"actual_width": @(actualWidth), @"actual_height": @(actualHeight),
            @"format": @(format), @"driver_max_samples": @(maximum),
            @"fullscreen_allocation": @(fullscreen), @"patched_mode": @(patchedMode),
            @"safety_clamp": @(fullscreen && maximum > 0 && requested > maximum),
            @"fullscreen_experiment": @(fullscreen && msaaCap >= 0)});
    }
}

static CGLError Create(CGLPixelFormatObj pixel, CGLContextObj shared, CGLContextObj *ctx)
{
    CGLError result = CGLCreateContext(pixel, shared, ctx);
    if (ready && result == kCGLNoError && ctx && *ctx) {
        GLint samples = 0, buffers = 0;
        CGLDescribePixelFormat(pixel, 0, kCGLPFASamples, &samples);
        CGLDescribePixelFormat(pixel, 0, kCGLPFASampleBuffers, &buffers);
        Emit(@{@"kind": @"context", @"context": [NSString stringWithFormat:@"%p", *ctx],
            @"pixel_format_samples": @(samples), @"pixel_format_sample_buffers": @(buffers)});
    }
    return result;
}

#define INTERPOSE(name, replacement, original) \
    __attribute__((used)) static struct { const void *newFunction; const void *oldFunction; } \
    name __attribute__((section("__DATA,__interpose"))) = {(void *)replacement, (void *)original}
INTERPOSE(presentHook, Present, CGLFlushDrawable);
INTERPOSE(allocationHook, Multisample, glRenderbufferStorageMultisampleEXT);
INTERPOSE(contextHook, Create, CGLCreateContext);

__attribute__((constructor)) static void Start(void)
{
    @autoreleasepool {
        // Loading the dylib in the standalone loader test must not call Unity.
        if (!objc_getClass("PlayerWindowView")) return;
        Dl_info image;
        if (!dladdr((void *)Start, &image) || !image.dli_fname) return;
        NSString *root = @(image.dli_fname);
        for (int i = 0; i < 4; ++i) root = root.stringByDeletingLastPathComponent;
        NSString *directory = [root stringByAppendingPathComponent:@"ContextFix/render-probe"];
        NSData *data = [NSData dataWithContentsOfFile:[directory stringByAppendingPathComponent:@"config.json"]];
        NSDictionary *config = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
        if (![config isKindOfClass:NSDictionary.class] || ![config[@"supported_player"] isEqual:@YES]) return;
        patchedMode = [config[@"patched_mode"] isEqual:@YES];
        NSNumber *cap = config[@"msaa_cap"];
        if ([cap isKindOfClass:NSNumber.class] &&
            (cap.intValue == 0 || cap.intValue == 2 || cap.intValue == 4)) msaaCap = cap.intValue;
        int fps = [config[@"fps_cap"] intValue];
        if (fps >= 15 && fps <= 240) fpsCap = fps;
        int seconds = [config[@"measurement_seconds"] intValue];
        if (seconds >= 30 && seconds <= 3600) measurementSeconds = seconds;
        int warmup = [config[@"warmup_seconds"] intValue];
        if (warmup >= 0 && warmup <= 300) warmupSeconds = warmup;
        NSString *logs = [directory stringByAppendingPathComponent:@"logs"];
        [NSFileManager.defaultManager createDirectoryAtPath:logs withIntermediateDirectories:YES attributes:nil error:nil];
        NSString *path = [logs stringByAppendingPathComponent:[NSString stringWithFormat:@"%d.jsonl", getpid()]];
        output = fopen(path.fileSystemRepresentation, "a");
        if (!output) return;
        slide = _dyld_get_image_vmaddr_slide(0);
        ready = YES;
        nativeFrameCap = (int (*)(void))dlsym(RTLD_DEFAULT, "ELContextFixGetFrameCap");
        BOOL hotkey = NO;
        if (measurementSeconds > 0) {
            hotkey = InstallMeasurementHook(objc_getClass("PlayerApplication"));
        }
        Emit(@{@"kind": @"probe_started", @"schema": @3, @"patched_mode": @(patchedMode),
               @"measurement_hotkey": @(hotkey), @"measurement_seconds": @(measurementSeconds),
               @"msaa_cap": @(msaaCap), @"fps_cap": @(fpsCap)});
    }
}
