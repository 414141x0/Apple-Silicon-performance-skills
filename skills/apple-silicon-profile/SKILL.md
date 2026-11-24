---
name: apple-silicon-profile
description: >-
  Profile and fix Apple Silicon / Darwin performance problems using Xcode
  Instruments and `xctrace`. Workflows: (A) CPU Bottlenecks / CPU Counters —
  Useful, Discarded, Delivery, Processing fractions, Critical L1D miss;
  (B) CPU Profiler hotspots — cycle PMI, functions and source lines, ISA
  signals; (C) contention — no perf c2c: dual-profile, ATOMIC_OR_EXCLUSIVE_FAIL
  density, System Trace / Points of Interest, 128 B layout, Thread Sanitizer;
  (D) AMP scaling — S/P/E cores, QoS, cannot pin core type, dynamic vs static
  partition; (E) structured hotspot report with annotated source and pattern
  observations; (F) SME engine — SMSTART/SMSTOP, ZA, FMOPA, streaming mode,
  cluster frequency drop, core↔engine bounce; (G) MetricKit in-process
  payloads; (H) XCTest measure / xcodebuild; (I) app xctrace — hangs, hitches,
  launch, Allocations, File Activity, Processor Trace, SwiftUI; (J) crash /
  jetsam / watchdog CLI (`atos`, `xcrun crashlog`, dSYM). Trigger on:
  Instruments, xctrace, CPU Counters, CPU Bottlenecks, CPU Profiler, Time
  Profiler, Processor Trace, MetricKit, XCTest, XCTCPUMetric, hang, hitch,
  launch time, dSYM, atos, crashlog, 0x8badf00d, jetsam, hotspot, profile,
  IPC, Useful bandwidth, false sharing, scaling, P-core, E-core, S-core, AMP,
  SME, SMSTART, why is this slow, where does time go, does not scale, cache
  miss, branch mispredict. When in doubt, invoke this skill — better to use
  it unnecessarily than to miss a performance opportunity.
---

<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Apple Silicon profiling skill

Guide the user through profiling on Apple Silicon / Darwin — from setup and
data collection through reporting and interpretation — using Xcode Instruments
and `xcrun xctrace`.

The skill is organized into five parts:

- **Part 1: Setup** — Developer Tools, signing, `-g` + dSYM, Rosetta, which
  instrument to use (always check these first)
- **Part 2: Flows** — end-to-end workflows A–J
- **Part 3: Cross-skill integrations** — when to delegate to
  `phoronix-test-suite`, `performance-patterns`, `apple-silicon-codegen`
- **Part 4: Building blocks** — focused data-collection primitives
- **Part 5: Resolution strategies** — named patterns in `performance-patterns`

Do **not** invent PMU event encodings. When a named event is required, pick
names from `references/named-events.md` in the CPU Counters picker. A missing
picker name means the event is not on this chip — skip it. Do not invent
encodings. Per-chip cache sizes: query `sysctl` (`sysctl.md`). Class
latencies: `apple-silicon-codegen/references/latency-lookup.md`. Family
placement: `chip-families.md`. Never hardcode them.

---

# Part 1: setup

## Developer Tools and Instruments

```bash
xcode-select -p
xcrun --find xctrace
xcrun xctrace version
xcrun xctrace list templates
xcrun xctrace list instruments
open -a Instruments
```

- `xcode-select -p` must point at an Xcode (or full Command Line Tools +
  Instruments) install that ships `xctrace`. If it does not, run
  `sudo xcode-select -s /Applications/Xcode.app/Contents/Developer` (or the
  Xcode-beta path) and retry.
- Instruments.app is the GUI. `xcrun xctrace` is the CLI for record / export /
  show / toc / symbolicate.
- Some PMU events and Processor Trace need a **Development-signed** Instruments
  session, a Development-signed target, Developer Mode, and (for on-device
  work) a paired device. There is **no** `/proc`, **no**
  `perf_event_paranoid`, and **no** `sudo` protocol for `perf`. If a recording
  fails with a signing, entitlement, or "kpc" / PMU permission error, tell the
  user what Instruments asked for and stop — do not invent a Linux-style sudo
  workaround.

## Detect Rosetta — do this before any recording

```bash
sysctl -n sysctl.proc_translated
```

