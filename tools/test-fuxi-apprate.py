#!/usr/bin/env python3
"""tools/fuxi-sb/FuxiAppRate.c on the host: the key an application's info dictionary gets, the launchd jobs runningboardd
hands it to, fxapp=0, and that the library is built and installed with the same path."""
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'tools/fuxi-sb/FuxiAppRate.c'
LIB = '/usr/local/lib/fuxi/libfuxi-app.dylib'

STUBS = {
    'CoreFoundation/CoreFoundation.h': r'''#pragma once
typedef const struct __CFString *CFStringRef;
typedef const struct __CFDictionary *CFDictionaryRef;
typedef struct __CFDictionary *CFMutableDictionaryRef;
typedef struct __CFBundle *CFBundleRef;
typedef const struct __CFBoolean *CFBooleanRef;
extern const CFBooleanRef kCFBooleanTrue, kCFBooleanFalse;
#define CFSTR(s) ((CFStringRef)(const void *)(s))
CFBundleRef CFBundleGetMainBundle(void);
CFDictionaryRef CFBundleGetInfoDictionary(CFBundleRef bundle);
const void *CFDictionaryGetValue(CFDictionaryRef dictionary, const void *key);
void CFDictionarySetValue(CFMutableDictionaryRef dictionary, const void *key, const void *value);
''',
    'objc/runtime.h': r'''#pragma once
#include <stdbool.h>
typedef struct objc_object *id;
typedef struct objc_class *Class;
typedef const struct objc_selector *SEL;
typedef struct objc_method *Method;
typedef void (*IMP)(void);
Class objc_getClass(const char *name);
Method class_getInstanceMethod(Class cls, SEL sel);
Class object_getClass(id object);
bool class_respondsToSelector(Class cls, SEL sel);
SEL sel_registerName(const char *name);
IMP method_setImplementation(Method method, IMP imp);
''',
    'objc/message.h': '#pragma once\n#include <objc/runtime.h>\nextern void (*objc_msgSend)(void);\n',
    'mach-o/dyld.h': '#pragma once\n#include <stdint.h>\nstruct mach_header;\nvoid _dyld_register_func_for_add_image(void (*)(const struct mach_header *, intptr_t));\n',
    'sys/sysctl.h': '#pragma once\n#include <stddef.h>\nint sysctlbyname(const char *, void *, size_t *, void *, size_t);\nconst char *getprogname(void);\n#define __unused __attribute__((unused))\n',
    'os/log.h': '#pragma once\n#define OS_LOG_DEFAULT ((void *)0)\n#define os_log(log, format, ...) ((void)(log), (void)(format))\n',
    'dlfcn.h': '#pragma once\n#define RTLD_DEFAULT ((void *)-2)\nvoid *fuxi_dlsym(void *handle, const char *name);\n#define dlsym fuxi_dlsym\n',
}

