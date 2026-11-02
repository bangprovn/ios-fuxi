/*
 * fuxi-init: process 1 of the iOS 27 boot on Fuxi, until it turns into Apple's launchd.
 *
 * The kernel (fuxi/0021, boot-args fuxi_initdisk=1 fuxi_init=/fuxi/init) mounts the boot payload's
 * RAM disk read only at /fuxi and starts this program from it. The iOS root is the IPSW system
 * volume, read only, with no Data volume, so launchd would find /private/var read only and
 * every one of iOS's ~660 LaunchDaemons. Before exec'ing /sbin/launchd this program:
 *
 *   1. binds the read-only /private/var of the system volume at /fuxi/mnt/var;
 *   2. mounts a memory filesystem (ramfs) on /private/var and recreates the system volume's
 *      /private/var skeleton in it: every directory and symlink with its mode and owner, and
 *      the files up to FILE_LIMIT bytes;
 *   3. binds the big read-only subtrees (BIND_SUBTREES, e.g. the apps iOS installs on its
 *      first boot) back in place instead of copying them;
 *   4. binds the disk's own job set over /System/Library/LaunchDaemons, an empty directory
 *      over LaunchAngels and NanoLaunchDaemons, and the disk's xpc directory (without
 *      launchd.plist) over /System/Library/xpc;
 *   5. merges the OS cryptex's /usr/lib into /usr/lib (merge_dir) and makes the system proxy
 *      personas launchd asks for (personas);
 *   6. leaves a child that copies the non-root jobs' output (JOB_LOGS) to the console.
 *
 * The approach -- a process 1 of our own that prepares a writable /private/var and a chosen
 * job set and then execs launchd -- is that of HakiOS / hakios-arm64e-dada (userland init.c
 * and mkjobs.py, APSL 2.0); this file is written for Fuxi and shares no code with them.
 *
 * Every step prints one line "fuxi-init: ..." on the console. A failed step is reported and
 * the boot goes on: launchd is exec'd in any case, so the next failure is launchd's own.
 */
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/param.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <unistd.h>

#define DISK            "/fuxi"
#define RO_VAR          DISK "/mnt/var"
#define FILE_LIMIT      (1024 * 1024)
#define JOB_LOGS        "/private/var/fuxi/log"
#define MAX_JOB_LOGS    512

/* Subtrees of /private/var bound read only from the system volume instead of copied. */
static const char *const BIND_SUBTREES[] = {
	"staged_system_apps",
	"MobileAsset/PreinstalledAssetsV2",
};

/* logd takes a log directory without these markers for one left by an older system. */
static const char *const LOG_MARKERS[] = {
	"/private/var/db/diagnostics/.migrated",
	"/private/var/db/diagnostics/timesync/.migrated",
	"/private/var/db/uuidtext/.migrated",
};

/* bsd/sys/persona.h (xnu, APSL): private, so not in the SDK. */
struct kpersona_info {
	uint32_t persona_info_version;
	uid_t persona_id;
	int persona_type;
	gid_t persona_gid;
	uint32_t persona_ngroups;
	gid_t persona_groups[NGROUPS];
	uid_t persona_gmuid;
	char persona_name[MAXLOGNAME + 1];
	uid_t persona_uid;
} __attribute__((packed));
_Static_assert(sizeof(struct kpersona_info) == 348, "kpersona_info");
#define PERSONA_INFO_V2         2
#define PERSONA_SYSTEM          4
#define PERSONA_SYSTEM_PROXY    6
int kpersona_alloc(struct kpersona_info *info, uid_t *id);

static struct {
	unsigned dirs, links, files, skipped, errors;
	unsigned long long bytes, skipped_bytes;
} count;

static void
say(const char *format, ...)
{
	char line[512];
	va_list ap;
	int n;

	strcpy(line, "fuxi-init: ");
	va_start(ap, format);
	n = vsnprintf(line + 11, sizeof(line) - 12, format, ap);
	va_end(ap);
	n = n < 0 ? 11 : (n > (int)sizeof(line) - 12 ? (int)sizeof(line) - 1 : n + 11);
	line[n++] = '\n';
	(void)write(2, line, (size_t)n);
}

