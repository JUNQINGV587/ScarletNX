/* SPDX-License-Identifier: GPL-3.0-or-later */

/* bc7_cache.c asks g_vk which layout an image is in.  A test runs without a device, so the state stays empty and
   VK_LAYOUT hands back the layout it was given: what the cache did before the layouts were unified. */

#include "nx/gpu/vk/vk.h"

VkState g_vk;
