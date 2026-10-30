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
