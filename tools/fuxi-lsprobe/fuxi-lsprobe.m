/*
 * fuxi-lsprobe: what LaunchServices tells a process about one application, as FrontBoardServices'
 * FBSApplicationLibrary asks for it when SpringBoard starts.
 *
 *   fuxi-lsprobe [BUNDLE_ID [BUNDLE_PATH]]   (default com.apple.springboard, /System/Library/CoreServices/SpringBoard.app)
 *
 * SpringBoard aborts with "Got nil for _systemAppInfo." when the library has no installed
 * application for its own bundle identifier. The library takes the installed applications from
 * LSApplicationRecord's enumerator (installed records only), and falls back to an
 * LSApplicationProxy for the current process by identifier and by bundle URL; each must give a
 * record that is valid, installed, not launch prohibited and not restricted, with an identity
 * (persona) from which RunningBoard can make a process identity. This program prints those
 * properties for the application at each of the times FUXI_LSPROBE_DELAY lists (seconds after
 * its start, comma separated, default 30), as lsd's store may change while usermanagerd and
 * installd come up. It runs as a launchd job of fuxi-initdisk.
 *
 * Only public Objective-C runtime calls are used: the LaunchServices classes are looked up by name.
 */
#import <Foundation/Foundation.h>
#include <sys/attr.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>

/* LaunchServices' methods used, declared here so that the program needs no private headers */
@interface NSObject (FuxiLaunchServices)
- (id)initWithBundleIdentifier:(NSString *)bundleIdentifier allowPlaceholder:(BOOL)placeholder error:(NSError **)error;
- (id)initWithURL:(NSURL *)url allowPlaceholder:(BOOL)placeholder error:(NSError **)error;
+ (id)applicationProxyForIdentifier:(NSString *)bundleIdentifier;
+ (id)applicationProxyForBundleURL:(NSURL *)url;
+ (id)enumeratorWithOptions:(NSUInteger)options;
+ (id)identityForLSApplicationIdentity:(id)identity LSApplicationRecord:(id)record;
+ (id)defaultWorkspace;
+ (id)coreTypesBundleRecord;
+ (id)typeWithIdentifier:(NSString *)identifier;
- (void)getKnowledgeUUID:(NSUUID **)uuid andSequenceNumber:(NSNumber **)number;
- (id)allInstalledApplications;
- (id)allApplications;
- (void)setErrorHandler:(void (^)(NSError *))handler;
@end

/* LaunchServices' C functions, found with dlsym (unslid addresses of 24A446's dyld cache) */
typedef void (*set_log_file_t)(FILE *);                  /* _LSBindingSetLogFile 0x186e8d3e8 */
typedef void (*set_reasons_t)(bool);                     /* _LSBindingSetReasonTrackingEnabled 0x186e8d3dc */
typedef CFURLRef (*copy_url_for_user_t)(uid_t);          /* _LSDatabaseCopyURLForUser 0x186f02aa4 */

/* the volume as LaunchServices sees it: statfs, getattrlist's identifiers, and NSURL's resource values */
static void
show_volume(const char *path)
{
	struct statfs sf;
	struct {
		uint32_t length;
		fsid_t fsid;
		uint64_t fileid;
	} __attribute__((packed)) ab;
	struct attrlist al = { .bitmapcount = ATTR_BIT_MAP_COUNT, .commonattr = ATTR_CMN_FSID | ATTR_CMN_FILEID };
	NSURL *url = [NSURL fileURLWithPath:@(path)];
	NSDictionary *v;

	if (statfs(path, &sf) == 0) {
		fprintf(stderr, "lsprobe: volume %s: %s on %s from %s, fsid 0x%x:0x%x, flags 0x%x\n", path, sf.f_fstypename,
		    sf.f_mntonname, sf.f_mntfromname, sf.f_fsid.val[0], sf.f_fsid.val[1], sf.f_flags);
	} else {
		fprintf(stderr, "lsprobe: volume %s: statfs: %s\n", path, strerror(errno));
	}
	if (getattrlist(path, &al, &ab, sizeof(ab), 0) == 0) {
		fprintf(stderr, "lsprobe:   getattrlist fsid 0x%x:0x%x fileid %llu\n", ab.fsid.val[0], ab.fsid.val[1],
		    (unsigned long long)ab.fileid);
	} else {
		fprintf(stderr, "lsprobe:   getattrlist: %s\n", strerror(errno));
	}
	v = [url resourceValuesForKeys:@[NSURLVolumeIdentifierKey, NSURLFileResourceIdentifierKey, NSURLVolumeUUIDStringKey,
	    NSURLVolumeURLKey, NSURLVolumeNameKey, NSURLIsPackageKey] error:NULL];
	for (NSString *key in v) {
		fprintf(stderr, "lsprobe:   %s = %s\n", key.UTF8String, [[v[key] description] UTF8String]);
	}
}

