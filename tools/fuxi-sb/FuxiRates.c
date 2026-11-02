/*
 * MIT License
 *
 * Copyright (c) 2026 OOSEX GANG
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
 * Ported from HakiOS hakios-xnu-kernel userland/src/inject_sb.c (MIT): display_link_rates() and
 * animation_rates(), commits 7caa457 and 3639b98.
 *
 * The frame rates SpringBoard asks for. iOS 16's window server moves each animation at the rate it
 * asks for, and the iPhone X's defaults were never meant for a panel at 120:
 *
 *   bit 0  The display link of UIViewInProcessAnimationManager, which runs the animations done in
 *          SpringBoard's own process (a home screen page settling), asks for 20..30. Its requests
 *          are dropped, so it runs at the panel's rate. HakiOS: a swipe 78 -> 115 a second. The
 *          other display links keep what they ask; at 120 they would wake SpringBoard for nothing.
 *   bit 1  Every animation added to a layer of SpringBoard's asks for 80..120, a copy of it: one
 *          already added is read-only, and setting it raises. HakiOS, through Metal as here: what
 *          asks for nothing went at 24 a second; Settings' scroll 92 -> 120.
 *
 * Boot-arg fxrate=N picks the bits: absent means both, fxrate=0 leaves SpringBoard alone (for A/B
 * runs without a root edit). Short because the command line has no room.
 */
#include <fcntl.h>
#include <mach-o/dyld.h>
#include <objc/message.h>
#include <objc/runtime.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/sysctl.h>
#include <unistd.h>

#define RATES_LINK      1u
#define RATES_ANIMATION 2u
#define RATES_ALL       (RATES_LINK | RATES_ANIMATION)
#define RATES_ARG       "fxrate="
#define RATES_SAID      24              /* the first requests are logged */
#define RATES_FULL_FOR  "UIViewInProcessAnimationManager"

typedef struct {
	float minimum, maximum, preferred;
} frame_rate_range;

static int say_fd = -1;
static unsigned said, wanted;
static char full_rate_mark;             /* the key of the mark a display link made for RATES_FULL_FOR carries */
static bool links_hooked, animations_hooked;

static id (*original_link_with)(id self, SEL selector, id target, SEL action);
static void (*original_set_fps)(id self, SEL selector, long fps);
static void (*original_set_range)(id self, SEL selector, frame_rate_range range);
static void (*original_add_animation)(id self, SEL selector, id animation, id key);

/* stderr does not reach the kernel's log from SpringBoard; the console does */
static void
say(const char *format, ...)
{
	char line[200];
	va_list ap;
	int n;

	if (say_fd < 0) {
		say_fd = open("/dev/console", O_WRONLY | O_NOCTTY | O_NONBLOCK);
		if (say_fd < 0) {
			say_fd = 2;
		}
	}
	va_start(ap, format);
	n = vsnprintf(line, sizeof(line), format, ap);
	va_end(ap);
	if (n > (int)sizeof(line) - 1) {
		n = sizeof(line) - 1;
	}
	if (n > 0) {
		(void)write(say_fd, line, (size_t)n);
	}
}

/* What kern.bootargs asks of RATES_ARG: its digit, or everything when it is absent or not a digit. */
static unsigned
rates_from(const char *args)
{
	size_t length = strlen(RATES_ARG);
	const char *p;

	for (p = args; (p = strstr(p, RATES_ARG)) != NULL; p += length) {
		if ((p == args || p[-1] == ' ') && p[length] >= '0' && p[length] <= '3' &&
		    (p[length + 1] == '\0' || p[length + 1] == ' ')) {
			return (unsigned)(p[length] - '0');
		}
	}
	return RATES_ALL;
}

/* Does this request already ask for what the panel can do? */
static bool
range_is_full(frame_rate_range asked)
{
	return asked.maximum >= 120.0f && asked.minimum >= 80.0f;
}

/* A mark on the link itself: UIKit makes one for every animation, and a list of them filled up at once. */
static bool
full_rate(id link)
{
	return objc_getAssociatedObject(link, &full_rate_mark) != NULL;
}

static id
link_with(id self, SEL selector, id target, SEL action)
{
	id link = original_link_with(self, selector, target, action);

	if (link != NULL && target != NULL && strcmp(object_getClassName(target), RATES_FULL_FOR) == 0) {
		objc_setAssociatedObject(link, &full_rate_mark, (id)link, OBJC_ASSOCIATION_ASSIGN);
		if (__atomic_fetch_add(&said, 1, __ATOMIC_RELAXED) < RATES_SAID) {
			say("fuxi: SpringBoard: a display link for %s runs at the panel's rate\n", RATES_FULL_FOR);
		}
	}
	return link;
}

