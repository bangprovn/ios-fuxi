#!/usr/bin/env python3
"""tools/fuxi-sb/FuxiNoGL.c on the host: CoreImage's OpenGL ES fallback in an application returns NULL (a nil
CIContext) when EAGLContext cannot make a GL context, runs as before when it can, and libfuxi-app.dylib is built
with it."""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'tools/fuxi-sb/FuxiNoGL.c'

STUBS = {
    'objc/runtime.h': r'''#pragma once
#include <stdbool.h>
typedef bool BOOL;
typedef struct objc_object { int unused; } *id;
typedef struct objc_class *Class;
typedef const struct objc_selector *SEL;
typedef struct objc_method *Method;
typedef void (*IMP)(void);
Class objc_getClass(const char *name);
Method class_getClassMethod(Class cls, SEL sel);
SEL sel_registerName(const char *name);
IMP method_setImplementation(Method method, IMP imp);
BOOL class_respondsToSelector(Class cls, SEL sel);
Class object_getClass(id obj);
''',
    # objc_msgSend as the casts call it, (receiver, selector[, one integer]), through a pointer like libobjc's
    'objc/message.h': '#pragma once\n#include <objc/runtime.h>\n'
                      'extern void (*model_msgsend)(void);\n#define objc_msgSend model_msgsend\n',
    'mach-o/dyld.h': '#pragma once\n#include <stdint.h>\nstruct mach_header;\n'
                     'void _dyld_register_func_for_add_image(void (*)(const struct mach_header *, intptr_t));\n',
    'os/log.h': '#pragma once\n#define OS_LOG_DEFAULT ((void *)0)\n#define os_log(log, format, ...) ((void)(log), (void)(format))\n',
}

TEST = r'''
#include <assert.h>
struct objc_selector { const char *name; };
struct objc_method { SEL sel; IMP imp; };
struct objc_class { const char *name; struct objc_method gl; bool has_gl, responds; };
enum { S_GL, S_ALLOC, S_INIT, S_MACRO, S_RELEASE, S_COUNT };
static struct objc_selector sels[S_COUNT] = {
	{ "internalGLContextWithOptions:" }, { "alloc" }, { "initWithAPI:" }, { "getMacroContextPrivate" }, { "release" },
};
static struct objc_class ci = { .name = "CIContext", .has_gl = true }, eagl = { .name = "EAGLContext", .responds = true };
static struct objc_object fresh, instance;
static bool ci_loaded, eagl_loaded, macro_ok;
static unsigned api_ok;                 /* bit n: initWithAPI:n gives a context */
static int allocs, inits, macros, releases, originals;
static void (*add_image)(const struct mach_header *, intptr_t);
const char *getprogname(void) { return "Preferences"; }
static void *original(id self, SEL selector, id options)
{
	(void)self; (void)options; assert(selector == &sels[S_GL]); originals++; return (void *)0x1234;
}
Class objc_getClass(const char *name)
{
	if (ci_loaded && !strcmp(name, ci.name))
		return &ci;
	return eagl_loaded && !strcmp(name, eagl.name) ? &eagl : NULL;
}
SEL sel_registerName(const char *name)
{
	for (int i = 0; i < S_COUNT; i++)
		if (!strcmp(sels[i].name, name))
			return &sels[i];
	assert(0);
	return NULL;
}
Method class_getClassMethod(Class c, SEL s) { assert(c == &ci && s == &sels[S_GL]); return c->has_gl ? &c->gl : NULL; }
IMP method_setImplementation(Method m, IMP imp) { IMP old = m->imp; m->imp = imp; return old; }
BOOL class_respondsToSelector(Class c, SEL s) { assert(c == &eagl && s == &sels[S_MACRO]); return c->responds; }
Class object_getClass(id o) { assert(o == &instance); return &eagl; }
void _dyld_register_func_for_add_image(void (*f)(const struct mach_header *, intptr_t)) { add_image = f; f(NULL, 0); }
void *model_send(id self, SEL s, unsigned long arg)
{
	if (s == &sels[S_ALLOC]) {
		assert(self == (id)&eagl); allocs++; return &fresh;
	}
	if (s == &sels[S_INIT]) {
		assert(self == &fresh && arg == 3 - (unsigned long)inits);      /* API 3 first, then 2, as CoreImage */
		inits++; return (api_ok >> arg) & 1 ? &instance : NULL;
	}
	if (s == &sels[S_MACRO]) {
		assert(self == &instance); macros++; return macro_ok ? (void *)0x99 : NULL;
	}
	assert(s == &sels[S_RELEASE] && self == &instance);
	releases++;
	return NULL;
}
void (*model_msgsend)(void) = (void (*)(void))model_send;
static void *call_gl(void) { return ((void *(*)(id, SEL, id))ci.gl.imp)((id)&ci, &sels[S_GL], NULL); }
static void again(void)
{
	atomic_store(&gl_state, GL_UNKNOWN);
	atomic_store(&nil_said, false);
	allocs = inits = macros = releases = originals = 0;
}
int main(void)
{
	ci.gl.imp = (IMP)original;
	/* CoreImage not loaded at the constructor: replaced from the image callback once it is, once */
	fuxi_nogl_init();
	assert(add_image != NULL && hooked == 0 && ci.gl.imp == (IMP)original);
	ci_loaded = true;
	add_image(NULL, 0);
	assert(hooked == 1 && ci.gl.imp == (IMP)gl_context && original_gl_context == original);
	add_image(NULL, 0);
	assert(ci.gl.imp == (IMP)gl_context && original_gl_context == original);
	/* this phone: EAGLContext gives nothing for API 3 or 2 -> NULL every time, asked once, said once */
	eagl_loaded = true;
	for (int i = 0; i < 3; i++)
		assert(call_gl() == NULL);
	assert(originals == 0 && allocs == 2 && inits == 2 && macros == 0 && releases == 0);
	/* an EAGLContext with no GL context behind it: NULL too, and the probe is released */
	again();
	api_ok = 1u << 2;
	assert(call_gl() == NULL && originals == 0 && allocs == 2 && inits == 2 && macros == 1 && releases == 1);
	/* no EAGLContext class at all: NULL */
	again();
	eagl_loaded = false;
	assert(call_gl() == NULL && allocs == 0 && originals == 0);
	/* OpenGL ES works (API 3 with a GL context): the original every time, asked once */
	again();
	eagl_loaded = true;
	api_ok = 1u << 3;
	macro_ok = true;
	assert(call_gl() == (void *)0x1234 && call_gl() == (void *)0x1234);
	assert(originals == 2 && allocs == 1 && inits == 1 && macros == 1 && releases == 1);
	/* an EAGLContext without CoreImage's test: CoreImage left as it is */
	again();
	eagl.responds = false;
	macro_ok = false;
	assert(call_gl() == (void *)0x1234 && macros == 0 && releases == 1);
	/* a CoreImage without the method: nothing replaced, said once */
	hooked = 0;
	ci.gl.imp = (IMP)original;
	ci.has_gl = false;
	assert(try_hook() && hooked == -1 && ci.gl.imp == (IMP)original);
	assert(try_hook() && ci.gl.imp == (IMP)original);
	puts("nogl model ok");
	return 0;
}
'''


