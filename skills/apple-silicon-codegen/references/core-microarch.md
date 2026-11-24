<!-- Structure inspired by Intel Performance Skills (MIT, 2026).
     Content is original agent instructions paraphrasing Apple Silicon CPU
     Optimization Guide 5.0. Do not copy Guide tables. MIT license. -->

# Core microarchitecture (Guide Ch. 5)

Owns recs **19–49**. Do not copy per-chip L1I/L1D/LL sizes or execution-class
bandwidth tables — query `sysctl` (`sysctl.md`) and open Appendix A
(`latency-lookup.md`) for numbers.

## Line and page sizes (write every layout against these)

| Thing | Size | Where |
|-------|------|--------|
| L1I / L1D / Private L2 **fill** line | **64 B** | core caches |
| Coherence / false-sharing line | **128 B** | `hw.cachelinesize`; Shared LL and below |
| Virtual page | **16 KiB** | `hw.pagesize` |
| `ADRP` architectural page | **4 KiB** | low 12 bits of the VA; not the OS page |

Typical unloaded latency order-of-magnitude (paraphrase of App. A; **open
Appendix A for the chip**): L1D hit a handful of cycles (**~4**), own Shared
LL **~15 cycles**, DRAM on the order of **~95 ns** (hundreds of cycles,
frequency-dependent). GPR→vector (`MV2VEC`) **~4–5 cycles**. SME transit
**~20 cycles** each way (`sme-engine.md`).

Do **not** block to M Cache capacity. M Cache sits at the DRAM controller,
is shared with GPU/ANE/other agents, and is not a CPU blocking target
(rec 36).

Cores are **single-threaded** (no SMT). Full I-cache / D-cache / TLB of a
core belong to the one running thread.

---

## Rec 19 — Hot functions in few pages (I-TLB) (Medium/Medium)

Group hot functions onto a small set of 16 KiB pages, or phase the
algorithm so it pounds one group of functions then another. Cold helpers
(`log`, `abort`, error formatters) go in their own TU or at least not
between two hot functions.

```c
// hot_gemm.c  — only the inner kernels
// cold_errors.c
__attribute__((cold, noinline))
void gemm_fail(int rc) { /* ... */ }
```

---

## Rec 20 — Hot portions in few L1I lines (Medium/Medium)

Inside a function, keep the common path compact. Park rare `if`/`else`
bodies at the **end of the function** (or in a `cold` callee) so their 64 B
I-lines occupy L1I only when taken.

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

`[[gnu::cold]]` / `__attribute__((cold))` on the rare callee.

---

## Rec 21 — Avoid L1I set conflicts (Low/Low)

16 KiB pages plus page-aligned dylibs can land hot I-blocks on the same
L1I sets. Unlikely to be the first bottleneck; if L1I miss is high with a
small working set of instructions, look at **where** dylibs and huge
functions were placed (page alignment), not at NOP padding (rec 22).

---

## Rec 22 — Do not NOP-align branch targets (Low/Medium)

Do **not** insert `nop` to align loops or labels to 16/32/64 B. Fetch is
flexible; extra NOPs cost I-cache and decode. Let the assembler/compiler
emit the natural stream.

---

## Rec 23 — Taken-branch density danger ~1 in 8 (Medium/High)

Taken branches interrupt fetch. When density approaches **one taken branch
per eight instructions**, fetch bandwidth collapses. Unroll, straighten so
the common path **falls through**, inline small callees, convert
unpredictable conditionals to CSEL (rec 24).

```c
// no — taken branch every iteration of a 4-insn body
// yes — unroll 4–8, test once, fall through the work
```

Biased branches: put the common body on the fall-through.

---

## Rec 24 — CSEL/CSET over unpredictable branches (High/Medium)

When the branch is hard to predict **and** both sides are cheap (no long
FP chain, no likely-miss load, no divide), execute both and select.

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

Family: `CSEL`, `CSET`, `CSETM`, `CINC`, `CINV`, `CNEG`, `CSINC`, `CSINV`,
`CSNEG`. Keep a **branch** when the unused side is expensive (cache-missing
load, long dependency, divide, table lookup).

M5 / A19 `FEAT_HBC` adds `BC.<cond>` as a “likely taken” hint. Apple
predictors **ignore the hint** — treat `BC.cond` as `B.cond`. Do not emit
`BC` expecting a better predictor.

---

## Rec 25 — Well-formed call/return for the RAS; limit depth (Medium/Low)

