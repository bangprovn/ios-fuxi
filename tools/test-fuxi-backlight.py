#!/usr/bin/env python3
"""Backlight probe: patch sync, gates, parser and logical power interests."""
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'xnu/fuxi-backlight'
source = (SRC / 'FuxiBacklight.cpp').read_text()
fb = (ROOT / 'xnu/fuxi-unixnu/SweetFramebuffer.cpp').read_text()


def function(name, code=source):
    start = code.index(name + '(')
    # Use the definition, not a class declaration.
    start = code.index('{', start)
    depth, end = 1, start + 1
    while depth:
        depth += (code[end] == '{') - (code[end] == '}')
        end += 1
    return code[start:end]


subprocess.run(['python3', str(ROOT / 'tools/sync-fuxi-backlight.py'), '--check'], check=True)
subprocess.run(['python3', str(ROOT / 'tools/sync-fuxi-unixnu.py'), '--check'], check=True)
assert source.startswith('/*') and '#if defined(FUXI)' in source
gate = function('fuxi_backlight_publish')
assert gate.index('PE_parse_boot_argn("fuxibacklight"') < gate.index('if (!fbl_enabled(present, enable)) return;')
assert gate.index('return;') < gate.index('new FuxiBacklight') < gate.index('service->attach(provider)')
start = function('FuxiBacklight::start')
assert start.index('if (!fbl_enabled(present, enable)) return false;') < start.index('IOService::start(provider)')
assert start.index('setProperty("backlight-control", kOSBooleanTrue)') < start.index('registerService();')
assert start.index('publishBrightness(fRequested)') < start.index('registerService();')
assert fb.index('fuxi_backlight_publish(this);') < fb.index('\n\tregisterService();')
assert 'fBacklightPowerNotifications = backlight == 2 || backlight == 3;' in fb
assert 'runAction(&AppleCLCD::powerChangeAction, this, argument)' in fb
power = function('AppleCLCD::powerChangeAction', fb)
assert power.index('IOLockUnlock(fb->fLock);') < power.index('fuxi_backlight_display_state(fb, !off)')
# fuxipaneldcs=4: selector 12 reaches the scanout side before the framebuffer lock and before fPowerOff changes.
assert power.index('if (fb->fScanout) fuxi_gpu_panel_power(!off);') < power.index('IOLockLock(fb->fLock);') < power.index('fb->fPowerOff = off;')
# The brightness hand-off happens after the registry update, outside the service lock, and only for a valid request.
props = function('FuxiBacklight::setProperties')
assert props.index('if (!valid) return kIOReturnBadArgument;') < props.index('IOLockUnlock(fLock);') < props.index('if (ok && cap) fuxi_gpu_panel_dbv(dbv);')
notify = function('fuxi_backlight_display_state')
assert notify.index('if (!fbl_power_notifications(present, enable)) return;') < notify.index('getClientIterator()')
assert notify.index('fbl_withhold_on(present, enable, on)') < notify.index('getClientIterator()')
assert 'OFF-only interest experiment enabled; ON withheld' in start
assert 'on ? kIOMessageDeviceHasPoweredOn : kIOMessageDeviceWillPowerOff' in notify
assert 'service->messageClients(message)' in notify
# CoreBrightness creates its internal display module (and with it the libEDR server QuartzCore waits on once
# backlight-control matches) only for a service conforming to AppleARMBacklight: the chain must carry that name.
assert 'class AppleARMBacklight : public IODisplay' in source
assert 'OSDefineMetaClassAndStructors(AppleARMBacklight, IODisplay);' in source
assert 'class FuxiBacklight : public AppleARMBacklight' in source
assert 'OSDefineMetaClassAndStructors(FuxiBacklight, AppleARMBacklight);' in source
assert start.index('setProperty("backlight-control", kOSBooleanTrue)') < start.index('conforms=AppleARMBacklight')
for key in ('min', 'max', 'value'):
    assert f'brightness->setObject("{key}"' in source
