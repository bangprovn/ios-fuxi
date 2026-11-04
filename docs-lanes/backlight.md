# Backlight phase 1: opt-in observation service

## OBSERVED

`research/bl/keys74-iok.log` records these failed lookups:

| Consumer | Trace lines | Lookup | Result |
| --- | --- | --- | --- |
| backboardd | 31, 45 | `IOPropertyMatch { backlight-control = true }` | none / zero services |
| backboardd | 35 | `IODeviceTree:/backlight` | none |
| SpringBoard | 186, 229 | `IOPropertyMatch { backlight-control = true }` | none |

The Boolean match contains no `IOProviderClass` restriction. A registered
IOService with an **OSBoolean true** property satisfies this predicate;
an integer 1 or an ADT byte property is not the same property type.
`IOServiceNotificationUser` is matching infrastructure metadata.

The scripted Power events at lines 267–283 lead to `SWEET FB: display off`
(273) then `SWEET FB: display on` (277). There is also an earlier off at 154.
The trace therefore demonstrates working display power requests despite the
missing backlight service. The user reports the post-wake lock screen keeps
animating at approximately 58 Hz and never auto-dims/auto-locks. Framebuffer
stats continue after the wake; their presented-frame rate is not itself a
measurement of animation or vsync frequency.

No successful backlight match, brightness read/write, backlight user-client
type, private selector ABI, or request for `IODisplayConnect` appears in this
trace. It cannot establish what happens after a successful match.

## Public API evidence

Apple's [IODisplayLib.c](https://github.com/apple-oss-distributions/IOKitUser/blob/main/graphics.subproj/IODisplayLib.c)
resolves an `IODisplay` directly or recursively below a supplied framebuffer.
It reads `IODisplayParameters`, expects parameter dictionaries containing
numeric `min`, `max`, `value`, and sends flat parameter dictionaries through
`IORegistryEntrySetCFProperties`. Float setters scale through the advertised
integer range. This is public IOGraphics behavior, not evidence of the closed
iOS 16.7 BacklightServices implementation.

XNU's [IORegistryEntry.cpp](https://github.com/apple-oss-distributions/xnu/blob/xnu-8796.141.3/iokit/Kernel/IORegistryEntry.cpp)
funnels the string and symbol property overloads through the OSSymbol
get/set methods. Copy-property wrappers call getters, while whole-table
serialization uses a separate snapshot path. The probe instruments both.

## GUESS and experiment

BacklightServices and SpringBoard may need a usable brightness endpoint to
advance their idle dim/off policy after wake. The missing endpoint is a
plausible cause, not an established cause: idle assertions, timer state,
framebuffer replies, or another missing service could also explain it. The
earlier off in this same trace limits the claim that backlight discovery is
always required for display off.

AppleARMBacklight-style property matching does not require that class name.
Publish `FuxiBacklight`, an IOService through a small `IODisplay` compatibility
metaclass, attached below AppleCLCD. This also gives public IODisplay helpers
a display descendant without implementing an IOGraphics framebuffer family.
It is not an implementation of Apple's private AppleARMBacklight ABI.

`IODisplayConnect` normally represents a framebuffer connection in the
IOGraphics service hierarchy; adding an empty service bearing that name
would not implement its semantics. No such nub is added in phase 1. The
direct `IODisplay` conformance handles the public helper's discovery rule.

## Published contract

Phase 1 uses **`fuxibacklight=1`**. Modes 2 and 3 below also publish the
service. Absent, zero, and values other than 1, 2, or 3
return before allocation, attachment, property publication, or probe logging.
Both publication and start verify the argument; all code and the AppleCLCD
call are FUXI-only. The built-in source is in `iokit/conf/files.arm64`.
AppleCLCD publishes the child before registering itself, so a consumer
awakened by the framebuffer's publication can already find the child.

```text
AppleCLCD
  FuxiBacklight : IODisplay : IOService
    backlight-control = true (OSBoolean)
    IODisplayParameters = {
      brightness = { min = 0, max = 65536, value = 65536 }
    }
```

The numbers are 32-bit OSNumbers. **0–65536 is a normalized integer range**,
chosen to preserve fractional brightness when public helpers map 0.0–1.0.
It is not nits, a measured panel maximum, or a DCS register range. Initial
65536 is a synthetic requested value; actual bootloader brightness is unknown.

`setProperties` accepts `brightness = OSNumber`, `brightness = { value =
OSNumber }`, and either shape wrapped in `IODisplayParameters`. Accepted
values 0–65536 are stored in `fRequested` and reflected in a newly allocated
immutable registry dictionary under a lock. Out-of-range or malformed
brightness returns bad argument, other requests return unsupported. Failed
allocation leaves the previous request intact. No arbitrary dictionary merge,
commit operation, calibrated nits, fade, or power-state implementation is claimed.

A diagnostic IOUserClient can be opened; it forwards property writes to the
service. All external selectors return unsupported and clear output counts.
Their numbers and scalar inputs are logged without guessing which one means
brightness. A request through an unknown selector is not treated as brightness.

## Logging and interpretation

Look for `FUXI BACKLIGHT:` followed by:

- `published observation-only brightness min=0 max=65536 value=65536`;
- `read` / `read-all` with property names, value types, numeric/Boolean values;
- `set` / `setProperties` with the requested dictionary shape;
- `brightness ... requested=... valid=... hardware=untouched`;
- `open ... process=... type=... -> ...`, `close`;
- `method ... selector=... scalars=...` and `scalar[index]=...` lines.

Each category (reads including snapshots, individual sets, setProperties,
opens/closes, external methods, brightness requests) has an atomic counter:
first 64 events, then event numbers divisible by 256. All scalar arguments
of a sampled method are printed. Dictionary logging is bounded to 64 entries
in total per event and three nested levels; strings are identified by type, binary
data by length, and keys are limited to 96 characters. Suppressed log events
still execute and store valid requests. Ordinary reads may come from kernel
matching as well as user space; they do not identify a particular consumer.

No MMIO, panel/DSI/PMIC writes, framebuffer pixel writes, or
DCS commands occur in this service. Mode 1 sends no power notifications.
Even brightness zero only changes stored
state. Existing `SWEET FB: display off/on` remains the independent power path.
Phase 1 may change userland policy by satisfying discovery; it does not promise
to restore auto-lock or reduce power. A later phase can map validated requests
to DCS `0x51` once units and protocol have been observed.

ADT is unchanged. `IOService` publication does not create
`IODeviceTree:/backlight`, so that lookup will still fail. If a later opt-in
trace shows the missing node prevents progress after service discovery,
`tools/mkadt-fuxi.py` / `tools/mkadt.py` can add a separately opt-in metadata
node with only properties actually requested. An ADT node alone cannot provide
a user client or live brightness handling; there is currently no evidence
that it is required in addition to this service.

## Source maintenance and host validation

`xnu/fuxi-backlight/` is canonical for patch `0109-fuxi-backlight.patch`;
`tools/sync-fuxi-backlight.py [--check]` maintains its source snapshots and
build-list hook. The AppleCLCD call stays in canonical
`xnu/fuxi-unixnu/SweetFramebuffer.cpp`, maintained by
`tools/sync-fuxi-unixnu.py` in patch 0106. This follows the existing split
between framebuffer integration and separately compiled IOKit services.

