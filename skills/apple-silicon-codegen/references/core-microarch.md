<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Core microarchitecture

Owns recs **19–49**. S/P/E why, M5 P ≠ M4 P, SME-8 vs SME-2:
`chip-families.md`. Class latencies / hierarchy: `latency-lookup.md`.
PMU names: `named-events.md`. Rec 65 LSE / QoS: `amp.md`. Sizes:
`sysctl.md`. Same ISA on S/P/E. OS places threads; hint with **QoS**,
never pin. Query `sysctl` — do not bake SKU tables. Cores are
**single-threaded (no SMT)**: full L1I / L1D / TLB belong to the one
running thread. No `_mm_pause`, HITM/c2c, SMT occupancy, NOP-align,
xor-self zero, or `vzeroupper`. The Apple tax is **MV2VEC** (rec 28).

## Line and page sizes (write every layout against these)

| Thing | Size | Where |
|-------|------|--------|
| L1I / L1D / PL2 **fill** line | **64 B** | core caches |
| Coherence / false-sharing / NT-store / Shared LL / DRAM line | **128 B** | `hw.cachelinesize` |
| Virtual page | **16 KiB** | `hw.pagesize` |
| `ADRP` architectural page | **4 KiB** | low 12 bits of the VA; not the OS page |

Ballpark: **L1D ~4c / own Shared LL ~15c / DRAM ~95 ns**. Other-cluster
LL ~50 ns; M Cache ~35 ns. Book: `latency-lookup.md`. Engine transit
~20c each way: `sme-engine.md`. Do **not** block to PL2 or **M Cache**
(rec 36) — M Cache is shared with GPU/ANE/other DRAM agents.

## Pipeline (write every stream against these)

Delivery (Fetch + Decode) keeps the window full. Processing (Map →
Schedule → EUs → Retire) finds independent work and **retires oldest
in order**. Cluster Interface sits outside the core.

- **Window:** hundreds of insns. Age = older/younger in the window.
  Results stash until in-order retire. A branch mispredict or a wrong
  memory-dep guess **flushes younger** work.
- **Map** inserts μops into the window (any-type slots). Sustained μop
  BW = Map. Burst = all EUs busy from window buffering. STA and STD
  count separately. Time-average **cannot exceed Map**. High IPC ≈
  near Map, not “the algorithm is fast” (rec 27).
- **Dispatch** = Map → Schedule. **Issue** = Schedule → EU. **Slots** =
  parallel in-order lanes.
- Most ISA insns = **1 μop**; extra srcs/dests or multi-pipe work =
  many (rec 26).
- **Demand** vs prefetch. **Unique miss** = first miss that starts a
  fill (hits-under-fill are not unique; a line can unique-miss many
  times if evicted).
- Datapath bypass skips RF write/read.
- Fetch reads a **group**, including **across a 64 B I-line**. A
  **taken** branch discards the rest of that group — a bandwidth hole
  (recs 22–23). Padding does not fill it.

## Branch vocab

- **Target** = next PC named by the insn.
- **Direct** = PC-relative in the insn. **Indirect** = target from a
  register.
- **Call** = `BL` / `BLR` writes **X30 = PC+4**. **Return** = `RET`
  from X30 (a special indirect).
- **All indirect branches are unconditional.** Conditional chooses
  target vs **PC+4**.
- **Fall-through / skip** = conditional FALSE → PC+4. **Taken** =
  unconditional, or conditional TRUE.
- **Predicted:** frontend guesses condition and/or indirect target and
  fetches ahead. **Direct unconditional `B`/`BL` are generally not
  predicted** (known). Everything else is — including every `BR` /
  `BLR` / `RET`.
- **Biased:** mostly one direction, or mostly one indirect target.
  Straighten so the common body **falls through**.
- **RAS** predicts returns (rec 25). Other indirect `BR`/`BLR` use a
  separate target predictor. Glossary (correct/wrong path, retired,
  highly predictable): `chip-families.md`.

## Memory EU, write-complete, AGU, barriers, DC ZVA

Widths: `latency-lookup.md`. STA (address) and STD (data) count
separately. **Write-complete:** two completed writes must be the
**same type** (data store, tag store, or SME op) else **one**. **E:
one** completed write. Do not mix data stores, MTE tag stores, and SME
ops in the same tight write stream. Engine insns (and predicate
updates) ship through the core LSU **as stores** (rec 57,
`sme-engine.md`). `MAP_LDST_UOP` includes GPR→vector converts **and
every engine insn**.

