/* SPDX-License-Identifier: GPL-3.0-or-later
   Copyright 2026 the ScarletNX contributors */
/* ffx_vk.cpp: FSR 4.1 on Linux through the FSFR4 Vulkan provider (third_party/fsr4vk, GPL-3.0-or-later, by
   dvj5411). The provider exports AMD's FidelityFX API and runs the INT8 FSR 4.1 model on any Vulkan 1.2 device with
   the descriptor buffer, mutable descriptor and compute shader derivative extensions. Its model files are AMD's, MIT,
   and live beside the provider (FSR4_VK_ASSET_ROOT, or the ffx_runtime setting). One context per upscaler, one
   dispatch per evaluate, as the Windows side: the two differ only in how the API is reached, linked here rather than
   through a loader DLL */
#include "nx/gpu/vk/upscaler/ffx.h"
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#if defined(SNX_FSR4_VK)

#include <vulkan/vulkan.h>

/* the provider's exports, linked into the upscaler: the same five functions AMD's loader provides */
#include <ffx_api.h>
#include <ffx_upscale.h>

#if defined(_MSC_VER)
#pragma warning(push, 0)
#endif
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Weverything"
#include "ffx_vk_device_requirements.h"
#pragma clang diagnostic pop
#if defined(_MSC_VER)
#pragma warning(pop)
#endif

extern "C" void snx_fsr4vk_set_asset_root(const char* path);

#ifndef SNX_FSR4_ASSETS
#define SNX_FSR4_ASSETS "third_party/fsr4vk/assets"
#endif

struct SnxFfx {
    SnxUpscalerInfo info;
    ffxContext ctx;
    int failed;
    char fail_reason[192];
};

namespace {

struct State {
    SnxFfxLog log;
    int started, failed;
    char why[256];
    char fail_reason[256];
};
State g;

void say(int level, const char* fmt, ...) {
    if (!g.log) return;
    char line[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);
    g.log(level, line);
}

const char* code_name(ffxReturnCode_t rc) {
    switch (rc) {
        case FFX_API_RETURN_OK: return "OK";
        case FFX_API_RETURN_ERROR: return "error";
        case FFX_API_RETURN_ERROR_UNKNOWN_DESCTYPE: return "a description type it does not know";
        case FFX_API_RETURN_ERROR_RUNTIME_ERROR: return "a runtime error";
        case FFX_API_RETURN_NO_PROVIDER: return "no provider";
        case FFX_API_RETURN_ERROR_MEMORY: return "no memory";
        case FFX_API_RETURN_ERROR_PARAMETER: return "a parameter it refuses";
        case FFX_API_RETURN_PROVIDER_NO_SUPPORT_NEW_DESCTYPE: return "a provider that knows no newer description";
        default: return "an unknown code";
    }
}

/* the provider's versions are named like " 4.1.1 VK INT8": only one whose name has 4.1 runs */
int is_41(const char* name) { return name && strstr(name, "4.1") != NULL; }

int srgb(VkFormat f) {
    switch (f) {
        case VK_FORMAT_R8_SRGB:
        case VK_FORMAT_R8G8_SRGB:
        case VK_FORMAT_R8G8B8_SRGB:
        case VK_FORMAT_B8G8R8_SRGB:
        case VK_FORMAT_B8G8R8A8_SRGB:
        case VK_FORMAT_A8B8G8R8_SRGB_PACK32: return 1;
        default: return 0;
    }
}

FfxApiSurfaceFormat surface(VkFormat f) {
    switch (f) {
        case VK_FORMAT_R8_UNORM: return FFX_API_SURFACE_FORMAT_R8_UNORM;
        case VK_FORMAT_R8G8_UNORM: return FFX_API_SURFACE_FORMAT_R8G8_UNORM;
        case VK_FORMAT_R8G8B8A8_UNORM: return FFX_API_SURFACE_FORMAT_R8G8B8A8_UNORM;
        case VK_FORMAT_R8G8B8A8_SRGB: return FFX_API_SURFACE_FORMAT_R8G8B8A8_SRGB;
        case VK_FORMAT_B8G8R8A8_UNORM: return FFX_API_SURFACE_FORMAT_B8G8R8A8_UNORM;
        case VK_FORMAT_B8G8R8A8_SRGB: return FFX_API_SURFACE_FORMAT_B8G8R8A8_SRGB;
        case VK_FORMAT_A2B10G10R10_UNORM_PACK32: return FFX_API_SURFACE_FORMAT_R10G10B10A2_UNORM;
        case VK_FORMAT_R16G16_SFLOAT: return FFX_API_SURFACE_FORMAT_R16G16_FLOAT;
        case VK_FORMAT_R16G16B16A16_SFLOAT: return FFX_API_SURFACE_FORMAT_R16G16B16A16_FLOAT;
        case VK_FORMAT_R32_SFLOAT: return FFX_API_SURFACE_FORMAT_R32_FLOAT;
        case VK_FORMAT_R32_UINT: return FFX_API_SURFACE_FORMAT_R32_UINT;
        case VK_FORMAT_D32_SFLOAT: return FFX_API_SURFACE_FORMAT_R32_FLOAT;
        case VK_FORMAT_D24_UNORM_S8_UINT: return FFX_API_SURFACE_FORMAT_R32_UINT;
        default: return FFX_API_SURFACE_FORMAT_UNKNOWN;
    }
}

/* the provider's backend description, before the FFX API's own backend structure: the provider reads the device,
   the physical device and the loader's vkGetDeviceProcAddr from it */
struct CreateBackendVkDesc {
    ffxCreateContextDescHeader header;
    VkDevice device;
    VkPhysicalDevice physicalDevice;
    PFN_vkGetDeviceProcAddr getDeviceProcAddr;
};

struct Fsr4VulkanApiVersionDesc {
    ffxApiHeader header;
    uint32_t apiVersion;
};

FfxApiResource resource(const SnxUpscaleImage* i, uint32_t state, uint32_t usage) {
    FfxApiResource r = {};
    r.resource = (void*)i->image;
    r.description.type = FFX_API_RESOURCE_TYPE_TEXTURE2D;
    r.description.format = surface(i->format);
    r.description.width = i->width;
    r.description.height = i->height;
    r.description.depth = 1;
    r.description.mipCount = 1;
    r.description.flags = 0;
    r.description.usage = usage;
    r.state = state;
    return r;
}

} /* namespace */

