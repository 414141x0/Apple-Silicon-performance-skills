---
name: apple-silicon-codegen
description: >-
  Write new Apple Silicon AArch64 / NEON / ASIMD / SME / SSVE code correctly.
  Trigger on: writing NEON/ASIMD/SME/SSVE, arm_neon.h, simd.h, FMOPA, SMSTART,
  ZA, -mcpu=apple-m1/m2/m3/m4/m5, sysctl hw.optional.arm, GPR to vector, CSEL,
  FMLA, cache line 128, 16KiB pages, QoS, GCD, Accelerate, vDSP, memcpy, MTE,
  weak memory, DMB, LDAR, STLR, LSE, LDADD, PAC, arm64e, ptrauth, DIT,
  timingsafe, X18, red zone, va_list, new SIMD kernel, assembly AArch64,
  Apple Silicon optimization while writing code. If the user is profiling, defer
  to apple-silicon-profile. If reviewing existing hot code for a named
  anti-pattern, defer to performance-patterns. This skill owns writing NEW code
  correctly.
---

<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Apple Silicon codegen

Write-time companion to `performance-patterns` (detect-and-fix) and
`apple-silicon-profile` (measure). Use this skill **while generating new
kernels, assembly, and data layouts** — not while annotating a profile, and
not while hunting a named anti-pattern in existing hot code.

| Situation | Skill |
|-----------|--------|
| Writing new AArch64 / NEON / SME / AMP code | **This skill** |
| Measuring, Instruments, CPU Bottlenecks, PMU events | `apple-silicon-profile` |
| Reviewing existing hot code for a named anti-pattern | `performance-patterns` |

AArch64 only. Apple silicon cores do not implement AArch32. Do not emit Thumb,
A32, or 32-bit Arm ABIs.

After the kernel compiles, verify with `apple-silicon-profile` (CPU Bottlenecks
+ CPU Profiler). If a named anti-pattern remains, hand off to
`performance-patterns`.

---

## Optimization process order

Pursue opportunities in this order. Skipping a step to jump to assembly is a
bug in the agent, not an optimization.

1. **Goals (rec 1, High/High).** Name a measurable target (throughput, latency,
   or outlier bound) and a measurement method (`mach_absolute_time()`,
   Instruments). Do not micro-optimize without a goal.
2. **Compiler.** Release config. `-O3` on files that contain the hot functions.
   Target a chip family with `-mcpu=apple-m1` (or `m2`/`m3`/`m4`/`m5`), never
   an Arm ISA version. `clang --print-supported-cpus` lists what this toolchain
   accepts. `-Ofast` / `-ffast-math` reassociate FP and enable FMLA; they change
   numerics (no Inf/NaN contract). Use them only when the caller accepts that.
   Consider PGO and LTO after the kernel is correct.
   Family aliases and `apple-latest` danger: `references/chip-families.md`.
3. **Platform libraries (rec 2, High/High).** Accelerate, vDSP, BLAS, LAPACK,
   vImage, vForce, BNNS, `simd`, Compression, Apple Archive, GCD, `memcpy`.
   Call a tuned library before writing NEON. → `references/integer-isa.md`
4. **Profile existing code** (if this is a rewrite, not a green-field kernel).
   → `apple-silicon-profile`
5. **Data structures and decoration.** Compact layouts, `const`, `restrict`,
   `#pragma clang loop unroll` / `vectorize`. 128 B coherence line, 16 KiB
   pages. → `references/core-microarch.md`
6. **Intrinsics for the hot kernel; compiler owns ABI, loops, RA (rec 12).**
   `<arm_neon.h>` / `<simd.h>`. Assembly only when the compiler cannot emit the
   needed instruction. SME/SSVE for matrix/outer-product tiles.
   → `references/asimd.md`, `references/sme-ssve.md`
7. **Cache blocking and access patterns.** Block to L1D and Shared LL sizes
   from `sysctl` (never M Cache, never PL2). → `references/core-microarch.md`
8. **Microarchitecture polish.** CSEL, taken-branch density, GPR↔vector,
   STLF, prefetch. Then SME engine isolation / fusion if ZA is in play.
   → `references/core-microarch.md`, `references/sme-engine.md`
9. **Class latency for unroll.** Recurring inner-loop op → its class in
   `references/latency-lookup.md`. Unroll ≈ latency / throughput on **this**
   S/P/E. Floor, then measure.

---

## Load only the chapter you need

Do not ingest every reference file. Pick from the routing table, then follow
cross-links for one-home facts.

