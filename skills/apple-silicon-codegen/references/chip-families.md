<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Chip families, core types, and why codegen changes

Home of family aliases, the ISA ladder, S/P/E differences that change
kernels, and SME-8 vs SME-2 placement. Cache **sizes** still come from
`sysctl` (`sysctl.md`). Class **latencies** live in `latency-lookup.md`.
PMU **names** live in `apple-silicon-profile/references/named-events.md`.

An **Mx family** is Mx + Pro + Max + Ultra: same core *design*, different
counts, topology, and bandwidth. Dispatch on `hw.perflevel{N}.*` and
`hw.optional.arm.FEAT_*`, never on a SKU name. `apple-m1-ultra` is not a
`-mcpu`. An **Ax family** is Ax / Bionic / Pro. **Apple silicon** here is
M-series from M1 plus A-series from A14.

Column aliases used everywhere in this skill pack:

| Column | Means |
|--------|--------|
| M1 | M1 family + A14 |
| M2 | M2 family + A15 |
| M3 | M3 family + A16 + A17 Pro |
| M4 | M4 family + A18 |
| M5 | M5 family + A19 |

A-series has **no S cores and no PL2**. A18/A19 non-Pro P-cluster Shared
LL is typically half the Pro SKU; query `l2cachesize`.

---

## Compiler targeting

- `-mcpu=apple-m1` / `m2` / `m3` / `m4` / `m5`. Never `-march=armv8.x`
  or `armv9.x` as the primary target. `clang --print-supported-cpus`.
- “Base Arm ISA” labels are **reference only** and overstate Apple’s
  set: M1 ≈ v8.5 **without BTI**, M2/M3 ≈ v8.6, M4 ≈ v9.2, M5 ≈ v9.3
  **without MOPS and without CMOW**. Do not target those versions.
- Binary baseline = **oldest family you still ship**. Specialize hot
  functions with `__attribute__((target("…")))` plus a `sysctl` guard.
  Clang ISA-extension names that match this ladder: `bf16`, `i8mm`.
- `apple-latest` tracks a moving CPU, can enable ISA the deployment chip
  lacks, and can disagree across compiler versions / TUs. Do not ship it.
- `+feature` modifiers exist (`-mcpu=apple-m1+bf16`); the portable
  pattern is baseline `-mcpu=apple-m1` + per-function target + `sysctl`.

---

## ISA ladder (still query `sysctl` before emitting)

Always-on from M1/A14 (Apple-Silicon-only binary may assume; checking is
harmless): ASIMD, CRC32, FP including FP16 convert, LSE, LSE2, DotProd,
AES, SHA1/256/3/512, PMULL, RDM, FCMA, FHM, FlagM/FlagM2, FP16, FRINTTS,
JSCVT, LRCPC/LRCPC2, DIT, DPB/DPB2, SB.

Always-on extras that change *which insn* you emit, not *whether the
family has it*:

- **FlagM / FlagM2** — `CFINV`, `RMIF`, `SETF8`/`SETF16`, `AXFLAG`/`XAFLAG`.
  Prefer `CSEL`/`CSET` (rec 24, `core-microarch.md`) over flag-glue.
- **LRCPC / LRCPC2** — `LDAPR`; `LDAPUR`/`STLUR` (9-bit unscaled offset).
  RCpc acquire, cheaper than a fence. Not a seq_cst stand-in for `LDAR`.
  Rec 7, `integer-isa.md`.
- **DPB / DPB2** — `DC CVAP` / `DC CVADP`. Persistence paths only; never
  in ordinary kernels.
- **FRINTTS** — `FRINT32*` / `FRINT64*`: round FP to an integer-valued
  FP. Do not convert-to-int, round, convert-back.
- **JSCVT** — `FJCVTZS` is JavaScript ToInt32 only. Not a general f→i.
- **RDM** — `SQRDMLAH`/`SQRDMLSH` (fused rounding-doubling MAC). Rec 15,
  `asimd.md`.
- **DIT / SB** — crypto constant-time only. `integer-isa.md`.

