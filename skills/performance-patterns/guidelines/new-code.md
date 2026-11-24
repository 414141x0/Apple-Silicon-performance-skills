<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->

# Guidelines: writing new performance-sensitive code

A checklist for agents writing **new** C/C++ or assembly code where performance
matters on Apple silicon. Apply these before writing the first line — they are
far cheaper to get right upfront than to retrofit later.

Each item links to the full pattern file for rationale and code templates.
Order follows Guide §1.2 / high-impact recommendations.

ISA catalogs, preferred mnemonics, and Appendix A latencies: use
`apple-silicon-codegen`. Do not duplicate them here.

---

## Checklist

### 1. Measurable goal

- [ ] **Set a measurable goal before changing code.**
  Time with `mach_absolute_time()` or `clock_gettime_nsec_np(CLOCK_UPTIME_RAW)`,
  not `gettimeofday`. Record what "done" means (GB/s, ns/op, frames, joules).
  High Useful-bandwidth in CPU Bottlenecks is not the goal — the algorithm is.

### 2. Platform libraries first

- [ ] **Call Accelerate / simd / GCD / `memcpy` / Compression before custom NEON.**
  Homemade `float32x4_t` kernels lose to vDSP/BLAS. Accelerate uses ASIMD
  and, from M4, SME; on earlier chips it uses private matrix hardware you
  cannot emit. Metal is GPU, not CPU SIMD.
  → `patterns/platform-libs-first.md`

### 3. sysctl for ISA and cache

- [ ] **Query `sysctl` for ISA and cache; never hardcode 64 B / 4 KiB / core count.**
  Cache line is **128 B** (`hw.cachelinesize`). Page is **16 KiB** (`hw.pagesize`).
  Core counts and L1/LL sizes live under `hw.perflevel{N}.*`. Dispatch on
  `hw.optional.arm.FEAT_*`, not on "Arm version".
  → `patterns/sysctl-hardcoding.md`, `library/cpu-dispatch.md`

### 4. 128 B layout

- [ ] **Pad independent writers to 128 B; pack hot fields onto the fewest lines.**
  False sharing is a 128-byte problem. The dual is Guide rec 38: group
  same-thread hot fields so they share a line.
  → `patterns/false-sharing.md`

### 5. FMLA / MADD

- [ ] **Prefer FMLA/MADD / `vfmaq_*` over separate mul+add.**
  Fused op-accumulate is density, latency, and (for FP) a single rounding.
  → `patterns/fused-mac.md`

### 6. CSEL / CSET over unpredictable branches

- [ ] **If-convert unpredictable short branches with CSEL/CSET/FCSEL.**
  Highly predictable branches can stay branches. Taken-branch density near
  1 in 8 instructions collapses fetch bandwidth.
  → `patterns/taken-branch-and-csel.md`

### 7. One pre/post-index per load/store sequence

- [ ] **Use one pre- or post-index per sequence; then positive base+imm.**
  Do not write a chain of `LDR`+`ADD` pairs when `[Xn], #imm` or
  `[Xn, #imm]!` exists.

### 8. Minimize GPR ↔ vector moves

- [ ] **Keep a value in one register file.**
  Load FP/SIMD data into V, not X then `FMOV`. Vector→GPR: prefer `FMOV`
  when no extend/convert is needed. Density of `FMOV`/`UMOV`/`INS`/`DUP(general)`
  in a hot loop is a bug.
  → `patterns/gpr-vector-moves.md`

### 9. Dynamic work, QoS, no spin-wait

- [ ] **Dynamic work queue, ≥3× tasks per core, QoS, no spin-wait.**
  Static OpenMP equal chunks leave E-core stragglers. Cannot pin P vs E;
  hint with `QOS_CLASS_USER_INTERACTIVE` … `BACKGROUND`. Block idle threads.
  → `patterns/amp-work-partition.md`, `patterns/spin-wait.md`, `patterns/ttas.md`

- [ ] **Never block the main thread on I/O, JSON, or a semaphore.**
  `URLSession` async, `Task.detached` for sync work, no `+load`.
  → `patterns/main-thread-blocking.md`

- [ ] **Do not dirty 16 KiB pages or rewrite whole files for a 1-byte change.**
  `F_BARRIERFSYNC` not `F_FULLFSYNC`; SQLite WAL; `XCTMemoryMetric` /
  `XCTStorageMetric` in tests.
  → `patterns/disk-and-dirty-memory.md`

### 10. SME