`python3 tools/test-fuxi-backlight.py` checks synchronization, patch application
after replaying all preceding build-list patches, publication/gating/observation structure, and runs
the production property parser with host dictionaries under ASan/UBSan.
It exercises input shapes, endpoints, invalid values, allocation failure, and
rate limits. `tools/ci/build.sh` runs it for both kernel profiles. These host
checks do not establish iOS acceptance; no device validation is part of phase 1.

Validation for this change: the backlight host test (ASan/UBSan), all eleven
`sync-fuxi-* --check` tools, ARM64 syntax checking of the new service against
local XNU 8796 headers, and the kernel CI shell syntax/dry-run checks passed.
The broad host sweep had 43 passing scripts; 22 need explicit external build
inputs and two need absent generated build artifacts. The staging test's
artifact-independent `--policy-storage-only` mode also passed. No kernel link,
boot, or device acceptance test was performed.

## Power wake investigation and mode 2 interest experiment

**Root cause status:** the missing logical backlight power notifications are
an established interface gap. Their responsibility for the missing post-wake
auto-lock is **GUESS**, not a demonstrated root cause. Mode 2 repairs that
specific gap as an opt-in experiment; no device result is claimed.

### OBSERVED: two boot traces

The supplied `research/it/bl79.log` and `research/it/wake80.log` both use
`fuxibacklight=1`. The service is discovered, brightness is read, and
`IOGeneralInterest` is installed. No backlight `setProperties` call appears.
The first 64 property writes would be logged, so the missing first call is
not explained by rate limiting.

In `wake80`, the initial on/off requests are at lines 2487/3462. The supplied
timing observation puts that off about 40 seconds after on. The scripted
Power down at line 3541 produces another on at 3546; Power up is at 3557.
There is no subsequent framebuffer off during the reported 420 seconds
without input. This establishes failure to request auto-off, not the internal
state of a SpringBoard timer. The comparative boot observation is that
publishing the service permits initial auto-lock; it does not suffice after wake.

`TouchDetectionMode=0x101` appears at 3554. AKS selectors 17/26/8 also occur,
with selector 8 returning `0xe00002c7` both before and after wake. That error
does not distinguish the successful initial idle cycle from the failed wake
cycle. Ordinary SpringBoard/BLS/CoreBrightness stderr and OS logging are not
available on this kernel console; missing userland messages prove nothing.

### OBSERVED: local iOS code and XNU contract

The supplied ARM64 shared cache was inspected offline with the already
installed `ipsw` disassembler. A small read-only HFS catalog reader also
extracted backboardd from the existing local root image. Extracted bytes,
symbol tables, and disassembly remain only in ignored `research/idle/`.
The following addresses are unslid entry points in those supplied images,
not runtime addresses or a portable ABI across iOS versions.

| Image / entry point | Static finding |
| --- | --- |
| CoreBrightness `_DisplayCreateWithID`, `0x1af0d8510` | Reads `IODisplayParameters` / `brightness` / `value`; initializes the cached backlight-on word at context offset `0x68` from whether value is nonzero. The synthetic initial 65536 therefore describes an on backlight. |
| CoreBrightness `_BacklightGeneralInterestNotificationCallback`, `0x1af0e29e4` | Recognizes **`0xe0000230` = ON** and **`0xe0000210` = OFF**, setting that same word to 1 or 0. The inspected callback ignores the payload and does not reread brightness or dispatch a BLS completion. |
| CoreBrightness `___DisplayFadeUpdateFactorFade`, `0x1af001adc` | Checks the cached on word during a positive-factor fade and has a bounded wait/retry path when the backlight is still considered off. This proves the word affects fade handling; it does not prove an idle-timer dependency. |
| IOMobileFramebuffer `_kern_GetBlock`, `0x1af224cac` | Selector **79 (`0x4f`)** reads a generic block, with an identifier and byte offset in scalar inputs. It copies data in chunks up to 4096 bytes. A 40-byte zero reply is not a power-completion acknowledgement. The trace does not identify the requested block. |
| IOMobileFramebuffer `_kern_RequestPowerChange`, `0x1af2284f0` | Uses selector 12. Power callbacks use the separate notification registration path. Neither boot trace contains selector 72 registration or a `notifications type` line, so there is no observed power-callback subscriber to notify. |
| SpringBoard `-[SBBacklightIdleTimer reset]`, `0x1c5b3447c` | Enables its attention timer if disabled, resets its expectation, and reconfigures attention timeouts with reset requested. A Power event reaching HID is not proof this method ran. |
| SpringBoard `-[SBBacklightController backlight:didCompleteUpdateToState:forEvent:]`, `0x1c5e2f218` | Invokes a pending change completion. The request completion block subsequently notifies backlight observers. This is a BLS completion path, distinct from the CoreBrightness interest callback. |
| BacklightServicesHost `-[BLSHBacklightOSInterfaceProvider transitionToDisplayMode:withDuration:]`, `0x1ef791c48` | Has a CBDisplayStateClient path and a legacy path using `BKSHIDServicesSetBacklightFactorWithFadeDurationAsync` plus its own completion timer when that client is unsupported. The runtime branch has not been measured here. A mandatory kernel display-on-complete handshake cannot be inferred from the method name. |
| backboardd `-[BKHIDUITouchDetectionModeDigitizerServiceWrapper applyUIMode:]`, `0x100080da8` | ORs **`0x100`** into the digitizer mode for hardware-button change sources; its log formatter calls this `fromHardwareButton`. Thus 0x101 carries source metadata, not an additional touch contact or a completion acknowledgement. |
| backboardd `-[BKUserEventTimer systemShellDidFinishLaunching:]`, `0x100031bf0` | Queues the block at `0x1000324c4` that sets `_safeToResetIdleTimer` and posts the safe-to-set notification. This inspected gate is a shell-startup event, not a per-wake backlight acknowledgement. |
| AttentionAwareness `AttentionServiceEventFilter::filter`, `0x1ab135e68` | Maps eligible HID events, including keyboard events, to attention masks and passes their timestamp to `processHIDEvent:mask:timestamp:`. This path does not itself wait for a backlight-service message. Delivery to a particular timer still depends on its client configuration. |

XNU `IOMessage.h` names the two recognized constants
`kIOMessageDeviceHasPoweredOn` and `kIOMessageDeviceWillPowerOff`.
`IOService::messageClients` fans out through `gIOGeneralInterest`, including
interest notifiers. Simply changing a registry property does not execute
this callback. The inspected CoreBrightness `DisplayOn` dictionary update is
an internal state/event path; it does not establish a required property named
`DisplayOn` or `backlight-state` on FuxiBacklight. No such property is invented.

### Change and limits

**`fuxibacklight=2`** publishes the same observation service as mode 1 and
sends the two recognized, payload-free messages from that child after each
accepted SweetFramebuffer selector-12 request: zero means OFF, nonzero means
ON. Repeated requests also report the current logical state. No message is
sent at publication, before userland has registered its interest.

Only mode 2 runs power requests through AppleCLCD's workloop gate, keeping
concurrent power requests and their notifications ordered. The framebuffer
lock is released before invoking interest clients, allowing callback re-entry.
The child is found under the requesting framebuffer rather than through a
global service pointer. Both the framebuffer integration and the notification
function check the opt-in. Absent/0/unknown values remain disabled; mode 1
retains its previous direct request path and sends no new messages.

This reports **logical power accepted by the existing framebuffer path**.
It is not a panel-ready signal: black clearing/redraw and GPU retirement may
finish later. Brightness remains the separately stored request; no synthetic
zero-brightness write, HID user-activity event, periodic auto-off timer,
selector-79 data, or IOMFB power callback is added. These choices keep the
experiment limited to the observed interest protocol.

