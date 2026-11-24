<!-- Structure inspired by Intel Performance Skills (MIT, 2026).
     Content is original agent instructions paraphrasing Apple Silicon CPU
     Optimization Guide 5.0. Do not copy Guide tables. MIT license. -->

# ASIMD / FP (Guide Ch. 3)

Owns recs **10–16**. GPR↔vector movement is recs 28–29 in
`core-microarch.md`; this file only states the write-time habit (load into V,
not X). Fused integer `MADD` is shown in `integer-isa.md` and points here for
the boxed rec.

There is **no 256-bit ASIMD**. Width options are:

- 128-bit `V0–V31` (`float32x4_t` and friends)
- two independent 128-bit vectors in ILP
- SSVE 512-bit `Z` in streaming mode (supporting role)
- SME `ZA` 4 KiB (throughput path for tiles)

Do not invent AVX2-style 256-bit NEON. Do not emit `vzeroupper` — it is an
x86 tax that does not exist here.

Do not dump Guide 3.3 mnemonic catalogs. Pick the instruction that matches
the rule, then look up its class in Appendix A (`latency-lookup.md`).

Platform libraries still win: rec 2 in `integer-isa.md`. This chapter is for
the kernel Accelerate cannot cover.

---

## Register file and types

32 × 128-bit vector registers `V0–V31`. Views: `Bn`/`Hn`/`Sn`/`Dn`/`Qn`.
SIMD layout is element-size × lanes equaling 64 or 128 bits (`V0.4S`,
`V0.8H`, `V0.16B`, `V0.2D`, …). A 64-bit view zeros the high half on write.

C types from `<arm_neon.h>`:

| C type | Meaning |
|--------|---------|
| `float32x4_t` | 4 × f32 in a Q register |
| `float64x2_t` | 2 × f64 |
| `int8x16_t` / `uint32x4_t` / … | integer lanes |
| `float32x4x2_t` | result of `vld2q_f32` (two Q regs) |

`<simd/simd.h>` (`simd_float4`, `simd_mul`, …) is the small-vector module;
it is not a substitute for a hot `vfmaq_f32` loop, and it is not 256-bit.

Compile the TU with `-mcpu=apple-mN` (`integer-isa.md`). Include
`<arm_neon.h>` for NEON ACLE.

---

## Rec 12 — Intrinsics for the hot kernel; compiler owns ABI/loops/RA (High/High)

Write the **math** as ACLE intrinsics. Leave to Clang:

- AAPCS64 / Apple ABI (X18, X29 frame record)
- pointer bump, trip count, tails
- register allocation and spill
- ordinary `ldr`/`str` around the kernel

Full assembly is for instructions ACLE cannot express, or for a verified
hot-path the compiler systematically mis-schedules. Even then, wrap it in C
with `restrict` and a scalar tail.

```c
#include <arm_neon.h>

// Compiler: loops, RA, ABI. You: FMLA + independent accumulators.
```

`-O3` on this file. `-ffast-math` / `-Ofast` let the compiler form `FMLA`
from `a += b * c` but they change rounding and Inf/NaN. Prefer explicit
`vfmaq_f32` so you get FMLA without silently flipping the TU’s FP contract.

---

## Rec 10 — LD2/3/4 and ST2/3/4 for interleaved structures (Medium/Low)

`LD2`/`LD3`/`LD4` deinterleave while loading; `ST2`/`ST3`/`ST4` reinterleave
while storing. Use them when you touch **more than a few** elements of an
AoS stream (RGB, RGBA, complex pairs, XYZ). For two or three scalars, a
plain `ldr` plus `ZIP`/`UZP` is cheaper than a multi-structure load.
`LD3`/`ST3` (and friends) are mostly **microcoded** (rec 26); they are
still the right op for a long interleaved stream. Re-layout to planar if
the interleave *is* the inner-loop tax.

```c
#include <arm_neon.h>

// planarize interleaved stereo: [L0 R0 L1 R1 ...] -> two float32x4_t
void deinterleave_stereo(const float *in, float *left, float *right, size_t n)
{
    size_t i = 0;
    for (; i + 8 <= n; i += 8) {          // 8 samples per side = 16 floats
        float32x4x2_t p0 = vld2q_f32(in + 2 * i);
        float32x4x2_t p1 = vld2q_f32(in + 2 * i + 8);
        vst1q_f32(left  + i,     p0.val[0]);
        vst1q_f32(right + i,     p0.val[1]);
        vst1q_f32(left  + i + 4, p1.val[0]);
        vst1q_f32(right + i + 4, p1.val[1]);
    }
    for (; i < n; ++i) {
        left[i]  = in[2 * i];
        right[i] = in[2 * i + 1];
    }
}
```

