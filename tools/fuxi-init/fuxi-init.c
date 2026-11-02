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
 *   6. leaves a child that copies the non-root jobs' output (JOB_LOGS) and DAEMON_LOGS to the console.
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
#include <sys/sysctl.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <signal.h>
#include <unistd.h>

#define DISK            "/fuxi"
#define RO_VAR          DISK "/mnt/var"
#define FILE_LIMIT      (1024 * 1024)
#define JOB_LOGS        "/private/var/fuxi/log"
#define DAEMON_LOGS     "/private/var/logs"
#define MAX_JOB_LOGS    512
/* What usermanagerd's boot task (fuxi-initdisk's BOOT_TASKS) prints last. */
#define UM_TASK_LOG     "fuxi.boottask.usermanagerd.out"
#define UM_TASK_DONE    "DONE UMD_INIT"

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
#define PERSONA_DEFAULT         5
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

/*
 * Files the init disk carries for /private/var (fuxi-initdisk.py VAR_FILES): the mobile user's
 * com.apple.migration preferences, which tell DataMigration that this build was migrated
 * already. Without them datamigrator runs every migration plugin, and backboardd waits for it
 * before it brings up its displays.
 */
/*
 * The container tree a restore lays out on the data volume. containermanagerd does not make the
 * top of it: without these its lookups of system group containers find nothing (ENOENT on
 * /private/var/containers/...), its boot state (MobileContainerManager/System) is never written,
 * and lsd's container_system_group_path_for_identifier() waits until lsd's own timeout aborts it.
 * The daemon containers (InternalDaemon) likewise: installd exited "Failed to get installd daemon
 * container" (ENOENT) at every start, and lsd's database seeding waited on it. A daemon that is not
 * mobile has its daemon containers in its own home (installd, _installd: /var/installd).
 * Owners as on a device: root's, except the mobile user's own.
 */
static void
var_dirs(void)
{
	static const struct {
		const char *path;
		mode_t mode;
		uid_t uid;
	} dirs[] = {
		{ "/private/var/containers", 0755, 0 },
		{ "/private/var/containers/Bundle", 0755, 0 },
		{ "/private/var/containers/Bundle/Application", 0755, 0 },
		{ "/private/var/containers/Bundle/Framework", 0755, 0 },
		{ "/private/var/containers/Bundle/VPNPlugin", 0755, 0 },
		{ "/private/var/containers/Bundle/PluginKitPlugin", 0755, 0 },
		{ "/private/var/containers/Data", 0755, 0 },
		{ "/private/var/containers/Data/System", 0755, 0 },
		{ "/private/var/containers/Data/ProtectedSystem", 0755, 0 },
		{ "/private/var/containers/Shared", 0755, 0 },
		{ "/private/var/containers/Shared/SystemGroup", 0755, 0 },
		{ "/private/var/containers/Temp/Bundle/PluginKitPlugin", 0755, 0 },
		{ "/private/var/containers/Temp/Data/System", 0755, 0 },
		{ "/private/var/root/Library/MobileContainerManager/System", 0755, 0 },
		{ "/private/var/root/Library/Caches/com.apple.containermanagerd/System", 0755, 0 },
		{ "/private/var/root/Library/Caches/com.apple.containermanagerd/System/Staging", 0755, 0 },
		{ "/private/var/root/Library/Caches/com.apple.containermanagerd/System/Dead", 0755, 0 },
		{ "/private/var/root/Library/Caches/com.apple.containermanagerd/System/Temp", 0755, 0 },
		{ "/private/var/mobile/Containers/Data", 0755, 501 },
		{ "/private/var/mobile/Containers/Data/Application", 0755, 501 },
		{ "/private/var/mobile/Containers/Data/PluginKitPlugin", 0755, 501 },
		{ "/private/var/mobile/Containers/Data/InternalDaemon", 0755, 501 },
		{ "/private/var/mobile/Containers/Data/XPCService", 0755, 501 },
		{ "/private/var/mobile/Containers/Data/VPNPlugin", 0755, 501 },
		{ "/private/var/mobile/Containers/Data/TempDir", 0755, 501 },
		{ "/private/var/mobile/Containers/Temp/Data/InternalDaemon", 0755, 501 },
		{ "/private/var/mobile/Containers/Temp/Data/PluginKitPlugin", 0755, 501 },
		{ "/private/var/mobile/Containers/Temp/Data/XPCService", 0755, 501 },
		{ "/private/var/installd/Containers/Data/InternalDaemon", 0755, 33 },
		{ "/private/var/installd/Containers/Temp/Data/InternalDaemon", 0755, 33 },
		{ "/private/var/mobile/Containers/Shared", 0755, 501 },
		{ "/private/var/mobile/Containers/Shared/AppGroup", 0755, 501 },
	};
	unsigned made = 0;

	for (unsigned i = 0; i < sizeof(dirs) / sizeof(dirs[0]); i++) {
		char path[256];

		/* parents first (what is missing gets the same owner and mode), then the directory */
		strlcpy(path, dirs[i].path, sizeof(path));
		for (char *slash = strchr(path + 1, '/'); ; slash = strchr(slash + 1, '/')) {
			if (slash != NULL) {
				*slash = '\0';
			}
			if (mkdir(path, dirs[i].mode) == 0) {
				chown(path, dirs[i].uid, dirs[i].uid);
				made++;
			} else if (errno != EEXIST) {
				say("mkdir %s: %s", path, strerror(errno));
				break;
			}
			if (slash == NULL) {
				break;
			}
			*slash = '/';
		}
	}
	say("var dirs: %u made", made);
}

