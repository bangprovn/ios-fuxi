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
 * One library, two programs:
 *   runningboardd   every application job it builds for launchd (-[RBLaunchdJobManager
 *                   _generateDataWithIdentity:context:error:]) gets this library in DYLD_INSERT_LIBRARIES.
 *                   runningboardd itself gets it from its own launchd job (fuxi-sys-root.py --app-rate).
 *   an application  the constructor sets the key in the main bundle's info dictionary. dyld runs an inserted
 *                   library's initializers before the application's own and QuartzCore's, and QuartzCore reads the key
 *                   only later, at its first frame rate or animation. The library is taken out of
 *                   DYLD_INSERT_LIBRARIES again, so nothing the application starts inherits it.
 *
 * Boot-arg fxapp=0 leaves the applications alone (A/B runs without a root edit).
 */
#include <CoreFoundation/CoreFoundation.h>
#include <dlfcn.h>
#include <mach-o/dyld.h>
#include <objc/message.h>
#include <objc/runtime.h>
#include <os/log.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/sysctl.h>
#include <unistd.h>

#define APP_KEY         "CADisableMinimumFrameDurationOnPhone"
#define APP_LIB         "/usr/local/lib/fuxi/libfuxi-app.dylib"
#define APP_ARG         "fxapp="
#define APP_SAID        24              /* the first launches are logged */
#define INSERT          "DYLD_INSERT_LIBRARIES"
#define JOB_ENV         "EnvironmentVariables"
#define JOB_PROGRAM     "Program"
#define JOB_CLOSURE     "DYLD_JUST_BUILD_CLOSURE"       /* a launch that only builds dyld's closure, then exits */
#define JOBS_CLASS      "RBLaunchdJobManager"
#define JOBS_METHOD     "_generateDataWithIdentity:context:error:"
#define LIST_MAX        1024

typedef void *fuxi_xpc_t;

/* libxpc's public dictionary calls, looked up at run time: the iPhoneOS SDK does not offer them to iOS code */
static struct {
	fuxi_xpc_t (*get_value)(fuxi_xpc_t dictionary, const char *key);
	const char *(*get_string)(fuxi_xpc_t dictionary, const char *key);
	void (*set_string)(fuxi_xpc_t dictionary, const char *key, const char *value);
	void (*set_value)(fuxi_xpc_t dictionary, const char *key, fuxi_xpc_t value);
	fuxi_xpc_t (*create)(const char *const *keys, const fuxi_xpc_t *values, size_t count);
	const void *(*type_of)(fuxi_xpc_t object);
	void (*release)(fuxi_xpc_t object);
	const void *dictionary;
} xpc;

static unsigned said;
static int hooked;                      /* 1 hooked, -1 given up */

static id (*original_generate)(id self, SEL selector, id identity, id context, id *error);

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

/* false only when kern.bootargs has the word fxapp=0 */
static bool
app_on(const char *args)
{
	size_t length = strlen(APP_ARG);
	const char *p;

	for (p = args; (p = strstr(p, APP_ARG)) != NULL; p += length) {
		if ((p == args || p[-1] == ' ') && p[length] == '0' && (p[length + 1] == '\0' || p[length + 1] == ' ')) {
			return false;
		}
	}
	return true;
}

/* Is APP_LIB one of the ':'-separated entries of list? */
static bool
listed(const char *list)
{
	size_t length = strlen(APP_LIB);
	const char *p = list;

	while (p != NULL) {
		if (strncmp(p, APP_LIB, length) == 0 && (p[length] == '\0' || p[length] == ':')) {
			return true;
		}
		p = strchr(p, ':');
		if (p != NULL) {
			p++;
		}
	}
	return false;
}