`LD2R`–`LD4R` broadcast a structure into all lanes. Do not open Guide 3.3.9
tables into the answer; the rule is: multi-structure load/store for long AoS,
scalar/ZIP for crumbs.

---

## Rec 11 — Math ops that include conversions (Medium/Medium)

If the arithmetic **widens**, use the widening op (`SADDL`/`UADDL`/`SMLAL`/
`SADDL2`, …), not `SXTL`/`UXTL` followed by `ADD`/`MLA`.

```c
#include <arm_neon.h>

// Sum adjacent int8 pairs into int16 — SADDL, not SXTL + ADD.
int16x8_t add_widen_s8(int8x16_t a, int8x16_t b)
{
    int16x8_t lo = vaddl_s8(vget_low_s8(a),  vget_low_s8(b));   // SADDL
    int16x8_t hi = vaddl_s8(vget_high_s8(a), vget_high_s8(b));  // SADDL2
    return vaddq_s16(lo, hi);
}
```

Same pattern: `SMULL` not `SXTL`+`MUL`; `SABAL` not `SABD`+`UXTL`+`ADD`.

Narrowing a DSP chain: `SHRN` truncates toward 0. Prefer **`RSHRN`**
(round-to-nearest, ties away from 0) when the ISA offers it — less bias
over many stages. Canonical integer pipeline: load narrow → lengthen as
you compute (`SADDL`/`SADDW`) → `RSHRN` → store.

---

## Rec 13 — Balance ILP vs 32 vector registers (~25 live) (Medium/Medium)

Unroll and interleave so the mapper sees independent FMLAs, but stop before
spills. **Keep about 25 live V-regs**; the other ~7 are for transients and
the compiler. Constants (broadcast weights, permute indices, zeros) count
as live.

If `objdump` / Instruments shows spill `str`/`ldr` of Q registers in the
inner loop, **cut the unroll**. Trial-and-error is expected (Guide 3.5.1).

Four accumulators × one vector of inputs is usually enough for FMLA latency
on the core; eight accumulators is the point to start counting live regs.
See `latency-lookup.md` for unroll ≈ latency/throughput.

---

## Rec 14 — Horizontal UMINV/UMAXV for any/all; not in the hot loop (Medium/Medium)

Any-lane / all-lane tests:

| Test | Sequence |
|------|----------|
| any lane zero | `UMINV` then compare with 0 |
| any lane nonzero | `UMAXV` then compare with 0 |
| all lanes zero | `UMAXV` == 0 |
| all lanes nonzero | `UMINV` != 0 |

ACLE:

```c
int any_zero_u8(uint8x16_t v)  { return vminvq_u8(v) == 0; }
int any_nonzero_u8(uint8x16_t v) { return vmaxvq_u8(v) != 0; }
```

Move the scalar with `FMOV` (not `UMOV`) when you need it in a GPR — rec 29
in `core-microarch.md`.

**Do not horizontal-reduce inside the hot loop.** Keep lane-wise accumulators
(`vfmaq_f32`) and call `vaddvq_f32` / `vminvq_*` **once** at the end. A
horizontal op every iteration is the ASIMD analogue of a serial scalar
accumulator.

There is no `FADDV`. `vaddvq_f32` is a `FADDP` tree. Dummy second-source
lanes of `FADDP` are ignored — feed **zeros** so denorms/NaNs do not leak
into the reduction.

---

## Rec 15 — Op-add / FMLA / MADD (High/Medium)

Use fused op-accumulate: `FMLA`/`FMLS`, integer `MLA`/`MLS`, `SDOT`/`UDOT`,
`SABA`, `SSRA`, `SMMLA` (I8MM, M2+), `BFDOT` (BF16, M2+). Benefits: fewer
instructions, shorter latency, and for FP **one less rounding** (more
accurate than `fmul`+`fadd`).

Clang will not form `FMLA` under IEEE rules without `-ffast-math`. Write
`vfmaq_f32` / `vfmsq_f32` yourself in the kernel.

Cover FMLA latency with **four independent accumulator chains**, then
reduce once (the kernel below). Through the **mul** inputs, FMLA latency
is about `FMUL`; through the **add** input it can be *longer* than a
standalone `FADD` — that is why a single fused accumulator still serializes.

GPR `MADD`: `integer-isa.md`. On S cores and on P from M4 / A18 it is
cracked (still emit it; rec 26).

### Canonical kernel: 4-accumulator `vfmaq_f32` dot product

