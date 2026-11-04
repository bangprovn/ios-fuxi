/*
 * FUXI backlight-control observer and opt-in logical power notifications.
 * Published by AppleCLCD before its first registerService(), only on opt-in.
 * The small IODisplay metaclass lets the public IODisplayLib parameter helpers
 * resolve this service directly or by walking AppleCLCD's children. It does
 * not implement IOGraphics power policy or the private AppleARMBacklight ABI.
 *
 * The service conforms to AppleARMBacklight (an empty metaclass of that name:
 * this kernel has no AppleARMPlatform, so the name is free). iOS 16.7 cache,
 * unslid addresses: QuartzCore takes its libEDR client path (EDRClient::client
 * 0x18833a168, EDRRegisterCallbackBlockOnQueue at 0x1882d6200, partly under a
 * CA mutex) as soon as backlight-control matches (CADeviceHasBacklight byte set
 * at 0x1884cb7c0). Its server, com.apple.libEDR.Notification, is started by
 * CoreBrightness only through -[CBDisplayModuleiOS initWithBacklight:queue:
 * display:] -> DisplayCreateWithID 0x1af0d8510 -> CBEDRServerStartServer, and
 * -[CBDisplayContaineriOS setupInternalModules] creates that module only when
 * IOObjectConformsTo(backlight, "AppleARMBacklight") (0x1af0cec74). Without the
 * conformance the client's 5000 ms RPCs (EDRIsEnabled 0x1bef757e8) time out
 * and backboardd rendered about one frame per 5 s (h-e0); UniXNU-sheng saw the
 * same stall on iOS 17 (int-236) and fixed it with this conformance.
 */
#if defined(FUXI)
#include <IOKit/IOService.h>
#include <IOKit/IOUserClient.h>
#include <IOKit/IOLib.h>
#include <IOKit/IOLocks.h>
#include <IOKit/IOMessage.h>
#include <libkern/OSAtomic.h>
#include <libkern/c++/OSBoolean.h>
#include <libkern/c++/OSCollectionIterator.h>
#include <libkern/c++/OSData.h>
#include <libkern/c++/OSDictionary.h>
#include <libkern/c++/OSNumber.h>
#include <libkern/c++/OSString.h>
#include <pexpert/pexpert.h>
#include <sys/proc.h>
#include <kern/clock.h>
#include <mach/mach_time.h>
#include "fuxi_backlight_logic.h"
/* bsd/dev/fuxi_gpu_scanout.inc (fuxipaneldcs): the DBV cap while brightness may reach the panel (else 0), and the hand-off. */
extern "C" unsigned fuxi_gpu_panel_dcs_max(void);
extern "C" void fuxi_gpu_panel_dbv(unsigned dbv);

enum { BL_READ, BL_SET, BL_PROPERTIES, BL_OPEN, BL_METHOD, BL_BRIGHTNESS, BL_POWER, BL_COUNT };
static volatile SInt32 bl_events[BL_COUNT];
static bool bl_log(unsigned kind, uint32_t *event, bool trace = false)
{
	*event = (uint32_t)OSIncrementAtomic(&bl_events[kind]);
	if (kind == BL_READ) return fbl_read_log_this(*event, trace);
	return fbl_log_this(*event);
}

/* Monotonic boot-time stamps; Metal's first-frame duration is relative. */
static void bl_stamp(const char *op, uint32_t event, uint64_t started)
{
	uint64_t now = mach_absolute_time(), ns = 0, elapsed = 0;
	absolutetime_to_nanoseconds(now, &ns);
	absolutetime_to_nanoseconds(now - started, &elapsed);
	char name[33] = {};
	proc_selfname(name, sizeof(name));
	IOLog("FUXI BACKLIGHT: trace %s #%u uptime_us=%llu io_us=%llu process=%.32s\n",
	    op, event, ns / 1000, elapsed / 1000, name);
}

