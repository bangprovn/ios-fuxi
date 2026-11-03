#!/usr/bin/env python3
"""P-SYS host tests; no device, Apple binaries, firmware or network.

Compiles the production 0107 kernel header and SWEET policy functions, the
FuxiAudio HAL plug-in and the SpringBoard snapshot library natively against
small stand-in headers, and exercises mkadt-fuxi.py's /product options and
fuxi-sys-root.py on synthetic inputs.
"""
import importlib.util
import os
from pathlib import Path
import plistlib
import re
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'tools'
sys.path.insert(0, str(TOOLS))
import adt  # noqa: E402
import mkadt  # noqa: E402

CC = ['clang', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined']


def function(text, name):
    match = re.search(r'(?:static )?(?:void|int)\n' + name + r'\([^;]+?\n\{', text)
    assert match, name
    depth, end = 1, match.end()
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[match.start():end]


def build_and_run(tmp, name, source, extra=()):
    path = Path(tmp) / (name + '.c')
    path.write_text(source)
    binary = Path(tmp) / name
    subprocess.run(CC + list(extra) + [str(path), '-o', str(binary)], check=True)
    out = subprocess.run([str(binary)], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout + out.stderr


# ---- kernel: 0107 fuxi_sys.h + the SWEET Sandbox policy functions -------------------------

KERNEL_MODEL = r'''
#include <assert.h>
#include <errno.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define KERN_SUCCESS 0
#define KERN_DENIED 53
typedef int kern_return_t;
typedef uintptr_t user_addr_t;
struct proc { int pid; };
typedef struct proc *proc_t;
typedef struct ucred { int uid; } *kauth_cred_t;
#define TUNABLE(t,n,k,v) t n = v
#define os_atomic_or_orig(p,v,m) __atomic_fetch_or((p),(v),__ATOMIC_RELAXED)
#define os_atomic_inc(p,m) __atomic_add_fetch((p),1,__ATOMIC_RELAXED)
/* user addresses are this process's own */
static int copyin(user_addr_t u, void *k, size_t n) { if (!u) return EFAULT; memcpy(k, (void *)u, n); return 0; }
static int copyout(const void *k, user_addr_t u, size_t n) { if (!u) return EFAULT; memcpy((void *)u, k, n); return 0; }
static int copyinstr(user_addr_t u, void *k, size_t n, size_t *done) {
 if (!u) return EFAULT;
 size_t l = strnlen((const char *)u, n);
 if (l == n) return ENAMETOOLONG;
 memcpy(k, (void *)u, l + 1); *done = l + 1; return 0;
}
#define PRIV_WORK_INTERVAL 1009
#define PRIV_PROC_UUID_POLICY 1001
#define PRIV_GLOBAL_PROC_INFO 1002
#define PRIV_SYSTEM_OVERRIDE 1003
#define PRIV_HW_DEBUG_DATA 1004
#define PRIV_SELECTIVE_FORCED_IDLE 1005
#define PRIV_DARKBOOT 1007
#define PRIV_AUDIO_LATENCY 1010
#define PRIV_KTRACE_BACKGROUND 1011
#define PRIV_SETPRIORITY_DARWIN_ROLE 1012
#define PRIV_PACKAGE_EXTENSIONS 1013
#define PRIV_TRIM_ACTIVE_FILE 1014
#define PRIV_PROC_CPUMON_OVERRIDE 1015
#define PRIV_VM_PRESSURE 6000
#define PRIV_VM_JETSAM 6001
#define PRIV_NET_PRIVILEGED_TRAFFIC_CLASS 10000
#define PRIV_NET_PRIVILEGED_SOCKET_DELEGATE 10001
#define PRIV_NET_INTERFACE_CONTROL 10002
#define PRIV_NET_PRIVILEGED_NETWORK_STATISTICS 10003
#define PRIV_NET_PRIVILEGED_NECP_POLICIES 10004
#define PRIV_NET_RESTRICTED_AWDL 10005
#define PRIV_NET_QOSMARKING_POLICY_OVERRIDE 10007
#define PRIV_NET_RESTRICTED_MULTIPATH_EXTENDED 10010
#define PRIV_NET_RESTRICTED_ROUTE_NC_READ 10011
#define PRIV_NET_VALIDATED_RESOLVER 10014
#define PRIV_NET_CUSTOM_PROTOCOL 10015
#define PRIV_NET_PRIVILEGED_NECP_DROP_ALL_BYPASS 10016
#define PRIV_NET_PRIVILEGED_IPSEC_WAKE_PACKET 10017
#define PRIV_NETINET_RESERVEDPORT 11000
#define PRIV_NETINET_TCP_KA_OFFLOAD 11001
#define PRIV_SKYWALK_REGISTER_USER_PIPE 12000
#define PRIV_SKYWALK_REGISTER_KERNEL_PIPE 12001
#define PRIV_SKYWALK_REGISTER_NET_IF 12002
#define PRIV_SKYWALK_REGISTER_FLOW_SWITCH 12003
#define PRIV_SKYWALK_OBSERVE_ALL 12010
#define PRIV_SKYWALK_OBSERVE_STATS 12011
#define PRIV_VFS_OPEN_BY_ID 14000
#define PRIV_VFS_MOVE_DATA_EXTENTS 14001
#define PRIV_VFS_SNAPSHOT 14002
#define PRIV_VFS_DATALESS_RESOLVER 14004
#define PRIV_VFS_SETSIZE 14006
static struct ucred self_cred, other_cred;
static struct proc self_proc = { 77 };
static const char *carried[8];
static char last_log[256];
static unsigned logs, queries;
static int container_error;
static unsigned container_calls;
static uint32_t fuxi_amfi_enabled = 1;
static int model_printf(const char *fmt, ...) {
 va_list ap; va_start(ap, fmt); vsnprintf(last_log, sizeof(last_log), fmt, ap); va_end(ap);
 logs++; return 0;
}
#define printf model_printf
static kauth_cred_t kauth_cred_get(void) { return &self_cred; }
static proc_t current_proc(void) { return &self_proc; }
static void proc_selfname(char *b, int n) { snprintf(b, n, "dasd"); }
static void proc_name(int pid, char *b, int n) { (void)pid; snprintf(b, n, "pkd"); }
static int proc_pid(proc_t p) { return p->pid; }
static kern_return_t sweet_amfi_query_boolean_with_proc(const proc_t proc, const char *name) {
 assert(proc == &self_proc); queries++;
 for (unsigned i = 0; i < 8 && carried[i]; i++) if (!strcmp(carried[i], name)) return KERN_SUCCESS;
 return KERN_DENIED;
}
static int fuxi_sandbox_container(user_addr_t arg) { (void)arg; container_calls++; return container_error; }
'''

KERNEL_TEST = r'''
#define SWEET_PRIV_LOG_MAX 64
static struct { int priv; char name[20]; } sweet_priv_logged[SWEET_PRIV_LOG_MAX];
static int sweet_priv_logged_count;
PRIV_FUNCTION
SANDBOX_FUNCTION
int main(void) {
 /* every pair is distinct and fits the log bitmap */
 assert(FUXI_SYS_PRIVS == 40);
 for (unsigned i = 0; i < FUXI_SYS_PRIVS; i++) {
  assert(fuxi_sys_priv_index(fuxi_sys_priv_entitlements[i].priv) == (int)i);
  for (unsigned j = i + 1; j < FUXI_SYS_PRIVS; j++) {
   assert(fuxi_sys_priv_entitlements[i].priv != fuxi_sys_priv_entitlements[j].priv);
   assert(strcmp(fuxi_sys_priv_entitlements[i].entitlement, fuxi_sys_priv_entitlements[j].entitlement));
  }
 }
 assert(fuxi_sys_priv_index(PRIV_WORK_INTERVAL) == -1);
 carried[0] = "vm-pressure-level";
 carried[1] = "com.apple.private.kernel.global-proc-info";
 /* default (fuxiprivs=0): exactly the previous behaviour, no entitlement is even looked at */
 assert(sweet_sandbox_priv_grant(&self_cred, PRIV_WORK_INTERVAL) == 0);
 assert(sweet_sandbox_priv_grant(&self_cred, PRIV_VM_PRESSURE) == EPERM);
 assert(queries == 0 && strstr(last_log, "dasd privilege 6000 denied"));
 assert(sweet_sandbox_priv_grant(&self_cred, PRIV_VM_PRESSURE) == EPERM && logs == 1);
 fuxi_sys_report();
 assert(logs == 1);                              /* nothing said while both are off */
 /* fuxiprivs=1 */
 fuxi_sys_privs = 1;
 fuxi_sys_report();
 assert(logs == 2 && strstr(last_log, "privileges by entitlement on (40 pairs)"));
 assert(sweet_sandbox_priv_grant(&self_cred, PRIV_VM_PRESSURE) == 0);
 assert(logs == 3 && strstr(last_log, "FUXI SYS: dasd privilege 6000 granted by vm-pressure-level"));
 assert(sweet_sandbox_priv_grant(&self_cred, PRIV_VM_PRESSURE) == 0 && logs == 3);   /* logged once */
 assert(sweet_sandbox_priv_grant(&self_cred, PRIV_GLOBAL_PROC_INFO) == 0 && logs == 4);
 /* entitlement missing: refused, and the old one-line denial log is kept */
 assert(sweet_sandbox_priv_grant(&self_cred, PRIV_DARKBOOT) == EPERM);
 assert(logs == 5 && strstr(last_log, "dasd privilege 1007 denied"));
 /* unknown privilege: no query */
 unsigned before = queries;
 assert(sweet_sandbox_priv_grant(&self_cred, 424242) == EPERM && queries == before);
 /* another credential: never answered from the current process's entitlements */
 assert(sweet_sandbox_priv_grant(&other_cred, PRIV_VM_PRESSURE) == EPERM && queries == before);
 assert(sweet_sandbox_priv_grant(&self_cred, PRIV_WORK_INTERVAL) == 0 && queries == before);
 /* Sandbox request 4: H1 result unless fuxisbpath=1 turns "no container" into success */
 container_error = ENOENT;
 assert(sweet_sandbox_syscall(&self_proc, 4, 0) == ENOENT);
 fuxi_sys_sbpath = 1;
 assert(sweet_sandbox_syscall(&self_proc, 4, 0) == 0);
 container_error = ESRCH;
 assert(sweet_sandbox_syscall(&self_proc, 4, 0) == ESRCH);
 container_error = ENAMETOOLONG;
 assert(sweet_sandbox_syscall(&self_proc, 4, 0) == ENAMETOOLONG);
 container_error = 0;
 assert(sweet_sandbox_syscall(&self_proc, 4, 0) == 0);
 /* fuxiamfi=0 keeps the unconditional ENOENT, without asking H1 */
 unsigned calls = container_calls;
 fuxi_amfi_enabled = 0;
 assert(sweet_sandbox_syscall(&self_proc, 4, 0) == ENOENT && container_calls == calls);
 fuxi_amfi_enabled = 1;
 assert(sweet_sandbox_syscall(&self_proc, 5, 0) == ENOENT && sweet_sandbox_syscall(&self_proc, 1, 0) == 0);
 /* Extensions, as libsystem_sandbox 20H392 asks: off unless fuxisbext=1, and then never touching what it was not given. */
 char token[2048];
 memset(token, 0x55, sizeof(token));
 struct fuxi_sys_extension_issue_args issue = { (user_addr_t)"com.apple.tcc.kTCCServiceMicrophone", 3, 0, 0, (user_addr_t)token, 0, 0 };
 assert(sweet_sandbox_syscall(&self_proc, 5, (user_addr_t)&issue) == ENOENT && (unsigned char)token[0] == 0x55);
 int64_t handle = -1;
 struct fuxi_sys_extension_consume_args consume = { (user_addr_t)token, sizeof(token), (user_addr_t)&handle };
 assert(sweet_sandbox_syscall(&self_proc, 6, (user_addr_t)&consume) == ENOENT && handle == -1);
 fuxi_sys_sbext = 1;
 assert(sweet_sandbox_syscall(&self_proc, 5, 0) == EFAULT);
 assert(sweet_sandbox_syscall(&self_proc, 5, (user_addr_t)&issue) == 0);
 assert(strcmp(token, "fuxi;3;com.apple.tcc.kTCCServiceMicrophone") == 0 && (unsigned char)token[strlen(token) + 1] == 0x55);
 /* a class too long for the token, and no class: an error, and nothing written */
 char long_name[200];
 memset(long_name, 'a', sizeof(long_name) - 1);
 long_name[sizeof(long_name) - 1] = 0;
 memset(token, 0x55, sizeof(token));
 issue.name = (user_addr_t)long_name;
 assert(sweet_sandbox_syscall(&self_proc, 5, (user_addr_t)&issue) == ENAMETOOLONG && (unsigned char)token[0] == 0x55);
 issue.name = 0;
 assert(sweet_sandbox_syscall(&self_proc, 5, (user_addr_t)&issue) == EFAULT && (unsigned char)token[0] == 0x55);
 assert(sweet_sandbox_syscall(&self_proc, 6, 0) == EFAULT);
 assert(sweet_sandbox_syscall(&self_proc, 6, (user_addr_t)&consume) == 0 && handle == 1);
 assert(sweet_sandbox_syscall(&self_proc, 6, (user_addr_t)&consume) == 0 && handle == 2);
 fuxi_sys_sbext = 0;
 fuxi_sys_privs = 0;
 fuxi_sys_report();
 assert(strstr(last_log, "privileges by entitlement off") && strstr(last_log, "succeeds untouched"));
 puts("kernel model ok");
 return 0;
}
'''


def test_kernel(tmp):
    subprocess.run([sys.executable, str(TOOLS / 'sync-fuxi-sys.py'), '--check'], check=True)
    sys_dir = ROOT / 'xnu/fuxi-sys'
    amfi = (sys_dir / 'sweet_amfi.c').read_text()
    assert '#include "fuxi_sys.h"' in amfi and amfi.index('#include "fuxi_amfi.h"') < amfi.index('#include "fuxi_sys.h"')
    test = KERNEL_TEST.replace('PRIV_FUNCTION', function(amfi, 'sweet_sandbox_priv_grant')) \
        .replace('SANDBOX_FUNCTION', 'static uint64_t sweet_sandbox_seen;\n' + function(amfi, 'sweet_sandbox_syscall'))
    out = build_and_run(tmp, 'kernel', KERNEL_MODEL + (sys_dir / 'fuxi_sys.h').read_text() + test)
    assert 'kernel model ok' in out
    # The default boot is unchanged: both switches default to 0.
    header = (sys_dir / 'fuxi_sys.h').read_text()
    assert 'TUNABLE(uint32_t, fuxi_sys_privs, "fuxiprivs", 0)' in header
    assert 'TUNABLE(uint32_t, fuxi_sys_sbpath, "fuxisbpath", 0)' in header
    assert 'TUNABLE(uint32_t, fuxi_sys_sbext, "fuxisbext", 0)' in header
    assert 'Ported from HackiOS hackios-xnu-kernel' in header and 'APSL' in header
    print('PASS: 0107 privilege-by-entitlement and container-path switches (model + patch sync)')


# ---- FuxiAudio.driver -------------------------------------------------------------------

CF_STUB = r'''
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#define __unused __attribute__((unused))
typedef uint8_t Boolean;
typedef uint32_t UInt32;
typedef int32_t SInt32;
typedef uint64_t UInt64;
typedef float Float32;
typedef double Float64;
typedef int32_t OSStatus;
typedef int32_t HRESULT;
typedef uint32_t ULONG;
typedef void *LPVOID;
#define S_OK 0
#define E_NOINTERFACE ((HRESULT)0x80000004)
struct __CFString { int tag; const char *s; };
typedef const struct __CFString *CFStringRef;
#define CFSTR(x) (&(const struct __CFString){ 1, x })
typedef struct { uint8_t b[16]; } CFUUIDBytes;
typedef CFUUIDBytes REFIID;
struct __CFUUID { int tag; CFUUIDBytes bytes; };
typedef const struct __CFUUID *CFUUIDRef;
typedef const void *CFTypeRef, *CFDictionaryRef, *CFAllocatorRef, *CFPropertyListRef;
#define kCFStringEncodingUTF8 0x08000100
static inline const char *CFStringGetCStringPtr(CFStringRef s, int e) { (void)e; return s->s; }
static inline Boolean CFEqual(const void *a, const void *b) {
 const int *ta = a, *tb = b;
 if (*ta != *tb) return 0;
 if (*ta == 1) return !strcmp(((CFStringRef)a)->s, ((CFStringRef)b)->s);
 return !memcmp(&((CFUUIDRef)a)->bytes, &((CFUUIDRef)b)->bytes, 16);
}
static inline CFTypeRef CFRetain(CFTypeRef v) { return v; }
static inline void CFRelease(CFTypeRef v) { (void)v; }
static inline CFUUIDRef CFUUIDCreateFromUUIDBytes(CFAllocatorRef a, CFUUIDBytes b) {
 (void)a; static struct __CFUUID pool[64]; static unsigned n; struct __CFUUID *u = &pool[n++ % 64];
 u->tag = 2; u->bytes = b; return u;
}
#define CFUUIDGetConstantUUIDWithBytes(a, ...) \
 CFUUIDCreateFromUUIDBytes(a, (CFUUIDBytes){ { __VA_ARGS__ } })
#define IUnknownUUID CFUUIDGetConstantUUIDWithBytes(NULL, 0, 0, 0, 0, 0, 0, 0, 0, 0xC0, 0, 0, 0, 0, 0, 0, 0x46)
'''

AUDIO_TYPES_STUB = r'''
#pragma once
typedef struct { Float64 mMinimum, mMaximum; } AudioValueRange;
typedef struct {
 Float64 mSampleRate; UInt32 mFormatID, mFormatFlags, mBytesPerPacket, mFramesPerPacket, mBytesPerFrame,
 mChannelsPerFrame, mBitsPerChannel, mReserved;
} AudioStreamBasicDescription;
typedef struct { Float64 mSampleTime; UInt64 mHostTime; Float64 mRateScalar; UInt64 mWordClockTime; UInt32 mFlags, mReserved; } AudioTimeStamp;
enum { kAudioFormatLinearPCM = 'lpcm', kAudioFormatFlagIsSignedInteger = 1u << 2, kAudioFormatFlagIsPacked = 1u << 3 };
'''

MACH_TIME_STUB = r'''
#pragma once
#include <stdint.h>
typedef struct { uint32_t numer, denom; } mach_timebase_info_data_t;
extern uint64_t model_now;
static inline uint64_t mach_absolute_time(void) { return model_now; }
static inline int mach_timebase_info(mach_timebase_info_data_t *t) { t->numer = 125; t->denom = 3; return 0; }
'''

AUDIO_TEST = r'''
uint64_t model_now = 1000;
static AudioServerPlugInDriverRef drv;
static UInt32 get_u32(AudioObjectID o, UInt32 sel, UInt32 scope) {
 AudioObjectPropertyAddress a = { sel, scope, 0 }; UInt32 v = 0xdeadbeef, size = 0;
 assert(interface.GetPropertyData(drv, o, 1, &a, 0, NULL, sizeof(v), &size, &v) == 0 && size == 4);
 return v;
}
static UInt32 list(AudioObjectID o, UInt32 sel, UInt32 scope, UInt32 *out, UInt32 room) {
 AudioObjectPropertyAddress a = { sel, scope, 0 }; UInt32 size = 0;
 assert(interface.HasProperty(drv, o, 1, &a));
 assert(interface.GetPropertyData(drv, o, 1, &a, 0, NULL, room * 4, &size, out) == 0);
 return size / 4;
}
int main(void) {
 void *made = FuxiAudio_Create(NULL, kAudioServerPlugInTypeUUID);
 assert(made != NULL && FuxiAudio_Create(NULL, IUnknownUUID) == NULL);
 drv = made;
 LPVOID got = NULL;
 CFUUIDBytes want = kAudioServerPlugInDriverInterfaceUUID->bytes;
 assert(interface.QueryInterface(made, want, &got) == S_OK && got == made);
 CFUUIDBytes other = { { 1, 2, 3 } };
 assert(interface.QueryInterface(made, other, &got) == E_NOINTERFACE);
 assert(interface.Initialize(drv, NULL) == 0 && ticks_per_frame > 0);
 UInt32 ids[8];
 assert(list(OBJECT_PLUGIN, kAudioPlugInPropertyDeviceList, kAudioObjectPropertyScopeGlobal, ids, 8) == 4);
 const char *uids[] = { "Speaker", "Hawking", "Actuator", "Codec" };
 for (int i = 0; i < 4; i++) {
  CFStringRef uid = &(const struct __CFString){ 1, uids[i] };
  AudioObjectPropertyAddress a = { kAudioPlugInPropertyTranslateUIDToDevice, kAudioObjectPropertyScopeGlobal, 0 };
  UInt32 dev = 0, size = 0;
  assert(interface.GetPropertyData(drv, OBJECT_PLUGIN, 1, &a, sizeof(uid), &uid, 4, &size, &dev) == 0);
  assert(dev == ids[i]);
  CFStringRef name = NULL;
  AudioObjectPropertyAddress n = { kAudioDevicePropertyDeviceUID, kAudioObjectPropertyScopeGlobal, 0 };
  assert(interface.GetPropertyData(drv, dev, 1, &n, 0, NULL, sizeof(name), &size, &name) == 0);
  assert(!strcmp(name->s, uids[i]));
  assert(get_u32(dev, kAudioDevicePropertyTransportType, kAudioObjectPropertyScopeGlobal) == 'bltn');
 }
 /* Hawking: one silent input, no output, never a default device */
 UInt32 s[4];
 assert(list(OBJECT_HAWKING, kAudioDevicePropertyStreams, kAudioObjectPropertyScopeInput, s, 4) == 1 && s[0] == OBJECT_HAWKING_INPUT);
 assert(list(OBJECT_HAWKING, kAudioDevicePropertyStreams, kAudioObjectPropertyScopeOutput, s, 4) == 0);
 assert(get_u32(OBJECT_HAWKING, kAudioDevicePropertyDeviceCanBeDefaultDevice, kAudioObjectPropertyScopeGlobal) == 0);
 assert(get_u32(OBJECT_SPEAKER, kAudioDevicePropertyDeviceCanBeDefaultDevice, kAudioObjectPropertyScopeGlobal) == 1);
 assert(get_u32(OBJECT_HAWKING_INPUT, kAudioStreamPropertyDirection, kAudioObjectPropertyScopeGlobal) == 1);
 assert(get_u32(OBJECT_HAWKING_INPUT, kAudioStreamPropertyTerminalType, kAudioObjectPropertyScopeGlobal) == 'micr');
 assert(list(OBJECT_SPEAKER, kAudioObjectPropertyOwnedObjects, kAudioObjectPropertyScopeGlobal, s, 4) == 3 && s[2] == OBJECT_SPEAKER_VOLUME);
 assert(list(OBJECT_ACTUATOR, kAudioDevicePropertyStreams, kAudioObjectPropertyScopeGlobal, s, 4) == 1 && s[0] == OBJECT_ACTUATOR_OUTPUT);
 /* Codec: the data source VirtualAudio needs */
 assert(get_u32(OBJECT_CODEC_SOURCE, kAudioSelectorControlPropertyCurrentItem, kAudioObjectPropertyScopeGlobal) == 'imic');
 UInt32 imic = 'imic', bad = 'smic';
 AudioObjectPropertyAddress cur = { kAudioSelectorControlPropertyCurrentItem, kAudioObjectPropertyScopeGlobal, 0 };
 assert(interface.SetPropertyData(drv, OBJECT_CODEC_SOURCE, 1, &cur, 0, NULL, 4, &imic) == 0);
 assert(interface.SetPropertyData(drv, OBJECT_CODEC_SOURCE, 1, &cur, 0, NULL, 4, &bad) == kAudioHardwareIllegalOperationError);
 /* formats */
 AudioStreamBasicDescription f; UInt32 size = 0;
 AudioObjectPropertyAddress fa = { kAudioStreamPropertyVirtualFormat, kAudioObjectPropertyScopeGlobal, 0 };
 assert(interface.GetPropertyData(drv, OBJECT_SPEAKER_OUTPUT, 1, &fa, 0, NULL, sizeof(f), &size, &f) == 0);
 assert(f.mSampleRate == 48000 && f.mChannelsPerFrame == 2 && f.mBitsPerChannel == 16 && f.mBytesPerFrame == 4);
 assert(interface.GetPropertyData(drv, OBJECT_CODEC_INPUT, 1, &fa, 0, NULL, sizeof(f), &size, &f) == 0 && f.mChannelsPerFrame == 1);
 assert(interface.SetPropertyData(drv, OBJECT_CODEC_INPUT, 1, &fa, 0, NULL, sizeof(f), &f) == 0);
 f.mSampleRate = 44100;
 assert(interface.SetPropertyData(drv, OBJECT_CODEC_INPUT, 1, &fa, 0, NULL, sizeof(f), &f) == kAudioDeviceUnsupportedFormatError);
 /* volume */
 Float32 v = 0.5f, back = 0;
 AudioObjectPropertyAddress va = { kAudioLevelControlPropertyScalarValue, kAudioObjectPropertyScopeGlobal, 0 };
 assert(interface.SetPropertyData(drv, OBJECT_SPEAKER_VOLUME, 1, &va, 0, NULL, 4, &v) == 0);
 assert(interface.GetPropertyData(drv, OBJECT_SPEAKER_VOLUME, 1, &va, 0, NULL, 4, &size, &back) == 0 && back == 0.5f);
 AudioObjectPropertyAddress da = { kAudioLevelControlPropertyDecibelValue, kAudioObjectPropertyScopeGlobal, 0 };
 assert(interface.GetPropertyData(drv, OBJECT_SPEAKER_VOLUME, 1, &da, 0, NULL, 4, &size, &back) == 0 && back == -24.0f);
 /* unknown property and object */
 AudioObjectPropertyAddress u = { 'zzzz', kAudioObjectPropertyScopeGlobal, 0 };
 assert(!interface.HasProperty(drv, OBJECT_HAWKING, 1, &u));
 assert(interface.GetPropertyDataSize(drv, OBJECT_HAWKING, 1, &u, 0, NULL, &size) == kAudioHardwareUnknownPropertyError);
 assert(interface.GetPropertyDataSize(drv, 99, 1, &u, 0, NULL, &size) == kAudioHardwareBadObjectError);
 /* clock: zero timestamps advance by whole periods of 16384 frames at 48 kHz */
 assert(interface.StartIO(drv, OBJECT_HAWKING, 1) == 0);
 assert(interface.StartIO(drv, OBJECT_SPEAKER, 1) == 0 && interface.StartIO(drv, OBJECT_CODEC, 1) == 0);
 Float64 st; UInt64 ht, seed;
 assert(interface.GetZeroTimeStamp(drv, OBJECT_HAWKING, 1, &st, &ht, &seed) == 0 && st == 0 && ht == 1000);
 model_now += (UInt64)(ticks_per_frame * 16384 * 2.5);
 assert(interface.GetZeroTimeStamp(drv, OBJECT_HAWKING, 1, &st, &ht, &seed) == 0 && st == 2 * 16384);
 assert(ht == 1000 + (UInt64)(2.0 * ticks_per_frame * 16384));
 /* I/O: inputs are silence, outputs are taken without waiting */
 int16_t buf[512];
 Boolean will, in_place;
 assert(interface.WillDoIOOperation(drv, OBJECT_HAWKING, 1, kAudioServerPlugInIOOperationReadInput, &will, &in_place) == 0 && will);
 memset(buf, 0x55, sizeof(buf));
 assert(interface.DoIOOperation(drv, OBJECT_HAWKING, OBJECT_HAWKING_INPUT, 1, kAudioServerPlugInIOOperationReadInput, 256, NULL, buf, NULL) == 0);
 for (int i = 0; i < 256; i++) assert(buf[i] == 0);
 assert(buf[256] == 0x5555);
 memset(buf, 0x55, sizeof(buf));
 assert(interface.DoIOOperation(drv, OBJECT_CODEC, OBJECT_CODEC_INPUT, 1, kAudioServerPlugInIOOperationReadInput, 128, NULL, buf, NULL) == 0);
 for (int i = 0; i < 128; i++) assert(buf[i] == 0);
 assert(interface.DoIOOperation(drv, OBJECT_SPEAKER, OBJECT_SPEAKER_OUTPUT, 1, kAudioServerPlugInIOOperationWriteMix, 128, NULL, buf, NULL) == 0);
 assert(written == 128);
 buf[3] = 1000;
 assert(interface.DoIOOperation(drv, OBJECT_ACTUATOR, OBJECT_ACTUATOR_OUTPUT, 1, kAudioServerPlugInIOOperationWriteMix, 64, NULL, buf, NULL) == 0);
 assert(haptic_said == 1);
 assert(interface.StopIO(drv, OBJECT_CODEC, 1) == 0 && interface.StopIO(drv, OBJECT_SPEAKER, 1) == 0);
 assert(interface.StopIO(drv, OBJECT_HAWKING, 1) == 0 && interface.StopIO(drv, OBJECT_HAWKING, 1) == 0);
 assert(interface.StartIO(drv, OBJECT_HAWKING_INPUT, 1) == kAudioHardwareBadObjectError);
 puts("audio model ok");
 return 0;
}
'''


def test_audio(tmp):
    inc = Path(tmp) / 'audio-include'
    for rel, text in (('CoreFoundation/CoreFoundation.h', CF_STUB), ('CoreFoundation/CFPlugInCOM.h', '#pragma once\n'),
                      ('CoreAudioTypes/CoreAudioTypes.h', AUDIO_TYPES_STUB), ('mach/mach_time.h', MACH_TIME_STUB)):
        (inc / rel).parent.mkdir(parents=True, exist_ok=True)
        (inc / rel).write_text(text)
    source = (TOOLS / 'fuxi-audio/FuxiAudio.c').read_text()
    assert source.startswith('/*\n * MIT License') and 'Ported from HackiOS hackios-xnu-userland userland/src/audio_plugin.c (MIT)' in source
    assert not re.search(r'(?<![A-Za-z_])(open|ioctl)\(', source), 'the plug-in must not touch device nodes'
    out = build_and_run(tmp, 'audio', '#include <assert.h>\n#include <stdio.h>\n' + source + AUDIO_TEST,
                        ['-I', str(inc), '-Wno-multichar', '-Wno-four-char-constants'])
    assert 'audio model ok' in out
    assert 'fuxi: audio: Speaker, Hawking, Actuator and Codec are with the HAL' in out
    assert 'fuxi: audio: the actuator plays: 64 frames, peak 1000' in out
    with open(TOOLS / 'fuxi-audio/Info.plist', 'rb') as f:
        info = plistlib.load(f)
    factory = info['CFPlugInTypes']['443ABAB8-E7B3-491A-B985-BEB9187030DB'][0]
    assert info['CFPlugInFactories'][factory] == 'FuxiAudio_Create' and info['CFBundleExecutable'] == 'FuxiAudio'
    print('PASS: FuxiAudio devices, Hawking input, formats, volume, clock and discard I/O (host model)')


# ---- libfuxi-sb.dylib -------------------------------------------------------------------

OBJC_STUB = r'''
#pragma once
#include <stdbool.h>
typedef signed char BOOL;
typedef struct objc_object { struct objc_class *isa; BOOL opaque; } *id;
typedef struct objc_class *Class;
typedef const struct objc_selector *SEL;
typedef struct objc_method *Method;
typedef void (*IMP)(void);
Class objc_getClass(const char *name);
Method class_getClassMethod(Class cls, SEL sel);
Method class_getInstanceMethod(Class cls, SEL sel);
Class object_getClass(id object);
SEL sel_registerName(const char *name);
IMP method_setImplementation(Method method, IMP imp);
'''

SB_TEST = r'''
#include <assert.h>
struct objc_selector { const char *name; };
struct objc_method { SEL sel; IMP imp; };
struct objc_class { const char *name; struct objc_method cmethod, imethod; bool has_cmethod, has_imethod; };
static struct objc_selector sels[4] = { { "_outputFormatForSnapshot:" }, { "isImageOpaque" } };
static struct objc_class manifest = { .name = "XBApplicationSnapshotManifestImpl", .has_cmethod = true };
static struct objc_class snapshot_cls = { .name = "XBApplicationSnapshot", .has_imethod = true };
static struct objc_class plain_cls = { .name = "NSObject" };
static bool manifest_loaded;
static char bootargs[256];
static void (*add_image)(const struct mach_header *, intptr_t);
static long original(id self, SEL sel, id snap) { (void)self; (void)sel; (void)snap; return 1; }
Class objc_getClass(const char *name) { return manifest_loaded && !strcmp(name, manifest.name) ? &manifest : NULL; }
SEL sel_registerName(const char *name) { for (int i = 0; i < 2; i++) if (!strcmp(sels[i].name, name)) return &sels[i]; assert(0); }
Method class_getClassMethod(Class c, SEL s) { return c->has_cmethod && s == &sels[0] ? &c->cmethod : NULL; }
Method class_getInstanceMethod(Class c, SEL s) { return c->has_imethod && s == &sels[1] ? &c->imethod : NULL; }
Class object_getClass(id o) { return o->isa; }
IMP method_setImplementation(Method m, IMP imp) { IMP old = m->imp; m->imp = imp; return old; }
BOOL objc_msgSend(id self, SEL sel) { assert(sel == &sels[1]); return self->opaque; }
void _dyld_register_func_for_add_image(void (*f)(const struct mach_header *, intptr_t)) { add_image = f; f(NULL, 0); }
int sysctlbyname(const char *n, void *o, size_t *len, void *ni, size_t nl) {
 (void)ni; (void)nl; assert(!strcmp(n, "kern.bootargs"));
 size_t l = strlen(bootargs) + 1; if (l > *len) l = *len; memcpy(o, bootargs, l); *len = l; return 0;
}
static const char *progname = "SpringBoard";
const char *getprogname(void) { return progname; }
static long call(id snap) {
 return ((long (*)(id, SEL, id))manifest.cmethod.imp)(NULL, &sels[0], snap);
}
int main(void) {
 manifest.cmethod.imp = (IMP)original;
 /* another program: nothing touched */
 progname = "backboardd"; manifest_loaded = true;
 fuxi_sb_init();
 assert(manifest.cmethod.imp == (IMP)original && add_image == NULL);
 /* opt-out boot-arg */
 progname = "SpringBoard";
 strcpy(bootargs, "-v fuxisbjpeg=0 serial=3");
 fuxi_sb_init();
 assert(manifest.cmethod.imp == (IMP)original);
 strcpy(bootargs, "-v xfuxisbjpeg=0 fuxisbjpeg=01");     /* neither is the switch */
 assert(!boot_arg_off("fuxisbjpeg"));
 /* SplashBoard not loaded yet: hooked from the image callback */
 manifest_loaded = false;
 fuxi_sb_init();
 assert(add_image != NULL && manifest.cmethod.imp == (IMP)original && hooked == 0);
 manifest_loaded = true;
 add_image(NULL, 0);
 assert(hooked == 1 && manifest.cmethod.imp == (IMP)snapshot_output_format && original_output_format == (IMP)original);
 add_image(NULL, 0);                                     /* idempotent */
 assert(original_output_format == (IMP)original);
 struct objc_object opaque = { &snapshot_cls, 1 }, clear = { &snapshot_cls, 0 }, odd = { &plain_cls, 1 };
 assert(call(&opaque) == 3 && call(&clear) == 0 && call(&odd) == 0 && call(NULL) == 0);
 /* a build without the selector: left alone, said once */
 hooked = 0; manifest.has_cmethod = false;
 assert(try_hook() && hooked == -1);
 puts("sb model ok");
 return 0;
}
'''


def test_sb(tmp):
    inc = Path(tmp) / 'sb-include'
    stubs = {
        'objc/runtime.h': OBJC_STUB,
        'objc/message.h': '#pragma once\n#include <objc/runtime.h>\nBOOL objc_msgSend(id self, SEL sel);\n',
        'mach-o/dyld.h': '#pragma once\n#include <stdint.h>\nstruct mach_header;\n'
                         'void _dyld_register_func_for_add_image(void (*)(const struct mach_header *, intptr_t));\n',
        'sys/sysctl.h': '#pragma once\n#include <stddef.h>\nint sysctlbyname(const char *, void *, size_t *, void *, size_t);\n'
                        'const char *getprogname(void);\n#define __unused __attribute__((unused))\n',
    }
    for rel, text in stubs.items():
        (inc / rel).parent.mkdir(parents=True, exist_ok=True)
        (inc / rel).write_text(text)
    source = (TOOLS / 'fuxi-sb/FuxiSnapshots.c').read_text()
    assert 'Ported from HackiOS hackios-xnu-userland userland/src/inject_sb.c (MIT)' in source
    # The library's sysctl.h must come first so __unused is defined before use.
    source = source.replace('#include <mach-o/dyld.h>', '#include <sys/sysctl.h>\n#include <mach-o/dyld.h>', 1)
    # The model calls the constructor itself, once per scenario.
    assert source.count('__attribute__((constructor)) static void') == 1
    source = source.replace('__attribute__((constructor)) static void', 'static void')
    out = build_and_run(tmp, 'sb', source + SB_TEST, ['-I', str(inc)])
    assert 'sb model ok' in out
    assert 'fuxi: SpringBoard: application snapshots as JPEG (opaque) or PNG, not ATX' in out
    assert 'fuxi: SpringBoard: fuxisbjpeg=0, application snapshots unchanged' in out
    assert 'fuxi: SpringBoard: application snapshot as JPEG' in out and 'NOT changed' in out
    print('PASS: SpringBoard snapshot format hook, late SplashBoard, opt-out boot-arg (host model)')


# ---- mkadt-fuxi.py /product options -----------------------------------------------------

def synthetic_d22ap(path):
    def node(name, props, children=()):
        return mkadt.node(name, props, children)
    haptics = node('haptics', [('AAPL,phandle', struct.pack('<I', 0xd9)), ('closed-loop', struct.pack('<I', 1)),
                               ('supports-3rd-party-haptics', struct.pack('<I', 1))])
    haptics.props.append(('syscfg-placeholder', 1, b'syscfg/XXXX'))
    camera = node('camera', [('AAPL,phandle', struct.pack('<I', 0xd6)), ('flash', struct.pack('<I', 1))])
    product = node('product', [('AAPL,phandle', struct.pack('<I', 5)), ('product-name', b'iPhone X\0')],
                   [camera, haptics])
    root = node('device-tree', [('AAPL,phandle', struct.pack('<I', 1))], [product])
    path.write_bytes(adt.serialize(root))


def test_adt(tmp):
    d22ap = Path(tmp) / 'd22ap.adt'
    synthetic_d22ap(d22ap)
    spec = importlib.util.spec_from_file_location('fuxi_adt', TOOLS / 'mkadt-fuxi.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    saved = mkadt.d22ap_product.__defaults__
    mkadt.d22ap_product.__defaults__ = (str(d22ap),)
    try:
        plain = m.build()
        product = next(c for c in plain.children if c.name == 'product')
        assert product.children == [] and product.prop('product-name') == b'iPhone X\0'
        before = adt.serialize(plain)
        assert adt.serialize(m.add_product_children(plain, False, False, d22ap)) == before   # default unchanged
        root = m.add_product_children(m.build(), True, True, d22ap)
        root, _ = adt.parse(adt.serialize(root))
        product = next(c for c in root.children if c.name == 'product')
        kids = {c.name: c for c in product.children}
        assert sorted(kids) == ['camera', 'haptics']
        assert kids['haptics'].prop('closed-loop') == struct.pack('<I', 1)
        assert kids['haptics'].prop('supports-3rd-party-haptics') == struct.pack('<I', 1)
        assert kids['haptics'].prop('syscfg-placeholder') is None
        assert [k for k, _, _ in kids['camera'].props] == ['name', 'AAPL,phandle']   # empty: no flash
        phandles = []

        def walk(n):
            value = n.prop('AAPL,phandle')
            if value:
                phandles.append(struct.unpack('<I', value)[0])
            for c in n.children:
                walk(c)
        walk(root)
        assert len(phandles) == len(set(phandles))
        # A template run (existing ADT) replaces rather than duplicates.
        again = m.add_product_children(root, True, False, d22ap)
        assert [c.name for c in next(c for c in again.children if c.name == 'product').children].count('haptics') == 1
        try:
            m.add_product_children(m.build(), True, False, Path(tmp) / 'missing.adt')
            raise AssertionError('missing D22AP tree accepted')
        except OSError:
            pass
        # disp0 display options: absent by default, replaced (not duplicated) on rebuild.
        plain = m.build(); before = adt.serialize(plain)
        assert adt.serialize(m.set_display_options(plain)) == before
        root = m.set_display_options(m.set_display_options(m.build(), 120000, False), 120800, True)
        root, _ = adt.parse(adt.serialize(root))
        disp0 = next(c for c in next(c for c in root.children if c.name == 'arm-io').children if c.name == 'disp0')
        assert disp0.prop('fuxi,refresh-mhz') == struct.pack('<I', 120800) and disp0.prop('fuxi,swap-hist') == struct.pack('<I', 1)
        assert [k for k, _, _ in disp0.props].count('fuxi,refresh-mhz') == 1 and disp0.prop('canvas-width') == struct.pack('<I', 1125)
        for bad in (60000, 240001):
            try:
                m.set_display_options(m.build(), bad)
                raise AssertionError('bad refresh accepted')
            except ValueError:
                pass
        mkadt.d22ap_product.__defaults__ = (str(Path(tmp) / 'absent.adt'),)
        try:
            m.add_product_children(m.build(), False, True, d22ap)
            raise AssertionError('ADT without /product accepted')
        except ValueError:
            pass
    finally:
        mkadt.d22ap_product.__defaults__ = saved
    print('PASS: mkadt-fuxi /product/haptics, /product/camera and disp0 display options (off by default)')


# ---- fuxi-sys-root.py -------------------------------------------------------------------

def synthetic_root(base):
    jobs = {
        '/System/Library/LaunchDaemons/com.apple.SpringBoard.plist': {
            'Label': 'com.apple.SpringBoard', 'EnvironmentVariables': {'CA_ASSERT_MAIN_THREAD_TRANSACTIONS': '1'}},
        '/System/Library/LaunchDaemons/com.apple.nanotimekitcompaniond.plist': {'Label': 'com.apple.nanotimekitcompaniond'},
        '/System/Library/LaunchDaemons/com.apple.homed.plist': {'Label': 'com.apple.homed'},
        '/System/Library/LaunchDaemons/com.apple.dmd.plist': {'Label': 'com.apple.dmd', 'Disabled': True},
        '/System/Library/LaunchDaemons/com.apple.mediaserverd.plist': {'Label': 'com.apple.mediaserverd'},
        '/System/Library/LaunchDaemons/com.apple.runningboardd.plist': {'Label': 'com.apple.runningboardd'},
    }
    cache = base / 'System/Library/xpc/launchd.plist'
    cache.parent.mkdir(parents=True)
    cache.write_bytes(plistlib.dumps({'LaunchDaemons': jobs, 'VersionNumber': 1}, fmt=plistlib.FMT_BINARY))
    va = base / 'Library/Audio/Plug-Ins/HAL.disabled/VirtualAudio.plugin'
    va.mkdir(parents=True)
    (va / 'VirtualAudio').write_bytes(b'x')
    (base / 'usr/local').mkdir(parents=True)
    bundle = base.parent / 'FuxiAudio.driver'
    bundle.mkdir()
    (bundle / 'FuxiAudio').write_bytes(b'\xcf\xfa\xed\xfe')
    os.chmod(bundle / 'FuxiAudio', 0o755)
    (bundle / '_CodeSignature').mkdir()
    (bundle / '_CodeSignature/CodeResources').write_bytes(b'<plist/>')
    with open(TOOLS / 'fuxi-audio/Info.plist', 'rb') as f:
        (bundle / 'Info.plist').write_bytes(f.read())
    dylib = base.parent / 'libfuxi-sb.dylib'
    dylib.write_bytes(b'\xcf\xfa\xed\xfe')
    return cache, bundle, dylib


def run_root(*args):
    return subprocess.run([sys.executable, str(TOOLS / 'fuxi-sys-root.py'), *map(str, args)],
                          check=True, capture_output=True, text=True).stdout


def load(path):
    with open(path, 'rb') as f:
        return plistlib.load(f)


def test_root(tmp):
    base = Path(tmp) / 'root' / 'mnt'
    base.mkdir(parents=True)
    cache, bundle, dylib = synthetic_root(base)
    original = cache.read_bytes()
    # dry run writes nothing
    out = run_root('--dry-run', '--audio-plugin', bundle, '--restore-virtualaudio', '--sb-snapshots', dylib,
                   '--quiet-jobs', 'observed,hackios', base)
    assert 'would: install FuxiAudio.driver' in out and 'would: com.apple.homed: Disabled=true' in out
    assert cache.read_bytes() == original and not (base / 'System/Library/Audio').exists()
    assert not (base / 'usr/local/lib').exists()
    # apply
    out = run_root('--audio-plugin', bundle, '--restore-virtualaudio', '--sb-snapshots', dylib,
                   '--quiet-jobs', 'observed,hackios', base)
    driver = base / 'System/Library/Audio/Plug-Ins/HAL/FuxiAudio.driver'
    assert (driver / 'FuxiAudio').stat().st_mode & 0o777 == 0o755
    assert (driver / 'Info.plist').stat().st_mode & 0o777 == 0o644
    assert (driver / '_CodeSignature/CodeResources').is_file()
    assert (base / 'Library/Audio/Plug-Ins/HAL/VirtualAudio.plugin/VirtualAudio').is_file()
    assert (base / 'usr/local/lib/fuxi/libfuxi-sb.dylib').stat().st_mode & 0o777 == 0o755
    jobs = {j['Label']: j for j in load(cache)['LaunchDaemons'].values()}
    env = jobs['com.apple.SpringBoard']['EnvironmentVariables']
    assert env == {'CA_ASSERT_MAIN_THREAD_TRANSACTIONS': '1', 'DYLD_INSERT_LIBRARIES': '/usr/local/lib/fuxi/libfuxi-sb.dylib'}
    assert jobs['com.apple.nanotimekitcompaniond']['Disabled'] and jobs['com.apple.nanotimekitcompaniond']['_FuxiSysDisabled']
    assert jobs['com.apple.homed']['_FuxiSysDisabled'] and '_FuxiSysDisabled' not in jobs['com.apple.dmd']
    assert 'Disabled' not in jobs['com.apple.mediaserverd']
    assert 'com.apple.routined: no such job, skipped' in out and 'com.apple.dmd: already disabled, left alone' in out
    # idempotent
    snapshot = cache.read_bytes()
    out = run_root('--sb-snapshots', dylib, '--quiet-jobs', 'observed', '--restore-virtualaudio', base)
    assert 'already has' in out and 'already disabled by this tool' in out and 'already moved' in out
    assert cache.read_bytes() == snapshot
    # another library already inserted is kept on undo
    data = load(cache)
    data['LaunchDaemons']['/System/Library/LaunchDaemons/com.apple.SpringBoard.plist']['EnvironmentVariables'][
        'DYLD_INSERT_LIBRARIES'] = '/other.dylib:/usr/local/lib/fuxi/libfuxi-sb.dylib'
    cache.write_bytes(plistlib.dumps(data, fmt=plistlib.FMT_BINARY))
    run_root('--no-sb-snapshots', '--unquiet-jobs', '--remove-audio-plugin', '--disable-virtualaudio', base)
    jobs = {j['Label']: j for j in load(cache)['LaunchDaemons'].values()}
    assert jobs['com.apple.SpringBoard']['EnvironmentVariables']['DYLD_INSERT_LIBRARIES'] == '/other.dylib'
    assert 'Disabled' not in jobs['com.apple.nanotimekitcompaniond'] and 'Disabled' not in jobs['com.apple.homed']
    assert jobs['com.apple.dmd']['Disabled'] is True
    assert not any('_FuxiSysDisabled' in j for j in jobs.values())
    assert not driver.exists() and not (base / 'usr/local/lib/fuxi/libfuxi-sb.dylib').exists()
    assert (base / 'Library/Audio/Plug-Ins/HAL.disabled/VirtualAudio.plugin/VirtualAudio').is_file()
    # a full undo of an untouched job list restores the original job contents
    run_root('--no-sb-snapshots', base)
    jobs = {j['Label']: j for j in load(cache)['LaunchDaemons'].values()}
    assert 'DYLD_INSERT_LIBRARIES' in jobs['com.apple.SpringBoard']['EnvironmentVariables']   # /other.dylib stays
    bad = subprocess.run([sys.executable, str(TOOLS / 'fuxi-sys-root.py'), '--quiet-jobs', 'nope', str(base)],
                         capture_output=True, text=True)
    assert bad.returncode != 0 and 'unknown preset' in bad.stderr
    # libfuxi-app.dylib: only the file (the kernel inserts it, fxinj=1); a v26 entry in runningboardd's job goes
    app = base.parent / 'libfuxi-app.dylib'
    app.write_bytes(b'\xcf\xfa\xed\xfe')
    lib = base / 'usr/local/lib/fuxi/libfuxi-app.dylib'
    before = cache.read_bytes()
    out = run_root('--dry-run', '--app-rate', app, base)
    assert 'would: install libfuxi-app.dylib -> /usr/local/lib/fuxi/libfuxi-app.dylib' in out
    assert cache.read_bytes() == before and not lib.exists()
    run_root('--app-rate', app, base)
    assert lib.stat().st_mode & 0o777 == 0o755 and cache.read_bytes() == before
    data = load(cache)
    data['LaunchDaemons']['/System/Library/LaunchDaemons/com.apple.runningboardd.plist']['EnvironmentVariables'] = {
        'DYLD_INSERT_LIBRARIES': '/usr/local/lib/fuxi/libfuxi-app.dylib'}
    cache.write_bytes(plistlib.dumps(data, fmt=plistlib.FMT_BINARY))
    out = run_root('--app-rate', app, base)
    assert 'runningboardd: /usr/local/lib/fuxi/libfuxi-app.dylib taken out of DYLD_INSERT_LIBRARIES' in out
    assert load(cache) == plistlib.loads(before) and lib.is_file()
    run_root('--no-app-rate', base)
    assert load(cache) == plistlib.loads(before) and not lib.exists()
    data = load(cache)
    del data['LaunchDaemons']['/System/Library/LaunchDaemons/com.apple.runningboardd.plist']
    cache.write_bytes(plistlib.dumps(data, fmt=plistlib.FMT_BINARY))
    run_root('--app-rate', app, base)                   # no runningboardd job: the file alone
    assert lib.is_file()
    print('PASS: fuxi-sys-root.py install/undo, dry run, idempotence, job presets, application library (synthetic root)')


def main():
    with tempfile.TemporaryDirectory(prefix='fuxi-sys-test-') as tmp:
        test_kernel(tmp)
        test_audio(tmp)
        test_sb(tmp)
        test_adt(tmp)
        test_root(tmp)


if __name__ == '__main__':
    main()