static void
set_fps(id self, SEL selector, long fps)
{
	original_set_fps(self, selector, full_rate(self) ? 0 : fps);
}

static void
set_range(id self, SEL selector, frame_rate_range range)
{
	if (full_rate(self)) {
		range = (frame_rate_range){ 0.0f, 0.0f, 0.0f };
	}
	original_set_range(self, selector, range);
}

static void
add_animation(id self, SEL selector, id animation, id key)
{
	SEL range_of = sel_registerName("preferredFrameRateRange");
	SEL set_range_of = sel_registerName("setPreferredFrameRateRange:");

	if (animation != NULL && class_respondsToSelector(object_getClass(animation), set_range_of)) {
		frame_rate_range asked = ((frame_rate_range (*)(id, SEL))objc_msgSend)(animation, range_of);

		if (!range_is_full(asked)) {
			id copy = ((id (*)(id, SEL))objc_msgSend)(animation, sel_registerName("copy"));

			if (__atomic_fetch_add(&said, 1, __ATOMIC_RELAXED) < RATES_SAID) {
				say("fuxi: SpringBoard: %s asked for %.0f..%.0f, now 80..120\n", object_getClassName(animation),
				    (double)asked.minimum, (double)asked.maximum);
			}
			((void (*)(id, SEL, frame_rate_range))objc_msgSend)(copy, set_range_of, (frame_rate_range){ 80.0f, 120.0f, 120.0f });
			original_add_animation(self, selector, copy, key);
			((void (*)(id, SEL))objc_msgSend)(copy, sel_registerName("release"));
			return;
		}
	}
	original_add_animation(self, selector, animation, key);
}

static void
hook_links(void)
{
	Class link = objc_getClass("CADisplayLink");
	Method made, fps, range;

	if (link == NULL) {
		return;                         /* QuartzCore not loaded yet */
	}
	links_hooked = true;
	made = class_getClassMethod(link, sel_registerName("displayLinkWithTarget:selector:"));
	fps = class_getInstanceMethod(link, sel_registerName("setPreferredFramesPerSecond:"));
	range = class_getInstanceMethod(link, sel_registerName("setPreferredFrameRateRange:"));
	if (made == NULL || fps == NULL || range == NULL) {
		say("fuxi: SpringBoard: CADisplayLink's methods not found; display links NOT changed\n");
		return;
	}
	original_link_with = (id (*)(id, SEL, id, SEL))method_setImplementation(made, (IMP)link_with);
	original_set_fps = (void (*)(id, SEL, long))method_setImplementation(fps, (IMP)set_fps);
	original_set_range = (void (*)(id, SEL, frame_rate_range))method_setImplementation(range, (IMP)set_range);
	say("fuxi: SpringBoard: %s's display links at the panel's rate\n", RATES_FULL_FOR);
}

static void
hook_animations(void)
{
	Class layer = objc_getClass("CALayer");
	Method add;

	if (layer == NULL) {
		return;
	}
	animations_hooked = true;
	add = class_getInstanceMethod(layer, sel_registerName("addAnimation:forKey:"));
	if (add == NULL) {
		say("fuxi: SpringBoard: -[CALayer addAnimation:forKey:] not found; animations NOT changed\n");
		return;
	}
	original_add_animation = (void (*)(id, SEL, id, id))method_setImplementation(add, (IMP)add_animation);
	say("fuxi: SpringBoard: animations ask for 80..120 a second\n");
}

static void
image_added_rates(__unused const struct mach_header *header, __unused intptr_t slide)
{
	if ((wanted & RATES_LINK) != 0 && !links_hooked) {
		hook_links();
	}
	if ((wanted & RATES_ANIMATION) != 0 && !animations_hooked) {
		hook_animations();
	}
}

__attribute__((constructor)) static void
fuxi_rates_init(void)
{
	char args[1024];
	size_t size = sizeof(args) - 1;
	const char *name = getprogname();

	if (name == NULL || strcmp(name, "SpringBoard") != 0 || sysctlbyname("kern.bootargs", args, &size, NULL, 0) != 0) {
		return;
	}
	args[size < sizeof(args) ? size : sizeof(args) - 1] = '\0';
	wanted = rates_from(args);
	say("fuxi: SpringBoard: frame rates %u (fxrate)\n", wanted);
	if (wanted != 0) {
		/* called at once for every image already there, then for each one added */
		_dyld_register_func_for_add_image(image_added_rates);
	}
}
