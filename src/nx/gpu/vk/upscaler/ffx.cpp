/* SPDX-License-Identifier: GPL-3.0-or-later
   Copyright 2026 the ScarletNX contributors */
/* ffx.h: AMD FidelityFX Super Resolution 4.1 through AMD's FidelityFX API (the FFX API). The API's headers are
   MIT (Copyright (c) 2026 Advanced Micro Devices, Inc., GPUOpen-LibrariesAndSDKs/FidelityFX-SDK v2.4.0, fetched by
   cmake/dependencies.cmake), and only a Windows build with them (SNX_FFX) runs FSR 4.1. Its runtime is AMD's, the
   user's download, loaded from the folder they name: nothing of it is linked or shipped. The context asks for the 4.x
   provider by id and refuses any other, so a driver with 3.1 alone leaves FSR 4.1 off rather than running 3.1 */
#include "nx/gpu/vk/upscaler/ffx.h"
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if defined(_WIN32) && defined(SNX_FFX)
#include <stddef.h>
#include <windows.h>
#include <wchar.h>
#include <wintrust.h>
#include <softpub.h>
#if defined(_MSC_VER)
#pragma warning(push, 0)
#endif
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Weverything"
#include <ffx_api.h>
#include <vk/ffx_api_vk.h>
#include <ffx_upscale.h>
#pragma clang diagnostic pop
#if defined(_MSC_VER)
#pragma warning(pop)
#endif

namespace {

struct Api {
    PfnFfxCreateContext create;
    PfnFfxDestroyContext destroy;
    PfnFfxQuery query;
    PfnFfxDispatch dispatch;
};

struct State {
    HMODULE module;
    Api api;
    SnxFfxLog log;
    int started, failed;
    char folder[512];
    char why[256];
    char fail_reason[256]; /* written once, before failed is set */
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

int refuse(const char** why, const char* fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(g.why, sizeof g.why, fmt, ap);
    va_end(ap);
    *why = g.why;
    return 0;
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
        case FFX_API_RETURN_PROVIDER_NO_SUPPORT_NEW_DESCTYPE: return "a description type its provider is too old for";
        default: return "an error";
    }
}

/* the runtime's messages, which come as wide strings, into the log of the caller's */
void from_ffx(uint32_t type, const wchar_t* text) {
    if (!text) return;
    char line[512];
    int n = WideCharToMultiByte(CP_UTF8, 0, text, -1, line, (int)sizeof line, NULL, NULL);
    if (n <= 1) snprintf(line, sizeof line, "(a message that is not UTF-8)");
    size_t len = strlen(line);
    while (len && (line[len - 1] == '\n' || line[len - 1] == '\r')) line[--len] = 0;
    say(type == FFX_API_MESSAGE_TYPE_ERROR ? 2 : type == FFX_API_MESSAGE_TYPE_WARNING ? 1 : 0, "FidelityFX: %s", line);
}

/* signed by AMD (Authenticode, the chain checked without going online), as DLSS's dlss.cpp checks NVIDIA's */
int signed_by_amd(const wchar_t* path) {
    WINTRUST_FILE_INFO file = {};
    file.cbStruct = sizeof file;
    file.pcwszFilePath = path;
    WINTRUST_DATA data = {};
    data.cbStruct = sizeof data;
    data.dwUIChoice = WTD_UI_NONE;
    data.fdwRevocationChecks = WTD_REVOKE_NONE;
    data.dwUnionChoice = WTD_CHOICE_FILE;
    data.pFile = &file;
    data.dwStateAction = WTD_STATEACTION_VERIFY;
    data.dwProvFlags = WTD_CACHE_ONLY_URL_RETRIEVAL;
    GUID action = WINTRUST_ACTION_GENERIC_VERIFY_V2;
    int valid = WinVerifyTrust(NULL, &action, &data) == ERROR_SUCCESS;
    if (valid) {
        CRYPT_PROVIDER_DATA* provider = WTHelperProvDataFromStateData(data.hWVTStateData);
        CRYPT_PROVIDER_SGNR* signer = provider ? WTHelperGetProvSignerFromChain(provider, 0, FALSE, 0) : NULL;
        CRYPT_PROVIDER_CERT* cert = signer ? WTHelperGetProvCertFromChain(signer, 0) : NULL;
        wchar_t name[128] = {0};
        valid = cert && CertGetNameStringW(cert->pCert, CERT_NAME_SIMPLE_DISPLAY_TYPE, 0, NULL, name, 128) > 1 &&
                wcsstr(name, L"Advanced Micro Devices") != NULL;
    }
    data.dwStateAction = WTD_STATEACTION_CLOSE;
    WinVerifyTrust(NULL, &action, &data);
    return valid;
}

template <typename T> int resolve(const char* name, T& fn) {
    fn = reinterpret_cast<T>(reinterpret_cast<void*>(GetProcAddress(g.module, name)));
    if (!fn) snprintf(g.why, sizeof g.why, "amd_fidelityfx_loader_vk.dll has no %s", name);
    return fn != NULL;
}

/* one image as the API takes it: our VkImage, its format, its size, and whether the effect reads it or writes it */
FfxApiResource image(const SnxUpscaleImage& i, uint32_t state, uint32_t usage) {
    FfxApiResource r = {};
    r.resource = (void*)i.image;
    r.description.type = FFX_API_RESOURCE_TYPE_TEXTURE2D;
    r.description.format = ffxApiGetSurfaceFormatVK(i.format);
    r.description.width = i.width;
    r.description.height = i.height;
    r.description.depth = 1;
    r.description.mipCount = 1;
    r.description.usage = usage;
    r.state = state;
    return r;
}

} // namespace