**GUESS:** failing to keep CoreBrightness's backlight-on latch consistent
with framebuffer power might interfere with its fade/transition handling and
subsequent idle policy. The callback only changes that latch; the investigation
has not shown it re-arms SpringBoard's timer. Assertions, attention-client
configuration, or a BLS/scene completion that never arrives remain alternatives.

### Exact acceptance experiment

Run matched boots with modes 1 and 2, retaining the existing boot configuration
and using `fuxikeyscript=150 fuxikeyscriptn=1`. Do not add touches or other
keys. Record at least 420 seconds after the scripted Power release. Check:

1. Initial auto-off still occurs. Mode 2 prints
   `FUXI BACKLIGHT: logical power interest notifications enabled` once.
2. Each `SWEET FB: display off` is followed by
   `FUXI BACKLIGHT: power #... off message=0xe0000210 result=0x0 logical-only`;
   each on by the corresponding `on message=0xe0000230` line. The message
   result is fanout status, not proof userland received or processed it.
3. After the single scripted Power wake/release, look for another
   `SWEET FB: display off` without input. Compare its interval to the initial
   idle cycle; allow 120 seconds before declaring a prompt re-arm failure,
   then retain the full 420-second observation for comparison with `wake80`.
4. Repeat the matched pair before attributing a restored auto-lock to the
   change. `0x101` may remain: its hardware-button bit is expected metadata.

If mode 2 posts both messages successfully but auto-off still fails, the
notification experiment has not repaired idle re-arm. The next discriminating
experiment is a temporary userland observer, writing explicit markers through
a verified kernel-console sink (not ordinary stderr): capture the wake-time
BLS state/transition completion, `SBBacklightIdleTimer` reset/descriptor
timeouts, and attention-client enabled state before wake, after Power release,
and at +60/+120 seconds. Use the entry points above to select hooks; do not
guess a new registry property or treat selector 79 as an acknowledgement.
That distinguishes a missing completion from an idle assertion or attention
timer that remains disabled despite completion.

### Host validation

`tools/test-fuxi-backlight.py` now executes the production notification fanout
and framebuffer power-request bodies under ASan/UBSan in addition to the
existing parser tests. It covers all argument gates, both message directions,
repeated/nonzero requests, provider isolation, missing children, iterator
allocation failure, state-before-notification ordering, callback property
re-entry without the framebuffer lock, and the unchanged mode-1 direct path.
The framebuffer host adapter also recognizes the IOReturn declaration.
Host tests cannot establish auto-lock restoration on iOS.

Validation for mode 2: 45 complete host scripts passed, including the backlight,
framebuffer, HID/key, idle and governor tests; all eleven sync checks passed.
Twenty-two other scripts require external build inputs. Two full artifact
checks lack generated Metal/WLAN outputs; the Metal staging policy-only check
and M9i compile-only check passed separately. ARM64 syntax checks passed for
both changed translation units against saved XNU 8796 headers, and for the
framebuffer without FUXI. Both kernel-profile CI dry runs and shell syntax
checks passed. No kernel link or device boot was performed.

## Round 2: isolate ON delivery; do not infer a missing brightness acknowledgement

### OBSERVED device results

These results supersede the untested mode 2 hypothesis above. Mode 1 remains
the preferred existing configuration. Durations are the supplied observations,
not timestamps reconstructed from console line numbers.

| Trace / mode | After SpringBoard display-on | Scripted Power result |
| --- | --- | --- |
| `research/it/bl79.log`, mode 1 | Initial auto-off, roughly 20–40 s across the mode 1 runs | First click wakes; this older script also has later clicks |
| `research/it/wake80.log`, mode 1 | Initial on/off at lines 2487/3462 | Single click wakes at 3546; no re-lock for 420 s |
| `research/it/bl2w81.log`, mode 2 | On at 2489; no auto-off for about 100 s | Click locks at 3820 |
| `research/it/bl2long82.log`, mode 2 | On at 2480; no auto-off for about 250 s | Click locks at 3885 |

Mode 2's startup off/on and scripted off have matching interest log lines,
all `result=0x0`: kernel fanout success, not a userland acknowledgement.
Neither mode 2 run measures auto-lock **after a Power wake**; its single click
turned an already-on display off. The startup off before SpringBoard's on
is distinct from the later mode 1 idle off.

All four traces lack a backlight `setProperties`, user-client open or external
method request. Publication's `set #1 ... IODisplayParameters` is our own
initial dictionary; `set #2 ... IOGeneralInterest` installs a notifier.
Neither is a userland brightness request. The first such request would be
logged, so this absence is meaningful.

### OBSERVED static paths: power, transition completion, and attention timeout

These findings are from the same supplied cache/root images inspected offline.
They establish conditional paths, not runtime branch selection. Extracted
images and long disassembly remain only in ignored `research/idle/`.

**CoreBrightness:** the interest callback (`0x1af0e29e4`) sets context `+0x68`
to 1 for ON or 0 for OFF. Apart from logging, it neither writes brightness nor
calls a fade/BLS completion. `_DisplayCreateWithID` initializes this word from
the advertised brightness, so mode 1's initial 65536 can already seed it to 1.
Absence of an ON message does not mean the latch is off.

`___DisplayFadeUpdateFactorFade` (`0x1af001adc`) delays a positive-target ramp
only when its gated-fade argument is set, context `+0x54 != 0`, `+0x68 == 0`
and `+0x58 == 0`. It stores the wait start at `+0x60`. For **less than 1.0 s**
it moves the fade start time to now and returns. At one second it logs
`Ramp delay hit timeout` and falls through to interpolation. ON removes this
delay. It does not start an infinite wait for a brightness write.

When the elapsed fade portion reaches 1, it stores the target (capped at 1),
clears the gated-fade flag at `+0x58`, calls the callback at fade `+0x28` with
its `+0x30` context (`0x1af002088` onward), and clears the fade period. It does
not poll `IODisplayParameters` or await a hardware acknowledgement. Its caller,
`___DisplayFadeCallback` (`0x1aefe2f58`), applies the factor separately. A legacy
physical-brightness path calls `IORegistryEntrySetCFProperties`
(`0x1af0eec08`) synchronously. Our parser already returns success and updates
the dictionary for valid brightness requests; none was observed here.
An upstream path failing to reach that setter remains possible.

**BLS/CBDisplayStateClient:**
`BLSHBacklightOSInterfaceProvider transitionToDisplayMode:withDuration:`
(`0x1ef791c48`) selects CBDisplayStateClient when supported. Its support query
uses the CoreBrightness property `DisplayStateControlSupported`; its completion
handler (`0x1af11cc84`) dispatches
`didCompleteTransitionToDisplayMode:withError:` to its delegate. This is a
CoreBrightness client/service contract, not a registry property to invent on
FuxiBacklight. The legacy branch calls
`BKSHIDServicesSetBacklightFactorWithFadeDurationAsync` and schedules its own
completion timer; its block (`0x1ef792240`) calls that same delegate method.
No brightness write therefore does not prove this branch is stuck.

Scene completion is separate:
`BLSHEnvironmentTransitionState animationCompleteToTarget:forEvent:animated:`
(`0x1ef798f18`) conditionally reports the completed target to its owner.
A framebuffer selector-12 ON does not prove any of these callbacks occurred.

