#!/usr/bin/env python3
"""Opt-in P-SYS root edits (docs-lanes/sys-port.md). Nothing happens unless named.

  sudo fuxi-sys-root.py [--dry-run] OPTIONS MOUNTED_ROOT

MOUNTED_ROOT is the HFS+ root mounted read-write (as for sweet-root-tweaks.py).
Every step is idempotent and has an undo option.

  --audio-plugin BUNDLE   install FuxiAudio.driver (tools/build-fuxi-sys.sh) as
                          /System/Library/Audio/Plug-Ins/HAL/FuxiAudio.driver:
                          the Speaker/Hawking/Actuator/Codec devices VirtualAudio
                          needs to build its routes (ported from HackiOS)
  --remove-audio-plugin   remove it again
  --restore-virtualaudio  move VirtualAudio.plugin back from HAL.disabled, where
                          sweet-root-tweaks.py put it (only useful together with
                          the plug-in: without its devices it crashed mediaserverd)
  --disable-virtualaudio  move it to HAL.disabled again (the current default image)
  --sb-snapshots DYLIB    install libfuxi-sb.dylib as /usr/local/lib/fuxi/ and add
                          it to SpringBoard's DYLD_INSERT_LIBRARIES in the launchd
                          cache: application snapshots as JPEG/PNG instead of ATX
  --no-sb-snapshots       take it out of the job and remove the library
  --app-rate DYLIB        install libfuxi-app.dylib as /usr/local/lib/fuxi/ and add
                          it to runningboardd's DYLD_INSERT_LIBRARIES: runningboardd
                          then inserts it into every application it starts, which
                          sees CADisableMinimumFrameDurationOnPhone = YES (frames
                          above 60 a second) with its signed Info.plist untouched
  --no-app-rate           take it out of the job and remove the library
  --quiet-jobs PRESET,... set Disabled=true on the respawn-storm jobs of a preset
                          (marked _FuxiSysDisabled so the undo restores only them):
                            observed  nanotimekitcompaniond (55 execs per boot on
                                      every recent Fuxi log; Apple Watch faces)
                            hackios   jobs HackiOS's mkjobs.py denies because they
                                      "start, crash and are asked for again" on
                                      iOS 16.5 (not seen looping on Fuxi yet)
  --unquiet-jobs          re-enable every job this tool disabled
  --dry-run               print what would change, write nothing
"""
import argparse
import os
import plistlib
import shutil
import sys

LAUNCHD = 'System/Library/xpc/launchd.plist'
HAL = 'System/Library/Audio/Plug-Ins/HAL'
AUDIO_BUNDLE = HAL + '/FuxiAudio.driver'
VIRTUAL_AUDIO = 'Library/Audio/Plug-Ins/HAL/VirtualAudio.plugin'
VIRTUAL_AUDIO_DISABLED = 'Library/Audio/Plug-Ins/HAL.disabled/VirtualAudio.plugin'
SB_JOB = '/System/Library/LaunchDaemons/com.apple.SpringBoard.plist'
SB_LIB = '/usr/local/lib/fuxi/libfuxi-sb.dylib'
APP_JOB = 'com.apple.runningboardd'
APP_LIB = '/usr/local/lib/fuxi/libfuxi-app.dylib'
MARK = '_FuxiSysDisabled'
PRESETS = {
    'observed': ('com.apple.nanotimekitcompaniond',),
    'hackios': ('com.apple.homed', 'com.apple.routined', 'com.apple.duetexpertd', 'com.apple.dmd',
                'com.apple.syncdefaultsd', 'com.apple.photoanalysisd', 'com.apple.avconferenced'),
}


