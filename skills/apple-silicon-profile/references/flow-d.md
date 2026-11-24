<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Flow D: execution steps — AMP scaling (S / P / E, QoS)

Use this flow when a workload does not scale well as thread count
increases: adding more threads produces little or no throughput gain, or
performance actually regresses.

Apple Silicon is an **asymmetric** multiprocessor: Super (S), Performance
(P), and Efficiency (E) cores, in chip-specific combinations. All cores
share the ISA and the same memory; threads may move between types. You
**cannot pin a thread to P vs E**. There is no `taskset` for core type.
Software hints with QoS; the OS decides.

Record `hw.nperflevels` and `hw.perflevel{N}.*` at the start of the
session and quote them in every table. Never hardcode a 4P+4E M1
topology onto an M4 / M5 machine.

---

## Phase 0 — Optional: threading pre-check and scaling sweep

Before function-level profiling, a scaling sweep can reveal whether the
workload is actually threaded and where its scaling ceiling is. The
threading check runs immediately (static inspection); the actual sweep
is offered to the user.

### Step 0a — Threading inspection (always run)

```bash
otool -L <binary> | grep -E 'libdispatch|libsystem_pthread|libomp|libiomp|libtbb|GCD'
nm -U <binary> 2>/dev/null | grep -E 'pthread_create|dispatch_async|omp_|std::thread' | head
```

Also check source if available:

```bash
grep -rE 'OMP_NUM_THREADS|pthread_create|dispatch_async|#pragma omp|std::thread|qos_class' \
    <source_dir> 2>/dev/null | head -20
```

Summarize — e.g. *"Binary links libdispatch (GCD) and libsystem_pthread"*
or *"No threading library found; source shows no pthread/GCD/OMP."*

Look specifically for **static partitioning** (`#pragma omp for
schedule(static)`, hand-rolled equal chunks, one task per core with a
barrier). On AMP that is `patterns/amp-work-partition.md`.

### Offer the sweep (informed by Step 0a)

> *"[Threading summary from Step 0a.] A scaling sweep (1 thread, P/S-count,
> all-logical, plus a few intermediate points) can show where performance
> caps out — and the CPU Profiler traces will be reused for Phase 1, so
> we will not need to run the workload again. I cannot pin P vs E; I will
> set QoS (`QOS_CLASS_USER_INTERACTIVE` vs `BACKGROUND`) and record it.
> This takes a few minutes — shall I run it, or go straight to
> function-level profiling?"*

If the workload appears unthreaded, note that explicitly:

> *"The binary doesn't appear to link a threading library. The sweep will
> likely confirm flat scaling — but it can still rule out environmental
> threading (Accelerate/OpenBLAS auto-threads, GCD in a dependency).
> Shall I run it anyway?"*

If the user declines, skip to Phase 1.

### Step 0b — Sweep and profile collection (after user agrees)

Run the workload at a bisecting sequence of **thread counts**, not pinned
CPU sets.

Start with: **1, 2, P, N/2, N** where

```bash
P=$(sysctl -n hw.perflevel0.physicalcpu)   # fastest core type count
N=$(sysctl -n hw.logicalcpu)
```

Remove duplicates for small N. If the scaling factor between two adjacent
points drops unexpectedly (< 0.80 / extra thread of ideal), insert a
midpoint and re-run.

**QoS is part of the configuration.** Default the sweep to
`QOS_CLASS_USER_INTERACTIVE` so the OS prefers S/P. Then, at N threads,
run one contrast with `QOS_CLASS_BACKGROUND` (E-preferred). If the
program has no QoS hook, say so and offer a two-line patch:

```c
#include <pthread/qos.h>
pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
```

or GCD:

```c
dispatch_queue_attr_t attr = dispatch_queue_attr_make_with_qos_class(
    DISPATCH_QUEUE_CONCURRENT, QOS_CLASS_USER_INTERACTIVE, 0);
```

**For each thread count**, run once for throughput numbers **without**
Instruments (xctrace adds overhead that would skew the sweep):

```bash
./<command> --threads <T> 2>&1 | grep -E '<metric_pattern>'
```

**Additionally, at the three anchor points (1, P or N/2, N), collect a
CPU Profiler trace** so Phase 1 can reuse them:

```bash
xcrun xctrace record --template 'CPU Profiler' \
  --output cpu_1t.trace --launch -- ./<command> --threads 1
xcrun xctrace record --template 'CPU Profiler' \
  --output cpu_Pt.trace --launch -- ./<command> --threads "$P"
xcrun xctrace record --template 'CPU Profiler' \
  --output cpu_Nt.trace --launch -- ./<command> --threads "$N"
```

