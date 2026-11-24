<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->

# Pattern triggers: profiling output

Use this file when you have profiling data — Instruments **CPU Profiler**,
**CPU Counters**, **CPU Bottlenecks** (Instruments 26+), `xctrace export`,
or a sampled cycle profile. Time Profiler is wall-clock; do not use it for
ISA work. Find the matching pattern below, then read the linked file in
`patterns/` for the full diagnosis and fix.

---

## Quick-match table

| Signal in profile | Pattern | Detail file |
|-------------------|---------|-------------|
| `FADDS` / `FADDD` / scalar `FMLA` / `FMADD` dominates; Useful high or Processing bottleneck; low L1D miss | Serial accumulator | `patterns/parallel-accumulator.md` |
| Scalar FP (`FADD`/`FMUL` without `.4S`/`.2D`) in a hot loop; or ASIMD when a large GEMM belongs on the SME grid | Narrow SIMD | `patterns/simd-upconversion.md` |
| Alias-check preamble in Clang asm (pointer sub + `B.cond` to a scalar clone) before an otherwise vectorizable loop | Missing restrict | `patterns/missing-restrict.md` |
| `CAS` / `LDXR`+`STXR` cluster with no plain load first; or a tight load-of-flag spin | Test-and-Set / spin | `patterns/ttas.md`, `patterns/spin-wait.md` |
| Dual-profile jumper + different offsets on the same 128 B line + `ATOMIC_OR_EXCLUSIVE_FAIL` | False sharing | `patterns/false-sharing.md` |
| `LDADD` / `STADD` on one counter; same offset, many threads | Shared statistics counter | `patterns/per-cpu-stats.md` |
| Hot symbol in a system dylib (`Accelerate`, `libSystem`, `Compression`) with a known better path | Library version upgrade | `patterns/library-version-upgrade.md` |
| Homemade NEON/dot/FFT/memcpy while Accelerate / libSystem is absent from `otool -L` | Platform libraries first | `patterns/platform-libs-first.md` |
| `CRC32B` / `CRC32CX` / `CRC32CW` single accumulator dominating a checksum | Fast CRC32C | `patterns/fast-crc32c.md` |
| `psynch_cvbroad` / `kevent` storm; GCD wake scaling with waiter count | CV thundering herd | `patterns/cv-thundering-herd.md` |
| `pthread_mutex_*` / `psynch_mutexwait` / `os_unfair_lock` on a read-mostly lookup | Mutex to rwlock | `patterns/mutex-to-rwlock.md` |
| `std::sort` / introsort hot on `float`/`double`/`int32`/`int64` arrays | SIMD sort | `patterns/simd-sort.md` |
| Hot name matches cosine / Hamming / Jaccard | Known algorithm | `references/known-algorithms-impl.md` |
| `FMOV`/`UMOV`/`INS`/`DUP` (general) dense in a hot loop | GPR ↔ vector moves | `patterns/gpr-vector-moves.md` |
| Delivery-bandwidth bottleneck; `B.cond` density near 1 taken per 8 insns | Taken branch / CSEL | `patterns/taken-branch-and-csel.md` |
| `FMUL`+`FADD` pair where `FMLA`/`vfmaq` would fuse | Fused MAC | `patterns/fused-mac.md` |
| OpenMP static / equal chunks; E-core stragglers in a dual-profile | AMP work partition | `patterns/amp-work-partition.md` |
| `WFE` / `YIELD` loop or tight load of a flag occupying S/P cores | Spin-wait | `patterns/spin-wait.md` |
| `SMSTART` plus core waiting; dependent `FMOV` after `SMSTOP` each tile | SME engine bounce | `patterns/sme-engine-bounce.md` |
| Publication of a pointer/flag without `DMB` / `STLR` / release | Weak memory | `patterns/weakly-ordered-memory.md` |
| Constants: 64 B line, 4 KiB page, 8 cores, "no SME" compiled in | sysctl hardcoding | `patterns/sysctl-hardcoding.md` |
| Many small stores forwarding into one wide load; STLF fails | Store forwarding | `patterns/store-forwarding.md` |
| Unaligned `ST1` of wide vectors in a streaming store loop | Unaligned access | `patterns/unaligned-access.md` |
| `PRFM` in a sequential scan the HW prefetcher already covers | Software prefetch | `patterns/software-prefetch.md` |
| `sys_icache_invalidate` / `ic ivau` storm from a JIT | JIT I-cache | `patterns/jit-icache.md` |
| Hot function calls error reporters; I-cache pollution | Cold-path annotation | `patterns/cold-path-annotation.md` |
| Hangs track / MetricKit hang rate; `0x8badf00d`; main-thread `recv`/`fsync` | Main-thread blocking | `patterns/main-thread-blocking.md` |
| Hitch rate >10 ms/s; SwiftUI long body >500 µs | SwiftUI update cost / main-thread | `patterns/swiftui-update-cost.md`, `patterns/main-thread-blocking.md` |
| Allocations dirty growth; jetsam `rpages`; File Activity write storm | Dirty pages / disk writes | `patterns/disk-and-dirty-memory.md` |