A concrete idle dependency exists in
`BLSHBacklightIdleProvider backlight:didCompleteUpdateToState:forEvent:`
(`0x1ef795e70`). It compares old/new states and a resume condition logged as
`needToResumeIdleTimer`. On a change or needed resume it calls
`updateForBacklightState:` (`0x1ef79519c`), which updates the AW idle timeout
with reset requested and activates the provider for the interactive state
when permitted. The completion path also reports `backlightTurnedOnForEvent:`.
SpringBoard's separate `backlight:didCompleteUpdateToState:forEvent:`
(`0x1c5e2f218`) invokes its pending change completion; the request completion
then notifies transition observers. These are actual completion boundaries
to measure after display-on, rather than equating ON interest with readiness.

**SpringBoard/attention:** `SBIdleTimerFactory` (`0x1c5aa8278`) uses the shared
`SBBacklightIdleTimer` for timer modes 0–2; mode 3 is a disabled timer.
The active timer wraps `ITAttentionAwareIdleTimer` from IdleTimerHosting,
which configures an AW client. No ordinary NSTimer fallback appears in the
inspected resume/reconfiguration paths when the AW client fails. SB timer
`reset` (`0x1c5b3447c`) enables it, resets its expectation and reconfigures
with reset requested. Expiration arrives via
`idleTimer:attentionLostTimeoutDidExpire:` (`0x1c5b34500`); the policy
aggregator subsequently requests a fade-out. Physical display-on alone
does not perform this reset/timeout sequence.

Face detection is only one event source. In
`_reconfigureAttentionClientAndReset:` (`0x1c5b34fdc`), recovery masks are
`0xa5e` for timer mode 1 and `0x856` otherwise. Finite nonzero sampling adds
**0x80** to the maintenance mask. IT translates this descriptor into AW
timeouts and sampling configuration. Disabling Face ID attention removes
sensor sampling but still uses AW for HID timeouts. Missing Face ID hardware
and a missing/nonfunctional attention service are different failure modes.
iPhone X identity alone proves neither that sampling was enabled nor that
attention stayed true: `SBAttentionAwarenessSettings` also checks supported
events bit 7, accessibility and managed settings.

**backboardd:** `BKUserEventTimer isSafeToResetIdleTimer` (`0x100031e50`)
reads its `+0x39` byte. The block queued by `systemShellDidFinishLaunching:`
sets it and posts the safe-to-set notification. The inspected per-wake path
does not clear/re-acknowledge it. `resetTimer:mode:` queues its own timer
work; this is distinct from SpringBoard's IT/AW timer. Forcing this startup
gate or injecting recurring HID activity is not justified by these results.

### GUESS: one explanation for both observations, not an established cause

The leading shared explanation is **initial timer configuration succeeds,
while a later interactive transition/reset path fails to finish or re-arm**.
Mode 1 can expire its initial timeout, then remain physically on after a
Power wake if a BLS/scene completion, SB reset, descriptor/disable assertion,
or AW reconfiguration prevents the next timeout. The 20–40 s boot interval
is consistent with a short idle policy; it does not establish a 30 s effective
auto-lock preference. Lock-screen policy has its own timeouts/sanitization.

Mode 2 changes both OFF/ON latch transitions and request scheduling. ON can
remove the one-second ramp-start delay and expose a completion/attention-reset
ordering problem that initial setup otherwise avoids. Its boot could then
resemble mode 1's failed post-wake cycle. Other interest subscribers or the
workloop change could also matter. The inspected callback does not disable
the idle timer, and mode 1 can already start with its latch set. Thus the
existing comparison **does not isolate ON as the cause** or prove this race.

"Nobody sets brightness, so our driver never completes the nonzero fade" is
not established: this factor fade completes by elapsed time and its off-latch
delay is bounded. A missing upstream display-ready/factor/scene completion
remains plausible, but no kernel acknowledgement ABI has been demonstrated.
Do not invent a ready property, selector reply, brightness request or periodic
power-off workaround. Missing Face ID is another testable hypothesis, not
proof of permanent attention. Console logs do not expose live BLS/IT/AW state.
If the initial off really is an AW idle expiration, a completely absent AW
service throughout boot would be a poor sole explanation for the asymmetry;
the source of that first off and the live service state still need measurement.

### Change: `fuxibacklight=3`, OFF-only interest diagnostic

Mode 3 publishes the same service/brightness as modes 1/2. It preserves mode
2's serialized framebuffer path and sends OFF after every accepted zero-state
request, including duplicates. For nonzero requests it updates framebuffer
power normally, **withholds ON**, logs the decision, and returns success
without traversing interest clients. This deliberately leaves an OFF latch
stale after wake; it is a discriminator, not a correct power protocol or a
claimed auto-lock repair. The bounded ramp-delay path remains available if
its other predicates hold.

Absent/0/unknown values remain disabled; mode 1's direct path and mode 2's
both-message path are unchanged. No publication notification, delayed ON,
timer, brightness mutation on power changes or hardware access is added.
Later positive brightness requests are stored but never release an ON.
Power logs retain the first-64/each-256 sampling policy.

### Exact acceptance experiment

Compare **modes 1, 2, 3** with the same root, other boot arguments and GPU
configuration, using `fuxikeyscript=300 fuxikeyscriptn=1` for each. No touches
or other keys, and no attention preference change during this comparison.
Repeat the triplet under similar cooling/boot conditions. Record startup and
at least 420 s after the single scripted Power release.

1. Record SpringBoard display-on, spontaneous off before the script and the
   click's actual direction. Script text `expect lock` does not measure it.
2. Mode 3 must log `FUXI BACKLIGHT: OFF-only interest experiment enabled; ON withheld`.
   For each sampled on, require `FUXI BACKLIGHT: power #... on message=0xe0000230 withheld experiment=off-only`.
   OFF still logs `message=0xe0000210 result=0x0 logical-only`.
   No mode 3 ON should report a fanout result. Retain brightness/open/method
   diagnostics: a new request changes the evidence.
3. If mode 3 restores initial auto-off while mode 2 reproduces suppression,
   ON delivery is implicated independently of retained OFF/gating. If mode 3
   still suppresses it, isolate OFF/gating next; an ON-only explanation has
   failed this test. Neither result measures the userland completion state.
4. If initial auto-off occurs, the click wakes. Require another spontaneous
   off within 120 s and retain the full 420 s. Initial recovery without
   post-wake recovery leaves the original failure unresolved. Without initial
   auto-off the click locks, so post-wake acceptance is **not measured**.
   A separate `fuxikeyscript=300 fuxikeyscriptn=2` run can then lock and wake
   (the two clicks are eight seconds apart); verify directions from FB logs
   and record 420 s after the second release.
5. A possible workaround requires repeatable initial **and** post-wake
   auto-off plus working explicit Power transitions and no new errors.
   Even success does not validate a deliberately missing ON as a final driver.

If wake idle still fails, the next direct measurement is a temporary userland
observer: BLS requested/completed states, scene/ramp completion, pending SB
completion, active idle descriptor/disable assertions, SB timer reset, IT
enabled state, AW resume/reset errors, timeout callbacks and configuration
generation. Capture before/after Power release and at +60/+120 s through a
verified console sink; ordinary stderr/OS-log silence proves nothing.
No observer or device experiment was run in this offline round.

### Proposed root experiment, not applied

In the mobile user's **`/var/mobile/Library/Preferences/com.apple.springboard.plist`**,
merge this Boolean entry, preserving other entries and ownership, before a
fresh boot to avoid cfprefsd/SpringBoard cached values:

```xml
<key>SBDisableAttentionAwareness</key>
<true/>
```

The spelling/type/default false are supported by `SBIdleTimerDefaults`
`_bindAndRegisterDefaults` (`0x18e6547ac`, binding at `0x18e654900` onward).
The abstract domain initializes to `com.apple.springboard` (`0x18e613714`).
`SBIdleTimerDescriptorFactory _shouldUseAttentionSensor` (`0x1c57f6290`)
explicitly returns NO when this setting is true. It disables sensor sampling
for this policy; it does **not** replace the AW timer or repair an absent AW
service. Do not substitute a guessed auto-lock key or change timeout settings
in the same experiment.

Use a separate matched pair with **mode 1**, `fuxikeyscript=300 fuxikeyscriptn=1`,
key absent/default versus true. Retain 420 s after wake and require both
initial and post-wake auto-off. Restore the saved original entry (or remove
the newly added key) before a fresh boot. If a verified userland log sink is
available, `_shouldUseAttentionSensor: NO because SBDisableAttentionAwareness is set`
confirms consumption; it is not expected on the kernel console. A negative
result does not rule out AW service failure. No root preference/image edits
were made here.

### Round 2 validation

The ASan/UBSan host test executes production parser, notification and framebuffer
request/action bodies. It covers gates, duplicates/nonzero states, mode 2's
unchanged message sequence, mode 3's OFF-only fanout and withheld log lines,
callback re-entry outside the framebuffer lock on the gate, no delayed ON
after brightness writes, missing children and iterator failure. Canonical
sources regenerate patches 0109 and 0106. Host tests cannot prove iOS timer
re-arm or device acceptance.

Round 2 checks passed: the backlight ASan/UBSan test, framebuffer/UFS host
test, HID/key, idle and governor tests, all eleven patch sync checks, ARM64
syntax of both changed translation units against the saved XNU headers, and
framebuffer syntax without FUXI. Both kernel-profile CI dry runs and shell
syntax passed. No kernel link, device boot or root edit was performed.

## Device results (modes 1–3, attention awareness)

**OBSERVED** on the device, with no input apart from one scripted Power
click (`fuxikeyscript=S fuxikeyscriptn=1`):

| Configuration | Auto-off after SpringBoard turns the display on | Re-lock after a Power wake | backboardd first frame |
| --- | --- | --- | --- |
| No backlight service | none within 330 s (one boot went off after ~290 s) | not measured | 7.6 s |
| `fuxibacklight=1` | after ~20–40 s | none within 420 s | 12.4–12.8 s |
| `fuxibacklight=2` | none within 250 s | not measured | 12.6–18.3 s |
| `fuxibacklight=3` | none within 260 s | not measured | 13.3 s |
| `fuxibacklight=1` + `SBDisableAttentionAwareness=true` | after ~30 s | none within 270 s | 12.4 s |

Delivering the OFF interest message during the boot-time display off is
enough to suppress the later auto-off. Turning attention awareness off
does not restore the re-lock. With the display off, the settled CPU
temperature was about 4 C lower than in a run that left it on
(**GUESS:** both panel and scanout power; back-to-back boots add residual
heat). Mode 1 stays the recommended setting despite the slower first
frame; re-lock after wake remains open.



## Round 3: startup delay investigation (incomplete)

**No startup performance fix or device acceptance is established.** The
justified change in this round is additional opt-in measurement. Publishing
invented capabilities, changing initial brightness, or deferring discovery
would change the policy that currently permits boot auto-off without an
identified timeout to repair.

### OBSERVED: startup comparison

The supplied `first-bl79` and `first-ctl85` traces have these ordered markers.
Line numbers establish order, not elapsed time between untimestamped lines.

| Marker | Mode 1 (`first-bl79`) | Service absent (`first-ctl85`) |
| --- | --- | --- |
| Initial interest / brightness read | 611–617: installs `IOGeneralInterest`, reads value 65536 | absent |
| QuartzCore backlight-presence lookup | 1366: FuxiBacklight | 1310: none |
| CoreBrightness backlight enumeration | 1958: one service | 1922: zero services |
| Color initialization reads | 1971–1982: whitepoint, CCT range, pre-strobe; 2023–2026: PAB/AML | absent |
| Night Shift initialization | 1984–1990: Night mode, CoreDuet, default status | absent |
| First frame marker | 2225: 12831.294 ms, GPU 1.852 ms | 2054: 7593.691 ms, GPU 1.849 ms |

The first-frame difference is **5237.603 ms**. Before that marker, both traces
record 17 pipeline-cache misses; their logged durations total 676.752 versus
448.762 ms. That 227.990 ms difference does not account for five seconds,
although these totals are not a complete CPU/scheduling profile. The command
lines also differ in scripted-key and scanout-check options; use matched
arguments for the next experiment.

Read 64 is reached before the first frame. Subsequent reads 65–255 are
suppressed by the existing sampler. The early interest/brightness read has
no caller identity; it must not be attributed to backboardd solely because
that process has already launched. No backlight open, external selector or
`setProperties` appears before the first frame. Installing an interest is
not a delivered power message.

### OBSERVED: offline code findings and exclusions

The locally supplied shared cache was inspected with the existing disassembler
and Ghidra headless decompiler. Extracted images and long analysis output
remain outside the commit. Addresses below are specific to that cache.

| Entry point | Finding |
| --- | --- |
| QuartzCore `___CADeviceHasBacklight_block_invoke`, `0x1884cb708` | Tests `backlight-control = true` once and caches whether a service exists. It does not read brightness or wait for a property. |
| CoreBrightness `-[CBDisplayContaineriOS findBacklight]`, `0x1af0cb224` | Enumerates that predicate, retains a service and handles display arrival. The successful branch enables additional container/color setup. |
| CoreBrightness `___54-[CBDisplayContaineriOS handleCBDisplayContainerStart]_block_invoke`, `0x1af0cd614` | The internal-display path finds the backlight before creating the color module; failed discovery skips that creation. |
| CoreBrightness `-[CBColorModuleiOS initColorStruct]`, `0x1af07c598` | Searches the service/parents for whitepoint and CCT properties, matching the observed read sequence. |
| CoreBrightness `___18-[BLControl start]_block_invoke`, `0x1af0a15a4`; `-[BLControl waitForALSArrival]`, `0x1af0a672c` | A device-type-gated HID ALS-arrival wait uses a 5,000,000,000 ns semaphore deadline. It runs before backlight enumeration, so finding that constant does not explain the service-dependent increment. |
| CoreBrightness `___DisplayFadeUpdateFactorFade`, `0x1af001adc` | Its off-latch ramp delay has a one-second bound. Mode 1 advertises nonzero initial brightness and sends no OFF interest. This is not evidence for a five-second startup fade. |
| CoreBrightness `-[NightModeControl initWithSupportObject:queue:callback:]`, `0x1af0428e8`; `saveStatusToPrefs:`, `0x1af04229c` | Default status creation reaches `NSUserDefaults synchronize`. The trace reaches this setup, but does not time the synchronize call. |
| CoreBrightness `-[SunriseSunsetProvider copySunriseSunsetInfo:]`, `0x1af027df8` | Its semaphore deadline is supplied by the caller. NightModeControl initializes that timeout to **20 ms**, not five seconds. |

No inspected path establishes a five-second `IOServiceWaitQuiet`, missing
`IODisplayConnect`, property retry, or required AAB/ALS capability that can
be safely repaired by adding a registry property. In particular, the
`AppleARMBacklight` conformance checks are real, but changing conformance
would enable an additional private driver path; it is not a demonstrated
startup fix.