If the 1-vs-N throughput ratio is < 1.05 (within 5%), the workload is
almost certainly not threaded. Report this clearly and **do not continue
the sweep** — there is nothing for this flow to diagnose until threading
is enabled.

Record occupancy if the environment exposes it (some Instruments /
`powermetrics` views show perflevel residency). If available, quote
"fraction of samples on perflevel0 vs perflevel1". If not, do not invent
it — QoS class + topology is the record.

### Step 0c — Format the sweep table

```markdown
## Scaling sweep

Topology: perflevel0=`Performance` × 8, perflevel1=`Efficiency` × 4,
`hw.logicalcpu`=12. QoS=`QOS_CLASS_USER_INTERACTIVE` unless noted.

| Threads | Score   | Score factor | Scaling factor | QoS |
|---------|---------|--------------|----------------|-----|
|       1 | 12.3 X  |      1.00×   |          —     | UI  |
|       2 | 22.1 X  |      1.80×   |      0.80×/t   | UI  |
|       8 | 67.2 X  |      5.46×   |      0.55×/t   | UI  |
|      12 | 71.0 X  |      5.77×   |      0.29×/t   | UI  |
|      12 | 18.4 X  |      1.50×   |          —     | BG  |
```

- **Score factor** = Score(T) / Score(1)
- **Scaling factor** = (Score(T) – Score(prev)) / (T – prev) / Score(1)
  — per-thread marginal gain, normalized to the single-thread score. A
  value well below 1.0 indicates Amdahl-limited, lock-contended, or
  AMP-imbalanced behaviour.

Note the **inflection point** — the thread count at which the scaling
factor first drops below **0.80**. An inflection scaling factor < 0.80
is still a useful diagnostic even when the curve never goes linear.
This is the primary target for Phase 1 profiling.

The BACKGROUND row at N threads is a QoS contrast: if UI and BG scores
at the same T differ a lot, the work is sensitive to S/P vs E, and
static equal chunks will fail harder.

> *"Scaling inflection: threads ≤ T give near-linear gains; beyond T,
> gains become sub-linear. Running Phase 1 dual-profile at 1 vs N —
> traces already in `cpu_1t.trace` and `cpu_Nt.trace`."*

---

## Phase 1 — Dual-profile collection

Run **Building block: Ensure debug symbols** on the binary **before**
Phase 1 recording. Dual-profile traces must use the same binary as any
later annotate pass — recompiling after recording invalidates them.

**If Phase 0 was completed**, `cpu_1t.trace` and `cpu_Nt.trace` are
already available — skip straight to the Dual-profile comparison
building block. Only re-record if the binary was recompiled for debug
symbols after Phase 0.

Use **Building block: Dual-profile comparison**.

Identify the **jumper** functions using the building-block thresholds
(rank rose ≥ 3, % ratio ≥ 2×, absolute gain ≥ 3%, or new entrant ≥ 2%).
**Jumper functions are still the right target on AMP** — they are how
contention and serial remnants show up.

**If the delta is overwhelming** (more than ~7 functions jump): offer
the midpoint profile (P-count vs N, or N/2 vs N) to get a shorter list.

Always print QoS and the perflevel card above the delta table.

---

## Phase 2 — Source collection for jumping functions

For each jumper identified in Phase 1:

### 2a. Collect the source

Use **Top-N lines within a function** to find the hot lines, and collect
the full source using Flow C display rules (≤ 20 lines: full body; > 20:
signature + context around hot lines).

### 2b. Check for inlining

```bash
xcrun xctrace show cpu_Nt.trace \
  --track 'CPU Profiler' --calltree 'Profile' \
  --include-source-info --format markdown | grep -A3 "<function_name>"
llvm-objdump -d -C --no-show-raw-insn <binary> | grep -A2 "<function_name>"
```

If inlined, decide which function is the true candidate:

| Inlinee size | Priority |
|--------------|----------|
| **Small helper** (≤ ~10 lines, wraps a single atomic or lock) | The **outer** function is the candidate — fix the calling pattern |
| **Non-trivial** (loop, decision tree) | The **inlinee** is the candidate |

Collect source for both; note the priority in the report.

---

## Phase 3 — Focused contention scan

Run **Building block: Contention scan**, then filter the presentation to
the jumper functions from Phase 2. This replaces "focused c2c".

### 3a. Collect fail density + layout

ATOMIC_OR_EXCLUSIVE_* if the picker has them (`references/named-events.md`).
A missing picker name means the event is not on this chip — skip it. Do
not invent encodings. 128 B layout dump of every struct the jumpers touch.

