/*
 * SWEET: HID event services for the iOS userland (backboardd's HID event
 * system).
 *
 * backboardd watches for IOService objects with HIDServiceSupport = true,
 * opens them with IOServiceOpen type 'esuc' (IOHIDEventServiceUserClient,
 * compiled here from IOHIDFamily-1915.140.3, the version the iOS 16.7
 * userland ships) and reads serialized IOHIDEvents from the user client's
 * shared queue. This file has the minimal IOHIDEventService base class and
 * two services:
 *
 *   SweetHIDButtons  consumer-control keys (power, volume, home)
 *   SweetHIDTouch    the built-in touch screen (digitizer collections)
 *
 * Hardware drivers feed them through the C entry points sweet_hid_key(),
 * sweet_hid_key_at() (with the press time as event timestamp) and
 * sweet_hid_touch(). Boot-arg sweethidtest=N (seconds) presses Home
 * every N seconds, to test the event path without input hardware.
 *
 * SweetHIDTouch records backboardd's TouchDetectionMode property for
 * tap-to-wake (sweet_hid_touch_mode(), used by bsd/dev/fuxi_keys.c).
 * Ported from HackiOS hackios-xnu-userland userland/src/inject.c (MIT):
 * the TouchDetectionMode values HackiOS observed (0x1 report touches, low
 * byte 0xfe display off with tap-to-wake, 0xff nothing). Copyright (c) 2026
 * OOSEX GANG; MIT notice in bsd/dev/fuxi_key_state.h.
 */
#include <IOKit/IOLib.h>
#include <IOKit/IOService.h>
#include <IOKit/IOTimerEventSource.h>
#include <libkern/c++/OSArray.h>
#include <libkern/c++/OSCollectionIterator.h>
#include <libkern/c++/OSNumber.h>
#include <libkern/c++/OSSet.h>
#include <libkern/c++/OSString.h>
#include <pexpert/pexpert.h>

#include "IOHIDEventService.h"
#include "IOHIDEventServiceUserClient.h"
#include "IOHIDEvent.h"
#include "IOHIDKeys.h"
#include "IOHIDPrivateKeys.h"
#include "IOHIDUsageTables.h"

extern "C" uint64_t mach_absolute_time(void);

/* One open client: the user client and its event callback. */
class IOHIDEventServiceClient : public OSObject {
	OSDeclareDefaultStructors(IOHIDEventServiceClient);
public:
	bool                       initClient() { return OSObject::init(); }
	IOService                 *client;
	void                      *context;
	IOHIDEventService::Action  action;
};
OSDefineMetaClassAndStructors(IOHIDEventServiceClient, OSObject);

#define super IOService
OSDefineMetaClassAndStructors(IOHIDEventService, IOService);

bool
IOHIDEventService::start(IOService *provider)
{
	if (!super::start(provider)) {
		return false;
	}
	_clientLock = IOLockAlloc();
	_clients = OSArray::withCapacity(2);
	_hidWorkLoop = getWorkLoop();
	if (!_clientLock || !_clients || !_hidWorkLoop) {
		return false;
	}
	_hidWorkLoop->retain();
	_hidGate = IOCommandGate::commandGate(this);
	if (!_hidGate || _hidWorkLoop->addEventSource(_hidGate) != kIOReturnSuccess) {
		return false;
	}

	OSString *transport = getTransport();
	OSString *product = getProduct();
	setProperty(kIOHIDTransportKey, transport);
	setProperty(kIOHIDProductKey, product);
	setProperty(kIOHIDManufacturerKey, "Sweet");
	setProperty(kIOHIDVendorIDKey, getVendorID(), 32);
	setProperty(kIOHIDProductIDKey, getProductID(), 32);
	setProperty(kIOHIDLocationIDKey, (unsigned long long)getProductID(), 32);
	setProperty(kIOHIDReportIntervalKey, getReportInterval(), 32);
	setProperty(kIOHIDPrimaryUsagePageKey, getPrimaryUsagePage(), 32);
	setProperty(kIOHIDPrimaryUsageKey, getPrimaryUsage(), 32);
	setProperty(kIOHIDBuiltInKey, kOSBooleanTrue);
	OSSafeReleaseNULL(transport);
	OSSafeReleaseNULL(product);

	OSDictionary *pair = OSDictionary::withCapacity(2);
	OSArray *pairs = OSArray::withCapacity(1);
	OSNumber *page = OSNumber::withNumber(getPrimaryUsagePage(), 32);
	OSNumber *usage = OSNumber::withNumber(getPrimaryUsage(), 32);
	if (pair && pairs && page && usage) {
		pair->setObject(kIOHIDDeviceUsagePageKey, page);
		pair->setObject(kIOHIDDeviceUsageKey, usage);
		pairs->setObject(pair);
		setProperty(kIOHIDDeviceUsagePairsKey, pairs);
	}
	OSSafeReleaseNULL(pair);
	OSSafeReleaseNULL(pairs);
	OSSafeReleaseNULL(page);
	OSSafeReleaseNULL(usage);

	/* What IOHIDFamily's "IOHIDEventServiceUserClient" personality merges in. */
	setProperty("HIDServiceSupport", kOSBooleanTrue);
	setProperty("IOUserClientClass", "IOHIDEventServiceUserClient");
	return true;
}

