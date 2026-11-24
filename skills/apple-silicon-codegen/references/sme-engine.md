<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# SME engine microarchitecture

Owns recs **50–58**. ISA (`SMSTART`, SVL, ZA tiles, P/PN) is `sme-ssve.md`.
Family placement of SME-8 vs SME-2 is `chip-families.md`. Class latencies
and Shared-LL→Z bandwidths are `latency-lookup.md`.

One SME engine per **cluster**, shared SMT-like by the cores in that
cluster, with a private ZA/Z/P copy per core. OS context-switch of a
core also switches the engine copy. The engine is **nonspeculative**.
It issues only after the core has committed the instruction.
Out-of-order inside the engine covers Shared-LL latency and pipeline
conflicts; it does **not** cut latency back to the core. Engine work
trails commingled core insns by dozens to **thousands** of cycles.

ZA = 4 KiB = 64 rows × 64 B, cells **8 rows × 8 B** co-located with
their PEs. Z = 32 × 512-bit. P = 16 × 64-bit, duplicated (core
generates/manipulates; engine governs). Grid: typically **one** grid
μop/cycle, ~**4-cycle** accumulate (some 8), ZA-vector readout ~**15**
cycles. Different rows may reorder; **all ops on a given row stay in
program order** (incl. WAR). Extract unit 64 B/cycle, parallel with the
grid. Rename of ZA is **whole-array only**.

Core Integer EU (predicate manip, GPR contents, packaging toward the
engine) is **speculative OOO** like other integer insns. Core LSU does
translation, permissions, MTE, and 4 KiB core↔engine ordering; it does
**not** touch caches for engine mem ops — it sends **physical
addresses**. Completions return for ordering. Data / predicate / NZCV
return on a **separate** channel.

≤ ~**two heavy SME threads per cluster**. One or two cores can fill the
engine; oversubscribe and throughput can **drop**. Do not spawn one SME
worker per logical CPU. Extra family-specific heuristics exist and are
often counterproductive to chase — stay on recs 50–58.

---

## Rec 50 — Do not bounce dependent data core ↔ engine (High/High)

Transit core → engine is **~20 cycles**. Engine → core is another **~20**,
**plus** nonspeculative issue, Shared-LL loads, and grid latency. Dependent
core code that waits on an engine result stalls **tens to thousands of
cycles**. Predicate / NZCV / GPR results from engine compares pay the same
tax: ~20c back, core manip, ~20c out; subsequent engine insns wait because
predicate updates **ship in order**.

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
- engine-element-compare predicates that must return to the core to
  govern the next engine insn

Returning a final block via memory at the **end** of a large kernel is
fine; the core will wait. Group those return insns and emit **no** further
engine insns after them. Do not hide a second kernel behind that wait
unless it is independent.

Wv ZA-row indices and edge predicates (`WHILELT`, `PTRUE`) are generated
**entirely in the core from core-held info** so the core can run **ahead**.
Keep it that way. Core Integer EU details: below.

---

## Rec 51 — SSVE supports; the grid is the throughput path (High/High)

SSVE `Z` is 64 B, but the SSVE unit issues **one** vector μop/cycle (and
many mul/FP-add μops take multiple issue slots even for one 64 B
vector). It has renaming and a ready-μop scheduler. Multi-vector SSVE
forms often still issue **per Z**. Net bandwidth ≈ core ASIMD because
of **one** engine issue port vs **multiple** core ASIMD ports (four on
M4 P). The Processing Grid is the wide machine. SSFP latency is similar
to core FP; bandwidth is **much** smaller.

Use SSVE to load, predicate, rearrange, and extract. Put the flops in
`FMOPA` / other ZA MOPs. SSVE-only vs 4-wide core ASIMD: `sme-ssve.md`.
Tiles that still fit in **L1D** can be faster as core ASIMD: the engine
LSU talks to Shared LL, not L1D.

SSFP (scalar FP in streaming mode) is **narrower** than core FP. Scalar
FP registers are **not** preserved across SM enter/exit (`sme-ssve.md`).
If the kernel needs a serious scalar-FP epilogue, `SMSTOP SM` (keep ZA)
and run it on the core, then re-enter SM — not a long SSFP chain. Do not
linger in ZA-only (power + CS tax).

