<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->

# Pattern triggers: source code

Use this file when you are reading source code and do not (yet) have profiling
data. Find the matching pattern below, then read the linked file in `patterns/`
for the full diagnosis and fix.

These patterns are worth fixing even without profiling confirmation — the code
structure alone is a strong predictor of the performance problem.

---

## Quick-match table

| Signal in source code | Pattern | Detail file |
|-----------------------|---------|-------------|
| Single FP variable updated in a loop: `sum += a[i]*b[i]`, `acc = fma(...)`, running max/min | Serial accumulator | `patterns/parallel-accumulator.md` |
| Scalar float loop, or ASIMD 128 on a large GEMM/matvec/conv that belongs on SME | Narrow SIMD | `patterns/simd-upconversion.md` |
| C function with two or more pointer parameters, at least one written, no `restrict`, separate in/out buffers | Missing restrict | `patterns/missing-restrict.md` |
| Spinlock is `while (!CAS)` / `LDXR`/`STXR` with no prior load; or `while (!flag);` | TAS / spin-wait | `patterns/ttas.md`, `patterns/spin-wait.md` |
| Struct fields written by different threads, no `alignas(128)` between them | False sharing | `patterns/false-sharing.md` |
| Global `count++` / `atomic_fetch_add` on a statistics field in a hot path | Shared statistics counter | `patterns/per-cpu-stats.md` |
| Hot function calls error-reporters without `[[gnu::cold]]` / `__attribute__((cold))` | Cold-path annotation | `patterns/cold-path-annotation.md` |
| `pthread_cond_broadcast` / `cv.notify_all()` / `dispatch_async` to every waiter regardless of job count | CV thundering herd | `patterns/cv-thundering-herd.md` |
| `pthread_mutex_lock` / `os_unfair_lock` guarding a lookup where writes are rare (<25%) | Mutex to rwlock | `patterns/mutex-to-rwlock.md` |
| Function named `hamming_distance`, `cosine_similarity`, `jaccard_*`, `iou`, … | Known algorithm | `references/known-algorithms-impl.md` |
| `std::sort` / `qsort` / `nth_element` on `float`/`double`/`int32`/`int64` arrays | SIMD sort | `patterns/simd-sort.md` |
| Function named `crc32c`; single `__crc32cd` acc; byte-table CRC32C | Fast CRC32C | `patterns/fast-crc32c.md` |
| Homemade NEON dot/FFT/GEMM/`memcpy` while Accelerate / libSystem would do | Platform libraries first | `patterns/platform-libs-first.md` |
| Integer load then `SCVTF`/`FMOV` into V; `UMOV`/`INS` in a hot loop | GPR ↔ vector | `patterns/gpr-vector-moves.md` |
| Unpredictable `if (a > b) x = y; else x = z;` that is not `CSEL` | Taken branch / CSEL | `patterns/taken-branch-and-csel.md` |
| `a * b + c` written as separate mul and add | Fused MAC | `patterns/fused-mac.md` |
| OpenMP `schedule(static)`, equal chunks, one task per core, barriers | AMP work partition | `patterns/amp-work-partition.md` |
| `while (!ready) ;` / `YIELD` / `WFE` backoff | Spin-wait | `patterns/spin-wait.md` |
| `SMSTART` around a scalar loop that reads ZA each iteration | SME engine bounce | `patterns/sme-engine-bounce.md` |
| Store payload then flag with plain `STR`; consumer plain load | Weak memory | `patterns/weakly-ordered-memory.md` |
| `#define CACHELINE 64`, `#define PAGE 4096`, assume 8 cores, assume no SME | sysctl hardcoding | `patterns/sysctl-hardcoding.md` |
| Several narrow stores, then one wide load of the same bytes | Store forwarding | `patterns/store-forwarding.md` |
| `vst1q` / `ST1` to a pointer with unknown 16 B / 128 B alignment | Unaligned access | `patterns/unaligned-access.md` |
| `__builtin_prefetch` / `PRFM` on a unit-stride scan | Software prefetch | `patterns/software-prefetch.md` |
| JIT calls `sys_icache_invalidate` per stub | JIT I-cache | `patterns/jit-icache.md` |
| Links a bundled BLAS/FFT instead of Accelerate | Library version upgrade | `patterns/library-version-upgrade.md` |
| `Data(contentsOf: url)`, `String(contentsOf:)`, `gethostbyname`, `Task { syncWork() }` on MainActor, `+load` / constructor | Main-thread blocking | `patterns/main-thread-blocking.md` |
| Whole-file JSON/plist rewrite, `fsync` / `F_FULLFSYNC`, SQLite without WAL, `VACUUM` | Dirty pages / disk writes | `patterns/disk-and-dirty-memory.md` |
| SwiftUI `body` does compute/I/O; stored `@escaping` view builder; `ObservableObject` blast | SwiftUI update cost | `patterns/swiftui-update-cost.md` |
| `pthread_yield_np()` in a poll loop | Spin-wait | `patterns/spin-wait.md` |