- **0** — this process is native arm64. Proceed.
- **1** — this process is running under Rosetta. **STOP.** Rebuild the binary
  for arm64 (`clang -arch arm64 …`, Xcode `arm64` slice, not `x86_64`).
  Profiling translated code is not an Apple Silicon session: the PMU, ISA, and
  hotspots describe the translator, not the user's algorithm.

If the user launched a terminal that is itself translated, every child is
translated. Check the shell too:

```bash
sysctl -n sysctl.proc_translated; file "$(which <binary>)"
```

`file` must show `arm64` (or `arm64e`), not `x86_64`.

## Always compile with `-g` AND keep `-O2`/`-O3`

Debug info does not weaken optimization. A Release profile session without
`-g` yields addresses, not source lines.

```bash
clang -O2 -g -mcpu=apple-m4 -o <output> <source>
dsymutil <output>
```

- Keep the user's optimization flags. Only **add** `-g`. Do not drop `-O3`,
  do not add `-O0`, do not change `-mcpu`.
- After linking, generate a dSYM: `dsymutil <binary>`. Instruments and `atos`
  need it when the debug info is not left in the binary.
- Target a chip family, never an Arm version: `-mcpu=apple-m1` (or
  `apple-m2` / `apple-m3` / `apple-m4` / `apple-m5`). Confirm names with
  `clang --print-supported-cpus`. Query ISA features with `sysctl`; never
  hardcode them.

If given a pre-built binary without `-g`, offer to recompile. If you cannot,
use **Building block: resolve address to source** (Part 4).

## Which instrument to use

| Instrument | What it is | Use it for |
|------------|------------|------------|
| **Time Profiler** | Wall-clock, simultaneous sampler across CPUs. Sample rate is **not** adjusted for frequency or residency. | "What is the app doing?" overview only. **Not** for ISA or microarch work. |
| **CPU Profiler** | Cycle-based PMI, independent per CPU. | ISA / hotspot work. Default for Flows B, D, E. |
| **CPU Counters** | PMU counting and sampling. | Named events from `references/named-events.md` via the picker. |
| **CPU Bottlenecks** | guided CPU Counters mode (Instruments 26+). Four fractions of sustainable μop bandwidth (`references/named-events.md`). | Flow A. Default `selectedCountingMode` of the CPU Counters template on current Xcode. |
| **Processor Trace** | Hardware control-flow reconstruction. Floor: M4 / A18 / iPhone 16 / iPad Pro M4; iOS 18.4+ / macOS 15.4+; Instruments 16.3+; <1% overhead; dSYM required. | Exact call sequences, ARC / C++ synthesized frames. Flow I. |
| **System Trace** / **Points of Interest** | Scheduling, syscalls, `os_signpost`. | Flow C / D when threads block, migrate, or wait. |
| **App Launch** / **Animation Hitches** / **Time Profiler** | Wall-clock / frame / first-draw. | Flow I. Not for ISA. |
| **Allocations** / **File Activity** / **SwiftUI** | Heap, writes, view-body cost. | Flow I. |
| **Thread Sanitizer** | Xcode Thread Sanitizer. | Correctness of sharing; use alongside Flow C, not as a substitute for a profile. |

**Rule:** if the question is "where do cycles go in this loop / this ISA
sequence?", use **CPU Profiler** (and CPU Counters / Bottlenecks). If the
question is "why is the UI hitching / hanging / slow to launch?", Flow I
(App Launch / Animation Hitches / Time Profiler). Mixing them up produces
the wrong answer.

Release `DEBUG_INFORMATION_FORMAT` is `dwarf-with-dsym`. `dsymutil` after
`clang -g`. Flow J owns `atos` / `xcrun crashlog`.

---

# Part 2: flows

Choose a flow based on what the user needs:

| Flow | Best for | Time needed |
|------|----------|-------------|
| **Flow A** — CPU Bottlenecks / Counters | Useful / Discarded / Delivery / Processing; is it algorithm, branches, fetch, or memory? | Seconds |
| **Flow B** — CPU Profiler hotspots | Which functions and source lines consume cycles | Minutes |
| **Flow C** — contention | False sharing (128 B), true sharing, atomic/exclusive fail density. No HITM sampler. | Minutes |
| **Flow D** — AMP scaling | Workload does not get faster (or gets slower) as threads increase; S/P/E, QoS | 30+ min |
| **Flow E** — hotspot analysis report | Structured deliverable: top functions + annotated source with % column + pattern observations | Minutes |
| **Flow F** — SME engine | SMSTART/ZA/FMOPA, cluster frequency drop, core waiting on the engine | Minutes |
| **Flow G** — MetricKit | Shipping-device CPU/hang/hitch/launch/memory/disk JSON | Code + daily payload |
| **Flow H** — XCTest measure | Repeatable CPU/clock/memory/storage baselines via `xcodebuild test` | Minutes |
| **Flow I** — app xctrace | Hangs, hitches, launch, Allocations, File Activity, Processor Trace, SwiftUI | Minutes |
| **Flow J** — crash / jetsam / watchdog | `.ips` / `.crash` / jetsam JSON; `atos` / `crashlog` / dSYM | Minutes |

When in doubt, start with Flow A — it is fast and often answers the question
without a full recording. If Flow A says **Useful is high and the code is
still slow**, the bottleneck is the algorithm or the ISA choice, not the
pipeline: hand off to `performance-patterns` / `apple-silicon-codegen` rather
than collecting more counters. App-level hang/hitch/launch/crash questions
start at G–J, not A.

---

## Flow A: CPU Bottlenecks / CPU Counters

Best for a fast efficiency answer: is the core doing useful work, throwing
work away on mispredicts, starved of instructions, or stuck processing?

Use **Building block: CPU Counters / Bottlenecks** (Part 4).

The four fractions (Useful / Discarded / Delivery / Processing) and the
deeper Critical L1D miss rows live in `references/named-events.md`. High
Useful + still slow → algorithm / ISA, not more counters. Memory-bound is
combined Critical L1D miss, not a Linux `cache-misses/refs` ratio.

Read `references/flow-a.md` for the execution steps.

---

## Flow B: CPU Profiler hotspots

Use this when the user wants to know *which functions* consume cycles, not
just aggregate fractions.

Time Profiler is allowed only as a "what is the app doing" overview. The
recording that feeds annotation, pattern scan, and Flow E **is CPU Profiler**.

Read `references/flow-b.md` for the execution steps.

---

## Flow C: Contention (no `perf c2c`)

Use to diagnose **false sharing** or **true sharing** of 128-byte cache lines
in multi-threaded code. Apple Silicon has no HITM sampler and no `perf c2c`.

**Trigger this flow when:**

- User mentions multithreaded scalability problems
- Flow B shows repeated access to the same small structures (not streaming
  over large arrays)
- `os_unfair_lock`, `pthread_mutex`, `std::atomic`, `LDADD`, `CAS`,
  `ldxr`/`stxr` appear in hot functions or call chains
- User mentions cache-line contention, false sharing, or atomic fail density
- Dual-profile (Flow D) shows jumper functions around atomics or small
  shared structs

**Threshold:** ignore ATOMIC_OR_EXCLUSIVE_FAIL densities that do not grow
with thread count, and ignore layout suspects whose dual-profile % Δ is
below **2** percentage points, unless the user overrides.

Read `references/flow-c.md` for the execution steps.

---

## Flow D: AMP scaling (S / P / E, QoS)

Use when a workload does not scale with thread count — throughput plateaus
or regresses as threads increase.

**You cannot pin a thread to P vs E.** There is no `taskset` for core type.
The OS picks the core; software hints with QoS
(`pthread_set_qos_class_self_np`, GCD QoS classes). Every sweep point must
record the QoS class in force.

**Trigger this flow when:**

- User says "adding more threads doesn't help" or "performance gets worse
  with more cores"
- A scaling sweep (throughput vs thread count) shows a flat or declining
  curve
- Flame graph / CPU Profiler shows lock, GCD, or atomic functions dominating
  at high thread counts
- Static OpenMP / equal-chunk partitioning on an S/P/E machine

Read `references/flow-d.md` for the execution steps.

---

## Flow E: Hotspot analysis report

Use when the user wants a structured, formatted deliverable rather than an
exploratory session — a document they can read, share, or file.

**Trigger this flow when:**

- User asks for a "hotspot report", "profiling report", or "analysis report"
- User wants the top functions with annotated source and percentages
- User wants a formatted, shareable summary of where time is spent

The source of `%` is CPU Profiler / `xctrace show` / `xctrace export`, never
Time Profiler.

