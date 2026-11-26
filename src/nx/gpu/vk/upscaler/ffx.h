/* SPDX-License-Identifier: GPL-3.0-or-later
   Copyright 2026 the ScarletNX contributors */
/* AMD FidelityFX Super Resolution 4.1, the upscalers' newer-FSR backend (upscaler.h,
   docs/layers/gpu/upscalers.md), through AMD's FidelityFX API (the FFX API). Only the API's headers (MIT) are built in,
   and only in a Windows build: the runtime itself (amd_fidelityfx_loader_vk.dll and the effect DLLs that it loads) is
   AMD's, is never in the program, and the program never downloads it. The user downloads it from AMD into a folder of
   their own and names that folder in a setting, and it is loaded from there (LoadLibraryEx) when it is signed by AMD.
   Without it, FSR 4.1 is not available and the choice of upscaler goes on to the next.

   The FFX API is one interface for FSR 3.1 and 4: the loader picks the effect that the GPU and the driver take.
   This asks it for the providers the GPU has, takes the 4.x one by id (ffxOverrideVersion) and refuses the context
   when the provider it gets is not 4.1, so a driver with 3.1 alone leaves FSR 4.1 off instead of running 3.1 here.
   Unlike Streamline the API needs neither the instance nor a hook of the swapchain's calls: an evaluation gives it the
   images as resources, and it records into the caller's command buffer. Like the rest of the interface it knows
   neither the renderer nor the settings: the caller passes the folder once, and a function for the API's messages */
#ifndef SNX_NX_GPU_VK_UPSCALER_FFX_H
#define SNX_NX_GPU_VK_UPSCALER_FFX_H
#include <stdint.h>
#include "nx/gpu/vk/upscaler/upscaler.h"
#ifdef __cplusplus
extern "C" {
#endif

typedef void (*SnxFfxLog)(int level, const char* text); /* level: 0 information, 1 warning, 2 error */
/* Before the first creation: AMD's runtime from folder, its amd_fidelityfx_loader_vk.dll signed by AMD. 1 when it runs,
   else 0 and why (an empty folder: FSR 4.1 is off). Windows only */
int snx_ffx_start(const char* folder, SnxFfxLog log, const char** why);

/* snx_upscaler_available's: started and no evaluation failed */
int snx_ffx_available(const char** why);
typedef struct SnxFfx SnxFfx;
SnxFfx* snx_ffx_create(const SnxUpscalerInfo* info, const char** why);
/* an evaluation into e->cb (the frame's command buffer, on the thread that records it). A failure here leaves FSR 4.1
   unavailable from then on, so the next evaluation chooses another upscaler */
int snx_ffx_evaluate(SnxFfx* f, const SnxUpscaleEval* e, const char** why);
void snx_ffx_destroy(SnxFfx* f);
#ifdef __cplusplus
}
#endif
#endif
