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
 * record that is valid, installed, not launch prohibited and not restricted. This program
 * prints those properties for the application, after waiting DELAY seconds (FUXI_LSPROBE_DELAY,
 * default 30) for lsd to have made its store. It runs as a launchd job of fuxi-initdisk.
 *
 * Only public Objective-C runtime calls are used: the LaunchServices classes are looked up by name.
 */
#import <Foundation/Foundation.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

/* LaunchServices' methods used, declared here so that the program needs no private headers */
@interface NSObject (FuxiLaunchServices)
- (id)initWithBundleIdentifier:(NSString *)bundleIdentifier allowPlaceholder:(BOOL)placeholder error:(NSError **)error;
- (id)initWithURL:(NSURL *)url allowPlaceholder:(BOOL)placeholder error:(NSError **)error;
+ (id)applicationProxyForIdentifier:(NSString *)bundleIdentifier;
+ (id)applicationProxyForBundleURL:(NSURL *)url;
+ (id)enumeratorWithOptions:(NSUInteger)options;
@end

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

int
main(int argc, char **argv)
{
	@autoreleasepool {
		NSString *bid = argc > 1 ? @(argv[1]) : @"com.apple.springboard";
		NSString *path = argc > 2 ? @(argv[2]) : @"/System/Library/CoreServices/SpringBoard.app";
		const char *delay = getenv("FUXI_LSPROBE_DELAY");
		NSArray *recordKeys = @[@"bundleIdentifier", @"URL", @"isPlaceholder", @"applicationState.isValid",
		    @"applicationState.isInstalled", @"applicationState.isRestricted", @"isLaunchProhibited",
		    @"managementDomain", @"identities", @"typeForInstallMachinery", @"compatibilityObject"];
		NSArray *proxyKeys = @[@"bundleIdentifier", @"bundleURL", @"isPlaceholder", @"appState.isValid",
		    @"appState.isInstalled", @"appState.isRestricted", @"isLaunchProhibited", @"applicationType",
		    @"correspondingApplicationRecord"];
		Class record = NSClassFromString(@"LSApplicationRecord");
		Class proxy = NSClassFromString(@"LSApplicationProxy");
		NSError *error = nil;
		id r;
		unsigned n = 0, found = 0;

		sleep(delay != NULL ? (unsigned)atoi(delay) : 30);
		fprintf(stderr, "lsprobe: pid %d uid %d, %s at %s\n", getpid(), getuid(), bid.UTF8String, path.UTF8String);

		r = [[record alloc] initWithBundleIdentifier:bid allowPlaceholder:NO error:&error];
		show(@"record (no placeholder)", r, recordKeys);
		fprintf(stderr, "lsprobe:   error = %s\n", error == nil ? "nil" : error.description.UTF8String);
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

		/* FBSApplicationLibrary's enumeration of the installed applications (options 0x80) */
		for (id each in [record enumeratorWithOptions:0x80]) {
			n++;
			if ([[each valueForKey:@"bundleIdentifier"] isEqual:bid]) {
				found++;
			}
			if (n <= 40) {
				fprintf(stderr, "lsprobe: installed %u: %s\n", n, [[[each valueForKey:@"bundleIdentifier"] description] UTF8String]);
			}
		}
		fprintf(stderr, "lsprobe: enumerator 0x80: %u records, %u of them %s\n", n, found, bid.UTF8String);
		fprintf(stderr, "lsprobe: DONE\n");
	}
	return 0;
}