**AGU** covers most Arm addressing in-line. Extra addr μop when:
pre/post-index writeback; ASIMD LD/ST `lsl #4` / extend `#4`
(immediate ≥ 4); multi-element / structured LD/ST. Still emit those
forms for density (rec 26). One writeback per LD/ST run (rec 5,
`integer-isa.md`).

`DMB` / `DSB` / `PSSBB` / `SSBB` serialize the Memory EUs. `ISB` / `SB`
serialize instruction delivery. Prefer `STLR`/`LDAR` (or C11
release/acquire) over `DMB ISH` for publish (rec 7, `integer-isa.md`).
Do not sprinkle DMB around STLF or memcpy.

**`DC ZVA`:** fast zero of a DC line. Prefer it for large zeros (with
`DC GVA` for tags; not combined `DC GZVA`). `INST_LDST` / `INST_INT_ST`
do **not** count `DC ZVA` — a ZVA memset looks like “no stores”.

---

## Rec 19 — Hot functions in few pages (I-TLB) (Medium/Medium)

App pages are **16 KiB**. L1I TLB (and L1D TLB) are backed by a larger
Shared L2 TLB plus a walker. I-TLB miss delays fetch **several cycles**;
Map starves. Group hot functions onto a small set of 16 KiB pages, or
phase so the algorithm pounds one group then another. Cold helpers
(`log`, `abort`, error formatters) go in their own TU, not between two
hot functions. Do not bake I-TLB entry counts — they change by family
(`chip-families.md`).

```c
// hot_gemm.c  — only the inner kernels
// cold_errors.c
__attribute__((cold, noinline))
void gemm_fail(int rc) { /* ... */ }
```

---

## Rec 20 — Hot portions in few L1I lines (Medium/Medium)

Private L1I, 64 B lines, backed by PL2 (some S) + mixed I+D Shared LL.
Miss delays fetch like an I-TLB miss; Map starves. Keep the common path
compact. Park rare `if`/`else` at the **end of the function** (or a
`cold` callee) so those 64 B I-lines occupy L1I only when taken.
`[[gnu::cold]]` / `__attribute__((cold))` / `__builtin_expect` /
`__builtin_expect_with_probability` / C++20 `[[likely]]`/`[[unlikely]]`.

```c
float hot_path(const float *x, size_t n, int flags)
{
    if (__builtin_expect(flags & kBad, 0))
        goto rare;
    // dense loop
    return acc;
rare:
    return cold_fixup(x, n, flags);
}
```

---

## Rec 21 — Avoid L1I set conflicts (Low/Low)

16 KiB pages plus page-aligned dylibs can land hot I-blocks on the same
L1I sets — more often than on a 4 KiB page. E 4-way (from M2) conflicts
sooner than 6-way P/S. Unlikely first bottleneck; if L1I miss is high
with a **small** I-working-set, look at **placement**, not NOP padding
(rec 22). Query L1I size via `sysctl`; do not bake SKU tables.

---

## Rec 22 — Do not NOP-align branch targets (Low/Medium)

Fetch has flexible alignment of L1I reads and aggressive packing into
Decode — Decode fills **regardless of position** in the I-line, and
already packs **across 64 B**. Other machines want the target at the
start of a line so Fetch delivers a full group; that is why people
NOP-pad. Here it is unnecessary and often **hurts** I-cache and size.
A **taken** branch still discards the rest of the group — padding does
not fill that hole. Keep unaligned targets; smaller code wins. Do
**not** insert `nop` / `.p2align` to 16/32/64 B to align loops or
labels. Emit the natural stream. Never pad “because Intel.”

---

## Rec 23 — Taken-branch density danger ~1 in 8 (Medium/High)

Taken branches interrupt fetch; remaining bytes of the group are
discarded. Worst: **short high-count loops**. Direct `B`/`BL` are
generally **not predicted** (known) — they still cost that fetch hole
every time they are taken. When `INST_BRANCH_TAKEN / INST_ALL` ≈
**1/8**: unroll, invert so the common path **falls through**, inline
small callees, if-convert (rec 24). **Inline** kills a taken `BL` **and**
a taken `RET`. **Unroll** converts taken loop-backs to fall-through;
counted loops drop compare/branch except last; remainder loop for
leftovers. Error path = **taken**. Biased branches: common body on the
fall-through. `__builtin_expect` / `__builtin_expect_with_probability`
/ C++20 `[[likely]]`/`[[unlikely]]`.

