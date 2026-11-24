<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Performance Patterns — Design Document

This file records the architectural decisions for the `performance-patterns` skill.
Update it whenever a structural decision is made or revised.

---

## Goals

- Provide a single skill that takes a user from symptom (profile signal or bad
  source code) all the way to a working fix — without requiring other skills to
  be installed.
- Grow gracefully: adding a new pattern should require no structural changes.
- Keep context load minimal: agents load only what is relevant to their current
  situation (profile data vs. source code; detection vs. implementation).
- Prefer platform libraries (Accelerate, vDSP, GCD, libSystem, Compression)
  before any custom ISA work.

---

## Directory layout

```
performance-patterns/
├── SKILL.md                    ← routing logic only; no pattern content
├── design.md                   ← this file
├── triggers/
│   ├── from-profile.md         ← pattern identification from profiling output
│   └── from-source.md          ← pattern identification from source code
├── guidelines/
│   └── new-code.md             ← write-time checklist for new performance-sensitive code
├── patterns/
│   ├── <name>.md               ← detection + root cause + fix (or fix summary)
│   ├── <name>-impl.md          ← full implementation guide (only when fix > ~150 lines)
│   └── tests/                  ← optional microbenchmarks for a pattern
├── library/
│   └── <capability>.md / .c    ← reusable implementation modules (shared across patterns)
└── references/
    └── <index>.md              ← version tables, algorithm name indexes
```

---

## Three-tier loading

1. **`SKILL.md`** — always in context; contains only the routing decision.
   Three contexts: load `triggers/from-profile.md`, `triggers/from-source.md`,
   or `guidelines/new-code.md` based on whether the agent has profiling data,
   is reviewing existing code, or is writing new code.
2. **Trigger / guideline file** — loaded once to identify which pattern applies
   (trigger files) or to get the write-time checklist (guideline file). Separated
   by context so agents with profile data never load source-code triggers and
   vice versa.
3. **Pattern file + optional impl file** — demand-loaded after a pattern is
   identified. The pattern file always covers detection and root cause. The
   impl file is loaded separately only when the agent is about to write the fix.

The `guidelines/new-code.md` file is a write-time checklist that cross-references
`patterns/` files for rationale. It is the inverted form of the patterns catalog:
the same knowledge reformatted as "do this from the start" rather than "here's
what's wrong and how to fix it".

---

## Pattern file anatomy

Every `patterns/<name>.md` must contain these sections, in order:

1. **When to apply** — source code signals and/or profiling signals
2. **Why this is slow** — root cause; motivates the fix without being a tutorial
3. **The fix** — step-by-step resolution with code examples
4. **Verification** — how to confirm the fix worked
5. **Presenting this to the user** — before/after structure, what to highlight

---

## Two-file split rule

Keep detection and fix in **one file** until the fix section alone would exceed
roughly **150 lines**. At that point, split:

```
patterns/<name>.md        ← detection + root cause + fix *summary* + link to impl
patterns/<name>-impl.md   ← full implementation: algorithms, code templates, edge cases
```

The impl file lives alongside the pattern file (not in a separate directory) so
the relationship is obvious. An agent doing detection reads `<name>.md` and
frequently never needs `<name>-impl.md`. An agent writing the fix loads it
on demand.

---

## Two classes of pattern

**Class 1 — Self-contained**
Fix is explanation + code snippets that fit in the main file. Single-file
structure. No split unless the pattern grows substantially.

**Class 2 — Complex implementation**
Detection is compact; implementation is a multi-step algorithm (hundreds of
lines). Use the two-file split. The `<name>.md` carries detection and a
self-contained quick fix for simple cases; `<name>-impl.md` carries the full
algorithm, edge cases, and dispatch wiring.

There is no Class 3. This skill is self-contained: SIMD promotion, CRC32C, and
runtime dispatch live here. Write-time ISA catalogs stay in
`apple-silicon-codegen` so they are not duplicated.

### Current class map