void
IOHIDEventService::free(void)
{
	if (_hidGate && _hidWorkLoop) {
		_hidWorkLoop->removeEventSource(_hidGate);
	}
	OSSafeReleaseNULL(_hidGate);
	OSSafeReleaseNULL(_hidWorkLoop);
	OSSafeReleaseNULL(_clients);
	if (_clientLock) {
		IOLockFree(_clientLock);
		_clientLock = NULL;
	}
	super::free();
}

bool
IOHIDEventService::open(IOService *client, IOOptionBits options, void *context, Action action)
{
	IOHIDEventServiceClient *data = OSTypeAlloc(IOHIDEventServiceClient);
	bool ok = false;

	if (data && data->initClient()) {
		data->client = client;
		data->context = context;
		data->action = action;
		ok = super::open(client, options, data);
	}
	OSSafeReleaseNULL(data);
	IOLog("SWEET HID: %s open by %s -> %d\n", getName(), client ? client->getName() : "?", ok);
	return ok;
}

void
IOHIDEventService::close(IOService *forClient, IOOptionBits options)
{
	super::close(forClient, options);
}

bool
IOHIDEventService::handleOpen(IOService *client, IOOptionBits options __unused, void *argument)
{
	IOHIDEventServiceClient *data = OSDynamicCast(IOHIDEventServiceClient, (OSObject *)argument);
	bool ok = false;

	if (!data) {
		return false;
	}
	IOLockLock(_clientLock);
	if (!handleIsOpenLocked(client)) {
		/* Copy on write: dispatchEvent iterates a snapshot without the lock. */
		OSArray *next = OSArray::withArray(_clients, _clients->getCount() + 1);
		if (next && next->setObject(data)) {
			_clients->release();
			_clients = next;
			ok = true;
		} else {
			OSSafeReleaseNULL(next);
		}
	}
	IOLockUnlock(_clientLock);
	return ok;
}

void
IOHIDEventService::handleClose(IOService *client, IOOptionBits options __unused)
{
	IOLockLock(_clientLock);
	OSArray *next = OSArray::withCapacity(_clients->getCount());
	if (next) {
		for (unsigned int i = 0; i < _clients->getCount(); i++) {
			IOHIDEventServiceClient *data = (IOHIDEventServiceClient *)_clients->getObject(i);
			if (data->client != client) {
				next->setObject(data);
			}
		}
		_clients->release();
		_clients = next;
	}
	IOLockUnlock(_clientLock);
}

bool
IOHIDEventService::handleIsOpenLocked(const IOService *client) const
{
	for (unsigned int i = 0; i < _clients->getCount(); i++) {
		IOHIDEventServiceClient *data = (IOHIDEventServiceClient *)_clients->getObject(i);
		if (client == NULL || data->client == client) {
			return true;
		}
	}
	return false;
}

bool
IOHIDEventService::handleIsOpen(const IOService *client) const
{
	IOLockLock(_clientLock);
	bool open = handleIsOpenLocked(client);
	IOLockUnlock(_clientLock);
	return open;
}