Read `references/flow-e.md` for the execution steps.

---

## Flow F: SME engine

Use when the profile or the source shows SME / SSVE / streaming mode, or
when a cluster's frequency drops while a sibling is in SM/ZA.

**Trigger this flow when:**

- Disassembly or source contains `SMSTART`, `SMSTOP`, `ZA`, `FMOPA`,
  `SMSTART ZA`, streaming-SVE loads/stores, or `#include <arm_sme.h>`
- CPU Counters picker shows SME engine events retiring (`INST_SME_ENGINE_*`
  family — pick names from `references/named-events.md`. A missing picker
  name means the event is not on this chip — skip it. Do not invent encodings)
- Core is waiting on the engine (picker names such as
  `CORE_WAITING_SME_ENGINE_CYCLE` if present)
- Matmul / conv / outer-product on M4+ / A18+, or a cluster-wide frequency
  drop coincident with SM/ZA

Read `references/flow-f.md` for the execution steps.

Hand off the actual kernel rewrite to `patterns/sme-engine-bounce.md` and to
`apple-silicon-codegen` (`references/sme-engine.md`, `references/sme-ssve.md`).

---

## Flow G: MetricKit in-process collection

Use when the user needs **shipping-device** metrics (CPU, hang, hitch,
launch, memory, disk writes, terminations) without Instruments.
MetricKit does **not** deliver on Simulator. `mxSignpost` (not
`OSSignposter`) for interval metrics; `trackLaunchTask` for work after
first draw.

Read `references/flow-g.md`.

---

## Flow H: XCTest performance tests

Use for a CI-able baseline of a kernel or feature. Measure **Release**:
debug executable off, coverage off, sanitizers off.
`XCTCPUMetric` / `XCTClockMetric` / `XCTMemoryMetric` /
`XCTStorageMetric` / `XCTOSSignpostMetric`. Thread Performance Checker
on for tests, **off** for `xctrace`.

Read `references/flow-h.md`.

---

## Flow I: App-level xctrace

Hangs (main run loop busy >250 ms; stacks at ≥1 s), hitch rate
(good ≤10 ms/s), launch (App Launch template), Allocations, File
Activity, SwiftUI, Processor Trace (M4+ / iOS 18.4+ / macOS 15.4+,
<1% overhead). Always `xctrace list templates` first.

Read `references/flow-i.md`.

---

## Flow J: Crash / jetsam / watchdog CLI

`DEBUG_INFORMATION_FORMAT=dwarf-with-dsym`, `xcrun crashlog`,
`CrashSymbolicator.py`, `atos -arch arm64 -o DWARF -l loadaddr`.
Match `0x8badf00d`, PAC, zombies, missing dylibs, jetsam
`rpages * pageSize` (16 KiB pages).

Read `references/flow-j.md`.

---

# Part 3: cross-skill integrations

## `phoronix-test-suite`: PTS benchmarks

**Invoke `phoronix-test-suite` immediately** — the moment the profiling
target is identified as a `pts/<name>` benchmark, before any Instruments
step.

### Trigger

- **Workload is `pts/<name>`** (any Phoronix Test Suite benchmark) → invoke
  `phoronix-test-suite` before Phase 0 of any flow.

### What it handles

The `phoronix-test-suite` skill owns the full lifecycle for PTS benchmarks:
install, source extraction, rebuild (with `-g` and `-mcpu=apple-*`), binary
deployment, and result recording. It knows the correct `install.sh` build
flags, source layout, and where to copy the resulting binary — things a
manual `clang` or `make` invocation will get wrong.

**Do not attempt to install, build, or rebuild a PTS test manually.** Always
delegate to this skill.

When the trigger fires, say: *"I'll invoke the `phoronix-test-suite` skill to
handle install and rebuild before profiling."* then invoke it — before
running any `xctrace` command.

---

## SIMD / ISA anti-patterns → `performance-patterns`

**Invoke `performance-patterns` as early as possible** — the moment you see
any of these, even from reading source or looking at Flow A fractions. Do
not wait for disassembly.

### Early triggers (source or Bottlenecks — no annotate needed)

- **Serial accumulator** in source (`s += a[i] * b[i]`, `sum += x[i]`,
  running max/min) → always a SIMD / parallel-accumulator opportunity