static void
console(void)
{
	int fd = open("/dev/console", O_RDWR | O_NOCTTY);

	if (fd >= 0) {
		dup2(fd, 0);
		dup2(fd, 1);
		dup2(fd, 2);
		if (fd > 2) {
			close(fd);
		}
	}
}

static unsigned long long
now_ms(void)
{
	struct timeval tv;

	gettimeofday(&tv, NULL);
	return (unsigned long long)tv.tv_sec * 1000 + (unsigned long long)tv.tv_usec / 1000;
}

static int
bind_ro(const char *from, const char *to)
{
	int ok = mount("bindfs", to, MNT_RDONLY, (void *)from) == 0;

	say("bind %s on %s: %s", from, to, ok ? "ok" : strerror(errno));
	return ok;
}

static int
bound_subtree(const char *rel)
{
	for (unsigned i = 0; i < sizeof(BIND_SUBTREES) / sizeof(BIND_SUBTREES[0]); i++) {
		if (strcmp(rel, BIND_SUBTREES[i]) == 0) {
			return 1;
		}
	}
	return 0;
}

static void
copy_file(const char *from, const char *to, const struct stat *st)
{
	static char buf[65536];
	int in, out;
	ssize_t n = 0;

	if (st->st_size > FILE_LIMIT) {
		count.skipped++;
		count.skipped_bytes += (unsigned long long)st->st_size;
		return;
	}
	in = open(from, O_RDONLY);
	out = in >= 0 ? open(to, O_WRONLY | O_CREAT | O_EXCL, st->st_mode & 07777) : -1;
	while (out >= 0 && (n = read(in, buf, sizeof(buf))) > 0) {
		if (write(out, buf, (size_t)n) != n) {
			n = -1;
			break;
		}
	}
	if (in < 0 || out < 0 || n < 0 || fchown(out, st->st_uid, st->st_gid) != 0 || fchmod(out, st->st_mode & 07777) != 0) {
		if (count.errors++ < 20) {
			say("copy %s: %s", to, strerror(errno));
		}
	} else {
		count.files++;
		count.bytes += (unsigned long long)st->st_size;
	}
	if (in >= 0) {
		close(in);
	}
	if (out >= 0) {
		close(out);
	}
}

/* Recreate RO_VAR/rel at /private/var/rel (rel "" is the top), recursively. */
static void
copy_tree(const char *rel)
{
	char from[1024], to[1024];
	DIR *dir;
	struct dirent *e;
	struct stat st;

	snprintf(from, sizeof(from), "%s%s%s", RO_VAR, *rel ? "/" : "", rel);
	if ((dir = opendir(from)) == NULL) {
		if (count.errors++ < 20) {
			say("opendir %s: %s", from, strerror(errno));
		}
		return;
	}
	while ((e = readdir(dir)) != NULL) {
		char sub[1024];

		if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0) {
			continue;
		}
		snprintf(sub, sizeof(sub), "%s%s%s", rel, *rel ? "/" : "", e->d_name);
		snprintf(from, sizeof(from), "%s/%s", RO_VAR, sub);
		snprintf(to, sizeof(to), "/private/var/%s", sub);
		if (lstat(from, &st) != 0) {
			continue;
		}
		if (S_ISDIR(st.st_mode)) {
			if ((mkdir(to, st.st_mode & 07777) != 0 && errno != EEXIST) || chown(to, st.st_uid, st.st_gid) != 0 ||
			    chmod(to, st.st_mode & 07777) != 0) {
				if (count.errors++ < 20) {
					say("mkdir %s: %s", to, strerror(errno));
				}
				continue;
			}
			count.dirs++;
			if (bound_subtree(sub)) {
				bind_ro(from, to);
			} else {
				copy_tree(sub);
			}
		} else if (S_ISLNK(st.st_mode)) {
			char target[1024];
			ssize_t n = readlink(from, target, sizeof(target) - 1);

			if (n >= 0) {
				target[n] = 0;
				if (symlink(target, to) == 0) {
					lchown(to, st.st_uid, st.st_gid);
					count.links++;
				}
			}
		} else if (S_ISREG(st.st_mode)) {
			copy_file(from, to, &st);
		}
	}
	closedir(dir);
}