SM=1 **disables core ASIMD/FP** and **zeros V, Z, and P** on enter/exit.
No fine-grain toggling.

---

## Rec 52 — Optimize for SME-8; SME-2 runs the same code slower (High/High)

Two engine implementations exist: **SME-8** (one PE per 8×8 B cell, 64 PEs)
and **SME-2** (each PE covers four cells, 16 PEs). The **ISA is identical**.
Code that saturates SME-8 also runs on SME-2; it is just slower (a PE walks
more cells). Placement: `chip-families.md`. Bandwidth: `latency-lookup.md`.

Write for SME-8 throughput: enough independent ZA tiles/vectors to keep a
64-PE grid busy, whole-ZA zero/rename, back-to-back MOPs, VGx2/VGx4 when
the algorithm is 1-D (SME-2 folding is built for those). Do not special-
case a “slow engine” path that issues fewer MOPs — that path is slower on
both. On fewer-row vector forms SME-2 can match or sit below SME-8;
still emit the SME-8-saturating stream. Do not hardcode the fractions.

Tile MOP dest is `ZAn.T` (`ZA0.S`–`ZA3.S`, `ZA0.D`–`ZA7.D`), **not**
`ZA.T[Wv]`. Indices are in **8×8 B cells**, independent of element size.
VGx2/VGx4 spread concatenated rows across independent PEs.

Scheduler multi-issue (common on SME-2 folding): μop occupies the **grid
issue port** for several cycles; nothing else issues to the grid. PE
multi-issue: one scheduler issue, extra cycles **inside a PE row**;
**other rows** can take a new μop that cycle. Example: 1 scheduler
issue + 2 internal PE issues → that row busy, other rows free; PEs are
pipelined so the same row can take a new μop after those extra cycles
(latency may be **~8**). Do not hand-schedule; **spread ZA vectors**
and let OOO fill slots.

---

## Rec 53 — Cover ~4-cycle grid latency with whole ZA (High/High)

Grid compute is typically **~4 cycles** of latency and **one** grid μop
per cycle. Hide it by keeping many independent ZA tiles / ZA vectors in
flight — use the **entire 4 KiB**, not one tile. Need ~**four independent
chains** for 1-μop insns.

`ZERO { ZA }` renames the **whole** array and lets the tail of kernel N
overlap the head of kernel N+1. Prefer whole-ZA zero over piecemeal row
zeros when starting a new accumulation. Unroll the MOP loop across
tiles until ZA is the limit, then check that SSVE loads can feed that
unroll (rec 51, rec 57).

### Long memory-μop latencies

Engine schedulers buffer ready μops. Even with Z rename, a Shared-LL
**hit is ≥20 cycles** under traffic → scheduler capacity pressure.
Dependent **load → grid → extract → store**: the grid waits ~20 for the
load; extract waits load+compute; store waits all. Grid class latency
is through the **accumulator**; source-operand latencies are
considerably longer. Loop that sequence and **any** scheduler clogs,
back-pressures accept, and later loads cannot issue.

Spend **Z as a load→consumer buffer**. Do not put the consumer
immediately after its loads — keep loading later iterations into other
Zs so the first MOP arrives when sources are likely ready. Same for
extract/store: after several compute (and extract) iterations.
Load-heavy kernels clog load/compute schedulers more than
extract/store. Prioritize Z as the load→consumer buffer.

### Preferred engine copy / zero

- **Z copy:** `ORR Zd.D, Zn.D, Zn.D` (usually `MOV`). `MOVPRFX` when
  avoiding a destructive dest.
- **No ZA-tile/vector copy insn** — restage through Z, or rename with
  whole-ZA zero.
- **Z zero:** `DUP Zd.D, #0` (usually `MOV #0` / `MOV Zd.D, #0`).
- **ZT0 zero:** `ZERO { ZT0 }`.
- **ZA zero:** `ZERO { ZA }` (whole-array **rename**); `ZERO { tiles }`
  (list up to the full array); `ZERO ZA.D[...]` (1, 2, or 4 ZA-vector
  slices). Row / partial zeros still obey WAR on that row.
