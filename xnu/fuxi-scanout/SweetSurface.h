/*
 * Built-in IOSurface for SWEET (see SweetSurface.cpp).
 *
 * Shared with SweetFramebuffer.cpp: the framebuffer looks surfaces up by
 * id when a swap names them and publishes the panel memory itself as the
 * default framebuffer surface.
 */
#ifndef _SWEET_SURFACE_H
#define _SWEET_SURFACE_H

#include <pexpert/arm64/board_config.h>

#if defined(SWEET)
#include <IOKit/IOMemoryDescriptor.h>
#include <IOKit/IOBufferMemoryDescriptor.h>
#include <libkern/c++/OSObject.h>
#include <mach/mach_types.h>

#define SWEET_SURFACE_MAX_PLANES        20

struct sweet_surface_plane {
	uint32_t width, height, bytesPerRow, offset, size;
	uint16_t bytesPerElement;
	uint8_t  elementWidth, elementHeight;
};

class SweetSurface : public OSObject {
	OSDeclareDefaultStructors(SweetSurface);

public:
	uint32_t id;
 uint32_t useCounts[2], displayUses;
 bool usePoisoned;
#if defined(FUXI)
 unsigned gpuWriters;
 uint64_t gpuReadyTime, gpuWriterSerial, gpuWriterEpoch;
 bool gpuQuarantined;
 /* fuxigpuncimport bit 20: writer serial/epoch at the last pre-clean; zero-copy survey class. */
 uint64_t gpuCleanSerial, gpuCleanEpoch;
 bool gpuCleanValid;
 uint8_t scanoutClass;
 /* fuxigpuncimport bit 24: ABI sequence when the writer epoch opened, and the writer fence when it closed (lock sequence + 1 when a
  * submit followed the lock, else 0). */
 uint32_t gpuLockSeq, gpuWriterFence;
#endif
	uint32_t width, height, bytesPerRow, offset, pixelFormat, allocSize;
	uint16_t bytesPerElement;
	uint8_t  elementWidth, elementHeight;
	uint32_t cacheMode, planeCount, parentID;
	struct sweet_surface_plane planes[SWEET_SURFACE_MAX_PLANES];

	IOMemoryDescriptor       *memory;       /* pixels (buffer or physical) */
	IOOptionBits              mapCache;     /* cache mode for user mappings */
	IOBufferMemoryDescriptor *state;        /* shared state page, read-only for users */
	IOMemoryMap              *kernelMap;    /* lazily created for kernel copies */

	bool init() APPLE_KEXT_OVERRIDE { return OSObject::init(); }
	void free() APPLE_KEXT_OVERRIDE;

	/* Kernel address of the pixels (mapped on first use), or 0. */
	uintptr_t kernelAddress();
};

#if defined(FUXI)
extern "C" {
#include "../../../bsd/dev/fuxi_adreno.h"
}
int sweet_surface_gpu_copy(SweetSurface *, uintptr_t, unsigned, unsigned,
    unsigned, unsigned, unsigned, unsigned);
int sweet_surface_gpu_poll(uint64_t *);
void sweet_surface_gpu_stats(struct fg_times *);
void sweet_surface_display_poison(SweetSurface *);
uint64_t sweet_surface_writer_serial(SweetSurface *, uint64_t *epoch = NULL);
unsigned sweet_surface_scanout_class(SweetSurface *, uint64_t offset, uint64_t bytes);
/* fuxigpuncimport bit 24: 2 queued for direct scanout (no copy), 3 retry, 0 copy. */
int sweet_surface_direct(SweetSurface *, uintptr_t src, unsigned sx, unsigned sy, unsigned width, unsigned height);
#endif

/* Retained surface for an id, or NULL. */
SweetSurface *sweet_surface_copy(uint32_t id);
bool sweet_surface_display_hold(SweetSurface *);
void sweet_surface_display_drop(SweetSurface *);

/*
 * Publish physical memory (the panel framebuffer) as a surface. Returns a
 * retained surface that stays in the id table until released by the caller
 * and every user.
 */
SweetSurface *sweet_surface_create_physical(uint64_t phys, uint32_t width,
    uint32_t height, uint32_t bytesPerRow, uint32_t pixelFormat);

/* Send right for the surface in task (IOSurfaceLookupFromMachPort). */
mach_port_name_t sweet_surface_port_in_task(SweetSurface *surface, task_t task);

#endif /* SWEET */
#endif /* _SWEET_SURFACE_H */