extern "C" int snx_ffx_start(const char* folder, SnxFfxLog log, const char** why) {
    if (g.started) return 1;
    g.log = log;
    const char* root = folder && folder[0] ? folder : SNX_FSR4_ASSETS;
    char probe[600];
    struct stat st;
    snprintf(probe, sizeof probe, "%s/fsr411", root);
    if (stat(probe, &st) != 0 || !S_ISDIR(st.st_mode)) {
        snprintf(g.why, sizeof g.why,
                 "no FSR 4.1 model files in %s: point ffx_runtime at the folder holding the provider's assets", root);
        if (why) *why = g.why;
        return 0;
    }
    snx_fsr4vk_set_asset_root(root);
    g.started = 1;
    say(0, "FSR 4.1: the FSR4 Vulkan provider, models in %s", root);
    return 1;
}

extern "C" int snx_ffx_available(const char** why) {
    if (g.started && !g.failed) return 1;
    if (why) *why = g.failed ? g.fail_reason : g.why[0] ? g.why
                                                       : "FSR 4.1 is not running: name the folder holding the FSR4 "
                                                         "Vulkan provider's model files in the ffx_runtime setting";
    return 0;
}

extern "C" int snx_ffx_prepare_wanted(void) { return g.started; }

extern "C" int snx_ffx_built_in_assets(void) { return 1; }

extern "C" int snx_ffx_prepare_device(VkPhysicalDevice phys, const VkDeviceCreateInfo* in,
                                      const VkDeviceCreateInfo** out, void** token, char* err, size_t n) {
    if (!g.started) return 0;
    ffxQueryDescVkPrepareDevice prep = {};
    prep.header.type = FFX_API_QUERY_DESC_TYPE_VK_PREPARE_DEVICE;
    prep.physicalDevice = phys;
    prep.sourceCreateInfo = in;
    prep.apiVersion = VK_API_VERSION_1_3;
    prep.getPhysicalDeviceFeatures2 = vkGetPhysicalDeviceFeatures2;
    prep.enumerateDeviceExtensionProperties = vkEnumerateDeviceExtensionProperties;
    ffxReturnCode_t rc = ffxQuery(NULL, &prep.header);
    if (rc != FFX_API_RETURN_OK || !prep.outputCreateInfo) {
        snprintf(err, n, "%s", prep.errorMessage ? prep.errorMessage
                                                 : "AMD's FSR 4.1 provider would not prepare this device");
        return 0;
    }
    *out = prep.outputCreateInfo;
    *token = prep.token;
    return 1;
}

extern "C" void snx_ffx_device_made(void* token) {
    ffxQueryDescVkReleaseDevice rel = {};
    rel.header.type = FFX_API_QUERY_DESC_TYPE_VK_RELEASE_DEVICE;
    rel.token = token;
    ffxQuery(NULL, &rel.header);
}