| From | Features | Agent action |
|------|----------|--------------|
| M1 / A14 | ladder above | Baseline path. No SME, no I8MM, no BF16, no BTI in the M1-column **base** ISA (BTI arrives M2). |
| M2 / A15 | BF16, BTI, I8MM, ECV | `BFDOT`/`BFMLAL`/`BFCVTN`; `SMMLA` / mixed-sign DOT. Query `FEAT_I8MM` / `FEAT_BF16`. **`FEAT_ECV` is Generic Timer virtualization** — do not dispatch BF16/I8MM on it. |
| M3 / A16 | AFP, RPRES | FPCR FIZ/AH/NEP (M3+; bit meaning in `asimd.md`; do not poke on M1/M2). Prefer `fenv.h` (rec 33). **RPRES**: `FRECPE`/`FRSQRTE` more precise than M1/M2 — still estimates, not IEEE recips. |
| M4 / A18 | SME, SME2, SME_F64F64, SME_I16I64, WFxT, plus SME outer-product subfeats bundled with SME/SME2 | Engine path exists. `WFET`/`WFIT` for **runtime** sub-quantum waits only. Integer `MADD` **cracks** on P (and always on S). |
| M5 / A19 | MTE, MTE2, MTE4, CANONICAL_TAGS, NO_ADDRESS_TAGS, STORE_ONLY; CSSC; HBC; SME2p1; SME_F16F16; SME_B16B16; SVE_B16B16; EBF16 | `MOVAZ`, non-widening F16/B16 ZA arithmetic. `BC.<cond>` exists; Apple predictors **ignore the hint**. CSSC GPR: `ABS`/`CNT`/`CTZ`/`SMAX`/`SMIN`/`UMAX`/`UMIN` (ALU 1). MTE ISA: rec 8, `integer-isa.md`. |

**Exclusions that trap agents:**

- **`FEAT_MOPS` is not on M5** (excluded from that family’s Armv9.3
  baseline). Do not emit `CPY*` / `SET*` as the M5 memcpy path. Call
  `memcpy`.
- **`FEAT_SSBS` is M1–M3 only.** Do not clear `PSTATE.SSBS=0` “for
  speed” on any family.
- **`FEAT_CMOW` is not on M5.**
- **`FEAT_SVE` / `FEAT_SVE2` are unset.** There is no non-streaming SVE.
  Do not check `FEAT_SVE` as a proxy for SME.
- **`FEAT_SME_FA64` is not implemented.** Many ASIMD encodings stay
  illegal in streaming mode.
- SME outer-product subfeats must be **queried**, not assumed from
  `FEAT_SME=1`. Some sysctl keys are named `FEAT_*` for compatibility
  even when Arm’s ARM does not use that name; missing key = `ENOENT` =
  unavailable.
  - With `FEAT_SME` (M4+): `SME_B16F32`, `SME_F16F32`, `SME_F32F32`,
    `SME_I8I32`, `FEAT_SME_F64F64`, `FEAT_SME_I16I64`.
  - With `FEAT_SME2` (M4+): `SME_BI32I32` (`BMOPA`), `SME_I16I32`.
  - M5 only: `FEAT_SME_F16F16`, `FEAT_SME_B16B16`, `FEAT_SVE_B16B16`
    (**SSVE**, not core ASIMD), `FEAT_SME2p1` (`MOVAZ`). Full type
    matrices: `sme-ssve.md`.

SME from M4/A18. M1–M3 stay on core ASIMD (Accelerate may still use
**private** matrix hardware you cannot emit — rec 2, `integer-isa.md`).

---

## S / P / E — same ISA, different machine

Software **cannot pin** a thread to a core type. QoS is a hint
(`amp.md`). One binary; write so a rec that helps S/P does not hurt E.

**No SMT.** One thread owns the core’s L1 and TLBs. Do not size pools as
`2× logicalcpu`.

| Type | Role | Why codegen / unroll / blocking change |
|------|------|----------------------------------------|
| **S** (Super, M5+) | Peak cores | Widest integer/ASIMD/mem EUs. **Only S has PL2** (I+D, 64 B lines). SME-**8**. PL2 cuts LL traffic for *that* core but **adds** latency for requests that still miss to Shared LL; extra snoop + larger private state can **slow aggressively shared MT**. Software prefetch **cannot target PL2**. |
| **P** (Performance) | Peak on M1–M4; on **M5, P is the power-efficient MT core** | M1–M4 P: SME-8 (from M4). **M5 P is not M4 P**: smaller L1I/L1D, fewer map slots, **2** load EUs not 3, SME-**2** (~180 B/cyc Shared-LL→Z, not 256). Query `sysctl`; do not reuse an M4 P blocking constant on M5 P. |
| **E** (Efficiency) | Battery + extra MT | Fewer pipes always. Integer **DIV not pipelined** (32b 7–13, 64b 7–21; next divide waits). 128-bit FDIV 1 per 2 cycles (+1 latency); FSQRT 1 per 4 (+2). Memory EUs narrower; **one** completed write. SME-**2** (~128 B/cyc). HW prefetch less aggressive. Same ISA — a 4-accumulator FMLA kernel still runs; it just saturates sooner. |