class Root:
    def __init__(self, path, dry_run):
        self.path, self.dry_run = path, dry_run

    def at(self, rel):
        return os.path.join(self.path, rel.lstrip('/'))

    def say(self, text):
        print(('would: ' if self.dry_run else '') + text)

    @staticmethod
    def own(path, mode):
        if os.geteuid() == 0:
            os.chown(path, 0, 0)
        os.chmod(path, mode)

    def load_launchd(self):
        path = self.at(LAUNCHD)
        with open(path, 'rb') as f:
            return plistlib.load(f)

    def save_launchd(self, plist):
        if self.dry_run:
            return
        path = self.at(LAUNCHD)
        st = os.stat(path)
        with open(path, 'wb') as f:
            plistlib.dump(plist, f, fmt=plistlib.FMT_BINARY)
        if os.geteuid() == 0:
            os.chown(path, st.st_uid, st.st_gid)
        os.chmod(path, st.st_mode & 0o7777)

    def install_tree(self, src, rel):
        """Copy a file or bundle as root:wheel, executables/dirs 0755, others 0644."""
        dest = self.at(rel)
        self.say(f'install {os.path.basename(src)} -> /{rel.lstrip("/")}')
        if self.dry_run:
            return
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        parent = os.path.dirname(dest)
        if not os.path.isdir(parent):
            os.makedirs(parent)
            self.own(parent, 0o755)
        if not os.path.isdir(src):
            shutil.copy2(src, dest)
            self.own(dest, 0o755)
            return
        shutil.copytree(src, dest, symlinks=True)
        self.own(dest, 0o755)
        for top, dirs, files in os.walk(dest):
            for name in dirs + files:
                path = os.path.join(top, name)
                if not os.path.islink(path):
                    executable = os.path.isdir(path) or os.access(path, os.X_OK)
                    self.own(path, 0o755 if executable else 0o644)

    def remove(self, rel):
        dest = self.at(rel)
        if not os.path.lexists(dest):
            print(f'/{rel.lstrip("/")}: not there')
            return
        self.say(f'remove /{rel.lstrip("/")}')
        if not self.dry_run:
            shutil.rmtree(dest) if os.path.isdir(dest) else os.unlink(dest)

    def move(self, src_rel, dst_rel):
        src, dst = self.at(src_rel), self.at(dst_rel)
        if not os.path.exists(src):
            print(f'/{src_rel}: not there' + (' (already moved)' if os.path.exists(dst) else ''))
            return
        if os.path.exists(dst):
            sys.exit(f'/{dst_rel} exists too; refusing to overwrite')
        self.say(f'move /{src_rel} -> /{dst_rel}')
        if not self.dry_run:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)


def check_bundle(bundle):
    info = os.path.join(bundle, 'Info.plist')
    if not os.path.isfile(info):
        sys.exit(f'{bundle}: no Info.plist (build it with tools/build-fuxi-sys.sh)')
    with open(info, 'rb') as f:
        data = plistlib.load(f)
    executable = os.path.join(bundle, data.get('CFBundleExecutable', ''))
    if data.get('CFBundleExecutable') != 'FuxiAudio' or not os.path.isfile(executable):
        sys.exit(f'{bundle}: not a FuxiAudio.driver bundle')


def sb_job(plist):
    job = plist['LaunchDaemons'].get(SB_JOB)
    if job is None or job.get('Label') != 'com.apple.SpringBoard':
        sys.exit('launchd.plist: no com.apple.SpringBoard job at ' + SB_JOB)
    return job


def labelled_job(plist, label):
    for job in plist['LaunchDaemons'].values():
        if job.get('Label') == label:
            return job
    sys.exit('launchd.plist: no job labelled ' + label)


def insert_library(root, plist, job, name, lib):
    env = job.setdefault('EnvironmentVariables', {})
    libs = [p for p in env.get('DYLD_INSERT_LIBRARIES', '').split(':') if p]
    if lib in libs:
        print(name + ': DYLD_INSERT_LIBRARIES already has ' + lib)
        return
    env['DYLD_INSERT_LIBRARIES'] = ':'.join(libs + [lib])
    root.say(name + ': DYLD_INSERT_LIBRARIES=' + env['DYLD_INSERT_LIBRARIES'])
    root.save_launchd(plist)


def take_library(root, plist, job, name, lib):
    """The other libraries stay; an environment emptied by this goes, as it was before insert_library."""
    env = job.get('EnvironmentVariables', {})
    libs = [p for p in env.get('DYLD_INSERT_LIBRARIES', '').split(':') if p]
    if lib in libs:
        libs.remove(lib)
        if libs:
            env['DYLD_INSERT_LIBRARIES'] = ':'.join(libs)
        else:
            env.pop('DYLD_INSERT_LIBRARIES')
            if not env:
                job.pop('EnvironmentVariables')
        root.say(name + ': ' + lib + ' taken out of DYLD_INSERT_LIBRARIES')
        root.save_launchd(plist)


def sb_snapshots(root, dylib):
    root.install_tree(dylib, SB_LIB)
    plist = root.load_launchd()
    insert_library(root, plist, sb_job(plist), 'SpringBoard', SB_LIB)


