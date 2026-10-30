# arm64e, stage 0: kernel PAC self-test

Goal: before building an arm64e kernel (iPhone XS 16.7 userland first, iOS 26/27 later), find out whether every
SM8550 core we run can be programmed with pointer-authentication keys at EL1 under Gunyah. UniXNU-sheng (same SoC)
boots an arm64e iPadOS 27 kernel that programs the keys itself, but it only verified that on the boot core. We run
7 of 8 cores (A510, A715, A710, X3; `fuxicores`).

## Switch: `fuxigpuncimport` bit 25 (value 33554432), default off

Free in the GPU parsers (`fg_abi_init`, `fuxi_gpu_scanout_configure`). Bits 8/16/32 are the present-ordering lane, bit 22 is the
direct-DPU-scanout survey and 23-24 are reserved for later DPU stages. Adding it costs one command-line byte:
`fuxigpuncimport=2948728` becomes `fuxigpuncimport=36503160`. With the bit clear, no register is touched. The only cost is one extra PC range compare per
synchronous exception, against an empty range.

## What runs (patch 0114, `xnu/fuxi-pac`)

`SweetCPU::initCPU` calls `fuxi_pac_probe_cpu` on each CPU, the boot CPU and each secondary as it comes up, with IRQ/FIQ
masked. It does this before `startAll` sees the CPU initialized. Every probe instruction sits in one table
(`fuxi_pac_stub`, raw `.inst` encodings for the PAC ops, `S3_0_C2_Cx_y` key registers, so the assembler needs no pauth
support). While a CPU is armed, `sleh_synchronous` skips a synchronous EL1 exception at a table address (`ELR += 4`) and
records its ESR. This covers a hypervisor-injected UNDEF for a trapped key register and an FPAC fault of a tampered AUT.
Steps, in order (each is `kprintf`'d before it runs and kept per CPU):

1. `guard`: one UDF. It must trap and be skipped. This proves the hook before anything risky runs. With `guard=0` nothing else runs.
2. `id`: ID_AA64ISAR1_EL1, ID_AA64ISAR2_EL1, SCTLR_EL1.
3. `keyrd`/`keywr`: the ten APIA/APIB/APDA/APDB/APGA Lo/Hi halves are saved, written with a fixed pattern (the same on
   every CPU) and read back.
4. `sctlr`/`auth`: set EnIA/EnIB/EnDA/EnDB. For IA, IB, DA and DB: PAC, then AUT of a kernel pointer with a modifier, and
   two tampered AUTs (other modifier, one PAC bit flipped). With FPAC the tampered AUTs run only under a proven guard.
5. `ga`: PACGA, when GPA/GPI/GPA3 say it exists. The result must be deterministic, depend on the modifier, and have the
   low 32 bits zero.
6. `restore`: the original keys are written back and read back. PACIA is redone with the original key (`key_used`: the
   hardware really uses the key that read back). Then the original SCTLR is written back and read back.

`startAll` prints `FUXI PAC: cpu=N probe begin` before each `processor_start`, one result line after each "came alive",
and a summary after "N/N CPUs initialized". If a CPU times out (5 s), its line reads `incomplete step=NAME` before the
existing panic.

## Log lines (device output not yet observed)

```
FUXI PAC: cpu=N probe begin
FUXI PAC: cpu=N midr=… isar1=… isar2=… apa= api= apa3= gpa= gpi= gpa3= en=<IA IB DA DB> guard= keys_rw=ok|ignored|trap|partial|none
  keys=<ok>/<readable>/<ignored>/<trapped> restored= pac_ok= auth=<IA IB DA DB GA> key_used= auth_fail_ok= fail=<IA IB DA DB> fpac=0|1|2 sig= traps= esr=
FUXI PAC: summary cpus= probed= pauth= keys_rw_ok= pac_ok= auth_fail_ok= fpac= restored= consistent= verdict=
```

`fail` per key: `P` returned a poisoned pointer (no FPAC), `F` FPAC fault (EC 0x1c), `E` another trap, `X` the tampered
pointer was accepted, `-` not run. `fpac`: 1 FPAC, 2 FPACCOMBINE (APA/API/APA3 ≥ 4). `sig` is PACIA of the test pointer
under the test key. The summary compares the IA/IB/DA/DB/GA signatures of all CPUs whose keys read back.

Observed on boot S224 (kernel 38ded11, arm64, bit 25 set):
`FUXI PAC: summary cpus=7 probed=7 pauth=7 keys_rw_ok=7 pac_ok=7 auth_fail_ok=7 fpac=7 restored=7 consistent=1 verdict=ready`.
cpu6 (X3, midr 0x411fd4e0): `isar1=0x10111101211052 isar2=0x0 apa=5 api=0 apa3=0 gpa=1 en=0000 keys_rw=ok keys=0x3ff/0x3ff
auth=11111 fail=FFFF fpac=2`. So EL1 programs all ten key halves on every core, a failed AUT raises FPAC, and the
algorithm is QARMA5 (APA=5, no PAuth_LR). Stage 1 is designed for that path.

## What each outcome means for arm64e

- `verdict=ready`: every core takes the keys at EL1, signs and authenticates with all four keys, rejects tampering,
  and computes identical signatures. An arm64e kernel can program its own keys on every core, and threads can migrate
  across clusters. Next: an arm64e kernel build (stage 1).
- `consistent=0` / `verdict=mixed`: the cores use different algorithms (APA vs API vs APA3) or key handling. Pointers
  signed on one cluster fail on another. arm64e would need one cluster type, or a common algorithm.
- `keys_rw=ignored`: Gunyah drops EL1 key writes (HCR_EL2.APK clear, write-ignored). The keys are fixed by the
  hypervisor. A kernel cannot give processes separate keys, so arm64e userland only works with a single shared key (weak),
  and user/kernel separation needs another approach.
- `keys_rw=trap` (`esr` EC 0x00 injected): EL1 key access is not allowed. Same conclusion, and the kernel must not touch
  the keys. With `auth=00000` and a trapped AUT (EC 0x00), PAC instructions are also disabled (HCR_EL2.API). arm64e code
  would then fault on every signed return. That rules arm64e out on this hypervisor configuration.
- `pauth=0` / `verdict=no-pauth`: the ID registers hide PAuth (Gunyah ID trapping). The same three conditions apply. A kernel
  that trusts the ID registers would not enable PAC at all.
- `auth_fail_ok=0` (`X`): authentication is not enforced. arm64e would run, but gives no protection.
- `fpac≥1`: a failed AUT faults instead of poisoning. arm64e XNU expects this (`ESR_EC_PAC_FAIL` handling), so it is not
  a blocker. It does mean that every unsigned pointer in a port that is only partly arm64e panics immediately.
- `restored=0`: the original state did not come back. Do not boot with the bit again before checking. The arm64 system
  keeps running only because nothing in the non-e kernel and userland uses PAC with the keys enabled.
- A hang: the last `FUXI PAC: cpu=N probe begin` names the CPU. The serial `step=` lines, or `incomplete step=` after the
  5 s timeout, name the step. A hang inside `keywr` or `auth`, with no skipped trap, means the hypervisor itself stops on
  the access.

## Tests

`tools/test-fuxi-pac.py` checks the following:

- the 0114 snapshots;
- FUXI-only hooks;
- that bit 25 is free in both GPU parsers, and that 22 is the DPU survey bit (23-24 stay reserved for DPU);
- the formatter and every verdict;
- with clang-18 and llvm-objdump-18: a compile of `fuxi_pac.c` for arm64 Mach-O under XNU's `-Weverything -Werror`
  set, against stand-in headers, with the probe table disassembled in op order.

`tools/sync-fuxi-pac.py --check`. Both also run in `tools/ci/build.sh` kernel targets. Not checked on the host: the real XNU
compile of the SweetCPU.cpp/sleh.c hunks (by inspection only) and every device effect.

# arm64e, stage 1: `kernel=metal4e` (arm64e build of the current kernel)

Goal: the same xnu-8796 kernel, patches and drivers as `metal4`, compiled and linked as arm64e. Kernel C/C++ gets
`-arch arm64e` (signed return addresses, function pointers, vtables), and the kernel programs its own keys in `start.s`.
Userland stays the arm64 iOS 16 root; arm64e userland is stage 2. Every device effect below is not yet observed.

## Switch: CI only, opt-in

`gh workflow run fuxi-build.yml -f kernel=metal4e` (workflow choice `metal4e`). `all` still means metal4 + wlan6g, so no
existing run changes. `tools/ci/build.sh` target `kernel-metal4e`:

- `setup` requires `xcrun --sdk macosx --find ld-classic`;
- `kernel` runs `tools/test-fuxi-arm64e.py` with the other host tests and exports `FUXI_ARM64E=1` to the builder (unset for every
  other kernel target);
- after the build, `kernel-arch.txt` records cputype/cpusub, `__TEXT,__thread_starts` and chained fixups, read through
  `tools/mkpayload.py`. The target fails unless cpusub is 2 (arm64e) with `__thread_starts` present. An arm64 target that
  produced arm64e also fails;
- artifact `fuxi-kernel-metal4e`: the kernel pair, `fuxi.adt`, the metal4 `cmdline.txt`, and `cmdline-cpus1.txt` (the same line with
  `cpus=1`, the fallback below).

`tools/ci/build.sh --dry-run kernel-metal4e` prints the kernel arch/env line without a Mac.

## Build changes (patch 0115, all behind `FUXI_ARM64E` / `__arm64e__`)

- `makedefs/MakeInc.def`: with `FUXI_ARM64E=1`, FUXI exports `ARCH_STRING=arm64e`, adds `-DFUXI_ARM64E=1 -march=armv8.3-a+crypto`,
  and links with `-fuse-ld=<ld-classic> -Wl,-threaded_starts_section` instead of `-add_split_seg_info -kernel`. The kernel then
  carries `__TEXT,__thread_starts`, so `arm_init.c:272` sees a static kernelcache. `rebase_threaded_starts`
  (`osfmk/mach/dyld_kernel_fixups.h:598`) slides and signs every pointer, then `OSRuntimeSignStructors` signs the
  constructors. The linker flags come from UniXNU `arm64e-threaded-starts.patch` (BSD-2-Clause-Patent), ported with attribution.
- `pexpert/pexpert/arm64/SWEET.h`: under `FUXI && __arm64e__`, `HAS_APPLE_PAC 1` and `HAS_NATIVE_PAC 1`. 8796 sets
  `HAS_APPLE_PAC` only from `apple_arm64_common.h`, for Apple CPUs. `SCTLR_EL1_DEFAULT` then includes EnIA/EnIB/EnDA/EnDB.
- `osfmk/arm64/pac_asm.h:199` native `PAC_INIT_KEY_STATE`: invoked at `start.s:852`, before the SCTLR write, on every CPU and on warm
  reset. It is described in the next section.
- `osfmk/arm64/start.s:304`: on the EL2 path, HCR_EL2 gets API|APK (bits 40/41), so EL1 key and PAC accesses do not trap. On SM8550 we
  enter at EL1 under Gunyah, so this is not the expected path.
- `osfmk/arm64/start.s` (end): `fuxi_native_pac`, 24 words in `__DATA,__data`, 64-byte aligned.
- `osfmk/arm64/machine_routines_asm.s:96/116`: without PARAVIRT, the bodies of `ml_enable_user_jop_key` and
  `ml_disable_user_jop_key` were empty and fell through into the next function. Under `HAS_NATIVE_PAC` they return
  (`mov x0, #0; ret` and `ret`), because there is one key set and nothing to swap. The fix follows UniXNU native-pac-keys.
- `iokit/Kernel/arm/SweetCPU.cpp:269`: the PSCI CPU_ON entry is `ptrauth_strip(&sweet_secondary_entry)` before
  `ml_static_vtop`. A signed function pointer would give a bogus physical address.

## Key initialisation (`PAC_INIT_KEY_STATE`, native)

The first pass (boot CPU) generates ten 64-bit key halves and stores them in `fuxi_native_pac[0..9]`. They are splitmix64
values seeded from CNTPCT_EL0. The pass then sets READY and MODE=1.

Every pass then does the following:

1. It sets stage 0x30 in `fuxi_early_status`.
2. It writes the ten halves with `msr`, runs `isb`, and reads them back. If the readback differs, it increments BAD and records
   BAD_MPIDR. On the first pass it also sets MODE=2 (hyp: the keys stay as the hypervisor left them).
3. If PACGA exists (ISAR1 GPA/GPI, or ISAR2 GPA3), it computes `pacga` of a fixed value with modifier `0x5eedfacecafe1234`.
   - The first pass stores the result as GA_REF.
   - A later CPU with another result increments GA_BAD, records GA_MPIDR, cleans the lines, sets stage 0x35 and parks in `wfe`.
     Its pointers could not be authenticated on the other cores. The boot then waits on that CPU, and `cmdline-cpus1.txt` is the
     way out.
4. Otherwise it increments CPUS, cleans the lines with `dc civac` (the MMU may be off), and sets stage 0x31.

`fuxi_native_pac` layout, in 64-bit words:

| Word | Field |
| --- | --- |
| 0-9 | Key halves: IA, IB, DA, DB, GA (Lo, Hi) |
| 10 | READY |
| 11 | MODE: 1 native, 2 hyp |
| 12 | CPUS: passes, warm resets included |
| 13 | BAD |
| 14 | BAD_MPIDR |
| 15 | GA_REF |
| 16 | GA_BAD |
| 17 | GA_MPIDR |
| 18-23 | Reserved |

The keys are never logged.

The fallback is not the expected path, because S224 shows that native key writes work on all 7 cores. MODE=2 keeps booting on the
hypervisor's keys. That is consistent only if the hypervisor gives every core the same keys. A trapped `msr` (hypervisor UNDEF) is not
handled: the vectors are not installed yet, so the CPU stops at stage 0x30 with no log.

## PAC self-test under arm64e (bit 25, live mode)

When `__arm64e__` is defined, `fuxi_pac.c` is built with `FP_LIVE`. The kernel runs on the very keys the probe would overwrite, so live
mode never writes keys or SCTLR. It reads them, runs the same PAC/AUT/PACGA round trips on the live keys, and compares signatures
across CPUs (the keys are shared). The new log fields are not yet observed:

```
FUXI PAC: cpu=N ... keys_rw=live ... esr=… mode=live
FUXI PAC: summary ... verdict=… mode=live
FUXI PAC: native mode=native|hyp|unset cpus=N/ncpu bad= bad_mpidr= ga=0|1 ga_bad= ga_mpidr= verdict=ok|missing|mixed|hyp-keys|readback|no-ga|cpus
```

On an arm64e kernel, the `native` line prints the `fuxi_native_pac` status words (never the keys), even with bit 25 clear.
`verdict=ok` means all of the following: mode native, no bad readback, no GA mismatch, and at least ncpu passes.

## tools/mkpayload.py

It parses sections, reports `thread_starts` and counts authenticated chained fixups. It refuses:

- an arm64e kernel without `__thread_starts`;
- any kernel whose chained fixups include auth fixups, because it would leave them unsigned. `--allow-unsigned-auth` overrides
  this, for inspection only.

The ld-classic threaded link should carry no `LC_DYLD_CHAINED_FIXUPS`. That is a guess until the first CI artifact.

## Audit (8796 + our patches, arm64e)

- Kernel entry and keys: `start.s:304`, `start.s:852`, `pac_asm.h:199` (above). `sweet_early_mark(1)` at `arm_init.c:350`
  runs before `arm_setup_pre_sign` rebases (`arm_init.c:272`). By inspection it uses no data pointers (a guess that it stays safe).
- PAC faults: `sleh.c:734` handles `ESR_EC_PAC_FAIL`. The 0114 hook at `sleh.c:626` only skips table addresses while armed.
- User JOP: `vm_shared_region.c:3239` signs the shared region only for `jop_key != 0`, which is a stage-2 concern. The jop-key routines now
  return (above).
- Cosmetic, and only under arm64e userland (stage 2): user LRs are printed unstripped in `osfmk/kern/exception.c:868,1032`,
  `bsd/dev/sweet_sample.c`, and 0111 `osfmk/kern/sweet_prof_sample.c:40,60` (`bt[n]=frame[1]`).
- Drivers and kexts: everything in the kernel must be arm64e. Our drivers are compiled with the kernel. A kext built for arm64 would be
  rejected or would crash on its first indirect call (guess).

## Expected first CI failures (guesses, most likely first)

1. `ld-classic` is missing, or the selected Xcode rejects `-threaded_starts_section`. Xcode 15.4 should ship ld-classic (guess).
   `setup` catches a missing ld-classic; a rejected flag fails at the link. A `-fuse-ld=` path warning under `-Werror` is also possible.
2. `-march=armv8.3-a+crypto` conflicts or warns together with `-arch arm64e` (unused or overridden target feature).
3. `HAS_APPLE_PAC` code that 8796 only ever compiled for Apple SoCs fails under MACHINE_CONFIG=FUXI. Candidates are missing
   `HAS_APPLE_PAC`-only symbols or headers: kpc/CPU counters, the `platform_tests` APIA key tests, `ml_*_jop` helpers, and the
   `pmap` user JOP paths.
4. The assembler rejects something in the native `PAC_INIT_KEY_STATE`: the `\@` labels, the `pacga` operand form, or the
   `fuxi_early_status` store in the stage macro.
5. The `kernel-arch.txt` check fails (cpusub not 2, or no `__thread_starts`) if `ARCH_STRING` did not reach the link.
6. An undefined symbol or another link failure that the host cannot show, because the full XNU tree is not compiled on Linux.

## Risks

- One key set for the kernel and every process, because EL0 and EL1 share the keys. There is no per-process isolation, which is
  acceptable for bring-up only.
- Key entropy is CNTPCT_EL0 through splitmix64, so the keys are predictable. There is no RNG this early.
- Cache coherency with the MMU off: the secondaries read `fuxi_native_pac` after the boot CPU cleaned it (`dc civac` + `dsb sy`).
  This is untested on device.
- A trapped key write hangs at stage 0x30. A PACGA mismatch parks that CPU at stage 0x35 and stalls the boot; boot with
  `cmdline-cpus1.txt` in that case.
- `rebase_threaded_starts` and `OSRuntimeSignStructors` have never run on this kernel. A wrong chain means an early panic or a
  hang before serial.
- The EL2 HCR path is untested, because we enter at EL1.
- Userland stays arm64, so its `jop_key` handling is not exercised until stage 2.
- The assumptions in `mkpayload.py` about the threaded-link layout (no chained fixups) are unverified until the first CI artifact.
- Every device effect is not yet observed.

## Tests

`tools/test-fuxi-arm64e.py` takes an optional `--tree DIR` to run against a replayed tree. It checks:

- the 0115 structure: every hunk gated on FUXI/arm64e, and exactly one removed line;
- the MakeInc.def evaluation for `FUXI_ARM64E` 0 and 1;
- the `fuxi_native_pac` layout against `fuxi_pac_logic.h`;
- the native `PAC_INIT_KEY_STATE` and the start.s additions, assembled with clang-18 for `arm64e-apple-macos`;
- the mkpayload arm64e refusals;
- the CI plan: `metal4e` is not in `all`, the dry-run line, and `cmdline-cpus1.txt`.

`tools/test-fuxi-pac.py` adds an arm64e compile and a host simulation of both modes:

- arm64 writes keys and SCTLR and ends with `verdict=ready`;
- arm64e writes neither. Its native line shows `verdict=ok`, and its summary shows `mode=live`.

Host clang-18 has no ptrauth codegen, so real arm64e C/C++ code generation is exercised only in CI.