/* Log values without serializing arbitrary user data or leaking addresses.
 * Bound dictionary traversal, including hostile self-referential collections.
 */
static void bl_value(const char *op, uint32_t event, const char *key,
    OSObject *value, unsigned depth = 0, unsigned *remaining = NULL)
{
	unsigned budget = 64;
	if (!remaining) remaining = &budget;
	if (!*remaining) return;
	--*remaining;
	OSNumber *number = OSDynamicCast(OSNumber, value);
	OSBoolean *boolean = OSDynamicCast(OSBoolean, value);
	OSDictionary *dict = OSDynamicCast(OSDictionary, value);
	OSData *data = OSDynamicCast(OSData, value);
	if (number) {
		IOLog("FUXI BACKLIGHT: %s #%u key=%.96s number=%llu\n", op, event, key,
		    number->unsigned64BitValue());
	} else if (boolean) {
		IOLog("FUXI BACKLIGHT: %s #%u key=%.96s bool=%u\n", op, event, key, boolean->isTrue());
	} else {
		IOLog("FUXI BACKLIGHT: %s #%u key=%.96s type=%s count=%u depth=%u\n", op,
		    event, key, value ? value->getMetaClass()->getClassName() : "missing",
		    dict ? dict->getCount() : (data ? data->getLength() : 0), depth);
	}
	if (!dict || depth >= 3) return;
	OSCollectionIterator *it = OSCollectionIterator::withCollection(dict);
	if (!it) return;
	OSString *child;
	while (*remaining && (child = OSDynamicCast(OSString, it->getNextObject())))
		bl_value(op, event, child->getCStringNoCopy(), dict->getObject(child), depth + 1, remaining);
	it->release();
}

class IODisplay : public IOService {
	OSDeclareDefaultStructors(IODisplay);
};
OSDefineMetaClassAndStructors(IODisplay, IOService);

/* Name only: CoreBrightness's internal-display modules and QuartzCore's EDR server need IOObjectConformsTo(..., "AppleARMBacklight"). */
class AppleARMBacklight : public IODisplay {
	OSDeclareDefaultStructors(AppleARMBacklight);
};
OSDefineMetaClassAndStructors(AppleARMBacklight, IODisplay);

class FuxiBacklight : public AppleARMBacklight {
	OSDeclareDefaultStructors(FuxiBacklight);
	IOLock *fLock;
	uint32_t fRequested;
	bool fStartupTrace;
	bool publishBrightness(uint32_t value);
public:
	using IOService::getProperty;
	using IOService::setProperty;
	bool start(IOService *provider) APPLE_KEXT_OVERRIDE;
	void free(void) APPLE_KEXT_OVERRIDE;
	OSObject *getProperty(const OSSymbol *key) const APPLE_KEXT_OVERRIDE;
	bool setProperty(const OSSymbol *key, OSObject *value) APPLE_KEXT_OVERRIDE;
	bool serializeProperties(OSSerialize *s) const APPLE_KEXT_OVERRIDE;
	IOReturn setProperties(OSObject *properties) APPLE_KEXT_OVERRIDE;
	IOReturn newUserClient(task_t task, void *securityID, UInt32 type,
	    OSDictionary *properties, IOUserClient **handler) APPLE_KEXT_OVERRIDE;
};
OSDefineMetaClassAndStructors(FuxiBacklight, AppleARMBacklight);

class FuxiBacklightUserClient : public IOUserClient {
	OSDeclareDefaultStructors(FuxiBacklightUserClient);
public:
	IOReturn clientClose(void) APPLE_KEXT_OVERRIDE;
	IOReturn setProperties(OSObject *properties) APPLE_KEXT_OVERRIDE;
	IOReturn externalMethod(uint32_t selector, IOExternalMethodArguments *args,
	    IOExternalMethodDispatch *dispatch, OSObject *target, void *reference) APPLE_KEXT_OVERRIDE;
};
OSDefineMetaClassAndStructors(FuxiBacklightUserClient, IOUserClient);

