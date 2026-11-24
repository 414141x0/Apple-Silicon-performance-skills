<!-- Structure inspired by Intel Performance Skills (MIT, 2026).
     Content is original agent instructions paraphrasing Apple Silicon CPU
     Optimization Guide 5.0. Do not copy Guide tables. MIT license. -->

# SME engine microarchitecture (Guide Ch. 6)

Owns recs **50–58**. ISA (SMSTART, SVL, ZA, predicates) is `sme-ssve.md`.
Do not copy Guide 6.8 fusion pair tables or engine bandwidth tables — open
those sections for the exact pairs.

One SME engine per **cluster**, shared by the cores in that cluster, with
a private ZA/Z/P copy per core. The engine is **nonspeculative**. It issues
only after the core has committed the instruction. Out-of-order inside the
engine exists to cover memory and pipeline conflicts, not to cut latency
back to the core.

---

## Rec 50 — Do not bounce dependent data core ↔ engine (High/High)

Transit core → engine is on the order of **~20 cycles**. Engine → core is
another **~20**, **plus** however long the engine sat behind nonspeculative
issue, Shared-LL loads, and grid latency. Dependent core code that waits
on an engine result can stall **tens to thousands of cycles**.

Write-time rule: one direction per phase.

1. Core prepares inputs into an engine-owned buffer (or the engine loads
   them itself from Shared LL).
2. Engine runs the whole tile kernel (loads, MOPs, extract, stores).
3. Core reads the **finished** outputs, once.

Do not:

- `FMOPA` then immediately `FMOV` a scalar from ZA/Z to decide a core
  branch
- core FMLA into a buffer the engine reloads every iteration
- mix per-element core fixups with per-tile engine MOPs on the same values

Returning a final block via memory at the **end** of a large kernel is
fine; the core will wait, so do not hide a second kernel behind that wait
unless it is independent.

---

## Rec 51 — SSVE supports; the grid is the throughput path (High/High)

SSVE `Z` is 64 B, but the SSVE unit issues **one** vector μop/cycle (and
many mul/FP-add μops take multiple issue slots). Multi-vector SSVE forms
often still issue **per Z**. The Processing Grid is the wide machine.

Use SSVE to load, predicate, rearrange, and extract. Put the flops in
`FMOPA` / other ZA MOPs. SSVE-only vs 4-wide core ASIMD: `sme-ssve.md`.
Tiles that still fit in **L1D** can be faster as core ASIMD: the engine
LSU talks to Shared LL, not L1D (Guide 6.9.1).

SSFP (scalar FP in streaming mode) is **narrower** than core FP. Scalar
FP registers are **not** preserved across SM enter/exit (`sme-ssve.md`).
If the kernel needs a serious scalar-FP epilogue, `SMSTOP SM` (keep ZA)
and run it on the core, then re-enter SM — not a long SSFP chain.

---

## Rec 52 — Optimize for SME-8; SME-2 runs the same code slower (High/High)

Two engine implementations exist: **SME-8** (one PE per 8×8 B cell, 64 PEs)
and **SME-2** (each PE covers four cells, 16 PEs). The **ISA is identical**.
Code that saturates SME-8 also runs on SME-2; it is just slower (a PE walks
more cells).

Write for SME-8 throughput: enough independent ZA tiles/vectors to keep a
64-PE grid busy, whole-ZA zero/rename, back-to-back MOPs. Do not special-
case a “slow engine” path that issues fewer MOPs — that path is slower on
both.

Do not oversubscribe the engine: a **small** number of cores (even one or
two) can fill one cluster’s engine. More than about **two heavy SME
threads per cluster** often loses to contention. The OS load-balances;
still, do not spawn one SME worker per logical CPU as if the engine were
private.

---

## Rec 53 — Cover ~4-cycle grid latency with whole ZA (High/High)

Grid compute is typically **~4 cycles** of latency and **one** grid μop
per cycle. Hide it by keeping many independent ZA tiles / ZA vectors in
flight — use the **entire 4 KiB**, not one tile.

