#!/usr/bin/env python3
"""Generate/check the two patches that carry the ath12k port into the kernel tree.

0099-fuxi-wlan.patch holds xnu/fuxi-wlan/fuxi_wlan.c as a new-file hunk: this rewrites only that hunk.
0117-fuxi-ath12k.patch adds bsd/dev/fuxi_ath12k_os.c and the two boundary headers, compiles the first, turns
FUXI_ATH12K on for fuxi_wlan.c and links $(FUXI_OBJECTS). Only kernel-metal4wifi applies 0117
(tools/ci/prepare-kernel.py --ath12k), which is what keeps every other kernel's inputs unchanged.
"""
import argparse
import difflib
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
WLAN_C = ROOT / 'xnu/fuxi-wlan/fuxi_wlan.c'
PATCH_0099 = ROOT / 'xnu/patches/0099-fuxi-wlan.patch'
PATCH_0117 = ROOT / 'xnu/patches/0117-fuxi-ath12k.patch'
NEW_FILES = (('xnu/fuxi-ath12k/os/fuxi_ath12k_os.c', 'bsd/dev/fuxi_ath12k_os.c'),
             ('xnu/fuxi-ath12k/os/fuxi_ath12k_ios.c', 'bsd/dev/fuxi_ath12k_ios.c'),
             ('xnu/fuxi-ath12k/os/fuxi_aks_wrap.h', 'iokit/Kernel/arm/fuxi_aks_wrap.h'),
             ('xnu/fuxi-ath12k/os/fuxi_remote.h', 'bsd/dev/fuxi_remote.h'),
             ('xnu/fuxi-ath12k/src/fa_xnu.h', 'bsd/dev/fa_xnu.h'),
             ('xnu/fuxi-ath12k/compat/include/lx_os.h', 'bsd/dev/fa_lx_os.h'))
CONF = '''--- a/bsd/conf/files
+++ b/bsd/conf/files
@@ -115,6 +115,8 @@
 bsd/dev/fuxi_perf.c			standard
 bsd/dev/fuxi_keys.c			standard
 bsd/dev/fuxi_wlan.c			standard
+bsd/dev/fuxi_ath12k_os.c		standard
+bsd/dev/fuxi_ath12k_ios.c		standard
 bsd/dev/fuxi_wifi_ios.c			standard
 bsd/dev/fuxi_wifi_net.c			standard
 bsd/dev/fuxi_wifi_sta.c			standard
'''
MAKEINC = '''--- a/makedefs/MakeInc.def
+++ b/makedefs/MakeInc.def
@@ -880,6 +880,11 @@
 #
 LD_KERNEL_LIBS	   = -lcc_kext
 LD_KERNEL_ARCHIVES = $(LDFLAGS_KERNEL_SDK) -lfirehose_kernel
+# Patch 0117, kernel-metal4wifi only: tools/ci/build-ath12k.py made this one object. Without it the link
+# fails at fuxi_wlan.c's fa_ calls, which is the right answer for a build that lost it.
+LD_KERNEL_ARCHIVES += $(FUXI_OBJECTS)
+MACHINE_FLAGS_ARM64_FUXI += -DFUXI_ATH12K=1
+
 # Link opensource binary library
 ifneq ($(filter T6000 T8101 VMAPPLE T6000 T8101 VMAPPLE,$(CURRENT_MACHINE_CONFIG)),)
 	LDFLAGS_KERNEL_ONLY += -rdynamic -Wl,-force_load,$(KDKROOT)/System/Library/KernelSupport/lib$(CURRENT_MACHINE_CONFIG).os.$(CURRENT_KERNEL_CONFIG).a
'''


# fuxiwifiios=2, as a change to the file 0104 adds: without 0117 the file and every other kernel's inputs stay as they are.
IOS_SOURCE = ROOT / 'xnu/fuxi-wifi-ios/fuxi_wifi_ios.c'
IOS_FIND = """	PE_parse_boot_argn("fuxiwifiios", &on, sizeof(on));
	if (on != 1 || enabled) {
"""
IOS_REPLACE = """	PE_parse_boot_argn("fuxiwifiios", &on, sizeof(on));
	if (on == 2 && !enabled) {
		extern const struct fuxi_wifi_backend *fuxi_ath12k_ios_backend(void);

		enabled = true;
		link.name = "ath12k";
		printf("FUXI WIFI IOS: enabled, ath12k backend; Apple80211 16.7 ABI unverified\\n");
		(void)fuxi_wifi_ios_attach(fuxi_ath12k_ios_backend());
		return;
	}
	if (on != 1 || enabled) {
"""