for signature in ('getProperty(const OSSymbol *key) const',
                  'setProperty(const OSSymbol *key, OSObject *value)',
                  'serializeProperties(OSSerialize *s) const',
                  'setProperties(OSObject *properties)', 'newUserClient(task_t task',
                  'externalMethod(uint32_t selector'):
    assert signature in source, signature
assert 'dictionaryWithProperties()' in function('FuxiBacklight::serializeProperties')
assert 'fbl_startup_trace(present, enable, tracePresent, trace)' in start
assert 'startup trace enabled mode=1 read_limit=512 interests=none' in start
assert 'args->scalarInput[i]' in function('FuxiBacklightUserClient::externalMethod')
assert 'return kIOReturnUnsupported;' in function('FuxiBacklightUserClient::externalMethod')
assert 'fRequested = (uint32_t)requested;' in source
assert 'IOLockLock(fLock);' in source and 'IOLockUnlock(fLock);' in source
# Hardware-free translation unit; logical notifications never drive scanout.
code = re.sub(r'/\*.*?\*/|//[^\n]*', '', source, flags=re.S)
for forbidden in ('ml_io_map', 'IOMemoryDescriptor', 'IOMemoryMap', 'writeRegister',
                  'fuxi_display_kick', 'sweet_fb_display_off', 'setPowerState',
                  'volatile uint', '__asm__', '0x51'):
    assert forbidden not in code, forbidden
ci = (ROOT / 'tools/ci/build.sh').read_text()
assert ci.index('python3 tools/test-fuxi-backlight.py') < ci.index('if [[ $target == kernel-wlan6g')