`ZERO { ZA }` renames the **whole** array and lets the tail of kernel N
overlap the head of kernel N+1. Prefer whole-ZA zero over piecemeal row
zeros when starting a new accumulation (row zeros still obey WAR on that
row). M5 / A19 `FEAT_SME2p1` adds `MOVAZ` (extract and zero). Query
before emitting it (`sysctl.md`). There is **no** ZA-tile/vector copy
insn — restage through Z, or rename with whole-ZA zero.

Unroll the MOP loop across tiles until ZA is the limit, then check that
SSVE loads can feed that unroll (rec 51, rec 57). Spend **Z as a
load→consumer buffer**: keep loading later iterations into other Zs so
the first MOP arrives when sources are likely ready. Do not put the
consumer immediately after its loads — Shared-LL hit is already tens of
cycles under traffic.

---

## Rec 54 — ZA Extraction Unit in parallel; fusion still wins (High/High)

Extract (`MOVA` from ZA into Z, and related) has its **own** pipeline. Issue
extracts beside grid MOPs on **other** rows/tiles. That is extra
parallelism.

When an extract would immediately convert / narrow / ZIP, **prefer the
Guide 6.8 fusion pair** even if that fused op occupies the grid rather than
the extract unit. Fusion beats “extract on the side + extra convert”.

128 B / 256 B `MOVA` cuts **insn/μop count**, not extract latency versus
64 B. Predicated single-vector `MOVA` typically occupies the **grid** and
blocks extract issue — prefer unpredicated multi-vector extract. Direct
ZA loads (`LD1Q` of a ZA slice, `LDR ZA`) are two μops: a Z-load into a
temp, then a **grid insertion that blocks other grid issue that cycle**.
Default path: multi-vector `MOVA` to Z, then store from Z, unless a 6.8
pair applies.

---

## Rec 55 — Alignment / padding for the engine LSU; avoid unaligned stores (Medium/High)

Engine loads/stores go to the **Shared LL**, not L1D. The LSU likes
contiguous accesses and will merge toward 64 B half-lines. Unaligned
**stores** still force read-modify-write. Align engine stores to the
element group (and 64 B when you can); pad tiles so Shared-LL banks are
not hammered on one index.

Experiment with padding if LL bandwidth looks low despite a streaming
access. Do not “fix” that with unaligned ST1 of overlapping tails.

For **column walks**, pad row length so it is **not a multiple of 256 B**.
Padding to `(k × 256 B) + 128 B` can also kill unaligned stores; it
grows the footprint — experiment. Prefer sequential layout. Shared-LL
bank hash differs by family; do not hardcode a pad from another chip.

---

## Engine cache blocking and prefetch (Guide 6.9)

Engine algorithms block to **Shared LL**, not L1D. Those tiles are
**larger** than the ASIMD version, still shared by every core in the
cluster. Query `hw.perflevel{N}.l2cachesize` (rec 36,
`core-microarch.md`). Do not copy a worked GEMM size out of the Guide.

SSVE/SME loads and stores train the same hardware prefetchers in a mode
that fills **Shared LL only** (not L1D, not PL2). Software `PRFM` for
engine streams uses the **L2 destination** encoding — that is Shared LL.
You **cannot target PL2**. Prefetch helps most when stride is **> 8 KiB**;
rearranging to a shorter consistent stride beats a spray of `PRFM`.
Engine `PRFM` often issues on the core **before** the matching engine
memory op becomes nonspeculative, so it naturally runs early.

NT: SSVE `LDNT1`/`STNT1` for large low-reuse streams (same idea as rec 40
on the core). Half-lines written by SME that **miss** Shared LL are **not
allocated** — they go toward memory. Keep hot tables temporal.

---

## Rec 56 — Isolate core vs engine data on separate aligned **4 KiB** regions (High/Medium)

The core tracks core↔engine memory hazards at **aligned 4 KiB**
granularity. Any core load/store in the same 4 KiB page as an engine
access — including the **stack** — can serialize the two sides.

Rules:

- Engine buffers: heap, `posix_memalign(..., 4096)`, never automatic
  locals.
- Core scratch / flags / stack: a different 4 KiB region.
- Do not place `buf_core` and `buf_engine` adjacent inside one `malloc`
  without ensuring they do not share a 4 KiB page.

### Original example: two 4 KiB-aligned buffers, no dependent ping-pong