def ios_selection():
    before = IOS_SOURCE.read_text()
    assert before.count(IOS_FIND) == 1
    after = before.replace(IOS_FIND, IOS_REPLACE)
    return ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        fromfile='a/bsd/dev/fuxi_wifi_ios.c', tofile='b/bsd/dev/fuxi_wifi_ios.c'))


# The black screen of w-ios1: who holds the registry busy while configd's namer waits. Logs only, in the file 0104 adds.
STACK_SOURCE = ROOT / 'xnu/fuxi-wifi-ios/FuxiNetworkStack.cpp'
STACK_HELPER_FIND = """class IONetworkStackUserClient : public IOUserClient
"""
STACK_HELPER = """/*
 * Patch 0117, kernel-metal4wifi only. The theory of w-ios1's five minutes of black screen: configd's namer waits for the
 * registry to go quiet. This says who keeps it busy, at the open and later, and when the root gets there. It only looks.
 */
#include <IOKit/IORegistryEntry.h>
#include <kern/clock.h>
#include <kern/thread_call.h>
#include <pexpert/pexpert.h>
#include <libkern/OSAtomic.h>

#define FUXI_BUSY_LINES_MOST    40
#define FUXI_BUSY_SECONDS_MOST  300

static thread_call_t fuxi_busy_call;
static volatile UInt32 fuxi_busy_started;
static unsigned fuxi_busy_seconds;
static bool fuxi_busy_quiet;

/*
 * w-ios2: the root stayed at 1 for all of the 300 s watched, with no service under it busy, so what holds it is nothing
 * anyone can finish, and configd's InterfaceNamer waits five minutes for a quiet that does not come. With nothing busy
 * under it 30 s after the open, that count is taken away once. fuxiwifiquiet=0 leaves it, to compare.
 */
static void
fuxi_busy_give_up(unsigned busy_under)
{
	IOService * root = IOService::getServiceRoot();
	int allowed = 1;

	if (PE_parse_boot_argn("fuxiwifiquiet", &allowed, sizeof(allowed)) && allowed == 0) {
		IOLog("FUXI ATH12K: M8 busy: left as it is, fuxiwifiquiet=0\\n");
		return;
	}
	if (busy_under != 0 || root->getBusyState() != 1) {
		IOLog("FUXI ATH12K: M8 busy: nothing given up: %u busy under the root, its state %u\\n", busy_under, (unsigned) root->getBusyState());
		return;
	}
	root->adjustBusy(-1);
	IOLog("FUXI ATH12K: M8 busy: the root's stuck count given up, state now %u\\n", (unsigned) root->getBusyState());
}

static unsigned
fuxi_busy_round(unsigned seconds)
{
	IOService * root = IOService::getServiceRoot();
	IORegistryIterator * walk;
	IORegistryEntry * entry;
	unsigned said = 0, more = 0;

	IOLog("FUXI ATH12K: M8 busy: %u s after the open, the root's state is %u\\n", seconds, (unsigned) root->getBusyState());
	walk = IORegistryIterator::iterateOver(root, gIOServicePlane, kIORegistryIterateRecursively);
	if (walk == NULL) {
		IOLog("FUXI ATH12K: M8 busy: no walk\\n");
		return (1);
	}
	while ((entry = walk->getNextObject()) != NULL) {
		IOService * service = OSDynamicCast(IOService, entry);

		if (service == NULL || service->getBusyState() == 0) {
			continue;
		}
		if (said < FUXI_BUSY_LINES_MOST) {
			IOLog("FUXI ATH12K: M8 busy: %s \\"%s\\" %u\\n", entry->getMetaClass()->getClassName(), entry->getName(), (unsigned) service->getBusyState());
			said++;
		} else {
			more++;
		}
	}
	if (!walk->isValid()) {
		IOLog("FUXI ATH12K: M8 busy: the registry changed, this walk is cut\\n");
	}
	if (more != 0) {
		IOLog("FUXI ATH12K: M8 busy: %u more\\n", more);
	}
	if (!walk->isValid()) {
		said++;				/* (what is not seen is not known to be idle) */
	}
	walk->release();
	return (said + more);
}

static void
fuxi_busy_tick(thread_call_param_t, thread_call_param_t)
{
	unsigned seconds = fuxi_busy_seconds++;

	if (!fuxi_busy_quiet && IOService::getServiceRoot()->getBusyState() == 0) {
		fuxi_busy_quiet = true;
		IOLog("FUXI ATH12K: M8 busy: the root reached 0, %u s after the open\\n", seconds);
	}
	if (seconds == 0 || seconds == 30 || seconds == 60 || seconds == 120 || seconds == FUXI_BUSY_SECONDS_MOST) {
		unsigned busy_under = fuxi_busy_round(seconds);

		if (seconds == 30) {
			fuxi_busy_give_up(busy_under);
		}
	}
	if (seconds < FUXI_BUSY_SECONDS_MOST) {
		uint64_t deadline;

		clock_interval_to_deadline(1, kSecondScale, &deadline);
		thread_call_enter_delayed(fuxi_busy_call, deadline);
	}
}

static void
fuxi_busy_watch(void)
{
	if (!OSCompareAndSwap(0, 1, &fuxi_busy_started)) {
		return;
	}
	fuxi_busy_call = thread_call_allocate(fuxi_busy_tick, NULL);
	if (fuxi_busy_call == NULL) {
		IOLog("FUXI ATH12K: M8 busy: no thread call\\n");
		return;
	}
	thread_call_enter(fuxi_busy_call);
}

"""
STACK_START_FIND = """	    (unsigned) IOService::getServiceRoot()->getBusyState());
	return true;
"""
STACK_START = """	    (unsigned) IOService::getServiceRoot()->getBusyState());
	fuxi_busy_watch();
	return true;
"""


