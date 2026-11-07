// Read-only Apple Silicon telemetry. No game injection or fan-control writes.
// SMC wire layout follows the established AppleSMC user-client interface.
#import <AppKit/AppKit.h>
#import <IOKit/IOKitLib.h>
#import <IOKit/ps/IOPowerSources.h>
#include <libproc.h>
#include <math.h>
#include <signal.h>
#include <stddef.h>
#include <time.h>
#include <unistd.h>

typedef struct { uint8_t major, minor, build, reserved; uint16_t release; } SMCVersion;
typedef struct { uint16_t version, length; uint32_t cpu, gpu, memory; } SMCLimits;
typedef struct { uint32_t size, type; uint8_t attributes; } SMCInfo;
typedef struct {
    uint32_t key; SMCVersion version; SMCLimits limits; SMCInfo info;
    uint8_t result, status, command; uint32_t index; uint8_t bytes[32];
} SMCPacket;
_Static_assert(sizeof(SMCPacket) == 80 && offsetof(SMCPacket, bytes) == 48, "SMC ABI");

static io_connect_t smc;
static io_service_t accelerator;
static NSMutableDictionary<NSString *, NSDictionary *> *keys;
static volatile sig_atomic_t stopped;
static void stop(int signalNumber) { (void)signalNumber; stopped = 1; }

static double monotonic(void)
{
    struct timespec value;
    clock_gettime(CLOCK_MONOTONIC, &value);
    return value.tv_sec + value.tv_nsec / 1e9;
}

static uint32_t fourcc(const char *text)
{
    return (uint32_t)(uint8_t)text[0] << 24 | (uint32_t)(uint8_t)text[1] << 16 |
           (uint32_t)(uint8_t)text[2] << 8 | (uint8_t)text[3];
}

static BOOL smcCall(SMCPacket *input, SMCPacket *output)
{
    size_t length = sizeof(*output);
    memset(output, 0, length);
    return smc && IOConnectCallStructMethod(smc, 2, input, sizeof(*input), output, &length) == KERN_SUCCESS &&
           length == sizeof(*output) && output->result == 0;
}

static BOOL smcRead(NSString *key, SMCPacket *output)
{
    SMCPacket input = {0}, info = {0};
    input.key = fourcc(key.UTF8String);
    NSDictionary *cached = keys[key];
    if (cached) {
        info.info.size = [cached[@"size"] unsignedIntValue];
        info.info.type = [cached[@"type"] unsignedIntValue];
        info.info.attributes = [cached[@"attributes"] unsignedCharValue];
    } else {
        input.command = 9; // Read key information.
        if (!smcCall(&input, &info) || info.info.size > 32) return NO;
        keys[key] = @{@"size": @(info.info.size), @"type": @(info.info.type), @"attributes": @(info.info.attributes)};
    }
    input.info = info.info;
    input.command = 5; // Read key bytes. This tool has no write command.
    if (!smcCall(&input, output)) return NO;
    output->info = info.info;
    return YES;
}

static NSNumber *smcNumber(NSString *key)
{
    SMCPacket packet;
    if (!smcRead(key, &packet)) return nil;
    double value = NAN;
    if (packet.info.type == fourcc("flt ") && packet.info.size == 4) {
        float number; memcpy(&number, packet.bytes, 4); value = number;
    } else if (packet.info.type == fourcc("sp78") && packet.info.size == 2) {
        value = (int16_t)((uint16_t)packet.bytes[0] << 8 | packet.bytes[1]) / 256.0;
    } else if (packet.info.type == fourcc("fpe2") && packet.info.size == 2) {
        value = ((uint16_t)packet.bytes[0] << 8 | packet.bytes[1]) / 4.0;
    } else if (packet.info.type == fourcc("ui8 ") && packet.info.size == 1) {
        value = packet.bytes[0];
    } else if (packet.info.type == fourcc("ui16") && packet.info.size == 2) {
        value = (uint16_t)packet.bytes[0] << 8 | packet.bytes[1];
    } else if (packet.info.type == fourcc("ui32") && packet.info.size == 4) {
        value = (uint32_t)packet.bytes[0] << 24 | (uint32_t)packet.bytes[1] << 16 |
                (uint32_t)packet.bytes[2] << 8 | packet.bytes[3];
    }
    return isfinite(value) ? @(value) : nil;
}

static void emit(NSDictionary *record)
{
    NSError *error = nil;
    NSData *data = [NSJSONSerialization dataWithJSONObject:record options:NSJSONWritingSortedKeys error:&error];
    if (!data) { fprintf(stderr, "JSON encoding failed: %s\n", error.description.UTF8String); return; }
    fwrite(data.bytes, 1, data.length, stdout); fputc('\n', stdout); fflush(stdout);
}