- Call with `BL` / `BLR`; return with `RET` (from X30).
- Do not `BR` to a return address, or `RET` to a value you did not receive
  from a call, or pop a homemade return stack.
- If return mispredicts stay high after pairing is fixed, **flatten** the
  call tree (inline, or convert recursion to a heap stack the RAS does not
  track). Deep callback pyramids overflow the RAS.
- RAS is **not saved across process switches**. First returns after a
  switch may miss; do not “fix” that with a homemade return stack.

---

## Rec 26 — Use cracked / microcoded insns when you need the whole op (Low/Medium)

A cracked instruction is 2–3 μops, seamless. A microcoded one is 4+ and
pays extra overhead inserting into the stream. Either is still the right
choice when you **need that operation**. Do not split it into a “simpler”
sequence that takes more architected instructions unless you have measured
a win. Conversely, do not reach for a microcoded helper when two simple
ALU ops suffice.

Write-time examples (open Appendix A for the chip; do not dump tables):

- Integer `MADD` is **cracked** into MUL-class + ALU-class on S cores and
  on P from M4 / A18. Still emit `MADD` (`integer-isa.md`).
- `TBL`/`TBX` cost 1 μop, cracked, or microcoded by **table size** — use
  the **smallest table that works**. Prefer `EXT`/`REV`/`TRN`/`UZP`/`ZIP`
  first (rec 16, `asimd.md`).
- Structured `LD3`/`ST3` (and friends) are powerful and mostly
  **microcoded**. Use them for a long AoS stream (rec 10); re-layout to
  planar if interleave is the inner-loop tax.

---

## Rec 27 — High IPC ≠ high performance (High)

IPC (or Useful μPC) near the core’s cap means the machine is executing
**this instruction stream** well. It does not mean the algorithm is fast.
A vectorized loop at lower IPC can beat a scalar loop at higher IPC.
If CPU Bottlenecks shows high Useful and the wall time is still wrong:
change the algorithm, the data structure, or the ISA (ASIMD / SME), not
the padding.

---

## Rec 28 — Minimize GPR ↔ vector moves (High impact; boxed Low/Medium)

Do not shuttle values between X and V. Load data destined for SIMD **into
V**. For a short integer prologue that will feed FP, keep it in V and use
SIMD integer ops on lane 0.

`FMOV`/`DUP`/`INS` from a GPR to a vector is **MV2VEC**: ~4–5 cycles **and
it consumes a load issue slot**, so it fights real loads.

```asm
    // no
    ldr     w0, [x1]
    scvtf   d1, w0              // MV2VEC + convert

    // yes
    ldr     s0, [x1]            // bits in a vector reg
    scvtf   d1, s0
```

Store-to-GPR then load-to-V is worse still. If you must cross files, `FMOV`
is the preferred vec→GPR form (rec 29).

---

## Rec 29 — FMOV when moving vector → GPR with no conversion

`FMOV` copies bits. `UMOV`/`SMOV` also extend and cost more. Even when the
lane holds an integer, use `FMOV` if you do not need the extend.

```asm
    fmov    w0, s0              // yes, integer bits sitting in S0
    // umov    w0, v0.s[0]      // extra work
```

FP→int conversions (`FCVT*`) are a convert plus a move (~6–7 cycles); that
is a different op.

---

## Recs 30–32 — Preferred copy / zero / constant (Guide 5.5.3)

**Rule: use the Guide 5.5.3 preferred forms. Do not invent a competing
sequence.** Open 5.5.3.1–3 before picking a move/zero/constant idiom.

Write-time facts you may apply without re-deriving the table:

- **Copy (5.5.3.1):** `MOV Xd, Xn` (or `ORR Xd, XZR, Xn` / `ADD Xd, Xn, #0`).
  ASIMD: `MOV.2D Qd, Qn`. When making **several** copies, copy from the
  **original** each time — do not chain `x7 = x6 = x5`.
- **Zero (5.5.3.2):** `MOVZ Xd, #0` (i.e. `MOV Xd, #0`). ASIMD: `MOVI.2D Qd, #0`.
  **`EOR Xd, Xd, Xd` does not break the dependence** on Arm — the EOR waits
  on the producer of `Xd`. Do not import the x86 `xor eax, eax` idiom.
- **Constants (5.5.3.3):** `MOVZ`/`MOVN`/`ORR Xd, XZR, #imm`/`ADR`/`ADRP`.
  Prefer the documented pairs that hit fast-constant generation; rec 6
  (`integer-isa.md`) is the ISA-level “no literal pool” rule.