struct SnxFfx {
    SnxUpscalerInfo info;
    ffxContext ctx;
    int failed;
    char fail_reason[192]; /* written once, before failed is set */
};

extern "C" {

int snx_ffx_start(const char* folder, SnxFfxLog log, const char** why) {
    const char* none;
    if (!why) why = &none;
    *why = NULL;
    if (g.started) return 1;
    g.log = log;
    if (!folder || !*folder)
        return refuse(why, "no folder holds AMD's FidelityFX API runtime (the ffx_runtime setting)");
    if (strlen(folder) >= sizeof g.folder) return refuse(why, "the folder's name is too long");
    snprintf(g.folder, sizeof g.folder, "%s", folder);
    wchar_t dir[MAX_PATH], path[MAX_PATH + 64];
    if (!MultiByteToWideChar(CP_UTF8, 0, folder, -1, dir, MAX_PATH))
        return refuse(why, "the folder's name is too long");
    size_t len = wcslen(dir);
    for (size_t i = 0; i < len; i++)
        if (dir[i] == L'/') dir[i] = L'\\'; /* LoadLibraryEx with an altered search path takes backslashes only */
    while (len && dir[len - 1] == L'\\') dir[--len] = 0;
    _snwprintf(path, MAX_PATH + 64, L"%ls\\amd_fidelityfx_loader_vk.dll", dir);
    if (GetFileAttributesW(path) == INVALID_FILE_ATTRIBUTES)
        return refuse(why, "%s has no amd_fidelityfx_loader_vk.dll (AMD's FidelityFX API)", folder);
    if (!signed_by_amd(path))
        return refuse(why, "%s\\amd_fidelityfx_loader_vk.dll is not signed by AMD: not loaded", folder);
    g.module = LoadLibraryExW(path, NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!g.module)
        return refuse(why, "%s\\amd_fidelityfx_loader_vk.dll can't be loaded (error %lu)", folder,
                      (unsigned long)GetLastError());
    Api& a = g.api;
    if (!resolve("ffxCreateContext", a.create) || !resolve("ffxDestroyContext", a.destroy) ||
        !resolve("ffxQuery", a.query) || !resolve("ffxDispatch", a.dispatch)) {
        FreeLibrary(g.module);
        g.module = NULL;
        *why = g.why;
        return 0;
    }
    g.started = 1;
    return 1;
}

int snx_ffx_available(const char** why) {
    const char* none;
    if (!why) why = &none;
    *why = NULL;
    if (g.failed) {
        *why = g.fail_reason;
        return 0;
    }
    if (g.started) return 1;
    *why = g.why[0] ? g.why
                     : "FSR 4.1 is off: name the folder of AMD's FidelityFX API runtime in the ffx_runtime "
                       "setting";
    return 0;
}

/* the major number in a provider's name (FidelityFX Super Resolution 4.1.2: 4), 0 when it has no number */
static int provider_major(const char* name) {
    if (!name) return 0;
    for (; *name; name++) {
        if (*name < '0' || *name > '9') continue;
        int v = 0;
        while (*name >= '0' && *name <= '9') v = v * 10 + (*name++ - '0');
        return v;
    }
    return 0;
}

SnxFfx* snx_ffx_create(const SnxUpscalerInfo* info, const char** why) {
    const char* none;
    if (!why) why = &none;
    *why = NULL;
    if (!snx_ffx_available(why)) return NULL;
    SnxFfx* f = (SnxFfx*)calloc(1, sizeof *f);
    if (!f) {
        *why = "no memory";
        return NULL;
    }
    f->info = *info;
    uint32_t flags = 0;
    if (info->flags & SNX_UP_FLAG_HDR)
        flags |= FFX_UPSCALE_ENABLE_HIGH_DYNAMIC_RANGE;
    else
        /* the game draws a display-referred picture: FSR works in linear light, and this says the values are not that */
        flags |= FFX_UPSCALE_ENABLE_NON_LINEAR_COLORSPACE;
    if (info->flags & SNX_UP_FLAG_DISPLAY_MOTION) flags |= FFX_UPSCALE_ENABLE_DISPLAY_RESOLUTION_MOTION_VECTORS;
    if (info->flags & SNX_UP_FLAG_JITTERED_MOTION)
        flags |= FFX_UPSCALE_ENABLE_MOTION_VECTORS_JITTER_CANCELLATION;
    if (info->flags & SNX_UP_FLAG_INVERTED_DEPTH) flags |= FFX_UPSCALE_ENABLE_DEPTH_INVERTED;
    if (info->flags & SNX_UP_FLAG_AUTO_EXPOSURE) flags |= FFX_UPSCALE_ENABLE_AUTO_EXPOSURE;

    VkDeviceContext vk = {info->device, info->physical, vkGetDeviceProcAddr};

    /* 4.1 is the only FSR this runs: ask the runtime which providers this GPU has, and take the 4.x one's id to say
       so to ffxCreateContext. A 3.1 provider would be a different upscaler under the setting that names FSR 4.1 */
    ffxOverrideVersion over = {};
    over.header.type = FFX_API_DESC_TYPE_OVERRIDE_VERSION;
    int forced = 0;
    uint64_t ids[32] = {0};
    const char* names[32] = {0};
    char seen[200] = "";
    uint64_t count = 0;
    ffxQueryDescGetVersions list = {};
    list.header.type = FFX_API_QUERY_DESC_TYPE_GET_VERSIONS;
    list.createDescType = FFX_API_CREATE_CONTEXT_DESC_TYPE_UPSCALE;
    list.device = &vk;
    list.outputCount = &count;
    if (g.api.query(NULL, &list.header) == FFX_API_RETURN_OK && count > 0) {
        uint64_t got = count < 32 ? count : 32;
        list.outputCount = &got;
        list.versionIds = ids;
        list.versionNames = names;
        if (g.api.query(NULL, &list.header) == FFX_API_RETURN_OK) {
            for (uint64_t i = 0; i < got; i++) {
                const char* name = names[i] ? names[i] : "";
                size_t at = strlen(seen);
                if (at + 1 < sizeof seen) snprintf(seen + at, sizeof seen - at, "%s%s", at ? ", " : "", name);
                if (!forced && provider_major(name) == 4) {
                    over.versionId = ids[i];
                    forced = 1;
                }
            }
            if (!forced) {
                g.failed = 1;
                snprintf(g.why, sizeof g.why,
                         "AMD's runtime for this GPU has no FSR 4.1, only %.150s. FSR 4.1 needs a driver with it, and "
                         "this game leaves it off",
                         seen);
                snprintf(g.fail_reason, sizeof g.fail_reason, "%s", g.why);
                say(2, "FSR 4.1: %s", g.why);
                free(f);
                *why = g.why;
                return NULL;
            }
        }
    }

    ffxCreateBackendVKDesc backend = {};
    backend.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_BACKEND_VK;
    backend.deviceContext = vk;
    ffxCreateContextDescUpscale up = {};
    up.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_UPSCALE;
    up.maxRenderSize = {info->render_max[0], info->render_max[1]};
    up.maxUpscaleSize = {info->output[0], info->output[1]};
    up.fpMessage = from_ffx;
    ffxCreateContextDescUpscaleVersion ver = {};
    ver.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_UPSCALE_VERSION;
    ver.version = FFX_UPSCALER_VERSION;
    backend.header.pNext = forced ? &over.header : &up.header;
    if (forced) over.header.pNext = &up.header;
    /* the effect's API version first (its provider may be older), then no dynamic resolution: a driver that refuses
       either still runs the upscaler, at a fixed size in the last case */
    auto attempt = [&](int dynamic_resolution, int with_version) {
        up.flags = flags | (dynamic_resolution ? FFX_UPSCALE_ENABLE_DYNAMIC_RESOLUTION : 0);
        up.header.pNext = with_version ? &ver.header : NULL;
        return g.api.create(&f->ctx, &backend.header, NULL);
    };
    ffxReturnCode_t rc = attempt(1, 1);
    if (rc != FFX_API_RETURN_OK) rc = attempt(1, 0);
    if (rc != FFX_API_RETURN_OK) rc = attempt(0, 0);
    if (rc != FFX_API_RETURN_OK) {
        snprintf(g.why, sizeof g.why, "AMD's FidelityFX API made no FSR 4.1 context (%s)", code_name(rc));
        free(f);
        *why = g.why;
        return NULL;
    }
    /* the context says which provider it runs: anything but 4.1 is refused here rather than quietly used */
    ffxQueryGetProviderVersion pv = {};
    pv.header.type = FFX_API_QUERY_DESC_TYPE_GET_PROVIDER_VERSION;
    ffxReturnCode_t qrc = g.api.query(&f->ctx, &pv.header);
    if (qrc != FFX_API_RETURN_OK || !pv.versionName || provider_major(pv.versionName) != 4) {
        if (pv.versionName)
            snprintf(g.why, sizeof g.why, "AMD's FidelityFX API gave %.120s, not FSR 4.1", pv.versionName);
        else
            snprintf(g.why, sizeof g.why, "AMD's FidelityFX API did not name the FSR it gave: not used");
        g.api.destroy(&f->ctx, NULL);
        g.failed = 1;
        snprintf(g.fail_reason, sizeof g.fail_reason, "%s", g.why);
        say(2, "FSR 4.1: %s", g.why);
        free(f);
        *why = g.why;
        return NULL;
    }
    say(0, "FSR 4.1: %s, from %s (%ux%u to %ux%u)", pv.versionName, g.folder, info->render_max[0],
        info->render_max[1], info->output[0], info->output[1]);
    return f;
}

int snx_ffx_evaluate(SnxFfx* f, const SnxUpscaleEval* e, const char** why) {
    const char* none;
    if (!why) why = &none;
    *why = NULL;
    if (!f) {
        *why = "FSR 4.1 is not available";
        return 0;
    }
    if (f->failed) {
        *why = f->fail_reason;
        return 0;
    }
    if (!e->depth.image || !e->motion.image) {
        *why = "FSR 4.1 needs depth and motion vectors";
        return 0;
    }
    ffxDispatchDescUpscale d = {};
    d.header.type = FFX_API_DISPATCH_DESC_TYPE_UPSCALE;
    d.commandList = (void*)e->cb;
    d.color = image(e->color, FFX_API_RESOURCE_STATE_PIXEL_COMPUTE_READ, FFX_API_RESOURCE_USAGE_READ_ONLY);
    d.depth = image(e->depth, FFX_API_RESOURCE_STATE_PIXEL_COMPUTE_READ, FFX_API_RESOURCE_USAGE_READ_ONLY);
    d.motionVectors = image(e->motion, FFX_API_RESOURCE_STATE_PIXEL_COMPUTE_READ, FFX_API_RESOURCE_USAGE_READ_ONLY);
    if (e->exposure.image && !(f->info.flags & SNX_UP_FLAG_AUTO_EXPOSURE))
        d.exposure = image(e->exposure, FFX_API_RESOURCE_STATE_PIXEL_COMPUTE_READ, FFX_API_RESOURCE_USAGE_READ_ONLY);
    if (e->reactive.image)
        d.reactive = image(e->reactive, FFX_API_RESOURCE_STATE_PIXEL_COMPUTE_READ, FFX_API_RESOURCE_USAGE_READ_ONLY);
    d.output = image(e->output, FFX_API_RESOURCE_STATE_UNORDERED_ACCESS, FFX_API_RESOURCE_USAGE_UAV);
    /* the API wants the camera's jitter, as the game gives it: the picture moved by -jitter, and a jitter that is
       dropped or given the other way shows as a soft or a shimmering picture */
    d.jitterOffset = {e->jitter[0], e->jitter[1]};
    d.motionVectorScale = {e->motion_scale[0], e->motion_scale[1]};
    d.renderSize = {e->render[0], e->render[1]};
    d.upscaleSize = {e->output.width, e->output.height};
    d.enableSharpening = e->sharpness > 0;
    d.sharpness = e->sharpness < 0 ? 0 : e->sharpness > 1 ? 1 : e->sharpness;
    d.frameTimeDelta = e->frame_ms;
    d.preExposure = e->pre_exposure > 0 ? e->pre_exposure : 1.0f;
    d.reset = e->reset != 0;
    d.cameraNear = e->near_plane > 0 ? e->near_plane : 0.1f;
    d.cameraFar = e->far_plane > 0 ? e->far_plane : 1000.0f;
    d.cameraFovAngleVertical = e->fov_y > 0 ? e->fov_y : 1.0f;
    ffxReturnCode_t rc = g.api.dispatch(&f->ctx, &d.header);
    if (rc != FFX_API_RETURN_OK) {
        f->failed = 1;
        snprintf(f->fail_reason, sizeof f->fail_reason, "FSR 4.1 failed to record (%s), and is off from now on",
                 code_name(rc));
        /* the next setup falls back: available() says no for every context after this one */
        g.failed = 1;
        snprintf(g.fail_reason, sizeof g.fail_reason, "%s", f->fail_reason);
        say(2, "FSR 4.1: %s", f->fail_reason);
        *why = f->fail_reason;
        return 0;
    }
    return 1;
}

void snx_ffx_destroy(SnxFfx* f) {
    if (!f) return;
    if (f->ctx) g.api.destroy(&f->ctx, NULL);
    free(f);
}

} // extern "C"

#else /* no FidelityFX API headers: a build for another system, or without them */

extern "C" {
static const char* const g_none =
    "FSR 4.1 runs through AMD's FidelityFX API runtime, which is Windows only";
int snx_ffx_start(const char* folder, SnxFfxLog log, const char** why) {
    (void)folder, (void)log;
    if (why) *why = g_none;
    return 0;
}
int snx_ffx_available(const char** why) {
    if (why) *why = g_none;
    return 0;
}
SnxFfx* snx_ffx_create(const SnxUpscalerInfo* info, const char** why) {
    (void)info;
    if (why) *why = g_none;
    return NULL;
}
int snx_ffx_evaluate(SnxFfx* f, const SnxUpscaleEval* e, const char** why) {
    (void)f, (void)e;
    if (why) *why = g_none;
    return 0;
}
void snx_ffx_destroy(SnxFfx* f) { (void)f; }
}
#endif