### 3b. Filter to jumping functions

Ignore contended lines whose accessors are not jumpers — the goal is to
link the scaling bottleneck to specific 128 B lines, not to do a general
contention audit.

```markdown
## Focused contention — lines touched by jumping functions

| Object | Fail density Δ | Accessor jumpers | Sharing type |
|--------|----------------|------------------|--------------|
| `stats` +0x00/+0x08 | 0.01% → 1.8% | `bump_writes`, `read_flag` | False sharing |
| `queue.head` +0x00 | 0.00% → 0.9% | `push`, `pop` | True sharing |
```

If a jumping function does not appear in any hot line and has no atomic
fail density, note it: the bottleneck may be compute, branch-miss, or
AMP static partition — revisit with **Flow B** or the static-chunk check
in Phase 4.

System Trace / Points of Interest: use when jumpers are
`ulock_wait` / `kevent` / GCD rather than user atomics.

---

## Phase 4 — Categorize and diagnose

For each jumping function and its associated hot line (if any), classify:

| Pattern | Signals | Resolution strategy |
|---------|---------|---------------------|
| **False sharing** | Jumper writes one field; another thread writes a different field on the same **128 B** line | `patterns/false-sharing.md` |
| **TAS / CAS spin** | `ldxr`/`stxr` or `cas` retry in a loop; names contain `lock`/`spin`/`try_lock`. LSE **fail density** rises with N. Annotate pattern scan will name it. | `patterns/ttas.md` **and** `patterns/spin-wait.md`. **Block, don't spin** except sub-scheduling-quantum waits. Prefer `os_unfair_lock` / GCD / pthreads. |
| **True sharing — statistics** | Contended field is a counter (`ldadd`, `++`); no compare-exchange loop | `patterns/per-cpu-stats.md` |
| **True sharing — data** | Contended field is genuine shared data | Atomics (LSE), finer locks, GCD serial queue, R/W. See Flow C. |
| **AMP static partition** | `#pragma omp for schedule(static)`, equal chunks, one barrier per phase; scaling dies between P and N; BACKGROUND QoS is much slower at same T | `patterns/amp-work-partition.md`. Dynamic queues, GCD, **≥ 3× tasks per core**. |
| **QoS / E-core parking** | UI vs BG at same T differs a lot; occupancy (if recorded) sits on perflevel1 | Set QoS on the hot path (`pthread_set_qos_class_self_np` / GCD QoS). Do not spin waiting for a P-core. |
| **Cross-cluster sharing** | Thread count > cores in one cluster (`hw.perflevel{N}.cpusperl2`); modified data ping-pongs | Shrink shared modified working set; shard; pack without false sharing. Hierarchy ballpark (~10× intra-cluster): `apple-silicon-codegen/references/latency-lookup.md`. |
| **No contention signal** | Function jumps but no fail-density / 128 B suspect | Flow B on that function (compute / branches / SME). |

Document the diagnosis before applying any fix. If multiple jumpers fall
into different categories, address them in order of dual-profile % Δ
(highest first).

---

## Phase 5 — Apply resolution

Apply the resolution strategy named in the Phase 4 table. Detailed fix
guidance lives in Part 5 of `SKILL.md`, which delegates to
`performance-patterns`.

Follow the "Presenting this to the user" section of the relevant
pattern — show the affected struct or code, explain the problem, propose
the fix, and wait for the user's go-ahead before modifying source.

After the fix is applied, rebuild and run a quick CPU Bottlenecks
recording (Building block: CPU Counters / Bottlenecks) plus the 1-thread
and N-thread throughput points to confirm overall throughput improved.

---

## Phase 6 — Iterate

Multiple bottlenecks almost always exist simultaneously. When one
dominates, it masks the others: fixing it reveals the next one.

**Always iterate:**

1. After applying and verifying a fix, go back to **Phase 1** with the
   updated binary.
2. Collect fresh 1-thread and N-thread profiles (**same QoS**).
3. Identify the new top jumper.
4. Update the Phase 7 report (add the next "Iteration N" section;
   update Solutions).
5. Repeat until either no function jumps by more than the threshold, or
   the workload scales acceptably.

The remaining-bottleneck order may change after each fix. Do not assume
the first Phase 1 list is still valid.

**When iteration is complete**, offer a final scaling sweep (only if
Phase 0 was done):

> *"All identified bottlenecks have been addressed. Since we ran a
> scaling sweep earlier, I can run the same sweep on the optimized
> binary (same thread counts, same QoS) to measure the total
> improvement. Shall I do that before generating the report?"*