void
IOHIDEventService::dispatchEvent(IOHIDEvent *event, IOOptionBits options)
{
	OSArray *clients;

	event->setSenderID(getRegistryEntryID());
	IOLockLock(_clientLock);
	clients = _clients;
	clients->retain();
	IOLockUnlock(_clientLock);

	for (unsigned int i = 0; i < clients->getCount(); i++) {
		IOHIDEventServiceClient *data = (IOHIDEventServiceClient *)clients->getObject(i);
		if (data->action) {
			(*data->action)(data->client, this, data->context, event, options);
		}
	}
	clients->release();
}

IOHIDEvent *
IOHIDEventService::copyEvent(IOHIDEventType type __unused, IOHIDEvent *matching __unused, IOOptionBits options __unused)
{
	return NULL;
}

IOHIDEvent *
IOHIDEventService::copyMatchingEvent(OSDictionary *matching __unused)
{
	return NULL;
}

IOReturn
IOHIDEventService::setElementValue(UInt32 usagePage __unused, UInt32 usage __unused, UInt32 value __unused)
{
	return kIOReturnUnsupported;
}

IOReturn
IOHIDEventService::setProperties(OSObject *properties)
{
	/* The event system pushes its settings here; nothing to configure. */
	return OSDynamicCast(OSDictionary, properties) ? kIOReturnSuccess : kIOReturnBadArgument;
}

UInt32
IOHIDEventService::getPrimaryUsagePage()
{
	return 0;
}

UInt32
IOHIDEventService::getPrimaryUsage()
{
	return 0;
}

OSString *
IOHIDEventService::getTransport()
{
	return OSString::withCString("SPU");
}

OSString *
IOHIDEventService::getProduct()
{
	return OSString::withCString(getName());
}

UInt32
IOHIDEventService::getVendorID()
{
	return 0x05ac;
}

UInt32
IOHIDEventService::getProductID()
{
	return 0;
}

UInt32
IOHIDEventService::getReportInterval()
{
	return 10000;	/* us */
}

IOReturn
IOHIDEventService::newUserClient(task_t owningTask, void *securityID, UInt32 type,
    OSDictionary *properties, IOUserClient **handler)
{
	IOUserClient *client;

	if (type != kIOHIDEventServiceUserClientType) {
		IOLog("SWEET HID: %s user client type 0x%x unsupported\n", getName(), (unsigned)type);
		return super::newUserClient(owningTask, securityID, type, properties, handler);
	}
	client = OSTypeAlloc(IOHIDEventServiceUserClient);
	if (!client) {
		return kIOReturnNoMemory;
	}
	if (!client->initWithTask(owningTask, securityID, type)) {
		client->release();
		IOLog("SWEET HID: %s user client init refused\n", getName());
		return kIOReturnBadArgument;
	}
	if (!client->attach(this)) {
		client->release();
		return kIOReturnUnsupported;
	}
	if (!client->start(this)) {
		client->detach(this);
		client->release();
		return kIOReturnUnsupported;
	}
	*handler = client;
	return kIOReturnSuccess;
}

/*
 * Buttons. iOS takes the side/volume/home buttons as consumer-control
 * keyboard events (AppleM68Buttons publishes the same usage pair). Usages,
 * all on the Consumer page 0x0C, the same as HackiOS sends to the iOS 16
 * backboardd (hackios-xnu-userland userland/src/inject.c send_button()):
 * side button Power 0x30 (SpringBoard's SBLockHardwareButton: lock, wake,
 * long press = Siri), Volume Increment 0xE9, Volume Decrement 0xEA, Menu
 * 0x40 (home, sweethidtest only). The phone has no ringer switch, so no
 * Telephony page (0x0B) or mute usage is ever sent.
 */
class SweetHIDButtons : public IOHIDEventService {
	OSDeclareDefaultStructors(SweetHIDButtons);
public:
	virtual bool start(IOService *provider) APPLE_KEXT_OVERRIDE;
	void key(UInt32 usage, bool down, uint64_t timestamp);
protected:
	virtual UInt32 getPrimaryUsagePage() APPLE_KEXT_OVERRIDE { return kHIDPage_Consumer; }
	virtual UInt32 getPrimaryUsage() APPLE_KEXT_OVERRIDE { return kHIDUsage_Csmr_ConsumerControl; }
	virtual UInt32 getProductID() APPLE_KEXT_OVERRIDE { return 1; }
private:
	static void testFired(OSObject *owner, IOTimerEventSource *sender);
	IOTimerEventSource *fTest;
	UInt32 fTestSecs;
	UInt32 fTestCount;
};
OSDefineMetaClassAndStructors(SweetHIDButtons, IOHIDEventService);