/* the store: its file, sequence number, core types and, from _LSDisplayData (lsregister -dump), its size */
static void
show_store(void)
{
	copy_url_for_user_t copy_url = (copy_url_for_user_t)dlsym(RTLD_DEFAULT, "_LSDatabaseCopyURLForUser");
	Class workspace = NSClassFromString(@"LSApplicationWorkspace");
	Class uttype = NSClassFromString(@"UTType");
	NSUUID *uuid = nil;
	NSNumber *seq = nil;
	char path[1024] = "";

	if (copy_url != NULL) {
		CFURLRef u = copy_url(getuid());
		struct stat st;

		if (u != NULL && CFURLGetFileSystemRepresentation(u, true, (UInt8 *)path, sizeof(path))) {
			int r = stat(path, &st);

			fprintf(stderr, "lsprobe: store for uid %d: %s: %s, %lld bytes\n", getuid(), path,
			    r == 0 ? "present" : strerror(errno), r == 0 ? (long long)st.st_size : -1LL);
		}
		if (u != NULL) {
			CFRelease(u);
		}
	}
	@try {
		[[workspace defaultWorkspace] getKnowledgeUUID:&uuid andSequenceNumber:&seq];
		fprintf(stderr, "lsprobe: knowledge uuid %s sequence %s\n", uuid.UUIDString.UTF8String, seq.description.UTF8String);
		fprintf(stderr, "lsprobe: allInstalledApplications %lu, allApplications %lu\n",
		    (unsigned long)[[[workspace defaultWorkspace] allInstalledApplications] count],
		    (unsigned long)[[[workspace defaultWorkspace] allApplications] count]);
	} @catch (NSException *e) {
		fprintf(stderr, "lsprobe: workspace: exception %s\n", e.reason.UTF8String);
	}
	fprintf(stderr, "lsprobe: UTType public.data %s, com.microsoft.windows-executable %s, core types bundle %s\n",
	    [uttype typeWithIdentifier:@"public.data"] != nil ? "present" : "nil",
	    [uttype typeWithIdentifier:@"com.microsoft.windows-executable"] != nil ? "present" : "nil",
	    [[NSClassFromString(@"LSBundleRecord") coreTypesBundleRecord] description].UTF8String);
}

static void
show(NSString *what, id obj, NSArray<NSString *> *keys)
{
	fprintf(stderr, "lsprobe: %s: %s\n", what.UTF8String, obj == nil ? "nil" : [[obj description] UTF8String]);
	for (NSString *key in keys) {
		id v = nil;

		@try {
			v = [obj valueForKeyPath:key];
		} @catch (NSException *e) {
			v = [NSString stringWithFormat:@"<exception %@>", e.reason];
		}
		fprintf(stderr, "lsprobe:   %s = %s\n", key.UTF8String, v == nil ? "nil" : [[v description] UTF8String]);
	}
}

/*
 * The units of each table of this process's store (lsd's database, mapped once lsd has seeded it):
 * _LSContextInit gives the database, whose store is at +0x28; _CSStoreEnumerateTables names the
 * tables and CSStoreGetUnitCount counts them. A store without app bundles has a near empty Bundle table.
 */