### GUESS and the opt-in change

**GUESS:** the extra time is in the additional container/color/Night Shift
startup work or a synchronous dependency it invokes. Preference synchronization
is one candidate; no particular call is yet the established cause. Both
the property sampler's blind interval and the lack of caller/timing markers
prevent the supplied console logs from locating the entire delay.

Use **`fuxibacklight=1 fuxibacklighttrace=1`** for the next diagnostic boot.
The second argument accepts exactly 1 and is effective only with mode 1.
It logs the first **512** read/snapshot events, then every 256th event, and
adds caller process, monotonic `uptime_us` and `io_us` for sampled reads,
individual sets and whole-table serialization. Each stamp shares its event
number with the existing property/value lines. `io_us` times the underlying
registry operation, excluding the diagnostic dictionary printing; timestamps
between events also include logging, scheduling and userland work.

Look for:

```text
FUXI BACKLIGHT: startup trace enabled mode=1 read_limit=512 interests=none
FUXI BACKLIGHT: trace set #2 uptime_us=... io_us=... process=...
FUXI BACKLIGHT: set #2 key=IOGeneralInterest ...
FUXI BACKLIGHT: trace read #... uptime_us=... io_us=... process=backboardd
FUXI BACKLIGHT: read #... key=... ...
FUXI MTL: backboardd first_frame_ms=... gpu_ms=...
SWEET FB: display off
```

Pair stamps by event number, not adjacency when CPUs interleave output.
Metal's first-frame duration is relative to its own initialization marker;
it is **not absolute uptime**. Compare the startup clock markers before
subtracting timestamps. A multi-second `io_us` implicates a registry operation;
a gap between quick reads implicates intervening work. If the gap remains
inside opaque userland initialization, a temporary userland entry/exit probe
is still needed to identify that call.

Defaults, existing mode gates, brightness values, publication ordering and
framebuffer power paths are unchanged. The diagnostic adds no interest
fanout, deferred publication, policy timer, or hardware writes. Modes 2/3
ignore the new argument. It therefore preserves mode 1's existing auto-off
protocol, but host testing cannot prove the extra logging has no timing effect.

### Acceptance experiment and validation

Compare repeated boots with service absent, mode 1, and mode 1 plus tracing,
using the same kernel/root, GPU/cache state, cooling, all other arguments
and no input before spontaneous off. Retain the initial display-on and at
least 120 seconds of idle observation. Do not send interest messages.
The trace boot is for localization; console overhead may perturb its timing.

A subsequent performance fix is accepted only if matched repeated boots give
**`first_frame_ms <= 8500` and spontaneous auto-off approximately 30 seconds
after SpringBoard display-on** (the observed mode 1 range is 20–40 seconds).
A faster first frame with missing auto-off fails. Record scripted Power
direction separately; the existing post-wake re-lock failure remains open.
This diagnostic is not claimed to meet the first-frame target.

Host validation executes the production registry overrides and stamp formatter
with controlled monotonic time. It checks exact opt-in gates, original read
sampling without tracing, extended sampling with tracing, unchanged returned
values/failures, snapshot release, absence of fanout, and existing parser/power
regressions under ASan/UBSan. Patch 0109 remains generated from canonical sources.

Round 3 checks passed: backlight ASan/UBSan, framebuffer/UFS, HID/key, idle
and governor host tests; all eleven patch sync checks; full ARM64 syntax
against saved XNU headers for backlight/framebuffer/UFS and framebuffer
without FUXI; both kernel-profile CI dry runs; shell syntax and diff whitespace.
No kernel link or device boot was performed. The precise stall and a
performance change remain unfinished.

## Round 4: Night Shift registration, timing and a preference experiment

**Result:** the inspected path contains synchronous dependencies, but neither
the supplied trace nor static analysis establishes a five-second Night Shift
stall. There is an exact preference for disabling scheduling; it does **not**
prevent SunriseSunsetProvider construction or CoreDuet registration. No
registry-based bypass was established, so no `fuxibacklightnonight` argument
or driver change is introduced. A startup-performance fix remains unproven.

### OBSERVED: the new trace materially limits the hypothesis

In `research/it/trace87.log`, mode 1 plus tracing gives backboardd reads
39–64 at monotonic uptimes 56,471,584–56,529,249 us: **57.665 ms**. Registry
operations take 0–2 us in the supplied trace. This rules out a five-second
registry read/set in that burst; it does not profile work before the burst.

The original, unnormalised NSLog prefixes also contain thread identifiers and
relative timing that the sorted/normalised message comparison removes:

| Message | Thread | Displayed fractional timestamp, same second |
| --- | --- | --- |
| `Night mode init` | 259 | -513 |
| `SunriseSunsetProvider initialisation` | 259 | -512 |
| `CoreDuet loaded dynamically` | 259 | -511 |
| `Creating defaults status info` | 259 | -509 |
| `Identifier called: com.apple.CoreBrightness.registrationForSunsetSunrise` | 309 | -508 |
| `dataDictionary : { status = 0; }` | 350 | -506 |

The boot wall clock's fractional formatting is abnormal around its epoch;
these same-second differences span approximately **7 ms**, and the messages
are independently bracketed by the monotonic read stamps above. In particular,
the provider constructor and its registration call have returned before the
same thread prints `Creating defaults status info`, about 4 ms after
`Night mode init`. The dictionary dump is on a different thread. It is not
evidence that the startup thread waited for a location result for five seconds.
The earlier comparison remains real: first frame is about 12.4–13.3 s with
the service versus 7.6 s without; this boot reports 13,249.543 ms.

### OBSERVED: local cache and Ghidra call paths

These are cache-specific unslid addresses. CoreBrightness was inspected with
the existing offline disassembler and Ghidra headless decompiler; the matching
CoreDuetContext and libEDR code was inspected offline as well. Extracted Apple
images and long analysis output remain ignored and outside the commit.