/*
 * Touch screen: digitizer collection events with finger children,
 * coordinates normalized to 0..1 like IOHIDEventDriver produces for
 * absolute digitizers.
 */
#define SWEET_TOUCH_FINGERS 10
/*
 * Contacts that start this close to a panel edge (pixels, ~3 mm) carry
 * kIOHIDDigitizerEventFromEdgeTip for their whole life, as Apple's touch
 * firmware reports them. UIKit derives UITouch's edge type from that bit
 * (_UIEventHIDTouchEdgeTypeForHIDEvent); without it there is no home or
 * app-switcher gesture.
 */
#define SWEET_TOUCH_EDGE_PX 48
/*
 * sweetfbfit: SweetFramebuffer sets sweet_fb_fit when it shows the whole
 * 1125x2436 frame scaled into 1080x2338 at panel y=31 (FUXI_FIT_* in
 * fuxi_adreno.h). A panel pixel then maps to x/1080 and (y-31)/2338 of the
 * frame, i.e. iOS pixels x/0.96 and (y-31)*2436/2338; touches in the black
 * bars clamp to the frame's first or last row, and edges count from the
 * frame's top and bottom (the bars included). sweetfbfit=2 (sweet_fb_fit
 * 2) stretches the frame over the whole panel: x/1080 and y/2400, i.e.
 * iOS pixels x/0.96 and y*2436/2400, edges as the panel's.
 */
#define SWEET_FIT_W 1080
#define SWEET_FIT_H 2338
#define SWEET_FIT_Y 31
extern "C" int sweet_fb_fit; /* 0 crop, 1 scaled with bars, 2 stretched */
class SweetHIDTouch : public IOHIDEventService {
	OSDeclareDefaultStructors(SweetHIDTouch);
public:
	virtual bool start(IOService *provider) APPLE_KEXT_OVERRIDE;
	virtual IOReturn setProperties(OSObject *properties) APPLE_KEXT_OVERRIDE;
	void touch(int finger, bool down, int x, int y);
	bool report(UInt32 mask, const int *x, const int *y);
protected:
	virtual UInt32 getPrimaryUsagePage() APPLE_KEXT_OVERRIDE { return kHIDPage_Digitizer; }
	virtual UInt32 getPrimaryUsage() APPLE_KEXT_OVERRIDE { return kHIDUsage_Dig_TouchScreen; }
	virtual UInt32 getProductID() APPLE_KEXT_OVERRIDE { return 2; }
private:
	IOLock *fLock;
	OSSet *fSeenKeys;	/* property names backboardd has set, logged once each */
	int fLogicalW, fLogicalH, fOffX, fOffY;
	bool fDown[SWEET_TOUCH_FINGERS];
	bool fEdge[SWEET_TOUCH_FINGERS];
	IOFixed fX[SWEET_TOUCH_FINGERS];
	IOFixed fY[SWEET_TOUCH_FINGERS];
};
OSDefineMetaClassAndStructors(SweetHIDTouch, IOHIDEventService);

static SweetHIDButtons *gSweetButtons;
static SweetHIDTouch *gSweetTouch;
static volatile SInt32 gSweetTouchMode = -1;	/* TouchDetectionMode, -1 never set */
#define SWEET_TOUCH_SEEN_KEYS 24

bool
SweetHIDButtons::start(IOService *provider)
{
	if (!IOHIDEventService::start(provider)) {
		return false;
	}
	if (PE_parse_boot_argn("sweethidtest", &fTestSecs, sizeof(fTestSecs)) && fTestSecs) {
		fTest = IOTimerEventSource::timerEventSource(this, &SweetHIDButtons::testFired);
		if (fTest && _hidWorkLoop->addEventSource(fTest) == kIOReturnSuccess) {
			fTest->setTimeoutMS(fTestSecs * 1000);
		}
	}
	gSweetButtons = this;
	registerService();
	IOLog("SWEET HID: buttons registered (test every %u s)\n", fTestSecs);
	return true;
}