extern "C" SnxFfx* snx_ffx_create(const SnxUpscalerInfo* info, const char** why) {
    if (!snx_ffx_available(why)) return NULL;
    SnxFfx* f = (SnxFfx*)calloc(1, sizeof *f);
    if (!f) {
        snprintf(g.why, sizeof g.why, "no memory for an FSR 4.1 context");
        if (why) *why = g.why;
        return NULL;
    }
    f->info = *info;

    uint32_t flags = 0;
    if (info->flags & SNX_UP_FLAG_HDR)
        flags |= FFX_UPSCALE_ENABLE_HIGH_DYNAMIC_RANGE;
    else
        flags |= FFX_UPSCALE_ENABLE_NON_LINEAR_COLORSPACE;
    if (info->flags & SNX_UP_FLAG_DISPLAY_MOTION) flags |= FFX_UPSCALE_ENABLE_DISPLAY_RESOLUTION_MOTION_VECTORS;
    if (info->flags & SNX_UP_FLAG_JITTERED_MOTION) flags |= FFX_UPSCALE_ENABLE_MOTION_VECTORS_JITTER_CANCELLATION;
    if (info->flags & SNX_UP_FLAG_INVERTED_DEPTH) flags |= FFX_UPSCALE_ENABLE_DEPTH_INVERTED;
    if (info->flags & SNX_UP_FLAG_AUTO_EXPOSURE) flags |= FFX_UPSCALE_ENABLE_AUTO_EXPOSURE;

    /* which versions the provider offers: only a 4.1 one is asked for */
    uint64_t count = 0;
    ffxQueryDescGetVersions versions = {};
    versions.header.type = FFX_API_QUERY_DESC_TYPE_GET_VERSIONS;
    versions.createDescType = FFX_API_CREATE_CONTEXT_DESC_TYPE_UPSCALE;
    versions.outputCount = &count;
    ffxQuery(NULL, &versions.header);
    uint64_t ids[8] = {};
    const char* names[8] = {};
    uint64_t got = count < 8 ? count : 8;
    versions.outputCount = &got;
    versions.versionIds = ids;
    versions.versionNames = names;
    ffxQuery(NULL, &versions.header);

    uint64_t version = 0;
    char seen[200];
    size_t at = 0;
    seen[0] = 0;
    for (uint64_t i = 0; i < got; i++) {
        if (names[i]) {
            int wrote = snprintf(seen + at, sizeof seen - at, "%s%s", at ? ", " : "", names[i]);
            if (wrote > 0) at += (size_t)wrote < sizeof seen - at ? (size_t)wrote : sizeof seen - at - 1;
            if (!version && is_41(names[i])) version = ids[i];
        }
    }
    if (!version) {
        snprintf(g.why, sizeof g.why, "the FSR4 provider offers no 4.1 version (%s)", seen[0] ? seen : "none");
        if (why) *why = g.why;
        say(2, "FSR 4.1: %s", g.why);
        free(f);
        return NULL;
    }

    ffxOverrideVersion over = {};
    over.header.type = FFX_API_DESC_TYPE_OVERRIDE_VERSION;
    over.versionId = version;
    Fsr4VulkanApiVersionDesc apiver = {};
    apiver.header.type = 0x46535234564b4150ull;
    apiver.apiVersion = VK_API_VERSION_1_3;
    CreateBackendVkDesc back = {};
    back.header.type = 3u;
    back.device = info->device;
    back.physicalDevice = info->physical;
    back.getDeviceProcAddr = vkGetDeviceProcAddr;
    ffxCreateContextDescUpscale up = {};
    up.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_UPSCALE;
    up.maxRenderSize.width = info->render_max[0];
    up.maxRenderSize.height = info->render_max[1];
    up.maxUpscaleSize.width = info->output[0];
    up.maxUpscaleSize.height = info->output[1];
    up.fpMessage = NULL;

    ffxReturnCode_t rc = FFX_API_RETURN_ERROR;
    for (int drs = 1; drs >= 0 && rc != FFX_API_RETURN_OK; drs--) {
        for (int with_version = 1; with_version >= 0 && rc != FFX_API_RETURN_OK; with_version--) {
            up.flags = flags | (drs ? FFX_UPSCALE_ENABLE_DYNAMIC_RESOLUTION : 0);
            back.header.pNext = with_version ? &over.header : &up.header;
            over.header.pNext = &up.header;
            up.header.pNext = &apiver.header;
            rc = ffxCreateContext(&f->ctx, &back.header, NULL);
        }
    }
    if (rc != FFX_API_RETURN_OK) {
        snprintf(g.why, sizeof g.why, "AMD's FSR 4.1 provider made no context (%s)", code_name(rc));
        if (why) *why = g.why;
        say(2, "FSR 4.1: %s", g.why);
        free(f);
        return NULL;
    }

    /* what was actually made: a 3.1 provider would be a driver fallback this build does not run */
    ffxQueryGetProviderVersion pv = {};
    pv.header.type = FFX_API_QUERY_DESC_TYPE_GET_PROVIDER_VERSION;
    if (ffxQuery(&f->ctx, &pv.header) == FFX_API_RETURN_OK) {
        if (!is_41(pv.versionName)) {
            snprintf(g.why, sizeof g.why, "the provider made %s, not FSR 4.1", pv.versionName ? pv.versionName : "?");
            if (why) *why = g.why;
            say(2, "FSR 4.1: %s", g.why);
            ffxDestroyContext(&f->ctx, NULL);
            free(f);
            return NULL;
        }
        say(0, "FSR 4.1: %s, from %s (%ux%u to %ux%u)", pv.versionName, "the FSR4 Vulkan provider",
            info->render_max[0], info->render_max[1], info->output[0], info->output[1]);
    }
    return f;
}