APPLE80211_SOURCE = ROOT / 'xnu/fuxi-wifi-ios/fuxi_wifi_apple80211.c'
APPLE80211_FIND = """	note(request, is_set, error);
"""
APPLE80211_NOTE = """	note(request, is_set, error);
	if (is_set) {
		extern void fuxi_ath12k_note_request(int number, int value);

		fuxi_ath12k_note_request(request->req_type, request->req_val);
	}
"""


def apple80211_note():
    before = APPLE80211_SOURCE.read_text()
    assert before.count(APPLE80211_FIND) == 1
    after = before.replace(APPLE80211_FIND, APPLE80211_NOTE)
    return ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        fromfile='a/bsd/dev/fuxi_wifi_apple80211.c', tofile='b/bsd/dev/fuxi_wifi_apple80211.c'))


def stack_watch():
    before = STACK_SOURCE.read_text()
    assert before.count(STACK_HELPER_FIND) == 1 and before.count(STACK_START_FIND) == 1
    after = before.replace(STACK_HELPER_FIND, STACK_HELPER + STACK_HELPER_FIND).replace(STACK_START_FIND, STACK_START)
    return ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        fromfile='a/iokit/Kernel/arm/FuxiNetworkStack.cpp', tofile='b/iokit/Kernel/arm/FuxiNetworkStack.cpp'))


# The keychain's writes, as a change to what patches 0038, 0043 and 0055 leave: other kernels' inputs stay as they are.
KEYSTORE = 'iokit/Kernel/arm/SweetKeyStore.cpp'
KEYSTORE_INCLUDE_FIND = """#include <sys/proc.h>
"""
KEYSTORE_INCLUDE = """#include <sys/proc.h>
#include "fuxi_aks_wrap.h"
"""
KEYSTORE_HELPER_FIND = """IOReturn
AppleKeyStoreUserClient::externalMethod(uint32_t selector, IOExternalMethodArguments *args,
"""
KEYSTORE_HELPER = """/*
 * Selectors 10 and 11: scalars in are the bag's handle and the key's class, the key comes in as a structure. No hardware
 * protects it: see fuxi_aks_wrap.h. Sizes are checked here and logged by the caller; the key itself never is.
 */
static IOReturn
sweet_aks_crypt(IOExternalMethodArguments *args, bool wrap)
{
	uint8_t in[FUXI_AKS_WRAPPED_MOST], out[FUXI_AKS_WRAPPED_MOST];
	uint32_t in_size = args->structureInputDescriptor != NULL ?
	    (uint32_t)args->structureInputDescriptor->getLength() : args->structureInputSize;
	uint32_t room = sweet_aks_struct_out_size(args);
	uint32_t key_class, length;
	IOReturn ret;

	if (args->scalarInputCount < 2 || in_size == 0 || in_size > sizeof(in)) {
		return kIOReturnBadArgument;
	}
	if (args->structureInputDescriptor != NULL) {
		if (args->structureInputDescriptor->readBytes(0, in, in_size) != in_size) {
			return kIOReturnBadArgument;
		}
	} else {
		if (args->structureInput == NULL) {
			return kIOReturnBadArgument;
		}
		memcpy(in, args->structureInput, in_size);
	}
	key_class = (uint32_t)args->scalarInput[1];
	room = room < sizeof(out) ? room : (uint32_t)sizeof(out);
	length = wrap ? fuxi_aks_wrap(in, in_size, key_class, out, room) : fuxi_aks_unwrap(in, in_size, key_class, out, room);
	if (length == 0) {
		ret = kIOReturnBadArgument;
	} else {
		ret = sweet_aks_struct_out(args, out, length);
	}
	if (wrap && ret == kIOReturnSuccess && args->scalarOutputCount >= 1) {
		args->scalarOutput[0] = key_class;
		args->scalarOutputCount = 1;
	} else {
		args->scalarOutputCount = 0;
	}
	bzero(in, sizeof(in));
	bzero(out, sizeof(out));
	return ret;
}

"""
KEYSTORE_CASE_FIND = """	case kAKSGetDeviceState:
	case kAKSGetExtDeviceState:
		args->scalarOutputCount = 0;
		ret = sweet_aks_device_state(args);
		break;
"""
KEYSTORE_CASE = """	case kAKSWrapKey:
		ret = sweet_aks_crypt(args, true);
		break;
	case kAKSUnwrapKey:
		ret = sweet_aks_crypt(args, false);
		break;
""" + KEYSTORE_CASE_FIND