- **M5/A19 `MOVAZ`** (`FEAT_SME2p1`): extract and zero 1/2/4 slices of
  ZA vectors or tiles. Query before emitting (`sysctl.md`).

---

## Rec 54 — ZA Extraction Unit in parallel; fusion still wins (High/High)

Extract (`MOVA` from ZA into Z, and related) has its **own** pipeline.
Issue extracts beside grid MOPs on **other** rows/tiles. That is extra
parallelism.

When an extract would immediately convert / narrow / ZIP, **prefer a rec 58
fusion pair** even if that fused op occupies the grid rather than the
extract unit. Fusion beats “extract on the side + extra convert”.

Eligible for extract unit: unpredicated ZA→Z moves, **no** convert/compute,
**do not** modify ZA.

128 B / 256 B `MOVA` cuts **insn/μop count**, not extract latency versus
64 B. Still emit the wide form (delivery + scheduler pressure). Predicated
single-vector `MOVA` typically occupies the **grid** and **also blocks
extract issue** — prefer unpredicated multi-vector extract.

Direct ZA loads (`LD1Q` of a ZA slice, `LDR ZA`) are two μops: a Z-load
into a temp, then a **grid insertion that blocks other grid issue that
cycle**. On SME-8, put insertions on PE rows that are **not** computing
(PE multi-issue). Default path: multi-vector `MOVA` to Z, then store
from Z, unless a rec 58 pair applies.

Direct ZA store: `ST1D` needs a **grid** μop; `STR ZA` uses **extract**;
then a store μop from temp. Predicate is applied on the temp→memory move,
so these still use extract despite a governing predicate. Predicate LD/ST
stay on the **core**. Extract of a row must issue **before** a later write
to that row; extracts of different rows may reorder.

---

## Rec 55 — Alignment / padding for the engine LSU; avoid unaligned stores (Medium/High)

Engine loads/stores go to the **Shared LL**, not L1D. Load latency is
longer than the core, but direct Shared LL means **larger** working
sets before blocking. The LSU likes contiguous accesses and will merge
toward 64 B half-lines. Translations / MTE / 4 KiB ordering: rec 56.
Unaligned **stores** still force read-modify-write. Align engine stores
to the element group (and 64 B when you can); pad tiles so Shared-LL
banks are not hammered on one index. Shared-LL bank hash **differs by
family**; do not hardcode a pad from another chip. Pad for **aligned
stores over aligned loads**. Contiguous unaligned **loads** are often
fine (merge). **Avoid unaligned stores.** Prefer sequential layout.
Prefer **256 B loads** and **128 B/256 B stores** over a train of 64 B.

For **column walks**, pad row length so it is **not a multiple of 256 B**.
Padding to `(k × 256 B) + 128 B` can also kill unaligned stores **and**
bank conflicts; it grows the footprint — experiment. Strided access
hits Shared-LL **bank imbalance** with **no L1D buffer**. Avoid strided
stores that leave partial lines; if unavoidable (temps), pad rows.

Crack: 1 μop per accessed **128 B** line — 64 B μop if contained in a
half-line, 128 B μop if both halves. Example: 128 B load at offset **48**
of line A → 128 B μop on A (80 B) + 64 B μop on A+1.lo (48 B). Then
**merge** across insns to the same line. Three sequential 128 B loads
at that offset crack to 6 μops and merge to 4 (the A+1.lo fragments
combine). Merge only if the second μop **arrives before the first
issues**.

- Load merge: up to **3** cracked load μops into 128 B line requests.
  Need not be back-to-back, but must be in the engine when the first is
  ready (**ideally ≤2–3 load insns apart**). Adjacent, no
  gaps/overlaps; per 64 B half, **only one** merging μop may have a
  non-all-TRUE predicate. Result: only partial lines at stream
  start/end. Post-merge 64 B/128 B **load** μop issues **once**.