```c
// no — taken branch every iteration of a 4-insn body
// yes — unroll 4–8, test once, fall through the work
```

---

## Rec 24 — CSEL/CSET over unpredictable branches (High/Medium)

When the branch is hard to predict **and** both sides are cheap (no
long FP chain, no likely-miss load, no divide, no de-interleave, no
`TBL`), execute both and select. Cond-op latency ≈ **max(then, else)**;
always pay both. A correctly predicted branch has optimal latency
(only needed insns, window stays full). Mispredict cost = wrong-path
work + restart Fetch/Map + lost correct-path speculation. Keep a
**branch** when the unused side is expensive, or when the branch is
highly biased (fall-through). All `B.<cond>` are **direct**. M5 / A19
`FEAT_HBC` `BC.<cond>`: Apple predictors **ignore the hint** — treat as
`B.cond`. Do not emit `BC` expecting a better predictor.

```c
#include <stdint.h>

// Clamp without a data-dependent branch.
int32_t clamp_i32(int32_t x, int32_t lo, int32_t hi)
{
    int32_t y;
    __asm__("cmp    %w1, %w2\n\t"
            "csel   %w0, %w2, %w1, lt\n\t"
            "cmp    %w0, %w3\n\t"
            "csel   %w0, %w3, %w0, gt"
            : "=&r"(y)
            : "r"(x), "r"(lo), "r"(hi)
            : "cc");
    return y;
}

int32_t abs_i32(int32_t y)
{
    int32_t r;
    __asm__("cmp    %w1, #0\n\t"
            "cneg   %w0, %w1, mi"
            : "=r"(r)
            : "r"(y)
            : "cc");
    return r;
}
```

Family: `CSEL`, `CSET`, `CSETM`, `CINC`, `CINV`, `CNEG`, `CSINC`,
`CSINV`, `CSNEG` (+ `FCSEL`). `CMP`+`CNEG` for abs; `FCMP`+`FCSEL` for
fmax (**`FMAX` NaN ≠ C `if`**). Compiler will **not** if-convert a
store only on one path; `res = cond ? … : res` enables `FCSEL`+`STR`
but **may fault or miss** the previously-skipped address.

---

## Rec 25 — Well-formed call/return for the RAS; limit depth (Medium/Low)

Call decode pushes next-PC onto the RAS; execute verifies; mispredict
flushes. Call with `BL` / `BLR` (X30 = PC+4); return with `RET` (from
X30). Do not `BR` to a return address, `RET` a homemade value, or skip
frames (`longjmp`-style single `RET` popping many) — that **misaligns
RAS**. A “fast” skip costs later. Inline short wrappers; spare
recursion. If return mispredicts stay high after pairing is fixed,
**flatten** the call tree (inline, or convert recursion to a heap stack
the RAS does not track). RAS is **not saved across process switches**.
First returns after a switch may miss; do not invent a software return
stack.

### Indirect `BR` / `BLR` (not returns)

All indirect are **unconditional** and **are predicted**. Returns use
the RAS above. Remaining `BR` / `BLR` use a target predictor. Hot
`switch` / vtable / function-pointer dispatch that mispredicts:
flatten to direct `B`/`BL` when the set is small and stable; keep
targets from bouncing every call; do not `BR` to a return address.
Direct uncond `B`/`BL` are generally **not** predicted — a computed
`BR` in a hot loop is the expensive kind. Measure
`BRANCH_INDIR_MISPRED_NONSPEC` and
`BRANCH_CALL_INDIR_MISPRED_NONSPEC` (`named-events.md`).

---

## Rec 26 — Use cracked / microcoded insns when you need the whole op (Low/Medium)

Cracked = 2–3 μops, seamless. Microcoded = 4+, extra insert overhead.
Use when you **need that operation**; do not split into more architected
insns unless measured. Do not reach for microcode when two ALU ops
suffice.

| Insn | Shape | Agent |
|------|-------|--------|
| Pre/post-index LD/ST | extra addr μop | **one** writeback per run |
| Integer `LDP` (not Q) | crack then **re-fuse** | prefer `LDP` |
| Integer `MADD` | S always; P from **M4**: MUL+ALU | **still emit `MADD`** |
| `TBL`/`TBX` | 1 / cracked / microcoded by table size | **smallest table**; prefer `EXT`/`REV`/`TRN`/`UZP`/`ZIP` |
| `LD3`/`ST3` (LD2–4/ST2–4) | mostly **microcoded** | long AoS (rec 10); else planar |
| `BFDOT`/`BFMMLA` | **3–8** vector μops | budget issue slots |

