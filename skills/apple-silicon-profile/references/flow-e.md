<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. Do not copy Guide tables. MIT license. -->

# Flow E: execution steps — Hotspot analysis report

Produce a structured, formatted hotspot report: a top-functions table
followed by per-function annotated source with a percentage column. The
output is a self-contained Markdown document.

The source of `%` is **CPU Profiler** / `xctrace show` / `xctrace export`.
Do **not** use Time Profiler weights for this report — they are
wall-clock samples, not cycle PMI.

---

## Phase 0 — Ensure debug symbols

Run **Building block: Ensure debug symbols** on the binary before
recording. If the binary lacks DWARF and the user agrees to recompile,
do so now — before Phase 1 — so the recording and annotation use the
same binary. If the user declines, note the limitation; source-unavailable
fallback format applies in Phase 4a.

Run **Check CPU capabilities**. If `sysctl.proc_translated` is `1`,
**STOP**.

---

## Phase 1 — Record

Use **Building block: CPU Profiler record**. Note the wall-clock duration
of the recording — it determines whether to run CPU Bottlenecks in
Phase 2.

If a `cpu.trace` file already exists, do not ask if you know it is valid
based on the session:

> *"I found an existing cpu.trace file. Should I use it, or re-record?"*

Call-tree inversion is not required (percentages come from
`--source`, not from caller chains), but if the user has already recorded
with CPU Profiler, reuse it.

```bash
xcrun xctrace record \
  --template 'CPU Profiler' \
  --output cpu.trace \
  --target-stdout - \
  --launch -- ./<binary> <args>
```

---

## Phase 2 — CPU Bottlenecks (conditional)

| Condition | Action |
|-----------|--------|
| CPU Bottlenecks / Counters already collected in this session | Reuse those numbers — do not re-run |
| Recording wall-clock duration < 60 s | Run CPU Bottlenecks now using **Building block: CPU Counters / Bottlenecks** |
| Recording duration ≥ 60 s | Skip — note its absence in the report header |

**If Bottlenecks data is available**, format it as a compact summary
table followed by one sentence describing the performance regime:

```markdown
## System-level summary

| Metric | Value |
|--------|-------|
| Useful | 41% |
| Discarded | 12% |
| Delivery | 9% |
| Processing | 38% |
| Critical L1D miss (while executing + critical) | 29% |
| CPU time (CPU Profiler) | 4.21 s |

*The workload is **memory-bound** (Processing dominated by Critical L1D
miss while executing + Critical L1D miss).*
```

Regime sentence rules:

- Critical L1D miss (while executing) + Critical L1D miss are the
  dominant Processing rows → "memory-bound (critical L1D miss)"
- Useful high, miss rows low, Discarded low, user still says slow →
  "compute-bound at high Useful — algorithm / ISA, not microarch"
- Discarded dominant → "branch / mispredict bound (Discarded)"
- Delivery dominant → "fetch-bound (Delivery: I-cache/I-TLB/taken-branch
  density)"
- `libsystem_kernel` / `kevent` / `mach_msg` > 30% in the function table
  → "I/O- or syscall-bound (dominant kernel time)"
- Otherwise → "mixed; see fractions"

Do **not** quote raw IPC as the headline metric. Useful is μPC / max μPC
for this core type. Do not compare Useful across chip families.

---

## Phase 3 — Top functions table

Use **Building block: Top-N functions** to get the ranked function list.

**Selection cutoffs** (apply the first rule that fires):

- Default maximum: **5 functions**; user may override (e.g. "show me 7"
  → use 7)
- Stop when the next function would be **below 5%**
- Stop when the **cumulative percentage has already reached or exceeded
  95%** — but always include the function that tips it over 95%

**Location column**: show the function's **definition line** (not the
hottest line). Obtain from `xctrace show --include-source-info` or
`atos`. If unavailable, show `<binary>` or `<stripped>`.