---

## Additional detection notes

These cover cases where the table alone does not carry enough specificity.
For the mechanism behind each pattern, read the linked `patterns/*.md` file.

- **Missing restrict** — C only; C++ has no standard `restrict` (only the
  non-portable `__restrict__` extension).
- **Narrow SIMD** — query `sysctl hw.optional.arm.FEAT_SME` (and friends)
  before recommending ZA; all M1+ have ASIMD 128. There is no 256-bit ASIMD.
- **SIMD sort** — check whether `std::stable_sort` is used before recommending
  a vectorized sort; Accelerate `vDSP_vsort` is not stable.
- **Platform libraries first** — fire this *before* writing NEON for a dot,
  FFT, GEMM, or copy. `otool -L` on the binary is the profile-side twin.
- **TTAS vs spin-wait** — TAS-without-read is `ttas.md`; any "idle but
  runnable" loop is `spin-wait.md`. Default advice for both: block.
- **False sharing vs per-CPU stats** — different offsets of one 128 B line →
  false sharing; same field, many threads → per-CPU stats.
- **Known algorithm** — function name is sufficient trigger; inspect the body
  only to confirm ISA level. Common name variants:

  | Algorithm | Common function names in code |
  |-----------|-------------------------------|
  | Cosine Similarity | `cosine_similarity`, `cosine_sim`, `cos_sim`, `cosine_distance`, `angular_similarity`, `dot_normalized` |
  | Hamming Distance | `hamming_distance`, `hamming_dist`, `hamming`, `count_differing_bits`, `bit_diff_count`, `popcount_xor` |
  | Jaccard Distance | `jaccard_distance`, `jaccard_similarity`, `jaccard_sim`, `jaccard_index`, `jaccard_coeff`, `iou` |

  If a name from the table is present, read `references/known-algorithms-impl.md`.
  Do not load it otherwise.
- **Fast CRC32C** — name is a sufficient trigger even without inspecting the
  loop body; variants include `crc32c`, `crc32_c`, `calc_crc32c`, `hash_crc32c`.
  zlib `crc32()` is a **different polynomial** — do not "upgrade" IEEE CRC-32
  to CRC32C.

---

## Pattern descriptions

### Serial accumulator

A single `float`/`double` is updated every iteration (`sum += …`,
`acc = fma(…)`, running min/max). The reduction is the only loop-carried
dependency. Level 1 is four scalar accumulators in C; Level 2 is four
`float32x4_t` with `vfmaq_f32`; Level 3 is SME ZA / `FMOPA` for large
GEMM/dot. Warn about FP associativity. Add `restrict`.

Read `patterns/parallel-accumulator.md`.

---

### Narrow SIMD

A tight numeric loop is scalar, or already ASIMD 128 when the math is a
large outer-product / GEMM that should use ZA on M4+. Promote with the
width ladder in `simd-upconversion-impl.md`: map each scalar op to a lane,
then decide more ASIMD ILP versus SME. SSVE-only rarely beats core ASIMD.
Keep a scalar tail.

Read `patterns/simd-upconversion.md`.

---

### Missing restrict

C function, two or more pointers, at least one written, buffers documented
not to overlap, no `restrict`. Clang must emit an overlap check or abandon
vectorization. Do not apply when aliasing is intentional (in-place filter).

Read `patterns/missing-restrict.md`.

