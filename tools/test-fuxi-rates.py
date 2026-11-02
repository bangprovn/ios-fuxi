#!/usr/bin/env python3
"""tools/fuxi-sb/FuxiRates.c on the host: which frame rates fxrate= picks, which requests stay, and that the library is built with it."""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'tools/fuxi-sb/FuxiRates.c'

STUBS = {
    'objc/runtime.h': r'''#pragma once
#include <stdbool.h>
typedef struct objc_object { int unused; } *id;
typedef struct objc_class *Class;
typedef const struct objc_selector *SEL;
typedef struct objc_method *Method;
typedef void (*IMP)(void);
#define OBJC_ASSOCIATION_ASSIGN 0
Class objc_getClass(const char *name);
Method class_getClassMethod(Class cls, SEL sel);
Method class_getInstanceMethod(Class cls, SEL sel);
Class object_getClass(id object);
bool class_respondsToSelector(Class cls, SEL sel);
const char *object_getClassName(id object);
SEL sel_registerName(const char *name);
IMP method_setImplementation(Method method, IMP imp);
id objc_getAssociatedObject(id object, const void *key);
void objc_setAssociatedObject(id object, const void *key, id value, int policy);
''',
    'objc/message.h': '#pragma once\n#include <objc/runtime.h>\nextern void (*objc_msgSend)(void);\n',
    'mach-o/dyld.h': '#pragma once\n#include <stdint.h>\nstruct mach_header;\nvoid _dyld_register_func_for_add_image(void (*)(const struct mach_header *, intptr_t));\n',
    'sys/sysctl.h': '#pragma once\n#include <stddef.h>\nint sysctlbyname(const char *, void *, size_t *, void *, size_t);\nconst char *getprogname(void);\n#define __unused __attribute__((unused))\n',
}

TEST = r'''
#include <assert.h>
int main(void)
{
	assert(rates_from("") == RATES_ALL);
	assert(rates_from("debug=0xc fuxisbext=1") == RATES_ALL);
	assert(rates_from("fxrate=0") == 0);
	assert(rates_from("a=1 fxrate=1 b=2") == RATES_LINK);
	assert(rates_from("a=1 fxrate=2") == RATES_ANIMATION);
	assert(rates_from("fxrate=3") == RATES_ALL);
	/* only the whole word, and only a digit it knows */
	assert(rates_from("afxrate=0") == RATES_ALL);
	assert(rates_from("fxrate=9") == RATES_ALL);
	assert(rates_from("fxrate=01") == RATES_ALL);
	assert(rates_from("afxrate=0 fxrate=1") == RATES_LINK);
	/* a request is replaced unless it already asks for the panel's rate */
	assert(!range_is_full((frame_rate_range){ 0.0f, 0.0f, 0.0f }));
	assert(!range_is_full((frame_rate_range){ 20.0f, 30.0f, 30.0f }));
	assert(!range_is_full((frame_rate_range){ 48.0f, 120.0f, 120.0f }));
	assert(range_is_full((frame_rate_range){ 80.0f, 120.0f, 120.0f }));
	puts("rates model ok");
	return 0;
}
'''


def main():
    text = SOURCE.read_text()
    assert 'Ported from HakiOS hakios-xnu-kernel userland/src/inject_sb.c (MIT)' in text and text.startswith('/*\n * MIT License')
    assert 'SpringBoard' in text and 'strcmp(name, "SpringBoard") != 0' in text
    # What it hooks: this display link's requests and this one method, and nothing it finds missing is forced.
    assert text.count('method_setImplementation(') == 4 and 'NOT changed' in text
    # An animation already added is read-only: the copy is what is changed.
    assert 'set_range_of, (frame_rate_range){ 80.0f, 120.0f, 120.0f }' in text and '(copy, set_range_of' in text
    build = (ROOT / 'tools/build-fuxi-sys.sh').read_text()
    assert 'tools/fuxi-sb/FuxiRates.c' in build and 'tools/fuxi-sb/FuxiSnapshots.c' in build
    with tempfile.TemporaryDirectory(prefix='fuxi-rates-') as tmp:
        inc = Path(tmp) / 'inc'
        for rel, stub in STUBS.items():
            (inc / rel).parent.mkdir(parents=True, exist_ok=True)
            (inc / rel).write_text(stub)
        source = '#include <sys/sysctl.h>\n' + text + TEST
        c = Path(tmp) / 'rates.c'
        c.write_text(source)
        exe = Path(tmp) / 'rates'
        # The Objective-C runtime is only declared: the checks are the pure logic, and the rest must compile cleanly.
        objects = Path(tmp) / 'stubs.c'
        objects.write_text(r'''
#include <stddef.h>
#include <objc/runtime.h>
#include <stdint.h>
struct mach_header;
Class objc_getClass(const char *n){(void)n;return NULL;}
Method class_getClassMethod(Class c,SEL s){(void)c;(void)s;return NULL;}
Method class_getInstanceMethod(Class c,SEL s){(void)c;(void)s;return NULL;}
Class object_getClass(id o){(void)o;return NULL;}
bool class_respondsToSelector(Class c,SEL s){(void)c;(void)s;return false;}
const char *object_getClassName(id o){(void)o;return "";}
SEL sel_registerName(const char *n){(void)n;return NULL;}
IMP method_setImplementation(Method m,IMP i){(void)m;(void)i;return NULL;}
id objc_getAssociatedObject(id o,const void *k){(void)o;(void)k;return NULL;}
void objc_setAssociatedObject(id o,const void *k,id v,int p){(void)o;(void)k;(void)v;(void)p;}
void (*objc_msgSend)(void);
void _dyld_register_func_for_add_image(void (*f)(const struct mach_header *,intptr_t)){(void)f;}
int sysctlbyname(const char *n,void *o,size_t *l,void *ne,size_t nl){(void)n;(void)o;(void)l;(void)ne;(void)nl;return -1;}
const char *getprogname(void){return "test";}
''')
        cc = os.environ.get('CC', 'cc')
        subprocess.run([cc, '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-Wno-cast-function-type', '-I', str(inc), str(c), str(objects), '-o', str(exe)], check=True)
        out = subprocess.run([str(exe)], check=True, capture_output=True, text=True).stdout
    assert 'rates model ok' in out
    print('PASS: SpringBoard frame rate choices, fxrate= (host model)')


if __name__ == '__main__':
    main()