typedef int (*context_init_t)(void **);                         /* _LSContextInit 0x186dd6800 */
typedef void (*context_destroy_t)(void **);                     /* _LSContextDestroy 0x186dd5aa0 */
typedef void (*enumerate_tables_t)(void *, void (^)(uint32_t, CFStringRef, const void *, uint32_t, bool *));
typedef uint32_t (*unit_count_t)(void *, uint32_t);

static void
show_tables(void)
{
	context_init_t init = (context_init_t)dlsym(RTLD_DEFAULT, "_LSContextInit");
	context_destroy_t destroy = (context_destroy_t)dlsym(RTLD_DEFAULT, "_LSContextDestroy");
	enumerate_tables_t enumerate = (enumerate_tables_t)dlsym(RTLD_DEFAULT, "_CSStoreEnumerateTables");
	unit_count_t count = (unit_count_t)dlsym(RTLD_DEFAULT, "CSStoreGetUnitCount");
	void *ctx = NULL, *store;
	int r;

	if (init == NULL || destroy == NULL || enumerate == NULL || count == NULL) {
		fprintf(stderr, "lsprobe: tables: missing %s%s%s%s\n", init ? "" : "_LSContextInit ", destroy ? "" : "_LSContextDestroy ",
		    enumerate ? "" : "_CSStoreEnumerateTables ", count ? "" : "CSStoreGetUnitCount");
		return;
	}
	if ((r = init(&ctx)) != 0 || ctx == NULL) {
		fprintf(stderr, "lsprobe: tables: _LSContextInit %d\n", r);
		return;
	}
	store = *(void **)((char *)ctx + 0x28);
	fprintf(stderr, "lsprobe: tables: database %p, store %p\n", ctx, store);
	if (store != NULL) {
		enumerate(store, ^(uint32_t table, CFStringRef name, const void *bytes, uint32_t size, bool *stop) {
			(void)bytes;
			(void)stop;
			fprintf(stderr, "lsprobe: table %s (%u): %u units, %u bytes\n", [(__bridge NSString *)name UTF8String], table,
			    count(store, table), size);
		});
	}
	destroy(&ctx);
}