| Entry point | Blocking behavior / ordering |
| --- | --- |
| `-[BLControl start]`, `0x1af0a14e8` (dispatch at `0x1af0a1574`) | Synchronously dispatches startup to its internal queue. Its startup block enumerates displays/backlights. A slow nested call can hold up the caller. |
| `-[BLControl findBacklight]`, `0x1af0a21f0` | After a discovered container successfully starts, calls `addDisplayContainer:` at `0x1af0a2424`. No discovered container means this call is skipped. |
| `-[BLControl addDisplayContainer:]`, `0x1af0a77c4` | Constructs the shared NightModeControl if absent, or adds the new support object if already present. **No Night Shift capability or saved-schedule test guards construction here.** |
| `-[NightModeControl initWithSupportObject:queue:callback:]`, `0x1af0428e8` | Constructs SunriseSunsetProvider at `0x1af042a40`, then reads saved status at `0x1af042d54`. The timeout stored at object `+0x18` is 20 milliseconds. Status/scheduling preferences cannot skip the earlier constructor. |
| `-[SunriseSunsetProvider initWithCallback:]`, `0x1af129e30` | Calls `dlopen` for CoreDuetContext, `_CDClientContext userContext`, builds the sunrise/sunset change registration and calls `registerCallback:` at `0x1af12a054`, inline. Its semaphore is created with count **1**; construction does not wait on it. |
| CoreDuetContext `+[_CDClientContext userContext]`, `0x190ae2bec`; `initWithEndpoint:`, `0x190ae2610` | Singleton `dispatch_once`; initialization synchronously dispatches connection setup at `0x190ae2808`. `currentConnection`, `0x190ae49d8`, also uses `dispatch_sync`. These can block the calling thread; neither inspected queue operation specifies a five-second deadline. |
| CoreDuetContext `-[_CDClientContext registerCallback:]`, `0x190aed2d0` | Runs `retryTimes:3 block:` inline at `0x190aed518`. Its block at `0x190b02048` gets a `synchronousRemoteObjectProxyWithErrorHandler:` and sends `registerCallback:` at `0x190b020e0`. This is a real synchronous XPC dependency, not a CoreBrightness five-second semaphore. |
| CoreDuetContext `retryTimes:block:`, `0x190ae9b14` | On a retryable error, sleeps **1 second**, then multiplies the next sleep by `1 + arc4random_uniform(3)`. Up to three failed attempts can sleep 3–13 seconds in total, including a sleep after the last failure; some sequences total 5 s. No error means no retry sleep. The observed constructor timing does not support these sleeps on this boot. |
| `copySunriseSunsetInfo:`, `0x1af027df8`; worker at `0x1af12a5a4` | First semaphore acquisition uses NOW; context lookup runs on a global worker. The explicit semaphore deadline is the supplied milliseconds, normally **20 ms**. The worker calls `copySunsetSunriseInfoFromContext`, `0x1af129aec`, whose dictionary subscript ultimately uses synchronous CoreDuet XPC. It does that lookup before acquiring the provider monitor. A blocked worker is not automatically a blocked startup thread; monitor acquisition itself has no explicit timeout. |
| Provider registration callback, `0x1af12a108`; `updateSunriseSunsetInfo`, `0x1af12a6ec` | `Identifier called:` is printed by the delivered change callback, not by an entry/exit probe around registration. The update uses semaphore NOW and queues the context lookup on a global worker (`0x1af12a828`). NightModeControl's supplied callback (`0x1af04343c`) also forwards asynchronously. This explains the different logged threads without a five-second startup wait. |
| Provider `dealloc`, `0x1af12a43c` | Does contain a semaphore **FOREVER** wait at `0x1af12a480`, after entering the provider monitor. It can block teardown while a query is outstanding. The retained startup provider is not observed being destroyed here; this is not a demonstrated startup timeout. |
| `copyStatusDictionaryFromPrefs`, `0x1af042384` | `NSUserDefaults initWithSuiteName:"com.apple.CoreBrightness"`, then `synchronize` at `0x1af0423e0`, then reads `CBBlueReductionStatus`. This synchronous preference access precedes the observed defaults-status message; it did not consume five seconds between provider construction and that message here. |

The registration error block sets the retry Boolean when its NSError is
non-null. A missing/slow context service can therefore block this synchronous
chain in other circumstances. The client connection setup inspected here
does not set an explicit five-second XPC timeout. It would be incorrect to
label the observed delay a fixed five-second CoreDuet timeout.
Default-status creation builds an in-memory dictionary; its log alone does
not establish a `saveStatusToPrefs:` call or another preference synchronize.

The caller dependency is statically real: backboardd creates BrightnessSystem
in its shared brightness-controller initialization (`0x100079d24`), and
BrightnessSystemInternal initialization calls BLControl start
(`0x1af05229c`). Runtime **thread identity for the delayed first frame remains
unmeasured**: the Metal first-frame marker has no thread ID or stack. The
NSLog thread IDs above demonstrate that the extra messages are not all on
one thread. No sampled stack or entry/exit timing places the five-second
increment inside this call chain.

Other matching constants are not proof either. BLControl's ALS-arrival wait
is five seconds but precedes enumeration and uses a device-type gate.
CBAPEndpoint property/command methods also have explicit five-second
semaphore deadlines (for example `copyProperty:` waits at `0x1af03f2e4`);
no observed call establishes their involvement here. QuartzCore's
`CA::EDRClient::client`, `0x18833a168`, becomes eligible when a backlight exists,
but calls libEDR's **asynchronous** client-creation wrapper with timeout 0.
libEDR's separate synchronous default of 5000 ms is not that call. None of
these constants can be substituted for a measured startup stack.

### OBSERVED: capability names are not a registry bypass

`CBU_IsNightShiftSupported` (`0x1af09b004`) calls `MGGetBoolAnswer` with the
opaque key `F1Xz9g1JORibBS9DYPUPrg`. `CBColorModuleiOS initColorStruct`
(`0x1af07c598`) uses that result to **publish** `SupportsNightMode` inside
its own `SupportedColorFX` dictionary. It does not read either name from
FuxiBacklight. `SupportsAmbientColorAdaptation` is another computed module
output. Publishing those names with false on the kernel service would not
change these inspected tests, and addDisplayContainer still constructs
NightModeControl independently of this capability result.

`BlueLightReductionDisableFlags` is removed from saved status during
initialization (`0x1af042e00`); it is not a persistent initialization-disable
switch. `CBDisplayPresetDisableNightShift` is handled after construction and
changes policy, not the earlier provider-registration call. No exact registry
property/value was found that safely bypasses this path while preserving the
backlight discovery contract. Defaults and modes 1–3 therefore remain intact;
mode 1 still sends **no interest messages**.

### Proposed root edit: disable Night Shift scheduling, not registration

For a separate preference experiment, merge the following top-level entry into
**`/var/mobile/Library/Preferences/com.apple.CoreBrightness.plist`** before a
fresh boot, preserving all unrelated entries, ownership and permissions.
The trace's Metal gate reports backboardd **uid=501**, and the inspected
local launchd plist names its user `mobile`; the NSUserDefaults suite uses
the current user. The root user's
`/var/root/Library/Preferences/com.apple.CoreBrightness.plist` is not the
target for this observed mobile process. This is an on-device plist location,
not a host artifact path. No root/image edits were made in this round.

```xml
<key>CBBlueReductionStatus</key>
<dict>
    <key>Version</key><integer>1</integer>
    <key>BlueReductionEnabled</key><integer>0</integer>
    <key>BlueReductionMode</key><integer>0</integer>
    <key>BlueReductionSunScheduleAllowed</key><integer>0</integer>
    <key>BlueLightReductionSchedule</key>
    <dict>
        <key>NightStartHour</key><integer>22</integer>
        <key>NightStartMinute</key><integer>0</integer>
        <key>DayStartHour</key><integer>7</integer>
        <key>DayStartMinute</key><integer>0</integer>
    </dict>
    <key>BlueLightReductionRevertToSunriseSunset</key><integer>0</integer>
    <key>BlueLightReductionAlgoOverride</key><integer>0</integer>
</dict>
```

All shown leaves are **plist integers**, matching the NSNumber/intValue
checks in `parseStatusDictionary:intoStruct:shouldUpdatePrefs:`
(`0x1af0416c0`), not guessed Boolean property names. Version 1, enabled, mode,
sun permission and the four schedule fields are required even with mode 0;
a short dictionary containing only the mode is rejected. Zero mode is the
unscheduled branch; mode 1 selects sunrise/sunset in `updateTransitionTimes:`
(`0x1af045070`). Zero enabled clears the manual-on state. Zero override/revert
avoids restoring a previously forced transition. The hours are the constructor's
defaults and are inactive with mode 0. A valid existing status can instead be
merged with these fields; preserve a backup of the original whole entry for
rollback, including any absent-file/absent-key state.