ASIMD `STP`/`STNP` and `lsl #4` / extend `#4` addressing: extra AGU
μop, still use for density. AGU otherwise covers Arm addressing
in-line; writeback and multi-element also extra μops. GPR↔V+convert
(`FCVTAS`, `SCVTF`, `DUP`/`INS` from GPR): cracked (recs 28–29).
Atomics (`LDop`/`STop`/`CAS`/`SWP`): cracked except **CAS** (rec 65,
`amp.md`) — still emit when full atomic semantics match; they replace
barrier sequences. Pre-M4 P/E `MADD` is one MAC-class μop; S always
and P from M4 have **no MAC class**. Do not split to `MUL`+`ADD`.
Density win is the 32-bit insn packing work, not μop count alone.

---

## Rec 27 — High IPC ≠ high performance (High)

`IPC = INST_ALL / CORE_ACTIVE_CYCLE`. High IPC means **this stream**
runs near Map (no bottlenecks, enough ILP). A vectorized loop at
**25% lower IPC** can still beat a scalar loop. One rich insn (`UABAL`)
replaces several → lower IPC, higher perf. If Useful is high and wall
time is still wrong: change the algorithm, data structure, or ISA
(ASIMD / SME), not the padding. Map vs burst widths: `latency-lookup.md`
— do not bake per-chip μop grids.

---

## Rec 28 — Minimize GPR ↔ vector moves (High impact; boxed Low/Medium)

Do not shuttle values between X and V. Load data destined for SIMD
**into V**. Short integer prologue that will feed FP: stay in V, SIMD
integer on lane 0. SIMD integer latency is longer than GPR, still
faster than vector-ST + integer-LD.

`FMOV`/`DUP`/`INS`/`MOV`/`SCVTF`/`UCVTF` from GPR is **MV2VEC**: ~4–5
cycles **and a load issue slot**. Extra logic if dest is not H/S/D or
if duplicating/converting. Partial-vector dest adds **2-cycle** latency
on that source. Direct `LDR S` + `SCVTF` **~7** vs GPR `LDR W` +
`SCVTF` **~11**.

```asm
    // no
    ldr     w0, [x1]
    scvtf   d1, w0              // MV2VEC + convert

    // yes
    ldr     s0, [x1]            // bits in a vector reg
    scvtf   d1, s0
```

Store-to-GPR then load-to-V is worse still. Vec→GPR: `FMOV` (rec 29).

---

## Rec 29 — FMOV when moving vector → GPR with no conversion

`FMOV` copies bits. `UMOV`/`SMOV` also extend (extra latency; common
for B/H). Even for integers in a lane: **FMOV if no extend**. Move
**3–4 cycles**. `FCVT*` scalar to GPR = convert+move **~6–7 cycles**.

```asm
    fmov    w0, s0              // yes, integer bits sitting in S0
    // umov    w0, v0.s[0]      // extra work
```

---

## Recs 30–32 — Preferred copy / zero / constant

Emit these. Do not invent a competing sequence.

**Rec 30 — copy (Medium/Medium).** Fan out from the **original** — do
not chain `x7 = x6 = x5`.

| File | Emit | Do not |
|------|------|--------|
| Integer | `MOV Xd, Xn` | `FMOV Dd, Dn` as an integer/ASIMD copy |
| | `ORR Xd, XZR, Xn` | chained `MOV X7, X6` after `MOV X6, X5` |
| | `ADD Xd, Xn, #0` | |
| ASIMD | `MOV.2D Qd, Qn` | `FMOV Dd, Dn` |
| | `ORR.16B Vd, Vn, Vm` when `Vm == Vn` | `DUP` as a whole-register copy |

**Rec 31 — zero (Low/Low).** Fixed-length ISA → imm 0 is the same size
as any insn (unlike x86 `xor` for size).

| File | Emit | Do not |
|------|------|--------|
| Integer | `MOVZ Xd\|Wd, #0` (equiv. `MOV Xd\|Wd, #0`) | `EOR Xd, Xd, Xd` |
| ASIMD | `MOVI.2D Qd, #0` | `EOR Vd, Vn, Vn` |