def main():
    text = SOURCE.read_text()
    assert text.startswith('/*\n * MIT License')
    # One class method replaced, the OpenGL ES fallback, and nothing else: not -initWithOptions:, not the Metal path,
    # not an application's own EAGLContext.
    assert '#define NOGL_SELECTOR   "internalGLContextWithOptions:"' in text and '#define NOGL_CLASS      "CIContext"' in text
    assert text.count('method_setImplementation(') == 1 and 'class_getClassMethod(ci, sel_registerName(NOGL_SELECTOR))' in text
    assert 'NOT changed' in text and 'getMacroContextPrivate' in text
    build = (ROOT / 'tools/build-fuxi-sys.sh').read_text()
    line = next(l for l in build.splitlines() if 'tools/fuxi-sb/FuxiAppRate.c' in l)
    assert 'tools/fuxi-sb/FuxiNoGL.c' in line and '-lobjc' in line, 'libfuxi-app.dylib is not built with FuxiNoGL.c'
    workflow = (ROOT / '.github/workflows/fuxi-sys.yml').read_text()
    assert 'tools/test-fuxi-nogl.py' in workflow
    with tempfile.TemporaryDirectory(prefix='fuxi-nogl-') as tmp:
        inc = Path(tmp) / 'inc'
        for rel, stub in STUBS.items():
            (inc / rel).parent.mkdir(parents=True, exist_ok=True)
            (inc / rel).write_text(stub)
        # The model calls the constructor itself.
        assert text.count('__attribute__((constructor)) static void') == 1
        source = ('const char *getprogname(void);\n' +
                  text.replace('__attribute__((constructor)) static void', 'static void') + TEST)
        c = Path(tmp) / 'nogl.c'
        c.write_text(source)
        exe = Path(tmp) / 'nogl'
        cc = os.environ.get('CC', 'cc')
        subprocess.run([cc, '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-cast-function-type', '-I', str(inc),
                        str(c), '-o', str(exe)], check=True)
        run = subprocess.run([str(exe)], check=True, capture_output=True, text=True)
    assert 'nogl model ok' in run.stdout, run.stdout + run.stderr
    said = run.stderr
    # once per process (the model starts the process again three times with no OpenGL ES)
    assert said.count('fuxi: Preferences: CIContext asked for OpenGL ES, none on this phone: nil context, not a crash\n') == 3
    assert said.count("fuxi: Preferences: OpenGL ES works here; CoreImage's GL contexts left as they are\n") == 2
    assert said.count("fuxi: Preferences: +[CIContext internalGLContextWithOptions:] not found; "
                      "CoreImage's OpenGL ES fallback NOT changed\n") == 1
    print('PASS: CoreImage without OpenGL ES in applications, nil CIContext instead of a NULL call (host model)')


if __name__ == '__main__':
    main()