static void
writable_var(void)
{
	unsigned long long t0 = now_ms();
	struct stat st;

	if (!bind_ro("/private/var", RO_VAR)) {
		return;
	}
	if (mount("ramfs", "/private/var", 0, NULL) != 0) {
		say("mount ramfs on /private/var: %s", strerror(errno));
		return;
	}
	if (stat(RO_VAR, &st) == 0) {
		chown("/private/var", st.st_uid, st.st_gid);
		chmod("/private/var", st.st_mode & 07777);
	}
	copy_tree("");
	for (unsigned i = 0; i < sizeof(LOG_MARKERS) / sizeof(LOG_MARKERS[0]); i++) {
		int fd = open(LOG_MARKERS[i], O_WRONLY | O_CREAT, 0644);

		if (fd >= 0) {
			fchown(fd, 272, 272);	/* _analyticsd, the owner of iOS's log directories */
			close(fd);
		}
	}
	/*
	 * Directories iOS makes at boot that the system volume's /private/var has not got:
	 * /private/var/keybags holds keybagd's system bag and usermanagerd's user sessions and
	 * persona manifest (every create there failed with ENOENT, and usermanagerd aborted).
	 */
	if (mkdir("/private/var/keybags", 0700) != 0) {
		say("mkdir /private/var/keybags: %s", strerror(errno));
	}
	say("/private/var on ramfs: %u dirs %u links %u files (%llu KB), %u files over %d KB skipped (%llu KB), "
	    "%u errors, %llu ms", count.dirs, count.links, count.files, count.bytes >> 10, count.skipped, FILE_LIMIT >> 10,
	    count.skipped_bytes >> 10, count.errors, now_ms() - t0);
}

/*
 * dyld searches the OS cryptex as well only when libignition hands it the cryptex root, and
 * dyld_flags=8 skips libignition (whose image4 sysctl blocks on this kernel). The files that are
 * only in the cryptex (libobjc-trampolines, the objc message-send variants, ...) are then not
 * found. dir becomes a directory on ramfs of symlinks: to every entry of the original (bound at
 * ro) and to every entry of each extra directory that the original lacks.
 */
static void
merge_dir(const char *dir, const char *ro, const char *const *extra, const char *merged)
{
	char from[1024], to[1024];
	unsigned own = 0, added = 0;
	DIR *d;
	struct dirent *e;
	struct stat st;

	if (!bind_ro(dir, ro) || mkdir(merged, 0755) != 0) {
		say("merge %s: %s", dir, strerror(errno));
		return;
	}
	for (int pass = 0; pass == 0 || extra[pass - 1] != NULL; pass++) {
		const char *src = pass == 0 ? ro : extra[pass - 1];

		if ((d = opendir(src)) == NULL) {
			say("merge %s: opendir %s: %s", dir, src, strerror(errno));
			continue;
		}
		while ((e = readdir(d)) != NULL) {
			if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0) {
				continue;
			}
			snprintf(from, sizeof(from), "%s/%s", src, e->d_name);
			snprintf(to, sizeof(to), "%s/%s", merged, e->d_name);
			if (lstat(to, &st) == 0) {
				continue;
			}
			if (symlink(from, to) != 0) {
				say("merge %s: symlink %s: %s", dir, to, strerror(errno));
				continue;
			}
			if (pass == 0) {
				own++;
			} else {
				added++;
			}
		}
		closedir(d);
	}
	say("merge %s: %u entries of its own, %u from the cryptexes", dir, own, added);
	if (added > 0) {
		bind_ro(merged, dir);
	}
}

static void
cryptex_files(void)
{
	static const char *const usr_lib[] = { "/System/Cryptexes/OS/usr/lib", NULL };

	if (mkdir("/private/var/fuxi", 0755) != 0) {
		say("mkdir /private/var/fuxi: %s", strerror(errno));
		return;
	}
	merge_dir("/usr/lib", DISK "/mnt/usrlib", usr_lib, "/private/var/fuxi/usr-lib");
}

/*
 * launchd looks up personas for the jobs it spawns: the system domain a system proxy persona
 * (type 6) with no user ("No matching personas of type 6 uid -1"), XPC services of mobile's
 * processes one with uid 501, user/501 a system persona (type 4). usermanagerd creates the personas from a manifest it keeps with AppleKeyStore, which
 * Fuxi has not got, so this program makes them.
 */