**`EOR Rd, Rn, Rn` does not break the dependence.** It waits for `Rn`.
`LDR X2, [X1]; EOR X3, X2, X2; LDR X4, [X1, X3]` **serializes the two
loads**. Never import `xor eax, eax`.

**Rec 32 — constants (Low/Medium).** Prefer fused pairs (`MOVZ`+`MOVK`).
Rec 6 (`integer-isa.md`): no literal pool as default.

| Emit | Notes |
|------|-------|
| `MOVZ X\|Wd, #imm` | 16-bit chunk; optional `LSL` 16/32/48 with `MOVK` |
| `MOVN X\|Wd, #imm` | inverted 16-bit chunk |
| `ORR X\|Wd, XZR, #imm` | logical immediate |
| `EOR X\|Wd, XZR, #imm` | logical immediate |
| `ADR Xd, <label>` | PC-relative |
| `ADRP Xd, <label>` | 4 KiB architectural page of PC, ±4 GiB |
| `BL <label>` | call; writes **X30 = PC+4** — not a GPR constant substitute |

```asm
    mov     x6, x5
    mov     x7, x5              // copies from the original; not x7 = x6

    movz    x0, #0              // zero; not eor x0, x0, x0
    movi    v0.2d, #0

    orr     v1.16b, v0.16b, v0.16b   // ASIMD copy when src==src
```

---

## Rec 33 — Limit FPCR/FPSR read/write frequency (High impact if ignored)

`MRS`/`MSR` of `FPCR`/`FPSR` are expensive. At most every few
**thousand** instructions. Do not poll FPSR per iteration. Set the mode
once at the kernel boundary.

---

## Rec 34 — Limited unroll + software pipeline of long loops (Medium/Medium)

Bodies of **several dozen** insns: whole body must Fetch+Map before the
next iter; finite window → few iters resident and **Map stalls**. Unroll
a **little** and interleave so iteration i+1 overlaps i, or **software
pipeline** (stagger the next iter; more complex, often less size growth).
Compilers unroll at high `-O`; Clang `#pragma unroll`. Then **re-check
L1I and I-TLB** (recs 19–20). Do not spill V-regs to “help” ILP (rec 13).
Use when cross-iter ILP is high but IPC is not.

---

## Rec 35 — Compact data / temporal locality (TLB + cache) (Medium/Medium)

Data pages are **16 KiB**. L1D TLB is backed by the same Shared L2 TLB
that backs L1I, then a walker. Miss all TLBs → hardware page-table walk
(tree; each step a memory op via PL2 / Shared LL / fabric). Overhead
**grows with working set**; L2 TLB and MMU BW are limited. Keep the
working **page** set inside the L1 D-TLB (a few MiB). Entry counts
change by family — query, do not bake (`chip-families.md`, `sysctl.md`).
Some gens drop D-TLB entries with other μarch wins — do not treat fewer
entries as a regression without measuring. Do not sprinkle live fields
across a sparse heap of 16 KiB pages. AoS pointer-chase per element is
a walk generator; pack, or phase so a page is hammered then dropped.
Do not mix two miss-streams in the same 16 KiB page (recs 47–48).

---

## Rec 36 — Cache-block to L1D and Shared LL sizes from sysctl (Medium/Medium)

Tile to **L1D** first, cluster **Shared LL** second. Read
`hw.perflevel0.l1dcachesize` and `hw.perflevel0.l2cachesize` (**that
key is Shared LL**). `perflevel0` = fastest cores. Do **not** block to
PL2 (S-only) or **M Cache**. Helpers: `sysctl.md`. L1D is usually
**write-back write-allocate**. Shared LL is 128 B, address-banked; the
hash **may change by chip**.

PL2 sits between L1 and Shared LL on some **S** cores (I+D, 64 B
lines). **No PL2 on P or E.** It cuts LL traffic for that core but
**adds** latency for requests that still go to Shared LL; extra snoop +
larger private state can **slow aggressively shared MT**. Software
prefetch **cannot target PL2** (rec 47). PRFM “L2” encoding = Shared
LL.

---

## Rec 37 — False sharing: independent shared vars on different **128 B** lines (High)

Two threads on **different clusters** writing independent fields on the
same **128 B** line bounce the line across the fabric. Pad or align to
128, not 64. Remaining thrash should be true sharing. Semaphores next
to counters are the classic trap.