void
SweetHIDButtons::key(UInt32 usage, bool down, uint64_t timestamp)
{
	IOHIDEvent *event = IOHIDEvent::keyboardEvent(timestamp, kHIDPage_Consumer, usage, down,
	    kIOHIDEventOptionIsBuiltIn);

	if (event) {
		IOLog("SWEET HID: key 0x%x %s (%u clients)\n", (unsigned)usage, down ? "down" : "up",
		    _clients ? _clients->getCount() : 0);
		dispatchEvent(event);
		event->release();
	}
}

void
SweetHIDButtons::testFired(OSObject *owner, IOTimerEventSource *sender)
{
	SweetHIDButtons *self = (SweetHIDButtons *)owner;

	/*
	 * Home (Menu) press, then the release 100 ms later; Setup's "Hello"
	 * screen waits for it. Even counts press, odd counts release.
	 */
	bool press = (self->fTestCount & 1) == 0;
	self->key(kHIDUsage_Csmr_Menu, press, mach_absolute_time());
	if (++self->fTestCount < 60) {
		sender->setTimeoutMS(press ? 100 : self->fTestSecs * 1000);
	}
}

bool
SweetHIDTouch::start(IOService *provider)
{
	fLock = IOLockAlloc();
	if (!fLock || !IOHIDEventService::start(provider)) {
		return false;
	}
	int native = 0;
	PE_parse_boot_argn("sweetfb_native", &native, sizeof(native));
	fLogicalW = native ? 1080 : 1125;
	fLogicalH = native ? 2400 : 2436;
	fOffX = (fLogicalW - 1080) / 2;
	fOffY = (fLogicalH - 2400) / 2;
	/* The digitizer covers the main display (backboardd maps it 1:1). */
	setProperty("DisplayIntegrated", kOSBooleanTrue);
	setProperty("SurfaceWidth", (unsigned long long)(68 << 16), 32);	/* mm, 16.16 */
	setProperty("SurfaceHeight", (unsigned long long)(151 << 16), 32);
	fSeenKeys = OSSet::withCapacity(SWEET_TOUCH_SEEN_KEYS);
	gSweetTouch = this;
	registerService();
	IOLog("SWEET HID: touch registered\n");
	return true;
}

/*
 * The event system pushes its settings here. TouchDetectionMode is kept for
 * tap-to-wake; every other property is accepted and ignored as before. The
 * first SWEET_TOUCH_SEEN_KEYS distinct names are logged once each.
 */
IOReturn
SweetHIDTouch::setProperties(OSObject *properties)
{
	OSDictionary *dict = OSDynamicCast(OSDictionary, properties);

	if (!dict) {
		return kIOReturnBadArgument;
	}
	OSNumber *mode = OSDynamicCast(OSNumber, dict->getObject("TouchDetectionMode"));
	if (mode) {
		SInt32 value = (SInt32)(mode->unsigned32BitValue() & 0x7fffffff);
		SInt32 old = gSweetTouchMode;
		gSweetTouchMode = value;
		if (old != value) {
			IOLog("SWEET HID: touch detection mode %#x\n", (unsigned)value);
		}
	}
	OSCollectionIterator *it = fSeenKeys ? OSCollectionIterator::withCollection(dict) : NULL;
	if (it) {
		OSString *name;
		IOLockLock(fLock);
		while ((name = OSDynamicCast(OSString, it->getNextObject()))) {
			if (fSeenKeys->getCount() < SWEET_TOUCH_SEEN_KEYS && !fSeenKeys->containsObject(name)) {
				fSeenKeys->setObject(name);
				IOLog("SWEET HID: touch property \"%s\" set by the event system\n", name->getCStringNoCopy());
			}
		}
		IOLockUnlock(fLock);
		it->release();
	}
	return kIOReturnSuccess;
}