def keystore_base():
    """SweetKeyStore.cpp as patches 0038, 0043 and 0055 leave it."""
    lines = None
    for name in ('0038-sweet-applekeystore.patch', '0043-sweet-console-crash-log.patch', '0055-sweet-aks-ext-state-sampler-depth.patch'):
        patch = (PATCH_0117.parent / name).read_text(errors='surrogateescape').splitlines(True)
        at = next(i for i, line in enumerate(patch) if line.rstrip('\n') == '+++ b/' + KEYSTORE) + 1
        hunks = []
        while at < len(patch) and patch[at].startswith('@@'):
            at += 1
            body = []
            while at < len(patch) and patch[at][:1] in (' ', '+', '-') and not patch[at].startswith(('--- ', '+++ ')):
                body.append(patch[at])
                at += 1
            hunks.append(body)
        if lines is None:
            assert len(hunks) == 1
            lines = [line[1:] for line in hunks[0]]
            continue
        cursor = 0
        for body in hunks:
            old = [line[1:] for line in body if line[0] in ' -']
            new = [line[1:] for line in body if line[0] in ' +']
            found = next(i for i in range(cursor, len(lines) - len(old) + 1) if lines[i:i + len(old)] == old)
            lines[found:found + len(old)] = new
            cursor = found + len(new)
    return ''.join(lines)


def keystore():
    before = keystore_base()
    for find in (KEYSTORE_INCLUDE_FIND, KEYSTORE_HELPER_FIND, KEYSTORE_CASE_FIND):
        assert before.count(find) == 1, find
    after = before.replace(KEYSTORE_INCLUDE_FIND, KEYSTORE_INCLUDE).replace(KEYSTORE_HELPER_FIND, KEYSTORE_HELPER + KEYSTORE_HELPER_FIND).replace(KEYSTORE_CASE_FIND, KEYSTORE_CASE)
    return ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile='a/' + KEYSTORE, tofile='b/' + KEYSTORE))


