/*
 * Minimal device-tree platform expert for Redmi Note 10 Pro (sweet).
 * Uses XNU's normal platform, power-domain and nub initialization. Hardware
 * interrupt and power-management services are supplied by separate drivers.
 */
#include <pexpert/arm64/board_config.h>

#if defined(SWEET)
#include <IOKit/IOPlatformExpert.h>
#include <IOKit/IODeviceTreeSupport.h>
#include <IOKit/IOLib.h>
#include <kern/clock.h>
#include <pexpert/pexpert.h>

class SweetPlatformExpert : public IODTPlatformExpert {
	OSDeclareDefaultStructors(SweetPlatformExpert);

public:
	bool start(IOService *provider) APPLE_KEXT_OVERRIDE;
	void processTopLevel(IORegistryEntry *root) APPLE_KEXT_OVERRIDE;
	const char *deleteList(void) APPLE_KEXT_OVERRIDE;
	const char *excludeList(void) APPLE_KEXT_OVERRIDE;
	long getGMTTimeOfDay(void) APPLE_KEXT_OVERRIDE;
	void setGMTTimeOfDay(long secs) APPLE_KEXT_OVERRIDE;
};

OSDefineMetaClassAndStructors(SweetPlatformExpert, IODTPlatformExpert);

/*
 * The time of day. The phone's real-time clock (in the PMK8550) is not driven,
 * so whoever starts the kernel says what time it is: the boot-arg
 * epoch=<seconds since 1970, UTC> or, failing that, /chosen boot-epoch (8 bytes,
 * little-endian), which a host script can put into the ADT without spending
 * boot-line bytes. It is taken as the time at which this platform expert starts
 * (about a second into the kernel), so a host that stamps it just before
 * `fastboot boot` leaves the calendar behind by the upload. The system timer
 * counts on from there; settimeofday (timed, date) moves the base until the
 * next boot. Without either the calendar starts in 1970, as before.
 * After HakiOS's findBootEpoch (oosexgang/hakios-xnu-kernel, GenericARMPE.cpp).
 */
static long fuxi_boot_epoch;            /* calendar seconds at which the system timer read zero */
static bool fuxi_boot_epoch_known;

static void
fuxi_find_boot_epoch(void)
{
	uint64_t epoch = 0;
	const char *from = "epoch=";
	clock_sec_t secs;
	clock_usec_t usecs;

	if (fuxi_boot_epoch_known) {
		return;
	}
	fuxi_boot_epoch_known = true;
	if (!PE_parse_boot_argn("epoch", &epoch, sizeof(epoch)) || epoch == 0) {
		IORegistryEntry *chosen = IORegistryEntry::fromPath("/chosen", gIODTPlane);
		OSData *data = chosen ? OSDynamicCast(OSData, chosen->getProperty("boot-epoch")) : NULL;

		epoch = 0;
		if (data != NULL && data->getLength() == sizeof(epoch)) {
			memcpy(&epoch, data->getBytesNoCopy(), sizeof(epoch));
		}
		OSSafeReleaseNULL(chosen);
		from = "/chosen boot-epoch";
	}
	clock_get_system_microtime(&secs, &usecs);
	if (epoch <= (uint64_t)secs) {
		IOLog("FUXI CLOCK: no epoch (boot-arg epoch= or /chosen boot-epoch): the calendar starts at 1970\n");
		return;
	}
	fuxi_boot_epoch = (long)epoch - (long)secs;
	IOLog("FUXI CLOCK: %s %llu at uptime %lu.%06u s, so the timer's zero is %ld\n",
	    from, epoch, (unsigned long)secs, (unsigned)usecs, fuxi_boot_epoch);
}

long
SweetPlatformExpert::getGMTTimeOfDay(void)
{
	static bool logged;
	clock_sec_t secs;
	clock_usec_t usecs;

	fuxi_find_boot_epoch();
	if (fuxi_boot_epoch == 0) {
		return 0;
	}
	clock_get_system_microtime(&secs, &usecs);
	if (!logged) {
		logged = true;
		IOLog("FUXI CLOCK: the calendar reads %ld at uptime %lu s\n", fuxi_boot_epoch + (long)secs, (unsigned long)secs);
	}
	return fuxi_boot_epoch + (long)secs;
}

void
SweetPlatformExpert::setGMTTimeOfDay(long secs)
{
	static SInt32 logged;
	clock_sec_t uptime;
	clock_usec_t usecs;
	long before = fuxi_boot_epoch;

	clock_get_system_microtime(&uptime, &usecs);
	fuxi_boot_epoch = secs - (long)uptime;
	fuxi_boot_epoch_known = true;
	if (OSIncrementAtomic(&logged) < 8) {
		IOLog("FUXI CLOCK: set to %ld at uptime %lu s, %ld s from the base before\n",
		    secs, (unsigned long)uptime, before != 0 ? fuxi_boot_epoch - before : 0);
	}
}

bool
SweetPlatformExpert::start(IOService *provider)
{
	IOLog("SWEET: starting device-tree platform expert\n");
	if (!IODTPlatformExpert::start(provider)) {
		return false;
	}
	fuxi_find_boot_epoch();
	registerService();
	IOLog("SWEET: platform expert registered\n");
	return true;
}

const char *
SweetPlatformExpert::deleteList(void)
{
	return "()";
}

const char *
SweetPlatformExpert::excludeList(void)
{
	return "('chosen', 'defaults', 'cpus', 'options')";
}

void
SweetPlatformExpert::processTopLevel(IORegistryEntry *root)
{
	/*
	 * The stock implementation requires an Apple NVRAM /options node.
	 * SWEET has no NVRAM controller; publish real CPU/device nodes without
	 * inventing one. createNubs adds the topology's logical CPU/cluster IDs.
	 */
	IORegistryEntry *cpus = root->childFromPath("cpus", gIODTPlane);
	if (cpus != NULL) {
		createNubs(this, IODTFindMatchingEntries(cpus, kIODTExclusive, NULL));
		cpus->release();
	}
	createNubs(this, IODTFindMatchingEntries(root, kIODTExclusive, excludeList()));

	/*
	 * No AppleARMIO driver publishes the /arm-io children: do it here so
	 * built-in drivers (the display, AppleCLCD) can match them. The GIC is
	 * driven by the platform code and gets no nub. The nubs hang off this
	 * platform expert: IOPlatformDevice::compareName asks its provider.
	 */
	IORegistryEntry *armio = root->childFromPath("arm-io", gIODTPlane);
	if (armio != NULL) {
		createNubs(this, IODTFindMatchingEntries(armio, kIODTExclusive, "('gic')"));
		armio->release();
	}
}

/*
 * hw.target / hw.product come from IODTPlatformExpert: the first root
 * "compatible" string and "model" (D22AP / iPhone10,3 in the ADT), which
 * is what the iPhone userland (MobileGestalt) expects.
 */
#endif /* SWEET */
