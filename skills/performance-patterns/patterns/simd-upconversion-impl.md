<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->

# SIMD width-ladder — implementation guide

Demand-loaded from `patterns/simd-upconversion.md` once a promotion
opportunity is identified. This is the promotion algorithm: scalar →
ASIMD 128 (map each scalar op to a lane), then SME vs. more ASIMD ILP.
Dispatch: `library/cpu-dispatch.md`.

ISA catalogs and Appendix A latencies: `apple-silicon-codegen`.

---

## Table of contents

1. [Width ladder](#width-ladder)
2. [Stage 1: scalar → ASIMD 128](#stage-1-scalar--asimd-128)
3. [Scalar tail](#scalar-tail)
4. [Stage 2: more ASIMD ILP vs. SSVE vs. SME](#stage-2-more-asimd-ilp-vs-ssve-vs-sme)
5. [Stage 3: SME ZA](#stage-3-sme-za)
6. [C templates](#c-templates)
7. [Post-transformation checklist](#post-transformation-checklist)

---

## Width ladder

1. **Scalar C** with 4 independent accumulators.
2. **ASIMD 128** (`arm_neon.h`, `float32x4_t` / `float64x2_t` / `int32x4_t` /
   `uint8x16_t`). ≤~25 live of 32 V-regs. FMLA. Loads into V, not X.
3. **SSVE 512** (VL-agnostic predicates; SVL is 512-bit on Apple silicon —
   **do not hardcode**). Supporting role only.
4. **SME ZA** outer-product / MLA / DOT for matmul, matvec, conv, huge
   tiled dots. Grid is the throughput path.

If SME is unavailable, stop at step 2 with four ASIMD vectors. SSVE-only
rarely beats those ports.

---

## Stage 1: scalar → ASIMD 128

Apply when the loop is scalar (or auto-vectorized poorly) and the math is
elementwise or a reduction — not yet a giant GEMM.

### Step 1 — Inventory

List every load, compute, and store in the inner loop. Mark the reduction
variable if any. If there is a serial accumulator, apply Level 1 of
`patterns/parallel-accumulator.md` first (four scalar acc), then continue.

### Step 2 — Choose lane type

| Element | Vector type | Lanes | Load / store |
|---------|-------------|-------|--------------|
| `float` | `float32x4_t` | 4 | `vld1q_f32` / `vst1q_f32` |
| `double` | `float64x2_t` | 2 | `vld1q_f64` / `vst1q_f64` |
| `int32_t` | `int32x4_t` | 4 | `vld1q_s32` / `vst1q_s32` |
| `uint8_t` | `uint8x16_t` | 16 | `vld1q_u8` / `vst1q_u8` |

### Step 3 — Map each scalar op to a lane

| Scalar | ASIMD (`f32`) |
|--------|----------------|
| `x + y` | `vaddq_f32(x, y)` |
| `x - y` | `vsubq_f32` |
| `x * y` | `vmulq_f32` |
| `s + x * y` | `vfmaq_f32(s, x, y)`  ← fused; prefer this |
| `fmin` / `fmax` | `vminq_f32` / `vmaxq_f32` |
| broadcast scalar | `vdupq_n_f32` |
| load 4 | `vld1q_f32(p)` |
| store 4 | `vst1q_f32(p, v)` |
| horizontal add (epilogue only) | `vaddvq_f32` |
| horizontal max (tests / epilogue) | `vmaxvq_f32` |

Integer: `vaddq_s32`, `vmulq_s32`, `vmlaq_s32`. Signed vs unsigned:
`vmovl_s8` vs `vmovl_u8`, `vcgtq_s32` vs `vcgtq_u32`. Getting this wrong
is a silent correctness bug — inspect before consolidating.

Do **not** load into a GPR and `FMOV`/`SCVTF` into V. `vld1q` lands in V.
→ `patterns/gpr-vector-moves.md`

### Step 4 — Double the work per iteration

Change the stride from 1 to `lanes` (4 for `float`). One vector iteration
now does what four scalar iterations did. If the original already used
four scalar accumulators `s0…s3`, those become four `float32x4_t`
accumulators (16 floats in flight).

```
scalar:     s0 += a[i]; s1 += a[i+1]; s2 += a[i+2]; s3 += a[i+3];  i += 4
ASIMD:      S0 = vaddq_f32(S0, vld1q_f32(a+i));
            S1 = vaddq_f32(S1, vld1q_f32(a+i+4));
            S2 = vaddq_f32(S2, vld1q_f32(a+i+8));
            S3 = vaddq_f32(S3, vld1q_f32(a+i+12));                 i += 16
```

Each scalar add maps to one lane of a vector add; four independent vectors
hide FMLA latency (~4 cycles, Appendix A).

### Step 5 — Fuse mul+add

If a `vmulq` result feeds a `vaddq` into an accumulator, emit `vfmaq_f32`
instead. One issue, one rounding. → `patterns/fused-mac.md`

### Step 6 — Horizontal reduce once

After the loop, pairwise-add the four vectors, then `vaddvq_f32` (or
`vmaxvq_*` for min/max). Horizontal ops in the *inner* loop are an
anti-pattern.

### Step 7 — Restrict, const, live-reg budget

`const T * restrict` inputs, `T * restrict` output. Count live V-regs;
spill starts well before 32 — keep ~25. If you are over, drop an
accumulator rather than thrash the stack.

### Abort conditions

Abandon and explain if:

- The loop-carried dependency is not a reduction (pointer-chasing).
- The body has unvectorizable side effects (unpredictable stores).
- Lane-crossing shuffles would have to change semantics (document and stop).

---

## Scalar tail

After the vector loop, `n % lanes` elements remain. **Do not widen the
tail.** Process it with Level 1 scalar 4-acc (or a 1-wide loop if fewer
than 4 remain).

If the original promised `n` is a multiple of 4, the new code must promise
a multiple of 16 (four vectors × 4 lanes) **or** grow a tail. Silent
out-of-bounds otherwise.

SSVE predicates can swallow the tail (`svwhilelt` / `svld1`). That is a
Stage 2/3 tool, not a reason to skip the ASIMD tail.

---

## Stage 2: more ASIMD ILP vs. SSVE vs. SME

After Stage 1 runs, decide whether to stop.

```
Is the loop bandwidth-bound (Critical L1D miss while executing)?
    → stop. Block to L1D / Shared LL (sysctl sizes). Do not "widen".

Is the algorithm a matrix, outer-product, convolution, or huge tiled
dot / matvec — and apple_feat("FEAT_SME")?
    → Stage 3. SSVE stages tiles into ZA; the grid does FMOPA / MLA / DOT.

Otherwise:
    → stay on core ASIMD with 4 (sometimes 8) independent vectors.
      SSVE-only rarely beats those issue ports.
```

More ASIMD ILP means **more independent 128-bit accumulators**, not a
256-bit register. Two `float32x4_t` FMLAs in flight already match a
theoretical 256-bit add; four hide latency.

SSVE (streaming SVE) on Apple silicon: SVL = 512 bits, but write
VL-agnostic predicates (`svcntw()`, `svwhilelt_b32`). Do not `#define SVL 512`.
Use SSVE to feed ZA or to predicate a tail — not as a private 512-bit
core vector unit.

---

## Stage 3: SME ZA

Only when Stage 2 selected the grid.

Rules (Guide ch. 4 and 6):

- Target **ZA + Processing Grid**. SSVE supports; it is not the throughput
  path.
- **Do not bounce** dependent data core ↔ engine. `SMSTART` once, fill ZA,
  extract once, `SMSTOP`. Engine trails the core by dozens to thousands of
  cycles. → `patterns/sme-engine-bounce.md`
- Isolate core-touched and engine-touched buffers on **separate aligned
  4 KiB regions**.
- Cover ~4-cycle grid latency with whole-ZA work.
- Optimize for **SME-8**; SME-2 runs the same kernel slower.
- Do not hardcode SVL. Query or use `svcnt*`.
- Compile the variant with `__attribute__((target("sme2")))` (or
  `"sme"`) and `-mcpu=apple-m4` / `apple-m5` for that translation unit.
  Guard with `apple_feat("FEAT_SME")`.
- Rosetta (`sysctl.proc_translated`): never enter Stage 3.

A full SME kernel (ZA tiles, `FMOPA`, predicates) is emitted with
`apple-silicon-codegen`. This file only decides *when* to go there.

---

## C templates

### Elementwise map (scale)

```c
#include <arm_neon.h>

void scale_f32(float * restrict dst, const float * restrict src,
               float s, int n)
{
    const float32x4_t vs = vdupq_n_f32(s);
    int i = 0;
    for (; i + 16 <= n; i += 16) {
        vst1q_f32(dst + i +  0, vmulq_f32(vld1q_f32(src + i +  0), vs));
        vst1q_f32(dst + i +  4, vmulq_f32(vld1q_f32(src + i +  4), vs));
        vst1q_f32(dst + i +  8, vmulq_f32(vld1q_f32(src + i +  8), vs));
        vst1q_f32(dst + i + 12, vmulq_f32(vld1q_f32(src + i + 12), vs));
    }
    for (; i < n; i++) dst[i] = src[i] * s;
}
```

### Reduction (dot) — four ASIMD accumulators

See `patterns/parallel-accumulator.md` Level 2 (`vfmaq_f32` × 4,
`vaddvq_f32` epilogue). That is the Stage 1 reduction template.

### Dispatch skeleton

```c
#include "cpu_dispatch.h"   /* apple_feat(), from library/cpu-dispatch.md */

typedef float (*dot_fn)(const float * restrict, const float * restrict, int);

static dot_fn resolve_dot(void)
{
    if (apple_is_translated())
        return dot_scalar_4acc;
    if (apple_feat("FEAT_SME"))
        return dot_sme;          /* huge N / tiled; else fall through */
    return dot_asimd;            /* ASIMD is baseline on every M1+ */
}

float dot(const float * restrict a, const float * restrict b, int n)
{
    static dot_fn fn;
    if (!fn) fn = resolve_dot();
    return fn(a, b, n);
}
```

Do not dispatch on "Armv8.6 vs Armv9". Dispatch on `FEAT_*`.

---

## Post-transformation checklist

1. **`restrict` / `const`** on pointer parameters.
2. **Scalar tail** uses 4-acc, not a fresh serial sum.
3. **FMLA** wherever mul+add fed an accumulator.
4. **No GPR↔vector** in the inner loop (`vld1q` / `vst1q`, not
   `LDR`+`FMOV`).
5. **Live V-regs ≤ ~25**.
6. **Horizontal reduce in the epilogue only**.
7. **`apple_feat("FEAT_*")` + `target(...)`** on every non-baseline variant.
8. **Rosetta** takes scalar/ASIMD, never SME.
9. **SVL not hardcoded**.
10. **Associativity warning** at the function boundary for FP reductions.
11. **Platform library** considered (`vDSP_dotpr`, `cblas_sgemm`) and
    rejected with a reason, or used instead of this kernel.
