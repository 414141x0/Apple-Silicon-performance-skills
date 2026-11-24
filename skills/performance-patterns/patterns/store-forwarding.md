<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->
# Pattern: hard-to-predict store-to-load forwarding

## When to apply

Apply when a hot path **writes memory and quickly reads it back**,
especially as **many small stores feeding one wide load** (recs
44–46). The core will try to forward from the store queue. When that
dependence is sporadic or the bytes come from several stores, the
predictor either:

- misses (pipeline flush — a memory-order violation), or
- hits a phantom dependence (the load waits for a store it did not
  need).

Either way the cheap path — keep the value in a **register** — was
available and unused.

### Source code signals

- Byte/halfword scatter into a buffer, then a 16-byte vector load of
  the same buffer (`buf[i] = …` in a 16-iteration loop, then
  `vld1q_u8(buf)`)
- Read-modify-write through memory in a tight loop:
  `hist[x[i]]++`, `slot->count += k` when `slot` aliases across
  iterations
- Building a `float`/`uint32_t` in memory from four `uint8_t` stores
  then loading the word
- A small local array used as a shuffle/scratch that never had to
  leave registers
- `STP`/`LDP` pairs on a **non-SP** base that immediately forward to
  each other in a tiny loop (rec 46: prefer single-register
  `STR`/`LDR` in that specific case)

### Profiling signals

- CPU Bottlenecks **Discarded** with memory-order violations (younger
  load replayed after an older store)
- CPU Profiler: `STRB`/`STRH` cluster immediately followed by `LDR`
  `Q` / `LD1` of the same address
- High density of stores that the next instruction’s load depends on,
  with poor Useful-bandwidth despite an L1-resident working set
- The same address is stored then loaded inside a few tens of
  instructions, but only on some iterations (the predictor thrashes)

---

## Why this is slow

Store-to-load forwarding (STLF) is a guess until both addresses are
known. Hard-to-predict pairs:

1. **Flush** when the load ran too early (worst).
2. **Stall** when the predictor conservatively ties a load to a store
   that does not actually overlap (poor).
3. **Many-to-one** — several `STRB`s feeding one 16 B load — the
   predictor cannot cheaply name which stores own which bytes, so it
   waits until addresses drain.

Forwarding is also slower than staying in a register. Rec 44 is High /
Medium: restructure so the read either happens much later (data
already in cache, dependence gone) or never happens (the producer
kept the register).

Named shapes that train the predictor wrong (rec 44):
**histograms**; **in-place FFT** (tiny radices train a conservative
predict); **pointer lists into a small object pool**; **hot stack top**
push/pop; growing structures that were dependent while small. Split
small-n vs large-n clones so early training does not poison the long
loop. Do not replace the collision with a per-iter `if`.

Rec 46 is the opposite corner (Low / Low): if reconstructing the
value in registers would take a thicket of shifts/merges, *use* the
forwarding network — but then match sizes and keep addressing simple.

If the accesses are also unaligned, fix that first or prefer unaligned
**loads** over unaligned stores (`unaligned-access.md`, rec 43).

---

## The fix

### 1. Do not assemble a wide value from many small stores

**Before:**

```c
#include <arm_neon.h>
#include <stdint.h>

uint8x16_t pack16(const uint8_t *src)
{
    uint8_t tmp[16];
    for (int i = 0; i < 16; i++)
        tmp[i] = src[i];          /* 16× STRB */
    return vld1q_u8(tmp);         /* 1× 16 B load — many-to-one STLF */
}
```

**After — matching width, or never leave registers:**

```c
uint8x16_t pack16(const uint8_t *src)
{
    return vld1q_u8(src);         /* one load; no store */
}
```

If `src` is scattered, gather into a vector with inserts / `BSL`, not
through a stack buffer.

### 2. Keep the RMW value in a register

**Before:**

```c
void scale_slot(float *slot, float k, int n)
{
    for (int i = 0; i < n; i++)
        slot[0] += k;             /* store then load next iter */
}
```

**After:**

```c
void scale_slot(float *slot, float k, int n)
{
    float acc = slot[0];
    for (int i = 0; i < n; i++)
        acc += k;                 /* register communication */
    slot[0] = acc;
}
```

Same pattern for histograms: when one bucket is hot, accumulate
locally and write back once (`per-cpu-stats.md` for the concurrent
case).

### 3. Match store and load sizes

```c
/* Before: four byte stores, one word load */
uint8_t b0, b1, b2, b3;
dst[0] = b0; dst[1] = b1; dst[2] = b2; dst[3] = b3;
uint32_t w = *(uint32_t *)dst;     /* STLF from four stores */

/* After: one word in a register */
uint32_t w = (uint32_t)b0
           | ((uint32_t)b1 << 8)
           | ((uint32_t)b2 << 16)
           | ((uint32_t)b3 << 24);
*(uint32_t *)dst = w;              /* store once, at the width you need */
```

If you *must* store small and load wide, insert enough independent
work that the stores drain to L1 before the load — or keep a second
copy of the loop body for the “small n” case so the predictor is not
trained on mixed dependent/independent iterations (rec 44).

### 4. When register communication is worse, use STLF deliberately

Rec 46: messy gathers that would need five `EXTR`/`BSL`s can stay in
memory. Then:

- Use **one** 4 B or 8 B integer store/load pair with simple
  base+offset addressing (the fast STLF case).
- Prefer `STR`/`LDR` over a non-stack `STP`/`LDP` pair that immediately
  forwards in a tiny loop.
- Avoid page- and line-spanning loads (`unaligned-access.md`).

---

## Verification

1. **Disassembly** — the hotspot no longer has `STRB`×N + `LDR Q`.
   Matching `STR`/`LDR` widths, or no load at all.
2. **CPU Bottlenecks** — Discarded memory-order violations drop.
   Processing should not grow by a forest of shift/merge insns; if it
   does, you overshot rec 46 and should go back to forwarding.
3. **Correctness** — endian/layout of packed words matches the old
   byte stores.

---

## Presenting this to the user

1. Show the small-store / wide-load pair marked
   `/* ◄ many-to-one STLF */`.
2. One paragraph: hard-to-predict forwarding flushes or stalls; a
   register or a matching-width access deletes the dependence.
3. Show the register / `vld1q` rewrite. Mention
   `unaligned-access.md` if the addresses are also unaligned —
   unaligned **loads** beat unaligned stores if only one side can be
   aligned.
4. Do not “fix” STLF by inserting `DMB`. That is
   `weakly-ordered-memory.md` and will make this slower.