static void
probe(NSString *bid, NSString *path)
{
	NSArray *recordKeys = @[@"bundleIdentifier", @"URL", @"isPlaceholder", @"applicationState.isValid",
	    @"applicationState.isInstalled", @"applicationState.isRestricted", @"applicationState.isAlwaysAvailable",
	    @"isLaunchProhibited", @"managementDomain", @"identities", @"_personasWithAttributes",
	    @"typeForInstallMachinery", @"compatibilityObject"];
	NSArray *proxyKeys = @[@"bundleIdentifier", @"bundleURL", @"isPlaceholder", @"appState.isValid",
	    @"appState.isInstalled", @"appState.isRestricted", @"isLaunchProhibited", @"applicationType",
	    @"correspondingApplicationRecord"];
	Class record = NSClassFromString(@"LSApplicationRecord");
	Class proxy = NSClassFromString(@"LSApplicationProxy");
	Class rbsIdentity = NSClassFromString(@"RBSProcessIdentity");
	NSError *error = nil;
	id r;

	fprintf(stderr, "lsprobe: pid %d uid %d, %s at %s\n", getpid(), getuid(), bid.UTF8String, path.UTF8String);
	show_store();
	show_tables();
	show_volume("/System/Library/CoreServices/SpringBoard.app");
	show_volume("/System/Library/CoreServices/MobileCoreTypes.bundle");

	r = [[record alloc] initWithBundleIdentifier:bid allowPlaceholder:NO error:&error];
	show(@"record (no placeholder)", r, recordKeys);
	fprintf(stderr, "lsprobe:   error = %s\n", error == nil ? "nil" : error.description.UTF8String);
	/* what FrontBoardServices makes of it: RunningBoard's identity for the record's first identity */
	if (r != nil && rbsIdentity != Nil) {
		id identities = nil;

		@try {
			identities = [r valueForKey:@"identities"];
			for (id identity in identities) {
				id pi = [rbsIdentity identityForLSApplicationIdentity:identity LSApplicationRecord:r];

				fprintf(stderr, "lsprobe:   RBS identity for %s = %s\n", [[identity description] UTF8String],
				    pi == nil ? "nil" : [[pi description] UTF8String]);
			}
		} @catch (NSException *e) {
			fprintf(stderr, "lsprobe:   RBS identity: exception %s\n", e.reason.UTF8String);
		}
		if ([identities count] == 0) {
			fprintf(stderr, "lsprobe:   no identities\n");
		}
	}
	error = nil;
	r = [[record alloc] initWithBundleIdentifier:bid allowPlaceholder:YES error:&error];
	show(@"record (placeholder allowed)", r, recordKeys);
	fprintf(stderr, "lsprobe:   error = %s\n", error == nil ? "nil" : error.description.UTF8String);
	error = nil;
	r = [[record alloc] initWithURL:[NSURL fileURLWithPath:path] allowPlaceholder:YES error:&error];
	show(@"record by URL", r, recordKeys);
	fprintf(stderr, "lsprobe:   error = %s\n", error == nil ? "nil" : error.description.UTF8String);

	show(@"proxy by identifier", [proxy applicationProxyForIdentifier:bid], proxyKeys);
	show(@"proxy by bundle URL", [proxy applicationProxyForBundleURL:[NSURL fileURLWithPath:path]], proxyKeys);

	/* FBSApplicationLibrary's enumeration of the installed applications (options 0x80), and the
	 * same without the restrictions (0x10000 added) */
	for (unsigned k = 0; k < 2; k++) {
		NSUInteger options = k == 0 ? 0x80 : 0x80 | 0x10000;
		unsigned n = 0, found = 0;
		id enumerator = [record enumeratorWithOptions:options];

		/* a record the enumerator drops goes to its error handler; an empty store calls none */
		[enumerator setErrorHandler:^(NSError *x) {
			fprintf(stderr, "lsprobe:   enumerator error: %s\n", x.description.UTF8String);
		}];
		for (id each in enumerator) {
			n++;
			if ([[each valueForKey:@"bundleIdentifier"] isEqual:bid]) {
				found++;
			}
			if (k == 0 && n <= 40) {
				fprintf(stderr, "lsprobe: installed %u: %s\n", n, [[[each valueForKey:@"bundleIdentifier"] description] UTF8String]);
			}
		}
		fprintf(stderr, "lsprobe: enumerator 0x%lx: %u records, %u of them %s\n", (unsigned long)options, n, found, bid.UTF8String);
	}
}

int
main(int argc, char **argv)
{
	@autoreleasepool {
		NSString *bid = argc > 1 ? @(argv[1]) : @"com.apple.springboard";
		NSString *path = argc > 2 ? @(argv[2]) : @"/System/Library/CoreServices/SpringBoard.app";
		const char *delays = getenv("FUXI_LSPROBE_DELAY");
		unsigned waited = 0;

		set_log_file_t set_log_file = (set_log_file_t)dlsym(RTLD_DEFAULT, "_LSBindingSetLogFile");
		set_reasons_t set_reasons = (set_reasons_t)dlsym(RTLD_DEFAULT, "_LSBindingSetReasonTrackingEnabled");

		(void)dlopen("/System/Library/PrivateFrameworks/RunningBoardServices.framework/RunningBoardServices", RTLD_NOW);
		/* the binding evaluator's own trace: its candidates and why each was refused */
		if (set_log_file != NULL) {
			set_log_file(stderr);
		}
		if (set_reasons != NULL) {
			set_reasons(true);
		}
		fprintf(stderr, "lsprobe: binding log %s, reasons %s\n", set_log_file != NULL ? "on" : "missing",
		    set_reasons != NULL ? "on" : "missing");
		for (const char *d = delays != NULL ? delays : "30"; *d != '\0';) {
			unsigned at = (unsigned)strtoul(d, (char **)&d, 10);

			if (at > waited) {
				sleep(at - waited);
				waited = at;
			}
			fprintf(stderr, "lsprobe: at %u s\n", waited);
			@autoreleasepool {
				probe(bid, path);
			}
			while (*d == ',' || *d == ' ') {
				d++;
			}
		}
		fprintf(stderr, "lsprobe: DONE\n");
	}
	return 0;
}
