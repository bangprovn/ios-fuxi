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
 * libfuxi-app.dylib: CADisableMinimumFrameDurationOnPhone = YES in every application, with its signed Info.plist
 * left as it is (HakiOS wrote the key into the files, mkhighrate.py c7b5f9a; here that breaks the code signature
 * SplashBoard checks, and the applications open black).
 *
 * QuartzCore of iOS 16.7 reads the key once, in the application's own process (dispatch_once block at
 * QuartzCore+0x247c94): CFDictionaryGetValue(CFBundleGetInfoDictionary(CFBundleGetMainBundle()), key). Without it
 * CADisplayLink's frame-rate client (QuartzCore+0x8ab00), CADynamicFrameRateSource (+0x1be1ac),
 * -[CAMetalDrawable present] and -[CAAnimation _setCARenderAnimation:layer:] hold the process to 60 a second
 * before anything reaches backboardd, so the window server cannot lift it. It is the Info.plist dictionary, not
 * the localized one: InfoPlist.loctable (root v23) did nothing. CFBundle parses Info.plist with
 * kCFPropertyListMutableContainers (CoreFoundation+0x559d0), so the dictionary every caller gets can take the key.
 *
 * The kernel inserts this library into every application's main executable (boot-arg fxinj=1, patch 0121): launchd
 * drops DYLD_ variables from com.apple jobs, so runningboardd could not be given it (root v26). The constructor sets
 * the key in the main bundle's info dictionary. dyld runs an inserted library's initializers before the
 * application's own and QuartzCore's, and QuartzCore reads the key only later, at its first frame rate or animation.
 * The library then leaves DYLD_INSERT_LIBRARIES, so nothing the application starts inherits it.
 */
#include <CoreFoundation/CoreFoundation.h>
#include <os/log.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define APP_KEY         "CADisableMinimumFrameDurationOnPhone"
#define APP_LIB         "/usr/local/lib/fuxi/libfuxi-app.dylib"
#define INSERT          "DYLD_INSERT_LIBRARIES"
#define LIST_MAX        1024

/* stderr of launchd's jobs is /dev/null; the kernel copies that to its log for the programs in boot-arg sweetnull */
static void
say(const char *format, ...)
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

/* list without APP_LIB (and without empty entries), into out; false when it was not there or out has no room */
static bool
list_without(const char *list, char *out, size_t size)
{
	size_t length = strlen(APP_LIB), used = 0, entry;
	const char *p = list, *end;
	bool found = false;

	if (size == 0) {
		return false;
	}
	out[0] = '\0';
	while (p != NULL) {
		end = strchr(p, ':');
		entry = end != NULL ? (size_t)(end - p) : strlen(p);
		if (entry == length && strncmp(p, APP_LIB, length) == 0) {
			found = true;
		} else if (entry > 0) {
			if (used + (used > 0) + entry + 1 > size) {
				return false;
			}
			if (used > 0) {
				out[used++] = ':';
			}
			memcpy(out + used, p, entry);
			used += entry;
			out[used] = '\0';
		}
		p = end != NULL ? end + 1 : NULL;
	}
	return found;
}

static void
app_rate(const char *name)
{
	CFBundleRef bundle;
	CFDictionaryRef info;
	CFStringRef key = CFSTR(APP_KEY);
	const char *inserted = getenv(INSERT);
	char rest[LIST_MAX];

	if (inserted != NULL && list_without(inserted, rest, sizeof(rest))) {
		if (rest[0] != '\0') {
			setenv(INSERT, rest, 1);
		} else {
			unsetenv(INSERT);
		}
	}
	bundle = CFBundleGetMainBundle();
	info = bundle != NULL ? CFBundleGetInfoDictionary(bundle) : NULL;
	if (info == NULL) {
		say("fuxi: %s: no Info.plist; frame rate NOT changed\n", name);
		return;
	}
	if (CFDictionaryGetValue(info, key) == kCFBooleanTrue) {
		say("fuxi: %s: %s was YES already\n", name, APP_KEY);
		return;
	}
	CFDictionarySetValue((CFMutableDictionaryRef)info, key, kCFBooleanTrue);
	if (CFDictionaryGetValue(info, key) == kCFBooleanTrue) {
		say("fuxi: %s: %s = YES, frames up to the panel's rate\n", name, APP_KEY);
	} else {
		say("fuxi: %s: %s did not take; frame rate NOT changed\n", name, APP_KEY);
	}
}

__attribute__((constructor)) static void
fuxi_app_init(void)
{
	const char *name = getprogname();

	app_rate(name != NULL ? name : "?");
}