void
SweetHIDTouch::touch(int finger, bool down, int x, int y)
{
	uint64_t now = mach_absolute_time();
	IOHIDEvent *collection;
	bool any = false;

	if (finger < 0 || finger >= SWEET_TOUCH_FINGERS) {
		return;
	}
	x = x < 0 ? 0 : (x > 1079 ? 1079 : x);
	y = y < 0 ? 0 : (y > 2399 ? 2399 : y);

	IOLockLock(fLock);
	bool was = fDown[finger];
	/* Panel pixels -> the presented 1125x2436 display (SweetFramebuffer.cpp), with sweetfbfit through its scale (and bars). */
	int fit = sweet_fb_fit;
	int top = fit == 1 ? SWEET_FIT_Y : 0, bottom = fit == 1 ? SWEET_FIT_Y + SWEET_FIT_H - 1 : 2399;
	int fy = y < top ? top : (y > bottom ? bottom : y);
	IOFixed nx = fit ? (IOFixed)(((int64_t)x << 16) / SWEET_FIT_W) : (IOFixed)(((int64_t)(x + fOffX) << 16) / fLogicalW);
	IOFixed ny = fit ? (IOFixed)(((int64_t)(fy - top) << 16) / (bottom - top + 1)) : (IOFixed)(((int64_t)(y + fOffY) << 16) / fLogicalH);
	bool moved = down && (nx != fX[finger] || ny != fY[finger]);
	if (was == down && !moved) {
		IOLockUnlock(fLock);
		return;
	}
	if (!was && down) {
		fEdge[finger] = x < SWEET_TOUCH_EDGE_PX || x > 1079 - SWEET_TOUCH_EDGE_PX ||
		    y < top + SWEET_TOUCH_EDGE_PX || y > bottom - SWEET_TOUCH_EDGE_PX;
	}

	collection = IOHIDEvent::digitizerEvent(now, 0, kIOHIDDigitizerTransducerTypeHand, false, 0, 0, 0);
	if (!collection) {
		IOLockUnlock(fLock);
		return;
	}
	collection->setIntegerValue(kIOHIDEventFieldDigitizerCollection, TRUE);

	UInt32 cmask = 0;
	for (int i = 0; i < SWEET_TOUCH_FINGERS; i++) {
		bool d = (i == finger) ? down : fDown[i];
		if (i != finger && !d) {
			continue;
		}
		IOFixed fx = (i == finger && down) ? nx : fX[i];
		IOFixed fy = (i == finger && down) ? ny : fY[i];
		IOHIDEvent *child = IOHIDEvent::digitizerEvent(now, (UInt32)i + 2, kIOHIDDigitizerTransducerTypeFinger,
		    d, d ? 1 : 0, fx, fy);
		if (!child) {
			continue;
		}
		UInt32 mask = fEdge[i] ? kIOHIDDigitizerEventFromEdgeTip : 0;
		if (i == finger) {
			if (was != down) {
				mask |= kIOHIDDigitizerEventTouch | kIOHIDDigitizerEventRange;
			}
			if (moved) {
				mask |= kIOHIDDigitizerEventPosition;
			}
			if (!was && down) {
				mask |= kIOHIDDigitizerEventIdentity;
			}
		}
		child->setIntegerValue(kIOHIDEventFieldDigitizerIndex, i);
		child->setIntegerValue(kIOHIDEventFieldDigitizerIdentity, i + 2);
		child->setIntegerValue(kIOHIDEventFieldDigitizerTouch, d);
		child->setIntegerValue(kIOHIDEventFieldDigitizerEventMask, mask);
		collection->appendChild(child);
		child->release();
		cmask |= mask;
		any |= d;
	}
	fDown[finger] = down;
	if (down) {
		fX[finger] = nx;
		fY[finger] = ny;
	}
	collection->setFixedValue(kIOHIDEventFieldDigitizerX, nx);
	collection->setFixedValue(kIOHIDEventFieldDigitizerY, ny);
	collection->setIntegerValue(kIOHIDEventFieldDigitizerRange, any);
	collection->setIntegerValue(kIOHIDEventFieldDigitizerTouch, any);
	collection->setIntegerValue(kIOHIDEventFieldDigitizerEventMask, cmask);
	collection->setIntegerValue(kIOHIDEventFieldDigitizerButtonMask, any ? 1 : 0);
	IOLockUnlock(fLock);

	if (was != down) {
		IOLog("SWEET HID: touch %d %s at %d,%d%s\n", finger, down ? "down" : "up", x, y,
		    fEdge[finger] ? " edge" : "");
	}
	dispatchEvent(collection);
	collection->release();
}