static void
personas(void)
{
	static const struct {
		const char *name;
		int type;
		uid_t uid;
	} proxies[] = {
		{ "fuxi.proxy", PERSONA_SYSTEM_PROXY, (uid_t)-1 },
		{ "fuxi.proxy.mobile", PERSONA_SYSTEM_PROXY, 501 },
		{ "fuxi.system.mobile", PERSONA_SYSTEM, 501 },
	};

	for (unsigned i = 0; i < sizeof(proxies) / sizeof(proxies[0]); i++) {
		struct kpersona_info info;
		uid_t id = 0;

		memset(&info, 0, sizeof(info));
		info.persona_info_version = PERSONA_INFO_V2;
		info.persona_id = (uid_t)-1;
		info.persona_type = proxies[i].type;
		info.persona_uid = proxies[i].uid;
		strlcpy(info.persona_name, proxies[i].name, sizeof(info.persona_name));
		if (kpersona_alloc(&info, &id) == 0) {
			say("persona %s (type %d, uid %d): id %u", proxies[i].name, proxies[i].type, (int)proxies[i].uid, id);
		} else {
			say("persona %s: %s", proxies[i].name, strerror(errno));
		}
	}
}

/*
 * Only root may open the console (xpcproxy gets EACCES for any other user), so fuxi-initdisk
 * points the other jobs' standard output and error at JOB_LOGS/<label>.log. A child of this
 * process, which stays when the parent becomes launchd, copies what they write to the console,
 * each chunk headed by the file name.
 */
static void
log_relay(void)
{
	static struct {
		char name[256];
		off_t done;
	} seen[MAX_JOB_LOGS];
	static char buf[4096];
	unsigned nseen = 0;

	if (mkdir(JOB_LOGS, 01777) != 0 || chmod(JOB_LOGS, 01777) != 0) {
		say("mkdir %s: %s", JOB_LOGS, strerror(errno));
		return;
	}
	if (fork() != 0) {
		return;
	}
	for (;;) {
		DIR *d = opendir(JOB_LOGS);
		struct dirent *e;

		while (d != NULL && (e = readdir(d)) != NULL) {
			char path[1024];
			unsigned i;
			ssize_t n;
			int fd;

			if (e->d_name[0] == '.') {
				continue;
			}
			for (i = 0; i < nseen && strcmp(seen[i].name, e->d_name) != 0; i++) {
			}
			if (i == nseen) {
				if (nseen == MAX_JOB_LOGS) {
					continue;
				}
				strlcpy(seen[nseen++].name, e->d_name, sizeof(seen[0].name));
			}
			snprintf(path, sizeof(path), "%s/%s", JOB_LOGS, e->d_name);
			if ((fd = open(path, O_RDONLY)) < 0) {
				continue;
			}
			while ((n = pread(fd, buf, sizeof(buf), seen[i].done)) > 0) {
				dprintf(2, "fuxi-log %s: ", e->d_name);
				(void)write(2, buf, (size_t)n);
				if (buf[n - 1] != '\n') {
					(void)write(2, "\n", 1);
				}
				seen[i].done += n;
			}
			close(fd);
		}
		if (d != NULL) {
			closedir(d);
		}
		usleep(500 * 1000);
	}
}

static void
job_set(void)
{
	bind_ro(DISK "/jobs/LaunchDaemons", "/System/Library/LaunchDaemons");
	bind_ro(DISK "/jobs/empty", "/System/Library/LaunchAngels");
	bind_ro(DISK "/jobs/empty", "/System/Library/NanoLaunchDaemons");
	bind_ro(DISK "/jobs/xpc", "/System/Library/xpc");
}

int
main(int argc, char **argv, char **envp)
{
	static char *const launchd_argv[] = { "/sbin/launchd", NULL };

	(void)argc;
	(void)argv;
	console();
	say("pid %d, preparing /private/var and the job set", getpid());
	writable_var();
	cryptex_files();
	personas();
	log_relay();
	job_set();
	say("exec /sbin/launchd");
	/*
	 * The kernel starts launchd with no open files; iOS 27's launchd takes an open standard
	 * output as being run by hand ("launchd cannot be run directly (stdout: 1)") and exits.
	 */
	close(0);
	close(1);
	close(2);
	execve("/sbin/launchd", launchd_argv, envp);
	console();
	say("exec /sbin/launchd: %s", strerror(errno));
	return 1;
}