---

## Pattern descriptions

### Serial accumulator

CPU Profiler / `llvm-objdump -d` shows scalar `FADDS`, `FADDD`, `FMULS`, or
scalar `FMLA`/`FMADD` at the top of a tight loop. CPU Bottlenecks reports
**Processing** (dependency) or high **Useful** with low L1D miss — not a
memory stall. Cycles per iteration sit at FP latency (look up Appendix A;
~4 cycles is the FMLA rule of thumb) even though the body is short. Four
independent accumulators, then ASIMD `vfmaq_f32`, then SME ZA for large
GEMM/dot.

Read `patterns/parallel-accumulator.md`.

---

### Narrow SIMD

The hot loop is scalar FP or ASIMD without `.4S`/`.2D` packing, on a core
that has 32 V registers. Or the loop *is* ASIMD 128 but the algorithm is a
large GEMM / outer-product / tiled matvec that belongs on the SME Processing
Grid (M4+). There is no 256-bit ASIMD; "wider" means more independent 128-bit
vectors or ZA, not a Y-register. Bandwidth-bound loops (Critical L1D miss
while executing) do not need a wider issue.

Read `patterns/simd-upconversion.md`.

---

### Missing restrict

Clang `-O3` assembly shows a scalar **aliasing preamble** — pointer
subtraction, compare, `B.cond` to a scalar clone — in front of a simple
array loop. Two versions of the same loop may appear; the scalar path exists
only for the overlapping-pointer case. Confirm with
`clang -O3 -Rpass=loop-vectorize`. C only (`restrict`); C++ uses
`__restrict__`.

Read `patterns/missing-restrict.md`.

---

### Test-and-Set spinlock / spin-wait

CPU Profiler shows a tight `CAS` or `LDXR`/`STXR` cluster with no ordinary
load of the lock first, or a load-of-flag / `WFE` / `YIELD` loop. Throughput
drops as threads increase. Guide 7.3: **block, do not spin**, except waits
on the order of a thread switch. Prefer GCD, `os_unfair_lock`, pthreads,
C11; prefer LSE `LDADD`/`CAS` over exclusive pairs. TTAS is the rare
short-wait form; see also `patterns/spin-wait.md`.

Read `patterns/ttas.md`.

---

### False sharing

There is no HITM sampler. Diagnose with a dual-profile jumper (hot at N
threads, invisible at 1), **different byte offsets** of the same 128 B line
written by different threads, and elevated `ATOMIC_OR_EXCLUSIVE_FAIL` when
atomics are involved. Pad to `alignas(128)`. Then pack same-thread hot
fields onto the fewest lines. Cross-cluster modified sharing is ~10×
intra-cluster Shared LL — keep writers inside one cluster when possible.

Read `patterns/false-sharing.md`.

---

### Shared statistics counter

`LDADD` / `STADD` / `atomic_fetch_add` on **one** counter in the hot path,
same offset, many threads — true sharing, not false sharing. Dual-profile
shows linear growth with thread count. Batch locally, or shard into 128 B
buckets (thread-group first; `pthread_cpu_number_np` is a heuristic that
may migrate).

Read `patterns/per-cpu-stats.md`.

---

### Library version upgrade

CPU Profiler / `sample` shows a symbol whose dylib is a **system** framework
(`Accelerate`, `libSystem`, `Compression`, `AppleArchive`) rather than the
app. The name matches `references/library-versions.md`. The win is an OS /
SDK / link-path update, not a source rewrite. Detect with `otool -L`,
`dyld_info`, `sw_vers` — not `ldconfig`.

Read `patterns/library-version-upgrade.md`.

---

### Platform libraries first

