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
 *      launchd.plist) over /System/Library/xpc.
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
#include <sys/stat.h>
#include <sys/time.h>
#include <unistd.h>

#define DISK            "/fuxi"
#define RO_VAR          DISK "/mnt/var"
#define FILE_LIMIT      (1024 * 1024)

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
	say("/private/var on ramfs: %u dirs %u links %u files (%llu KB), %u files over %d KB skipped (%llu KB), "
	    "%u errors, %llu ms", count.dirs, count.links, count.files, count.bytes >> 10, count.skipped, FILE_LIMIT >> 10,
	    count.skipped_bytes >> 10, count.errors, now_ms() - t0);
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