- Store merge: up to **2** cracked store μops. Priority is **killing
  partial-half RMW**. Same adjacency / no-gap / one-non-TRUE-per-half
  rule as loads. 64 B unaligned stores **always** crack into **two
  partial 64 B** (even if they could form a partial 128 B) so each half
  can merge with a neighbor into a complete 64 B. Streaming stores that
  write every byte, regardless of alignment, leave partials only at
  ends. Post-merge 64 B store issues once; post-merge **128 B store
  issues twice**. Group unaligned stores.
- Ineligible for merge: SSFP LD/ST, broadcast/replicating SSVE loads,
  SSVE LD/ST whose **memory** element size < **register** element size.

Scheduler: up to **two** ready merged μops OOO to the engine LSU. Per
line, program order (younger loads see older stores). Predicated-store
heavy kernels: **merge in registers first**. Engine loads are
nonspeculative (stores never are). Completions return to the core for
ordering.

Half-lines written by SME that **miss** Shared LL, or that hit
**Shared** state, are **not allocated** — they go toward memory
(partials too) and are tracking-limited. Hit Exclusive/Modified: write
LL; partial / unaligned / predicated-off → **RMW**. Store-heavy
miss/RMW **back-pressures** the engine. Keep hot tables temporal.

---

## Engine cache blocking, prefetch, NT

Engine algorithms block to **Shared LL**, not L1D. Those tiles are
**larger** than the ASIMD version, still shared by every core in the
cluster. Query `hw.perflevel{N}.l2cachesize` (rec 36, `core-microarch.md`).
Do not bake a worked GEMM size. Use somewhat **smaller** blocks for
compatibility across cluster sizes. Blocking also contains TLB
footprint.

SSVE/SME loads and stores train the same hardware prefetchers in a mode
that fills **Shared LL only** (not L1D, not PL2). Row-major **row**
walks (or column-major **column** walks) should stream. Cross-row /
cross-column strides are harder. Software `PRFM` can help unusual
strides; insert far enough ahead. Engine `PRFM` often issues on the
core **before** the matching engine memory op becomes nonspeculative,
so it naturally runs early. Use the **L2 destination** encoding — that
is Shared LL. You **cannot target PL2**. Prefetch helps most when
stride is **> 8 KiB**; rearranging to a shorter consistent stride beats
a spray of `PRFM`.

NT: SSVE `LDNT1`/`STNT1` of B/H/W/D for large low-reuse streams (huge
vector/matrix manip, copies — same idea as rec 40 on the core). Choose
**per structure / per function** so **hot tables stay temporal**. SME
miss half-lines are **not allocated** into Shared LL — do not NT those
hot tables.

---

## Core Integer EU while SM on

The core Integer EU **orchestrates** the engine. It adjusts pointers by
element count, manipulates predicates, resolves branches, and holds the
integer/flag data the engine consumes or produces. Those integer ops
**are speculative OOO** like other core integer insns.

Loop control lives in GPRs, so only **correct-path** engine insns retire
and ship. Edge predicates (`WHILELT`, `PTRUE`) and Wv ZA-row indices
are generated **entirely in the core from core-held info** — not from
engine data — so the core can run **ahead**. Engine insns, predicate
updates, and physical addresses are pushed and trail.

Do not feed the next engine insn from an engine-element compare, scalar
integer, or NZCV that must return to the core (rec 50). Group
unavoidable end-of-kernel returns; emit **no** further engine insns
after them.

Predicate manip with merge predication (`Pg/M`) reads the dest.
Cracked/microcoded sequences vary by core type; generally confined to
**one integer pipe**, typically **~3 cycles**.

---

## Rec 56 — Isolate core vs engine data on separate aligned **4 KiB** regions (High/Medium)

### Core LSU while SM on

The core LSU does **all** address work for SSVE/SME mem ops and all
predicate LD/ST. It does **not** touch caches for engine mem ops — it
sends **physical addresses** to the engine. Engine loads are
nonspeculative (stores never are). Completions return to the core for
ordering. Data / predicate / NZCV return on a **separate** channel. The
core also issues HW prefetches into **Shared LL** from engine access
patterns.

**Translations.** All V→P, core or engine, uses the core L1D TLB +
shared L2 TLB. Inactive predicated elements do **not** take TLB misses
and are **not** MTE-checked. **MTE tag checks for engine accesses run
in the core.** Exceptions are signalled in the core **before** the insn
is sent. Manage the engine TLB working set like the core (block; rec
36). ISA MTE rules: `integer-isa.md` rec 8.