The hot symbol is *homemade* (`dot_neon`, `my_fft`, `simd_memcpy`) and
`otool -L` does not show Accelerate / Compression / libSystem owning that
work. Guide rec 2 (High/High): call vDSP/BLAS/`memcpy`/GCD first. Custom
ASIMD is the fallback.

Read `patterns/platform-libs-first.md`.

---

### Fast CRC32C

`CRC32B` / `CRC32CX` / `CRC32CW` with a **single** accumulator, or a
byte-table CRC, dominates a checksum. One CRC32CX is latency-bound; three
independent streams hide that latency. Production checksums: Compression /
zlib in libSystem if Castagnoli is not required; otherwise
`library/crc32c_arm64.c`.

Read `patterns/fast-crc32c.md`.

---

### SIMD sort

`std::sort`, introsort, or `qsort` is hot on `float` / `double` / `int32_t`
/ `uint32_t` / `int64_t` / `uint64_t`. Discarded/branch-mispredict events
are often elevated. Use `vDSP_vsort` where it fits, `std::sort` for general
objects, Highway if a vectorized numeric sort is required. Do not pull an
x86 SIMD-sort repo onto M-series.

Read `patterns/simd-sort.md`.

---

### Known algorithm — optimized SIMD replacement available

CPU Profiler shows a function whose name matches the table below. Generate
the ASIMD / DotProd / I8MM / SME version directly. Dispatch with
`hw.optional.arm.FEAT_*`, not a vendor uarch string.

| Algorithm | Common function names in code |
|-----------|-------------------------------|
| Cosine Similarity | `cosine_similarity`, `cosine_sim`, `cos_sim`, `cosine_distance`, `angular_similarity`, `dot_normalized` |
| Hamming Distance | `hamming_distance`, `hamming_dist`, `hamming`, `count_differing_bits`, `bit_diff_count`, `popcount_xor` |
| Jaccard Distance | `jaccard_distance`, `jaccard_similarity`, `jaccard_sim`, `jaccard_index`, `jaccard_coeff`, `iou` |

If a name matches, read `references/known-algorithms-impl.md`. Do not load it
otherwise.

---

### CV thundering herd

`psynch_cvbroad`, `__psynch_cvbroad`, `kevent`, `dispatch_kevent_worker`, or
a GCD wake path is hot. Context-switch rate scales with waiter count.
Useful work collapses while CPU stays busy. Differentiator from lock
contention: the lock hold is short; the cost is N useless wakes. Preferred
fix: GCD. Still valid: precise wakeup, semaphore.

Read `patterns/cv-thundering-herd.md`.

---

### Mutex to rwlock

`pthread_mutex_lock`, `psynch_mutexwait`, or `os_unfair_lock` is prominent
on a lookup/search/cache-read whose writes are rare. Dual-profile: IPC /
Useful drops as threads grow while user work is constant. User-space Darwin
fix: `pthread_rwlock`, or a GCD concurrent queue with barrier writes.
`os_unfair_lock` stays correct for *short exclusive* sections.

Read `patterns/mutex-to-rwlock.md`.

---

### GPR ↔ vector moves

Hot loop is dense in `FMOV` (general), `UMOV`, `INS` (general),
`DUP` (general). Each move eats a load-issue slot and ~3–5 cycles
(Appendix A / Guide 5.5.1–5.5.2). Load FP data into V, keep it there, store
from V. Prefer `FMOV` over `UMOV`/`SMOV` when no extend is needed.

Read `patterns/gpr-vector-moves.md`.

---

### Taken branch / CSEL

CPU Bottlenecks **Delivery** (fetch bandwidth) with high taken-branch
density (`INST_BRANCH_TAKEN` / `INST_ALL` approaching 1/8). `B.cond` on
unpredictable short if/else that CSEL/CSET/FCSEL could if-convert. Unroll
and straighten loops first; do not NOP-align branch targets.

Read `patterns/taken-branch-and-csel.md`.

---

### Fused MAC

Annotate shows `FMUL` immediately followed by `FADD`/`FADDS` (or integer
`MUL`+`ADD`) on the same registers. Rewrite as `FMLA` / `FMADD` /
`vfmaq_f32` / `MADD`. One issue, one rounding for FP, shorter dependency.

Read `patterns/fused-mac.md`.

---

### AMP work partition

OpenMP `schedule(static)`, equal chunks, or a barrier after each phase.
Dual-profile: E-core workers are still in the chunk after P/S cores have
hit the barrier. Cannot `taskset` onto P cores; hint with QoS. Dynamic
queue, ≥3× tasks per core, GCD.

