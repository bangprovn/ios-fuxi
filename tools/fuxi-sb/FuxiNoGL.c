/*
 * MIT License
 *
 * Copyright (c) 2026 ios-fuxi contributors
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */
/*
 * Part of libfuxi-app.dylib: CoreImage in an application, on a phone with no OpenGL ES.
 *
 * Settings -> Notifications crashed every time (CRASH Preferences sig 11 pc 0x0, lr CoreImage+0x19c58c; boots h-fit1,
 * h-hot80, st-0). Read in 20H392's shared cache:
 *   - MobileIcons scales each application icon with +[CIContext _MI_sharedIconCompositorContext], made once by
 *     [[CIContext alloc] initWithOptions:] with HighQualityDownsample YES, CacheIntermediates NO,
 *     IntermediateMemoryTarget 0 and name "MobileIcons-Scaler" (no colour space, no working format).
 *   - -[CIContext initWithOptions:] (CoreImage+0x6f1c) takes Metal when MTLCreateSystemDefaultDevice() gives a device
 *     that supportsFamily: MTLGPUFamilyApple2. In an application it gives none here: FuxiMetal hands a device only to
 *     the processes in metal-clients.conf (tools/fuxi-metal/ClientGate.hpp). Then, unless the caller set
 *     kCIContextUseMetalRenderer, it calls +[CIContext internalGLContextWithOptions:], which is
 *     [CIContext internalContextWithEAGLContext:nil options:]. CI::GLContext's constructor asks EAGLContext for an
 *     OpenGL ES 3, then 2 context, does not check what it got, and calls the first entry of the GL dispatch table of
 *     -[EAGLContext getMacroContextPrivate]. This phone has no OpenGL ES driver: the entry is NULL, pc 0.
 *
 * +[CIContext internalGLContextWithOptions:] is replaced. If OpenGL ES can make a context (an EAGLContext for API 3
 * or 2 whose -getMacroContextPrivate is not NULL, the test CoreImage itself relies on), the original runs. If not, it
 * returns NULL. Its one caller, -initWithOptions:, already handles that (CoreImage+0x7118: release, return nil): the
 * CIContext is nil, as CoreImage gives when it has no renderer at all. MobileIcons then keeps the unscaled icon and
 * draws it into the same rectangle with CGContextDrawImage (MobileIcons+0x1d44), so icons are scaled by CoreGraphics
 * instead of CILanczosScaleTransform, in MobileIcons' own bitmap context and colour space. The Metal path and an
 * application's own EAGLContext (-initWithEAGLContext:options:) are not touched. Said once per process.
 */
#include <mach-o/dyld.h>
#include <objc/message.h>
#include <objc/runtime.h>
#include <os/log.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define NOGL_CLASS      "CIContext"
#define NOGL_SELECTOR   "internalGLContextWithOptions:"
#define NOGL_EAGL       "EAGLContext"

enum { GL_UNKNOWN, GL_WORKS, GL_NONE };

static atomic_int gl_state;             /* GL_UNKNOWN until the first fallback asks */
static atomic_bool nil_said;
static int hooked;                      /* 0 not yet, 1 replaced, -1 given up */
static void *(*original_gl_context)(id self, SEL selector, id options);

/* as FuxiAppRate.c: stderr reaches the kernel's log for the names in boot-arg sweetnull, os_log the unified log */
static void
nogl_say(const char *format, ...)
{
	char line[200];
	va_list ap;
	int n;

	va_start(ap, format);
	n = vsnprintf(line, sizeof(line), format, ap);
	va_end(ap);
	if (n > (int)sizeof(line) - 1) {
		n = sizeof(line) - 1;
	}
	if (n <= 0) {
		return;
	}
	(void)write(2, line, (size_t)n);
	os_log(OS_LOG_DEFAULT, "%{public}s", line);
}

static const char *
nogl_name(void)
{
	const char *name = getprogname();

	return name != NULL ? name : "?";
}

/* What CI::GLContext needs: an EAGLContext (API 3, else 2, as CoreImage asks) with a GL context behind it. */
static bool
gl_works(void)
{
	Class eagl = objc_getClass(NOGL_EAGL);
	SEL alloc = sel_registerName("alloc"), init = sel_registerName("initWithAPI:");
	SEL macro = sel_registerName("getMacroContextPrivate"), release = sel_registerName("release");
	id context = NULL;
	bool works;

	if (eagl == NULL) {
		return false;
	}
	for (unsigned long api = 3; api >= 2 && context == NULL; api--) {
		id fresh = ((id (*)(id, SEL))objc_msgSend)((id)eagl, alloc);

		context = fresh != NULL ? ((id (*)(id, SEL, unsigned long))objc_msgSend)(fresh, init, api) : NULL;
	}
	if (context == NULL) {
		return false;
	}
	/* an EAGLContext without CoreImage's own test: leave CoreImage as it is */
	works = !class_respondsToSelector(object_getClass(context), macro) ||
	    ((void *(*)(id, SEL))objc_msgSend)(context, macro) != NULL;
	((void (*)(id, SEL))objc_msgSend)(context, release);
	return works;
}

static void *
gl_context(id self, SEL selector, id options)
{
	int state = atomic_load(&gl_state);

	if (state == GL_UNKNOWN) {
		int expected = GL_UNKNOWN, found = gl_works() ? GL_WORKS : GL_NONE;

		if (atomic_compare_exchange_strong(&gl_state, &expected, found) && found == GL_WORKS) {
			nogl_say("fuxi: %s: OpenGL ES works here; CoreImage's GL contexts left as they are\n", nogl_name());
		}
		state = atomic_load(&gl_state);
	}
	if (state == GL_WORKS) {
		return original_gl_context(self, selector, options);
	}
	if (!atomic_exchange(&nil_said, true)) {
		nogl_say("fuxi: %s: CIContext asked for OpenGL ES, none on this phone: nil context, not a crash\n", nogl_name());
	}
	return NULL;
}

/* true once there is nothing more to do: replaced, or given up */
static bool
try_hook(void)
{
	Class ci;
	Method method;

	if (hooked != 0) {
		return true;
	}
	ci = objc_getClass(NOGL_CLASS);
	if (ci == NULL) {
		return false;                   /* CoreImage not loaded yet */
	}
	method = class_getClassMethod(ci, sel_registerName(NOGL_SELECTOR));
	if (method == NULL) {
		hooked = -1;
		nogl_say("fuxi: %s: +[CIContext %s] not found; CoreImage's OpenGL ES fallback NOT changed\n", nogl_name(),
		    NOGL_SELECTOR);
		return true;
	}
	original_gl_context = (void *(*)(id, SEL, id))method_setImplementation(method, (IMP)gl_context);
	hooked = 1;
	return true;
}

static void
nogl_image_added(const struct mach_header *header, intptr_t slide)
{
	(void)header;
	(void)slide;
	if (hooked == 0) {
		(void)try_hook();
	}
}

__attribute__((constructor)) static void
fuxi_nogl_init(void)
{
	/* called at once for every image already there, then for each one added */
	_dyld_register_func_for_add_image(nogl_image_added);
}