If the user agrees, re-run the same points from Phase 0. Record optimized
scores alongside the original — they appear as a second column in the
Phase 7 table.

Then proceed to **Phase 7 — Final report**.

---

## Phase 7 — Final report

Generate the report after the **first** completed iteration and update
it after each subsequent one — don't wait until all iterations are done.

After each iteration: add the next "Iteration N" section, update
Solutions (promoting `Proposed:` to `Implemented:` as fixes land), and
reprint or save.

Print the current report to the terminal after each update, then offer
to save:

> *"Report printed above. Save to `scaling-report-<workload>-<YYYYMMDD>.md`?"*

Only offer to save when running interactively (not when invoked as a
sub-flow).

**If a graphing skill is available**, note it after printing the sweep
table:

> *"A graphing skill is available — ask me to plot the scaling curves if
> you'd like a chart."*

Do not offer this note if no graphing skill is present, and do not
generate CSV unprompted. If the user asks for a graph, use the graphing
skill if available; otherwise generate a CSV:

```
threads,qos,baseline_score[,optimized_score]
1,UI,12.3
2,UI,22.1
...
```

### Report template

Use this template exactly. Omit sections that have no data (e.g. omit
the scaling table if Phase 0 was skipped; omit "Optimized" columns if
no second sweep was done).

````markdown
# AMP scaling report — <workload>

Topology (sysctl, not assumed):
- `hw.nperflevels` = …
- perflevel0 `name`/`physicalcpu`/`cpusperl2`/`l2cachesize` = …
- perflevel1 … (and further levels if present)
- `hw.logicalcpu` = …, `hw.cachelinesize` = 128, `hw.pagesize` = 16384

QoS in force unless a row says otherwise: `QOS_CLASS_USER_INTERACTIVE`.
Cannot pin P vs E.

## Scaling sweep
<!-- Only include if Phase 0 was performed -->

| Threads | Score (baseline) | Score factor | Scaling factor | QoS | Score (optimized) | Improvement |
|---------|------------------|--------------|----------------|-----|-------------------|-------------|
|       1 | 12.3 X           |       1.00×  |             —  | UI  | 18.1 X            |      +47%   |
|       2 | 22.1 X           |       1.80×  |    0.80×/t     | UI  | 34.5 X            |      +56%   |
|      12 | 71.0 X           |       5.77×  |    0.29×/t     | UI  | 148.2 X           |     +109%   |
|      12 | 18.4 X           |       1.50×  |             —  | BG  | 40.0 X            |     +117%   |

Omit the "Score (optimized)" and "Improvement" columns if no post-fix
sweep was done.

<!-- If graphing skill available and user asked for a graph, embed it -->

## Iteration <N> — Key functions
<!-- One such section per iteration; N starts at 1 -->

### Dual-profile table

<!-- Phase 1 delta table for this iteration; bold the jumper rows -->

| N-thread rank | Function | N-thread % | baseline rank | baseline % | Rank Δ | % Δ |
|-------------|----------|----------|---------------|------------|--------|-----|
| **1** | **`os_unfair_lock_lock`** | **12.3%** | **8** | **2.1%** | **▲7** | **+10.2%** |

### Jumper function details

One subsection per jumper. Use Flow C annotation conventions
(≤ 20 lines: full body; > 20: summarize around hot lines).

Annotate the hot line with the contention marker:

| Bottleneck type | Marker |
|-----------------|--------|
| False sharing / true sharing | `/* ◄ cache contention */` |
| Lock / CAS / LDXR spin | `/* ◄ lock CAS */` |
| Atomic counter increment | `/* ◄ atomic counter */` |
| Static AMP partition / barrier | `/* ◄ static partition */` |

#### `<function_name>` — `path/to/file.c:<line>`

```c
void long_function(struct foo *s)
{
    ...
    s->hit_count++;   /* ◄ atomic counter */
    ...
}
```

### Diagnostic results

Summarize Phase 3–4 for this iteration: which 128 B lines were hot,
fail-density Δ, sharing type, QoS contrast, and which resolution
strategy was selected.

## Solutions

One subsection per proposed or implemented fix, across all iterations.

### Proposed: <short description>   <!-- or: Implemented: <short description> -->

- What was changed (struct, function, file, QoS, OpenMP schedule)
- Why it addresses the bottleneck
- Code diff or before/after snippet
- Verification (fail-density drop, throughput improvement, sweep table)

Update the subsection header from `Proposed:` to `Implemented:` once
the fix is applied.
````