bool FuxiBacklight::publishBrightness(uint32_t value)
{
	OSDictionary *params = OSDictionary::withCapacity(1);
	OSDictionary *brightness = OSDictionary::withCapacity(3);
	OSNumber *minimum = OSNumber::withNumber(0ULL, 32);
	OSNumber *maximum = OSNumber::withNumber(FUXI_BACKLIGHT_MAX, 32);
	OSNumber *current = OSNumber::withNumber(value, 32);
	bool ok = params && brightness && minimum && maximum && current;
	if (ok) ok = brightness->setObject("min", minimum) &&
	    brightness->setObject("max", maximum) && brightness->setObject("value", current) &&
	    params->setObject("brightness", brightness) && setProperty("IODisplayParameters", params);
	OSSafeReleaseNULL(current);
	OSSafeReleaseNULL(maximum);
	OSSafeReleaseNULL(minimum);
	OSSafeReleaseNULL(brightness);
	OSSafeReleaseNULL(params);
	return ok;
}

bool FuxiBacklight::start(IOService *provider)
{
	/* Defend the gate here as well as before allocation/attachment. */
	int enable = 0;
	bool present = PE_parse_boot_argn("fuxibacklight", &enable, sizeof(enable));
	if (!fbl_enabled(present, enable)) return false;
	int trace = 0;
	bool tracePresent = PE_parse_boot_argn("fuxibacklighttrace", &trace, sizeof(trace));
	fStartupTrace = fbl_startup_trace(present, enable, tracePresent, trace);
	if (!IOService::start(provider)) return false;
	if (fStartupTrace)
		IOLog("FUXI BACKLIGHT: startup trace enabled mode=1 read_limit=512 interests=none\n");
	fLock = IOLockAlloc();
	fRequested = FUXI_BACKLIGHT_MAX;
	if (!fLock || !setProperty("backlight-control", kOSBooleanTrue) ||
	    !publishBrightness(fRequested)) return false;
	setName("FuxiBacklight");
	IOLog("FUXI BACKLIGHT: published observation-only brightness min=0 max=%u value=%u conforms=AppleARMBacklight\n",
	    FUXI_BACKLIGHT_MAX, fRequested);
	registerService();
	if (enable == 3)
		IOLog("FUXI BACKLIGHT: OFF-only interest experiment enabled; ON withheld\n");
	else if (fbl_power_notifications(present, enable))
		IOLog("FUXI BACKLIGHT: logical power interest notifications enabled\n");
	return true;
}

void FuxiBacklight::free(void)
{
	if (fLock) IOLockFree(fLock);
	IOService::free();
}

OSObject *FuxiBacklight::getProperty(const OSSymbol *key) const
{
	uint64_t started = fStartupTrace ? mach_absolute_time() : 0;
	OSObject *value = IOService::getProperty(key);
	uint32_t event;
	if (bl_log(BL_READ, &event, fStartupTrace)) {
		if (fStartupTrace) bl_stamp("read", event, started);
		bl_value("read", event, key->getCStringNoCopy(), value);
	}
	return value;
}

bool FuxiBacklight::setProperty(const OSSymbol *key, OSObject *value)
{
	uint32_t event;
	bool sampled = bl_log(BL_SET, &event);
	if (sampled) bl_value("set", event, key->getCStringNoCopy(), value);
	/* Exclude console printing from the underlying registry operation's time. */
	uint64_t started = fStartupTrace ? mach_absolute_time() : 0;
	bool result = IOService::setProperty(key, value);
	if (fStartupTrace && sampled) bl_stamp("set", event, started);
	return result;
}

