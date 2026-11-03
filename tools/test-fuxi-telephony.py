#!/usr/bin/env python3
"""tools/fuxi-sb/FuxiTelephony.c on the host: when SpringBoard's CoreTelephony methods are replaced, what they do then,
and that libfuxi-sb.dylib is built with it."""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'tools/fuxi-sb/FuxiTelephony.c'

STUBS = {
    'objc/runtime.h': r'''#pragma once
#include <stdbool.h>
typedef bool BOOL;
#define NO false
#define YES true
typedef struct objc_object { int unused; } *id;
typedef struct objc_class *Class;
typedef const struct objc_selector *SEL;
typedef struct objc_method *Method;
typedef void (*IMP)(void);
Class objc_getClass(const char *name);
Method class_getInstanceMethod(Class cls, SEL sel);
SEL sel_registerName(const char *name);
IMP method_setImplementation(Method method, IMP imp);
''',
    'objc/message.h': '#pragma once\n#include <objc/runtime.h>\n',
    'mach-o/dyld.h': '#pragma once\n#include <stdint.h>\nstruct mach_header;\n'
                     'void _dyld_register_func_for_add_image(void (*)(const struct mach_header *, intptr_t));\n',
    'mach/mach.h': '#pragma once\ntypedef unsigned mach_port_t;\ntypedef int kern_return_t;\n#define KERN_SUCCESS 0\n'
                   '#define MACH_PORT_NULL 0u\nmach_port_t mach_task_self(void);\n'
                   'kern_return_t mach_port_deallocate(mach_port_t task, mach_port_t name);\n',
    'servers/bootstrap.h': '#pragma once\n#include <mach/mach.h>\nextern mach_port_t bootstrap_port;\n'
                           'kern_return_t bootstrap_look_up(mach_port_t bp, const char *name, mach_port_t *sp);\n',
    'sys/sysctl.h': '#pragma once\n#include <stddef.h>\nint sysctlbyname(const char *, void *, size_t *, void *, size_t);\n'
                    'const char *getprogname(void);\n#define __unused __attribute__((unused))\n',
}

TEST = r'''
#include <assert.h>
struct objc_selector { const char *name; };
struct objc_method { SEL sel; IMP imp; };
struct objc_class { const char *name; struct objc_method error, ecbm; bool has_error, has_ecbm; };
static struct objc_selector sels[2] = { { "_serverConnectionDidError:" }, { "isInEmergencyCallbackMode" } };
static struct objc_class telephony = { .name = "SBTelephonyManager", .has_error = true, .has_ecbm = true };
static bool class_loaded, service_there;
static int lookups, deallocated, original_errors;
static char bootargs[256];
static const char *progname = "SpringBoard";
static void (*add_image)(const struct mach_header *, intptr_t);
mach_port_t bootstrap_port = 7;
static void original_error(id self, SEL sel, id error) { (void)self; (void)sel; (void)error; original_errors++; }
static BOOL original_ecbm(id self, SEL sel) { (void)self; (void)sel; return YES; }
Class objc_getClass(const char *name) { return class_loaded && !strcmp(name, telephony.name) ? &telephony : NULL; }
SEL sel_registerName(const char *name) { for (int i = 0; i < 2; i++) if (!strcmp(sels[i].name, name)) return &sels[i]; assert(0); return NULL; }
Method class_getInstanceMethod(Class c, SEL s)
{
	if (s == &sels[0])
		return c->has_error ? &c->error : NULL;
	return c->has_ecbm ? &c->ecbm : NULL;
}
IMP method_setImplementation(Method m, IMP imp) { IMP old = m->imp; m->imp = imp; return old; }
void _dyld_register_func_for_add_image(void (*f)(const struct mach_header *, intptr_t)) { add_image = f; f(NULL, 0); }
mach_port_t mach_task_self(void) { return 1; }
kern_return_t mach_port_deallocate(mach_port_t task, mach_port_t name) { assert(task == 1 && name == 99); deallocated++; return 0; }
kern_return_t bootstrap_look_up(mach_port_t bp, const char *name, mach_port_t *sp)
{
	assert(bp == 7 && !strcmp(name, "com.apple.commcenter.xpc"));
	lookups++;
	if (!service_there)
		return 1102;            /* BOOTSTRAP_UNKNOWN_SERVICE */
	*sp = 99;
	return KERN_SUCCESS;
}
int sysctlbyname(const char *n, void *o, size_t *len, void *ni, size_t nl)
{
	(void)ni; (void)nl; assert(!strcmp(n, "kern.bootargs"));
	size_t l = strlen(bootargs) + 1; if (l > *len) l = *len; memcpy(o, bootargs, l); *len = l; return 0;
}
const char *getprogname(void) { return progname; }
static bool untouched(void)
{
	return telephony.error.imp == (IMP)original_error && telephony.ecbm.imp == (IMP)original_ecbm;
}
int main(void)
{
	say_fd = 2;                     /* the model's output, not the host's console */
	telephony.error.imp = (IMP)original_error;
	telephony.ecbm.imp = (IMP)original_ecbm;
	class_loaded = true;
	/* another program: nothing asked, nothing touched */
	progname = "backboardd";
	fuxi_telephony_init();
	assert(untouched() && lookups == 0 && add_image == NULL);
	/* the boot-arg switch, the whole word only */
	progname = "SpringBoard";
	strcpy(bootargs, "-v fxct=0 serial=3");
	fuxi_telephony_init();
	assert(untouched() && lookups == 0);
	assert(arg_off("fxct=0", "fxct") && !arg_off("afxct=0", "fxct") && !arg_off("fxct=01", "fxct"));
	assert(!arg_off("fxct=1", "fxct") && arg_off("afxct=0 fxct=0", "fxct"));
	/* CommCenter's service is there: SpringBoard is left alone */
	strcpy(bootargs, "fuxisbext=1 fxinj=3");
	service_there = true;
	fuxi_telephony_init();
	assert(untouched() && lookups == 1 && deallocated == 1 && hooked == 0);
	/* no service, class not there yet: hooked from the image callback */
	service_there = false;
	class_loaded = false;
	fuxi_telephony_init();
	assert(lookups == 2 && add_image != NULL && hooked == 0 && untouched());
	class_loaded = true;
	add_image(NULL, 0);
	assert(hooked == 1 && telephony.error.imp == (IMP)connection_did_error &&
	    telephony.ecbm.imp == (IMP)in_emergency_callback_mode);
	add_image(NULL, 0);             /* idempotent */
	assert(telephony.error.imp == (IMP)connection_did_error);
	/* an error schedules nothing (the original is never called) and is said only at first */
	for (int i = 0; i < 6; i++)
		((void (*)(id, SEL, id))telephony.error.imp)(NULL, &sels[0], NULL);
	assert(original_errors == 0 && errors_said == 6);
	assert(((BOOL (*)(id, SEL))telephony.ecbm.imp)(NULL, &sels[1]) == NO);
	/* a build without one of the two: both left alone, said once */
	hooked = 0;
	telephony.error.imp = (IMP)original_error;
	telephony.ecbm.imp = (IMP)original_ecbm;
	telephony.has_ecbm = false;
	assert(try_hook() && hooked == -1 && untouched());
	assert(try_hook() && untouched());
	puts("telephony model ok");
	return 0;
}
'''


