<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright 2026 the ScarletNX contributors -->
# AMD FSR 4.1

A game's upscale (a Switch 2 game's DLSS evaluations, and any other upscale the
[upscalers](../layers/gpu/upscalers.md) run) can run AMD FidelityFX Super Resolution 4.1, AMD's machine-learning
temporal upscaler. Without it, or with a driver whose FSR is 3.1 alone, the upscaler goes on to AMD FSR 2.2 as before.

How FSR 4.1 is reached differs by system.

- **Linux**: ScarletNX builds the FSR4 Vulkan provider, by dvj5411 (<https://github.com/dvj5411/fsr4vk>, GPL-3.0), as
  part of the program (`third_party/fsr4vk`). It runs AMD's INT8 FSR 4.1.1 model, whose shaders and weights are AMD's
  (MIT), from the files in that folder. Nothing is downloaded: a build has the provider and its model files, and
  `ffx_runtime` only names the folder when it moves them somewhere else.
- **Windows**: FSR 4.1 is AMD's software under AMD's licence. ScarletNX does not include it: you download AMD's
  FidelityFX SDK 2.4 and keep its runtime in a folder of your own. Only AMD's FidelityFX API headers, which are MIT,
  are built into the program.

## Linux

The build makes `libsnx_fsr4vk.so` from `third_party/fsr4vk` and links it. FSR 4.1 runs on a Vulkan 1.2 device with
the descriptor buffer, mutable descriptor and compute shader derivatives extensions, and a recent Mesa: RDNA 2 and
later (the handheld's Z2 Extreme included). The provider's limits are its own: render and output sizes from 64x64 to
3840x2160, never an output smaller than the render, and the last row and column of an odd-sized output left as they
were.

The model files are read from `third_party/fsr4vk/assets` (the source tree's, `SNX_FSR4_ASSETS`), or from the folder
`ffx_runtime` names. The provider refuses a folder without `general/` and `fsr411/`, and the log says so.

## Windows

1. Get the FidelityFX SDK 2.4 from AMD's FidelityFX SDK page, <https://gpuopen.com/fidelityfx-sdk/>, or from its
   releases, <https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK/releases/tag/v2.4.0>. Read its licence first:
   using the files means taking it.
2. Copy `amd_fidelityfx_loader_vk.dll` and `amd_fidelityfx_upscaler_vk.dll` from the SDK's prebuilt Windows binaries
   into one folder, for example `D:\fsr`. The loader finds the upscaler beside it.
3. Set the game's `ffx_runtime` to that folder (in the game's `.ini`, or `--ffx_runtime=D:\fsr` on the command line).

The program loads the loader from that folder when the game starts, after checking that `amd_fidelityfx_loader_vk.dll`
is signed by AMD. FSR 4.1 needs a recent driver (SDK 2.4's notes name Adrenalin 26.10.1).

## Both

The program asks the runtime for its FSR 4.1 provider by name and refuses any other, so a 3.1 provider is not run:
FSR 2.2 stands in for that setup, and the next setup asks again. The log says which upscaler started.

## Settings

| Key | Default | What it does |
| --- | --- | --- |
| `ffx_runtime` | empty | The folder above. On Linux, empty means the build's own model files |
| `render_upscaler` | `0` | `0` (Best) takes FSR 4.1 when it can run, `5` asks for it. `1` asks for FSR 2 even with the files there, `3` (None) a filtered copy |
| `internal_resolution` | `0` | The size the game renders at before the upscale: `0` the game's choice, `1` the output's size, `2` to `5` Quality to Ultra Performance |
| `fsr_sharpness` | `0` | The sharpening FSR applies after upscaling, `0` to `1` |

## What the log says

- `vulkan: FSR 4.1: FidelityFX Super Resolution 4.1.2, from ... (1280x720 to 2560x1440)`: the files were found and a
  context was made for the game's sizes.
- `vulkan: no FSR 4.1: ...` says why it can't run (no model files, a file missing or refused, a parameter the API
  refuses), and FSR 2 runs instead. A runtime whose GPU has only FSR 3.1 says so there too: FSR 4.1 stays off for
  that setup, and the next setup asks again.
- A dispatch that fails turns FSR 4.1 off until the next start, and the next upscaler takes over from the next
  evaluation. A context that can't be made does not: the next upscaler takes that frame, and the next setup tries
  FSR 4.1 again.