def no_sb_snapshots(root):
    plist = root.load_launchd()
    take_library(root, plist, sb_job(plist), 'SpringBoard', SB_LIB)
    root.remove(SB_LIB)


def app_rate(root, dylib):
    plist = root.load_launchd()
    job = labelled_job(plist, APP_JOB)
    root.install_tree(dylib, APP_LIB)
    insert_library(root, plist, job, 'runningboardd', APP_LIB)


def no_app_rate(root):
    plist = root.load_launchd()
    take_library(root, plist, labelled_job(plist, APP_JOB), 'runningboardd', APP_LIB)
    root.remove(APP_LIB)


def quiet_jobs(root, presets):
    labels = []
    for preset in presets:
        if preset not in PRESETS:
            sys.exit(f'unknown preset {preset!r}; known: {", ".join(PRESETS)}')
        labels += [label for label in PRESETS[preset] if label not in labels]
    plist = root.load_launchd()
    by_label = {job.get('Label'): job for job in plist['LaunchDaemons'].values()}
    changed = 0
    for label in labels:
        job = by_label.get(label)
        if job is None:
            print(f'{label}: no such job, skipped')
        elif job.get(MARK):
            print(f'{label}: already disabled by this tool')
        elif job.get('Disabled'):
            print(f'{label}: already disabled, left alone')
        else:
            job['Disabled'] = True
            job[MARK] = True
            changed += 1
            root.say(f'{label}: Disabled=true')
    if changed:
        root.save_launchd(plist)


def unquiet_jobs(root):
    plist = root.load_launchd()
    changed = 0
    for job in plist['LaunchDaemons'].values():
        if job.pop(MARK, False):
            job.pop('Disabled', None)
            changed += 1
            root.say(f'{job.get("Label")}: enabled again')
    if changed:
        root.save_launchd(plist)
    else:
        print('no job was disabled by this tool')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('root')
    ap.add_argument('--dry-run', action='store_true')
    audio = ap.add_mutually_exclusive_group()
    audio.add_argument('--audio-plugin', metavar='BUNDLE')
    audio.add_argument('--remove-audio-plugin', action='store_true')
    va = ap.add_mutually_exclusive_group()
    va.add_argument('--restore-virtualaudio', action='store_true')
    va.add_argument('--disable-virtualaudio', action='store_true')
    sb = ap.add_mutually_exclusive_group()
    sb.add_argument('--sb-snapshots', metavar='DYLIB')
    sb.add_argument('--no-sb-snapshots', action='store_true')
    app = ap.add_mutually_exclusive_group()
    app.add_argument('--app-rate', metavar='DYLIB')
    app.add_argument('--no-app-rate', action='store_true')
    jobs = ap.add_mutually_exclusive_group()
    jobs.add_argument('--quiet-jobs', metavar='PRESET,...')
    jobs.add_argument('--unquiet-jobs', action='store_true')
    args = ap.parse_args()
    if not os.path.isfile(os.path.join(args.root, LAUNCHD)):
        ap.error('not a mounted iOS root: ' + args.root)
    root = Root(args.root, args.dry_run)
    if args.audio_plugin:
        check_bundle(args.audio_plugin)
        root.install_tree(args.audio_plugin, AUDIO_BUNDLE)
    if args.remove_audio_plugin:
        root.remove(AUDIO_BUNDLE)
    if args.restore_virtualaudio:
        root.move(VIRTUAL_AUDIO_DISABLED, VIRTUAL_AUDIO)
    if args.disable_virtualaudio:
        root.move(VIRTUAL_AUDIO, VIRTUAL_AUDIO_DISABLED)
    if args.sb_snapshots:
        if not os.path.isfile(args.sb_snapshots):
            ap.error('no such library: ' + args.sb_snapshots)
        sb_snapshots(root, args.sb_snapshots)
    if args.no_sb_snapshots:
        no_sb_snapshots(root)
    if args.app_rate:
        if not os.path.isfile(args.app_rate):
            ap.error('no such library: ' + args.app_rate)
        app_rate(root, args.app_rate)
    if args.no_app_rate:
        no_app_rate(root)
    if args.quiet_jobs:
        quiet_jobs(root, [p for p in args.quiet_jobs.split(',') if p])
    if args.unquiet_jobs:
        unquiet_jobs(root)


if __name__ == '__main__':
    main()
