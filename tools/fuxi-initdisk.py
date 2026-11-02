#!/usr/bin/env python3
"""Lay out the tree of the Fuxi init disk (the boot payload's RAM disk, mounted at /fuxi by fuxi/0021).

  fuxi-initdisk.py OUT_DIR --os OS.dmg --system-os SYSTEMOS.dmg --app-os APPOS.dmg [--init FILE] [--log all|LABEL...]
                   [--allow WORD...] [LABEL...]

OUT_DIR gets:
  init                      process 1 (tools/fuxi-init, built and signed on the Mac), when --init is given
  mnt/var/                  where fuxi-init binds the system volume's read-only /private/var
  mnt/usrlib/               where it binds /usr/lib before merging the OS cryptex's /usr/lib into it
  jobs/LaunchDaemons/       the job set bound over /System/Library/LaunchDaemons
  jobs/empty/               bound over LaunchAngels and NanoLaunchDaemons
  jobs/xpc/                 bound over /System/Library/xpc: its files except launchd.plist(.sig), so that
                            launchd reads the job directory, not the prebuilt job cache

The job set follows HakiOS / hakios-arm64e-dada's mkjobs.py (APSL 2.0), reimplemented here: the LABELs
(DEFAULT_LABELS when none are given) are copied whole and start as iOS starts them; every other job with
Mach services whose program is present is kept but on demand only (RunAtLoad, KeepAlive and the other
start triggers removed), except the labels containing a DENY word (hardware Fuxi has not got, or jobs
that must not run). Every job's standard output and error go to a file that fuxi-init copies to the console (only root
may open the console itself). BOOT_TASKS (keybagd --init, usermanagerd --init) are added as run-once jobs. --log adds os_log to
standard error for the named labels (or all). _Conclave and _LimitLoadToDeviceTree are dropped: launchd
refuses a conclave on a job it does not trust, and the device-tree conditions name Apple hardware.

The output holds Apple's job plists: it is a local build product, never committed.
"""
import argparse, importlib.util, os, plistlib, sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('apfsro', os.path.join(HERE, 'apfs-ro.py'))
apfsro = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(apfsro)

DEFAULT_LABELS = (
    'com.apple.logd', 'com.apple.configd', 'com.apple.notifyd', 'com.apple.cfprefsd.xpc.daemon',
    'com.apple.distnoted.xpc.daemon', 'com.apple.UserEventAgent-System', 'com.apple.runningboardd',
    'com.apple.containermanagerd', 'com.apple.fseventsd', 'com.apple.mobile.keybagd', 'com.apple.powerd',
    'com.apple.backboardd', 'com.apple.SpringBoard',
)
# hakios-arm64e-dada's iOS 26 list (no such hardware here, or must not run), plus Fuxi's: AMFI's daemon
# (no AMFI kext) and the exclave daemons (no exclaves).
DENY = ('watchdog', 'CommCenter', 'wifi', 'WiFi', 'BTServer', 'softwareupdate', 'backupd', 'nfcd',
        'biometrickitd', 'pearld', 'obliterat', 'storage_mounter', 'com.apple.fud', 'sepd', 'seputil', 'camera',
        'restore', 'jetsamproperties', 'usb', 'syncdefaultsd', 'osanalytics', 'PerfPowerServices',
        'MobileFileIntegrity', 'exclave')
START_KEYS = ('RunAtLoad', 'KeepAlive', 'StartInterval', 'StartCalendarInterval', 'WatchPaths',
              'QueueDirectories', 'StartOnMount', 'LaunchEvents')
STDERR_LOG = {'OS_ACTIVITY_DT_MODE': 'YES', 'ACTIVITY_LOG_STDERR': '1', 'CFLOG_FORCE_STDERR': '1'}
JOBS = '/System/Library/LaunchDaemons'
JOB_LOGS = '/private/var/fuxi/log'   # fuxi-init's JOB_LOGS
# launchd's own boot tasks that set up /private/var/keybags (usermanagerd's persona manifest among it). launchd
# skips them on this boot, and AMFI's launch constraints kill them when anything but launchd starts them, so
# they become jobs that run once at load.
BOOT_TASKS = {'keybag': ['/usr/libexec/keybagd', '--init'], 'usermanagerd': ['/usr/libexec/usermanagerd', '--init']}


