<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->

# Pattern: narrow SIMD → width-ladder upconversion

## When to apply

Apply when a hot loop uses scalar floating-point, or uses ASIMD 128-bit when
the algorithm belongs on the SME Processing Grid. Apple silicon has **no
256-bit ASIMD**. "Wider" means more independent 128-bit vectors, or ZA, not
a bigger packed register on the core.

### Source code signals

- A tight float/double/int loop with no SIMD at all
- `arm_neon.h` already in use, but a large GEMM / matvec / convolution is
  still running on core ASIMD
- Compiler auto-vectorized to scalar FP (`FADD` without `.4S`)

### Profiling signals

- CPU Profiler shows scalar FP (`FADDS`, `FMULS`, `FADD D0, D1, D2`) in a
  tight loop — no `.4S` / `.2D`
- ASIMD `.4S` on a workload that is clearly a large outer-product / GEMM
  and `hw.optional.arm.FEAT_SME` is 1
- CPU Bottlenecks: high Useful or Processing, low L1D miss (compute-bound)

---

## Caveats before proceeding

| Concern | Detail |
|---------|--------|
| **ISA check** | ASIMD is on every M1 / A14+. SME / SSVE from M4 / A18 (`hw.optional.arm.FEAT_SME`). Query sysctl; never assume. |
| **Memory-bandwidth-bound loops** | If Critical L1D miss while executing is already the bottleneck, wider issue hits the same wall. Gain is real but smaller. |
| **Latency-bound loops (serial accumulator)** | Dependency chains are not helped by width alone. Apply `patterns/parallel-accumulator.md` first (4-acc), then this pattern. |
| **SSVE-only** | Streaming SVE without the SME grid rarely beats four-wide core ASIMD. SSVE's job is to *support* ZA. |
| **Numerical accuracy** | Widening reorders FP ops and FMLA fuses a rounding step. Integer loops are unaffected by FP rounding but must keep signed vs unsigned. |

### Checking for memory-bandwidth saturation

Before widening, look at CPU Bottlenecks:

- **Compute-bound** (high Useful or Processing, low L1D miss) → proceed
- **Possibly bandwidth-bound** (Critical L1D miss while executing) → ask the user
- **Clearly bandwidth-saturated** → shrinking the working set or blocking
  to L1D / Shared LL (sizes from sysctl) may yield more; proceed only if
  the user agrees

### Numerical accuracy

**1. Operation reordering (associativity).**
Lane grouping changes which elements add together. FP add is not associative,
so results differ by rounding (ε). Not a logic error; bit-exact tests must
regenerate references.

**2. FMA fusion.**
Adjacent mul+add becomes `vfmaq` / `FMLA` — one rounding instead of two,
at most 1 ULP vs. separate mul then add, and usually *more* accurate.

| Scenario | What to do |
|----------|------------|
| FP reordering | Regenerate reference vectors; 1 ULP is not a logic error |
| Integer sign/unsigned | See the promotion algorithm in `simd-upconversion-impl.md` |

---

## Why this is fast

A packed `FADD.4S` does four floats in one instruction. Four independent
ASIMD accumulators keep the FMLA pipes busy. For large tiles of matrix
arithmetic, ZA + the Processing Grid issues outer-products that core ASIMD
cannot match. SSVE-only is rarely that path.

---

## The fix

Read `patterns/simd-upconversion-impl.md` for the step-by-step promotion
algorithm. That file contains:

- **Stage 1** — scalar → ASIMD 128: map each scalar op to a lane
  (`arm_neon.h`), 4-acc, scalar tail, FMLA, no GPR↔vector.
- **Stage 2** — decide more ASIMD ILP vs. SSVE vs. SME ZA.
- **Stage 3** — SME ZA outer-product / MLA for matmul, matvec, conv;
  SSVE in a supporting role.
- Dispatch via `library/cpu-dispatch.md`.

### Deciding which stage to apply

| Situation | Use |
|-----------|-----|
| Scalar numeric loop, not a giant GEMM | Stage 1 (ASIMD 128, 4 vectors) |
| Loop has a serial accumulator | `parallel-accumulator.md` Level 1 then Stage 1 |
| Large GEMM / outer-product / conv, `FEAT_SME` | Stage 3 (ZA + grid) |
| SME unavailable | Stay on Stage 1 with 4 ASIMD vectors |
| Compiler already auto-vectorized, want a second variant | `library/cpu-dispatch.md` |

### Steps

1. Identify the current width (scalar vs. ASIMD 128) and the operation.
2. Confirm features with `apple_feat("FEAT_SME")` etc. — never hardcode.
3. Check bandwidth saturation (above).
4. Check platform libraries (`vDSP_dotpr`, `cblas_sgemm`) before writing
   NEON. → `patterns/platform-libs-first.md`
5. Read `simd-upconversion-impl.md` and apply the matching stage.
   Pay attention to: scalar tail, FMLA fusion, live V-reg count (~25),
   4 KiB isolation if entering SME.
6. Add `restrict` if missing. → `patterns/missing-restrict.md`
7. Run the post-transformation checklist in the impl file.

---

## Verification

1. **Correctness** — FP may differ by ε / 1 ULP (reordering + FMLA). Larger
   diffs are logic errors. Integer: signed vs unsigned preserved.
2. **Dispatch guards** — every SME/I8MM/DotProd variant is behind
   `apple_feat("FEAT_*")` and `__attribute__((target(...)))`.
3. **Profiling** — CPU Profiler shows `.4S`/`.2D` or SME grid issue in the
   hot loop, not scalar `FADDS`.
4. **Benchmark** — `clock_gettime_nsec_np`; expect a large gain on
   compute-bound loops, little on bandwidth-bound ones.

---

## Presenting this to the user

1. Annotate the hot loop `/* scalar */` or `/* ASIMD 128; GEMM belongs on ZA */`.
2. One sentence on the ladder: scalar → ASIMD 128 → SME ZA for tiles.
3. Note caveats (bandwidth, associativity, SSVE-only rarely wins).
4. Show the promoted loop from the impl file.
5. If SME is off the table, say so explicitly: four ASIMD vectors is the stop.
