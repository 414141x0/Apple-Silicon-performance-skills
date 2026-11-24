<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. Do not copy Guide tables. MIT license. -->

# Flow A: execution steps — CPU Bottlenecks / CPU Counters

Best for a fast efficiency answer. Seconds, not minutes. Do this before a
full CPU Profiler session unless the user already knows the hot function.

---

## Phase 0 — Capability card and debug symbols

Run **Building block: Check CPU capabilities**. If
`sysctl.proc_translated` is `1`, **STOP** and rebuild native arm64.

`-g` is not required for Bottlenecks fractions (they are core-wide), but
run **Ensure debug symbols** anyway if you expect to continue into Flow B
on the same binary.

---

## Phase 1 — Record

Use **Building block: CPU Counters / Bottlenecks**.

Prefer the GUI **CPU Bottlenecks** template (Instruments 26+) or the
**CPU Counters** template with `selectedCountingModeDisplayName: CPU
Bottlenecks` (the current default — confirm with
`--show-recording-options`).

```bash
xcrun xctrace record \
  --template 'CPU Counters' \
  --output counters.trace \
  --target-stdout - \
  --launch -- ./<binary> <args>
```

Repeat with `-r`-style discipline: if the workload is < 2 s, run it
enough times that the fractions stabilize, or lengthen the workload.
Do not compare a 50 ms run to a 20 s run.

---

## Phase 2 — Read the four fractions

From the CPU Bottlenecks track (GUI) or `xctrace show` / `toc` (CLI),
fill:

| Fraction | What it is (paraphrase of Guide 8.1.1) |
|----------|----------------------------------------|
| **Useful** | Retired work: measured μPC / **max μPC for this core type**. High means the pipeline is busy with correct-path work. |
| **Discarded** | Bandwidth wasted on the wrong predicted path, plus recovery. Mispredicts and pipeline restarts. |
| **Delivery** | Instruction Delivery could not keep the processing window full (I-cache, I-TLB, taken-branch density, short sequential streams). |
| **Processing** | The window is full: execution is slower than sustainable bandwidth (ILP, memory, long-latency chains). |

Deeper modes (chip-dependent; offered on newer families — use them if
Instruments shows them, skip if not):

| Deeper row | Meaning |
|------------|---------|
| Delivery latency / from I-cache / from I-TLB | Fetch misses. Locality of hot functions and pages. |
| Delivery bandwidth | Too many taken branches, calls, returns — streams shorter than the fetch width. |
| Executing | Doing some work, but not enough parallelism to fill the core. |
| **Critical L1D miss while executing** | Oldest load/store is an L1D miss, **and** the core still found a μop to run. |
| **Critical L1D miss** | Oldest load/store is an L1D miss, and **nothing** issued. |
| Non-critical L1D miss | Younger load missed; not the oldest. |
| Execution latency | Nothing issued; critical path is compute (or L1D-hitting loads). |
| SME engine track | Workload used the engine; more modes may be available → Flow F. |

**Memory-bound** on Apple Silicon is the **sum** of Critical L1D miss
while executing **and** Critical L1D miss — not a Linux
`cache-misses/refs` ratio, and not raw IPC.

---

## Phase 3 — Interpret (mandatory)

After presenting the table, always add an interpretation section. Apply
whichever rows match. Do **not** treat raw IPC as the goal. Do **not**
compare Useful fractions across chip families: sustainable μop bandwidth
rises, so the same software can look "less Useful" on a newer family
while running faster.