- **High Useful + still slow** and the loop is scalar → algorithm / ISA
  choice; invoke patterns and possibly codegen
- **`FMLA` / `FMOV` / `CSEL` / `LDADD` / `SMSTART`** in source or asm
- **Accelerate / vDSP / BLAS already in the call tree** → do **not** rewrite
  the kernel; see `patterns/platform-libs-first.md`

### Later triggers (from Flow B / annotate)

- Scalar FP in a hot loop (`FADD`/`FMUL`/`FADDS` without `.4S` / `.2D` /
  `FMLA`)
- GPR↔vector moves (`FMOV`, `UMOV`, `INS`) on the hot path
- Taken-branch density approaching 1 in 8 instructions; `B.cond` where
  `CSEL`/`CSET` would do
- `LDXR`/`STXR` exclusive loops where LSE `LDADD`/`CAS` exists
- `SMSTART` / `SMSTOP` around a dependent round-trip with scalar/ASIMD code

When any trigger fires, say: *"I'll now invoke the `performance-patterns`
skill to analyze and optimize the hot loop."* then invoke it — before doing
further manual assembly analysis yourself.

---

## New ISA-level code / sysctl / SME structure → `apple-silicon-codegen`

**Invoke `apple-silicon-codegen`** when the work is writing or restructuring
ISA / SME / sysctl code, not just spotting an anti-pattern in existing
source.

### Trigger

- New kernel that needs ASIMD / SSVE / SME ISA structure
- `sysctlbyname` feature dispatch, cache-block sizes, page/line sizes
  (`sysctl.md`, `chip-families.md`)
- SME routine structure: ZA tiles, streaming mode, 4 KiB isolation, no
  core↔engine bounce — `apple-silicon-codegen` (`references/sme-engine.md`,
  `references/sme-ssve.md`)
- Latency lookup: `apple-silicon-codegen/references/latency-lookup.md` for
  the current chip's class latencies; do not paste tables
- Apple ABI / PAC / DIT (`X18`, `va_list`, `ptrauth.h`, `timingsafe.h`)

When the trigger fires, say: *"I'll invoke `apple-silicon-codegen` for the
ISA / SME / sysctl structure."* then invoke it.

---

# Part 4: building blocks

Detailed command syntax, flags, and output formats for each primitive. Read
`references/building-blocks.md` when you need to execute one.

| Building block | Purpose |
|----------------|---------|
| `Check CPU capabilities` | `sysctl hw.optional.arm.FEAT_*`, `hw.nperflevels`, `hw.perflevel{N}.*`, line/page size, Rosetta; used by every other block |
| `Ensure debug symbols` | `dsymutil`, `llvm-dwarfdump`, `atos`; always `-g` in Release profile sessions |
| `CPU Counters / Bottlenecks` | Record Useful / Discarded / Delivery / Processing; attach, launch, export |
| `CPU Profiler record` | Cycle PMI `.trace`; contrast Time Profiler |
| `Disassembly annotate` | CPU Profiler detail / `xctrace show --source`; `otool -tv` / `llvm-objdump -d -C` |
| `resolve address to source` | `atos -arch arm64 -o binary -l loadaddr` |
| `Contention scan` | ATOMIC_OR_EXCLUSIVE_* in the picker (`references/named-events.md`), 128 B layout dump, dual-profile. Replaces `perf c2c`. |
| `Top-N functions / Top-N lines` | Ranked list from a `.trace` via `xctrace show` / `xctrace export` |
| `Dual-profile comparison` | 1 thread vs `hw.logicalcpu` (and P-count); **must** record QoS; cannot `taskset` to P vs E |
| `Annotate pattern scan` | AArch64 mnemonics: FADD vs FMLA.4S, FMOV/UMOV, CSEL vs B.cond, LDADD vs LDXR, SMSTART/FMOPA, taken-branch density, serial FADDS |
| `Branch probability measurement` | `tools/branchprob.py` (Clang PGO / CSV / `--from-annotate`) + `tools/gccbranchprob.py` + Clang `-fprofile-instr-generate` |
| `MetricKit subscribe + JSON` | Flow G. Device only. |
| `XCTest measure / xcodebuild` | Flow H. Release, no coverage, no sanitizers. |
| `App xctrace templates` | Flow I. `list templates` then App Launch / Hitches / Allocations / File Activity / SwiftUI / Processor Trace. |
| `Crash symbolicate` | Flow J. `xcrun crashlog`, `CrashSymbolicator.py`, `atos`. |