```c
#include <arm_neon.h>
#include <stddef.h>

float dot_f32_neon(const float *restrict a,
                   const float *restrict b,
                   size_t n)
{
    float32x4_t acc0 = vdupq_n_f32(0.f);
    float32x4_t acc1 = vdupq_n_f32(0.f);
    float32x4_t acc2 = vdupq_n_f32(0.f);
    float32x4_t acc3 = vdupq_n_f32(0.f);

    size_t i = 0;
    for (; i + 16 <= n; i += 16) {
        acc0 = vfmaq_f32(acc0, vld1q_f32(a + i),      vld1q_f32(b + i));
        acc1 = vfmaq_f32(acc1, vld1q_f32(a + i + 4),  vld1q_f32(b + i + 4));
        acc2 = vfmaq_f32(acc2, vld1q_f32(a + i + 8),  vld1q_f32(b + i + 8));
        acc3 = vfmaq_f32(acc3, vld1q_f32(a + i + 12), vld1q_f32(b + i + 12));
    }

    float32x4_t acc = vaddq_f32(vaddq_f32(acc0, acc1), vaddq_f32(acc2, acc3));
    float sum = vaddvq_f32(acc);   // horizontal once, after the loop

    for (; i < n; ++i)
        sum += a[i] * b[i];
    return sum;
}
```

Notes:

- `restrict` on both inputs so Clang does not emit overlap checks.
- Loads go **straight into V**, never `ldr xN` + `fmov`.
- FP associativity: document at the API if callers need bit-exact serial sums.
- If `n` is huge and this is a BLAS-shaped problem, rec 2 (`vDSP_dotpr`)
  still wins.

---

## Rec 16 — EXT/REV/TRN/UZP/ZIP over TBL/TBX (Medium/Medium)

`EXT`, `REV`/`REV16`/`REV32`/`REV64`, `TRN1`/`TRN2`, `UZP1`/`UZP2`,
`ZIP1`/`ZIP2` cover almost every regular permute. Combine them. `TBL`/`TBX`
are the catch-all byte table and cost more (1 μop / cracked / microcoded
by table size, rec 26) — use them only when the pattern is irregular
(arbitrary gather of bytes), and then the **smallest table that works**.

```c
#include <arm_neon.h>

// 2×2 transpose of 64-bit lanes (two float64x2_t).
void transpose2x2_f64(float64x2_t r0, float64x2_t r1,
                      float64x2_t *o0, float64x2_t *o1)
{
    *o0 = vtrn1q_f64(r0, r1);
    *o1 = vtrn2q_f64(r0, r1);
}

// Deinterleave evens/odds: UZP, not TBL.
void split_even_odd_f32(float32x4_t a, float32x4_t b,
                        float32x4_t *even, float32x4_t *odd)
{
    *even = vuzp1q_f32(a, b);
    *odd  = vuzp2q_f32(a, b);
}
```

Do not paste Guide 3.5.7 shuffle tables into the answer. Open that section
when a non-obvious combination is required.

---

## BF16 converts (Guide 3.5.8, `FEAT_BF16`, M2+)

Accumulate is always **FP32**. Arithmetic that lengthens (`BFDOT`,
`BFMLALB`/`T`, `BFMMLA`) is preferred over an explicit convert plus
compute.

- **BF16 → FP32:** there is no convert insn. `SHLL` / `SHLL2` `#16`
  (high 16 bits of BF16 are FP32 sign/exp/top mantissa; low mantissa
  zeros).
- **FP32 → BF16:** **`BFCVTN` / `BFCVTN2`** (proper FP round), not
  `SHRN`. Scalar `BFCVT Hd, Sn` exists.

Query `hw.optional.arm.FEAT_BF16` (`sysctl.md`) before emitting these.

---

## Compare → all-1s/0s + BSL/BIT/BIF (no k-masks)

ASIMD compares write a **lane mask of all-1s or all-0s**, not an AVX-512
k-register. Blend with `BSL`/`BIT`/`BIF` (`vbslq_*`).

```c
#include <arm_neon.h>

float32x4_t blend_if_gt(float32x4_t pred, float32x4_t a, float32x4_t b)
{
    uint32x4_t m = vcgtq_f32(pred, vdupq_n_f32(0.f));
    return vbslq_f32(m, a, b);
}
```

Do not try to materialize a scalar bitmask each iteration. Keep the mask in
a V register. SSVE predicates (`P`/`PN`) are the SME-mode analogue —
`sme-ssve.md`.

---

## Load into V, not X then FMOV

```asm
    // no
    ldr     w0, [x1]
    fmov    s0, w0

    // yes — even if the bits are integer
    ldr     s0, [x1]
```

Moving GPR → vector is `MV2VEC`: it **steals a load issue slot** and costs
~4–5 cycles (recs 28–29, `core-microarch.md`). Short integer sequences that
will feed SIMD should stay in V, wasting lanes 1–3 if needed.

---

## After the kernel

Unroll factor: Appendix A class latency / throughput (`latency-lookup.md`).
Verify: `apple-silicon-profile` (CPU Bottlenecks + CPU Profiler). Named
anti-pattern still present: `performance-patterns`.