/* list with APP_LIB at the end, into out; false when it is there already or out has no room */
static bool
list_with(const char *list, char *out, size_t size)
{
	int n;

	if (list != NULL && listed(list)) {
		return false;
	}
	if (list != NULL && list[0] != '\0') {
		n = snprintf(out, size, "%s:%s", list, APP_LIB);
	} else {
		n = snprintf(out, size, "%s", APP_LIB);
	}
	return n > 0 && (size_t)n < size;
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

/* ---- in an application ------------------------------------------------------------------ */

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

/* ---- in runningboardd ------------------------------------------------------------------- */

static bool
find_xpc(void)
{
	xpc.get_value = (fuxi_xpc_t (*)(fuxi_xpc_t, const char *))dlsym(RTLD_DEFAULT, "xpc_dictionary_get_value");
	xpc.get_string = (const char *(*)(fuxi_xpc_t, const char *))dlsym(RTLD_DEFAULT, "xpc_dictionary_get_string");
	xpc.set_string = (void (*)(fuxi_xpc_t, const char *, const char *))dlsym(RTLD_DEFAULT, "xpc_dictionary_set_string");
	xpc.set_value = (void (*)(fuxi_xpc_t, const char *, fuxi_xpc_t))dlsym(RTLD_DEFAULT, "xpc_dictionary_set_value");
	xpc.create = (fuxi_xpc_t (*)(const char *const *, const fuxi_xpc_t *, size_t))dlsym(RTLD_DEFAULT,
	    "xpc_dictionary_create");
	xpc.type_of = (const void *(*)(fuxi_xpc_t))dlsym(RTLD_DEFAULT, "xpc_get_type");
	xpc.release = (void (*)(fuxi_xpc_t))dlsym(RTLD_DEFAULT, "xpc_release");
	xpc.dictionary = dlsym(RTLD_DEFAULT, "_xpc_type_dictionary");
	return xpc.get_value != NULL && xpc.get_string != NULL && xpc.set_string != NULL && xpc.set_value != NULL &&
	       xpc.create != NULL && xpc.type_of != NULL && xpc.release != NULL && xpc.dictionary != NULL;
}

static bool
is_application(id identity)
{
	SEL application = sel_registerName("isApplication");

	return identity != NULL && class_respondsToSelector(object_getClass(identity), application) &&
	       ((bool (*)(id, SEL))objc_msgSend)(identity, application);
}

/* One launchd job (an xpc dictionary): this library joins its DYLD_INSERT_LIBRARIES. */
static void
insert_into(fuxi_xpc_t job)
{
	fuxi_xpc_t env;
	const char *program, *slash;
	char list[LIST_MAX];

	if (xpc.type_of(job) != xpc.dictionary) {
		return;
	}
	env = xpc.get_value(job, JOB_ENV);
	if (env != NULL && xpc.type_of(env) != xpc.dictionary) {
		say("fuxi: runningboardd: a job's %s is not a dictionary; NOT changed\n", JOB_ENV);
		return;
	}
	if (env != NULL && xpc.get_string(env, JOB_CLOSURE) != NULL) {
		return;
	}
	if (!list_with(env != NULL ? xpc.get_string(env, INSERT) : NULL, list, sizeof(list))) {
		return;
	}
	if (env == NULL) {
		env = xpc.create(NULL, NULL, 0);
		xpc.set_string(env, INSERT, list);
		xpc.set_value(job, JOB_ENV, env);
		xpc.release(env);
	} else {
		xpc.set_string(env, INSERT, list);
	}
	if (__atomic_fetch_add(&said, 1, __ATOMIC_RELAXED) < APP_SAID) {
		program = xpc.get_string(job, JOB_PROGRAM);
		slash = program != NULL ? strrchr(program, '/') : NULL;
		say("fuxi: runningboardd: %s starts with %s\n",
		    slash != NULL ? slash + 1 : program != NULL ? program : "an application", APP_LIB);
	}
}

static id
generate(id self, SEL selector, id identity, id context, id *error)
{
	id job = original_generate(self, selector, identity, context, error);

	if (job != NULL && is_application(identity)) {
		insert_into((fuxi_xpc_t)job);
	}
	return job;
}

/* true once there is nothing more to try */
static bool
try_hook(void)
{
	Class jobs = objc_getClass(JOBS_CLASS);
	Method method;

	if (jobs == NULL) {
		return false;                   /* RunningBoard not loaded yet */
	}
	method = class_getInstanceMethod(jobs, sel_registerName(JOBS_METHOD));
	if (method == NULL) {
		hooked = -1;
		say("fuxi: runningboardd: -[%s %s] not found; applications NOT changed\n", JOBS_CLASS, JOBS_METHOD);
		return true;
	}
	original_generate = (id (*)(id, SEL, id, id, id *))method_setImplementation(method, (IMP)generate);
	hooked = 1;
	say("fuxi: runningboardd: applications start with %s (fxapp)\n", APP_LIB);
	return true;
}

static void
image_added_app(__unused const struct mach_header *header, __unused intptr_t slide)
{
	if (hooked == 0) {
		(void)try_hook();
	}
}

__attribute__((constructor)) static void
fuxi_app_init(void)
{
	char args[1024];
	size_t size = sizeof(args) - 1;
	const char *name = getprogname();

	if (name == NULL) {
		return;
	}
	if (strcmp(name, "runningboardd") != 0) {
		app_rate(name);
		return;
	}
	if (sysctlbyname("kern.bootargs", args, &size, NULL, 0) != 0) {
		size = 0;
	}
	args[size < sizeof(args) ? size : sizeof(args) - 1] = '\0';
	if (!app_on(args)) {
		say("fuxi: runningboardd: fxapp=0, applications start unchanged\n");
		return;
	}
	if (!find_xpc()) {
		say("fuxi: runningboardd: libxpc's dictionary calls not found; applications NOT changed\n");
		return;
	}
	/* called at once for every image already there, then for each one added */
	_dyld_register_func_for_add_image(image_added_app);
}
