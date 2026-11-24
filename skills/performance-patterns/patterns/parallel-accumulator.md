<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->

# Pattern: serial accumulator → parallel accumulator rewrite

## When to apply

Apply when a hot loop is **latency-bound** due to a sequential dependency chain
through a single accumulator. Each iteration must wait for the previous
iteration's result before it can begin, even though the underlying data is
independent.

### Source code signals

- A single floating-point variable is updated inside a loop: `sum += a[i] * b[i]`,
  `acc = fma(a[i], b[i], acc)`, `total += x[i]`, running max/min
- The reduction operation is the only loop-carried dependency
- Loop body is otherwise straight-line (no branches on the accumulator)

### Profiling signals

- Scalar `FADDS` / `FADDD` / `FMULS` / scalar `FMLA` / `FMADD` carries a
  disproportionately high sample count in CPU Profiler
- CPU Bottlenecks: **Processing** (dependency stall) or high **Useful**, with
  **low L1D miss** — not a memory stall
- Cycles per iteration ≥ FP latency of the op (look up Appendix A for the
  chip; **~4 cycles** is the FMLA / FADD rule of thumb) despite a short body

---

## Why this is slow

A single accumulator forces strictly sequential execution — each `+=` depends
on the previous result — so throughput is limited by FP add **latency**
(~4 cycles) instead of **throughput** (multiple independent FP ops per cycle
on S/P cores).

```c
float sum = 0.0f;
for (int i = 0; i < n; i++)
    sum += a[i] * b[i];   /* each += depends on previous sum */
```

Check platform libraries first: `vDSP_dotpr` / `cblas_sdot` may already be
the right fix. → `patterns/platform-libs-first.md`

### Associativity requirement

All fixes below reorder floating-point operations. This is mathematically valid
only when the reduction operation is **associative** — i.e., `(A op B) op C == A
op (B op C)`. Addition, multiplication, min, and max all satisfy this. Results
will differ from the serial version by floating-point rounding (ε), not by a
logical error. If exact reproducibility of FP results is required, these
optimizations cannot be applied.

---

## The fix: three levels of improvement

Apply the level appropriate for the context. Each level builds on the previous
and can be combined.

### Level 1 — Pure C: multiple accumulators (no SIMD required)

Unroll the loop across independent accumulator variables so the out-of-order
engine can overlap their dependency chains. No SIMD, no intrinsics.

```c
float s0 = 0, s1 = 0, s2 = 0, s3 = 0;
int i;
for (i = 0; i + 3 < n; i += 4) {
    s0 += a[i+0];
    s1 += a[i+1];
    s2 += a[i+2];
    s3 += a[i+3];
}
for (; i < n; i++) s0 += a[i];
float sum = (s0 + s1) + (s2 + s3);
```

Use this when SIMD is unavailable, forbidden by policy, or when Level 1 alone
is enough.

**Important**: when the function has a scalar fallback path (the `n % lanes`
remainder of a SIMD function), that path should *also* use Level 1
accumulators — not a fresh single-accumulator loop.

### Level 2 — ASIMD 128-bit, four vectors (recommended default)

Process 16 floats per iteration: four `float32x4_t` accumulators, fused
multiply-add, horizontal reduce **once** at the end with `vaddvq_f32`.
All M1 / A14+ chips have ASIMD.

```c
#include <arm_neon.h>

float dot_asimd(const float * restrict a, const float * restrict b, int n)
{
    float32x4_t s0 = vdupq_n_f32(0.0f);
    float32x4_t s1 = vdupq_n_f32(0.0f);
    float32x4_t s2 = vdupq_n_f32(0.0f);
    float32x4_t s3 = vdupq_n_f32(0.0f);
    int i = 0;
    for (; i + 16 <= n; i += 16) {
        s0 = vfmaq_f32(s0, vld1q_f32(a + i +  0), vld1q_f32(b + i +  0));
        s1 = vfmaq_f32(s1, vld1q_f32(a + i +  4), vld1q_f32(b + i +  4));
        s2 = vfmaq_f32(s2, vld1q_f32(a + i +  8), vld1q_f32(b + i +  8));
        s3 = vfmaq_f32(s3, vld1q_f32(a + i + 12), vld1q_f32(b + i + 12));
    }
    s0 = vaddq_f32(vaddq_f32(s0, s1), vaddq_f32(s2, s3));
    float result = vaddvq_f32(s0);
    for (; i < n; i++) result += a[i] * b[i];
    return result;
}
```

Load into V registers (`vld1q_f32`), not into GPR then `FMOV`. Keep live
V-regs at or under ~25 of 32. Horizontal `vaddvq` belongs in the epilogue,
not the inner loop (Guide 3.5.3).

### Level 3 — SME ZA / FMOPA for large GEMM and dots

Use the SME Processing Grid (`FMOPA`, ZA-tile MLA/DOT) when the algorithm
is a **matrix, outer-product, convolution, or huge tiled dot** and
`hw.optional.arm.FEAT_SME` is 1 (M4 / A18+). SSVE stages tiles; the grid
does the throughput work.

If SME is unavailable, **stay on Level 2** (four ASIMD vectors). SSVE-only
rarely beats core ASIMD ports. Do not treat SSVE Z registers as a
drop-in 16-wide packed replacement for a reduction.

Isolate core-touched and engine-touched data on separate aligned 4 KiB
regions; do not bounce a dependent value core ↔ engine every iteration.
Do not hardcode SVL. → `patterns/sme-engine-bounce.md`

Wire variants with `library/cpu-dispatch.md`. For the scalar→ASIMD→SME
promotion algorithm, read `patterns/simd-upconversion-impl.md`.

### Accumulator count guideline

A good rule of thumb is **FP latency ÷ throughput** accumulators to saturate
the pipeline. With FMLA latency ~4 cycles (Appendix A), **4 vector
accumulators** covers most loops; use more only while you still have V-regs
and the loop stays compute-bound.

---

## Verification

After applying the fix:

1. **Correctness** — run on the same input as the original and confirm outputs
   match within floating-point rounding (ε). Differences beyond ε indicate
   a logic error in the rewrite.
2. **Profiling** — rerun CPU Bottlenecks. Processing/dependency should drop;
   Useful work should move to packed `.4S` / `.2D` (or SME grid issue).
3. **Benchmark** — `clock_gettime_nsec_np(CLOCK_UPTIME_RAW)` around the kernel.
   Level 1 is typically several times faster than serial; Level 2 another
   multiple on compute-bound loops. Do not quote a universal × factor.

---

## Presenting this to the user

1. Show the current loop with `/* serial dependency */` on the accumulate line.
2. Explain the latency vs. throughput gap in one short paragraph. Point at
   Appendix A for the chip's FMLA latency rather than inventing a number.
3. **Always warn about floating-point associativity**, even when a quick
   verification shows zero diff on your test data. Use phrasing like:
   *"Reordering FP additions requires associativity. Results may differ from
   the serial version by floating-point rounding (ε), not a logic error. If
   bit-exact reproducibility is required, these optimizations cannot be
   applied."*
4. Add `restrict` to pointer parameters when rewriting. See
   `patterns/missing-restrict.md` (C only; use `__restrict__` for C++).
5. Pick the level: Level 1 if SIMD is not worth it; Level 2 as the default
   on every M-series chip; Level 3 only for large GEMM/dot on SME, otherwise
   stay on four ASIMD vectors.
6. Mention `vDSP_dotpr` / Accelerate if the call is a plain dot or GEMM —
   custom ASIMD is not always the first fix.
7. For multi-variant binaries, dispatch via `library/cpu-dispatch.md`.
