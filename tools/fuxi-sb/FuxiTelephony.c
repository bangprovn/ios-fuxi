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
 * SpringBoard without CommCenter: no CoreTelephony reconnect every second.
 *
 * The root has no com.apple.CommCenter job: tools/sweet-root-tweaks.py takes both of them out (the cellular one and
 * com.apple.CommCenter-noncellular). The noncellular one is the job launchd picks without a /baseband node, yet on a
 * kernel without AppleIPAppender and AppleBaseband it still died every 15 s or so on HakiOS, with the same iPhone10,3
 * identity and no /baseband node (SIGSEGV after "Could not find AppleIPAppender service", SIGABRT after "Unable to
 * communicate with AppleBaseband driver"). Not tried on this port; each of those restarts would reset the idle timer too.
 *
 * Without the daemon, SpringBoard 20H392 loops for ever, once a second:
 *   -[SBTelephonyManager _serverConnectionDidError:]  logs "CTServerConnection returned error", then always
 *       performSelector:_handleTelephonyDaemonRestart afterDelay:1.0;
 *   -_handleTelephonyDaemonRestart makes a new connection and calls -handleCallControlFailure: -> -updateCalls ->
 *       -updateSpringBoard, which ends in -[SBIdleTimerGlobalCoordinator resetIdleTimer];
 *   the idle timer's descriptor asks -isInEmergencyCallbackMode, which queries CoreTelephony while its cached state
 *       is unknown (ivar sign bit), the query fails (launchd: com.apple.commcenter.xpc, No such process), and that
 *       error is the next -_serverConnectionDidError:.
 * r-tour1 (research_notes/suc-khoe-r-tour1.md, item 4): 89 idle-timer resets and 89 CT errors in 90 s of idle, so
 * the 60 s auto-lock never came. MobileGestalt already says cellular-data = NO ("non-cellular device"); nothing in
 * SpringBoard's code skips the loop for that, so no root setting can stop it.
 *
 * When launchd has no com.apple.commcenter.xpc at SpringBoard's start (bootstrap_look_up fails), two methods change:
 *   -[SBTelephonyManager _serverConnectionDidError:]  said for the first errors, nothing scheduled
 *   -[SBTelephonyManager isInEmergencyCallbackMode]   NO, without a CoreTelephony query (no modem, no callback mode)
 * With the service there (a root with CommCenter again), SpringBoard is left alone. Boot-arg fxct=0 leaves it alone
 * too, for A/B runs without a root edit. The methods are found by name and left alone if missing.
 */
#include <fcntl.h>
#include <mach-o/dyld.h>
#include <mach/mach.h>
#include <objc/message.h>
#include <objc/runtime.h>
#include <servers/bootstrap.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/sysctl.h>
#include <unistd.h>

#define CT_CLASS                "SBTelephonyManager"
#define CT_ERROR_SELECTOR       "_serverConnectionDidError:"
#define CT_ECBM_SELECTOR        "isInEmergencyCallbackMode"
#define CT_SERVICE              "com.apple.commcenter.xpc"
#define CT_ARG                  "fxct"
#define CT_SAID                 4       /* the first errors are logged */

static int say_fd = -1;
static unsigned errors_said;
static int hooked;                      /* 0 not yet, 1 replaced, -1 given up */

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

/* Does this boot line carry WORD=0? */
static bool
arg_off(const char *args, const char *word)
{
	size_t length = strlen(word);
	const char *p;

	for (p = args; (p = strstr(p, word)) != NULL; p += length) {
		if ((p == args || p[-1] == ' ') && p[length] == '=' && p[length + 1] == '0' &&
		    (p[length + 2] == '\0' || p[length + 2] == ' ')) {
			return true;
		}
	}
	return false;
}

/* -[SBTelephonyManager _serverConnectionDidError:] with no CommCenter to come back: say it, schedule nothing */
static void
connection_did_error(__unused id self, __unused SEL selector, __unused id error)
{
	unsigned n = __atomic_add_fetch(&errors_said, 1, __ATOMIC_RELAXED);

	if (n <= CT_SAID) {
		say("fuxi: SpringBoard: CoreTelephony error %u, no CommCenter: not reconnecting\n", n);
	}
}

/* -[SBTelephonyManager isInEmergencyCallbackMode]: no modem, so never */
static BOOL
in_emergency_callback_mode(__unused id self, __unused SEL selector)
{
	return NO;
}

/* Is CommCenter's service in launchd? A look-up does not start the daemon. */
static bool
commcenter_present(kern_return_t *result)
{
	mach_port_t port = MACH_PORT_NULL;

	*result = bootstrap_look_up(bootstrap_port, CT_SERVICE, &port);
	if (*result != KERN_SUCCESS) {
		return false;
	}
	if (port != MACH_PORT_NULL) {
		(void)mach_port_deallocate(mach_task_self(), port);
	}
	return true;
}

/* Replace both methods once the class is there. True when done (either way). */
static bool
try_hook(void)
{
	Class cls;
	Method error_method, ecbm_method;

	if (__atomic_load_n(&hooked, __ATOMIC_ACQUIRE) != 0) {
		return true;
	}
	cls = objc_getClass(CT_CLASS);
	if (cls == NULL) {
		return false;                   /* SpringBoard's framework not there yet */
	}
	error_method = class_getInstanceMethod(cls, sel_registerName(CT_ERROR_SELECTOR));
	ecbm_method = class_getInstanceMethod(cls, sel_registerName(CT_ECBM_SELECTOR));
	if (error_method == NULL || ecbm_method == NULL) {
		__atomic_store_n(&hooked, -1, __ATOMIC_RELEASE);
		say("fuxi: SpringBoard: -[%s %s] or -%s not found; CoreTelephony errors NOT changed\n", CT_CLASS,
		    CT_ERROR_SELECTOR, CT_ECBM_SELECTOR);
		return true;
	}
	(void)method_setImplementation(error_method, (IMP)connection_did_error);
	(void)method_setImplementation(ecbm_method, (IMP)in_emergency_callback_mode);
	__atomic_store_n(&hooked, 1, __ATOMIC_RELEASE);
	say("fuxi: SpringBoard: no %s, CoreTelephony errors no longer reconnect every second\n", CT_SERVICE);
	return true;
}

static void
image_added_telephony(__unused const struct mach_header *header, __unused intptr_t slide)
{
	(void)try_hook();
}

__attribute__((constructor)) static void
fuxi_telephony_init(void)
{
	char args[1024];
	size_t size = sizeof(args) - 1;
	const char *name = getprogname();
	kern_return_t result;

	if (name == NULL || strcmp(name, "SpringBoard") != 0) {
		return;
	}
	if (sysctlbyname("kern.bootargs", args, &size, NULL, 0) == 0) {
		args[size < sizeof(args) ? size : sizeof(args) - 1] = '\0';
		if (arg_off(args, CT_ARG)) {
			say("fuxi: SpringBoard: fxct=0, CoreTelephony errors unchanged\n");
			return;
		}
	}
	if (commcenter_present(&result)) {
		say("fuxi: SpringBoard: %s is there, CoreTelephony errors unchanged\n", CT_SERVICE);
		return;
	}
	say("fuxi: SpringBoard: look-up of %s: %d\n", CT_SERVICE, (int)result);
	if (!try_hook()) {
		/* called at once for every image already there, then for each one added */
		_dyld_register_func_for_add_image(image_added_telephony);
	}
}