def main():
    text = SOURCE.read_text()
    assert text.startswith('/*\n * MIT License') and 'strcmp(name, "SpringBoard") != 0' in text
    # Two methods and nothing else, and only while launchd has no CommCenter service.
    assert text.count('method_setImplementation(') == 2 and 'NOT changed' in text
    assert 'bootstrap_look_up(bootstrap_port, CT_SERVICE, &port)' in text
    build = (ROOT / 'tools/build-fuxi-sys.sh').read_text()
    line = next(l for l in build.splitlines() if 'tools/fuxi-sb/FuxiSnapshots.c' in l)
    assert 'tools/fuxi-sb/FuxiTelephony.c' in line, 'libfuxi-sb.dylib is not built with FuxiTelephony.c'
    workflow = (ROOT / '.github/workflows/fuxi-sys.yml').read_text()
    assert 'tools/test-fuxi-telephony.py' in workflow
    with tempfile.TemporaryDirectory(prefix='fuxi-telephony-') as tmp:
        inc = Path(tmp) / 'inc'
        for rel, stub in STUBS.items():
            (inc / rel).parent.mkdir(parents=True, exist_ok=True)
            (inc / rel).write_text(stub)
        assert text.count('__attribute__((constructor)) static void') == 1
        source = '#include <sys/sysctl.h>\n' + text.replace('__attribute__((constructor)) static void', 'static void') + TEST
        c = Path(tmp) / 'telephony.c'
        c.write_text(source)
        exe = Path(tmp) / 'telephony'
        cc = os.environ.get('CC', 'cc')
        subprocess.run([cc, '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-cast-function-type', '-I', str(inc),
                        str(c), '-o', str(exe)], check=True)
        run = subprocess.run([str(exe)], check=True, capture_output=True, text=True)
    assert 'telephony model ok' in run.stdout, run.stdout + run.stderr
    said = run.stderr
    assert 'fxct=0, CoreTelephony errors unchanged' in said
    assert 'com.apple.commcenter.xpc is there, CoreTelephony errors unchanged' in said
    assert 'look-up of com.apple.commcenter.xpc: 1102' in said
    assert 'no com.apple.commcenter.xpc, CoreTelephony errors no longer reconnect every second' in said
    assert 'CoreTelephony error 4, no CommCenter: not reconnecting' in said and 'CoreTelephony error 5' not in said
    assert said.count('NOT changed') == 1
    print('PASS: SpringBoard CoreTelephony errors without CommCenter, fxct= (host model)')


if __name__ == '__main__':
    main()