---

# Part 5: resolution strategies

The resolution strategies are owned by the **`performance-patterns` skill**.
When a flow identifies a named pattern, invoke `performance-patterns` — it
will load `triggers/from-profile.md` to match the signal, then the
appropriate `patterns/<name>.md` for the full fix.

Apple names (not `vzeroupper`):

| Resolution strategy | `performance-patterns` pattern file |
|---------------------|-------------------------------------|
| TTAS / block-not-spin (block idle threads; do not spin except sub-quantum waits) | `patterns/ttas.md` and `patterns/spin-wait.md` |
| Width ladder ASIMD → SSVE/SME | `patterns/simd-upconversion.md` |
| Parallel accumulator rewrite | `patterns/parallel-accumulator.md` |
| False sharing 128 B + pack hot variables onto the fewest lines | `patterns/false-sharing.md` |
| Per-CPU / per-thread statistics aggregation | `patterns/per-cpu-stats.md` |
| GPR↔vector moves (`FMOV`/`UMOV`/`INS`) | `patterns/gpr-vector-moves.md` |
| Taken branch / `CSEL`/`CSET` | `patterns/taken-branch-and-csel.md` |
| SME engine bounce (core↔engine dependent data) | `patterns/sme-engine-bounce.md` |
| AMP static partition | `patterns/amp-work-partition.md` |
| Platform libraries first (Accelerate, vDSP, BLAS, `memcpy`, GCD) | `patterns/platform-libs-first.md` |
| Main-thread I/O, hang, hitch, watchdog, `Task.detached` | `patterns/main-thread-blocking.md` |
| Dirty 16 KiB pages, disk writes, WAL, `F_BARRIERFSYNC` | `patterns/disk-and-dirty-memory.md` |
| SwiftUI long `body` / update storm | `patterns/swiftui-update-cost.md` |

---

# Reference

## Useful commands

```bash
# topology / ISA — never hardcode
sysctl hw.optional.arm.FEAT_SME hw.optional.arm.FEAT_DotProd \
       hw.optional.arm.FEAT_I8MM hw.optional.arm.FEAT_BF16 \
       hw.optional.arm.FEAT_LSE hw.optional.armv8_crc32 \
       hw.nperflevels hw.cachelinesize hw.pagesize \
       hw.physicalcpu hw.logicalcpu sysctl.proc_translated

# Instruments CLI
xcrun xctrace list templates
xcrun xctrace list instruments
xcrun xctrace record --template 'CPU Profiler' --launch -- ./binary
xcrun xctrace record --template 'App Launch' --launch -- ./App.app/Contents/MacOS/App
xcrun xctrace record --template 'Animation Hitches' --attach App --time-limit 20s
xcrun xctrace record --template 'Processor Trace' --launch -- ./binary   # M4/A18+
xcrun xctrace toc recording.trace
xcrun xctrace show recording.trace --track 'CPU Profiler' --calltree 'Profile' --format markdown
xcrun xctrace export recording.trace --toc
xcrun xctrace symbolicate --input recording.trace --dsym binary.dSYM

# symbols / disassembly
dsymutil ./binary
atos -arch arm64 -o ./binary -l <loadaddr> <addr>
xcrun crashlog ./report.ips
otool -tv ./binary
otool -L ./binary
llvm-objdump -d -C --no-show-raw-insn ./binary

# coarse Darwin samples (not for ISA work)
sample <pid> 10 -file /tmp/sample.txt
sudo fs_usage -w -f filesys <pid>

# XCTest / dSYM
xcodebuild test -scheme App -configuration Release -enableCodeCoverage NO
xcodebuild … DEBUG_INFORMATION_FORMAT=dwarf-with-dsym
```

Always `xcrun xctrace list templates` / `list instruments` before assuming a
template name. **CPU Bottlenecks** is a guided counting mode of the
**CPU Counters** template on Instruments 26+ (confirm with
`--show-recording-options`); it may also appear as a GUI template.

Event names vary by chip family. Pick names from `references/named-events.md`
in the CPU Counters picker. A missing picker name means the event is not on
this chip — skip it. Do not invent encodings.