**Ordering.** Intra-thread program order holds regardless of core vs
engine. Tracking is **aligned 4 KiB** (quarters of 16 KiB pages),
**not** cache-line. Barriers wait for **all** engine accesses to that
4 KiB before later core accesses (and vice versa). Combined with the
engine trail, the core may wait **dozens–thousands** of cycles for the
engine to finish that page.

Normal coherence still moves lines among L1D, optional PL2, and Shared
LL. Do not ping-pong the same structures. Returning finals via memory
at the end of a large kernel is usually OK; the core **will** wait —
group that communication (rec 50).

Any core load/store in the same 4 KiB as an engine access — including
the **stack** — can serialize the two sides. Do not access the stack
from the engine LSU (ZA/Z/B/H/S/D/Q while SM=1).

Rules:

- Engine buffers: heap, `posix_memalign(..., 4096)`, never automatic
  locals / stack.
- Core scratch / flags / stack: a different 4 KiB region.
- Do not place `buf_core` and `buf_engine` adjacent inside one `malloc`
  without ensuring they do not share a 4 KiB page.

Profile signal: `LD_BLOCKED_BY_SME_LDST` growing (`named-events.md`).

### Original example: two 4 KiB-aligned buffers, no dependent ping-pong

```c
#include <stdlib.h>
#include <stdint.h>
#include <string.h>

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

Every engine instruction **and every predicate update** ships through the
core **store** pipe (~one store-equivalent write each). Max core→engine
insn BW is **less than** max in-engine issue; many engine insns are
multi-μop so achieved issue is lower still. Core stores mixed into that
stream punch bubbles (L1D hit: several-cycle bubble; miss: long). Older
core stores drain before younger engine insns ship; **younger core
stores wait** until prior engine insns are sent. **Block** core stores
and engine ops; do not interleave them at instruction granularity.

Core predicate updates ship as their own in-order insn to the engine P
file (same resources as any other ship).

**Packing fusion** (distinct from rec 58 operation fusion): two adjacent
eligible SSFP/SSVE/SME **compute** instructions ship as one
store-equivalent, cutting delivery pressure.

Eligible: most adjacent SSFP/SSVE/SME **compute**.

Ineligible:

- loads / stores
- control
- anything that carries **GPR / NZCV / predicate** results **to or from**
  the core:
  - **sources:** NZCV or GPRs (except `Ws`/`Wv` as a ZA row index)
  - **results:** new NZCV, GPR contents, or predicate results shipped
    back
- exception: **`Ws`/`Wv` as a ZA row index** is allowed (does not
  disqualify)

Occasional extra microarch vetoes exist and are not predictable from the
stream; still pair compute.

Write-time: emit **MOP/compute runs back-to-back**; then a **block** of
engine loads/stores; then compute again. Interleave **blocks** of
compute with **blocks** of memory to avoid filling OOO tracking. Batch
core predicate updates. Profile: `INST_SME_ENGINE_PACKING_FUSED`.

---

## Rec 58 — Operation fusion sequences (LUTI, extract+convert/ZIP) (High/High)

The engine fuses **pairs** of SSVE+SME ops **inside the engine** (distinct
from packing fusion) to cut latency, RF traffic, sometimes issue.
Extract+downcast/shift/saturate/ZIP is the pair that applies to **most**
algorithms: fusion performs the convert **in the grid**, reducing ZA
readout to a **single** vector. If the MOVA dests are **not** overwritten,
MOVA **also** executes singly → **four extra vectors extracted** — **worse
bandwidth than not fusing**.

Hard rules:

- Do **not** insert **any** other SSFP/SSVE/SME insn between the two fused
  ops.
- **Core-only** insns (`ADD W12, W12, #16`, GPR loop math) **may** sit
  between them and do **not** break fusion.
- Fusion is **dynamic**: if the two insns arrive too far apart in time,
  fusion does not occur. Keep them adjacent in the engine stream.