| Pattern | Class | Notes |
|---------|-------|-------|
| `parallel-accumulator.md` | 1 | Scalar 4-acc + ASIMD templates in-file; SME is a pointer |
| `simd-upconversion.md` | 2 | Width ladder in `simd-upconversion-impl.md` |
| `missing-restrict.md` | 1 | |
| `false-sharing.md` | 1 | 128 B lines; dual of pack-hot-vars |
| `ttas.md` | 1 | Default is *block*, not spin |
| `per-cpu-stats.md` | 1 | 128 B buckets; thread-group sharding |
| `cold-path-annotation.md` | 1 | |
| `cv-thundering-herd.md` | 1 | GCD first |
| `mutex-to-rwlock.md` | 1 | `pthread_rwlock` / GCD concurrent queue |
| `library-version-upgrade.md` | 1 | Darwin dylibs, not ELF `.so` |
| `fast-crc32c.md` | 2 | `fast-crc32c-impl.md` + `library/crc32c_*.c` |
| `simd-sort.md` | 1 | Accelerate / `std::sort` / Highway |
| `gpr-vector-moves.md` | 1 | Keep a value in one file |
| `taken-branch-and-csel.md` | 1 | CSEL/CSET over unpredictable `B.cond` |
| `fused-mac.md` | 1 | FMLA / MADD / `vfmaq` |
| `amp-work-partition.md` | 1 | Dynamic queue, ≥3× tasks/core, QoS |
| `spin-wait.md` | 1 | Block idle threads |
| `sme-engine-bounce.md` | 1 | No core↔engine ping-pong; 4 KiB isolation |
| `platform-libs-first.md` | 1 | Accelerate / simd / GCD / memcpy first |
| `weakly-ordered-memory.md` | 1 | DMB/DSB or acquire/release |
| `sysctl-hardcoding.md` | 1 | Never hardcode 64 B / 4 KiB / core count |
| `store-forwarding.md` | 1 | Avoid hard-to-predict STLF |
| `unaligned-access.md` | 1 | Prefer unaligned loads over stores |
| `software-prefetch.md` | 1 | PRFM sparingly; recode for HW prefetcher |
| `jit-icache.md` | 1 | Avoid frequent I-cache invalidation |

---

## The `library/` directory

Reusable implementation modules that apply to **two or more patterns**. A library
file is a standalone how-to guide — not tied to any single pattern's detection
story. Pattern files (or impl files) reference library files when they need the
shared capability.

### Current library

| File | Purpose | Used by |
|------|---------|---------|
| `library/cpu-dispatch.md` | Runtime dispatch: `sysctl` `FEAT_*` + function pointers | simd-upconversion, parallel-accumulator, fast-crc32c, known-algorithms |
| `library/crc32c_arm64.c` | ARM CRC32CX multi-accumulator CRC32C | fast-crc32c |
| `library/crc32c_portable.c` | ISA-neutral table CRC32C | fast-crc32c fallback |

### Adding a library module

Create a library module when:
- The same implementation technique is needed by two or more patterns
- The technique is generic enough that it could apply to future patterns not yet written
- Writing it inline in each pattern file would create duplication that would diverge

Name the file after the technique, not the pattern: `cpu-dispatch.md` not
`simd-dispatch.md`. Add it to the table above and reference it from the relevant
pattern files.

### Current references

| File | Purpose |
|------|---------|
| `references/library-versions.md` | Known Darwin dylib / framework symbols with a better platform implementation; used by library-version-upgrade |
| `references/known-algorithms.md` | Compact index: algorithm name → common function names. Inlined into trigger files so detection costs no extra file load |
| `references/known-algorithms-impl.md` | Per-algorithm implementation details: ASIMD / DotProd / I8MM / SME, dispatch guards. Demand-loaded only after a function name match |

### Adding a reference file

Create a reference file when data needs to grow independently of pattern
logic — versioned library lists, algorithm catalogs, CPU feature tables.
Name the file after what it indexes, not the pattern that reads it.

---

## Pattern class note: platform library before custom ISA

A first-class pattern class, not a footnote. Rec 2 is
High/High: explore Accelerate, vDSP, BLAS, `memcpy`, GCD, Compression, and
simd before writing `arm_neon.h` or SME. `patterns/platform-libs-first.md` is
the detection playbook. `guidelines/new-code.md` item 2 is the write-time form.
Custom ASIMD / SME is correct only after that check fails.

---

## Cross-skill handoff

`apple-silicon-profile` owns data collection (Instruments, `xctrace`, CPU
Bottlenecks, CPU Profiler, CPU Counters) and hotspot identification. When it
identifies a known pattern, it invokes `performance-patterns`.

`apple-silicon-codegen` owns write-time ISA (integer, ASIMD, SSVE/SME,
microarch, `references/latency-lookup.md`, `references/sysctl.md`,
`references/chip-families.md`). Pattern files may point at it; they must not paste
ISA catalogs.

Do not send the agent to `linux-perf` for Apple silicon work.

---

## Trigger file maintenance

Each trigger file (`triggers/from-profile.md`, `triggers/from-source.md`)
contains:
1. A **quick-match table** — one row per pattern, key signal → file
2. A **description paragraph** per pattern — 4–8 sentences, enough to confirm
   a match without opening the pattern file

When adding a pattern, both trigger files must be updated. The tables are the
agent's primary lookup — keep them tight and scannable.