```asm
    mov     x6, x5
    mov     x7, x5              // copies from the original

    movz    x0, #0              // zero; not eor x0, x0, x0
    movi    v0.2d, #0
```

---

## Rec 33 — Limit FPCR/FPSR read/write frequency (High impact if ignored)

`MRS`/`MSR` of `FPCR`/`FPSR` are expensive. Touch them at most every few
**thousand** instructions. Do not read FPSR per loop iteration to poll
inexact/overflow. Set the mode once at the kernel boundary.

---

## Rec 34 — Limited unroll + software pipeline of long loops (Medium/Medium)

For loop bodies of several dozen instructions with cross-iteration ILP but
mediocre IPC: unroll a **little** and interleave (software pipeline) so
independent ops from iteration i+1 overlap with i. Then **re-check L1I and
I-TLB miss** — unrolling can create rec 19–20 problems. Combine with rec 13
(do not spill V-regs).

---

## Rec 35 — Compact data / temporal locality (TLB + cache) (Medium/Medium)

Do not sprinkle live fields across a sparse heap of 16 KiB pages. Keep the
working **page** set inside the L1 D-TLB. AoS with a pointer-chase per
element is a TLB walk generator; pack, or phase accesses so a page is
hammered then dropped.

---

## Rec 36 — Cache-block to L1D and Shared LL sizes from sysctl (Medium/Medium)

Tile so a working set fits in **L1D** (first) or the cluster **Shared LL**
(second). Read:

- `hw.perflevel0.l1dcachesize`
- `hw.perflevel0.l2cachesize` (this is Shared LL, despite the name)

`perflevel0` is the fastest cores. Do **not** block to Private L2 (only some
S cores) or to **M Cache**. Helpers: `sysctl.md`.

PL2 sits between L1 and Shared LL on some **S** cores (I+D, 64 B lines).
**No PL2 on P or E.** It cuts LL traffic for that core, but it **adds**
latency for requests that must still go to Shared LL versus a core without
PL2. Extra snoop latency and larger private state can **slow aggressively
shared** multithreaded code. Software prefetch **cannot target PL2**
(rec 47).

---

## Rec 37 — False sharing: independent shared vars on different **128 B** lines (High)

Two threads writing independent fields on the same **128 B** line bounce
the line across clusters. Pad or align to 128, not 64.

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

`hw.cachelinesize` is 128 on Apple silicon; do not hardcode if you can
query, but 128 is the layout constant this skill writes.

---

## Rec 38 — Pack hot variables onto the fewest cache lines (High)

The dual of rec 37. Fields used together in the inner loop go on the
**same 64 B fill line** (and the same 128 B coherence line) to cut L1D
occupancy. Cold fields fill the gaps; leftover cold fields sit **after**
the hot cluster, not between hot fields.

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

There is a **~3-cycle** load-to-load bypass when the first load’s result
feeds the **base (Xn)** of the next load, with no intervening write to that
register. Do not put the pointer in the **offset (Xm)** register, and do
not `add`/`and` it in between. Unrelated instructions that do not touch
the pointer are fine.

```asm
1:  ldr     x1, [x1, #8]        // result is the next base
    // other ops, but do not write x1
    cbnz    x1, 1b
```

Prefetch will not save a pointer chase (rec 47). Pack `next` with the hot
payload (rec 38) so one 64 B line covers the hop.

---

## Rec 40 — Non-temporal stores for large low-reuse sets (Medium/Low)

Streaming a buffer you will not reread: NT stores, preferably covering a
full **128 B** line in a short window. Do not mix NT and regular stores to
that line, and do not NT-store data you will load again soon.

```c
void dump_scratch(float *dst, const float *src, size_t n)
{
    for (size_t i = 0; i < n; ++i)
        __builtin_nontemporal_store(src[i], dst + i);
}
```

The builtin does not **guarantee** an NT encoding. Prefer rec 41 for bulk
copies. NT loads: only when the line is touched briefly and not later
stored with a regular store.

---

## Rec 41 — Tuned memcpy for data movement (Medium/High)

Call `memcpy` / `memmove` / `memset`. They already pick NT vs temporal,
alignment, and (on some chips) engine paths. Do not write a `ldr q`/`str q`
copy loop “to be faster than libc”. Rec 2 lists `memcpy` as a platform
technology; **this** rec is the microarch reason.

---

## Rec 42 — Minimize unaligned large-array access (Medium/Low)