- After extract+ZIP/convert, **overwrite every Z the MOVA wrote**.
- Prefer rows 1–4 below over aggregated extraction (rows 5–6) when both
  apply.
- LUTI fusion is **consecutive aligned 2- and 4-destination `LUTI2`/`LUTI4`
  only** — **not** the strided versions.
- End a LUTI consumer block with an insn that **completely overwrites**
  those Zs so the LUTI does not also execute singly. A later `LUTI` that
  overwrites the same Zs is a valid terminator (and itself a new first
  insn).

### Emit these pairs

**1. LUTI → ZA vector compute**

- First: `LUTI2` / `LUTI4` from `ZT0`, write **2 or 4 consecutive aligned
  Zs**.
- Second: most ZA **vector** computation (example: `FMLA ZA[W8], {Z1,Z2},
  Z6[n]`) whose dests are consecutive aligned.
- The 2- or 4-vector set must match **exactly** with either/both `Zn`/`Zm`
  of the second insn.
- Effect: LUTI latency → **1 cycle on top of the consumer**; eliminates
  the **1–4 μops** of the LUTI.

```asm
    luti2   {z1, z2}, zt0, z5[0]
    fmla    za[w8], {z1, z2}, z6[0]
    fmla    za[w8], {z1, z2}, z6[1]
    // contiguous block of FMLA that all consume exactly {z1,z2}
    // a FSUB z3, z1, z7 in the middle BREAKS the block
```

**2. LUTI → ZA tile compute**

- First: `LUTI2` / `LUTI4` from `ZT0`, write **1 vector**.
- Second: most ZA **tile** computation (`FMOPA` / `SMOPA` / other tile
  MOPs).
- That 1 vector must match **exactly** with either/both `Zn`/`Zm`.

**3. MOVA extract → downcast / shift / saturate / ZIP / UZP** (the common
win)

- First: `MOVA` of a **ZA tile slice**, horizontal from ZA **byte tile**,
  write **2 or 4 vectors**.
- Second — emit **only** these: `BFCVT`, `BFCVTN`, `FCVT`, `FCVTN`,
  `SQCVT`, `SQCVTN`, `SQCVTU`, `SQCVTUN`, `SQRSHR`, `SQRSHRN`, `SQRSHRU`,
  `SQRSHRUN`, `SQSHRN`, `SQSHRUN`, `UQCVT`, `UQCVTN`, `UQRSHR`,
  `UQRSHRN`, `UZP`, `ZIP`.
- 2- or 4-vector set must match **exactly**.
- FP: converting **S (32b) accumulators → H or BF16**. `FCVTN` (S→H)
  already interleaves ⇒ extract + down-convert + interleave in one fused
  op.
- Integer: truncation / right shift / rounding / saturation as
  accumulators are halved or quartered.
- Lengthening MOPs/DOT/MLA often **interleave** results — ZIP after
  extract is the common pair.

Tile-slice extract uses **W12–W15**:

```asm
    mova    {z0.b, z1.b}, za0h.b[w12, 0:1]
    bfcvtn  z4.h, {z0.s, z1.s}
    // later MOVA that overwrites z0/z1 so the first MOVA does not
    // execute singly. ST1H after the converts does not break fusion.
```

ZA-vector VGx extract uses **W8–W11** (same file as vector compute).
**Do not “fix” W8 to W12.** ZIP dests = MOVA dests ⇒ extract μop
**drops**:

```asm
    mova    {z0.d, z1.d, z2.d, z3.d}, za.d[w8, #0, vgx4]
    zip     {z0.s, z1.s, z2.s, z3.s}, {z0.s, z1.s, z2.s, z3.s}
```

**4. MOVAZ extract → ZIP/UZP only** (`FEAT_SME2p1`, M5/A19)

- First: `MOVAZ` of a ZA tile slice, write 2 or 4 vectors.
- Second: `UZP`, `ZIP` **only**.
- 2- or 4-vector set must match **exactly**, **and destinations must
  completely overwrite sources**.

**5. Aggregated extraction (fallback)**

Two 2-vector `MOVA` of ZA tile slices combine into one 4-vector extract.
Use when a natural 4-vector extract is not aligned. Prefer 1–4 when they
apply.