**Percentage**: round to nearest whole percent. Use **self** weight
(cycles in this function's own instructions), not total-with-callees,
unless the user asked for inclusive time.

```markdown
## Top functions

| Rank | Function     | Location      | % |
|------|--------------|---------------|---|
|    1 | `foo(int)`   | src/foo.c:91  | 79% |
|    2 | `bar(void)`  | src/bar.c:21  | 19% |
```

---

## Phase 4 — Per-function detailed sections

For each function in the top table, produce a named section with
annotated source.

### Step 4a — Get annotated source

For each function, take its CPU Profiler node ID from Phase 3 and run:

```bash
xcrun xctrace show cpu.trace \
  --track 'CPU Profiler' \
  --calltree 'Profile' \
  --source <node-id> \
  --source-mode source
```

This outputs source lines with per-line weights that are **relative
within the function** (they sum to approximately 100%). Use the
source-line percentages for the report.

Also collect disassembly for Observations (4e):

```bash
xcrun xctrace show cpu.trace \
  --track 'CPU Profiler' \
  --calltree 'Profile' \
  --source <node-id> \
  --source-mode interleave
```

If `show --source` fails, XML export is a last resort (`xctrace export
--xpath` on the schema `toc` lists). Prefer opening the `.trace` in
Instruments and transcribing the detail view over parsing a 30k-line XML
dump.

If source lines are not available (stripped binary or missing `-g`),
show the annotated **assembly** instead — using the same 6-char `%`
prefix format. Add a note above the block:

> ⚠️ *Binary has no debug symbols — assembly view shown. Source is
> available at `<path>` if known.*

Assembly fallback format: show the hot inner loop(s) with a few
instructions of context before and after. Omit cold setup/teardown
preamble entirely. Use `...` (6-space prefix) to represent omitted
sections. Add a brief `;` comment per instruction to name what it does
(load, FMA, store, loop back, etc.) — this replaces the source-line
context that would otherwise be visible.

Example assembly fallback (AArch64, not x86):

```asm
      ; <function_name> — inner loop (source unavailable)
      ...
   36%  :   0x1000045c0:  ld1.4s  {v0}, [x1], #16     ; load C[j]
   44%  :   0x1000045c4:  fmla.4s v0, v2, v3          ; C += a_val * A
   14%  :   0x1000045c8:  fmadd   s0, s1, s2, s0      ; scalar leftover (bad)
    5%  :   0x1000045cc:  st1.4s  {v0}, [x0], #16     ; store C[j]
         :   0x1000045d0:  subs    x2, x2, #1
         :   0x1000045d4:  b.ne    0x1000045c0        ; loop back
      ...
```

### Step 4b — Short vs long function

Count the number of **source lines** in the function body (excluding
blank lines and comments if ambiguous).

- **≤ 20 source lines**: show the **entire function** — no omissions,
  no `...`
- **> 20 source lines**: apply the summarization strategy in Step 4c

### Step 4c — Summarization strategy (functions > 20 lines)

Show a condensed view that keeps the most relevant context and hides
the rest.

**Always show:**

1. **Function signature and opening brace** (no percentage needed even
   if % > 0 there)
2. **Variable definitions** for any variable that appears in a hot line
   — helps the reader understand the hot expression without scrolling
3. **Closing `}`**

**For each hot line (≥ 5% within-function), show:**

- 1–2 lines of context **before** the hot line
- The hot line itself
- 1–2 lines of context **after** the hot line
- If the hot line is inside a loop, show the **loop header** as well —
  loops amplify the per-iteration cost and are essential context

**Merging**: if the context windows of two adjacent hot regions overlap,
merge them into a single continuous block (no `...` between them).

**Omitted code**: represent with a single `...` line (6-space prefix,
no percentage).

### Step 4d — Percentage column format

Apply this prefix to **every line shown** (hot lines, context lines, and
the function header and footer):

```
[pos 1–4: right-aligned integer][pos 5: % sign][pos 6: space][source line as-is]
```

Rules:

- Percentage is the **within-function** share of cycles for that source
  line
- Round to the **nearest whole number** (0.5% rounds up to 1%)
- Lines with **< 0.5%** (would round to 0) show **6 spaces** — no
  number, no `%`
- The `%` sign is always at position 5; numbers are right-aligned
  against it:
  - 1% → `    1% ` (3 spaces + digit + `%` + space)
  - 12% → `   12% ` (2 spaces + digits + `%` + space)
  - 99% → `   99% ` (1 space + digits + `%` + space)
- `...` omission lines get 6 spaces (`      ...`)
- After the 6-char prefix, the source line appears **exactly as in the
  source file**, preserving its original indentation

Example (long function, summarized):

```c

      void foo(int a)
      {
          int loop;
          double counter = 0;
          ...
          for (loop = 1; loop < a; loop++) {
              ...
   4%         do_something_else();
  95%         counter += exp(memory[loop]);
              ...
          }
          ...
      }

```

Example (short function, ≤ 20 lines, shown in full):

```c

      void bar(void)
      {
          int loop;
          double counter = 0;

          for (loop = 1; loop < 5000; loop++) {
 100%          counter += exp(memory[loop]);
          }
      }

```

### Step 4e — Observations

Run **Building block: Annotate pattern scan** on the interleaved
assembly from Step 4a. Present each detected pattern as a bullet —
include the **Pattern** name and **Evidence**; omit the Suggested RS
column (this is a reporting flow, not a prescriptive one).

If no patterns are detected, omit this section.

**Do not prescribe fixes.** Name the pattern only. If the user wants to
act on an observation, they can invoke the appropriate flow or
resolution strategy separately.

---

## Phase 5 — Output and save

Print the complete report to the terminal in Markdown.

**Save prompt** — only if this flow was invoked directly by a user (not
called from another flow or agent). After printing, ask:

> *"Would you like to save this report to `hotspot-report-<appname>.md`?"*

If yes, write the file. If the session is non-interactive (invoked
programmatically), skip this prompt entirely.

---

## Report template

````markdown
# Hotspot report for `<appname>`

Hotspot analysis was performed using CPU Profiler (cycle PMI) on:
`<full command line>`

QoS: `<QOS_CLASS_… or unset>`.
`sysctl.proc_translated` = 0 (native arm64).
Chip card: perflevel0=…, `hw.logicalcpu`=…, `hw.cachelinesize`=128.

## System-level summary

| Metric | Value |
|--------|-------|
| Useful | XX% |
| Discarded | XX% |
| Delivery | XX% |
| Processing | XX% |
| CPU time | X.XX s |

*<one-sentence regime description>*

---

## Top functions

| Rank | Function     | Location        | %   |
|------|--------------|-----------------|-----|
|    1 | `<function>` | `<file>:<line>` | XX% |
|    2 | `<function>` | `<file>:<line>` | XX% |

---

### Function 1: `<function>` detailed report

```c

      <annotated source with % column>

```

**Observations:**
- <pattern, if any>

---

### Function 2: `<function>` detailed report

```c

      <annotated source with % column>

```

---
````
