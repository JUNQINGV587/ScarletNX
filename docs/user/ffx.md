<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright 2026 the ScarletNX contributors -->
# AMD FSR 4.1

On Windows with a recent AMD graphics driver, a game's upscale (a Switch 2 game's DLSS evaluations, and any other
upscale the [upscalers](../layers/gpu/upscalers.md) run) can run AMD FidelityFX Super Resolution 4.1, AMD's
machine-learning temporal upscaler. Elsewhere, or without the files below, or with a driver whose FSR is 3.1 alone, it
runs AMD FSR 2.2 as before.

ScarletNX does not include FSR 4.1. It is AMD's software under AMD's licence, which you download from AMD and keep in
a folder of your own. Only AMD's FidelityFX API headers, which are MIT, are built into the program.

## Getting the files

1. Get the FidelityFX SDK 2.4 from AMD's FidelityFX SDK page, <https://gpuopen.com/fidelityfx-sdk/>, or from its
   releases, <https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK/releases/tag/v2.4.0>. Read its licence first:
   using the files means taking it.
2. Copy `amd_fidelityfx_loader_vk.dll` and `amd_fidelityfx_upscaler_vk.dll` from the SDK's prebuilt Windows binaries
   into one folder, for example `D:\fsr`. The loader finds the upscaler beside it.
3. Set the game's `ffx_runtime` to that folder (in the game's `.ini`, or `--ffx_runtime=D:\fsr` on the command line).

Nothing is installed: the program loads the loader from that folder when the game starts, after checking that
`amd_fidelityfx_loader_vk.dll` is signed by AMD. The loader is AMD's and it looks for the upscaler beside it.

FSR 4.1 runs on a Radeon RX 6000, 7000 or 9000, or a Ryzen AI APU whose driver has it, and it needs a recent driver
(SDK 2.4's notes name Adrenalin 26.10.1). The program asks the runtime for its FSR 4.1 provider by name and refuses
any other, so a driver with FSR 3.1 alone does not run it: FSR 2.2 stands in for that setup, and the next setup
asks the runtime again. The log says which upscaler started.

## Settings

| Key | Default | What it does |
| --- | --- | --- |
| `ffx_runtime` | empty | The folder above. Empty = no FSR 4.1 |
| `render_upscaler` | `0` | `0` (Best) takes FSR 4.1 when it can run, `5` asks for it. `1` asks for FSR 2 even with the files there, `3` (None) a filtered copy |
| `internal_resolution` | `0` | The size the game renders at before the upscale: `0` the game's choice, `1` the output's size, `2` to `5` Quality to Ultra Performance |
| `fsr_sharpness` | `0` | The sharpening FSR applies after upscaling, `0` to `1` |

## What the log says

- `vulkan: FSR 4.1: FidelityFX Super Resolution 4.1.2, from D:\fsr (1280x720 to 2560x1440)`: the files were found,
  their signature is AMD's, and a context was made for the game's sizes.
- `vulkan: no FSR 4.1: ...` says why it can't run (no folder, a file missing or not signed, a parameter the API
  refuses), and FSR 2 runs instead. A runtime whose GPU has only FSR 3.1 says so there too: FSR 4.1 stays off for
  that setup, and the next setup asks again.
- A dispatch that fails turns FSR 4.1 off until the next start, and the next upscaler takes over from the next
  evaluation. A context that can't be made does not: the next upscaler takes that frame, and the next setup tries
  FSR 4.1 again.