extern "C" int snx_ffx_evaluate(SnxFfx* f, const SnxUpscaleEval* e, const char** why) {
    if (!f || f->failed) {
        if (why) *why = f ? f->fail_reason : "no FSR 4.1 context";
        return 0;
    }
    if (!e->depth.image || !e->motion.image) {
        snprintf(g.why, sizeof g.why, "FSR 4.1 needs depth and motion vectors");
        if (why) *why = g.why;
        return 0;
    }
    ffxDispatchDescUpscale d = {};
    d.header.type = FFX_API_DISPATCH_DESC_TYPE_UPSCALE;
    d.commandList = (void*)e->cb;
    d.color = resource(&e->color, FFX_API_RESOURCE_STATE_PIXEL_COMPUTE_READ, FFX_API_RESOURCE_USAGE_READ_ONLY);
    d.depth = resource(&e->depth, FFX_API_RESOURCE_STATE_PIXEL_COMPUTE_READ, FFX_API_RESOURCE_USAGE_READ_ONLY);
    d.motionVectors =
        resource(&e->motion, FFX_API_RESOURCE_STATE_PIXEL_COMPUTE_READ, FFX_API_RESOURCE_USAGE_READ_ONLY);
    if (e->exposure.image) {
        d.exposure =
            resource(&e->exposure, FFX_API_RESOURCE_STATE_PIXEL_COMPUTE_READ, FFX_API_RESOURCE_USAGE_READ_ONLY);
    }
    if (e->reactive.image) {
        d.reactive =
            resource(&e->reactive, FFX_API_RESOURCE_STATE_PIXEL_COMPUTE_READ, FFX_API_RESOURCE_USAGE_READ_ONLY);
    }
    d.output = resource(&e->output, FFX_API_RESOURCE_STATE_UNORDERED_ACCESS, FFX_API_RESOURCE_USAGE_UAV);
    d.jitterOffset.x = e->jitter[0];
    d.jitterOffset.y = e->jitter[1];
    d.motionVectorScale.x = e->motion_scale[0];
    d.motionVectorScale.y = e->motion_scale[1];
    d.renderSize.width = e->render[0];
    d.renderSize.height = e->render[1];
    d.upscaleSize.width = e->output.width;
    d.upscaleSize.height = e->output.height;
    d.enableSharpening = e->sharpness > 0;
    d.sharpness = e->sharpness > 0 ? (e->sharpness < 1 ? e->sharpness : 1) : 0;
    d.frameTimeDelta = e->frame_ms;
    d.preExposure = e->pre_exposure > 0 ? e->pre_exposure : 1.0f;
    d.reset = e->reset != 0;
    d.cameraNear = e->near_plane > 0 ? e->near_plane : 0.1f;
    d.cameraFar = e->far_plane > 0 ? e->far_plane : 1000.0f;
    d.cameraFovAngleVertical = e->fov_y > 0 ? e->fov_y : 1.0f;
    if (srgb(e->color.format)) d.flags |= FFX_UPSCALE_FLAG_NON_LINEAR_COLOR_SRGB;
    ffxReturnCode_t rc = ffxDispatch(&f->ctx, &d.header);
    if (rc != FFX_API_RETURN_OK) {
        snprintf(f->fail_reason, sizeof f->fail_reason,
                 "FSR 4.1 failed to record (%s), and is off from now on", code_name(rc));
        f->failed = 1;
        g.failed = 1;
        snprintf(g.fail_reason, sizeof g.fail_reason, "%s", f->fail_reason);
        if (why) *why = f->fail_reason;
        return 0;
    }
    return 1;
}

extern "C" void snx_ffx_destroy(SnxFfx* f) {
    if (!f) return;
    ffxDestroyContext(&f->ctx, NULL);
    free(f);
}

#endif /* SNX_FSR4_VK */
