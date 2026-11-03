#!/usr/bin/env python3
"""tools/fuxi-sb/FuxiAppRate.c on the host: the key an application's info dictionary gets, the library leaving
DYLD_INSERT_LIBRARIES, and that it is built and installed with the path the kernel inserts (patch 0121)."""
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
    'os/log.h': '#pragma once\n#define OS_LOG_DEFAULT ((void *)0)\n#define os_log(log, format, ...) ((void)(log), (void)(format))\n',
}

TEST = r'''
#include <assert.h>

/* CoreFoundation: a main bundle whose info dictionary holds one key */
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
static const char *progname = "Preferences";
const char *getprogname(void) { return progname; }

int main(void)
{
	char out[LIST_MAX];

	/* DYLD_INSERT_LIBRARIES lists */
	assert(list_without(APP_LIB, out, sizeof(out)) && !strcmp(out, ""));
	assert(list_without("/a.dylib:" APP_LIB ":/b.dylib", out, sizeof(out)) && !strcmp(out, "/a.dylib:/b.dylib"));
	assert(list_without("::" APP_LIB "::/b.dylib", out, sizeof(out)) && !strcmp(out, "/b.dylib"));
	assert(!list_without("/a.dylib:" APP_LIB ".old", out, sizeof(out)) && !list_without("", out, sizeof(out)));
	assert(!list_without("/a.dylib:/b.dylib:" APP_LIB, out, 5));

	/* an application: the key is set, and the library leaves the environment */
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
	progname = NULL;
	fuxi_app_init();
	puts("apprate model ok");
	return 0;
}
'''


def main():
    text = SOURCE.read_text()
    assert text.startswith('/*\n * MIT License')
    assert '#define APP_KEY         "CADisableMinimumFrameDurationOnPhone"' in text
    assert '#define APP_LIB         "' + LIB + '"' in text
    # QuartzCore reads the Info.plist dictionary, so that is the one changed; nothing is hooked, nothing else runs.
    assert 'CFBundleGetInfoDictionary(bundle)' in text and 'CFBundleGetLocalInfoDictionary' not in text
    assert 'method_setImplementation' not in text and 'objc/' not in text and 'xpc' not in text
    build = (ROOT / 'tools/build-fuxi-sys.sh').read_text()
    assert 'tools/fuxi-sb/FuxiAppRate.c' in build and '-install_name ' + LIB in build and 'libfuxi-app.dylib' in build
    assert '"' + LIB + '"' in (ROOT / 'xnu/fuxi-inject/fuxi_exec_inject.h').read_text()
    with tempfile.TemporaryDirectory(prefix='fuxi-apprate-') as tmp:
        inc = Path(tmp) / 'inc'
        for rel, stub in STUBS.items():
            (inc / rel).parent.mkdir(parents=True, exist_ok=True)
            (inc / rel).write_text(stub)
        # The model calls the constructor itself, once per scenario.
        assert text.count('__attribute__((constructor)) static void') == 1
        source = re.sub(r'__attribute__\(\(constructor\)\) static void', 'static void', text)
        c = Path(tmp) / 'apprate.c'
        c.write_text('const char *getprogname(void);\n' + source + TEST)
        exe = Path(tmp) / 'apprate'
        cc = os.environ.get('CC', 'cc')
        subprocess.run([cc, '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-I', str(inc), str(c), '-o', str(exe)], check=True)
        run = subprocess.run([str(exe)], check=True, capture_output=True, text=True)
    out = run.stdout + run.stderr
    assert 'apprate model ok' in out, out
    for line in ('fuxi: Preferences: CADisableMinimumFrameDurationOnPhone = YES, frames up to the panel\'s rate',
                 'fuxi: Preferences: CADisableMinimumFrameDurationOnPhone was YES already',
                 'fuxi: Preferences: no Info.plist; frame rate NOT changed',
                 'fuxi: ?: no Info.plist; frame rate NOT changed'):
        assert line in out, line
    print('PASS: applications\' CADisableMinimumFrameDurationOnPhone, the library leaving the environment (host model)')


if __name__ == '__main__':
    main()
