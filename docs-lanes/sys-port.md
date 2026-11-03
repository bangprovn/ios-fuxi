# P-SYS: HackiOS system fixes, ported opt-in

HackiOS (OOSEX GANG; [kernel](https://github.com/oosexgang/hackios-xnu-kernel) APSL 2.0,
[userland](https://github.com/oosexgang/hackios-xnu-userland) MIT) runs xnu and iOS 16.5 on a POCO X5
Pro. Its userland notes name the daemons that crash, loop or stall when iPhone hardware is missing. This
lane ports the fixes that apply to Fuxi (iOS 16.7, 20H392). **None of them is on by default:** with no new
boot-arg, ADT option or root-edit option, the kernel, ADT and root are unchanged.

| Item | Where | Switch | Source (licence) |
| --- | --- | --- | --- |
| Privileges by entitlement | `xnu/fuxi-sys/fuxi_sys.h`, patch 0107 | boot-arg `fuxiprivs=1` | `hackios-xnu-kernel xnu/files/bsd/kern/kern_minimac.c` (APSL 2.0) |
| Container path for an uncontained process | same | boot-arg `fuxisbpath=1` | same |
| Sandbox extensions issued and consumed | same | boot-arg `fuxisbext=1` | Fuxi; HackiOS only returns success |
| SpringBoard's frame rates: its in-process animations' display link, its animations through the window server | `tools/fuxi-sb/FuxiRates.c`, in `libfuxi-sb.dylib` | on where the library is installed; boot-arg `fxrate=0` turns it off, `1` and `2` one at a time | `hakios-xnu-kernel userland/src/inject_sb.c` (MIT) |
| Every application above 60 a second (`CADisableMinimumFrameDurationOnPhone`), its Info.plist untouched | `tools/fuxi-sb/FuxiAppRate.c`, `libfuxi-app.dylib` | `fuxi-sys-root.py --app-rate`; boot-arg `fxapp=0` turns it off | Fuxi; the key from `hakios-xnu-kernel userland/mkhighrate.py` (MIT) |
| HAL devices Speaker/Hawking/Actuator/Codec | `tools/fuxi-audio/` | `fuxi-sys-root.py --audio-plugin` (+ `--restore-virtualaudio`) | `hackios-xnu-userland userland/src/audio_plugin.c`, `audio_plugin.plist` (MIT) |
| `/product/haptics`, `/product/camera` | `tools/mkadt-fuxi.py` | `--product-haptics`, `--product-camera` | `hackios-xnu-userland` loader/identity notes (MIT); values from the local D22AP tree |
| App snapshots as JPEG/PNG, not ATX | `tools/fuxi-sb/` | `fuxi-sys-root.py --sb-snapshots`; boot-arg `fuxisbjpeg=0` turns it off again | `hackios-xnu-userland userland/src/inject_sb.c` (MIT) |
| Respawn-storm jobs disabled | `tools/fuxi-sys-root.py` | `--quiet-jobs observed[,hackios]` | `hackios-xnu-userland tools/mkjobs.py` deny list (MIT) |

The ported files keep their licence header and say `Ported from HackiOS <path> (<licence>)`. HackiOS code
was read, not executed or built. `tools/test-fuxi-sys.py` tests each item on the host.

## What each item addresses

The **Observed** notes come from Fuxi console logs (`research/device/ios-0098-m9j/` and the earlier
logs, local only). The **HackiOS** notes are what HackiOS reports for its phone. **Guess** marks
expectations that no device run has confirmed.

### Kernel: patch 0107 (`fuxiprivs`, `fuxisbpath`, `fuxisbext`)

0107 replaces `bsd/kern/sweet_amfi.c` (the H1 version from 0103 plus three hooks) and adds
`bsd/kern/fuxi_sys.h`. The canonical sources are in `xnu/fuxi-sys/`. Regenerate or check the patch with
`tools/sync-fuxi-sys.py [--check]`. If H1's `sweet_amfi.c` changes, copy the change into
`xnu/fuxi-sys/sweet_amfi.c` and re-run the sync. `--check` fails while the two disagree, because 0107
must apply on top of 0103.

- **`fuxiprivs=1`, privileges by entitlement.** The SWEET "Sandbox" policy grants only
  `PRIV_WORK_INTERVAL` and refuses every other `priv_check_cred()` privilege. With `fuxiprivs=1`, 40
  privileges are granted when the asking process carries the matching boolean entitlement. Apple's
  Sandbox does the same, and HackiOS's `minisandbox_privileges[]` table encodes it. The 40 pairs were
  checked against xnu-8796 `priv.h`. The grant is answered only for the caller's own credential
  (`cred == kauth_cred_get()`), and each privilege is logged once.
  - Observed: about 125 `SWEET Sandbox: <proc> privilege <n> denied` lines per log, from rapportd
    (12000 SKYWALK_REGISTER_USER_PIPE, 1002), identityservicesd (12001, 12000), symptomsd (10003
    NET_PRIVILEGED_NETWORK_STATISTICS), dasd (10001, 1005, 1002), cloudd (1002, 10001), logd,
    aggregated, healthd, accountsd, callservicesd, ReportMemoryException (1002 GLOBAL_PROC_INFO),
    SpringBoard (1007 DARKBOOT) and kernel_task (10001).
  - HackiOS: "on a device the sandbox hands them out by entitlement". Its examples are dasd
    `vm-pressure-level`, symptomsd and aggregated `com.apple.private.network.statistics`, and
    mediaserverd and mDNSResponder `socket-delegate`. HackiOS also notes that the NECP message "does
    not hold the necessary entitlement to delegate network traffic" is harmless noise. `PRIV_WORK_INTERVAL`
    ("Failed to create work interval") killed SpringBoard on its first swipe, and Fuxi already grants it.
  - Guess: fewer retries in rapportd, identityservicesd, symptomsd and dasd. Entitlement checks keep this
    narrower than a blanket grant.
- **`fuxisbext=1`, Sandbox requests 5 and 6 (extensions).** Both returned `ENOENT`. Measured in `a-v8b`
  (the phone's unified log, read back from the root): tccd logs "Granting ... via entitlement
  'com.apple.private.tcc.allow'", then "Failed to issue Sandbox Extension for kTCCServiceMicrophone", and
  Safari gets `kTCCErrorDomain` 6 and denies `getUserMedia` with no prompt. In that boot and the one before
  it the failure appears 3318 times: TCC for Contacts and Photos (tccd, PeopleWidget, searchd, assetsd),
  and cloudd, mobileassetd, linkd, nsurlsessiond. With the switch, request 5 writes a token into the buffer
  libsystem_sandbox allocated, and request 6 returns a handle counting from 1. The argument layouts come from
  disassembling `libsystem_sandbox.dylib` in 20H392's shared cache. HackiOS returns success without writing,
  which leaves a client holding whatever malloc returned.
- **`fuxisbpath=1`, Sandbox request 4 (container path).** H1 answers from `CFFIXED_USER_HOME` and gives
  `ENOENT` to a process without a container. HackiOS returns success with an untouched buffer
  ("success and an untouched buffer", what every process got before). The ENOENT choice was deliberate
  in H1. `fuxisbpath=1` switches to the HackiOS result for A/B testing. Other errors (`ESRCH`,
  `ENAMETOOLONG`, `EFAULT`) and found paths are unchanged, and with `fuxiamfi=0` request 4 still
  returns ENOENT without lookup.
  - HackiOS: "plugin has invalid sandbox or container" (`-[PKService checkEnvironment:]`) was the end of
    every Intents extension while request 4 "succeeded" without writing anything. Fuxi already writes
    the path when there is one, which is the part that fixes this.
  - Guess: some daemons that call `sandbox_container_path_for_pid()` on themselves log or retry on
    ENOENT. Test this only if such logs appear.

### `/product/haptics` and `/product/camera` (mkadt-fuxi.py)

`--product-haptics` copies D22AP's `/product/haptics` (properties include `closed-loop = 1` and
`supports-3rd-party-haptics = 1`) into the Fuxi ADT. `--product-camera` adds an empty `/product/camera`.
D22AP's camera properties (flash and so on) are left out, because they promise a torch and cameras
that are not there. Both work with `--template`, replace an existing child of the same name, and give
new phandles above the tree's highest one. The D22AP tree is read from `research/ipsw-x/d22ap.adt`
(local, not in git) or `--d22ap-adt PATH`.

- HackiOS (`xnuloader/loader.c`, `mkidentity.py`): `closed-loop` sets MobileGestalt's
  DeviceSupportsClosedLoopHaptics, so iOS plays vibrations and feedbacks as waveforms on the "Actuator"
  audio device. "Without it iOS looks for an IOKit vibration motor that the iPhone X does not have."
  The loader always adds haptics. It adds `camera` only with `torch=`/`cam=`, because the node itself
  makes MobileGestalt report `video-camera`, and the flash enables the Control Center flashlight.
- Guess: haptics is useful only together with FuxiAudio's Actuator device. `--product-camera`
  makes iOS believe there is a video camera, and nothing can capture. It is included because the brief
  asked for it and for A/B runs, but keep it off unless camera-related daemons are looping because the
  node is missing.

### FuxiAudio.driver (mediaserverd / audiomxd)

A HAL AudioServerPlugIn that shows the four devices HackiOS found VirtualAudio needs:
**Speaker** (stereo out, silent stereo in), **Hawking** (one silent input; VirtualAudio's 'vhaw'
companion, without which "everything stays on the null port"), **Actuator** (one channel out, for
haptics) and **Codec** (one channel in, data source 'imic'). The format is 16-bit at 48 kHz, and the
clock is `mach_absolute_time()`. Fuxi has no audio output path, so the HackiOS kernel sinks
(`/dev/redwood-audio`, `-mic`, `-haptic`) and the volume ioctl were removed. Output is counted and
dropped, input is silence, and volume is only stored and logged.

- Observed: the current image disables `VirtualAudio.plugin` (`sweet-root-tweaks.py` moves it to
  `HAL.disabled`), because it crashed mediaserverd (SIGSEGV in `ApplyBeamFormerPreference`) when it
  found no iPhone devices. `StartRecorderServer=false` is already set. Neither mediaserverd nor
  audiomxd loops in the m9j log with VirtualAudio disabled, but every audio client sees no route.
- HackiOS: with VirtualAudio and these four devices, mediaserverd settles. VirtualAudio builds the
  speaker route through Hawking, and audio sessions and haptics activate instead of failing.
- Guess: `--audio-plugin` together with `--restore-virtualaudio` reproduces that on Fuxi, so audio
  sessions open silently. **If mediaserverd crashes in VirtualAudio again, undo with
  `--disable-virtualaudio`.** The plug-in alone, with VirtualAudio still disabled, is a safe first step.

### libfuxi-sb.dylib (SpringBoard application snapshots)

The library replaces `+[XBApplicationSnapshotManifestImpl _outputFormatForSnapshot:]` (SplashBoard) by
name, through the ObjC runtime. It returns 3 (JPEG) if `-[snapshot isImageOpaque]`, else 0 (PNG), and
never ATX/ASTC. Both selectors are present in the 20H392 shared cache. HackiOS checks the original
implementation against a 16.5 address before replacing it. This port finds the method by name, and if
the method is missing it logs `NOT changed` and leaves SpringBoard alone. If SplashBoard loads after
the library, the hook is installed from a `_dyld_register_func_for_add_image` callback. The library is
loaded through `DYLD_INSERT_LIBRARIES` in SpringBoard's job in the launchd cache. H1 AMFI answers dyld
policy query 0x5a with all features allowed, so dyld honours the variable.

- HackiOS: ATX compression on the CPU takes seconds per picture, and the app waits for it until the
  10-second scene-update watchdog kills it: "no application survived going to the background".
- Guess: on Fuxi, apps that go to the background (home gesture or app switch) are killed the same way
  once apps launch reliably. No Fuxi log has shown this yet, because app switching has not been tested.

### libfuxi-app.dylib (applications at the panel's rate)

Measured in 20H392's shared cache: QuartzCore reads the key once per process, in the application's own
process, with `CFDictionaryGetValue(CFBundleGetInfoDictionary(CFBundleGetMainBundle()), key)` (a
`dispatch_once` block at QuartzCore+0x247c94, cfstring at 0x1d5b5f400, result byte at 0x1dcdfbee8). Without
it, CADisplayLink's frame-rate client (+0x8ab00, "CAFrameRateClient: update", reached from
`-[CADisplayLink setPreferredFrameRateRange:]`, `setPreferredFramesPerSecond:`, `setPaused:`),
`CADynamicFrameRateSource` (+0x1be1ac, from its `setPreferredFrameRateRange:` and `setHighFrameRateReasons:count:`),
`-[CAMetalDrawable present]` and `-[CAAnimation _setCARenderAnimation:layer:]` hold the process to 60 a
second before anything reaches backboardd, so a window-server hook cannot lift it. UIKitCore never names
the key. It is the Info.plist dictionary, not the localized one, so `InfoPlist.loctable` did nothing (root
v23, r-v25), and editing Info.plist breaks the signature SplashBoard checks (roots v13-v22).

CFBundle parses Info.plist with `kCFPropertyListMutableContainers` (CoreFoundation+0x559d0), so the
library sets the key in that dictionary from its constructor, which dyld runs before the application's
and QuartzCore's initializers. It reaches every application through runningboardd: runningboardd's own
launchd job inserts the library, which replaces `-[RBLaunchdJobManager _generateDataWithIdentity:context:error:]`
and adds itself to `EnvironmentVariables` / `DYLD_INSERT_LIBRARIES` of each job whose identity
`isApplication` (not daemons, not extensions, not `DYLD_JUST_BUILD_CLOSURE` launches). In the application it
takes itself out of `DYLD_INSERT_LIBRARIES` again. Lines go to stderr (the kernel log for the names in
boot-arg `sweetnull`) and to the unified log.

### Respawn-storm jobs (`--quiet-jobs`)

- `observed`: `com.apple.nanotimekitcompaniond`. Observed: exec'd 55 times per boot on the m9j log. It
  is the Apple Watch faces companion, and it is restarted by notifyd `ApplicationsChanged` events and
  an xpc activity. With no Watch paired, it has nothing to do.
- `hackios`: homed, routined, duetexpertd, com.apple.dmd, syncdefaultsd, photoanalysisd and
  avconferenced, the jobs HackiOS's `mkjobs.py` denies because on its phone they start, crash and are
  requested again. Observed: none of them loops on Fuxi so far, so this preset is for A/B runs only.
- Jobs that are already disabled are skipped (for example fairplayd.H2). Jobs this tool disables get
  `_FuxiSysDisabled = true`, so `--unquiet-jobs` re-enables only those. The one crash in the m9j log,
  driverkitd (SIGKILL from jetsam, once per boot), is not a respawn storm and is not covered here.

## Left out, and why

- **`inject_media.c` portrait-prewarm skip:** it patches unslid iOS 16.5 addresses, which do not
  match 20H392. The `StartRecorderServer=false` setting Fuxi already uses avoids the same recorder start.
- **containermanagerd persona spin (`inject.c`):** Fuxi's usermanagerd creates the personas, and the
  spin has not been seen on Fuxi.
- **Camera, torch, microphone and haptic-motor kernel drivers, and HackiOS's `/dev/redwood-*` sinks:**
  these are hardware work on a different phone, and no Fuxi driver exists for them. FuxiAudio discards
  output instead.
- **CommCenter stubs:** CommCenter is already removed from the Fuxi image.
- **mkjobs on-demand pass, Metal hidden from mediaserverd/Siri, syssleep, cpuload, xpcproxy
  identity:** these belong to the cooling lane (patch 0105).
- **Lock-button long press and Siri on wake:** these belong to the buttons lane.
- **SpringBoard activation/setup-reason, battery and poster hooks in `inject_sb.c`:** they are
  outside P-SYS. They are tied to HackiOS's addresses, and Fuxi already handles activation in its own
  way.
- **`rc.ios`, `libmini`, `jb-preinstall.py`:** they contain no fix for a loop seen on Fuxi.

## Build and install

```
tools/build-fuxi-sys.sh [OUT]                  # macOS + Xcode: FuxiAudio.driver, libfuxi-sb.dylib, libfuxi-app.dylib
sudo tools/fuxi-sys-root.py --dry-run --audio-plugin OUT/FuxiAudio.driver \
     --sb-snapshots OUT/libfuxi-sb.dylib --app-rate OUT/libfuxi-app.dylib --quiet-jobs observed MOUNTED_ROOT
tools/mkadt-fuxi.py ... --product-haptics [--product-camera]
```

Undo options: `--remove-audio-plugin`, `--disable-virtualaudio`, `--no-sb-snapshots`, `--no-app-rate`,
`--unquiet-jobs`.
The plug-in and library are ad-hoc signed. Fuxi's AMFI policy accepts them as it accepts the other
lane binaries. Not tested on device yet.

## Device test plan

Each step is a separate boot, so one change can be compared against the previous run. Use a root copy
(root edits are pre-approved, and sweet-root-tweaks.py has already been applied to it). Capture the
console with `tools/fuxi-xnu-monitor.py` and compare against the m9j baseline.

1. **Kernel, both off (regression).** Boot the 0107 kernel with the usual boot-args. Expected: no
   `FUXI SYS:` line, and the same `SWEET Sandbox: ... privilege ... denied` lines as before.
2. **`fuxiprivs=1`.** Expected at policy registration:
   `FUXI SYS: privileges by entitlement on (40 pairs), uncontained container path ENOENT`. Then one line
   per granted pair, for example `FUXI SYS: rapportd privilege 12000 granted by com.apple.private.skywalk.register-user-pipe`
   (the entitlement is whichever one the process carries). Denials remain only for processes without
   the entitlement. Check that rapportd, identityservicesd, symptomsd and dasd do not exec more often
   than in the baseline, and that SpringBoard reaches the same point.
3. **`fuxisbpath=1`** (with or without `fuxiprivs`). Expected: `... uncontained container path succeeds untouched`.
   Compare `PKService`/`containermanager` errors with step 1. Keep this off if nothing improves.
4. **ADT `--product-haptics`.** Expected: mkadt prints `; /product children haptics`. On device,
   nothing should regress. Fewer vibration-motor/CoreHaptics lookup errors would be a gain (Guess).
5. **Root `--audio-plugin` only (VirtualAudio still disabled).** Expected in mediaserverd's stderr
   (it reaches the console only if the job's stderr is routed there; otherwise use
   `log`/the root's log files): `fuxi: audio: Speaker, Hawking, Actuator and Codec are with the HAL (outputs discarded, inputs silent)`.
   mediaserverd must not crash or restart more than in the baseline.
6. **Add `--restore-virtualaudio`.** Expected: the step 5 line, no `ApplyBeamFormerPreference` crash,
   and when something plays, `fuxi: audio: Speaker playing` followed later by
   `fuxi: audio: Speaker stopped after N.NN s (discarded)`. With step 4 also applied, tapping a
   keyboard or switch should log `fuxi: audio: the actuator plays: ... (no motor driver: not felt)`.
   If mediaserverd crashes, revert with `--disable-virtualaudio` and keep the plug-in.
7. **`--sb-snapshots`.** Expected in SpringBoard's stderr:
   `fuxi: SpringBoard: application snapshots as JPEG (opaque) or PNG, not ATX`, then
   `fuxi: SpringBoard: application snapshot as JPEG` when an app goes to the background. The app should
   survive going to the background, with no scene-update watchdog kill. `fuxisbjpeg=0` on the same root
   should print `fuxi: SpringBoard: fuxisbjpeg=0, application snapshots unchanged`. If the class is
   missing, the library prints `... NOT changed` instead.
8. **`--quiet-jobs observed`.** Expected: `nanotimekitcompaniond` no longer appears in exec lines (55
   execs per boot before). Undo with `--unquiet-jobs`.
9. **`--app-rate`.** Boot with `sweetnull=backboardd,runningboardd,Preferences`. Expected:
   `|runningboardd| fuxi: runningboardd: applications start with /usr/local/lib/fuxi/libfuxi-app.dylib (fxapp)`,
   then on opening Settings `|runningboardd| fuxi: runningboardd: Preferences starts with ...` and
   `|Preferences| fuxi: Preferences: CADisableMinimumFrameDurationOnPhone = YES, frames up to the panel's rate`
   (all applications log it to the unified log too). Scrolling Settings should lose its excess of
   double-vsync frames (gaps b in `FUXI SCANOUT: waits per 600 starts`). `fxapp=0` on the same root
   prints `fuxi: runningboardd: fxapp=0, applications start unchanged`.

## Tests

`tools/test-fuxi-sys.py` (no device, no Apple files):

- `sync-fuxi-sys.py --check`: the canonical sources match 0107, and all earlier patches plus 0107 apply.
- Kernel model: compiles the production `fuxi_sys.h` with the extracted `sweet_sandbox_priv_grant` and
  `sweet_sandbox_syscall`. It checks the defaults (no entitlement lookup when off), entitlement
  present or absent, another credential, logging once, request 4 mapping, `fuxiamfi=0` and the report
  line.
- FuxiAudio: compiles `FuxiAudio.c` against stub CF/CoreAudioTypes headers and checks the device list,
  UID translation (including Hawking), streams, 'imic' source, formats, volume/dB, unknown properties,
  the zero-timestamp clock, silent inputs and discarded outputs.
- libfuxi-sb: checks the hook against a stub ObjC runtime: other programs, `fuxisbjpeg=0` parsing,
  late SplashBoard load, JPEG/PNG choice, and a missing selector.
- mkadt-fuxi: uses a synthetic D22AP tree. Default output is unchanged, haptics properties are copied,
  the camera node is empty, phandles are unique, a template replaces rather than duplicates, and a
  missing tree or `/product` raises an error.
- fuxi-sys-root.py: uses a synthetic root. It covers dry run, install modes, the merged
  `DYLD_INSERT_LIBRARIES`, presets with markers, idempotence, and undo that keeps foreign libraries
  and pre-disabled jobs, and runningboardd's job with `--app-rate` and back.

`tools/test-fuxi-apprate.py` compiles `FuxiAppRate.c` against stub CF, ObjC, libxpc and dyld: the key set
(also over NO) and the library leaving `DYLD_INSERT_LIBRARIES`, `fxapp=0`, missing libxpc calls, a late
RunningBoard, application jobs with and without an environment, daemons, closure builds and a missing method.

The local cross-compile (`tools/xnu-cc.py` against the CI build tree) of `bsd/kern/sweet_amfi.c` with
0107 applied: 0 of 1 files failed.
