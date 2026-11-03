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
#include <sys/fsgetpath.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <dlfcn.h>
#include <errno.h>
#include <ptrauth.h>
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
- (id)initWithURL:(NSURL *)url flags:(NSUInteger)flags error:(NSError **)error;
- (BOOL)isVolume;
- (BOOL)isDirectory;
- (BOOL)isResolvable;
- (NSString *)nameWithError:(NSError **)error;
- (NSString *)extensionWithError:(NSError **)error;
- (NSString *)canonicalPathWithError:(NSError **)error;
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
	/* and back from the file id to the path, as LaunchServices' FSNode does (fsgetpath, file reference URLs) */
	if (getattrlist(path, &al, &ab, sizeof(ab), 0) == 0) {
		fsid_t ids[2] = { ab.fsid, { { ab.fsid.val[0], 0 } } };

		for (int k = 0; k < 2; k++) {
			char back[1024];
			ssize_t n = fsgetpath(back, sizeof(back), &ids[k], ab.fileid);

			fprintf(stderr, "lsprobe:   fsgetpath(0x%x:0x%x, %llu) = %s\n", ids[k].val[0], ids[k].val[1],
			    (unsigned long long)ab.fileid, n >= 0 ? back : strerror(errno));
		}
	}
	{
		NSURL *ref = [url fileReferenceURL];
		NSURL *back = [ref filePathURL];

		fprintf(stderr, "lsprobe:   fileReferenceURL %s -> filePathURL %s\n", ref.absoluteString.UTF8String,
		    back == nil ? "nil" : back.path.UTF8String);
	}
	v = [url resourceValuesForKeys:@[NSURLVolumeIdentifierKey, NSURLFileResourceIdentifierKey, NSURLVolumeUUIDStringKey,
	    NSURLVolumeURLKey, NSURLVolumeNameKey, NSURLIsPackageKey] error:NULL];
	for (NSString *key in v) {
		fprintf(stderr, "lsprobe:   %s = %s\n", key.UTF8String, [[v[key] description] UTF8String]);
	}
}

/*
 * How LaunchServices' registration sees a bundle (each step of _LSRegisterBundleNode): getattrlist's name and
 * mount status (the bundle class comes from the name's extension; a mount point is class 0), the attribute set
 * CoreServicesInternal asks for, FSNode's getters, _LSGetBundleClassForNode and, with register set, a
 * registration in this process (_LSRegisterBundleURL), whose error names the step that fails.
 */
typedef int (*context_init_t)(void **);                         /* _LSContextInit 0x186dd6800 */
typedef void (*context_destroy_t)(void **);                     /* _LSContextDestroy 0x186dd5aa0 */
typedef uint32_t (*bundle_class_t)(void *, id);                                 /* _LSGetBundleClassForNode 0x186de6958 */
typedef BOOL (*register_url_t)(void *, NSURL *, uint32_t, id, NSError **);      /* _LSRegisterBundleURL 0x186f368b0 */

static void *
ls_function(uintptr_t unslid)
{
	void *anchor = dlsym(RTLD_DEFAULT, "_LSBindingSetLogFile");

	if (anchor == NULL) {
		return NULL;
	}
	return ptrauth_sign_unauthenticated((void *)(unslid + (uintptr_t)ptrauth_strip(anchor, ptrauth_key_function_pointer) -
	    0x186e8d3e8), ptrauth_key_function_pointer, 0);
}