```c
#include <stdlib.h>
#include <stdint.h>

enum { kPage = 4096 };

typedef struct {
    float *buf_core;     // core produces here
    float *buf_engine;   // engine consumes here — different 4 KiB region
    size_t bytes;
} SplitBuf;

int splitbuf_init(SplitBuf *s, size_t bytes)
{
    void *c = NULL, *e = NULL;
    if (posix_memalign(&c, kPage, bytes) != 0)
        return -1;
    if (posix_memalign(&e, kPage, bytes) != 0) {
        free(c);
        return -1;
    }
    s->buf_core = c;
    s->buf_engine = e;
    s->bytes = bytes;
    return 0;
}

// Phase 1 — core only, writes buf_core.
// Phase 2 — memcpy or engine loads from buf_engine after an explicit
//           handoff of *independent* data. Do not have the engine wait
//           on a scalar the core just stored in the same page.
void splitbuf_handoff_independent(SplitBuf *s)
{
    // Rec 41: libc memcpy is allowed; it is not a dependent scalar bounce.
    memcpy(s->buf_engine, s->buf_core, s->bytes);
}

void splitbuf_free(SplitBuf *s)
{
    free(s->buf_core);
    free(s->buf_engine);
    s->buf_core = s->buf_engine = NULL;
}
```

The engine then consumes `buf_engine` only. No per-element flag in
`buf_core` that the engine polls.

---

## Rec 57 — Pack engine compute insns back-to-back (delivery fusion) (Medium/High)

Every engine instruction ships through the core **store** pipe (~one store
slot each). Core stores mixed into that stream punch bubbles (L1D hit: a
few cycles; miss: long). **Block** core stores and engine ops; do not
interleave them at instruction granularity.

**Packing fusion:** two adjacent eligible SSFP/SSVE/SME **compute**
instructions can ship as one. Ineligible: loads/stores, control, anything
that carries GPR/NZCV/predicate results back to the core (except `Ws` as a
ZA row index).

Write-time: emit MOP/compute runs back-to-back; then a block of engine
loads/stores; then compute again. Predicate updates from the core also
consume a ship slot — batch them.

---

## Rec 58 — Operation fusion sequences (LUTI, extract+convert) (High/High)

The engine can fuse **pairs** of SSVE+SME ops (distinct from packing
fusion). The important idea:

- `LUTI2`/`LUTI4` into Z, immediately consumed by a ZA compute whose `Zn`
  matches the LUTI destination set.
- `MOVA` extract of 2 or 4 ZA vectors, immediately followed by downcast /
  shift / saturate / ZIP / UZP that **overwrites** those Z regs. The
  extract μop can drop. Lengthening MOPs/DOT/MLA often **interleave**
  results — ZIP after extract is the common pair.
- Aggregated extraction (two small MOVAs combining) is the fallback;
  prefer the more specific pairs when they apply.

**Open Guide 6.8 / Table 6.1 for the exact mnemonic pairs.** Do not guess
a pair that is not in the table. Do not insert **any** other
SSFP/SSVE/SME instruction between the two fused ops. After extract+ZIP,
overwrite every Z the MOVA wrote.

```asm
    // Idea only — confirm the pair in Guide 6.8 before shipping:
    mova    {z0.d, z1.d, z2.d, z3.d}, za.d[w8, #0, vgx4]
    zip     {z0.s, z1.s, z2.s, z3.s}, {z0.s, z1.s, z2.s, z3.s}
```

---

## Cluster frequency while SM/ZA is on

An entire cluster shares a clock. **Any** core in SM or ZA mode can drop
that cluster to a slightly lower frequency, powers up the engine, and
fattens context switches (Z/P/ZA state). Do not colocate a
latency-sensitive core-only thread on a cluster that is sitting in
streaming mode. Enter SM/ZA only for the kernel that needs it; exit when
done. Do not fine-grain the toggle (mode switch also zeros V and Z).

---

## After the engine kernel

ISA wrapper / `SMSTART`: `sme-ssve.md`. Verify with
`apple-silicon-profile` (CPU Bottlenecks + the SME-oriented flow if
present). Named anti-pattern (bounce, 4 KiB hazard):
`performance-patterns`.
