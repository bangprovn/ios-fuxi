/*
 * fxinj=N (FUXI): DYLD_INSERT_LIBRARIES added by the kernel at exec, for some programs.
 *
 * launchd (20H392) drops every DYLD_ variable except DYLD_PRINT_* from a job's EnvironmentVariables
 * ("Disallowing environment variable: %s", the test at launchd 0x10001a720 called from 0x1000092e8), and lets them
 * through only for a job whose label does not start with "com.apple." (0x100008b34). So the libraries named in the
 * launchd cache for com.apple.SpringBoard and com.apple.runningboardd never reached dyld (root v26: no line from
 * either). HakiOS adds the variable in the kernel for the same reason (hakios-xnu-kernel xnu/hakios.patch,
 * kern.exec_inject: "launchd refuses to pass DYLD_ variables from a job description").
 *
 *   bit 0 (1)  the main executable of an application (".../NAME.app/EXECUTABLE"), SpringBoard excepted:
 *              libfuxi-app.dylib (CADisableMinimumFrameDurationOnPhone = YES, tools/fuxi-sb/FuxiAppRate.c)
 *   bit 1 (2)  SpringBoard: libfuxi-sb.dylib (tools/fuxi-sb/FuxiRates.c, FuxiSnapshots.c)
 *
 * dyld honours it because AMFI's dyld policy answer (patch 0103, request 0x5a) allows everything, including a library
 * that is missing. The variable follows the program's own environment. Pure functions only:
 * tools/test-fuxi-inject.py compiles this file on the host.
 */
#define FUXI_INJECT_APPS        1u
#define FUXI_INJECT_SPRINGBOARD 2u
#define FUXI_INJECT_ALL         (FUXI_INJECT_APPS | FUXI_INJECT_SPRINGBOARD)
#define FUXI_INJECT_APP_LIB     "/usr/local/lib/fuxi/libfuxi-app.dylib"
#define FUXI_INJECT_SB_LIB      "/usr/local/lib/fuxi/libfuxi-sb.dylib"
#define FUXI_INJECT_ENV_MAX     128
#define FUXI_INJECT_SAID        24      /* the first programs given a library are logged */

/* path is ".../X.app/EXECUTABLE": an application's main executable, not an .appex or a helper deeper inside */
static inline bool
fuxi_inject_is_app(const char *path)
{
	const char *slash = strrchr(path, '/');

	return slash != NULL && slash[1] != '\0' && slash - path >= 5 && memcmp(slash - 4, ".app", 4) == 0 &&
	       slash[-5] != '/';
}

/* The library this program gets under mode, or NULL. name is the last path component. */
static inline const char *
fuxi_inject_library(unsigned mode, const char *path, const char *name)
{
	if (path == NULL || name == NULL) {
		return NULL;
	}
	if (strcmp(name, "SpringBoard") == 0) {
		return (mode & FUXI_INJECT_SPRINGBOARD) != 0 ? FUXI_INJECT_SB_LIB : NULL;
	}
	return (mode & FUXI_INJECT_APPS) != 0 && fuxi_inject_is_app(path) ? FUXI_INJECT_APP_LIB : NULL;
}

/* "DYLD_INSERT_LIBRARIES=library" into env; false without a library or room */
static inline bool
fuxi_inject_env(const char *library, char *env, size_t size)
{
	int n;

	if (library == NULL || size == 0) {
		return false;
	}
	n = snprintf(env, size, "DYLD_INSERT_LIBRARIES=%s", library);
	return n > 0 && (size_t)n < size;
}