# fuxiremote=1, as a change to the file 0092 adds: boot/phone.py's taps. Without 0117 the file and every other kernel's inputs stay as they are.
KEYS_SOURCE = ROOT / 'xnu/fuxi-h1/fuxi_keys.c'
KEYS_INCLUDE_FIND = """#include "fuxi_key_state.h"
"""
KEYS_INCLUDE = """#include "fuxi_key_state.h"
#include "fuxi_remote.h"
"""
KEYS_HELPER_FIND = """void
fuxi_keys_init(void)
{
"""
KEYS_HELPER = """/*
 * fuxiremote=1: touches and keys from the PC, one at a time, for looking at iOS with nobody there (boot/phone.py). The
 * monitor writes the mailbox with its MMU off: see fuxi_remote.h for who cleans what. Without the argument the mailbox
 * stays zero, so the monitor finds no magic and refuses, and this thread does not exist. No text comes this way.
 */
extern struct fuxi_remote_box fuxi_remote_box;
struct fuxi_remote_box fuxi_remote_box __attribute__((aligned(64)));

static void
fk_remote_clean(void)
{
	__asm__ volatile("dc civac, %0; dsb sy" :: "r"(&fuxi_remote_box) : "memory");
}

static void
fk_remote_act(const struct fuxi_remote_cmd *c)
{
	switch (c->kind) {
	case FUXI_REMOTE_TAP:
		printf("FUXI REMOTE: tap %d %d seq %u\\n", c->x0, c->y0, c->seq);
		fk_tap(c->x0, c->y0);
		break;
	case FUXI_REMOTE_FLICK:
		printf("FUXI REMOTE: flick %d %d %d %d %u ms seq %u\\n", c->x0, c->y0, c->x1, c->y1, c->ms, c->seq);
		fk_flick(c->x0, c->y0, c->x1, c->y1, c->ms);
		break;
	default: {
		struct fuxi_key_inject key = {(unsigned)c->x0, FUXI_KEY_INJECT_CLICK, c->ms};

		printf("FUXI REMOTE: key %d %u ms seq %u\\n", c->x0, c->ms, c->seq);
		(void)fk_inject(&key);
		break;
	}
	}
}

static void
fk_remote(void *arg, wait_result_t wr)
{
	(void)arg;
	(void)wr;
	fuxi_remote_box.version = FUXI_REMOTE_VERSION;
	fuxi_remote_box.magic = FUXI_REMOTE_MAGIC;
	fk_remote_clean();
	printf("FUXI REMOTE: on\\n");
	for (;;) {
		volatile struct fuxi_remote_cmd *seen = &fuxi_remote_box.cmd;
		struct fuxi_remote_cmd cmd;

		__asm__ volatile("dc civac, %0; dsb sy" :: "r"(seen) : "memory");
		cmd.seq = seen->seq;
		if (cmd.seq != fuxi_remote_box.done) {
			cmd.kind = seen->kind;
			cmd.x0 = seen->x0;
			cmd.y0 = seen->y0;
			cmd.x1 = seen->x1;
			cmd.y1 = seen->y1;
			cmd.ms = seen->ms;
			cmd.check = seen->check;
			if (fuxi_remote_valid(&cmd)) {
				fk_remote_act(&cmd);
			} else {
				fuxi_remote_box.refused++;
				printf("FUXI REMOTE: refused seq %u\\n", cmd.seq);
			}
			fuxi_remote_box.done = cmd.seq;
			fk_remote_clean();
		}
		fk_script_sleep(20);
	}
}

"""
KEYS_START_FIND = """	unsigned human = 0;
"""
KEYS_START = """	unsigned remote = 0;
	if (PE_parse_boot_argn("fuxiremote", &remote, sizeof(remote)) && remote == 1) {
		if (kernel_thread_start(fk_remote, NULL, &thread) == KERN_SUCCESS) {
			thread_deallocate(thread);
		} else {
			printf("FUXI REMOTE: thread_start failed\\n");
		}
	}
	unsigned human = 0;
"""


def remote_keys():
    before = KEYS_SOURCE.read_text()
    assert before.count(KEYS_INCLUDE_FIND) == 1 and before.count(KEYS_HELPER_FIND) == 1 and before.count(KEYS_START_FIND) == 1
    after = before.replace(KEYS_INCLUDE_FIND, KEYS_INCLUDE).replace(KEYS_HELPER_FIND, KEYS_HELPER + KEYS_HELPER_FIND).replace(KEYS_START_FIND, KEYS_START)
    return ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        fromfile='a/bsd/dev/fuxi_keys.c', tofile='b/bsd/dev/fuxi_keys.c'))


def new_file(source, dest):
    return ''.join(difflib.unified_diff([], (ROOT / source).read_text().splitlines(True),
                                        fromfile='/dev/null', tofile='b/' + dest))


def expected_0117():
    return CONF + MAKEINC + ''.join(new_file(*pair) for pair in NEW_FILES) + remote_keys() + apple80211_note() + ios_selection() + stack_watch() + keystore()


def expected_0099():
    patch = PATCH_0099.read_text()
    head, rest = patch.split('+++ b/bsd/dev/fuxi_wlan.c\n', 1)
    rest = rest.split('\n--- a/', 1)[1]
    lines = WLAN_C.read_text().splitlines()
    hunk = f'@@ -0,0 +1,{len(lines)} @@\n' + ''.join('+' + line + '\n' for line in lines)
    return head + '+++ b/bsd/dev/fuxi_wlan.c\n' + hunk + '--- a/' + rest


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()
    wanted = {PATCH_0099: expected_0099(), PATCH_0117: expected_0117()}
    for prior in sorted(PATCH_0117.parent.glob('*.patch')):
        if prior != PATCH_0117:
            for _, dest in NEW_FILES:
                assert not re.search(r'^\+\+\+ b/' + re.escape(dest) + '$', prior.read_text(errors='surrogateescape'), re.M), prior.name
    for path, text in wanted.items():
        if args.check:
            assert path.exists() and path.read_text() == text, f'{path.name} differs; run tools/sync-fuxi-ath12k.py'
        else:
            path.write_bytes(text.encode())
    print('PASS: 0099 carries fuxi_wlan.c exactly; 0117 carries the ath12k OS file and link hook')


if __name__ == '__main__':
    main()
