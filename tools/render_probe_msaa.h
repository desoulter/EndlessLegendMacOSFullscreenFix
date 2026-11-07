// The diagnostic interposer can replace, rather than chain through, the release
// interposer. Preserve its safety clamp before applying an explicit experiment.
#ifndef EL_RENDER_PROBE_MSAA_H
#define EL_RENDER_PROBE_MSAA_H

#include <stdbool.h>

static inline int ELProbeMSAASamples(int requested, int driverMaximum,
                                    bool patched, bool fullscreenAllocation,
                                    int experimentCap)
{
    if (!patched || !fullscreenAllocation) return requested;
    int samples = requested;
    if (driverMaximum > 0 && samples > driverMaximum) samples = driverMaximum;
    if ((experimentCap == 0 || experimentCap == 2 || experimentCap == 4) && samples > experimentCap)
        samples = experimentCap;
    return samples;
}

#endif