| Writing this | Load |
|--------------|------|
| Any new kernel (always) | This `SKILL.md` (process order) |
| Which family / S vs P vs E / ISA ladder / SME-8 vs SME-2 | `references/chip-families.md` |
| Platform-lib vs custom, `-mcpu`, ABI (X18/X29, Apple type sizes, variadics), DIT, PAC/`ptrauth.h`/`arm64e`, addressing, constants, weak memory, MTE ISA, JIT | `references/integer-isa.md` |
| NEON / ASIMD / `arm_neon.h` / `simd.h` / FMLA / LD2–4 / horizontal reduce | `references/asimd.md` |
| `SMSTART`, ZA, `FMOPA`, SSVE Z/P, SVL, `arm_sme.h` | `references/sme-ssve.md` |
| I-cache / branches / CSEL / GPR↔vector / cache-block / 128 B false sharing / memcpy / prefetch / STLF | `references/core-microarch.md` |
| Core↔engine bounce, 4 KiB isolation, SME-8, packing fusion, ZA extract, LUTI/ZIP pairs | `references/sme-engine.md` |
| Threads, QoS, GCD, XPC/launchd `ProcessType`, spin-wait, LSE atomics, cross-cluster sharing | `references/amp.md` |
| `sysctlbyname`, `FEAT_*`, cache/page/line sizes, perflevels | `references/sysctl.md` |
| Instruction class latency / bandwidth / unroll ≈ latency/throughput | `references/latency-lookup.md` |
| Measured wall-clock on **one** Apple M4 Pro (not a family table) | `references/m4-pro-measured.md` |

### Recommendation homes (recs 1–66)

Each rec lives in **exactly one** file. Cross-link; do not restate the full
rule in a second file.

| Recs | Home |
|------|------|
| 1 (goals + process) | this file |
| 2–9 | `references/integer-isa.md` |
| 10–16 | `references/asimd.md` |
| 17–18 | `references/sme-ssve.md` |
| 19–49 | `references/core-microarch.md` |
| 50–58 | `references/sme-engine.md` |
| 59–65 | `references/amp.md` |
| 66 | `references/sysctl.md` |

`latency-lookup.md` is the class-latency book. `chip-families.md` is family
aliases, ISA ladder, S/P/E why, SME-8 vs SME-2. PMU names live in
`apple-silicon-profile/references/named-events.md`.
`references/m4-pro-measured.md` is one-SKU wall-clock (Apple M4 Pro,
this machine). Do not treat it as a family table.

---

## Non-negotiable platform facts

- Target `-mcpu=apple-mN`, not `-march=armv8.x`. `apple-latest` is dangerous
  (`chip-families.md`).
- Query ISA and cache sizes at runtime (`sysctl.md`). ISA ladder:
  `chip-families.md`. **No `FEAT_MOPS` on M5.** **SSBS is M1–M3 only** — do
  not clear `PSTATE.SSBS=0` for speed. No `FEAT_SVE`/`SVE2`. No
  `FEAT_SME_FA64`.
- Coherence / false-sharing line is **128 B** (`hw.cachelinesize`). L1I / L1D /
  PL2 **fill** line is 64 B. Pages are **16 KiB** (`hw.pagesize`); `ADRP` still
  uses a 4 KiB architectural page.
- X18 is reserved. X29 must address a valid frame record. Apple
  `long double` is `double`. Variadics are stack-only. PAC/`arm64e`
  and DIT live in `references/integer-isa.md`.
- Weak memory: `DMB`/`DSB`, `LDAR`/`STLR`, LSE. Not TSO.
- No 256-bit ASIMD. Width ladder: scalar → 128-bit V → (matrix tiles) SME ZA.
  SSVE-only is rarely a win versus 4-wide ASIMD on the core.
- No SMT. Cannot pin a thread to S vs P vs E; QoS is a hint.
- Do not bounce dependent data core ↔ SME engine (~20 cycles transit each
  way; engine trails by tens to thousands of cycles). Rec 58 VGx `MOVA`
  uses **W8–W11**; tile-slice movement uses **W12–W15**.
- Bandwidths in decimal SI (`MB/s`); sizes in binary (`KiB`/`MiB`).
  `sysctl` cache keys are bytes.
- An Mx family is Mx + Pro + Max + Ultra. Dispatch on `hw.perflevel{N}.*`,
  not on a SKU name. M5 P is **not** M4 P (`chip-families.md`).

## Width / compute ladder

1. Scalar C with ≥4 independent accumulators and `restrict`.
2. ASIMD 128-bit, ≤~25 live of 32 V-regs, FMLA, loads into V not X.
3. If `FEAT_SME=1` **and** the algorithm is a large-tile matrix / outer
   product / batched dot: SME ZA + Processing Grid, SSVE as staging.
   Isolate 4 KiB. Target SME-8. Tiles that still fit in **L1D** can be
   faster as core ASIMD.
4. If the algorithm cannot use the grid: stay on core ASIMD.

Unroll from `latency-lookup.md`. Query `hw.optional.arm.FEAT_*` before
emitting SME / I8MM / BF16 / MTE.

One-SKU wall-clock that backs this ladder on an Apple M4 Pro:
`references/m4-pro-measured.md` (not a family table). Time both QoS
classes before shipping a width / ILP choice.