- [ ] **If using SME: SSVE supports the grid; no core↔engine bounce; 4 KiB isolation; do not hardcode SVL.**
  ZA is 4 KiB; SVL is 512-bit on Apple silicon but portable code must query it.
  Target SME-8; SME-2 runs the same kernel slower. SSVE-only rarely beats
  core ASIMD.
  → `patterns/sme-engine-bounce.md`

### 11. Weak memory

- [ ] **DMB/DSB or acquire/release when publishing across threads.**
  Apple silicon is weakly ordered. `atomic_store_explicit(..., memory_order_release)`
  / `LDAR`/`STLR` / `os_atomic_*`.
  → `patterns/weakly-ordered-memory.md`

### 12. MTE

- [ ] **Use the Apple allocator; keep allocations dense.**
  Do not write a custom allocator to "beat" `malloc` under MTE (M5 / A19+).
  Tag-unaware homemade pools fight the hardware.

### 13. restrict / const, 4-acc, scalar tail, cold annotation

- [ ] **Add `restrict` to all non-aliased pointer parameters.** (C; C++: `__restrict__`)
  → `patterns/missing-restrict.md`

- [ ] **Mark read-only inputs `const`.**

- [ ] **Never use a single accumulator in a hot loop.**
  Start with four independent accumulators; combine at the end.
  → `patterns/parallel-accumulator.md`

- [ ] **Warn callers about FP associativity.**
  Multi-accumulator and SIMD reductions reorder floating-point operations.

- [ ] **Include a scalar tail for non-multiple lengths.**
  The tail should also use four accumulators, not a fresh serial loop.

- [ ] **Mark error-reporting / rare-path functions `__attribute__((cold))` /
  `[[gnu::cold]]` on the definition.**
  → `patterns/cold-path-annotation.md`

- [ ] **Pad per-thread data to 128 B; shard stats instead of one global `LDADD`.**
  → `patterns/per-cpu-stats.md`

### 14. Width ladder

- [ ] **Promote along the Apple width ladder, not a 256/512-bit vector width.**
  1. Scalar C with 4 accumulators.
  2. ASIMD 128-bit (`arm_neon.h`), ≤~25 live of 32 V-regs, FMLA, loads into V.
  3. SME ZA + Processing Grid for matrix / outer-product / large-tile dot;
     SSVE stages, grid computes. If SME is unavailable, stay on ASIMD with
     4 vectors. SSVE-only rarely wins.
  → `patterns/simd-upconversion.md`

### 15. Known algorithms

- [ ] **If the algorithm is in the known-algorithms table, generate the
  vectorized version with `FEAT_*` dispatch directly.**
  SIMD sort: Accelerate / `std::sort` / Highway — not an x86 GitHub repo.
  → `references/known-algorithms.md`, `patterns/simd-sort.md`

### 16. CRC32C

- [ ] **Do not write a CRC32C function from scratch.**
  Use `library/crc32c_arm64.c` (CRC32CX multi-acc) with the portable fallback,
  dispatched as in `patterns/fast-crc32c-impl.md`. If any checksum will do,
  Compression.framework / zlib in libSystem first.
  → `patterns/fast-crc32c.md`, `patterns/platform-libs-first.md`

### 17. SwiftUI

- [ ] **Keep `body` a description.** No I/O or heavy compute in `body` /
  `onAppear`. Prefer `@Observable`. Do not store escaping view-builder
  closures that capture `self`.
  → `patterns/swiftui-update-cost.md`

---

## Quick reference by language

| Concern | C | C++ | Inline asm |
|---------|---|-----|-----------|
| No-alias hint | `restrict` | `__restrict__` | — |
| Read-only input | `const T * restrict` | `const T * __restrict__` | — |
| SIMD cleanup | none | none | keep the value in V *or* X |
| Feature annotation | `/* FEAT_SME */` | same | same |
| Runtime dispatch | `apple_feat("FEAT_*")` | same | same |
| Clone attribute | `__attribute__((target("sme2")))` | same | `.arch_extension sme2` |
| Compile target | `-mcpu=apple-m1` (m2/m3/m4/m5) | same | same |
| Cache line padding | `alignas(128)` (C11) | `alignas(128)` | — |
| Cold function | `__attribute__((cold))` | `[[gnu::cold]]` | — |
| QoS | `pthread_set_qos_class_self_np` | same | — |
| Fused MAC | `vfmaq_f32` | same | `FMLA.4S` |
| Atomics | C11 `atomic_*` → LSE | `std::atomic` | `LDADD` / `CAS`, not `LDXR`/`STXR` |
| Timing | `clock_gettime_nsec_np` | `clock_gettime_nsec_np` | `CNTVCT_EL0` |