static NSNumber *mean(NSArray<NSNumber *> *values)
{
    if (!values.count) return nil;
    double total = 0;
    for (NSNumber *value in values) total += value.doubleValue;
    return @(total / values.count);
}

int main(int argc, char **argv)
{
    @autoreleasepool {
        double interval = 1;
        int sampleCount = -1;
        NSString *gameExecutable = @"";
        for (int i = 1; i < argc; ++i) {
            if (!strcmp(argv[i], "--interval") && i + 1 < argc) interval = atof(argv[++i]);
            else if (!strcmp(argv[i], "--count") && i + 1 < argc) sampleCount = atoi(argv[++i]);
            else if (!strcmp(argv[i], "--game-executable") && i + 1 < argc) gameExecutable = @(argv[++i]);
            else { fprintf(stderr, "Unexpected telemetry argument\n"); return 2; }
        }
        if (!isfinite(interval) || interval < 0.2) return 2;
        signal(SIGINT, stop); signal(SIGTERM, stop); signal(SIGPIPE, stop);
        keys = [NSMutableDictionary dictionary];
        NSMutableArray<NSString *> *temperatureKeys = [NSMutableArray array];
        NSMutableArray<NSString *> *fanKeys = [NSMutableArray array];
        NSMutableArray<NSString *> *warnings = [NSMutableArray array];

        // Apple Silicon exposes several AppleSMC services. The first match is
        // the controller, which rejects this user client; read keys through the
        // specific KeysEndpoint, as used by established SMC readers.
        io_iterator_t services = 0;
        kern_return_t opened = kIOReturnNotFound;
        if (IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("AppleSMC"), &services) == KERN_SUCCESS) {
            io_service_t service;
            while ((service = IOIteratorNext(services))) {
                io_name_t name;
                BOOL endpoint = IORegistryEntryGetName(service, name) == KERN_SUCCESS && !strcmp(name, "AppleSMCKeysEndpoint");
                if (endpoint) opened = IOServiceOpen(service, mach_task_self(), 0, &smc);
                IOObjectRelease(service);
                if (endpoint) break;
            }
            IOObjectRelease(services);
        }
        if (opened != KERN_SUCCESS) {
            [warnings addObject:[NSString stringWithFormat:@"SMC unavailable (0x%08x): temperatures and fans are null", opened]];
        } else {
            NSNumber *count = smcNumber(@"#KEY");
            if (!count || count.unsignedIntValue > 20000) {
                [warnings addObject:@"SMC key enumeration unavailable"];
            } else {
                for (uint32_t index = 0; index < count.unsignedIntValue; ++index) {
                    SMCPacket input = {0}, output = {0}; input.command = 8; input.index = index;
                    if (!smcCall(&input, &output)) continue;
                    char bytes[5] = {(char)(output.key >> 24), (char)(output.key >> 16),
                                     (char)(output.key >> 8), (char)output.key, 0};
                    BOOL temperature = bytes[0] == 'T' && strchr("geps", bytes[1]);
                    BOOL fan = bytes[0] == 'F' && bytes[2] == 'A' && bytes[3] == 'c';
                    if (!temperature && !fan) continue;
                    NSString *key = [NSString stringWithUTF8String:bytes];
                    if (!key) continue;
                    NSNumber *value = smcNumber(key);
                    if (fan && value) [fanKeys addObject:key];
                    else if (value && value.doubleValue > 0 && value.doubleValue < 150) [temperatureKeys addObject:key];
                }
            }
        }
        [temperatureKeys sortUsingSelector:@selector(compare:)];
        [fanKeys sortUsingSelector:@selector(compare:)];
        accelerator = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("AGXAccelerator"));
        if (!accelerator) [warnings addObject:@"AGX GPU utilization unavailable"];
        emit(@{@"kind": @"capabilities", @"temperature_keys": temperatureKeys, @"fan_keys": fanKeys,
               @"smc_available": @(opened == KERN_SUCCESS), @"gpu_service_available": @(accelerator != 0),
               @"warnings": warnings, @"pid": @(getpid())});

        double next = monotonic();
        for (int index = 0; !stopped && (sampleCount < 0 || index < sampleCount); ++index) {
            @autoreleasepool {
                double began = monotonic();
                NSMutableDictionary *row = [@{@"kind": @"sample", @"monotonic_s": @(began),
                    @"thermal_state": @([NSProcessInfo processInfo].thermalState)} mutableCopy];
                CGDisplayModeRef mode = CGDisplayCopyDisplayMode(CGMainDisplayID());
                if (mode) {
                    row[@"display_pixel_width"] = @(CGDisplayModeGetPixelWidth(mode));
                    row[@"display_pixel_height"] = @(CGDisplayModeGetPixelHeight(mode));
                    double refresh = CGDisplayModeGetRefreshRate(mode);
                    if (refresh > 0) row[@"display_refresh_hz"] = @(refresh);
                    CGDisplayModeRelease(mode);
                }
                row[@"display_query_ms"] = @((monotonic() - began) * 1000);
                double stage = monotonic();
                CFTypeRef power = IOPSCopyPowerSourcesInfo();
                if (power) {
                    CFStringRef source = IOPSGetProvidingPowerSourceType(power);
                    if (source) row[@"power_source"] = (__bridge NSString *)source;
                    CFRelease(power);
                }
                row[@"power_source_query_ms"] = @((monotonic() - stage) * 1000);
                stage = monotonic();
                if (accelerator) {
                    NSDictionary *stats = CFBridgingRelease(IORegistryEntryCreateCFProperty(
                        accelerator, CFSTR("PerformanceStatistics"), kCFAllocatorDefault, 0));
                    if ([stats isKindOfClass:NSDictionary.class]) {
                        for (NSArray *pair in @[@[@"Device Utilization %", @"gpu_util_pct"],
                                                @[@"Renderer Utilization %", @"gpu_renderer_pct"],
                                                @[@"Tiler Utilization %", @"gpu_tiler_pct"],
                                                @[@"In use system memory", @"gpu_memory_bytes"]]) {
                            id value = stats[pair[0]];
                            if ([value isKindOfClass:NSNumber.class]) row[pair[1]] = value;
                        }
                    }
                }
                row[@"gpu_query_ms"] = @((monotonic() - stage) * 1000);
                stage = monotonic();
                NSMutableDictionary *temperatures = [NSMutableDictionary dictionary];
                NSMutableArray *gpu = [NSMutableArray array], *cpu = [NSMutableArray array];
                for (NSString *key in temperatureKeys) {
                    NSNumber *value = smcNumber(key);
                    if (!value || value.doubleValue <= 0 || value.doubleValue >= 150) continue;
                    temperatures[key] = value;
                    [( [key hasPrefix:@"Tg"] ? gpu : cpu) addObject:value];
                }
                row[@"temperatures_c"] = temperatures;
                row[@"gpu_temp_mean_c"] = mean(gpu) ?: NSNull.null;
                row[@"gpu_temp_max_c"] = [gpu valueForKeyPath:@"@max.self"] ?: NSNull.null;
                row[@"cpu_temp_mean_c"] = mean(cpu) ?: NSNull.null;
                row[@"cpu_temp_max_c"] = [cpu valueForKeyPath:@"@max.self"] ?: NSNull.null;
                NSMutableDictionary *fans = [NSMutableDictionary dictionary];
                for (NSString *key in fanKeys) { NSNumber *value = smcNumber(key); if (value) fans[key] = value; }
                row[@"fans_rpm"] = fans;
                row[@"smc_query_ms"] = @((monotonic() - stage) * 1000);
                stage = monotonic();

                row[@"game_pid"] = NSNull.null;
                // Let NSWorkspace deliver launch/termination notifications.
                // Avoid scanning every system process/path once a second.
                CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0, true);
                pid_t found = 0;
                NSRunningApplication *runningGame = nil;
                NSString *gameBundle = gameExecutable.stringByDeletingLastPathComponent
                    .stringByDeletingLastPathComponent.stringByDeletingLastPathComponent;
                for (NSRunningApplication *app in NSWorkspace.sharedWorkspace.runningApplications) {
                    if (![app.executableURL.path.stringByResolvingSymlinksInPath isEqualToString:gameExecutable] &&
                        ![app.bundleURL.path.stringByResolvingSymlinksInPath isEqualToString:gameBundle]) continue;
                    found = app.processIdentifier;
                    runningGame = app;
                    break;
                }
                if (found > 0) {
                    row[@"game_pid"] = @(found);
                    if (runningGame) row[@"game_active"] = @(runningGame.isActive);
                    struct proc_taskinfo task;
                    if (proc_pidinfo(found, PROC_PIDTASKINFO, 0, &task, sizeof(task)) == sizeof(task)) {
                        row[@"game_cpu_time_ns"] = @(task.pti_total_user + task.pti_total_system);
                        row[@"game_rss_bytes"] = @(task.pti_resident_size);
                    }
                }
                row[@"process_query_ms"] = @((monotonic() - stage) * 1000);
                row[@"collection_ms"] = @((monotonic() - began) * 1000);
                emit(row);
            }
            next += interval;
            double wait = next - monotonic();
            if (wait > 0 && !stopped) usleep((useconds_t)(wait * 1e6));
            else if (wait < -interval) next = monotonic();
        }
        if (accelerator) IOObjectRelease(accelerator);
        if (smc) IOServiceClose(smc);
    }
    return 0;
}