```c
#include <stdalign.h>
#include <stdint.h>

struct Counter {
    alignas(128) uint64_t value;
};

struct Pair {
    alignas(128) uint64_t writer_a;
    alignas(128) uint64_t writer_b;   // not 64
};
```

`hw.cachelinesize` is 128 on Apple silicon; 128 is the layout constant
this skill writes.

---

## Rec 38 — Pack hot variables onto the fewest cache lines (High)

Dual of rec 37. Co-used inner-loop fields on the **same 64 B fill
line** (L1D/PL2). Same 128 B coherence line also helps Shared LL (may
still cost two 64 B L1 fills). Cold after, not between. C will not
reorder struct fields; group same-align members to cut padding.
Workflow: false-share first → pack hot compact → fill gaps with cold →
remaining cold. List/tree: **next-pointer at front** with hot payload
(rec 39).

```c
struct Node {
    struct Node *next;   // rec 39: next pointer with the hot payload
    uint64_t key;
    float    acc[4];     // hot
    uint32_t _pad;
    char     debug_name[64];  // cold — after, not between
};
```

---

## Rec 39 — Fast load-to-load when pointer chasing (Medium/Medium)

**~3-cycle** load-to-load when the first load’s result feeds the
**base (Xn)** of the next load, no intervening write to that register.
Any addressing form, any reg number. Do not put the pointer in **Xm**
(offset); do not `add`/`and` it in between. Unrelated insns that do not
touch the pointer are fine.

```asm
1:  ldr     x1, [x1, #8]        // result is the next base
    // other ops, but do not write x1
    cbnz    x1, 1b
```

Prefetch will not save a pointer chase (rec 47). Pack `next` with the
hot payload (rec 38) so one 64 B line covers the hop.

---

## Rec 40 — Non-temporal stores for large low-reuse sets (Medium/Low)

`LDNP` / `STNP` hint allocation. Streaming a buffer you will not
reread: NT **stores**, covering a full **128 B** line in a short window.
Do not mix NT and regular stores to that line; do not NT-store data you
will load again soon. NT **loads**: brief use; don’t mix with regular
in the window; don’t NT if you’ll store soon. Mixing NT and regular on
a line is architecturally ordered but **slow**.

```c
void dump_scratch(float *dst, const float *src, size_t n)
{
    for (size_t i = 0; i < n; ++i)
        __builtin_nontemporal_store(src[i], dst + i);
}
```

The builtin does not **guarantee** an NT encoding. Prefer rec 41 for
bulk copies. `ST_NT_UOP` **includes** SSVE/SME loads; `LD_NT_UOP`
**excludes** them (`named-events.md`).

---

## Rec 41 — Tuned memcpy for data movement (Medium/High)

Call `memcpy` / `memmove` / `memset`. They already pick NT vs temporal,
alignment, and (on some chips) engine paths. Do not write a `ldr q` /
`str q` copy loop “to be faster than libc”. Rec 2 lists `memcpy` as a
platform technology; **this** rec is the microarch reason.

**`FEAT_MOPS` is not on M5** — do not emit `CPY*`/`SET*` as the memcpy
path (`chip-families.md`). Unaligned memcpy typically uses 16 B ops:
every store hits 16 B, every 4th load hits 64 B, plus page splits
(recs 42–43).

---

## Rec 42 — Minimize unaligned large-array access (Medium/Low)

Align large arrays to the **access size** (4 B elements on 4 B, 16 B
ASIMD on 16 B, NT stores to 128 B). Crossing 64 B, 128 B, or 16 KiB
costs extra μops. Sporadic unaligned is fine; **stores hurt more than
loads**. Inner loops over large buffers are not “occasional.”

- Loads: any align **inside a 64 B line = aligned cost**. Split 64 B +
  miss (esp. both halves) can be **≥2×** a normal miss, **serialized**.
- **Page-split loads wait until they are the oldest load** (both
  translations). Avoid.
- Stores: split **16 B** uses **two D-cache write ports** → up to
  **2×** if every store splits. Page-split stores wait until **oldest
  store**.

```c
alignas(16) float tile[1024];
```

---

## Rec 43 — If unaligned, prefer unaligned **loads** over stores (Medium/Low)

Keep stores aligned; let loads be the unaligned side. Exception: if
loads hugely outnumber stores, aligning the loads and accepting
unaligned stores can win because store penalty hides under load misses.

---

## Rec 44 — Reduce hard-to-predict STLF (High/Medium)

