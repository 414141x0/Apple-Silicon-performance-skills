<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->
# Pattern: GPR ↔ vector register bounce

## When to apply

Apply when a hot path **moves the same value between the general-purpose
register file (X/W) and the vector/FP register file (V/Q/S/D)** just to feed
the next operation. The move is not the algorithm — it is a tax on the load
issue slots and a 4–5 cycle MV2VEC delay (recs 28–29;
`apple-silicon-codegen/references/latency-lookup.md` has the current
chip’s numbers).

### Source code signals

- An integer load immediately followed by a SIMD/FP convert or SIMD use:
  `int x = *p; float f = (float)x;` compiled as `LDR W0` + `SCVTF …, W0`
- `vdup_n_*`, `vset_lane_*`, `vget_lane_*`, `vmov_n_*` inside a tight loop
  whose inputs already live in memory that could have been loaded into V/S/D
- Inline asm or intrinsics that `UMOV`/`SMOV` a lane to a GPR, do a short
  integer op, then `INS`/`FMOV` it back
- A scalar integer helper (`scale`, `bias`, `<< k`) sitting between a vector
  load and a vector convert, forcing a round trip
- Store from a vector register, then integer-load the same bytes (memory
  bounce — worse than a register move)

### Profiling signals

- CPU Profiler / `llvm-objdump -d` on the hotspot shows `FMOV` (general),
  `DUP` (general), `INS` (general), `UMOV`, `SMOV`, or `SCVTF`/`UCVTF` with a
  **W/X source** rather than an S/D source
- CPU Bottlenecks: **Processing** with load-unit pressure; MV2VEC consumes a
  load issue slot, so genuine `LDR`/`LDP` traffic stalls behind the moves
- The convert/mul/add that *uses* the value dominates cycles, but the
  producer is a GPR→vector move rather than a vector load
- Useful-bandwidth is not the story — the extra 4–5 cycles sit on a
  dependent chain (low ILP, not a cache miss)

---

## Why this is slow

Apple silicon keeps integer ALUs and the Advanced SIMD / FP unit on different
register files. A GPR→vector transfer (MV2VEC) is not a free rename: it

1. **Steals a load issue slot** — the same ports that should be doing `LDR`.
2. **Costs ~4–5 cycles** for the movement itself, plus any convert. See
   recs 28–29 and `apple-silicon-codegen/references/latency-lookup.md`
   for the exact latency/bandwidth of the chip you are on; do not bake
   a table into the patch.

```text
/* Typical compiler output for:  float f = (float)(*p); */
LDR   W0, [X1]       /*  integer load into a GPR          */
SCVTF D1, W0         /*  MV2VEC (~4–5 cyc) + convert      */
```

Loading the same bytes straight into the vector file skips the move:

```text
LDR   S0, [X1]       /*  bits land in a vector register   */
SCVTF D1, S0         /*  convert only — no MV2VEC         */
```

`S0` is an FP name; it can still hold an integer bit-pattern. Vector→GPR is
cheaper than GPR→vector, but still not free (~3–4 cycles). `UMOV`/`SMOV`
also sign- or zero-extend; **`FMOV` is the preferred vec→GPR copy when no
extend is required**, even if the bits are an integer.

A store from V followed by an integer load of the same address is slower
than any of the register moves — never “fix” a GPR↔vector bounce by going
through memory.

---

## The fix

Keep each value in the file that will consume it. If the next user is SIMD
or FP, **load into V/S/D**. If the next user is integer-only, load into X/W
and stay there.

### 1. Load integers destined for SIMD/FP directly into V/S/D

**Before** (C that compiles to GPR load + convert):

```c
float i32_to_f64_sum(const int32_t *p, int n)
{
    double s = 0.0;
    for (int i = 0; i < n; i++) {
        int32_t x = p[i];          /* LDR W  */
        s += (double)x;            /* SCVTF from W — MV2VEC */
    }
    return (float)s;
}
```

**After** (load into the vector file; convert in place):

