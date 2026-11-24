<!-- Structure inspired by Intel Performance Skills (MIT, 2026).
     Content is original agent instructions paraphrasing Apple Silicon CPU
     Optimization Guide 5.0. Do not copy Guide tables. MIT license. -->

# Latency lookup — how to use Guide Appendix A

This file does **not** own a boxed rec (rec 65 LSE atomics lives in
`amp.md`). It is the write-time procedure for Appendix A. **Do not
reproduce** instruction-latency rows, execution-class bandwidth tables, or
per-core-type unit counts.

---

## What to open

Open **Apple Silicon CPU Optimization Guide 5.0, Appendix A** for the
**current chip family** (M1–M5 / A14–A19) and the **core type** the kernel
will actually run on (S / P / E). Latencies sometimes differ by core type;
bandwidth (how many units of that class) almost always does.

The appendix splits operations into classes, then maps classes onto
execution units:

| World | Classes (idea, not a dump) |
|-------|----------------------------|
| Core integer | ALU, mul, div, miscellaneous, branches, … |
| Core ASIMD / FP | FMLA / FP add / integer SIMD / permute / convert / … |
| Core load/store | including **MV2VEC** (GPR→vector uses a load slot) |
| Engine SSVE / SSFP | one issue, some μops occupy the unit for extra cycles |
| Engine SME grid | the high-throughput path |
| Engine extract | ZA → Z, parallel with the grid |
| Engine load/store | Shared LL, not L1D |

A mnemonic in the “example instructions” column of an Operation Class table
is a **proxy** for that μop. If your instruction is not listed, match it by
the class description, not by guessing a latency of 1.

---

## How to use a row while writing a kernel

1. Identify the **recurring** op in the inner loop (`FMLA.4S`, `FMOPA`,
   `LD1`, `UDOT`, …).
2. Find its **class** in Appendix A. Read **latency** and **how many such
   units** that core type has (the Availability table for S/P/E).
3. Pick independent accumulators / tiles so that

   **unroll ≈ latency / throughput_per_cycle**

   Example shape (numbers come from the book, not from this file): if FMLA
   is latency L and the P core can issue T of that class per cycle, start
   with about `ceil(L/T)` independent accumulators, then cut back if you
   spill (rec 13, `asimd.md`) or blow L1I (recs 19–20,
   `core-microarch.md`).
4. Recheck the **load class**. A 4-accumulator FMLA kernel that is starved
   for `LD1` is load-bound; unroll will not help until you change layout
   (rec 36, rec 48).
5. For SME, look up **grid** class latency (~4 cycles is the planning
   figure in Ch. 6) and hide it with whole-ZA ILP (rec 53,
   `sme-engine.md`). SSVE FMLA is **not** the grid class.

Consumer ops do **not** always start the cycle after the producer: window
entry, other operands, and missing bypasses all add delay. Treat Appendix A
as the **floor**, then measure (`apple-silicon-profile`).

---

## Order-of-magnitude memory and cross-file numbers

Do not substitute these for Appendix A. They exist so you can refuse
obviously wrong designs without opening the PDF. Full write-up:
`core-microarch.md` (cache) and `sme-engine.md` (transit).

| Path | Order of magnitude |
|------|--------------------|
| L1D hit | handful of cycles (**~4**) |
| Own Shared LL | **~15 cycles** |
| Other-cluster LL / M Cache / DRAM | tens of ns; DRAM **~95 ns** |
| GPR → vector (`MV2VEC`) | **~4–5 cycles** and a **load** slot |
| Vector → GPR (`FMOV`, no convert) | ~3–4 cycles |
| Load-to-load pointer chase (result in **base**) | ~3 cycles |
| SME core ↔ engine transit | **~20 cycles each way**, plus nonspeculative trail |

DRAM is quoted in **nanoseconds**, not cycles. Convert with the core’s
clock if you need a cycle budget; do not write “95-cycle DRAM”.

---

## Integer vs ASIMD vs SSVE vs SME-grid

Pick the class that matches the **machine** you intend to run on:

- **Integer ALU** — GPR work, CSEL, addressing. Cheap, many units.
- **ASIMD / FP on the core** — 128-bit `V` registers, multiple issue ports
  on P/S. Default for vector kernels that are not ZA tiles.
- **SSVE on the engine** — 512-bit `Z`, typically **one** issue. Supporting
  role (rec 17, rec 51).
- **SME grid** — ZA tiles / outer products. Highest flop bandwidth. One
  grid μop per cycle; hide ~4-cycle latency with the whole array.
- **ZA extract** — parallel path off the grid; fuse with convert when
  Guide 6.8 says so.

If you “vectorized” by moving a scalar FP loop into SSVE without ZA, look
up **both** the core FP class and the SSVE class. The wider register often
loses on issue width (`sme-ssve.md`).

---

## What not to do

- Do not copy Appendix A tables into a skill, a comment block, or a blog
  answer. They change by family and by core type.
- Do not assume x86 latencies (`addss` vs `vfmadd`).
- Do not assume E-core bandwidth equals P-core bandwidth. Same ISA,
  different Availability tables.
- Do not optimize for an undocumented bypass. If the sequence is not the
  Guide 5.5.3 preferred idiom (`core-microarch.md` recs 30–32), open 5.5.3
  and Appendix A before being clever.
- Integer `MADD` is a MAC-class op on older P/E. On S cores and on P from
  M4 / A18 it **cracks** into MUL + ALU (still emit `MADD`; rec 26). Open
  Table A.1 for the chip; do not copy the row.
- `ISB` and `SB` **serialize instruction delivery**. Do not sprinkle them
  in kernels. Crypto DIT close is the documented exception
  (`integer-isa.md`).

---

## After looking the mnemonic up

Emit the kernel (`asimd.md` / `sme-ssve.md`), then verify with
`apple-silicon-profile` (CPU Bottlenecks + CPU Profiler). Useful-bandwidth
near cap at the wrong wall time is rec 27, not a missing unroll.