**6. Aggregated extraction of ZA stores (fallback)**

Two `ST1*` (ZA tile slice) or `STR` (ZA row) combine their single-vector
extractions. Prefer 1–4 when they apply.

### W8 vs W12 (rec 58)

- **ZA-vector VGx `MOVA`/`MOV`/`MOVAZ`** addressing
  `ZA.D[Wv, #imm{, VGx2/VGx4}]` uses **W8–W11**.
- **Tile-slice** `MOVA`/`LD1*`/`ST1*` and ZA `LDR`/`STR` use **W12–W15**.
- That split exists so software can hold **eight** ZA positions (four
  compute + four movement). Changing the VGx example from `W8` to `W12`
  is an **ISA error**. Full addressing: `sme-ssve.md`.

---

## Multithreading and mode (cluster frequency)

The engine is a **shared** resource, fine-grained across cores in the
cluster (SMT-like). **One or two** cores can fill it. Oversubscribe and
throughput can **drop**. Rule: **do not create more than two
heavy-SME threads per cluster.** OS load-balances across clusters;
still experiment for intensity and burstiness. Do not spawn one SME
worker per logical CPU.

An entire cluster shares a clock. **Any** core in SM or ZA mode:

- drops the **whole cluster** to a slightly lower frequency (power
  allotted to the engine)
- **powers up** the engine (resources other cores cannot use)
- fattens CS (Z / P / ZA state)

Do not colocate a latency-sensitive core-only thread on a cluster that
is sitting in streaming mode. Enter SM/ZA **only** for the kernel that
needs it; exit when done. Mode switch has cost — **no fine-grain
toggle** (also zeros V, Z, and P). SSFP is slower than core scalar FP.

Most software enables/disables **both** together. Independent control:

- **SM only:** long Z + predication without ZA. Single-issue SSVE +
  slower LL loads + expensive core transit usually **lose to core
  ASIMD**. Favor ASIMD unless the grid is in play.
- **ZA only:** hold ZA while running core ASIMD. Typical: `SMSTOP SM`
  after both were on, for a **brief** core excursion (especially scalar
  FP) without spilling 4 KiB ZA. Do **not** linger (power + CS tax).
  Scalar FP regs are **not** preserved across SM switches (V subsets
  out of SM, Z subsets in SM).

---

## Emit checklist

1. Rec 2 first (`cblas_sgemm` / BNNS) if it exists (`integer-isa.md`).
2. Rec 50: one direction per phase; no dependent scalar/predicate bounce.
3. Rec 51: flops in ZA MOPs; SSVE = load/pred/rearrange/extract; L1D-fit
   tiles → core ASIMD.
4. Rec 52: write **SME-8 full-tile MOPs**; no slow-engine special case;
   ≤2 heavy SME threads/cluster.
5. Rec 53: whole 4 KiB ZA; `ZERO { ZA }`; Z as load→consumer buffer; ~4
   independent chains.
6. Rec 54: unpredicated multi-vector `MOVA`; fusion outranks
   extract-on-the-side; no predicated single-vector MOVA; no default
   direct ZA `LDR`/`LD1Q`.
7. Rec 55: align stores; column-walk row length **≠ k×256 B**; optional
   `(k×256 B)+128 B`; group unaligned stores.
8. Rec 56: engine data on heap, **4 KiB isolated** from core/stack.
9. Rec 57: packing fusion — compute block, then mem block; no core-store
   interleave.
10. Rec 58: operation fusion — emit a pair above with **no** engine insn
    between; **W8 on VGx MOVA is correct**.
11. Prefetch: L2 dest = Shared LL only; NT `LDNT1`/`STNT1` for low-reuse;
    miss half-lines not allocated.
12. Mode: SM/ZA only while needed; cluster frequency drops for
    **everyone** on that cluster; ≤2 heavy SME threads; ZA-only is a
    **brief** `SMSTOP SM` excursion.

ISA wrapper / `SMSTART`: `sme-ssve.md`. Verify with
`apple-silicon-profile` (CPU Bottlenecks + Flow F). Named anti-pattern
(bounce, 4 KiB hazard): `performance-patterns`.