**E is not a slow P.** “SSVE is 4× wider so it must be faster” is false:
one engine issue vs several core ASIMD pipes on P/S.

Illustrative map (do not bake into headers; `sysctl` owns sizes):

- M1: 4P+4E typical; no SME, no PL2. E L1I 8-way.
- M2: E L1I becomes **4-way** (set-conflict risk rises).
- M4: SME arrives — P cluster SME-8, E cluster SME-2.
- M5: **S cores appear**; S cluster SME-8 + PL2; P and E SME-2.

Ultra = two dies (UltraFusion). Other-cluster LL is a **fabric** hit
(~50 ns class), not local Shared LL (~15 cycles). Cross-cluster modified
sharing is ~**10×** intra-cluster (`amp.md` rec 63).

---

## Per-family optimization deltas (the “why”)

### M1 / A14

Integer `MADD` is one MAC-class μop on P/E (acc-from-previous-MAC
bypass 1 cycle). No engine. Baseline ASIMD + DotProd + LSE.

### M2 / A15

I8MM / BF16 on. Accumulate BF16 in **FP32**. `BFDOT`/`BFMMLA` are
cracked/microcoded (long latency) — still the right op when you need
that reduction. E L1I 4-way: keep hot I compact (recs 19–20).

### M3 / A16 + A17 Pro

AFP/RPRES (FIZ/AH/NEP in `asimd.md`; tighter `FRECPE`/`FRSQRTE`).
Delivery **I-cache / I-TLB / other** Bottleneck subsets and
**Processing** bottleneck analysis begin here (`named-events.md`). Some
TLB *entry counts* drop with other μarch wins — do not treat fewer
entries as a regression without measuring.

### M4 / A18 — biggest codegen fork

- **SME/SME2.** Write for SME-8 full-tile MOPs (`sme-engine.md`). Same
  binary runs on SME-2 slower. ≤ ~2 heavy SME threads per cluster.
- Integer `MADD` **cracks** into MUL + ALU on P (S always cracked).
  **Still emit `MADD`.** The MAC class is gone on M4+ S/P.
- Discarded Bottleneck accounting is **more accurate**; the same code
  may show a **larger** discarded fraction than on M1–M3.
- `INST_BRANCH_COND` adds `CBZ`/`CBNZ`/`TBZ`/`TBNZ` (was `B.<cond>`
  only). Do not compare that counter across the M3/M4 cut.
- `ARM_*` event aliases appear (`named-events.md`).
- Processor Trace exists (also needs iOS 18.4+ / macOS 15.4+).
- WFxT for sub-quantum waits inside a runtime you own. Application
  kernels **block** (`amp.md` rec 61).

### M5 / A19

- **S cores + PL2.** Block to L1D and Shared LL from `sysctl`; never to
  PL2. Do not mix two miss-streams in one 16 KiB page (worse with PL2
  in the path).
- MTE: platform allocators only; Apple does **not** tag the stack;
  SM/ZA accesses **are** tag-checked (`integer-isa.md` rec 8).
- CSSC: `ABS`/`CNT`/`CTZ`/`SMAX`/`SMIN`/`UMAX`/`UMIN` on GPRs. ALU
  class, latency 1.
- HBC: `BC.<cond>` = `B.<cond>` on Apple predictors.
- SME2p1: `MOVAZ` (extract and zero). F16F16 / B16B16 non-widening ZA.
- **No FEAT_MOPS. No SSBS.**
- M5 P SME-2 at ~180 B/cyc — write the SME-8 stream anyway.
- PMU: data-source `LD_SRC_*`, `CORE_WAITING_SME_ENGINE_CYCLE`,
  `MAP_TAKENBR_SLOT`, `DECODE_UOP` accurate, `INST_MICROCODED`,
  `LDST_OLDEST_MTE_TAG_CHECK_CYCLE`.
