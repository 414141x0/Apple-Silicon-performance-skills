<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->
# Pattern: taken-branch density and unpredictable CSEL

## When to apply

Apply in two related situations (Guide recs 23–24):

1. **Taken-branch density** is high — a taken branch (loop-back, call, or
   biased-the-wrong-way conditional) about **once every 8 instructions**.
   Fetch delivers a truncated group after a predicted-taken branch, so
   Decode starves.
2. A **data-dependent conditional** in a hot loop is poorly predicted, and
   both sides are cheap (a move, an add, a clamp). A `CSEL` / `CSET` /
   `CSINC` / `CNEG` / `FCSEL` is cheaper than a mispredicted `B.cond`.

They often co-occur: a short loop with an inner `if` is both dense in taken
loop-backs *and* a mispredict factory.

### Source code signals

- Short loop bodies (a handful of statements) with a high trip count
- `if` / `?:` on noisy data: sign tests, clamps, ReLU, `abs`,
  `if (x > 0) s += x`, min/max that is not `FMAX`/`FMIN`
- Error / rare paths inlined into the hot function so the common case is a
  **taken** branch rather than fall-through
- Tiny callees invoked every iteration (a taken `BL` + taken `RET`)
- Counted loops the compiler did not unroll

### Profiling signals

- CPU Bottlenecks **Delivery** bandwidth bound, with high taken-branch
  density (retired taken branches approaching ~1/8 of retired instructions)
- CPU Bottlenecks **Discarded** bound, dominated by conditional-branch
  mispredicts (not memory-order replays)
- CPU Profiler: a tight block ending in `B.LT` / `B.EQ` / `B.GT` whose
  target is the next iteration or a two-instruction then-clause
- `gccbranchprob.py` / PGO report on the `if` is near 50/50 — unpredictable
  — or the cold path is actually the fall-through

---

## Why this is slow

The fetch unit reads a group of instructions, including bytes past a
predicted-taken branch, then **throws those extra bytes away**. When taken
branches are dense, Decode is fed leftovers. Danger starts near **one taken
branch in eight instructions** (Guide 5.4.4). Short loops hit this first:
every iteration’s loop-back is a taken branch.

Separately, a mispredicted conditional flushes work that already executed
and loses the chance to have the correct path in flight. If both sides are
a handful of independent ALU/FP ops, executing **both** and picking with
`CSEL` is cheaper than gambling on the predictor (Guide 5.4.5).

Keep a real branch when:

- The condition is strongly biased (then straighten so the common path
  **falls through** — see `cold-path-annotation.md`)
- The unused side is expensive (divide, cache-missing load, long FP chain)

M5 / A19 `FEAT_HBC` `BC.<cond>` is a “likely taken” hint. Apple
predictors **ignore it** — treat `BC.cond` as `B.cond`. Do not emit `BC`
expecting a better predictor (codegen rec 24).

---

## The fix

### 1. Unroll, straighten, inline (taken-branch density)

- **Unroll** counted loops so most iterations fall through instead of
  taking the loop-back. A remainder loop handles the tail.
- **Straighten** the hot path: invert the condition so the common case is
  fall-through. Mark rare callees `[[gnu::cold]]` /
  `__attribute__((cold))` — `patterns/cold-path-annotation.md`. In C++20,
  `[[likely]]` / `[[unlikely]]`; in C, `__builtin_expect`.
- **Inline** tiny hot callees to delete a taken `BL` and a taken `RET`.

Do **not** NOP-align branch targets (Guide 5.4.3). That grows I-cache
footprint and does not help these cores.

### 2. `CSEL` / `CSET` / `CSINC` over unpredictable branches

**Absolute value via `CSEL`:**

```c
/* Before — unpredictable sign test */
int abs_i(int x)
{
    if (x < 0)
        return -x;
    return x;
}

/* After — both values exist; CSEL picks. Compiler: CMP + CSEL/CNEG. */
int abs_i(int x)
{
    int neg = -x;
    return x < 0 ? neg : x;
}
```

Expected asm:

```text
NEG   W1, W0
CMP   W0, #0
CSEL  W0, W1, W0, LT    /* or CNEG W0, W0, MI */
RET
```

**Clamp via `CSEL`:**

```c
/* Before */
int clamp_i(int x, int lo, int hi)
{
    if (x < lo) return lo;
    if (x > hi) return hi;
    return x;
}

/* After */
int clamp_i(int x, int lo, int hi)
{
    x = x < lo ? lo : x;
    x = x > hi ? hi : x;
    return x;
}
```

**Positive-only accumulate — `if (x > 0) s += x`:**

```c
/* Before — a taken or mispredicted branch every element */
int sum_positive(const int *x, int n)
{
    int s = 0;
    for (int i = 0; i < n; i++) {
        if (x[i] > 0)
            s += x[i];
    }
    return s;
}

/* After — CSEL (or bitwise mask) deletes the branch */
int sum_positive(const int *x, int n)
{
    int s = 0;
    for (int i = 0; i < n; i++) {
        int v = x[i];
        int add = v > 0 ? v : 0;   /* CMP + CSEL */
        s += add;
    }
    return s;
}
```

Bitwise equivalent when two’s-complement arithmetic is acceptable:

```c
        /* mask is 0 or 0xFFFFFFFF; does not mispredict */
        int mask = -(v > 0);
        s += v & mask;
```

Unroll the loop as well so the loop-back is not itself a density problem.

**Boolean via `CSET`:**

```c
int in_band(int y)
{
    return (unsigned)(y - 5) < 5u;   /* SUB + CMP + CSET; no branch */
}
```

### 3. When *not* to if-convert

Leave a branch (and annotate it) when the unused side would:

- Touch memory that might fault or miss (speculative `STR`/`LDR` on the
  false path)
- Run a long dependent chain (FP divide, polynomial, table lookup)

Those belong in `cold-path-annotation.md` plus PGO, not in a `CSEL`.

---

## Verification

1. **CPU Bottlenecks** — Delivery should recede once taken-branch density
   drops below the ~1-in-8 danger zone; Discarded should recede once the
   noisy `if` is a `CSEL`. Record with the CPU Bottlenecks template
   (Instruments 26+) on the same workload before and after.
2. **Disassembly** — hotspot shows `CSEL` / `CSET` / `CSINC` / `CNEG` /
   `FCSEL`, not `B.cond` around a two-instruction body. Loop-back `B`
   appears once per unrolled body, not once per element.
3. **Branch probability** — for *cold* branches you intend to keep, confirm
   they are actually cold before annotating:

   ```bash
   # Clang/GCC static estimates (no run needed)
   clang -O3 -fdump-tree-profile_estimate-lineno -dumpdir dump/ -c foo.c
   python3 skills/apple-silicon-profile/tools/gccbranchprob.py foo.c dump/ func

   # Or PGO: -fprofile-instr-generate / -fprofile-instr-use
   ```

   Near 0% taken → `cold-path-annotation.md`. Near 50% → this pattern
   (`CSEL`). Strongly biased but currently *taken* → invert so it falls
   through.

---

## Presenting this to the user

1. Quote the `if` / short loop and mark it `/* ◄ taken or noisy branch */`.
2. Name which half of the pattern it is: fetch density, mispredicts, or
   both. Point at Delivery vs Discarded in CPU Bottlenecks.
3. Show the `CSEL` (or unroll/straighten) rewrite next to the original.
4. State the rule of thumb: **unpredictable + cheap both sides → CSEL;
   rare + expensive → cold branch that falls through on the hot path.**
5. Do not NOP-pad labels. Do not if-convert a path that loads or divides.