LD/ST issue OOO; true overlap is a mem dep not in the RF. The predictor
guesses whether a ready load depends on an older store whose address
may still be unknown. Do not write a location and sporadically reload
it a few tens-to-hundreds of instructions later. Keep the value in a
register, or push the reload far enough that the store has hit the
cache. Predictor often **conservative** (flush is expensive). Occasional
overlap trains it wrong. Thrash = enforce when unused **and** miss when
needed. Production: **~50%** loss; fixes recover **50–150%** when
possible (not always — cache-hit tradeoffs). RMW in a large window
overlaps successive updates if they are “close” (a few loop iters,
**not** thousands).

| Predicted \ actual | Independent | Dependent |
|--------------------|-------------|-----------|
| Independent | high | **flush** (worst) |
| Dependent | phantom stall | wait (still slower than regs) |

Predicted-dependent load waits for store data and may read **store
buffers** before cache write (mix of younger stores + cache bytes).
Line/page-split loads delay forwarding. Multi-store → one-load makes
the predictor conservative (rec 45).

Shapes: **histograms**; **in-place FFT** (tiny radices train a
conservative predict, later passes do not need it); **pointer lists
into a small object pool** (graphics/physics — worst thrash); **hot
stack top** push/pop (stale predicts on later reuse; stack addr is
often simple enough to skip the predictor); growing structures that
were dependent while small; small in-place 2D (neighbor `(m,n−1)`
still in the window); passing pointers that cross.

Mitigations: RMW in a **register**; separate in/out buffers /
double-buffer; split small-n vs large-n loop clones so early training
does not poison the long loop; unroll **n** when every nth iter
forwards (1 dependent + n−1 independent copies); FFT: finish a radix
pass before the next, switch to cache-opt after **~1K pts**;
sort/spread pointer lists so the same object is not adjacent iters.
Do not replace the collision with a per-iter `if` (swaps a mem-order
flush for a branch mispredict). Do **not** “fix” STLF with `DMB`.
Pattern: `performance-patterns` `store-forwarding.md`.

---

## Rec 45 — Avoid many small stores forwarding into one wide load (Medium/Medium)

Do not `STRB` a buffer together and immediately `LD1` 16 B of it. The
predictor may serialize the wide load behind **all** older stores
(better than a flush, still serializes). Window can keep stores
**hundreds of insns** old. Write the wide type you intend to read, or
finish the byte stores in a **different phase** before the vector load.
Do not access the same memory as two widths inside a few hundred insns.

---

## Rec 46 — Flexible STLF when register communication is worse (Low/Low)

If register merge would be a spaghetti of shifts, let STLF do it.
Predicted-dependent load waits for store data and may read store
buffers before cache write. Extra HW opt: single-element **4 B or 8 B
integer** LD/ST, **no sign-extend**, **simple base+offset**.
Whole-register spill/fill with simple `base+imm` is specifically
optimized. `STP`/`LDP` are generally good, but **non-SP** paired ST→LD
in a **tight loop** may **block** those extra opts — use single-reg
STR/LDR there. Line/page-split loads delay forwarding.

---

## Rec 47 — Software prefetch sparingly, after HW-prefetcher-friendly layout (Medium/Low)

Hardware prefetch is the default: per-core, watches **that core’s
misses**, launches DRAM early. Linear and many irregular streams work;
max stride is limited. **S/P often need 4–30 lines lookahead, E less —
same binary retunes; software prefetch distance does not.** HW uses
idle LD/ST cycles and can drop under contention. SW `PRFM` is
**mandatory**, occupies decode/ROB/load slots, and **masks** streams
the HW prefetcher would have learned (SW does **not** train HW). Insert
`PRFM` only after rec 48 **and** measured residual L1D/LL misses, then
A/B with and without. Distance: **hundreds of cycles**; grows on newer
cores. Pointer-chase cannot be prefetched — addresses are not known
early. Large strides **≳ a page** may be tracked as separate streams.

**One PRFM per line**; address-within-line is irrelevant. `PRFM` uses
LDR/STR addressing; `PRFUM` uses LDUR/STUR addressing — **same op**.
L1 dest = **L1D**. L2 dest = **Shared LL**. You **cannot target PL2**.
Prefetch **never faults** (safe past array / NULL) — also cannot take a
real page fault early. MTE: tags prefetch too; L1 dest fills **L1 Tag
Cache**, not L1D.