bool FuxiBacklight::serializeProperties(OSSerialize *s) const
{
	/* CreateCFProperties bypasses getProperty: log its entire snapshot too. */
	uint32_t event;
	bool sampled = bl_log(BL_READ, &event, fStartupTrace);
	if (sampled) {
		OSDictionary *snapshot = dictionaryWithProperties();
		bl_value("read-all", event, "*", snapshot);
		OSSafeReleaseNULL(snapshot);
	}
	uint64_t started = fStartupTrace ? mach_absolute_time() : 0;
	bool result = IOService::serializeProperties(s);
	if (fStartupTrace && sampled) bl_stamp("read-all", event, started);
	return result;
}

IOReturn FuxiBacklight::setProperties(OSObject *properties)
{
	uint32_t event;
	if (bl_log(BL_PROPERTIES, &event)) bl_value("setProperties", event, "*", properties);
	OSDictionary *dict = OSDynamicCast(OSDictionary, properties);
	if (!dict) return kIOReturnBadArgument;
	OSObject *nested = dict->getObject("IODisplayParameters");
	if (nested) {
		dict = OSDynamicCast(OSDictionary, nested);
		if (!dict) return kIOReturnBadArgument;
	}
	OSObject *value = dict->getObject("brightness");
	if (!value) return kIOReturnUnsupported;
	OSDictionary *range = OSDynamicCast(OSDictionary, value);
	if (range) value = range->getObject("value");
	OSNumber *number = OSDynamicCast(OSNumber, value);
	uint64_t requested = number ? number->unsigned64BitValue() : 0;
	bool valid = number && fbl_valid(requested);
	/* fuxipaneldcs>=3 (scanout side): the request becomes the panel's DBV; otherwise nothing touches hardware. */
	uint32_t cap = valid ? fuxi_gpu_panel_dcs_max() : 0, dbv = cap ? fbl_dbv(requested, cap) : 0;
	if (bl_log(BL_BRIGHTNESS, &event)) {
		if (cap)
			IOLog("FUXI BACKLIGHT: brightness #%u requested=%llu valid=%u sent dbv=%#x max=%#x\n",
			    event, requested, valid, dbv, cap);
		else
			IOLog("FUXI BACKLIGHT: brightness #%u requested=%llu valid=%u hardware=untouched\n",
			    event, requested, valid);
	}
	if (!valid) return kIOReturnBadArgument;
	IOLockLock(fLock);
	/* Replace the immutable registry dictionary; never mutate a reader's snapshot. */
	bool ok = publishBrightness((uint32_t)requested);
	if (ok) fRequested = (uint32_t)requested;
	IOLockUnlock(fLock);
	if (ok && cap) fuxi_gpu_panel_dbv(dbv); /* never blocks: coalesced and sent between two transfers */
	return ok ? kIOReturnSuccess : kIOReturnNoMemory;
}

IOReturn FuxiBacklight::newUserClient(task_t task, void *securityID, UInt32 type,
    OSDictionary *properties, IOUserClient **handler)
{
	*handler = NULL;
	FuxiBacklightUserClient *client = new FuxiBacklightUserClient;
	IOReturn result = kIOReturnNoMemory;
	if (client) {
		if (!client->initWithTask(task, securityID, type, properties)) result = kIOReturnBadArgument;
		else if (!client->attach(this)) result = kIOReturnNoResources;
		else if (!client->start(this)) {
			client->detach(this);
			result = kIOReturnError;
		} else {
			*handler = client;
			result = kIOReturnSuccess;
		}
		if (result != kIOReturnSuccess) client->release();
	}
	uint32_t event;
	if (bl_log(BL_OPEN, &event)) {
		char name[33] = {};
		proc_selfname(name, sizeof(name));
		IOLog("FUXI BACKLIGHT: open #%u process=%.32s type=%u -> 0x%x\n",
		    event, name, type, result);
	}
	return result;
}

IOReturn FuxiBacklightUserClient::clientClose(void)
{
	uint32_t event;
	if (bl_log(BL_OPEN, &event)) IOLog("FUXI BACKLIGHT: close #%u\n", event);
	terminate();
	return kIOReturnSuccess;
}