- FP FMLA/FMUL S and D: alternate latency 3 (was 4) on M5/A19.
- FDIV S/D: 7/9 (was 8/10). FSQRT S/D: 9/12 (was 10/13).
- GPR→vector converts: conversion is **inline with MV2VEC** (no extra
  CVT μop); still ~4–5 cycles and a load slot.

---

## SME-8 vs SME-2 (engine, M4+)

| | SME-8 | SME-2 |
|--|-------|-------|
| Typical home | M4 P; M5 **S** | E always; **M5 P** |
| PEs | 64 (1 per 8×8 B cell) | 16 (each PE covers 4 cells) |
| Shared-LL → Z | ~**256 B/cycle** | E ~**128**; M5 P ~**180** |
| ISA | identical | identical |

Write **SME-8 saturating** code: full-tile MOPs into `ZAn.T`, whole-ZA
`ZERO {ZA}`, ~four independent ZA chains, back-to-back compute.
Do **not** emit a “slow engine” path that issues fewer MOPs — slower on
both. SME-2 folding is built to keep **VGx2/VGx4** busy.

One engine **per cluster**, private ZA/Z/P per core. Engine is
**nonspeculative**; transit ~20 cycles each way. Details: `sme-engine.md`.

---

## Memory and line sizes (all families)

The cluster last-level cache is **Shared LL**. Do not call it Shared L2
— that name is gone now that some S cores have a **Private L2 (PL2)**
above it. `hw.perflevel{N}.l2cachesize` is Shared LL, not PL2.

Bandwidths in decimal SI (`MB/s`); sizes and footprints in binary
(`KiB`/`MiB`). `sysctl` cache keys are **bytes**.

| Thing | Size |
|-------|------|
| Coherence / false sharing / NT-store line | **128 B** (`hw.cachelinesize`) |
| L1I / L1D / PL2 **fill** | **64 B** |
| VM page | **16 KiB** (`hw.pagesize`) |
| `ADRP` architectural page | **4 KiB** |
| Core↔engine hazard granule | **aligned 4 KiB** |

Little-endian. AArch64 only. Weak memory (not TSO).

---

## Branch vocabulary (write control flow with these)

Recs 23–25 (`core-microarch.md`) own the optimizations. Terms:

- **Direct** — PC-relative in the insn. **Indirect** — target from a
  register. **All indirect branches are unconditional.** Do not invent
  a conditional `BR`.
- **Call** — `BL`/`BLR` writes `PC+4` to **X30**. **Return** — `RET`
  from X30. Pair them (rec 25).
- **Fall-through / skip** — conditional with a false condition (`PC+4`).
  **Taken** — unconditional, or conditional with a true condition.
- **Predicted** — frontend guesses condition and/or indirect target and
  fetches ahead. Direct uncond `B`/`BL` generally **not** predicted
  (known). Taken-branch danger ~**1 in 8** (rec 23); common body on
  the fall-through.
- **Correct path** vs **wrong path** (flushed). A **retired** branch
  committed; it may still have been mispredicted (wrong-path fetch
  happened).
- **Biased** — mostly one direction, or mostly one indirect target.
- **Highly predictable** — predictor usually right (distinct from
  biased: a 50/50 the predictor has learned is still predictable).

---

## Width / compute ladder (pick per algorithm, then per chip)

1. Scalar C, ≥4 independent accumulators, `restrict`.
2. ASIMD 128-bit, ≤~25 live of 32 V-regs, FMLA, loads into V not X.
   **Default** for everything that is not a large ZA tile.
3. If `FEAT_SME=1` **and** the algorithm is a large-tile matrix / outer
   product / batched dot: ZA + Processing Grid, SSVE as staging, 4 KiB
   isolation, write for SME-8.
4. Tiles that still fit in **L1D** can be faster as core ASIMD (engine
   LSU talks to Shared LL). SSVE-only is rarely a win vs 4-wide P/S
   ASIMD.

---

## After identifying the chip

1. `sysctl.md` — query FEAT_* and `hw.perflevel{N}.*`.
2. `latency-lookup.md` — unroll ≈ latency / throughput for this core type.
3. ISA file for the kernel: `integer-isa.md` / `asimd.md` / `sme-ssve.md`.
4. Engine: `sme-engine.md`. AMP: `amp.md`.