TEST = r'''
#include <assert.h>

/* ---- CoreFoundation: a main bundle whose info dictionary holds one key ---- */
struct __CFBoolean { int value; };
static const struct __CFBoolean true_value = { 1 }, false_value = { 0 };
const CFBooleanRef kCFBooleanTrue = &true_value, kCFBooleanFalse = &false_value;
struct __CFDictionary { const char *key; const void *value; };
struct __CFBundle { struct __CFDictionary *info; };
static struct __CFDictionary info_dict;
static struct __CFBundle main_bundle = { &info_dict };
static bool has_bundle = true;
CFBundleRef CFBundleGetMainBundle(void) { return has_bundle ? &main_bundle : NULL; }
CFDictionaryRef CFBundleGetInfoDictionary(CFBundleRef bundle) { return bundle->info; }
const void *CFDictionaryGetValue(CFDictionaryRef d, const void *key) {
	return d->key != NULL && strcmp(d->key, (const char *)key) == 0 ? d->value : NULL;
}
void CFDictionarySetValue(CFMutableDictionaryRef d, const void *key, const void *value) { d->key = key; d->value = value; }

/* ---- libxpc: dictionaries and strings with reference counts ---- */
static const char dict_type, string_type;
struct xobj { const void *type; int refs, n; const char *keys[8]; struct xobj *values[8]; char str[LIST_MAX + 64]; };
static struct xobj pool[64];
static int pooled;
static struct xobj *xnew(const void *type) {
	struct xobj *o = &pool[pooled++];
	assert(pooled < 64);
	memset(o, 0, sizeof(*o)); o->type = type; o->refs = 1; return o;
}
static int xfind(struct xobj *d, const char *key) {
	assert(d->type == &dict_type);
	for (int i = 0; i < d->n; i++) if (strcmp(d->keys[i], key) == 0) return i;
	return -1;
}
static void *x_get_value(void *d, const char *key) { int i = xfind(d, key); return i < 0 ? NULL : ((struct xobj *)d)->values[i]; }
static const char *x_get_string(void *d, const char *key) {
	struct xobj *v = x_get_value(d, key); return v != NULL && v->type == &string_type ? v->str : NULL;
}
static void x_set_value(void *d, const char *key, void *value) {
	struct xobj *o = d; int i = xfind(o, key);
	((struct xobj *)value)->refs++;
	if (i < 0) { i = o->n++; o->keys[i] = key; } else { o->values[i]->refs--; }
	o->values[i] = value;
}
static void x_set_string(void *d, const char *key, const char *s) {
	struct xobj *v = xnew(&string_type); snprintf(v->str, sizeof(v->str), "%s", s); x_set_value(d, key, v); v->refs--;
}
static void *x_create(const char *const *keys, void *const *values, size_t count) {
	(void)keys; (void)values; assert(count == 0); return xnew(&dict_type);
}
static const void *x_type(void *o) { return ((struct xobj *)o)->type; }
static void x_release(void *o) { ((struct xobj *)o)->refs--; }
static const char *missing_symbol = "";
void *fuxi_dlsym(void *handle, const char *name) {
	assert(handle == RTLD_DEFAULT);
	if (strcmp(name, missing_symbol) == 0) return NULL;
	if (!strcmp(name, "xpc_dictionary_get_value")) return (void *)x_get_value;
	if (!strcmp(name, "xpc_dictionary_get_string")) return (void *)x_get_string;
	if (!strcmp(name, "xpc_dictionary_set_string")) return (void *)x_set_string;
	if (!strcmp(name, "xpc_dictionary_set_value")) return (void *)x_set_value;
	if (!strcmp(name, "xpc_dictionary_create")) return (void *)x_create;
	if (!strcmp(name, "xpc_get_type")) return (void *)x_type;
	if (!strcmp(name, "xpc_release")) return (void *)x_release;
	if (!strcmp(name, "_xpc_type_dictionary")) return (void *)&dict_type;
	assert(0); return NULL;
}

/* ---- Objective-C: RBLaunchdJobManager and process identities ---- */
struct objc_selector { const char *name; };
struct objc_method { IMP imp; };
struct objc_class { const char *name; bool application, answers; };
struct objc_object { struct objc_class *isa; };
static struct objc_selector sel_generate = { JOBS_METHOD }, sel_application = { "isApplication" };
static struct objc_method generate_method;
static bool jobs_loaded, has_method = true;
static struct objc_class jobs_class = { JOBS_CLASS, false, false };
static struct objc_class app_class = { "RBSProcessIdentity", true, true }, daemon_class = { "RBSProcessIdentity", false, true };
static struct objc_class plain_class = { "NSObject", true, false };
Class objc_getClass(const char *name) { return jobs_loaded && strcmp(name, JOBS_CLASS) == 0 ? &jobs_class : NULL; }
Method class_getInstanceMethod(Class c, SEL s) { return c == &jobs_class && s == &sel_generate && has_method ? &generate_method : NULL; }
SEL sel_registerName(const char *name) {
	if (!strcmp(name, sel_generate.name)) return &sel_generate;
	if (!strcmp(name, sel_application.name)) return &sel_application;
	assert(0); return NULL;
}
IMP method_setImplementation(Method m, IMP imp) { IMP old = m->imp; m->imp = imp; return old; }
Class object_getClass(id o) { return o->isa; }
bool class_respondsToSelector(Class c, SEL s) { return s == &sel_application && c->answers; }
static bool message(id self, SEL s) { assert(s == &sel_application); return self->isa->application; }
void (*objc_msgSend)(void) = (void (*)(void))message;
static void *next_job;
static id original(id self, SEL s, id identity, id context, id *error) {
	(void)self; (void)s; (void)identity; (void)context; (void)error; return (id)next_job;
}

/* ---- dyld, sysctl ---- */
static void (*add_image)(const struct mach_header *, intptr_t);
void _dyld_register_func_for_add_image(void (*f)(const struct mach_header *, intptr_t)) { add_image = f; f(NULL, 0); }
static char bootargs[256];
int sysctlbyname(const char *n, void *o, size_t *len, void *ni, size_t nl) {
	(void)ni; (void)nl; assert(!strcmp(n, "kern.bootargs"));
	size_t l = strlen(bootargs) + 1; if (l > *len) l = *len; memcpy(o, bootargs, l); *len = l; return 0;
}
static const char *progname;
const char *getprogname(void) { return progname; }

static void *launch(struct objc_class *identity_class, void *job) {
	struct objc_object identity = { identity_class };
	next_job = job;
	return ((id (*)(id, SEL, id, id, id *))generate_method.imp)(NULL, &sel_generate, &identity, NULL, NULL);
}

int main(void)
{
	char out[LIST_MAX];

	/* fxapp=0, the whole word only */
	assert(app_on("") && app_on("debug=0xc fxrate=0") && app_on("fxapp=1") && app_on("afxapp=0") && app_on("fxapp=01"));
	assert(!app_on("fxapp=0") && !app_on("serial=3 fxapp=0 wdt=-1") && !app_on("afxapp=0 fxapp=0"));
	/* DYLD_INSERT_LIBRARIES lists */
	assert(list_with(NULL, out, sizeof(out)) && !strcmp(out, APP_LIB));
	assert(list_with("", out, sizeof(out)) && !strcmp(out, APP_LIB));
	assert(list_with("/a.dylib", out, sizeof(out)) && !strcmp(out, "/a.dylib:" APP_LIB));
	assert(!list_with("/a.dylib:" APP_LIB, out, sizeof(out)) && !list_with(APP_LIB ":/b.dylib", out, sizeof(out)));
	assert(list_with(APP_LIB ".old", out, sizeof(out)) && !strcmp(out, APP_LIB ".old:" APP_LIB));
	assert(!list_with("/a.dylib", out, 12));
	assert(list_without(APP_LIB, out, sizeof(out)) && !strcmp(out, ""));
	assert(list_without("/a.dylib:" APP_LIB ":/b.dylib", out, sizeof(out)) && !strcmp(out, "/a.dylib:/b.dylib"));
	assert(!list_without("/a.dylib:" APP_LIB ".old", out, sizeof(out)));

	/* an application: the key is set once, and the library leaves the environment */
	progname = "Preferences";
	setenv("DYLD_INSERT_LIBRARIES", "/x.dylib:" APP_LIB, 1);
	fuxi_app_init();
	assert(CFDictionaryGetValue(&info_dict, APP_KEY) == kCFBooleanTrue);
	assert(!strcmp(getenv("DYLD_INSERT_LIBRARIES"), "/x.dylib"));
	setenv("DYLD_INSERT_LIBRARIES", APP_LIB, 1);
	fuxi_app_init();                                        /* already YES: said, left */
	assert(getenv("DYLD_INSERT_LIBRARIES") == NULL);
	CFDictionarySetValue(&info_dict, APP_KEY, kCFBooleanFalse);
	fuxi_app_init();                                        /* NO becomes YES too */
	assert(CFDictionaryGetValue(&info_dict, APP_KEY) == kCFBooleanTrue);
	has_bundle = false;
	fuxi_app_init();
	has_bundle = true;
	assert(add_image == NULL);                              /* an application hooks nothing */

	/* runningboardd with fxapp=0: nothing */
	progname = "runningboardd";
	generate_method.imp = (IMP)original;
	strcpy(bootargs, "serial=3 fxapp=0");
	fuxi_app_init();
	assert(add_image == NULL && generate_method.imp == (IMP)original);
	/* libxpc incomplete: nothing */
	strcpy(bootargs, "serial=3");
	missing_symbol = "xpc_get_type";
	fuxi_app_init();
	assert(add_image == NULL);
	missing_symbol = "";
	/* RunningBoard not loaded yet: hooked from the image callback, once */
	fuxi_app_init();
	assert(add_image != NULL && hooked == 0 && generate_method.imp == (IMP)original);
	jobs_loaded = true;
	add_image(NULL, 0);
	assert(hooked == 1 && generate_method.imp == (IMP)generate && original_generate == original);
	add_image(NULL, 0);
	assert(original_generate == original);

	/* an application's job without an environment gets one */
	struct xobj *job = xnew(&dict_type);
	x_set_string(job, JOB_PROGRAM, "/Applications/Preferences.app/Preferences");
	assert(launch(&app_class, job) == job);
	struct xobj *env = x_get_value(job, JOB_ENV);
	assert(env != NULL && env->type == &dict_type && env->refs == 1);
	assert(!strcmp(x_get_string(env, "DYLD_INSERT_LIBRARIES"), APP_LIB));
	/* again (relaunch of the same job data): unchanged */
	assert(launch(&app_class, job) == job && env->n == 1 && !strcmp(x_get_string(env, "DYLD_INSERT_LIBRARIES"), APP_LIB));
	/* an environment with another library keeps it */
	job = xnew(&dict_type); env = xnew(&dict_type);
	x_set_string(env, "DYLD_INSERT_LIBRARIES", "/other.dylib"); x_set_string(env, "HOME", "/var/mobile");
	x_set_value(job, JOB_ENV, env); x_release(env);
	launch(&app_class, job);
	assert(!strcmp(x_get_string(env, "DYLD_INSERT_LIBRARIES"), "/other.dylib:" APP_LIB) && !strcmp(x_get_string(env, "HOME"), "/var/mobile"));
	/* daemons, identities that cannot answer, closure builds, odd environments and no job: left alone */
	job = xnew(&dict_type);
	launch(&daemon_class, job);
	launch(&plain_class, job);
	assert(x_get_value(job, JOB_ENV) == NULL);
	env = xnew(&dict_type); x_set_string(env, JOB_CLOSURE, "1"); x_set_value(job, JOB_ENV, env); x_release(env);
	launch(&app_class, job);
	assert(x_get_string(env, "DYLD_INSERT_LIBRARIES") == NULL);
	job = xnew(&dict_type); x_set_string(job, JOB_ENV, "not a dictionary");
	launch(&app_class, job);
	assert(x_get_string(job, JOB_ENV) != NULL);
	assert(launch(&app_class, NULL) == NULL);
	/* a RunningBoard without the method: left alone, said once */
	hooked = 0; has_method = false;
	assert(try_hook() && hooked == -1);
	puts("apprate model ok");
	return 0;
}
'''


