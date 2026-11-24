<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. Do not copy Guide tables. MIT license. -->

# Flow B: execution steps — CPU Profiler hotspots

Use this when the user wants to know *which functions* consume cycles,
not just aggregate Bottlenecks fractions.

**Time Profiler is not this flow.** Time Profiler is a wall-clock
overview ("what is the app doing?"). The recording, annotation, and
pattern scan below use **CPU Profiler** (cycle PMI). Mixing them up
attributes time to whichever core happened to be sampled, not to the
ISA work.

---

## Phase 0 — Ensure debug symbols

Run **Building block: Ensure debug symbols** on the binary before
recording. If the binary lacks DWARF and the user agrees to recompile,
do so now — before Phase 1 — so the recording and annotation use the
same binary. If the user declines, note the limitation and proceed.

Also run **Check CPU capabilities**. If `sysctl.proc_translated` is `1`,
**STOP**.

---

## Phase 1 — Record

Use **Building block: CPU Profiler record**.

```bash
xcrun xctrace record \
  --template 'CPU Profiler' \
  --output cpu.trace \
  --target-stdout - \
  --launch -- ./<binary> <args>
```

**After proposing this, ask the user** (skip if the prompt is already
clear):

> *"CPU Profiler attributes cycles to functions. Would you also like a
> Time Profiler overview of what the app is doing (wall-clock, including
> blocked time)? That is a separate recording and is not used for ISA
> conclusions."*

If they want both, record Time Profiler **in addition**, never instead.

Reuse an existing `cpu.trace` when the user prefers and the binary has
not been rebuilt.

Optional: a 5–10 s Time Profiler pass first, if the user has no idea
whether the process is CPU-bound at all. If Time Profiler shows the
process asleep in `kevent` / `mach_msg` the whole time, say so and do
not pretend CPU Profiler will find an ISA bug.

---

## Phase 2 — Top functions

Use **Building block: Top-N functions / Top-N lines**.

```bash
xcrun xctrace toc cpu.trace
xcrun xctrace show cpu.trace \
  --track 'CPU Profiler' \
  --calltree 'Profile' \
  --include-source-info \
  --format markdown
```

Key columns: **self weight**, **total weight**, **source file:line**,
**binary / dylib**, **symbol**.

**If symbols show as raw addresses:** the binary is stripped or the dSYM
was not found. Recompile with `-g`, `dsymutil`, re-record — or
`xctrace symbolicate --input cpu.trace --dsym binary.dSYM` if only the
dSYM was missing (binary unchanged). Else use **resolve address to
source**.

Open `cpu.trace` in Instruments if the CLI track name does not match;
the GUI detail view is the same data.

---

## Phase 3 — Interpret

- **`libsystem_kernel` / `mach_msg` / `kevent` / `read` / `write`
  dominating (> ~40% self)** → syscall / I/O bound. Confirm with
  System Trace or `sudo fs_usage -w -f filesys <pid>`. Not an ISA
  problem. Batch, coalesce `kevent`, overlap I/O.
- **`libdispatch` / `libsystem_pthread` / `os_unfair_lock` /
  `pthread_mutex` / `_os_once` dominating at high thread counts** →
  Flow C / Flow D, not a hotter inner loop.
- **`malloc` / `free` / `operator new` / `swift_allocObject`
  dominating** → allocation-heavy. Arena / pool / reuse; Apple
  allocators (MTE-aware) rather than a custom malloc.
- **`vDSP_*` / `cblas_*` / `BNNS*` / `Accelerate` / `appleblas`
  already hot** → **the library is winning**. Do not rewrite the kernel.
  Invoke `patterns/platform-libs-first.md` and stop.
- **No clear hotspot (< 5% per function)** → well-distributed. Look for
  parallelism, algorithmic improvements, or AMP partitioning (Flow D).
- **One function > 20%** → strong optimization candidate; proceed to
  Phase 4.
- **Spin / CAS / `ldxr` / `ldadd` wrappers prominent** → possible TAS
  spin or atomic-counter contention; Phase 4 to confirm, then TTAS /
  block-not-spin / per-CPU stats.
- **`SMSTART` / `fmopa` / `libswift_Builtin_float` SME stubs / engine
  frames** → Flow F.
- **Scalar FP in a function you can already read** (`for (...) s +=`) →
  invoke `performance-patterns` immediately; Phase 4 confirms.

---

## Phase 4 — Drill-down with annotated disassembly

**Mandatory:** if any function accounts for **≥ 20% of samples**, use
**Building block: Disassembly annotate** — do not just offer it, actually
run it. If the binary is stripped, fall back to **resolve address to
source** plus `llvm-objdump`.

```bash
xcrun xctrace show cpu.trace \
  --track 'CPU Profiler' \
  --calltree 'Profile' \
  --source <node-id> \
  --source-mode interleave
```

Once you have the annotate output, run **Building block: Annotate
pattern scan**. For each pattern returned, apply the **Suggested RS**
using Part 5 of `SKILL.md` (invoke `performance-patterns`).

### Vectorization verification (picker, not encodings)

After a SIMD fix (upconversion, accumulator rewrite, or SME kernel),
verify with CPU Counters. Open Guide **Table 8.1** and pick, if present
on this chip:

- ASIMD compute: `INST_SIMD_ALU` / `INST_SIMD_ALU_VEC`
- SME grid: `INST_SME_ENGINE_ALU` (and LD/ST if you care about traffic)
- Scalar FP leaking into the engine: `INST_SME_ENGINE_SCALARFP`

If packed/SME counts are non-zero and the hot function no longer shows
scalar `fadd s*` / `fmul s*`, SIMD/SME is active. If scalar is still
dominant, the vectorizer is still blocked.

**Most common vectorization blocker: a strided inner loop** — pointer
arithmetic of the form `base + k*stride` where stride is large (e.g.
column access of a row-major matrix). The fix is a **loop reorder** so
the innermost loop is stride-1 — then rebuild and re-run the pattern
scan.

Other blockers: missing `restrict` / `__restrict` (aliasing),
loop-carried dependence through a single accumulator (Pattern 3),
GPR↔vector round-trips that the compiler cannot see through
(Pattern 8).

### Accelerate short-circuit

If Phase 3 showed Accelerate/vDSP/BLAS as the hot frame, **do not**
spend Phase 4 rewriting it as hand ASIMD. Platform libraries first.

---

## Phase 5 — Handoff

| Finding | Next |
|---------|------|
| Named anti-pattern in the scan table | `performance-patterns` now |
| Need a shareable annotated-source report | Flow E (reuse `cpu.trace`) |
| Jumpers / locks / atomics at N threads | Flow C / Flow D |
| SME mnemonics or engine events | Flow F |
| New kernel to write | `apple-silicon-codegen` |