IOReturn FuxiBacklightUserClient::setProperties(OSObject *properties)
{
	FuxiBacklight *service = OSDynamicCast(FuxiBacklight, getProvider());
	return service ? service->setProperties(properties) : kIOReturnNotAttached;
}

IOReturn FuxiBacklightUserClient::externalMethod(uint32_t selector, IOExternalMethodArguments *args,
    IOExternalMethodDispatch *, OSObject *, void *)
{
	uint32_t event;
	if (bl_log(BL_METHOD, &event)) {
		char name[33] = {};
		proc_selfname(name, sizeof(name));
		IOLog("FUXI BACKLIGHT: method #%u process=%.32s selector=%u scalars=%u struct-in=%llu struct-out=%llu -> unsupported\n",
		    event, name, selector, args ? args->scalarInputCount : 0,
		    args ? (uint64_t)args->structureInputSize : 0,
		    args ? (uint64_t)args->structureOutputSize : 0);
		if (args && args->scalarInput)
			for (uint32_t i = 0; i < args->scalarInputCount; i++)
				IOLog("FUXI BACKLIGHT: method #%u selector=%u scalar[%u]=0x%llx\n",
				    event, selector, i, args->scalarInput[i]);
	}
	/* No guessed selector ABI, no fabricated successful getter results. */
	if (args) { args->scalarOutputCount = 0; args->structureOutputSize = 0; }
	return kIOReturnUnsupported;
}

void fuxi_backlight_publish(IOService *provider)
{
	int enable = 0;
	bool present = PE_parse_boot_argn("fuxibacklight", &enable, sizeof(enable));
	if (!fbl_enabled(present, enable)) return;
	FuxiBacklight *service = new FuxiBacklight;
	if (!service) { IOLog("FUXI BACKLIGHT: allocation failed\n"); return; }
	if (!service->init()) {
		IOLog("FUXI BACKLIGHT: init failed\n");
	} else if (!service->attach(provider)) {
		IOLog("FUXI BACKLIGHT: attach failed\n");
	} else if (!service->start(provider)) {
		IOLog("FUXI BACKLIGHT: start failed\n");
		service->detach(provider);
	}
	service->release(); /* registry attachment owns the successful service */
}

/* Called on AppleCLCD's workloop gate, after releasing its framebuffer lock.
 * CoreBrightness's BacklightGeneralInterestNotificationCallback consumes these
 * exact messages without a payload. This reports accepted logical power, not
 * panel readiness, brightness, or completion of a BLS transition.
 * Mode 3 deliberately withholds ON to isolate its effect from OFF and gating;
 * it is a diagnostic protocol violation, not a completed backlight driver.
 */
void fuxi_backlight_display_state(IOService *provider, bool on)
{
	int enable = 0;
	bool present = PE_parse_boot_argn("fuxibacklight", &enable, sizeof(enable));
	if (!fbl_power_notifications(present, enable)) return;
	if (fbl_withhold_on(present, enable, on)) {
		uint32_t event;
		if (bl_log(BL_POWER, &event))
			IOLog("FUXI BACKLIGHT: power #%u on message=0xe0000230 withheld experiment=off-only\n", event);
		return;
	}
	OSIterator *it = provider->getClientIterator();
	if (!it) {
		IOLog("FUXI BACKLIGHT: power notification iterator unavailable\n");
		return;
	}
	OSObject *child;
	while ((child = it->getNextObject())) {
		FuxiBacklight *service = OSDynamicCast(FuxiBacklight, child);
		if (!service) continue;
		UInt32 message = on ? kIOMessageDeviceHasPoweredOn : kIOMessageDeviceWillPowerOff;
		IOReturn result = service->messageClients(message);
		uint32_t event;
		if (bl_log(BL_POWER, &event))
			IOLog("FUXI BACKLIGHT: power #%u %s message=0x%x result=0x%x logical-only\n",
			    event, on ? "on" : "off", message, result);
		break;
	}
	it->release();
}
#endif /* FUXI */
