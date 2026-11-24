<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Building blocks

Read this file when executing any building block referenced from Part 2 flows.

## Table of contents

- [Check CPU capabilities](#building-block-check-cpu-capabilities)
- [Ensure debug symbols](#building-block-ensure-debug-symbols)
- [CPU Counters / Bottlenecks](#building-block-cpu-counters--bottlenecks)
- [CPU Profiler record](#building-block-cpu-profiler-record)
- [Disassembly annotate](#building-block-disassembly-annotate)
- [Resolve address to source](#building-block-resolve-address-to-source)
- [Contention scan](#building-block-contention-scan)
- [Top-N functions / Top-N lines](#building-block-top-n-functions--top-n-lines)
- [Dual-profile comparison](#building-block-dual-profile-comparison)
- [Annotate pattern scan](#building-block-annotate-pattern-scan)
- [Branch probability measurement](#building-block-branch-probability-measurement)
- [GCC / Clang static branch probability](#building-block-gcc--clang-static-branch-probability)
- [Useful commands cheatsheet](#useful-commands-cheatsheet)

These are focused, composable primitives that flows and other skills can
invoke directly. They collect and format one specific type of data with no
interpretation overhead beyond the minimum needed to make the numbers
usable.

Never hardcode cache sizes, line size, page size, core counts, or ISA
features. Query `sysctl` (`sysctl.md`). Never invent PMU encodings; pick
names from `references/named-events.md` in the CPU Counters picker. A
missing picker name means the event is not on this chip — skip it. Do
not invent encodings. Class latencies:
`apple-silicon-codegen/references/latency-lookup.md`. Family placement:
`chip-families.md`.

---

## Building block: Check CPU capabilities

Report ISA features, AMP topology, line/page size, and Rosetta state. Cache
the result for the session — the chip does not change between steps.

### Step 0 — Rosetta (hard stop)

```bash
sysctl -n sysctl.proc_translated
```

If this prints `1`, **STOP** the profiling session. Rebuild native arm64.
See Part 1 of `SKILL.md`.

### Step 1 — ISA features and constants

```bash
sysctl hw.optional.arm.FEAT_SME \
       hw.optional.arm.FEAT_DotProd \
       hw.optional.arm.FEAT_I8MM \
       hw.optional.arm.FEAT_BF16 \
       hw.optional.arm.FEAT_LSE \
       hw.optional.armv8_crc32 \
       hw.nperflevels \
       hw.cachelinesize \
       hw.pagesize \
       hw.physicalcpu \
       hw.logicalcpu
```

Notes the agent must not violate:

- A missing key (`sysctl: ...: unknown oid`) means the feature is absent
  on this OS/chip, **not** that you should guess. Treat unknown as 0.
- **FEAT_MOPS is not on M5.** Do not generate MOPS sequences for M5, and
  do not assume the sysctl exists.
- **SSBS is on M1–M3 only.** Do not clear SSBS "for speed" on any family.
- Compile with `-mcpu=apple-m1` (or `apple-m2` / `apple-m3` / `apple-m4` /
  `apple-m5`), never by Arm version (`-march=armv8.4-a` as the primary
  target). List names:

  ```bash
  clang --print-supported-cpus
  ```

### Step 2 — Per-performance-level topology

`hw.nperflevels` is the number of core types. Lower `perflevel` = faster
cores (`perflevel0` is Super or Performance, depending on the chip).

```bash
N=$(sysctl -n hw.nperflevels)
for i in $(seq 0 $((N-1))); do
  echo "----- perflevel$i -----"
  sysctl hw.perflevel$i.name \
         hw.perflevel$i.physicalcpu \
         hw.perflevel$i.logicalcpu \
         hw.perflevel$i.l1dcachesize \
         hw.perflevel$i.l1icachesize \
         hw.perflevel$i.l2cachesize \
         hw.perflevel$i.cpusperl2
done
```

Use these values for cache blocking and for the Flow D sweep points
(1 thread, P/S-count = `hw.perflevel0.physicalcpu`, all-logical =
`hw.logicalcpu`). Do **not** hardcode per-chip sizes; query `sysctl`
(`sysctl.md`).

`hw.cachelinesize` is **128** on current Apple silicon. `hw.pagesize` is
**16384**. Verify; do not assume. The architectural ADRP page is still
4 KiB — that is unrelated to the VM page size.

### Step 3 — Interpret for later blocks

From the output, record a one-screen capability card and reuse it:

```markdown
## CPU capability card

| Item | Value |
|------|-------|
| Rosetta (`sysctl.proc_translated`) | 0 (native) |
| Line size | 128 B |
| VM page size | 16 KiB |
| Logical CPUs | 12 |
| perflevel0 | Performance × 8; L1D/L1I/LL from sysctl |
| perflevel1 | Efficiency × 4; L1D/L1I/LL from sysctl |
| FEAT_SME | 1 (M4+ / A18+) |
| FEAT_LSE | 1 |
| FEAT_DotProd / I8MM / BF16 | as reported |
| Compile target | `-mcpu=apple-m4` (from `clang --print-supported-cpus` + this Mac) |
```

**Vector / matrix width ladder** (replaces SSE→AVX2→AVX-512):

| Tier | Condition | What it means for pattern scan |
|------|-----------|--------------------------------|
| SME ZA + Processing Grid | `FEAT_SME=1` and the algorithm is matmul / outer-product / large tiled dot | Throughput path. SSVE supports; isolate 4 KiB; no core bounce. |
| SSVE-only | `FEAT_SME=1` but the kernel cannot use ZA | Rarely beats 4-wide ASIMD ports on the core. Stay on ASIMD unless measured. |
| ASIMD 128-bit | always (M1 / A14+) | `V0–V31`, `FMLA.4S`, ≤ ~25 live of 32. |
| Scalar | no packed ops in the hot loop | Parallel-accumulator / simd-upconversion candidate. |

Report as a one-liner: *"Width ladder: ASIMD; SME available — use ZA+grid
only if the algorithm is a tile/outer-product."*

---

## Building block: Ensure debug symbols

Check whether the binary (and its dSYM) have DWARF, and if they don't,
offer to recompile with `-g` **before** any expensive Instruments
collection. A CPU Profiler pass on a stripped binary yields addresses,
not source lines.

**Run this building block before `xctrace record` in any flow that will
reach annotation or Flow E.**

### Step 1 — Identify the binary

Full path of the binary to be profiled, from the user's command or from
the installed test directory (for Phoronix:
`~/Library/Application Support/Phoronix Test Suite/installed-tests/pts/<test-name>/`
or `~/.phoronix-test-suite/installed-tests/pts/<test-name>/` — confirm
which the `phoronix-test-suite` skill uses).

### Step 2 — Check for DWARF and a dSYM

```bash
nm -gU <binary> 2>/dev/null | head -5 || echo "stripped-or-no-nm"
dsymutil --dump-debug-map <binary> >/dev/null 2>&1 && echo "dsymutil-ok" || echo "no-debug-map"
xcrun llvm-dwarfdump --verify <binary> 2>&1 | tail -20
ls -d <binary>.dSYM 2>/dev/null || echo "no-dSYM-bundle"
```

- **DWARF present** (`llvm-dwarfdump` lists compile units; `nm` shows
  symbols) → source-line annotation will work. Still run `dsymutil` if
  no `.dSYM` bundle exists, so `atos` and Instruments agree:

  ```bash
  dsymutil <binary>
  ```

- **No DWARF** → note the result and proceed to Step 3.

Also record architecture:

```bash
file <binary>
lipo -info <binary> 2>/dev/null
```

If `file` shows `x86_64` only, this is not an Apple Silicon binary.
Rebuild with `-arch arm64`.

### Step 3 — Offer to recompile (if DWARF absent)

> ⛔ **If the workload is `pts/<name>`, STOP here.** Do not attempt any
> manual recompilation. The `phoronix-test-suite` skill MUST handle the
> rebuild — invoke it now if not already done.

For all other workloads, **check for a build skill first**. If none is
available, ask the user:

> *"The binary has no debug info (`-g` was not used at compile time), so
> CPU Profiler annotation will show assembly addresses only — no source
> line numbers. Would you like me to recompile with `-g` (keeping your
> `-O2`/`-O3` and `-mcpu`) and regenerate the dSYM? (I'll re-record
> after.)"*

If the user declines, note in the final report that source line mapping
is unavailable and proceed.

**If the user agrees, recompile — use a skill if one is available:**

| Priority | Workload type | Recompile approach |
|---|---|---|
| 1 | **Skill available** | Use that skill's compile/rebuild step. |
| 2 | **User-built binary** — no skill | Re-run the exact original build command with `-g` appended. Do **not** change any other flags. Then `dsymutil <binary>`. |
| 3 | **System binary** (e.g. `Safari`, `/usr/bin/python3`) | Source not available. Instruments may still symbolicate system dSYMs if they are on the machine; otherwise report addresses. |

**Critical: only add `-g`, change nothing else.** Changing `-O3` → `-O2`
or removing defines produces a different binary — the profile will not
represent the workload the user cares about.

After recompiling:

```bash
dsymutil <binary>
xcrun llvm-dwarfdump --verify <binary>.dSYM 2>&1 | tail -5
```

### Step 4 — Output

If DWARF was present or has been added: *"Binary has debug symbols and a
dSYM — source line annotation is available."*

If DWARF is absent and the user declined: *"Proceeding without debug
symbols. CPU Profiler will show assembly only; source line numbers will
not be available in the report."*

**Note:** after recompiling with `-g`, any existing `.trace` is stale
(addresses no longer match). The calling flow must re-record before
annotating. `xctrace symbolicate --input old.trace --dsym new.dSYM` is
**not** a substitute when the text of the binary changed.

---

## Building block: CPU Counters / Bottlenecks

Collect the CPU Bottlenecks four-fraction taxonomy (Useful / Discarded /
Delivery / Processing) and, when needed, named PMU events from the
Instruments picker. No interpretation here — Flow A owns that.

### GUI (often easier for Bottlenecks)

1. Open Instruments.app.
2. File → New (or the template chooser at launch).
3. Pick **CPU Bottlenecks** if it is listed (Instruments 26+). Otherwise
   pick **CPU Counters**.
4. Choose the target process, or set the instrument to launch the binary.
5. Record a representative run; stop.
6. In the CPU Bottlenecks (or Counters) track, read the four fractions.
   On chips that support the deeper modes, drill into **Instruction
   Delivery** and **Instruction Processing** (Critical L1D miss while
   executing, Critical L1D miss, Execution latency, SME-engine track).

### CLI — inspect the template before recording

```bash
xcrun xctrace list templates
xcrun xctrace record --template 'CPU Counters' --show-recording-options
```

On current Xcode the CPU Counters template defaults to

```json
"selectedCountingModeDisplayName": "CPU Bottlenecks"
```

with `"countingMode": "bottlenecks"`. If the JSON on this machine shows
a different mode, write a small JSON file that selects bottlenecks and
pass it with `--recording-options` (only keys that `--show-recording-options`
emitted; do not invent keys).

### CLI — launch a command

```bash
xcrun xctrace record \
  --template 'CPU Counters' \
  --output counters.trace \
  --target-stdout - \
  --launch -- ./<binary> <args>
```

### CLI — attach to a running process

```bash
xcrun xctrace record \
  --template 'CPU Counters' \
  --attach <pid-or-name> \
  --time-limit 10s \
  --output counters.trace
```

### CLI — whole-system snapshot (short)

```bash
xcrun xctrace record \
  --template 'CPU Counters' \
  --all-processes \
  --time-limit 5s \
  --output counters-sys.trace
```

Prefer launching or attaching the target over `--all-processes` for ISA
work.

### Named events (manual mode)

Do **not** put encodings on the command line. To count a named event:

1. Pick names from `references/named-events.md` in the CPU Counters
   picker. A missing picker name means the event is not on this chip —
   skip it. Do not invent encodings.
2. In Instruments: CPU Counters → recording options → event picker →
   select the name.
3. Or: `--show-recording-options`, copy the JSON, fill `allEventsAndFormulas`
   using names the picker accepts, pass `--recording-options file.json`.

Useful *names* to look up (picker labels, not encodings; family and
footnotes in `references/named-events.md`):

- Fail / success density: `ATOMIC_OR_EXCLUSIVE_FAIL`,
  `ATOMIC_OR_EXCLUSIVE_SUCC`
- Branches: `INST_BRANCH`, `INST_BRANCH_TAKEN`, `BRANCH_MISPRED_NONSPEC`,
  `BRANCH_COND_MISPRED_NONSPEC`, `INST_BRANCH_COND`
- Mix / taken-branch density: `INST_BRANCH` / `INST_ALL` → rec 23
  (danger near one taken branch in eight)
- ASIMD: `INST_SIMD_ALU`, `INST_SIMD_ALU_VEC`
- SME: `INST_SME_ENGINE_ALU`, `INST_SME_ENGINE_LD`, `INST_SME_ENGINE_ST`,
  `SME_ENGINE_SM_ENABLE`, `CORE_WAITING_SME_ENGINE_CYCLE`
- 4 KiB isolation: `LD_BLOCKED_BY_SME_LDST`
- MTE (M5): `LDST_OLDEST_MTE_TAG_CHECK_CYCLE`,
  `LDST_MEM_ACCESS_CHECKED_X2K`

Footnotes that change ratios (`INST_BRANCH_COND` def change,
`MAP_LDST_UOP` includes SME, `ST_NT` includes SSVE loads, exclusives
undercount M1–M4, `DECODE_UOP` accurate M5+, `LD_SRC_*` M5) live in
`references/named-events.md`. Do not restate them here.

### Export / inspect

```bash
xcrun xctrace toc counters.trace
xcrun xctrace export counters.trace --toc
xcrun xctrace show counters.trace --track 'CPU Counters' --summary 'CPU Bottlenecks' --format markdown
```

Track and detail names vary with Xcode. If `show` errors, run `toc` and
use the track/detail strings it prints. Opening `counters.trace` in
Instruments.app is always valid.

### Output table (agent-facing)

```markdown
## CPU Bottlenecks — <command>

| Fraction | Value | Notes |
|----------|-------|-------|
| Useful | xx% | μPC / max μPC for this core type |
| Discarded | xx% | mispredict / restart waste |
| Delivery | xx% | fetch / decode could not fill the window |
| Processing | xx% | window full: ILP or memory |
| Critical L1D miss while executing | xx% | if the deeper mode is offered |
| Critical L1D miss | xx% | if the deeper mode is offered |
```

Fill only rows the recording actually produced. Do not invent percentages.

---

## Building block: CPU Profiler record

Capture a cycle-PMI recording. All flows that need a `.trace` of *where
cycles go* start here.

**Parameters:**

- **command / workload** — what to profile
- **time limit** — for attach / all-process; omit when `--launch` should
  run until the process exits
- **existing `.trace`** — reuse if the user prefers and the binary has
  not been rebuilt

### Contrast with Time Profiler (do not confuse them)

| | **CPU Profiler** | **Time Profiler** |
|---|------------------|-------------------|
| Mechanism | Cycle PMI, independent per CPU | Wall-clock, simultaneous across CPUs |
| Frequency / residency | Tracks the core that ran the code | **Not** adjusted for frequency or residency |
| ISA / microarch | **Yes** — default for this skill | **No** — Time Profiler is not the ISA tool |
| Use | Flows B, D, E, F; pattern scan | "What is the app doing?" overview only |

```bash
# ISA / hotspot work — THIS one
xcrun xctrace record \
  --template 'CPU Profiler' \
  --output cpu.trace \
  --target-stdout - \
  --launch -- ./<binary> <args>

# Overview only — do not feed this to annotate / Flow E
xcrun xctrace record \
  --template 'Time Profiler' \
  --output time.trace \
  --target-stdout - \
  --launch -- ./<binary> <args>
```

### Attach / time-limit / env / QoS

```bash
xcrun xctrace record \
  --template 'CPU Profiler' \
  --attach <pid-or-name> \
  --time-limit 10s \
  --output cpu.trace

# QoS for a launched C program that reads an env flag you control:
xcrun xctrace record \
  --template 'CPU Profiler' \
  --env QOS=user_interactive \
  --launch -- ./<binary> <args>
```

The CPU Profiler template currently also includes Hangs and Points of
Interest. Leave them; they are cheap context for Flow C/D.

### Inspect recording options (optional)

```bash
xcrun xctrace record --template 'CPU Profiler' --show-recording-options
```

`highFrequency` and `recordKernelCallstacks` default off. Turn kernel
call stacks on only when Flow B Phase 3 suggests
`libsystem_kernel` / `mach_msg` / `kevent` dominance.

### After recording

```bash
open cpu.trace          # GUI
xcrun xctrace toc cpu.trace
xcrun xctrace list runs cpu.trace
```

If symbols are missing in the GUI, symbolicate:

```bash
xcrun xctrace symbolicate --input cpu.trace --dsym <binary>.dSYM
```

Reuse an existing `.trace` whenever the user prefers or the workload is
hard to reproduce.

---

## Building block: Disassembly annotate

Annotate a function's assembly with per-instruction (or per-source-line)
cycle attribution. Use this when you need to know *what the CPU is
executing* inside a hot function.

**Prerequisite:** binary compiled with `-g` and a dSYM. If stripped, use
**resolve address to source** plus `llvm-objdump`.

**N is a parameter:**

- Specified (e.g. "top 5 instructions") → hard limit
- Omitted → accumulate until **cumulative instruction overhead exceeds
  99%**; include the tipping entry

### Step 1 — Prefer CPU Profiler detail (has %)

```bash
xcrun xctrace toc cpu.trace
xcrun xctrace show cpu.trace \
  --track 'CPU Profiler' \
  --calltree 'Profile' \
  --include-source-info \
  --format markdown
```

Find the node ID of the hot function, then:

```bash
xcrun xctrace show cpu.trace \
  --track 'CPU Profiler' \
  --calltree 'Profile' \
  --source <node-id> \
  --source-mode interleave
```

`--source-mode` values: `source` | `disassembly` | `interleave`. Use
`interleave` for pattern scan (source + asm together); `disassembly`
when there is no `-g`.

Track / calltree names come from `toc`. If the template was CPU Profiler,
the track is typically `CPU Profiler` and the call tree is typically
`Profile`. Confirm; do not guess past a `show` error.

### Step 2 — Static disassembly (no %)

When you need mnemonics without a trace, or to confirm a pattern:

```bash
otool -tv -p _<function> <binary>
llvm-objdump -d -C --no-show-raw-insn --disassemble-symbols=_<function> <binary>
```

C++ names: pass the mangled symbol or use `-C` and search the demangled
form. `nm -C <binary> | grep <guess>` helps.

### Step 3 — Format as Markdown

| Overhead | Instruction | Source hint |
|----------|-------------|-------------|
| 35% | `fmla.4s v0, v1, v2` | `dot.c:42` |
| 20% | `fadd s0, s0, s1` | `dot.c:42` |
| 12% | `ld1.4s {v1}, [x0], #16` | `dot.c:38` |

Overhead is within-function share from `--source`. Static `otool` rows
get Overhead `—`.

---

## Building block: resolve address to source

Given a code address, find the corresponding source file and line. Use as
a fallback when CPU Profiler / `xctrace show` does not show source
(stripped binary, inlined code, or a system dylib).

### Load address

`atos` needs the **slide / load address** of the image, not just the
runtime PC.

```bash
# from the trace's process table
xcrun xctrace export cpu.trace \
  --xpath '/trace-toc/run[@number="1"]/processes'

# or, for a live process
vmmap <pid> | head
```

### User-space binary

```bash
atos -arch arm64 -o <binary> -l <loadaddr> <addr> [<addr> ...]
atos -arch arm64 -o <binary>.dSYM/Contents/Resources/DWARF/<name> \
     -l <loadaddr> <addr>
```

- `-arch arm64` is required on universal binaries.
- Multiple addresses in one invocation are fine (batch).
- Inlining: `atos` prints the inlined chain when DWARF has it.

### Stripped binary (no `-g`)

```bash
llvm-objdump -d -C --no-show-raw-insn <binary> | sed -n '/<function_name>:/,/^$/p'
```

Correlate the target address against instruction offsets manually. Offer
to recompile with `-g` if the source is available — it is faster and more
reliable.

### System / kernel frames

Frames in `/usr/lib/system/libsystem_kernel.dylib`,
`libsystem_pthread.dylib`, `libdispatch.dylib` usually symbolicate with
the system dSYM already on the Mac. If they do not, report the dylib
name and offset; do not attempt to fetch kernel debug kits unless the
user asks.

### Prefer other tools when available

If the agent has a language server, debugger, or symbol lookup tool,
prefer those over `atos` for inlining, templates, and multiple CUs.

---

## Building block: Contention scan

Surface cache-line / atomic contention without HITM. Apple Silicon has
no `perf c2c`. This block returns (1) atomic/exclusive fail density if
the picker offers the events, (2) a 128 B layout dump of hot structs,
(3) dual-profile jumpers that implicate sharing.

**N / threshold:** ignore fail-density that does not grow with thread
count; ignore layout suspects whose dual-profile absolute % Δ is below
**2** points unless the user overrides.

### Step 1 — Atomic / exclusive fail density

Pick names from `references/named-events.md` in the CPU Counters picker.
If this chip's picker lists `ATOMIC_OR_EXCLUSIVE_FAIL` and
`ATOMIC_OR_EXCLUSIVE_SUCC` (and `INST_ALL` / `ARM_INST_RETIRED`), select
them. A missing picker name means the event is not on this chip — skip
it. Do not invent encodings.

Record once at 1 thread and once at `hw.logicalcpu` threads, **same QoS**:

```bash
xcrun xctrace record --template 'CPU Counters' \
  --output atomics_1t.trace --launch -- ./<binary> --threads 1

xcrun xctrace record --template 'CPU Counters' \
  --output atomics_Nt.trace --launch -- ./<binary> --threads "$(sysctl -n hw.logicalcpu)"
```

Report:

```
fail density ≈ ATOMIC_OR_EXCLUSIVE_FAIL / INST_ALL
```

A density that **rises** with thread count is contention. A density that
is flat is not. Exclusive-fail interpretation (M1–M4 undercount) is
footnote 5 in `references/named-events.md` — do not treat a zero count as
proof of no LDXR contention.

If the events are absent from the picker, skip Step 1 and rely on
Steps 2–3. Do not invent encodings.

### Step 2 — 128 B layout dump of hot structs

For each struct implicated by a jumper function or by source:

```c
struct hot {
    _Atomic uint32_t written_a_lot;   /* +0x00 */
    uint32_t         innocent;        /* +0x04 */
    _Atomic uint32_t read_a_lot;      /* +0x08 */
};
```

Compute offsets with the compiler, not by guessing:

```bash
clang -c -g -Xclang -fdump-record-layouts <file.c> -o /dev/null
# or
python3 - <<'PY'
import ctypes
# rebuild the struct with ctypes and print field offsets
PY
```

Annotate every field with `/* +0xNN */`. State whether any two
independent hot fields share a **128-byte** line (`offset // 128` equal).
Padding example (int + pad = 128 B):

```c
struct padded {
    _Atomic uint32_t written_a_lot; /* +0x00 */
    char _pad[124];                 /* +0x04 .. +0x7F */
    _Atomic uint32_t read_a_lot;    /* +0x80 */
};
static_assert(alignof(struct padded) >= 128, "line");
/* or: alignas(128) on each independently shared field */
```

### Step 3 — Dual-profile + System Trace / TSan

Run **Building block: Dual-profile comparison**. Jumpers whose names or
disassembly involve atomics, mutexes, GCD, or small shared structs are
contention candidates.

Complementary recordings (optional, when jumpers look like blocking
rather than bouncing lines):

```bash
xcrun xctrace record --template 'System Trace' \
  --output sys.trace --launch -- ./<binary>

# Points of Interest is already in the CPU Profiler template.
# Thread Sanitizer: Xcode → Product → Scheme → Diagnostics → Thread Sanitizer
# (Xcode Thread Sanitizer). TSan is a correctness tool; it is not a cycle profile.
```

### Step 4 — Classify true vs false sharing

**Offsets are the discriminator:**

- Significant accessors share the **same offset** on the 128 B line →
  **true sharing** (same datum).
- Significant accessors use **different offsets** on the same 128 B line →
  **false sharing**.

Cross-cluster modified sharing is expensive: the fabric is on the order
of **10×** intra-cluster Shared-LL latency
(`apple-silicon-codegen/references/latency-lookup.md` hierarchy ballpark).
Independent pools of data are fine across clusters; a hot atomic
ping-ponging across clusters is not.

### Step 5 — Format

| Signal | 1-thread | N-thread | Sharing type | Evidence |
|--------|----------|----------|--------------|----------|
| `ATOMIC_OR_EXCLUSIVE_FAIL` density | 0.01% | 1.8% | (see layout) | grows with N |
| `stats.counters[tid]` | — | jumper +3.1% | false | offsets 0x00 vs 0x08, same line |

---

## Building block: Top-N functions / Top-N lines

Surface the hottest functions, or the hottest source lines inside one
function, from a `.trace`. No interpretation.

**N is a parameter:**

- Specified (e.g. "top 10 functions") → hard limit
- Omitted → accumulate until **cumulative overhead exceeds 99%**; include
  the tipping entry

**Prerequisite for lines:** `-g` + dSYM.

### Step 1 — Record

Use **Building block: CPU Profiler record**. Reuse an existing `.trace`
if the user prefers.

### Step 2 — Functions

```bash
xcrun xctrace toc cpu.trace
xcrun xctrace show cpu.trace \
  --track 'CPU Profiler' \
  --calltree 'Profile' \
  --include-source-info \
  --format markdown
```

If this `xctrace` build advertises a top-functions function on the call
tree (`--list-functions`), use it to collapse recursion / multiple
callers into self+total weight. Otherwise read self-weight from the
call tree (heaviest-stack header plus the tree).

Extract the top rows. Strip system-library noise only when the user asked
for *application* hotspots; never hide `libsystem_kernel` /
`libdispatch` — those are Flow B signals.

### Step 3 — Lines within a function

Take the node ID of the function from Step 2:

```bash
xcrun xctrace show cpu.trace \
  --track 'CPU Profiler' \
  --calltree 'Profile' \
  --source <node-id> \
  --source-mode source
```

Keep lines with a non-zero weight. If fewer than 3 lines have attributed
overhead, include all of them.

### Step 4 — Format

```markdown
## Top functions — `<command>`

| Rank | Overhead | Cumulative | Function | Binary / Object | Location |
|------|----------|------------|----------|-----------------|----------|
| 1 | 35.1% | 35.1% | `dot_product` | `myprogram` | `dot.c:40` |
| 2 | 12.3% | 47.4% | `kevent` | `libsystem_kernel` | — |
```

```markdown
## Hot lines in `<function_name>`

Source file: /absolute/path/to/source.c
Profiled binary: <binary>  |  Event: cycles (CPU Profiler)

| Line | Overhead | Cumulative | Source snippet |
|------|----------|------------|----------------|
|   42 | 35%      | 35%        | `acc = vfmaq_f32(acc, a, b);` |
|   38 | 12%      | 47%        | `for (size_t i = 0; i < n; ++i) {` |
```

If no source lines can be attributed, say so and suggest recompiling
with `-g`.

XML fallback when `show` cannot name the tree (older traces):

```bash
xcrun xctrace export cpu.trace --toc
xcrun xctrace export cpu.trace \
  --xpath '/trace-toc/run[@number="1"]/data/table[@schema="time-profile"]'
```

CPU Profiler traces may use a `cpu-profile` schema instead of
`time-profile`. Read `--toc` and pick the schema it lists. Do not parse
a 30k-line XML dump unless `show` failed.

---

## Building block: Dual-profile comparison

Run the workload under two (or three) thread counts and produce a
side-by-side delta table of the top-15 hottest functions. Functions that
"jump up" between the 1-thread and N-thread profiles are the targets for
scaling investigation.

**You cannot pin P vs E.** There is no `taskset` for core type. Sweep
thread count; set QoS; record `hw.nperflevels` and `hw.perflevel{N}.*`.

### Preparation

```bash
sysctl hw.logicalcpu hw.physicalcpu hw.nperflevels
sysctl hw.perflevel0.name hw.perflevel0.physicalcpu
sysctl hw.perflevel1.name hw.perflevel1.physicalcpu
```

Let:

- `P = hw.perflevel0.physicalcpu` (fastest core type: S or P)
- `N = hw.logicalcpu`

If the workload has its own thread-count flag (`--threads`,
`OMP_NUM_THREADS`, GCD width), use that. Do not wrap the process in a
core-affinity tool and claim you selected P-cores.

**MUST record QoS class** on every run. If the program does not set QoS,
say so (`QoS: unset — OS default`) and, when the user agrees, rebuild
with an explicit

```c
pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
/* or QOS_CLASS_BACKGROUND for the contrast run */
```

or GCD `dispatch_queue_attr_make_with_qos_class`.

### Step 1 — 1-thread profile

```bash
xcrun xctrace record --template 'CPU Profiler' \
  --output cpu_1t.trace \
  --launch -- ./<binary> --threads 1
```

Extract top-15 with **Top-N functions**. Save as `/tmp/top15_1t.txt`.
Note QoS.

### Step 2 — P-count and all-logical profiles

```bash
P=$(sysctl -n hw.perflevel0.physicalcpu)
N=$(sysctl -n hw.logicalcpu)

xcrun xctrace record --template 'CPU Profiler' \
  --output cpu_Pt.trace \
  --launch -- ./<binary> --threads "$P"

xcrun xctrace record --template 'CPU Profiler' \
  --output cpu_Nt.trace \
  --launch -- ./<binary> --threads "$N"
```

Same QoS as Step 1. Extract top-15 from each.

Optional QoS contrast (same thread count, different class):

```bash
# user-interactive vs background — OS will prefer S/P vs E
xcrun xctrace record --template 'CPU Profiler' \
  --output cpu_Nt_ui.trace \
  --launch -- ./<binary> --threads "$N" --qos user_interactive
xcrun xctrace record --template 'CPU Profiler' \
  --output cpu_Nt_bg.trace \
  --launch -- ./<binary> --threads "$N" --qos background
```

### Step 3 — Midpoint (optional)

Only when the 1-vs-N delta is overwhelming (more than ~7 functions jump).
Compare N vs P (or N vs N/2) to filter low-count noise.

### Step 4 — Delta table

```markdown
## Dual-profile comparison — <workload> — <N> threads vs 1 thread
QoS: `QOS_CLASS_USER_INTERACTIVE` (both runs)
perflevel0: Performance × 8;  perflevel1: Efficiency × 4

| N-thread rank | Function | N-thread % | 1-thread rank | 1-thread % | Rank Δ | % Δ |
|-------------|----------|----------|-------------|----------|--------|----------|
| 1 | `os_unfair_lock_lock` | 12.3% | 8 | 2.1% | ▲7 | +10.2% |
| 2 | `dot_kernel` | 8.5% | 1 | 40.0% | ▼1 | −31.5% |
| 3 | `new_func` | 7.1% | — | — | new | +7.1% |
```

Functions not in the baseline top-15 get rank `—` and % `—`; flag them
as **new entrants**.

### Step 5 — Identify "jumper" functions

Flag a function as a **jumper** (scaling bottleneck candidate) if any of
these thresholds are met:

| Criterion | Threshold | Notes |
|-----------|-----------|-------|
| Rank rise | ≥ 3 places | Rank 12 at 1-thread, rank 3 at N-thread |
| Percentage ratio | ≥ 2× increase | 2% → ≥ 4% |
| Absolute percentage gain | ≥ 3% | 1% → 4% |
| New entrant | any % ≥ 2% | Not in 1-thread top-15 |

Bold the jumper rows. These functions are the input to Flow D / Flow C.

---

## Building block: Annotate pattern scan

Scan the interleaved source/assembly of one function and return a
structured table of detected anti-patterns. Each entry names the pattern,
cites the evidence, and references the applicable resolution strategy.

Flows use this output differently: diagnostic flows (B, D, F) apply the
suggested RS; reporting flows (E) surface Pattern + Evidence only.

### Prerequisites

**CPU capability card** from **Check CPU capabilities** (cached).

**Bottlenecks / Counters numbers** (optional) — Useful / Discarded /
Critical L1D miss from a prior Flow A run improve confidence.

### Step 1 — Collect annotate output

```bash
xcrun xctrace show cpu.trace \
  --track 'CPU Profiler' \
  --calltree 'Profile' \
  --source <node-id> \
  --source-mode interleave \
  | tee /tmp/annotate_<function>.txt
```

Fallback without a trace:

```bash
llvm-objdump -d -C --no-show-raw-insn --disassemble-symbols=_<function> <binary> \
  | tee /tmp/objdump_<function>.txt
```

(Static dumps have no %; confidence drops one grade.)

### Step 2 — Apply pattern checklist

Work through these checks in order. A function may match multiple
patterns. This list is mandatory but not exhaustive: also read the
source for problems the mnemonics do not show (aliasing, stride, QoS,
static OpenMP chunks).

#### Pattern 1: Scalar FP — no vectorization

**Signal:** hot instructions are scalar FP — `fadd s*`, `fmul s*`,
`fadd d*`, `fmadd s*` / `d*` — and **no** packed `fadd.4s` / `fmul.4s` /
`fmla.4s` / `fadd.2d` / `fmla.2d` (and no SME `fmopa`) appear.

**Confidence:** high if scalar FP > 50% of the function's samples and
zero packed ops.

**Suggested RS:** SIMD vector width upconversion
(`patterns/simd-upconversion.md`) after diagnosing the vectorization
blocker — most commonly a strided inner loop or missing `restrict`.
Also consider `patterns/parallel-accumulator.md`.

#### Pattern 2: Narrow / wrong-unit SIMD

**Signal:** packed ASIMD is present, but (a) the algorithm is a large
tiled matmul/outer-product on a chip with `FEAT_SME=1` and there is no
`smstart` / `fmopa` / ZA, or (b) SSVE-only (`z` registers, no ZA) in a
kernel that is not feeding the Processing Grid.

**Confidence:** high if SME is available and the hot loop is a tile
dot / outer product. Low if the kernel is a short reduction that ASIMD
ports already saturate.

**Suggested RS:** width ladder `patterns/simd-upconversion.md`; SME
structure via `apple-silicon-codegen` + `patterns/sme-engine-bounce.md`.

#### Pattern 3: Serial accumulator

**Signal:** a **single** FP instruction (`fadd s0, s0, s1`, `fmadd s0,
s0, s1`, or even a packed `fmla.4s v0, v1, v2` into **one** accumulator)
accounts for > 40% of samples, **and** either:

- Useful is high but the loop is slow, **or**
- Critical L1D miss is low, **or**
- destination == a source (obvious chain)

**Confidence:** high with two or more sub-signals.

**Suggested RS:** Parallel accumulator rewrite
(`patterns/parallel-accumulator.md`).

#### Pattern 4: Horizontal-reduction anti-pattern

**Signal:** `faddp`, `faddv` / `faddvq`, `addv`, or a chain of `ext` +
scalar `fadd` **immediately after** a packed `fmul` / `fmla`, inside the
**inner** loop (not once at the end).

**Confidence:** high if the cluster is > 10% of samples.

**Suggested RS:** Parallel accumulator (keep lanes independent; reduce
once).

#### Pattern 5: Test-and-Set / exclusive spin

**Signal:** `ldxr`/`stxr` (or `ldaxr`/`stlxr`) in a tight loop, or
`cas` / `casal` retrying in a spin, accounting for > 10% of samples.
Function names containing `lock`, `spin`, `try_lock`.

**Confidence:** high if the retry is clearly a loop. Medium if a single
`cas` with no visible retry.

**Suggested RS:** TTAS **and** block-not-spin (`patterns/ttas.md`,
`patterns/spin-wait.md`). Block idle threads; spin only for waits on
the order of a thread-switch.

#### Pattern 6: Memory load pressure

**Signal:** `ldr` / `ld1` / `ldp` / `ldur` account for the largest share
of samples (> 30% combined) with no FP/integer compute at comparable
weight.

**Confidence:** medium. Raise to high if Flow A showed Critical L1D miss
(while executing) dominant.

**Suggested RS:** none owned here — locality, 128 B false sharing,
working-set, prefetcher-friendly stride. Cross-check Flow C.

#### Pattern 7: Atomic counter contention

**Signal:** `ldadd` / `stadd` / `ldaddal` / `swp` on a **shared**
counter, > 10% of samples, result not used to decide a lock.

Distinguish from Pattern 5:

- retry loop on `cas` / `ldxr` → TTAS / block-not-spin
- `ldadd` whose result is ignored, field named `count`/`hits`/`stat` →
  per-CPU stats (`patterns/per-cpu-stats.md`)

#### Pattern 8: GPR↔vector moves

**Signal:** `fmov` (GPR↔scalar SIMD), `umov`, `ins`, `dup` from an `x`/`w`
register inside the inner loop, > 10% of samples.

**Suggested RS:** `patterns/gpr-vector-moves.md`. Keep data in `v`
registers; load directly into `v`, not `x` then `fmov`.

#### Pattern 9: Taken branch / CSEL opportunity

**Signal:** `b.eq` / `b.ne` / `b.lt` / `cbz` / `tbz` density in the hot
loop approaching **1 taken branch per 8 instructions**, or a
two-way `b.cond` that only selects a value (the `CSEL`/`CSET` shape).

**Suggested RS:** `patterns/taken-branch-and-csel.md`. Straighten the
hot path so it falls through; replace value-selecting branches with
`CSEL`/`CSET`/`FCSEL`.

#### Pattern 10: LSE vs exclusive pair

**Signal:** `ldxr`/`stxr` used for an atomic add/or/swap on a chip with
`FEAT_LSE=1`.

**Suggested RS:** single-instruction LSE (`ldadd`, `cas`, `swp`) and
platform locks (`os_unfair_lock`, GCD, pthreads) rather than a home-grown
exclusive loop.

#### Pattern 11: SME bounce / streaming

**Signal:** `smstart` / `smstop` / `smstart za`, `fmopa`, `mova` ZA↔Z,
or a dependent scalar/ASIMD use of a value the engine just produced;
or `LD_BLOCKED_BY_SME_LDST` in Counters.

**Suggested RS:** `patterns/sme-engine-bounce.md` and Flow F.

#### Pattern 12: Platform library already winning

**Signal:** hot frames in `vDSP_*`, `cblas_*`, `BNNS*`, `Accelerate`,
`appleblas`, `libcompression`, `memcpy`/`memmove` from `libsystem`.

**Suggested RS:** `patterns/platform-libs-first.md` — **do not rewrite**.

### Step 3 — Format

```markdown
### Pattern scan — `<function_name>`

| Pattern | Evidence | Suggested RS |
|---------|----------|--------------|
| Scalar FP — no vectorization | `fadd s0, s0, s1` at 95% of samples; no `.4s`/`fmla` | simd-upconversion / parallel-accumulator |
| Serial accumulator | `fmadd s0, s1, s2, s0` each iteration; Useful high, still slow | parallel-accumulator |
```

If no patterns match: *"No anti-patterns detected in `<function_name>`."*

**When invoked from Flow E:** suppress Suggested RS; emit Pattern and
Evidence as observation bullets only.

---

## Building block: Branch probability measurement

Measure the runtime probability of each conditional branch in a hot
function. Answers: *"which branches are almost never taken?"* — the input
for `__attribute__((cold))` / `[[gnu::cold]]`.

Intel `BR_INST_RETIRED.NEAR_TAKEN` does **not** exist here. Use Clang
PGO, a CSV export, or sample-density on the branch vs fall-through from
CPU Profiler. Pick branch-mispredict names from `references/named-events.md`
in the CPU Counters picker if you need a Discarded-side cross-check
(`BRANCH_COND_MISPRED_NONSPEC`, `INST_BRANCH_TAKEN`, …). A missing picker
name means the event is not on this chip — skip it. Do not invent encodings.

### Automated procedure

`tools/branchprob.py` is the Darwin tool. Output on **stdout** (Markdown
table). Progress on stderr.

```bash
# (a) CSV of (file, line, taken, not_taken) exported from Instruments or a script
python3 tools/branchprob.py --csv branches.csv foo.c

# (b) LLVM profdata (Clang -fprofile-instr-generate)
python3 tools/branchprob.py --profdata default.profdata foo.c process_data

#     or a pre-rendered `llvm-profdata show` dump:
llvm-profdata show --all-functions --counts default.profdata > /tmp/pshow.txt
python3 tools/branchprob.py --llvm-profdata-show /tmp/pshow.txt foo.c

# (c) .trace — prints how to export, still emits the table schema
python3 tools/branchprob.py --trace cpu.trace foo.c

# (d) static annotate: list AArch64 conditional branches from objdump
python3 tools/branchprob.py --from-annotate ./binary foo.c process_data
```

Focused on hot functions (recommended after Flow B):

```bash
python3 tools/branchprob.py --profdata default.profdata foo.c process_data compute_hash
```

`--fuzzy` substring-matches function names.
`--profile FILE` writes `/* BRANCH PROB: X.XX% */` annotated source.
`--help` prints the collection recipe (PGO + picker events).

### How the agent should collect (also in `--help`)

**Preferred — Clang PGO (Darwin-native):**

```bash
clang -O2 -g -fprofile-instr-generate -fcoverage-mapping -mcpu=apple-m4 \
      -o binary source.c
LLVM_PROFILE_FILE=default.profraw ./binary <representative args>
xcrun llvm-profdata merge -o default.profdata default.profraw
xcrun llvm-profdata show --all-functions --counts default.profdata
xcrun llvm-cov show ./binary -instr-profile=default.profdata
```

Then run `tools/branchprob.py --profdata default.profdata source.c`.

**Counters cross-check (no encodings):**

Pick names from `references/named-events.md` in the CPU Counters picker.
Select the branch-mispredict / taken-branch names this chip lists. A
missing picker name means the event is not on this chip — skip it. Do
not invent encodings. A high Discarded fraction in Flow A plus a
near-50% PGO taken% on a hot `b.cond` is the CSEL candidate.

**CPU Profiler sample density:** if PGO is unavailable, `--from-annotate`
lists every `b.<cond>` / `cbz` / `tbz`. Compare CPU Profiler samples on
the taken target vs the fall-through instruction — that ratio *is* the
measurement. The script will not invent a Taken% from objdump alone.

### Output table (same shape as the original Intel tool, plus cold-path)

```markdown
## `process_data`

| Taken% | Samples | Function | Source line | Branch target | Assembly | Cold-path candidate |
|-------:|--------:|----------|------------:|--------------:|----------|---------------------|
|  91.2% |  607094 | `process_data` | :11 | :18 | `b.eq ...` | no |
|   0.3% |     412 | `process_data` | :27 | :42 | `cbz w0, ...` | yes — `__attribute__((cold))` |
```

Near-0% taken → the **target** is a cold-path candidate
(`[[gnu::cold]]` / `__attribute__((cold))`). See
`patterns/cold-path-annotation.md`.

---

## Building block: GCC / Clang static branch probability

Report compile-time branch-probability estimates when:

- **No workload is available** to run `branchprob.py`
- **Calibrating** measured Taken% against compiler guesses
- **Cross-platform** static dump (GCC `profile_estimate`)

`tools/gccbranchprob.py` is **ISA-agnostic**: it parses GCC's
`profile_estimate` dump and does not care whether the target is x86 or
AArch64. On Darwin the native companion is Clang PGO
(`-fprofile-instr-generate` / `llvm-profdata show`), which
`tools/branchprob.py` consumes. Prefer Clang PGO when a run is possible;
use `gccbranchprob.py` when the project already builds with GCC dumps or
when you need a static estimate without executing.

### GCC dump (unchanged parser)

```bash
mkdir -p dump/
# inject into the project's compile:
#   -g -fdump-tree-profile_estimate-lineno -dumpdir dump/
python3 tools/gccbranchprob.py foo.c dump/ process_data
```

### Clang PGO static-ish view

Block counts from `llvm-profdata show --all-functions --counts` are
*measured*, not guessed. Use `branchprob.py --profdata` rather than
pretending they are GCC `Est%`.

### Interpret divergences

| Compiler vs measured | Meaning |
|----------------------|---------|
| GCC/Clang hot, measured cold | annotating `cold` is a real win |
| GCC/Clang cold, measured hot | may need `hot`; profile first |
| ~50/50 static, measured skewed | data-dependent; PGO / CSEL |

---

## Useful commands cheatsheet

```bash
# --- ISA / topology ---
sysctl hw.optional.arm.FEAT_SME hw.optional.arm.FEAT_DotProd \
       hw.optional.arm.FEAT_I8MM hw.optional.arm.FEAT_BF16 \
       hw.optional.arm.FEAT_LSE hw.optional.armv8_crc32 \
       hw.nperflevels hw.cachelinesize hw.pagesize \
       hw.physicalcpu hw.logicalcpu sysctl.proc_translated
sysctl hw.perflevel0.name hw.perflevel0.physicalcpu \
       hw.perflevel0.l1dcachesize hw.perflevel0.l1icachesize \
       hw.perflevel0.l2cachesize hw.perflevel0.cpusperl2
clang --print-supported-cpus

# --- Instruments ---
xcrun xctrace version
xcrun xctrace list templates
xcrun xctrace list instruments
xcrun xctrace record --template 'CPU Counters' --show-recording-options
xcrun xctrace record --template 'CPU Profiler' --launch -- ./binary
xcrun xctrace record --template 'CPU Counters' --attach PID --time-limit 10s
xcrun xctrace record --template 'System Trace' --launch -- ./binary
xcrun xctrace record --template 'Processor Trace' --launch -- ./binary   # M4/A18+; iOS 18.4+/macOS 15.4+
xcrun xctrace record --template 'App Launch' --launch -- ./App.app/Contents/MacOS/App
xcrun xctrace record --template 'Animation Hitches' --attach App --time-limit 20s
xcrun xctrace toc recording.trace
xcrun xctrace show recording.trace --track 'CPU Profiler' --calltree 'Profile' --format markdown
xcrun xctrace show recording.trace --track 'CPU Profiler' --calltree 'Profile' --source NODE --source-mode interleave
xcrun xctrace export recording.trace --toc
xcrun xctrace export recording.trace --xpath '/trace-toc/run[@number="1"]/data/table[@schema="time-profile"]'
xcrun xctrace symbolicate --input recording.trace --dsym binary.dSYM
open recording.trace
open -a Instruments

# --- symbols / disassembly ---
dsymutil ./binary
xcrun llvm-dwarfdump --verify ./binary.dSYM
atos -arch arm64 -o ./binary -l <loadaddr> <addr>
xcrun crashlog ./report.ips
otool -tv ./binary
otool -tv -p _function ./binary
otool -L ./binary
nm -C ./binary | head
llvm-objdump -d -C --no-show-raw-insn ./binary

# --- PGO ---
clang -O2 -g -fprofile-instr-generate -fcoverage-mapping -mcpu=apple-m4 -o binary source.c
LLVM_PROFILE_FILE=default.profraw ./binary
xcrun llvm-profdata merge -o default.profdata default.profraw
xcrun llvm-profdata show --all-functions --counts default.profdata
xcrun llvm-cov show ./binary -instr-profile=default.profdata

# --- coarse Darwin (not for ISA) ---
sample <pid> 10 -file /tmp/sample.txt
sudo fs_usage -w -f filesys <pid>
```