static void
var_files(void)
{
	static const char *const files[] = {
		"mobile/Library/Preferences/com.apple.migration.plist",
	};
	char from[1024], to[1024];
	struct stat st;

	for (unsigned i = 0; i < sizeof(files) / sizeof(files[0]); i++) {
		snprintf(from, sizeof(from), "%s/var/%s", DISK, files[i]);
		snprintf(to, sizeof(to), "/private/var/%s", files[i]);
		if (stat(from, &st) != 0) {
			continue;
		}
		st.st_uid = st.st_gid = 501;    /* mobile */
		st.st_mode = 0600;
		unlink(to);
		copy_file(from, to, &st);
		say("var file %s: %s", to, access(to, F_OK) == 0 ? "ok" : strerror(errno));
	}
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
	var_dirs();
	var_files();
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
 * processes one with uid 501, user/501 a system persona (type 4). usermanagerd creates them
 * (its boot task, a minute after launchd has spawned the user domain's jobs), and containermanagerd
 * answers a process only if usermanagerd knows its persona (else INVALID_PERSONA: lsd aborted at
 * every start). So these are usermanagerd's own personas, ids and names as it makes them, and the
 * kernel (fuxi/0036) lets usermanagerd allocate them again.
 */
static void
personas(void)
{
	static const struct {
		uid_t id;
		const char *name;
		int type;
		uid_t uid;
	} proxies[] = {
		{ 99, "FEEDEEEE-DDDD-CCCC-BBBB-330000000000", PERSONA_SYSTEM, (uid_t)-1 },
		{ 199, "FEEDEEEE-DDDD-CCCC-BBBB-550000000000", PERSONA_SYSTEM_PROXY, (uid_t)-1 },
		{ 100, "FEEDEEEE-DDDD-CCCC-BBBB-3333000001F5", PERSONA_SYSTEM, 501 },
		{ 200, "FEEDEEEE-DDDD-CCCC-BBBB-5555000001F5", PERSONA_SYSTEM_PROXY, 501 },
		{ 1000, "FEEDEEEE-DDDD-CCCC-BBBB-0000000001F5", PERSONA_DEFAULT, 501 },
	};

	for (unsigned i = 0; i < sizeof(proxies) / sizeof(proxies[0]); i++) {
		struct kpersona_info info;
		uid_t id = 0;

		memset(&info, 0, sizeof(info));
		info.persona_info_version = PERSONA_INFO_V2;
		info.persona_id = proxies[i].id;
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
 * usermanagerd's daemon reads the persona manifest once, at start. launchd runs the boot task
 * (usermanagerd --init) as an ordinary job here, not before the daemons as on iOS, and the daemon
 * comes up as soon as the task has saved the manifest's first version (the system personas):
 * it then answers every user persona query with ENOENT, containermanagerd has no personas
 * and refuses persona 200 (INVALID_PERSONA), and lsd and SpringBoard never finish starting.
 * When the task is done, the daemon is killed; launchd starts it again on demand, with the
 * whole manifest.
 */
static void
restart_usermanagerd(void)
{
	int mib[4] = { CTL_KERN, KERN_PROC, KERN_PROC_ALL, 0 };
	struct kinfo_proc *procs;
	size_t size = 0;

	if (sysctl(mib, 3, NULL, &size, NULL, 0) != 0 || (procs = malloc(size += 16 * sizeof(*procs))) == NULL) {
		return;
	}
	if (sysctl(mib, 3, procs, &size, NULL, 0) == 0) {
		for (size_t i = 0; i < size / sizeof(*procs); i++) {
			if (strcmp(procs[i].kp_proc.p_comm, "usermanagerd") == 0) {
				say("usermanagerd's boot task is done: stopping usermanagerd[%d] to reload the persona manifest",
				    procs[i].kp_proc.p_pid);
				(void)kill(procs[i].kp_proc.p_pid, SIGKILL);   /* it ignores SIGTERM */
			}
		}
	}
	free(procs);
}

/*
 * Only root may open the console (xpcproxy gets EACCES for any other user), so fuxi-initdisk
 * points the other jobs' standard output and error at JOB_LOGS/<label>.log. A child of this
 * process, which stays when the parent becomes launchd, copies what they write to the console,
 * each chunk headed by the file name. It copies DAEMON_LOGS too, where some daemons (usermanagerd)
 * write their own log files.
 */
static void
log_relay(void)
{
	static const char *const dirs[] = { JOB_LOGS, DAEMON_LOGS };
	static struct {
		char path[512];
		off_t done;
	} seen[MAX_JOB_LOGS];
	static char buf[4096];
	unsigned nseen = 0;
	int um_task = 0;        /* 1: its last line seen, 2..: waited that many rounds, -1: restarted */

	if (mkdir(JOB_LOGS, 01777) != 0 || chmod(JOB_LOGS, 01777) != 0) {
		say("mkdir %s: %s", JOB_LOGS, strerror(errno));
		return;
	}
	if (fork() != 0) {
		return;
	}
	for (;;) {
		for (unsigned k = 0; k < sizeof(dirs) / sizeof(dirs[0]); k++) {
			DIR *d = opendir(dirs[k]);
			struct dirent *e;

			while (d != NULL && (e = readdir(d)) != NULL) {
				char path[512];
				unsigned i;
				ssize_t n;
				int fd;

				if (e->d_name[0] == '.') {
					continue;
				}
				snprintf(path, sizeof(path), "%s/%s", dirs[k], e->d_name);
				for (i = 0; i < nseen && strcmp(seen[i].path, path) != 0; i++) {
				}
				if (i == nseen) {
					if (nseen == MAX_JOB_LOGS) {
						continue;
					}
					strlcpy(seen[nseen++].path, path, sizeof(seen[0].path));
				}
				if ((fd = open(path, O_RDONLY)) < 0) {
					continue;
				}
				while ((n = pread(fd, buf, sizeof(buf), seen[i].done)) > 0) {
					dprintf(2, "fuxi-log %s: ", e->d_name);
					(void)write(2, buf, (size_t)n);
					if (um_task == 0 && strcmp(e->d_name, UM_TASK_LOG) == 0 &&
					    memmem(buf, (size_t)n, UM_TASK_DONE, strlen(UM_TASK_DONE)) != NULL) {
						um_task = 1;
					}
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
		}
		/* give the task a second to exit, so that only the daemon is left */
		if (um_task > 0 && ++um_task > 3) {
			restart_usermanagerd();
			um_task = -1;
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