```c
#include <arm_neon.h>

float i32_to_f64_sum(const int32_t *p, int n)
{
    float64x2_t v0 = vdupq_n_f64(0.0);
    float64x2_t v1 = vdupq_n_f64(0.0);
    int i = 0;
    for (; i + 3 < n; i += 4) {
        int32x4_t xi = vld1q_s32(p + i);          /* load into V, not X */
        float64x2_t lo = vcvt_f64_s32(vget_low_s32(xi));
        float64x2_t hi = vcvt_f64_s32(vget_high_s32(xi));
        v0 = vaddq_f64(v0, lo);
        v1 = vaddq_f64(v1, hi);
    }
    double s = vgetq_lane_f64(v0, 0) + vgetq_lane_f64(v0, 1)
             + vgetq_lane_f64(v1, 0) + vgetq_lane_f64(v1, 1);
    for (; i < n; i++)
        s += (double)p[i];                 /* inspect asm: want LDR S + SCVTF D, S */
    return (float)s;
}
```

The scalar tail still must not `LDR W` + `SCVTF D, W`. Inspect the asm.
The desired sequence is `LDR S0, [Xn]` / `SCVTF D1, S0`, never
`LDR W0` / `SCVTF D1, W0`. If the compiler loads into W, keep the
value in a `float32x2_t` / `vld1_s32` even for one lane.

### 2. Prefer `FMOV` for vector → GPR (no extend)

**Before:**

```c
uint32_t lane0(uint32x4_t v)
{
    return vgetq_lane_u32(v, 0);   /* often UMOV W0, V0.S[0] */
}
```

**After** (when the value is already 32/64-bit and needs no extend):

```c
uint32_t lane0(uint32x4_t v)
{
    uint32_t x;
    __asm__("fmov %w0, %s1" : "=r"(x) : "w"(v));
    return x;
}
```

Leave `UMOV`/`SMOV` in place when you *need* the extend (8- or 16-bit lanes).
Do not add an extra `UXTB`/`SXTH` after `FMOV` — that throws away the win.

### 3. Short integer work can stay in SIMD (waste lanes)

If a loaded integer needs a shift, add, or mask **and then** a convert / SIMD
use, do that op with an integer-typed SIMD instruction on element 0 and
ignore the other lanes. A 1–2 instruction integer detour in the SIMD unit
beats a GPR round trip. A long integer chain does **not**: SIMD integer
latencies are typically higher than the scalar ALU, so after a handful of
ops the move would have been cheaper.

```c
/* Need (p[i] + bias) converted to float, then fused into a vector acc. */
int32x4_t xi = vld1q_s32(p + i);
xi = vaddq_s32(xi, vdupq_n_s32(bias));   /* stay in V; other lanes unused is OK */
float32x4_t xf = vcvtq_f32_s32(xi);
acc = vfmaq_f32(acc, xf, w);
```

### 4. Do not bounce

Once a value is in V, keep it in V until the algorithm is done with it.
Once it is in X, keep it in X. Crossing twice in one loop iteration is the
anti-pattern this file exists to kill.

---

## Verification

1. **Disassembly** — `clang -O3 -mcpu=apple-m4 -S` or `llvm-objdump -d`.
   Confirm the hotspot has `LDR S`/`LDR D`/`LD1` feeding `SCVTF`/`UCVTF`/
   `FMLA`, not `LDR W`/`LDR X` feeding those. Vec→GPR copies should be
   `FMOV`, not `UMOV`, unless an extend is required.
2. **CPU Profiler** — the MV2VEC-class instructions drop out of the hot
   annotated region; load-unit samples shift toward real `LDR`/`LDP`.
3. **`apple-silicon-codegen/references/latency-lookup.md` (recs 28–29)** —
   if you need a number for a write-up, look it up there. Do not paste
   class latencies into the patch or the commit message.
4. **Correctness** — bit patterns that were sign-extended by `SMOV` must
   still be extended (now explicitly) after switching to `FMOV`.

---

## Presenting this to the user

1. Show the C (or asm) with `/* ◄ GPR→vec MV2VEC */` on the offending line.
2. One paragraph: the move steals a load issue slot and costs ~4–5 cycles;
   numbers live in `apple-silicon-codegen/references/latency-lookup.md`.
3. Show the rewritten load-into-V (or `FMOV`) form next to the original.
4. Warn that a long integer chain should **not** be forced into SIMD — only
   the short prefix that would otherwise round-trip.
5. Ask them to rebuild with `-g` + dSYM and re-annotate; the smoking gun is
   the mnemonic (`SCVTF D1, S0` vs `SCVTF D1, W0`), not a cycle count you
   guessed.