| Signal | What it means | What to do next |
|--------|---------------|-----------------|
| **High Useful + still slow** | The core is retiring a large fraction of *its* peak. The algorithm (or the ISA choice) does too much work per result. | Not a microarch problem. More work per instruction: vectorize, change the algorithm, use Accelerate. Hand off to `performance-patterns` and/or `apple-silicon-codegen`. |
| **High Discarded** | Control flow or memory dependence is hard to predict. The core throws away speculated work. | Branches: straighten the hot path, `CSEL`/`CSET`/`FCSEL` for value selects, cut taken-branch density. Danger when taken branches approach **1 in 8 instructions**. `patterns/taken-branch-and-csel.md`. Confirm with `tools/branchprob.py`. |
| **High Delivery** | Fetch/decode cannot fill the window. | I-cache / I-TLB locality: pack hot functions into a small set of pages and lines; inline / unroll / straighten the common path so fetch is sequential. For **cold** code, `-Os` (C) / `-Osize` (Swift) — not `-O3`. |
| **High Processing** | Window full. Either not enough ILP, or memory. | Split using the deeper rows. **Critical L1D miss while executing + Critical L1D miss** dominant → memory-bound: layout, 128 B false sharing, cache-block to `hw.perflevel*.l1dcachesize` / `l2cachesize`, sequential strides for the hardware prefetcher. Execution-latency dominant → dependency chains: parallel accumulators, unroll. |
| **Critical L1D miss (while executing) high, Useful not** | Memory-bound with some overlapping work. | Data layout, 128 B line isolation, prefetch only after the access pattern is prefetcher-friendly. Flow C if multithreaded. |
| **High kernel / `libsystem_kernel` / `mach_msg` / `kevent`** (from a quick Time Profiler overlay, or CPU Profiler if you already have it) | Syscall / I/O / blocking. Bottlenecks fractions may look "fine" because the core is asleep. | Reduce syscall frequency, batch I/O, `kevent` coalescing, GCD. Not an ISA problem. |
| **SME engine track active** | Engine is in use. | Flow F. Do not optimize the core path in isolation. |
| **`INST_BRANCH` / `INST_ALL` near ~1/8** | Taken-branch density collapsing fetch (rec 23). | Unroll, fall-through, `CSEL`. `patterns/taken-branch-and-csel.md`. |
| **`LDST_OLDEST_MTE_TAG_CHECK_CYCLE` hot (M5)** | Tag checks on the oldest access. | Dense platform allocations (rec 49); not a homemade tag pool. |
| **No single dominant fraction** | Mixed. | Report the raw numbers and continue to Flow B. |
| **Useful high, miss rows low, Discarded low, still slow** | Algorithm. | `patterns/platform-libs-first.md` first (Accelerate/vDSP/BLAS). Then parallel-accumulator / simd-upconversion / codegen. |

When using named branch events, **`INST_BRANCH_COND` is not the same
counter across families** (M3: `B.<cond>` only; M4 adds CBZ/TBZ; M5 adds
`BC.<cond>`). Do not compare it M3 vs M5 as a mix-density. Details:
`building-blocks.md`.

Discarded on M4+ / A18+ is accounted more accurately than on earlier
families; the same source may show a **larger** Discarded fraction on M4
than on M3. Do not panic-compare across families.

---

## Phase 4 — Pattern matching → `performance-patterns`

Do not wait for disassembly if the fraction plus the source already name
a pattern.

| If you see… | Invoke |
|-------------|--------|
| Serial `sum += a[i]*b[i]` / running min/max, Useful high | `patterns/parallel-accumulator.md`, `patterns/simd-upconversion.md` |
| Source full of `if (x) y = a; else y = b;` and Discarded high | `patterns/taken-branch-and-csel.md` |
| Multithreaded, Processing + L1D miss, small shared structs | Flow C → `patterns/false-sharing.md` |
| `SMSTART` / ZA / matmul on M4+ | Flow F → `patterns/sme-engine-bounce.md` |
| Already calling `vDSP_*` / `cblas_*` | `patterns/platform-libs-first.md` — stop rewriting |
| Static OpenMP chunks, AMP machine | Flow D → `patterns/amp-work-partition.md` |
| `FMOV`/`UMOV` in a kernel you already dumped | `patterns/gpr-vector-moves.md` |

When a pattern fires, say: *"I'll invoke `performance-patterns` for
&lt;name&gt;."* and invoke it. If the next step is writing new ISA
structure rather than applying a named fix, invoke `apple-silicon-codegen`
instead.

---

## Phase 5 — Handoff rules

| Outcome | Next |
|---------|------|
| Question answered (e.g. "memory-bound, here is why") | Stop. Present the table + interpretation. |
| High Useful + slow, source is a hot loop | `performance-patterns` / codegen. Skip more counters. |
| Need the function name | Flow B (CPU Profiler). |
| Need sharing vs scaling | Flow C / Flow D. |
| SME track on | Flow F. |
| User asked for a shareable document | Flow E (record CPU Profiler; reuse these Bottlenecks numbers in the header). |
