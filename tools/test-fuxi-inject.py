#!/usr/bin/env python3
"""xnu/fuxi-inject/fuxi_exec_inject.h on the host: which programs fxinj= gives which library, the environment string,
and that patch 0121, the libraries and fuxi-sys-root.py agree on the paths."""
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
HEADER = ROOT / 'xnu/fuxi-inject/fuxi_exec_inject.h'

TEST = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include "fuxi_exec_inject.h"

static const char *
lib(unsigned mode, const char *path)
{
	const char *slash = strrchr(path, '/');
	return fuxi_inject_library(mode, path, slash ? slash + 1 : path);
}

int main(void)
{
	char env[FUXI_INJECT_ENV_MAX];

	/* applications: the main executable of a .app, wherever it is installed */
	assert(fuxi_inject_is_app("/Applications/Preferences.app/Preferences"));
	assert(fuxi_inject_is_app("/private/var/containers/Bundle/Application/0F3A/Some App.app/Some App"));
	assert(fuxi_inject_is_app("/System/Library/CoreServices/SpringBoard.app/SpringBoard"));
	/* not: extensions, helpers deeper inside, daemons, odd paths */
	assert(!fuxi_inject_is_app("/Applications/MobileSafari.app/PlugIns/Share.appex/Share"));
	assert(!fuxi_inject_is_app("/Applications/X.app/XPCServices/y.xpc/y"));
	assert(!fuxi_inject_is_app("/Applications/X.app/Frameworks/F.framework/F"));
	assert(!fuxi_inject_is_app("/usr/libexec/runningboardd") && !fuxi_inject_is_app("/usr/libexec/xpcproxy"));
	assert(!fuxi_inject_is_app("/Applications/X.app/") && !fuxi_inject_is_app("/.app/x") && !fuxi_inject_is_app(".app/x"));
	assert(!fuxi_inject_is_app("Preferences") && !fuxi_inject_is_app("/a.apps/x") && !fuxi_inject_is_app("/a.appx/x"));

	/* fxinj bits */
	assert(lib(0, "/Applications/Preferences.app/Preferences") == NULL);
	assert(!strcmp(lib(1, "/Applications/Preferences.app/Preferences"), FUXI_INJECT_APP_LIB));
	assert(!strcmp(lib(3, "/Applications/Preferences.app/Preferences"), FUXI_INJECT_APP_LIB));
	assert(lib(2, "/Applications/Preferences.app/Preferences") == NULL);
	assert(lib(1, "/System/Library/CoreServices/SpringBoard.app/SpringBoard") == NULL);   /* has the key itself */
	assert(!strcmp(lib(2, "/System/Library/CoreServices/SpringBoard.app/SpringBoard"), FUXI_INJECT_SB_LIB));
	assert(!strcmp(lib(3, "/System/Library/CoreServices/SpringBoard.app/SpringBoard"), FUXI_INJECT_SB_LIB));
	assert(lib(3, "/usr/libexec/runningboardd") == NULL && lib(3, "/usr/libexec/backboardd") == NULL);
	assert(fuxi_inject_library(1, NULL, "x") == NULL && fuxi_inject_library(1, "/a.app/x", NULL) == NULL);

	/* the environment string */
	assert(fuxi_inject_env(FUXI_INJECT_APP_LIB, env, sizeof(env)));
	assert(!strcmp(env, "DYLD_INSERT_LIBRARIES=" FUXI_INJECT_APP_LIB));
	assert(!fuxi_inject_env(NULL, env, sizeof(env)) && !fuxi_inject_env(FUXI_INJECT_APP_LIB, env, 30));
	assert(!fuxi_inject_env(FUXI_INJECT_APP_LIB, env, 0));
	assert(strlen("DYLD_INSERT_LIBRARIES=" FUXI_INJECT_SB_LIB) < FUXI_INJECT_ENV_MAX);
	puts("inject model ok");
	return 0;
}
'''


def define(text, name):
    return re.search(r'#define %s\s+"([^"]+)"' % name, text)[1]


def main():
    header = HEADER.read_text()
    patch = (ROOT / 'xnu/patches/0121-fuxi-exec-inject.patch').read_text()
    # The hook: SWEET-only, after the program's own environment, counted like the strings before it.
    assert patch.count('#if defined(ARM64_BOARD_CONFIG_SWEET)') == 2 and 'PE_parse_boot_argn("fxinj"' in patch
    assert 'exec_add_user_string(imgp, CAST_USER_ADDR_T(inject_env), UIO_SYSSPACE, TRUE)' in patch
    assert patch.index('+\t\t\timgp->ip_envc++;') < patch.index(' \t/* Save space for envv[] NULL terminator */')
    assert '+#include "fuxi_exec_inject.h"' in patch and '+++ b/bsd/kern/fuxi_exec_inject.h' in patch
    # One path for each library everywhere.
    app = define(header, 'FUXI_INJECT_APP_LIB')
    sb = define(header, 'FUXI_INJECT_SB_LIB')
    assert define((ROOT / 'tools/fuxi-sb/FuxiAppRate.c').read_text(), 'APP_LIB') == app
    root_tool = (ROOT / 'tools/fuxi-sys-root.py').read_text()
    assert "APP_LIB = '%s'" % app in root_tool and "SB_LIB = '%s'" % sb in root_tool
    assert '-install_name %s' % app in (ROOT / 'tools/build-fuxi-sys.sh').read_text()
    with tempfile.TemporaryDirectory(prefix='fuxi-inject-') as tmp:
        c = Path(tmp) / 'inject.c'
        c.write_text(TEST)
        exe = Path(tmp) / 'inject'
        cc = os.environ.get('CC', 'cc')
        subprocess.run([cc, '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-I', str(HEADER.parent), str(c), '-o', str(exe)],
                       check=True)
        out = subprocess.run([str(exe)], check=True, capture_output=True, text=True).stdout
    assert 'inject model ok' in out
    print('PASS: fxinj programs and libraries, DYLD_INSERT_LIBRARIES string, 0121 hook shape (host model)')


if __name__ == '__main__':
    main()