---

### Test-and-Set / spin-wait

`while (!atomic_compare_exchange…)` with no relaxed load first, or
`while (!flag) ;`. Guide 7.3–7.4: prefer GCD, `os_unfair_lock`, pthreads,
C11; prefer LSE `LDADD`/`CAS`. Spin only for waits comparable to a thread
switch; otherwise block. Link `patterns/spin-wait.md` whenever the loop
occupies a core while idle.

Read `patterns/ttas.md`.

---

### False sharing

Per-thread fields packed into one struct without `alignas(128)` /
`char _pad[124]`. Writers are independent. Pad first, verify with a
dual-profile, then pack same-thread hot fields onto the fewest 128 B lines
(Guide rec 38). Cross-cluster modified sharing is especially expensive.

Read `patterns/false-sharing.md`.

---

### Shared statistics counter

`atomic_fetch_add` on a global `hits`/`bytes`/`errors` in a hot path, no
CAS retry (that would be TAS). Batch in a local, or shard 128 B buckets by
thread group. `pthread_cpu_number_np` may migrate — do not treat it as
pinning.

Read `patterns/per-cpu-stats.md`.

---

### Cold-path annotation

Error reporters, `abort` wrappers, or once-per-process setup called from a
hot function without `[[gnu::cold]]`. The annotation helps the *caller*'s
I-cache, not the cold function. 16 KiB pages: do not NOP-align branch
targets; beware L1I set conflicts on page-aligned dylibs.

Read `patterns/cold-path-annotation.md`.

---

### CV thundering herd

`pthread_cond_broadcast`, `notify_all`, or a loop of `notify_one` that
wakes every worker regardless of pending jobs. Preferred Apple fix: GCD
(queues, groups, semaphores). Precise wakeup and POSIX semaphores remain
valid.

Read `patterns/cv-thundering-herd.md`.

---

### Mutex to rwlock

A mutex / `os_unfair_lock` around a lookup that rarely writes. Readers
serialize for no reason. Use `pthread_rwlock` or a GCD concurrent queue
with `dispatch_barrier` for writes. Keep `os_unfair_lock` for short
exclusive critical sections.

Read `patterns/mutex-to-rwlock.md`.

---

### Known algorithm

Name match from the table above. Cosine: `vDSP_dotpr` + norms, or ASIMD
`vfmaq_f32` 4-acc, or SME DOT/MLA for huge N. Hamming/Jaccard: `CNT` +
`UADDLV` / `UDOT`; `FEAT_I8MM` on M2+. Dispatch `FEAT_DotProd` /
`FEAT_I8MM` / `FEAT_SME`.

Read `references/known-algorithms-impl.md`.

---

### SIMD sort

`std::sort` / `qsort` / `nth_element` on numeric primitives. Order: (1)
`vDSP_vsort` for float/double, (2) `std::sort` for general objects, (3)
Highway or an ARM-ported simd-sort if a vectorized numeric sort is
required. Do not recommend numpy/x86-simd-sort on M-series.

Read `patterns/simd-sort.md`.

---

### Fast CRC32C

`crc32c*` in the name, a byte table, or a single `__crc32cd` accumulator.
Use `library/crc32c_arm64.c` (3-stream CRC32CX) plus the portable table
fallback. If any checksum will do, Compression.framework / zlib in
libSystem. Do not confuse with IEEE `crc32()`.

Read `patterns/fast-crc32c.md`.

---

### Platform libraries first

New or existing code implements a dot, GEMM, FFT, convolution, sort of
floats, compression, or `memcpy` by hand. Check Accelerate / simd / GCD /
Compression / `memcpy` first (Guide rec 2). Custom ISA is what remains.

Read `patterns/platform-libs-first.md`.

---

### GPR ↔ vector moves

`LDR` into `W`/`X` then `SCVTF`/`FMOV` into V, or `UMOV`/`INS`/`DUP`
(general) inside the inner loop. Load into V; keep the value in one file.
Vector→GPR without extend: `FMOV`, not `UMOV`.

Read `patterns/gpr-vector-moves.md`.

---

### Taken branch / CSEL