/*
 * UniXNU/NativeHidInput ba03e5d147452b2166aab7435d219fa10877672b:
 * dispatch a report as one collection. Identifier matching (75efe1c) is
 * already provided by our fixed hardware-ID arrays, with no packed slots.
 * BSD-2-Clause-Patent; notice in LICENSE.NativeHidInput beside this file.
 * Unlike upstream we construct IOHIDEvents directly: there is no HID
 * descriptor/parser Touch Valid bit or DigitizerCollectionDispatch property
 * to set. Explicit Finger children, including lifts, keep their type (22f2dd4).
 * Called only for the opt-in fuxitbatch path; touch() remains the old path.
 * A false return requests a latest-set retry; true is not a queue-delivery ACK.
 */
bool
SweetHIDTouch::report(UInt32 mask, const int *x, const int *y)
{
	mask &= (1u << SWEET_TOUCH_FINGERS) - 1;
	IOFixed nx[SWEET_TOUCH_FINGERS], ny[SWEET_TOUCH_FINGERS];
	bool edge[SWEET_TOUCH_FINGERS];
	UInt32 old = 0, changed = 0;
	uint64_t now = mach_absolute_time();

	IOLockLock(fLock);
	/* As touch(): sweetfbfit maps through the scale (and bars). */
	int fit = sweet_fb_fit;
	int top = fit == 1 ? SWEET_FIT_Y : 0, bottom = fit == 1 ? SWEET_FIT_Y + SWEET_FIT_H - 1 : 2399;
	for (int i = 0; i < SWEET_TOUCH_FINGERS; i++) {
		UInt32 bit = 1u << i;
		bool down = (mask & bit) != 0;
		int px = x[i] < 0 ? 0 : (x[i] > 1079 ? 1079 : x[i]);
		int py = y[i] < 0 ? 0 : (y[i] > 2399 ? 2399 : y[i]);
		int fy = py < top ? top : (py > bottom ? bottom : py);
		old |= fDown[i] ? bit : 0;
		/* A lift keeps the last reported position, not the leave payload. */
		nx[i] = !down ? fX[i] : fit ? (IOFixed)(((int64_t)px << 16) / SWEET_FIT_W) : (IOFixed)(((int64_t)(px + fOffX) << 16) / fLogicalW);
		ny[i] = !down ? fY[i] : fit ? (IOFixed)(((int64_t)(fy - top) << 16) / (bottom - top + 1)) : (IOFixed)(((int64_t)(py + fOffY) << 16) / fLogicalH);
		edge[i] = !fDown[i] && down ?
		    px < SWEET_TOUCH_EDGE_PX || px > 1079 - SWEET_TOUCH_EDGE_PX ||
		    py < top + SWEET_TOUCH_EDGE_PX || py > bottom - SWEET_TOUCH_EDGE_PX : fEdge[i];
		if (fDown[i] != down || (down && (nx[i] != fX[i] || ny[i] != fY[i]))) {
			changed |= bit;
		}
	}
	if (!changed) {
		IOLockUnlock(fLock);
		return true;
	}
	IOHIDEvent *collection = IOHIDEvent::digitizerEvent(now, 0,
	    kIOHIDDigitizerTransducerTypeHand, false, 0, 0, 0);
	if (!collection) {
		IOLockUnlock(fLock);
		return false;
	}
	collection->setIntegerValue(kIOHIDEventFieldDigitizerCollection, TRUE);
	UInt32 cmask = 0;
	unsigned children = 0, positions = 0;
	int64_t sx = 0, sy = 0;
	for (int i = 0; i < SWEET_TOUCH_FINGERS; i++) {
		UInt32 bit = 1u << i;
		if (!((mask | old) & bit)) {
			continue;
		}
		bool down = (mask & bit) != 0, was = (old & bit) != 0;
		IOHIDEvent *child = IOHIDEvent::digitizerEvent(now, (UInt32)i + 2,
		    kIOHIDDigitizerTransducerTypeFinger, down, down ? 1 : 0, nx[i], ny[i]);
		if (!child) {
			collection->release();
			IOLockUnlock(fLock);
			return false; /* Do not publish a partial set or advance its state. */
		}
		UInt32 events = edge[i] ? kIOHIDDigitizerEventFromEdgeTip : 0;
		if (was != down) {
			events |= kIOHIDDigitizerEventTouch | kIOHIDDigitizerEventRange;
		}
		if (down && (nx[i] != fX[i] || ny[i] != fY[i])) {
			events |= kIOHIDDigitizerEventPosition;
		}
		if (!was && down) {
			events |= kIOHIDDigitizerEventIdentity;
		}
		child->setIntegerValue(kIOHIDEventFieldDigitizerIndex, i);
		child->setIntegerValue(kIOHIDEventFieldDigitizerIdentity, i + 2);
		child->setIntegerValue(kIOHIDEventFieldDigitizerTouch, down);
		child->setIntegerValue(kIOHIDEventFieldDigitizerEventMask, events);
		collection->appendChild(child);
		child->release();
		if (!collection->getChildren() || collection->getChildren()->getCount() != ++children) {
			collection->release();
			IOLockUnlock(fLock);
			return false;
		}
		cmask |= events;
		if (down || !mask) {
			sx += nx[i];
			sy += ny[i];
			positions++;
		}
	}
	for (int i = 0; i < SWEET_TOUCH_FINGERS; i++) {
		bool down = (mask & (1u << i)) != 0;
		if (fDown[i] != down) {
			IOLog("SWEET HID: touch %d %s at %d,%d%s\n", i, down ? "down" : "up",
			    x[i], y[i], edge[i] ? " edge" : "");
		}
		fDown[i] = down;
		fEdge[i] = edge[i];
		fX[i] = nx[i];
		fY[i] = ny[i];
	}
	collection->setFixedValue(kIOHIDEventFieldDigitizerX, (IOFixed)(sx / positions));
	collection->setFixedValue(kIOHIDEventFieldDigitizerY, (IOFixed)(sy / positions));
	collection->setIntegerValue(kIOHIDEventFieldDigitizerRange, mask != 0);
	collection->setIntegerValue(kIOHIDEventFieldDigitizerTouch, mask != 0);
	collection->setIntegerValue(kIOHIDEventFieldDigitizerEventMask, cmask);
	collection->setIntegerValue(kIOHIDEventFieldDigitizerButtonMask, mask ? 1 : 0);
	IOLockUnlock(fLock);
	dispatchEvent(collection);
	collection->release();
	return true;
}