This is the cheapest **verified scheduling control found**, not a verified
way to suppress initialization. The constructor sets the internal
`AutoBlueReductionEnabled`/`BlueReductionAvailable` availability outputs true
even after valid status parsing; setting those saved fields false is not an
initialization gate. Provider construction, CoreDuet callback registration,
and preference synchronization still happen with the proposed status. Default
status already has mode 0 and enabled false in this cache, so the observed
`Creating defaults status info` boot was already unscheduled. **GUESS:**
preseeding valid status could change later work/timing, but a five-second
startup improvement is not predicted by the inspected scheduling branch.

### Acceptance experiment, logs and unresolved measurement

Keep **`fuxibacklight=1`** and all other kernel/root/GPU/cache settings matched.
Compare repeated baseline boots against the proposed status, with no input
before initial spontaneous off. Use `fuxibacklighttrace=1` on a separate
localization pair; keep tracing identical within each pair. Do not combine
this with interest modes 2/3 or an attention-preference change.

Look for `Defaults found and valid` instead of `Creating defaults status info`
or `Defaults found and invalid`. `Night mode init`, provider initialization,
the registration identifier and a dictionary dump can still appear: **their
disappearance is not an expected result of disabling the schedule**. An
invalid/defaults message means the intended preference was not consumed as
a valid status. Retain the published-brightness line, first-frame marker and
`SWEET FB: display on/off` lines. Acceptance requires both
**`first_frame_ms <= 8500`** and spontaneous off **approximately 30 seconds
after SpringBoard display-on** (record the actual 20–40 s baseline range),
with the service present and mode 1 sending no power interests. Restore the
original status before a rollback boot. The existing post-wake failure is
separate and remains open.

If this does not meet both targets, it is a failed performance experiment,
not evidence to add invented capability properties. The next discriminating
measurement is paired monotonic entry/exit markers and thread IDs around
backboardd's BrightnessSystem initialization/BLControl start, the provider
constructor/registration, and the first render/submit. Capture a stack during
any actual multi-second interval, including the waiting caller and owning
queue. That distinguishes an earlier ALS wait, XPC retry, another synchronous
dependency, or rendering setup from fast Night Shift messages. **Cause remains
GUESS; no five-second Night Shift stall or accepted mitigation is claimed.**

Round 4 validation passed: backlight ASan/UBSan, framebuffer/UFS, HID/keys,
idle and governor host tests; all eleven patch sync checks; both kernel-profile
CI dry runs, CI shell syntax and diff whitespace. The documented XML parses
as a plist with the stated integer values. No kernel source or generated patch
changed, so no new kernel-gate host test is applicable. No kernel link, device
experiment, root edit or performance acceptance result is claimed.

## Round 5: backboardd stalls on libEDR; AppleARMBacklight conformance (4 Nov 2026)

**OBSERVED** (`h-e0`, kernel 3fe7fd6, boot line identical to `h-v33b` apart
from `fuxibacklight=1`): backboardd drew 78 GPU frames in about 10 minutes,
`hz=0.00-0.50`, first frame at 11.4 s; `h-v33b` drew 6,927 frames. No
failures, `first_frame_ms=11367`, and no `brightness #` line although the
slider was moved. One frame every ~5 s, and the earlier +5.2 s first-frame
delay of mode 1, match a 5 s Mach RPC timeout.

**Static chain** in the local iOS 16.7 shared cache (`~/work/dsc`, unslid;
read with a small Mach-O/selector reader and capstone):

| Step | Address | Finding |
| --- | --- | --- |
| QuartzCore `___CADeviceHasBacklight_block_invoke` | `0x1884cb7b4`/`0x1884cb7c0` | `IOServiceGetMatchingService({backlight-control=true})` sets the cached byte `0x1dcdf8538` |
| QuartzCore libEDR registration | `0x1882d5fe8` | Only when that byte is 1: `EDRClient::client` (`0x18833a168`, `EDRCreateClientForDisplayWithTimeoutAndCallback`), then `EDRRegisterCallbackBlockOnQueue` (`0x1882d6200`) with a CA mutex taken |
| libEDR client | `0x1bef7ba34`, `0x1bef7d7d8` | Connects to `com.apple.libEDR.Notification` (`bootstrap_look_up`, 10 000 parameter); RPCs are `mach_msg` with send+receive timeouts, e.g. `EDRIsEnabled` passes 5000 ms (`0x1bef757e8`) |
| CoreBrightness server start | `0x1af095398` | `CBEDRServerStartServer` (dlsym'd `EDRServerStartService`) is reached from `DisplayCreateWithID` (`0x1af0d8510` → `0x1af0e0e2c`, unless `DisplaySyncBrightnessTransactions`) and `-[CBEDRModule initWithQueue:display:colorModule:andDisplayModule:]` |
| CoreBrightness `-[CBDisplayContaineriOS setupInternalModules]` | `0x1af0cec74` | Creates `-[CBDisplayModuleiOS initWithBacklight:queue:display:]` (`0x1af035708`, which calls `DisplayCreateWithID`) and the brightness control modules **only if `IOObjectConformsTo(backlight, "AppleARMBacklight")`**; otherwise only the IOHIDDevice (external display) branch |
| `-[CBDisplayContaineriOS findBacklight]` | `0x1af0cb398` | Same conformance picks the color-module type (1 vs 2); the color module is created either way, which is why mode 1 still logged the whitepoint/CCT reads |

So with `backlight-control` alone QuartzCore becomes a libEDR client of a
server nobody starts, and no brightness module exists to send `setProperties`.
UniXNU-sheng reached the same conclusion on device for iOS 17 (commit 978e3f7,
record int-236: "the display server waited forever on a libEDR notification
nobody serves") and fixed it by making its backlight conform to
`AppleARMBacklight` (`package/AppleARMBacklight.cpp`, `publishFirst`).

**Change:** `FuxiBacklight : AppleARMBacklight : IODisplay : IOService`.
`AppleARMBacklight` is an empty metaclass: this kernel loads no AppleARMPlatform,
so the name is free (UniXNU's duplicate-class panic, int-229, cannot happen).
Our kernel has no sandbox, so no `iokit-set-properties` class rule applies.
Gates, properties, interest modes and the parser are unchanged; the publish
line now ends in `conforms=AppleARMBacklight`.

**Acceptance (device, not yet run):** `fuxibacklight=1`, otherwise the
`h-v33b` line. Require backboardd `hz` back near the no-service boot, first
frame near 7-9 s, and `FUXI BACKLIGHT: brightness #... requested=...` when
the slider moves. Record any `open`/`method` lines: the display module may
now open the service's user client (all selectors still return unsupported).

## Kernel auto-lock (`fuxiautolock=S`)

The kernel side of auto-lock lives in `xnu/fuxi-h1/fuxi_keys.c`. After the
first display off, a one-second thread presses Power (the same click the
key script injects) once the display has been on for `S` seconds
(10–3600) without a key or touch report, so iOS locks through its normal
Power path. The backlight service is not needed.

**OBSERVED** (no backlight service, `fuxiautolock=60 fuxikeyscript=300
fuxikeyscriptn=1`): `autolock press 1` about 60 s after SpringBoard's
display on, then `SWEET FB: display off`. The scripted Power at 300 s woke
the display, and `autolock press 2` locked it again 60 s later. The first
frame came at 9.0 s, compared with 12.4–13.3 s with `fuxibacklight=1`.
Recommended: `fuxiautolock=30` without `fuxibacklight`. It also locks
while a video plays with no input, because it does not see iOS idle-timer
assertions.