def main():
    text = SOURCE.read_text()
    assert text.startswith('/*\n * MIT License')
    assert '#define APP_KEY         "CADisableMinimumFrameDurationOnPhone"' in text
    assert '#define APP_LIB         "' + LIB + '"' in text
    # QuartzCore reads the Info.plist dictionary, so that is the one changed; nothing else is hooked in an application.
    assert 'CFBundleGetInfoDictionary(bundle)' in text and 'CFBundleGetLocalInfoDictionary' not in text
    assert text.count('method_setImplementation(') == 1 and 'NOT changed' in text
    assert 'strcmp(name, "runningboardd") != 0' in text
    build = (ROOT / 'tools/build-fuxi-sys.sh').read_text()
    assert 'tools/fuxi-sb/FuxiAppRate.c' in build and '-install_name ' + LIB in build and 'libfuxi-app.dylib' in build
    root_tool = (ROOT / 'tools/fuxi-sys-root.py').read_text()
    assert "APP_LIB = '" + LIB + "'" in root_tool and "APP_JOB = 'com.apple.runningboardd'" in root_tool
    with tempfile.TemporaryDirectory(prefix='fuxi-apprate-') as tmp:
        inc = Path(tmp) / 'inc'
        for rel, stub in STUBS.items():
            (inc / rel).parent.mkdir(parents=True, exist_ok=True)
            (inc / rel).write_text(stub)
        # The model calls the constructor itself, once per scenario.
        assert text.count('__attribute__((constructor)) static void') == 1
        source = re.sub(r'__attribute__\(\(constructor\)\) static void', 'static void', text)
        c = Path(tmp) / 'apprate.c'
        c.write_text('#include <sys/sysctl.h>\n' + source + TEST)
        exe = Path(tmp) / 'apprate'
        cc = os.environ.get('CC', 'cc')
        subprocess.run([cc, '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-Wno-cast-function-type',
                        '-I', str(inc), str(c), '-o', str(exe)], check=True)
        run = subprocess.run([str(exe)], check=True, capture_output=True, text=True)
    out = run.stdout + run.stderr
    assert 'apprate model ok' in out, out
    for line in ('fuxi: Preferences: CADisableMinimumFrameDurationOnPhone = YES, frames up to the panel\'s rate',
                 'fuxi: Preferences: CADisableMinimumFrameDurationOnPhone was YES already',
                 'fuxi: Preferences: no Info.plist; frame rate NOT changed',
                 'fuxi: runningboardd: fxapp=0, applications start unchanged',
                 'fuxi: runningboardd: libxpc\'s dictionary calls not found; applications NOT changed',
                 'fuxi: runningboardd: applications start with ' + LIB + ' (fxapp)',
                 'fuxi: runningboardd: Preferences starts with ' + LIB,
                 'fuxi: runningboardd: a job\'s EnvironmentVariables is not a dictionary; NOT changed',
                 'fuxi: runningboardd: -[RBLaunchdJobManager _generateDataWithIdentity:context:error:] not found'):
        assert line in out, line
    print('PASS: applications\' CADisableMinimumFrameDurationOnPhone, runningboardd job environment, fxapp= (host model)')


if __name__ == '__main__':
    main()