static void
show_registration(const char *path, bool reg)
{
	struct {
		uint32_t length;
		attrreference_t name;
		uint32_t mntstatus;
		char buf[MAXPATHLEN];
	} __attribute__((packed)) nb;
	struct attrlist al = { .bitmapcount = ATTR_BIT_MAP_COUNT, .commonattr = ATTR_CMN_NAME, .dirattr = ATTR_DIR_MOUNTSTATUS };
	/* CoreServicesInternal's group 0 (common 0xd01f9e0d, dir 5, file 1, extended common 0x240), options 0x25 */
	struct attrlist csi = { .bitmapcount = ATTR_BIT_MAP_COUNT, .commonattr = 0xd01f9e0d, .dirattr = 0x5, .fileattr = 0x1,
		                .forkattr = 0x240 };
	static char big[8192];
	NSURL *url = [NSURL fileURLWithPath:@(path)];
	Class fsnode = NSClassFromString(@"FSNode");
	bundle_class_t bundle_class = (bundle_class_t)ls_function(0x186de6958);
	register_url_t register_url = (register_url_t)ls_function(0x186f368b0);
	context_init_t init = (context_init_t)ls_function(0x186dd6800);
	context_destroy_t destroy = (context_destroy_t)ls_function(0x186dd5aa0);
	NSError *e = nil;
	id node;

	if (getattrlist(path, &al, &nb, sizeof(nb), 0) == 0) {
		fprintf(stderr, "lsprobe: registration %s: getattrlist name '%.*s' (length %u), mount status 0x%x\n", path,
		    (int)nb.name.attr_length, (char *)&nb.name + nb.name.attr_dataoffset, nb.name.attr_length, nb.mntstatus);
	} else {
		fprintf(stderr, "lsprobe: registration %s: getattrlist name: %s\n", path, strerror(errno));
	}
	for (int k = 0; k < 4; k++) {
		static const uint32_t more[4] = { 0, 0x06000000, 0x08000000, 0x4000 };      /* groups 0, 5, 7, 2 */
		struct attrlist a = csi;
		int r;

		a.commonattr |= more[k];
		if (k == 1) {
			a.forkattr |= 0x110;
		}
		r = getattrlist(path, &a, big, sizeof(big), 0x25);
		fprintf(stderr, "lsprobe:   getattrlist CoreServices group %d (common 0x%x): %s\n", k == 0 ? 0 : k == 1 ? 5 : k == 2 ? 7 : 2,
		    a.commonattr, r == 0 ? "ok" : strerror(errno));
	}
	node = fsnode == nil ? nil : [[fsnode alloc] initWithURL:url flags:0 error:&e];
	if (node == nil) {
		fprintf(stderr, "lsprobe:   FSNode: %s\n", fsnode == nil ? "no class" : e.description.UTF8String);
		return;
	}
	{
		NSError *e1 = nil, *e2 = nil, *e3 = nil;
		NSString *name = [node nameWithError:&e1], *ext = [node extensionWithError:&e2], *canon = [node canonicalPathWithError:&e3];

		fprintf(stderr, "lsprobe:   FSNode volume %d directory %d resolvable %d name %s ext %s canonical %s\n", [node isVolume],
		    [node isDirectory], [node isResolvable], name ? name.UTF8String : e1.description.UTF8String,
		    ext ? ext.UTF8String : e2.description.UTF8String, canon ? canon.UTF8String : e3.description.UTF8String);
	}
	if (bundle_class != NULL) {
		fprintf(stderr, "lsprobe:   _LSGetBundleClassForNode = %u (2, 11, 13, 14, 16 may register)\n", bundle_class(NULL, node));
	}
	/* with a context of its own: given NULL, _LSRegisterBundleNode's context has no database (-10817) */
	if (reg && register_url != NULL && init != NULL && destroy != NULL) {
		NSError *re = nil;
		void *ctx = NULL;
		int r = init(&ctx);
		BOOL ok = r == 0 && ctx != NULL ? register_url(&ctx, url, 0x18000001, nil, &re) : NO;

		fprintf(stderr, "lsprobe:   _LSRegisterBundleURL (context %d) = %d %s\n", r, ok, re == nil ? "" : re.description.UTF8String);
		if (re.userInfo != nil) {
			fprintf(stderr, "lsprobe:   error userInfo %s\n", re.userInfo.description.UTF8String);
		}
		if (ctx != NULL) {
			destroy(&ctx);
		}
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
typedef void (*enumerate_tables_t)(void *, void (^)(uint32_t, CFStringRef, const void *, uint32_t, bool *));
typedef uint32_t (*unit_count_t)(void *, uint32_t);

static void
show_tables(void)
{
	/*
	 * _LSContextInit and _LSContextDestroy are not exported: found from the slide of an exported
	 * neighbour (_LSBindingSetLogFile, 0x186e8d3e8), for build 24A446 only
	 */
	void *anchor = dlsym(RTLD_DEFAULT, "_LSBindingSetLogFile");
	uintptr_t slide = anchor != NULL ? (uintptr_t)ptrauth_strip(anchor, ptrauth_key_function_pointer) - 0x186e8d3e8 : 0;
	context_init_t init = anchor == NULL ? NULL :
	    (context_init_t)ptrauth_sign_unauthenticated((void *)(0x186dd6800 + slide), ptrauth_key_function_pointer, 0);
	context_destroy_t destroy = anchor == NULL ? NULL :
	    (context_destroy_t)ptrauth_sign_unauthenticated((void *)(0x186dd5aa0 + slide), ptrauth_key_function_pointer, 0);
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
	fprintf(stderr, "lsprobe: tables: slide 0x%lx, database %p, store %p\n", (unsigned long)slide, ctx, store);
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
	show_registration("/System/Library/CoreServices/MobileCoreTypes.bundle", getenv("FUXI_LSPROBE_REGISTER") != NULL);
	show_registration("/System/Library/CoreServices/SpringBoard.app", getenv("FUXI_LSPROBE_REGISTER") != NULL);

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
