---
name: performance-patterns
description: >-
  Detect and fix Apple Silicon / AArch64 / C/C++/Swift-adjacent C performance
  patterns from source or Instruments/xctrace. Trigger on: serial accumulator,
  ASIMD/NEON, vfmaq, FMLA, SMSTART, FMOPA, false sharing 128, restrict, TTAS,
  os_unfair_lock, GCD, QoS, LDADD, CSEL, FMOV GPR-vector, Accelerate, vDSP,
  CRC32, sysctl FEAT_, P/E cores, static OpenMP partition, spin-wait, SME bounce,
  hang, hitch, 0x8badf00d, Task.detached, SwiftUI body, Observable, fsync,
  F_BARRIERFSYNC, XCTStorageMetric, jetsam.
  Invoke when user asks to optimize, review for performance, or write new SIMD
  even without a profile.
---

<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->

# Performance patterns skill

A catalog of well-known code patterns that cause performance problems on
Apple silicon, with detection signals and resolution playbooks for each.

Write-time ISA catalogs, mnemonic tables, and Appendix A latency numbers live
in `apple-silicon-codegen`. This skill does not duplicate them. Load that skill
when emitting new ASIMD / SSVE / SME kernels from scratch; load this skill when
detecting and fixing a known anti-pattern.

---

## How to use this skill

### Step 1 — Load the right file for your context

| Context | Read this file |
|---------|---------------|
| You have **profiling output** (Instruments, `xctrace`, CPU Profiler, CPU Counters, CPU Bottlenecks, flamegraphs) | `triggers/from-profile.md` |
| You are **reading existing source code** and have no profiling data yet | `triggers/from-source.md` |
| You are **writing new** performance-sensitive C/C++ or SIMD code | `guidelines/new-code.md` |

The trigger files cover all the same patterns; they are separated so you only
load what is relevant. `guidelines/new-code.md` is a write-time checklist —
load it instead of a trigger file when generating new code, not reviewing it.

### Step 2 — Identify the matching pattern

Each trigger file contains a compact table and brief descriptions — enough to
decide whether the code or profile matches a known pattern.

### Step 3 — Read the pattern detail file

When a pattern matches, read the corresponding file from `patterns/`. Do not
attempt the fix from memory.

### Step 4 — Apply the fix and verify

Follow the step-by-step instructions and verification method in the pattern file.

Multiple patterns can co-apply. Check all plausible matches before picking one.

---

## Reusable library modules

These standalone implementation guides are available to any agent working in this
skill, not only when following a specific pattern. Load the relevant file directly
if the capability is needed.

| Module | What it provides |
|--------|-----------------|
| `library/cpu-dispatch.md` | Runtime ISA / topology detection via `sysctlbyname("hw.optional.arm.FEAT_*")` and `hw.perflevel{N}.*`. Function-pointer dispatch is the Darwin default. Use whenever a function has multiple performance-level implementations. |
| `library/crc32c_arm64.c` + `library/crc32c_portable.c` | Drop-in CRC32C: ARM CRC32CX multi-accumulator (`crc32c_arm64.c`) plus ISA-neutral table fallback (`crc32c_portable.c`). Dispatch wrapper is in `patterns/fast-crc32c-impl.md`. Prefer Compression.framework / libSystem when a generic checksum is enough. |
| `patterns/simd-upconversion-impl.md` | Width-ladder promotion: scalar → ASIMD 128 (`arm_neon.h`) → SSVE predicates / SME ZA for matrix, outer-product, and large-tile dots. Scalar tail, associativity, bandwidth-bound caveat. |

For write-time instruction selection, register pressure, and latency lookup,
invoke `apple-silicon-codegen` rather than copying chapter catalogs into a
pattern fix.

---

## Pattern catalog

Load a trigger file first; then open only the matching `patterns/<name>.md`.
Class 2 patterns have a separate `-impl.md`.

| File | One-line |
|------|----------|
| `patterns/platform-libs-first.md` | Accelerate / simd / GCD / memcpy / Compression before custom NEON |
| `patterns/sysctl-hardcoding.md` | Query `FEAT_*` / 128 B / 16 KiB / core counts; never hardcode |
| `patterns/parallel-accumulator.md` | Serial `sum +=` → 4-acc → ASIMD `vfmaq` → SME ZA for large GEMM |
| `patterns/simd-upconversion.md` | Width ladder: scalar → ASIMD 128 → SSVE/SME ZA (`-impl.md`) |
| `patterns/fused-mac.md` | FMLA / MADD / `vfmaq` over separate mul+add |
| `patterns/gpr-vector-moves.md` | Keep a value in V *or* X; `FMOV`/`UMOV`/`INS` density |
| `patterns/taken-branch-and-csel.md` | CSEL/CSET; taken-branch density near 1 in 8 |
| `patterns/missing-restrict.md` | `restrict` on non-aliased C pointers |
| `patterns/false-sharing.md` | Independent writers on **128 B** lines; pack hot fields |
| `patterns/store-forwarding.md` | Hard-to-predict STLF; many small stores into one wide load |
| `patterns/unaligned-access.md` | Prefer unaligned loads over unaligned wide stores |
| `patterns/software-prefetch.md` | PRFM sparingly; recode for the HW prefetcher |
| `patterns/ttas.md` | Block first; TTAS only for ~thread-switch waits; LSE CAS |
| `patterns/spin-wait.md` | Do not occupy S/P cores in a wait loop |
| `patterns/amp-work-partition.md` | Dynamic queue, ≥3× tasks/core, QoS; no static OpenMP |
| `patterns/per-cpu-stats.md` | Shard `LDADD` counters; 128 B buckets |
| `patterns/cv-thundering-herd.md` | GCD first; precise wakeup; `psynch_cvbroad` / `kevent` |
| `patterns/mutex-to-rwlock.md` | `pthread_rwlock` / GCD concurrent queue on read-mostly |
| `patterns/weakly-ordered-memory.md` | DMB/DSB or acquire/release on publication |
| `patterns/sme-engine-bounce.md` | No core↔engine ping-pong; 4 KiB isolation; no hardcoded SVL |
| `patterns/cold-path-annotation.md` | `[[gnu::cold]]`; 16 KiB L1I set conflicts; no NOP-align |
| `patterns/jit-icache.md` | Batch I-cache invalidation |
| `patterns/fast-crc32c.md` | ARM CRC32CX 3-acc (`-impl.md` + `library/crc32c_arm64.c`) |
| `patterns/simd-sort.md` | vDSP / `std::sort` / Highway — not an x86 sort repo |
| `patterns/library-version-upgrade.md` | OS Accelerate / libSystem / Compression vs bundled dylibs |
| `patterns/main-thread-blocking.md` | Sync I/O on main, `Task { sync }`, `+load`, watchdog `0x8badf00d` |
| `patterns/disk-and-dirty-memory.md` | 16 KiB dirty pages, WAL, `F_BARRIERFSYNC`, `XCTStorageMetric` |
| `patterns/swiftui-update-cost.md` | Long `body`, `Observable()`, stored escaping closures |