Align large arrays to the **access size** (4 B elements on 4 B boundaries,
16 B ASIMD on 16 B, NT stores to 128 B when you can). Crossing 64 B, 128 B,
or 16 KiB costs extra μops. A load that crosses a **16 KiB page** waits
until it is the **oldest load**.

```c
alignas(16) float tile[1024];
```

---

## Rec 43 — If unaligned, prefer unaligned **loads** over stores (Medium/Low)

Stores pay more boundary cases than loads. If the algorithm can choose,
keep stores aligned and let loads be the unaligned side. Exception: if
loads hugely outnumber stores, aligning the loads and accepting unaligned
stores can win because store penalty hides under load misses.

---

## Rec 44 — Reduce hard-to-predict STLF (High/Medium)

Do not write a location and sporadically reload it a few tens-to-hundreds
of instructions later. Either keep the value in a register, or push the
reload far enough that the store has hit the cache. Predicted-wrong
store-to-load forwarding flushes the window.

Shapes that train the predictor wrong (Guide 5.6.11): **histograms**;
**in-place FFT** (tiny radices train a conservative predict, later passes
do not need it); **pointer lists into a small object pool**; **hot stack
top** push/pop; growing structures that were dependent while small.
Mitigations: keep the RMW value in a register; separate in/out buffers;
split small-n vs large-n loop clones so early training does not poison
the long loop. Do not replace the collision with a per-iter `if` — that
swaps a memory-order flush for a branch mispredict. Named pattern:
`performance-patterns` `store-forwarding.md`.

---

## Rec 45 — Avoid many small stores forwarding into one wide load (Medium/Medium)

Do not `STRB` a buffer together and immediately `LD1` 16 B of it. The
dependence predictor may serialize the wide load behind **all** older
stores. Write the wide type you intend to read, or finish the byte stores
and wait (different phase) before the vector load.

---

## Rec 46 — Flexible STLF when register communication is worse (Low/Low)

If forwarding through registers would require a spaghetti of shifts/merges,
let the store-to-load network do it. Whole-register spill/fill with simple
`base+imm` 4 B/8 B integer LDR/STR is specifically optimized. For
**non-stack** `STP`/`LDP` that STLF in a tight loop, prefer single-register
STR/LDR so the extra forwarding optimizations can engage.

---

## Rec 47 — Software prefetch sparingly, after HW-prefetcher-friendly layout (Medium/Low)

Hardware prefetch is the default. Software `PRFM` occupies decode, ROB, and
load/store slots and **masks** the streams the HW prefetcher would have
learned. Insert `PRFM` only after (1) you recoded the access (rec 48) and
(2) you measured residual L1D/LL misses. Distance is typically **hundreds
of cycles**; it grows on newer cores. Prefetch does not fix pointer chasing.

You **cannot target PL2**. The architectural “L2” destination is **Shared
LL**. Do not mix **two miss-streams in the same 16 KiB page** (adjacent
2D rows look like one irregular stream). Pair a top-half row with a
bottom-half row, or rearrange into one linear stream, before inserting
`PRFM`. Engine prefetch fills Shared LL only (`sme-engine.md`).

---

## Rec 48 — Recode access patterns for the hardware prefetcher (Medium/Medium)

Give the HW prefetcher one or a few regular streams, not a braid of
adjacent rows. Walk one row at a time; if you must touch two rows, pick
rows far enough apart that they do not look like one confused stream on
the same **16 KiB** page (rec 47). Before inserting `PRFM`, simplify.

---

## Rec 49 — Platform allocators (MTE + performance); dense layouts (High/High)

Use the system allocator. Apple does **not** recommend custom allocators
(MTE safety + tag-cache behavior). Rec 8 (`integer-isa.md`) is the ISA
view; this rec adds:

- Dense allocations so one L1 Tag Cache line (covering 2 KiB of data at L1,
  4 KiB at LL) serves many live L1D lines. Sparse tagged heaps thrash tags.
- Do not use `STZG`/`ST2G`/`STGP`/`DC GZVA` as the fast path — split tag
  stores and data stores; prefer `DC GVA` + `DC ZVA` for large blocks.
- `MSR PSTATE.TCO, #imm` (immediate form) if a memory manager must store
  tags then data without STLF-on-tags; do not sprinkle TCO in app code.

---

## After layout / instruction choices

Look up the mnemonic’s class in Appendix A (`latency-lookup.md`) before
freezing an unroll. Verify with `apple-silicon-profile`. Remaining named
anti-pattern: `performance-patterns`.