/* Time of the latest key or touch report, read by the fuxiautolock thread
 * in bsd/dev/fuxi_keys.c. */
extern "C" { uint64_t sweet_hid_last_input; }

/* key: 0 power (side button), 1 volume up, 2 volume down, 3 home */
extern "C" void
sweet_hid_key_at(int key, int down, uint64_t timestamp)
{
	__atomic_store_n(&sweet_hid_last_input, mach_absolute_time(), __ATOMIC_RELAXED);
	static const UInt32 usages[] = {
		kHIDUsage_Csmr_Power, kHIDUsage_Csmr_VolumeIncrement,
		kHIDUsage_Csmr_VolumeDecrement, kHIDUsage_Csmr_Menu,
	};

	if (gSweetButtons && key >= 0 && key < (int)(sizeof(usages) / sizeof(usages[0]))) {
		gSweetButtons->key(usages[key], down != 0, timestamp);
	}
}

extern "C" void
sweet_hid_key(int key, int down)
{
	sweet_hid_key_at(key, down, mach_absolute_time());
}

extern "C" int
sweet_hid_touch_mode(void)
{
	return gSweetTouchMode;
}

extern "C" void
sweet_hid_touch(int finger, int down, int x, int y)
{
	__atomic_store_n(&sweet_hid_last_input, mach_absolute_time(), __ATOMIC_RELAXED);
	if (gSweetTouch) {
		gSweetTouch->touch(finger, down != 0, x, y);
	}
}

extern "C" int
sweet_hid_touch_report(uint32_t mask, const int *x, const int *y)
{
	__atomic_store_n(&sweet_hid_last_input, mach_absolute_time(), __ATOMIC_RELAXED);
	if (gSweetTouch) {
		return gSweetTouch->report(mask, x, y);
	}
	return 0;
}