# Compile the real setProperties body with a small dictionary/number adapter.
# Covers the public flat input, nested diagnostic inputs, malformed data,
# exact endpoints, failed publication, immutable-snapshot update ordering.
MODEL = r'''
#include <cassert>
#include <cstdint>
#include <map>
#include <string>
#include <vector>
#include <functional>
#include <cstdarg>
#include <cstdio>
#define FUXI 1
#include "fuxi_backlight_logic.h"
struct OSObject { virtual ~OSObject() = default; };
struct OSNumber : OSObject {
    uint64_t value;
    explicit OSNumber(uint64_t v) : value(v) {}
    uint64_t unsigned64BitValue() { return value; }
};
struct OSDictionary : OSObject {
    std::map<std::string, OSObject *> values;
    unsigned releases = 0;
    void release() { ++releases; }
    OSObject *getObject(const char *key) {
        auto p = values.find(key); return p == values.end() ? nullptr : p->second;
    }
};
struct OSSymbol {
    std::string value;
    explicit OSSymbol(const char *v) : value(v) {}
    const char *getCStringNoCopy() const { return value.c_str(); }
};
struct OSSerialize {};
#define OSSafeReleaseNULL(p) do { if (p) (p)->release(); (p) = nullptr; } while (0)
#define OSDynamicCast(t, p) dynamic_cast<t *>(p)
enum { kIOReturnSuccess, kIOReturnBadArgument, kIOReturnUnsupported, kIOReturnNoMemory };
enum { BL_READ, BL_SET, BL_PROPERTIES, BL_OPEN, BL_METHOD, BL_BRIGHTNESS, BL_POWER, BL_COUNT };
using IOReturn = int;
using UInt32 = uint32_t;
constexpr UInt32 kIOMessageDeviceHasPoweredOn = 0xe0000230;
constexpr UInt32 kIOMessageDeviceWillPowerOff = 0xe0000210;
using SInt32 = int32_t;
static SInt32 OSIncrementAtomic(volatile SInt32 *n) { return ++*n; }
static void bl_value(const char *, uint32_t, const char *, OSObject *) {}
static std::vector<std::string> logs;
static void IOLog(const char *format, ...) {
    char buffer[512];
    va_list args;
    va_start(args, format);
    vsnprintf(buffer, sizeof(buffer), format, args);
    va_end(args);
    logs.emplace_back(buffer);
}
static uint64_t trace_clock;
static uint64_t mach_absolute_time() { return trace_clock += 3000; }
static void absolutetime_to_nanoseconds(uint64_t time, uint64_t *ns) { *ns = time; }
static void proc_selfname(char *name, unsigned size) {
    assert(size == 33); snprintf(name, size, "backboardd");
}
static unsigned locked;
static void IOLockLock(void *) { assert(!locked); ++locked; }
static void IOLockUnlock(void *) { assert(locked == 1); --locked; }
static bool boot_present;
static int boot_value;
static bool PE_parse_boot_argn(const char *name, void *value, unsigned size) {
    assert(std::string(name) == "fuxibacklight" && size == sizeof(int));
    if (boot_present) *static_cast<int *>(value) = boot_value;
    return boot_present;
}
struct OSIterator {
    std::vector<OSObject *> children;
    unsigned next = 0, releases = 0;
    OSObject *getNextObject() { return next < children.size() ? children[next++] : nullptr; }
    void release() { ++releases; }
};
struct IOService : OSObject {
    mutable unsigned registryReads = 0, serializations = 0;
    unsigned registryWrites = 0;
    bool failRegistry = false;
    std::map<std::string, OSObject *> registry;
    mutable OSDictionary snapshot;
    OSObject *getProperty(const OSSymbol *key) const {
        ++registryReads;
        auto p = registry.find(key->value);
        return p == registry.end() ? nullptr : p->second;
    }
    bool setProperty(const OSSymbol *key, OSObject *value) {
        ++registryWrites;
        if (failRegistry) return false;
        registry[key->value] = value; return true;
    }
    OSDictionary *dictionaryWithProperties() const { return &snapshot; }
    bool serializeProperties(OSSerialize *) const { ++serializations; return !failRegistry; }
    OSIterator iterator;
    unsigned traversals = 0;
    bool failIterator = false;
    std::vector<UInt32> messages;
    std::function<void(UInt32)> observer;
    OSIterator *getClientIterator() {
        ++traversals; iterator.next = 0;
        return failIterator ? nullptr : &iterator;
    }
    IOReturn messageClients(UInt32 message) {
        assert(!locked);
        messages.push_back(message);
        if (observer) observer(message);
        return kIOReturnSuccess;
    }
};
struct FuxiBacklight : IOService {
    void *fLock = nullptr;
    uint32_t fRequested = FUXI_BACKLIGHT_MAX, published = FUXI_BACKLIGHT_MAX;
    bool fail = false;
    bool fStartupTrace = false;
    OSObject *getProperty(const OSSymbol *) const;
    bool setProperty(const OSSymbol *, OSObject *);
    bool serializeProperties(OSSerialize *) const;
    bool publishBrightness(uint32_t value) {
        assert(locked == 1);
        if (fail) return false;
        published = value; return true;
    }
    IOReturn setProperties(OSObject *properties);
};
static int sweet_fb_display_off;
// fuxipaneldcs scanout side: the DBV cap (0 = off) and the hand-offs it received; panel power calls must not hold the fb lock.
static uint32_t dcs_max;
static std::vector<unsigned> dcs_sent;
static std::vector<int> panel_power;
static unsigned fuxi_gpu_panel_dcs_max() { return dcs_max; }
static void fuxi_gpu_panel_dbv(unsigned dbv) { assert(!locked); dcs_sent.push_back(dbv); }
static void fuxi_gpu_panel_power(int on) { assert(!locked); panel_power.push_back(on); }
struct IOWorkLoop {
    unsigned calls = 0, depth = 0;
    using Action = IOReturn (*)(OSObject *, void *, void *, void *, void *);
    IOReturn runAction(Action action, OSObject *owner, void *state) {
        ++calls; ++depth;
        IOReturn result = action(owner, state, nullptr, nullptr, nullptr);
        --depth;
        return result;
    }
};
struct AppleCLCD : IOService {
    void *fLock = nullptr;
    IOWorkLoop workloop;
    IOWorkLoop *fWorkLoop = &workloop;
    bool fPowerOff = false, fDirty = false, fBacklightPowerNotifications = false, fScanout = false;
    unsigned fFadeStep = 7, wakes = 0;
    void wakePresentLocked() { assert(locked == 1); ++wakes; }
    IOReturn requestPowerChange(uint64_t);
    static IOReturn powerChangeAction(OSObject *, void *, void *, void *, void *);
};
'''
MODEL += '\nstatic volatile SInt32 bl_events[BL_COUNT];\n'
MODEL += '\nstatic bool bl_log(unsigned kind, uint32_t *event, bool trace = false)\n' + function('bl_log')
MODEL += '\nstatic void bl_stamp(const char *op, uint32_t event, uint64_t started)\n' + function('bl_stamp')
MODEL += '\nOSObject *FuxiBacklight::getProperty(const OSSymbol *key) const\n' + function('FuxiBacklight::getProperty')
MODEL += '\nbool FuxiBacklight::setProperty(const OSSymbol *key, OSObject *value)\n' + function('FuxiBacklight::setProperty')
MODEL += '\nbool FuxiBacklight::serializeProperties(OSSerialize *s) const\n' + function('FuxiBacklight::serializeProperties')
MODEL += '\nIOReturn FuxiBacklight::setProperties(OSObject *properties)\n' + function('FuxiBacklight::setProperties')
MODEL += '\nvoid fuxi_backlight_display_state(IOService *provider, bool on)\n' + notify
MODEL += '\nIOReturn AppleCLCD::powerChangeAction(OSObject *owner, void *state, void *, void *, void *)\n' + power
MODEL += '\nIOReturn AppleCLCD::requestPowerChange(uint64_t state)\n' + function('AppleCLCD::requestPowerChange', fb)
MODEL += r'''
int main() {
    for (int value : {-1, 0, 1, 2, 3, 4, 65536}) {
        assert(!fbl_enabled(0, value));
        assert(fbl_enabled(1, value) == (value >= 1 && value <= 3));
        assert(!fbl_power_notifications(0, value));
        assert(fbl_power_notifications(1, value) == (value == 2 || value == 3));
        for (bool on : {false, true}) {
            assert(!fbl_withhold_on(0, value, on));
            assert(fbl_withhold_on(1, value, on) == (value == 3 && on));
        }
    }
    for (uint32_t n = 0; n <= 4096; ++n)
        assert(bool(fbl_log_this(n)) == (n && (n <= 64 || n % 256 == 0)));
    assert(!fbl_log_this(UINT32_MAX));
    for (int mode : {-1, 0, 1, 2, 3, 4})
        for (bool present : {false, true})
            for (bool tracePresent : {false, true})
                for (int trace : {-1, 0, 1, 2})
                    assert(bool(fbl_startup_trace(present, mode, tracePresent, trace)) ==
                           (present && mode == 1 && tracePresent && trace == 1));
    for (uint32_t n = 0; n <= 4096; ++n) {
        assert(fbl_read_log_this(n, false) == fbl_log_this(n));
        assert(bool(fbl_read_log_this(n, true)) == (n && (n <= 512 || n % 256 == 0)));
    }
    assert(!fbl_read_log_this(UINT32_MAX, true));

    // Run the production registry overrides and trace clock/formatting. Trace
    // sampling must never alter the value, error, snapshot lifetime or fanout.
    FuxiBacklight probe;
    OSSymbol key("IODisplayParameters"), interest("IOGeneralInterest");
    OSObject property;
    OSSerialize serializer;
    for (bool trace : {false, true}) {
        probe.fStartupTrace = trace;
        logs.clear(); trace_clock = 0;
        bl_events[BL_READ] = bl_events[BL_SET] = 0;
        assert(probe.setProperty(&key, &property));
        assert(probe.setProperty(&interest, &property));
        unsigned reads = probe.registryReads, snapshots = probe.snapshot.releases;
        for (unsigned i = 0; i < 769; ++i)
            assert(probe.getProperty(&key) == &property);
        // #770 is suppressed in either mode, but still serializes normally.
        assert(probe.serializeProperties(&serializer));
        assert(probe.registryReads == reads + 769);
        assert(probe.snapshot.releases == snapshots);
        bl_events[BL_READ] = 0;
        assert(probe.serializeProperties(&serializer));
        assert(probe.snapshot.releases == snapshots + 1);
        probe.failRegistry = true;
        assert(!probe.setProperty(&key, nullptr));
        assert(probe.getProperty(&key) == &property);
        assert(!probe.serializeProperties(&serializer));
        probe.failRegistry = false;
        assert(probe.messages.empty() && !locked);
        if (trace) {
            // 512 initial reads + #768; three sets, three other sampled reads.
            assert(logs.size() == 519);
            for (const auto &log : logs) {
                assert(log.find("FUXI BACKLIGHT: trace ") == 0);
                assert(log.find("uptime_us=") != std::string::npos);
                assert(log.find("io_us=3 process=backboardd") != std::string::npos);
            }
            assert(logs[1].find("trace set #2 ") != std::string::npos);
        } else {
            assert(logs.empty() && trace_clock == 0);
        }
    }
    FuxiBacklight service;
    OSNumber zero(0), half(32768), maximum(65536), high(65537), negative(UINT64_MAX);
    OSObject wrong;
    OSDictionary flat, range, params, nested, empty;
    assert(service.setProperties(nullptr) == kIOReturnBadArgument);
    assert(service.setProperties(&wrong) == kIOReturnBadArgument);
    assert(service.setProperties(&empty) == kIOReturnUnsupported);
    flat.values["brightness"] = &zero;
    assert(service.setProperties(&flat) == kIOReturnSuccess && service.fRequested == 0);
    range.values["value"] = &half;
    params.values["brightness"] = &range;
    nested.values["IODisplayParameters"] = &params;
    assert(service.setProperties(&nested) == kIOReturnSuccess && service.fRequested == 32768);
    flat.values["brightness"] = &maximum;
    assert(service.setProperties(&flat) == kIOReturnSuccess && service.fRequested == 65536);
    OSObject *badValues[] = {&high, &negative, &wrong, &empty};
    for (OSObject *bad : badValues) {
        flat.values["brightness"] = bad;
        assert(service.setProperties(&flat) == kIOReturnBadArgument);
        assert(service.fRequested == 65536 && service.published == 65536);
    }
    nested.values["IODisplayParameters"] = &wrong;
    assert(service.setProperties(&nested) == kIOReturnBadArgument);
    flat.values["brightness"] = &zero;
    service.fail = true;
    assert(service.setProperties(&flat) == kIOReturnNoMemory);
    assert(service.fRequested == 65536 && service.published == 65536 && !locked);
    service.fail = false;

    // fuxipaneldcs>=3: DBV = 4 + v*(max-4)/65536, 0 still 4, never above the scanout side's cap; off (cap 0): hardware untouched.
    for (uint32_t max : {0u, 3u, 4u, 0x0a0u, 0x400u, 0x7ffu})
        for (uint64_t v : std::initializer_list<uint64_t>{0, 1, 16384, 32768, 65535, 65536, 70000, UINT64_MAX}) {
            uint32_t d = fbl_dbv(v, max), cap = max < 4 ? 4 : max;
            assert(d >= 4 && d <= cap);
            assert(d == 4 + (uint32_t)((v > 65536 ? 65536 : v) * (cap - 4) / 65536));
        }
    assert(fbl_dbv(0, 0x400) == 4 && fbl_dbv(65536, 0x400) == 0x400 && fbl_dbv(32768, 0x400) == 0x202 && fbl_dbv(65536, 0x7ff) == 0x7ff);
    for (uint32_t v = 0; v < 65536; v += 97) assert(fbl_dbv(v, 0x400) <= fbl_dbv(v + 97, 0x400)); // monotonic
    logs.clear(); bl_events[BL_BRIGHTNESS] = 0; dcs_sent.clear();
    flat.values["brightness"] = &half;
    assert(service.setProperties(&flat) == kIOReturnSuccess && dcs_sent.empty());
    assert(logs.size() == 1 && logs[0].find("valid=1 hardware=untouched") != std::string::npos);
    dcs_max = 0x400;
    assert(service.setProperties(&flat) == kIOReturnSuccess && (dcs_sent == std::vector<unsigned>{0x202}));
    assert(logs.size() == 2 && logs[1].find("requested=32768 valid=1 sent dbv=0x202 max=0x400") != std::string::npos);
    flat.values["brightness"] = &zero;
    assert(service.setProperties(&flat) == kIOReturnSuccess && dcs_sent.back() == 4 && service.fRequested == 0);
    flat.values["brightness"] = &high;  // invalid: nothing handed over, previous request kept
    assert(service.setProperties(&flat) == kIOReturnBadArgument && dcs_sent.size() == 2 && service.fRequested == 0);
    assert(logs.back().find("valid=0 hardware=untouched") != std::string::npos);
    flat.values["brightness"] = &maximum; service.fail = true; // failed publication: nothing handed over
    assert(service.setProperties(&flat) == kIOReturnNoMemory && dcs_sent.size() == 2);
    service.fail = false;
    assert(service.setProperties(&flat) == kIOReturnSuccess && dcs_sent.back() == 0x400);
    dcs_max = 0; dcs_sent.clear();

    // Execute the production notification fanout and framebuffer power action.
    AppleCLCD fb;
    IOService unrelated, otherProvider;
    FuxiBacklight backlight, untouched;
    fb.iterator.children = {&unrelated, &backlight};
    otherProvider.iterator.children = {&untouched};
    for (int value : {-1, 0, 1, 2, 3, 4}) {
        for (bool present : {false, true}) {
            for (bool on : {false, true}) {
                boot_present = present; boot_value = value;
                unsigned traversals = fb.traversals;
                size_t count = backlight.messages.size();
                fuxi_backlight_display_state(&fb, on);
                bool enabled = present && (value == 2 || (value == 3 && !on));
                assert(fb.traversals == traversals + enabled);
                assert(backlight.messages.size() == count + enabled);
            }
        }
    }
    backlight.messages.clear();
    boot_present = true; boot_value = 2;
    fb.fBacklightPowerNotifications = true;
    // Interest callbacks may reenter property handling: no framebuffer lock held.
    backlight.observer = [&](UInt32 message) {
        assert(fb.workloop.depth == 1);
        assert(fb.fPowerOff == (message == kIOMessageDeviceWillPowerOff));
        assert(sweet_fb_display_off == int(fb.fPowerOff));
        assert(backlight.setProperties(&flat) == kIOReturnSuccess);
    };
    for (uintptr_t state : {0u, 1u, 1u, 0u, 4u}) {
        fb.fDirty = false; fb.fFadeStep = 6;
        bool changed = fb.fPowerOff != (state == 0);
        assert(fb.requestPowerChange(state) == kIOReturnSuccess);
        assert(fb.fPowerOff == (state == 0) && fb.fDirty == changed);
        assert(fb.fFadeStep == (changed ? 0 : 6));
    }
    assert((backlight.messages == std::vector<UInt32>{0xe0000210, 0xe0000230,
                                                   0xe0000230, 0xe0000210, 0xe0000230}));
    assert(fb.wakes == 5 && !locked && untouched.messages.empty());
    // Withholding the child, iterator allocation failure and mode 1 do not
    // turn a logical power request into an error or change brightness.
    backlight.observer = nullptr;
    uint32_t requested = backlight.fRequested, published = backlight.published;
    fb.failIterator = true;
    size_t count = backlight.messages.size();
    assert(fb.requestPowerChange(0) == kIOReturnSuccess);
    assert(backlight.messages.size() == count);
    fb.failIterator = false;
    fb.iterator.children = {&unrelated};
    fuxi_backlight_display_state(&fb, true);
    assert(backlight.messages.size() == count);
    fb.iterator.children = {&backlight};
    boot_value = 1; fb.fBacklightPowerNotifications = false;
    unsigned traversals = fb.traversals;
    unsigned gated = fb.workloop.calls;
    fb.requestPowerChange(1);
    assert(fb.workloop.calls == gated);
    assert(fb.traversals == traversals && backlight.messages.size() == count);
    assert(fb.iterator.releases == fb.traversals - 1); // one failed iterator
    assert(backlight.fRequested == requested && backlight.published == published);

    // Mode 3 preserves the entire mode 2 power sequence and workloop gate,
    // including duplicates; only ON fanout is removed. Brightness writes do
    // not release a deferred message (there is no deferred state or timer).
    boot_value = 3; fb.fBacklightPowerNotifications = true;
    backlight.messages.clear(); logs.clear();
    backlight.observer = [&](UInt32 message) {
        assert(fb.workloop.depth == 1 && !locked);
        assert(message == kIOMessageDeviceWillPowerOff && fb.fPowerOff);
        assert(sweet_fb_display_off == 1);
    };
    gated = fb.workloop.calls;
    traversals = fb.traversals;
    for (uintptr_t state : {0u, 1u, 1u, 0u, 4u, 0u}) {
        fb.fDirty = false; fb.fFadeStep = 6;
        bool changed = fb.fPowerOff != (state == 0);
        assert(fb.requestPowerChange(state) == kIOReturnSuccess);
        assert(fb.fPowerOff == (state == 0) && fb.fDirty == changed);
        assert(sweet_fb_display_off == int(state == 0));
        assert(fb.fFadeStep == (changed ? 0 : 6));
    }
    assert(fb.workloop.calls == gated + 6 && fb.traversals == traversals + 3);
    assert((backlight.messages == std::vector<UInt32>{0xe0000210, 0xe0000210, 0xe0000210}));
    unsigned withheld = 0;
    for (const auto &log : logs)
        withheld += log.find("on message=0xe0000230 withheld experiment=off-only") != std::string::npos;
    assert(withheld == 3);
    flat.values["brightness"] = &maximum;
    assert(backlight.setProperties(&flat) == kIOReturnSuccess);
    assert(backlight.fRequested == 65536 && backlight.messages.size() == 3);
    fb.failIterator = true;
    assert(fb.requestPowerChange(1) == kIOReturnSuccess && !fb.fPowerOff);
    assert(fb.requestPowerChange(0) == kIOReturnSuccess && fb.fPowerOff);
    assert(backlight.messages.size() == 3 && !locked);
    // Selector 12 reaches the scanout side (fuxipaneldcs=4 decides there) only with scanout, before the fb lock and before fPowerOff.
    assert(panel_power.empty());
    fb.fScanout = true;
    for (uintptr_t state : {1u, 0u, 0u, 1u}) assert(fb.requestPowerChange(state) == kIOReturnSuccess);
    assert((panel_power == std::vector<int>{1, 0, 0, 1}) && !locked && !fb.fPowerOff);
}
'''
with tempfile.TemporaryDirectory(prefix='fuxi-backlight-test-') as tmp:
    path, exe = Path(tmp) / 'backlight.cpp', Path(tmp) / 'backlight'
    path.write_text(MODEL)
    subprocess.run(['clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-I', str(SRC), str(path), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
print('PASS: fuxi backlight (sync, opt-in, registry shape, parser, logical power interests, startup trace, rate limits)')