class Volumes:
    """The root (OS) volume with the SystemOS and AppOS cryptexes where a device grafts them."""

    def __init__(self, os_dmg, system_os, app_os):
        self.root = apfsro.Container(os_dmg).volumes[0]
        self.grafts = {'/System/Cryptexes/OS/': apfsro.Container(system_os).volumes[0],
                       '/System/Cryptexes/App/': apfsro.Container(app_os).volumes[0]}

    def present(self, path):
        vol = self.root
        for prefix, v in self.grafts.items():
            if path.startswith(prefix):
                vol, path = v, '/' + path[len(prefix):]
        try:
            vol.resolve(path)
            return True
        except (FileNotFoundError, apfsro.ApfsError):
            return False


def program_of(job):
    return job.get('Program') or (job.get('ProgramArguments') or [''])[0]


def to_console(job, label, log):
    # Only root may open the console: for any other user xpcproxy's open fails with EACCES (the node
    # is 0622, so a policy refuses it), and a job without UserName may still run as mobile (its domain
    # is user/501). Every job writes to a file in JOB_LOGS, which fuxi-init's log relay copies to the
    # console.
    job['StandardErrorPath'] = job['StandardOutPath'] = f'{JOB_LOGS}/{label}.log'
    job.pop('_Conclave', None)
    job.pop('_LimitLoadToDeviceTree', None)
    if 'all' in log or label in log:
        job.setdefault('EnvironmentVariables', {}).update(STDERR_LOG)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('out')
    ap.add_argument('labels', nargs='*')
    ap.add_argument('--os', required=True)
    ap.add_argument('--system-os', required=True)
    ap.add_argument('--app-os', required=True)
    ap.add_argument('--init')
    ap.add_argument('--log', nargs='*', default=[])
    ap.add_argument('--allow', nargs='*', default=[])
    a = ap.parse_args()
    labels = a.labels or list(DEFAULT_LABELS)
    deny = tuple(w for w in DENY if w not in a.allow)
    vols = Volumes(a.os, a.system_os, a.app_os)
    root = vols.root

    for d in ('mnt/var', 'mnt/usrlib', 'jobs/LaunchDaemons', 'jobs/empty', 'jobs/xpc'):
        os.makedirs(os.path.join(a.out, d), exist_ok=True)
    if a.init:
        with open(a.init, 'rb') as f, open(os.path.join(a.out, 'init'), 'wb') as g:
            g.write(f.read())
        os.chmod(os.path.join(a.out, 'init'), 0o755)

    for name, _, _ in root.readdir(root.lookup('/System/Library/xpc')):
        if not name.startswith('launchd.plist'):
            with open(os.path.join(a.out, 'jobs/xpc', name), 'wb') as f:
                f.write(root.read('/System/Library/xpc/' + name))

    names = sorted(n for n, _, _ in root.readdir(root.lookup(JOBS)) if n.endswith('.plist'))
    named = on_demand = 0
    for name in names:
        label = name[:-len('.plist')]
        try:
            job = plistlib.loads(root.read(f'{JOBS}/{name}'))
        except Exception as e:
            print(f'  skip {name}: {e}')
            continue
        program = program_of(job)
        if label in labels:
            named += 1
            print(f'  job {label}: {program}{"" if vols.present(program) else "   (NOT on the volumes!)"}')
        elif any(w in label for w in deny) or not job.get('MachServices') or not vols.present(program):
            continue
        else:
            for key in START_KEYS:
                job.pop(key, None)
            on_demand += 1
        to_console(job, label, a.log)
        with open(os.path.join(a.out, 'jobs/LaunchDaemons', name), 'wb') as f:
            plistlib.dump(job, f, fmt=plistlib.FMT_BINARY)
    for task, argv in BOOT_TASKS.items():
        label = f'fuxi.boottask.{task}'
        job = {'Label': label, 'ProgramArguments': argv, 'RunAtLoad': True, 'LaunchOnlyOnce': True, 'UserName': 'root'}
        to_console(job, label, a.log)
        with open(os.path.join(a.out, 'jobs/LaunchDaemons', label + '.plist'), 'wb') as f:
            plistlib.dump(job, f, fmt=plistlib.FMT_BINARY)
    missing = [l for l in labels if f'{l}.plist' not in names]
    print(f'{named} named jobs, {on_demand} on demand, of {len(names)}' + (f'; missing labels {missing}' if missing else ''))
    return 1 if missing else 0


if __name__ == '__main__':
    sys.exit(main())