Read `patterns/amp-work-partition.md`.

---

### Spin-wait

Tight load of a flag, `WFE`/`YIELD` loop, or homemade backoff occupying
S/P cores. Guide 7.3: block unless the wait is on the order of a thread
switch. Spinning pins work onto E cores and steals S/P from the rest of
the machine.

Read `patterns/spin-wait.md`.

---

### SME engine bounce

`SMSTART` then the core waits on engine results via `FMOV`/`MOVA` every
tile, or `SMSTOP`/`SMSTART` around a dependent scalar loop. Engine trails
the core by dozens to thousands of cycles. Isolate core-touched and
SME-touched data on separate aligned 4 KiB regions; compute a whole ZA
before extracting.

Read `patterns/sme-engine-bounce.md`.

---

### Weak memory

A producer stores a payload then a flag/pointer with plain `STR`, and the
consumer observes the flag without `LDAR`/`DMB`. Intermittent torn
publication, TSan hits, or "works until a P/E migration". Apple silicon is
not TSO. Use acquire/release, `os_atomic`, or `DMB ISH`.

Read `patterns/weakly-ordered-memory.md`.

---

### sysctl hardcoding

Binary assumes 64 B lines, 4 KiB pages, 8 cores, or "this chip has no SME".
Wrong padding, wrong cache blocking, illegal SME opcodes on M1–M3, wasted
ZA on M4+. Query `hw.cachelinesize`, `hw.pagesize`, `hw.perflevel{N}.*`,
`hw.optional.arm.FEAT_*`. Detect Rosetta with `sysctl.proc_translated`.

Read `patterns/sysctl-hardcoding.md`.

---

### Store forwarding

A wide `LD1`/`LDR` of a value that was just written as several small
`STRB`/`STRH`/`STP` pieces, or a store/reload that could have stayed in a
register. Guide rec 44–45: hard-to-predict STLF and many-small-stores-to-one
load are expensive. Keep the value in a register; if you must store, match
widths.

Read `patterns/store-forwarding.md`.

---

### Unaligned access

Unaligned `ST1` of Q / streaming-Z vectors in a copy or store loop. Apple
silicon prefers unaligned **loads** over unaligned stores; wide unaligned
stores split on 128 B lines. Align destinations; if only one side can be
aligned, align the stores.

Read `patterns/unaligned-access.md`.

---

### Software prefetch

`PRFM PLDL1KEEP` (or `__builtin_prefetch`) in a unit-stride scan. The
hardware prefetcher already covers sequential access; extra PRFM steals
issue and can evict. Recode the access pattern first; prefetch only after
that fails.

Read `patterns/software-prefetch.md`.

---

### JIT I-cache

Hot `sys_icache_invalidate`, `ic ivau`+`dsb`+`isb`, or `__clear_cache` from
a JIT emitting tiny traces. Guide rec 9: invalidation is expensive. Batch
emissions, reuse pages, avoid toggling NX on every stub.

Read `patterns/jit-icache.md`.

---

### Cold-path annotation

A hot function's body is interleaved with error reporters in I-cache.
Branch-mispredict / Discarded is minor; L1I pressure is the cost. Mark
callees `[[gnu::cold]]`. Confirm with
`apple-silicon-profile/tools/gccbranchprob.py` or Clang PGO. Do not
NOP-align targets; 16 KiB pages make L1I set conflicts likelier for
page-aligned dylibs.

Read `patterns/cold-path-annotation.md`.

---

### Main-thread blocking

Hangs instrument / MetricKit hang rate (main run loop busy >250 ms),
Animation Hitches, App Launch time-to-first-draw, or a watchdog
`.ips` with `0x8badf00d`. CPU Profiler of the hang often shows
`kevent` / `recv` / `fsync` / `semaphore_wait` on
`com.apple.main-thread`.

Read `patterns/main-thread-blocking.md`.

---

### SwiftUI update cost

SwiftUI template Long View Body Updates (orange >500 µs, red
>1000 µs) or a long Update Groups lane with cheap bodies. Hitches
track coincident with those updates.

Read `patterns/swiftui-update-cost.md`.

---

### Dirty pages / disk writes

Allocations dirty anonymous VM growing across a generation; jetsam
`rpages * pageSize` (16 KiB); File Activity small-write / `fsync`
storm; MetricKit disk-write exception.

Read `patterns/disk-and-dirty-memory.md`.