Emit these. `KEEP` = temporal; `STRM` = non-temporal.

| Emit | Op | Dest | Locality |
|------|----|------|----------|
| `PRF(U)M PLDL1KEEP` | load | L1D | KEEP |
| `PRF(U)M PLDL1STRM` | load | L1D | STRM |
| `PRF(U)M PLDL2KEEP` | load | Shared LL | KEEP |
| `PRF(U)M PLDL2STRM` | load | Shared LL | STRM |
| `PRF(U)M PSTL1KEEP` | store | L1D | KEEP |
| `PRF(U)M PSTL1STRM` | store | L1D | STRM |
| `PRF(U)M PSTL2KEEP` | store | Shared LL | KEEP |
| `PRF(U)M PSTL2STRM` | store | Shared LL | STRM |

Do **not** emit `PLDL3*` / `PSTL3*` (no L3 dest) or `PLI*` (instruction
prefetch). Do not encode “L2” expecting PL2.

Do not mix **two miss-streams in the same 16 KiB page** (adjacent 2D
rows look like one irregular stream). Pair a top-half row with a
bottom-half row, or rearrange into one linear stream, **before**
`PRFM`. Engine prefetch fills Shared LL only (`sme-engine.md`).

---

## Rec 48 — Recode access patterns for the hardware prefetcher (Medium/Medium)

Give the HW prefetcher one or a few regular streams. Walk one row at a
time. If two rows: far apart (not the same 16 KiB page). Options before
`PRFM`:

- Avoid adjacent-row unroll. Pair `m` with `m+M/2`.
- Rearrange into one linear miss stream (`X, X+1, …` then the next row,
  not `X, X+M, X+1, X+M+1, …`).
- Tiny **demand** read-ahead (stride = L1 line) then the compute loop;
  or read-ahead both rows as one linear 2N stream.

Partial SW: `PRFM` row `m+2` so demand does not stall — SW **does not
train HW**, so aim **further** than a demand read-ahead. Leave the
other row as demand so HW still sees one clean stream. All-SW: PRFM
both future rows; may interleave; can fuse prefetch+compute per
line-chunk; don’t prefetch so far lines fall out of L1D. Fuse only if
the read-ahead is **pure PRFM** (demand in the same loop re-creates two
streams). Neither HW nor SW fixes pointer chasing (recs 38–39).

---

## Rec 49 — Platform allocators (MTE + performance); dense layouts (High/High)

Use the system allocator. Apple does **not** recommend custom
allocators (MTE safety + tag-cache behavior). Rec 8 (`integer-isa.md`)
is the ISA view (stack not tagged; SP/PC-relative not checked; SM/ZA
**are** checked). This rec adds the microarch:

- Tag memory is normal cacheable and **coherent**, but the ISA requires
  **software sync** between a tag access and a data access to the same
  location. Tags have their own insns. Tag lines snoop/share like data
  — GC threads that rewrite tags bounce lines.
- 4 bits / **16 B** granule. Separate **L1 Tag Cache** (no L1D capacity
  fight; tags **do** share PL2/LL). One 64 B tag line covers **2 KiB**
  of data; one 128 B LL tag line covers **4 KiB**. Dense allocations so
  one tag line serves many live L1D lines. Sparse tagged heaps (one
  data line per 2 KiB) make the tag cache the limiter — extra traffic,
  higher LD/ST latency. Same density rule as D-TLB.
- Loads: data+tag in parallel; **speculative forward without waiting
  for tag**; **retirement** waits (exception on mismatch). Little extra
  exec latency; slightly less speculation depth. Stores wait for tag
  before writing data; **forwarding can still happen while waiting**.
  Data prefetch also prefetches tags. Do not mix tag stores and data
  stores in one tight write stream (write-complete, above).
- Do not use combined tag+data stores (`STZG`/`ST2G`/`STZ2G`/`STGP`/
  `DC GZVA`) as the fast path — multi-μop, often slower than **separate
  tag-store block + data-store block**. Prefer `DC GVA` + `DC ZVA` for
  large blocks.
- `MSR PSTATE.TCO, #imm` (immediate form) if a memory manager must
  store tags then data without STLF-on-tags; do not sprinkle TCO in app
  code.

---

## After layout / instruction choices

Class of the inner-loop op: `latency-lookup.md`. Verify:
`apple-silicon-profile`. Named anti-pattern: `performance-patterns`.