Short unpredictable `if`/`else` assigning one of two values, or a loop
with a taken branch every few instructions. If-convert with CSEL/CSET;
unroll to cut taken-branch density. Predictable branches can stay
branches.

Read `patterns/taken-branch-and-csel.md`.

---

### Fused MAC

Separate multiply and add on the same chain (`s += a[i] * b[i]` without
`fma`/`vfmaq`/`FMLA`). Write `vfmaq_f32` / `FMLA.4S` / `MADD`. Integer
and FP both apply.

Read `patterns/fused-mac.md`.

---

### AMP work partition

`#pragma omp parallel for schedule(static)`, equal slice per worker, or
"spawn `hw.logicalcpu` threads and give each `n/threads`". Fails on S/P/E.
Dynamic queue, ≥3× tasks per core, GCD, QoS.

Read `patterns/amp-work-partition.md`.

---

### Spin-wait

Any idle loop that stays runnable. Block with a mutex, condvar, GCD, or
semaphore unless the wait is known to be ~one thread switch.

Read `patterns/spin-wait.md`.

---

### SME engine bounce

`SMSTART` / `svzero` / ZA load, then a core-side dependent use of that
data before the engine has a full ZA of work, then `SMSTOP`. Isolate 4 KiB
regions; do not hardcode SVL.

Read `patterns/sme-engine-bounce.md`.

---

### Weak memory

Producer: `data = x; ready = 1;` without release. Consumer: `if (ready)
use data;` without acquire. Needs `memory_order_release`/`acquire`,
`os_atomic`, or `DMB ISH`.

Read `patterns/weakly-ordered-memory.md`.

---

### sysctl hardcoding

`#define CACHELINE 64`, `#define PAGE_SIZE 4096`, `nthreads = 8`,
`#ifdef __ARM_FEATURE_SVE` assumed true on all M-series. Query sysctl.
Rosetta (`sysctl.proc_translated`) must take the portable path.

Read `patterns/sysctl-hardcoding.md`.

---

### Store forwarding

Build a wide value via several narrow stores to a stack slot or buffer,
then load it wide — or store/reload instead of keeping a register. Match
store and load widths; prefer registers.

Read `patterns/store-forwarding.md`.

---

### Unaligned access

`vst1q_*` / `ST1` through a `uint8_t *` of unknown alignment, especially
in a copy. Align destinations; prefer unaligned loads over unaligned
stores if only one side can be aligned.

Read `patterns/unaligned-access.md`.

---

### Software prefetch

`__builtin_prefetch(&a[i + k])` on a sequential scan. Delete it first;
recode the access pattern for the hardware prefetcher; add PRFM only if a
profile still shows late fills.

Read `patterns/software-prefetch.md`.

---

### JIT I-cache

Emitter calls `sys_icache_invalidate` (or `__clear_cache`) once per
tiny stub. Batch into whole pages; reuse RX regions.

Read `patterns/jit-icache.md`.

---

### Library version upgrade

The project ships OpenBLAS, FFTW, a private zlib, or an old OpenSSL dylib
for work Accelerate / libSystem / CommonCrypto / Compression already do.
`otool -L` plus `references/library-versions.md`.

Read `patterns/library-version-upgrade.md`.

---

### Main-thread blocking

Sync network or file I/O on the main thread, `Task { syncWork() }`
inheriting MainActor, `dispatch_sync` onto main, `+load` /
constructor work before first draw. Hang rate (run loop busy >250 ms)
and watchdog `0x8badf00d` are the crash-side twins.

Read `patterns/main-thread-blocking.md`.

---

### Dirty pages / disk writes

A 1-byte store dirties a **16 KiB** VM page. Whole-file serialized
rewrites, `fsync` / `F_FULLFSYNC`, SQLite without WAL, unbounded
caches. Measure with `XCTMemoryMetric` / `XCTStorageMetric`.

Read `patterns/disk-and-dirty-memory.md`.

---

### SwiftUI update cost

`body` / `onAppear` / stored escaping closures that capture `self`.
`ObservableObject` invalidates readers of other properties. Long-body
lanes in the SwiftUI template: orange >500 µs, red >1000 µs.

Read `patterns/swiftui-update-cost.md`.
