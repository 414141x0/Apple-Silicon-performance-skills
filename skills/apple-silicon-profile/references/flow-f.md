<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Flow F: execution steps — SME engine

Use this flow when the profile or the source shows Scalable Matrix
Extension / Streaming SVE, or when a cluster's frequency drops while a
sibling is in streaming / ZA mode.

SME is available beginning on **M4 / A18** families (`sysctl
hw.optional.arm.FEAT_SME`). Each cluster has one engine, shared by the
cores in that cluster, with a private ZA copy per core. The engine is
nonspeculative. Engine instructions **trail** the core by dozens,
hundreds, or thousands of cycles. Transit of dependent data
core↔engine is on the order of **~20 cycles each way** — and that is
the *best* case before the trail is included. Class latencies:
`apple-silicon-codegen/references/latency-lookup.md`. Do **not** copy
engine latency tables.

SSVE-only (Z/P, no ZA) rarely beats the core's 4-wide ASIMD ports. The
throughput path is **ZA + the Processing Grid**; SSVE stages data in
and out.

---

## When to run this flow

Trigger if **any** of the following is true:

- Source or disassembly contains `SMSTART`, `SMSTOP`, `SMSTART ZA`,
  `ZA`, `FMOPA` / `BMOPA` / `SMOPA`, `MOVA` ZA↔Z, streaming-SVE
  `LD1*`/`ST1*` while `PSTATE.SM=1`, or `#include <arm_sme.h>`
- CPU Counters picker shows SME engine events retiring (names such as
  `INST_SME_ENGINE_ALU`, `INST_SME_ENGINE_LD`, `INST_SME_ENGINE_ST`,
  `SME_ENGINE_SM_ENABLE` — pick names from `references/named-events.md`.
  A missing picker name means the event is not on this chip — skip it.
  Do not invent encodings)
- Core waiting on the engine (`CORE_WAITING_SME_ENGINE_CYCLE`,
  `SCHEDULE_WAITING_SME_ENGINE_REG_DATA`,
  `LDST_UNIT_WAITING_SME_ENGINE_*` if present)
- 4 KiB hazard (`LD_BLOCKED_BY_SME_LDST`, `ST_BARRIER_BLOCKED_BY_SME_LDST`)
- Matmul / conv / outer-product on M4+ that is **not** already in
  Accelerate / BNNS
- Cluster-wide frequency drop coincident with a sibling in SM/ZA
  (Instruments CPU Counters / Power, or `powermetrics`)

If `FEAT_SME` is 0, **stop**: this chip has no engine. Use ASIMD on the
core (Flow B + `patterns/simd-upconversion.md`).

---

## Playbook

Work the ten steps in order. After step 1, skip any step whose
precondition is already satisfied. Hand off the kernel rewrite to
`performance-patterns` and `apple-silicon-codegen` rather than
improvising ZA tile layout in this skill.

### 1. Confirm FEAT_SME via sysctl

```bash
sysctl hw.optional.arm.FEAT_SME
sysctl hw.optional.arm.FEAT_SME2 2>/dev/null || true
```

Value `1` → proceed. Missing oid or `0` → this is not an SME session.

Record the capability card (Building block: Check CPU capabilities),
including perflevel names. P/S clusters typically host the higher
throughput engine (SME-8); E clusters typically host SME-2. The **same
ISA** runs on both; SME-2 is the same code, slower. Do not hardcode
which cluster is which — quote `hw.perflevel{N}.name`.

### 2. Design for ZA + Processing Grid; SSVE supports

The engine has two compute stories:

- **ZA + Processing Grid** — 4 KiB ZA, outer-product / tile ops
  (`FMOPA` and friends). This is the throughput path.
- **SSVE Execution Unit** — 512-bit Z, predicates P. One vector issue
  per cycle on the engine, comparable bandwidth to ASIMD on the core
  and often *less* for FP add/mul (multiple issue cycles). SSVE exists
  to **feed the grid**, not to replace `FMLA.4S` on the core.

If the algorithm cannot use ZA (not a tile / outer-product / long
accumulate into a 2D array), **stay on core ASIMD**. SSVE-only kernels
rarely win. Invoke `apple-silicon-codegen` (`references/sme-engine.md`,
`references/sme-ssve.md`) for structure.

SVL is 512-bit and ZA is 4 KiB **on Apple silicon today**. Do not
hardcode SVL in portable code; query the runtime (`svcntb()` / ABI)
even if you know the number on this Mac.

### 3. No dependent data core↔engine bounce

Transit is ~20 cycles each way; the engine trails the core by tens to
thousands of cycles because it does not speculate. A core instruction
that consumes a value the engine just produced waits an indeterminate
(but long) time.

**Illegal shapes:**

- `SMSTART` → engine produces a scalar/predicate/NZCV → `SMSTOP` →
  core uses it in a tight loop → `SMSTART` again
- Per-iteration round-trip of a reduction to a GPR (`UMOV` / `FMOV`
  from Z/ZA to `x`/`w`/`s` inside the inner loop)
- Predicate computed on the core, used on the engine, updated on the
  core, used on the engine again, every iteration

**Legal shape:** load a large tile into ZA, compute on the grid until
the tile is done, extract once, store once. SSVE stages Z in and out
of the grid without waking the core's FP pipes.

If the annotate scan shows this bounce, invoke
`patterns/sme-engine-bounce.md` **now**.

### 4. Isolate core-touched vs engine-touched data on separate aligned 4 KiB regions

The core tracks memory hazards between core and engine at **aligned
4 KiB** granularity. If the core and the engine touch the same 4 KiB
page, the core stalls until the engine drains that page.

```c
void *engine_buf = NULL;
posix_memalign(&engine_buf, 4096, n * sizeof *engine_buf);
/* core initializes, then does not touch until the engine is done */
```

Do **not** let the engine load/store the **stack** (ZA/Z/B/H/S/D/Q
references to stack slots while `SM=1`). Dynamically allocate engine
working sets.

Picker names that confirm a violation (if present):
`LD_BLOCKED_BY_SME_LDST`, `ST_BARRIER_BLOCKED_BY_SME_LDST`.

### 5. Target SME-8; SME-2 runs the same code slower

Write the kernel for the higher-throughput engine (SME-8, typically
S/P clusters). SME-2 (typically E clusters) **executes the same
instructions** with fewer processing elements — longer latency, lower
throughput, not a different ISA.

Do not special-case SME-2 with a second kernel unless a measurement on
this chip says so. QoS (`QOS_CLASS_USER_INTERACTIVE`) keeps the work on
the cluster that has SME-8 when the system can. Background QoS may
land the same binary on SME-2; that is expected, not a bug.

Look up grid issue / extract latencies in
`apple-silicon-codegen/references/latency-lookup.md`. Design targets
(not a table dump): grid accumulate on the order of **4 cycles**, ZA
vector extract longer. Cover them with **work in flight across ZA**,
not with core-side busy-waits.

### 6. Cover ~4-cycle grid latency with whole ZA

The grid wants independent work on many ZA rows / tiles at once.
A single outer-product into one tile, then a dependent extract, then
the next outer-product, leaves the PEs idle.

**Shape:** distribute accumulations across as much of ZA as the
algorithm allows (tiles or vector groups). Stage through Z so the
out-of-order schedulers are not the limit. Issue rates:
`apple-silicon-codegen/references/latency-lookup.md`.

### 7. Pack compute instructions back-to-back for delivery fusion

The core can fuse adjacent SME-engine compute instructions
("packing fusion") so they ride to the engine together. Fine-grained
interleaving of engine compute with **core** memory ops breaks that.

**Shape:** a block of engine compute, then a block of engine
loads/stores, not `ld1 ; add x ; fmopa ; ldr w ; fmopa`. Operation
fusion (LUTI, extract+convert, MOVAZ+zip, …) lives in
`apple-silicon-codegen/references/sme-engine.md` — prefer it when the
sequence matches, and do not break a fusion sequence with unrelated
ops. This flow only flags the anti-pattern when annotate shows
isolated `fmopa` surrounded by core integer work.

Picker name (if present): `INST_SME_ENGINE_PACKING_FUSED` vs
`INST_SME_ENGINE_ALU`. A fused/ALU ratio near zero on a dense kernel
is a packing miss.

### 8. Alignment / padding for the engine LSU; avoid unaligned stores

The engine LSU reads and writes the **Shared LL** directly (not the
core L1D) and likes consecutive, cache-line-aligned streaming
accesses. Unaligned **stores** are worse than unaligned loads.

- Align engine buffers to at least the line size (`hw.cachelinesize`,
  128 B); 4 KiB isolation (step 4) already implies page alignment.
- Prefer sequential scans. For unavoidable column strides, pad the
  row stride so columns spread across LL banks. Experiment; do not
  guess a pad from a blog.
- Do not mix engine stores into a buffer the core is also storing
  unaligned.

### 9. Cluster-wide frequency drop

An entire cluster runs at one frequency: cores, Shared LL, and the
SME engine. When **any** core in the cluster is in Streaming SVE or
ZA mode, the **entire cluster** may run at a slightly lower clock.

**Do not mix latency-sensitive core work on the same cluster while a
sibling is in SM/ZA.** Practical consequences:

- A UI / audio / tight scalar thread that happens to land on the same
  cluster as a matmul will see a frequency haircut it did not ask for.
- QoS and GCD width are the levers you have (you still cannot pin).
  Keep engine work in its own queue at a QoS that the OS can isolate;
  do not `dispatch_async` a ZA kernel onto the queue that also services
  a latency-critical callback.
- Measuring this: CPU Counters / Power, `powermetrics`, or a sibling
  thread's wall-clock regression when the engine starts. Do not invent
  a PMU encoding for frequency.

### 10. Hand off

This skill **diagnoses**. The rewrite lives elsewhere:

| Need | Invoke |
|------|--------|
| Bounce / 4 KiB isolation / SMSTART around a dependent scalar | `performance-patterns` → `patterns/sme-engine-bounce.md` |
| ZA tile structure, streaming-mode entry/exit, SSVE staging, fusion sequences | `apple-silicon-codegen` → `references/sme-engine.md` and `references/sme-ssve.md` |
| "Should this even be SME?" (short reduction, already-fast ASIMD, Accelerate exists) | `patterns/platform-libs-first.md` first; then stay on ASIMD |

Latencies: `apple-silicon-codegen/references/latency-lookup.md`. Engine
rewrite: `apple-silicon-codegen/references/sme-engine.md` and
`apple-silicon-codegen/references/sme-ssve.md`. Events:
`references/named-events.md`. Quote the rule and the measurement you
took; do not paste tables.

---

## Recording recipe (when you need numbers)

```bash
# capability
sysctl hw.optional.arm.FEAT_SME hw.nperflevels
sysctl hw.perflevel0.name hw.perflevel1.name

# ISA / cycle hotspots
xcrun xctrace record --template 'CPU Profiler' \
  --output sme_cpu.trace --launch -- ./<binary>

# engine events — pick names from references/named-events.md in the
# CPU Counters picker. A missing picker name means the event is not on
# this chip — skip it. Do not invent encodings.
# (INST_SME_ENGINE_*, SME_ENGINE_SM_ENABLE, CORE_WAITING_SME_ENGINE_CYCLE,
#  LD_BLOCKED_BY_SME_LDST, INST_SME_ENGINE_PACKING_FUSED, …)
xcrun xctrace record --template 'CPU Counters' \
  --output sme_ctrs.trace --launch -- ./<binary>
```

Disassemble the hot kernel:

```bash
xcrun xctrace show sme_cpu.trace \
  --track 'CPU Profiler' --calltree 'Profile' \
  --source <node-id> --source-mode interleave
llvm-objdump -d -C --no-show-raw-insn --disassemble-symbols=_<kernel> ./<binary>
```

Look for `smstart`, `smstop`, `fmopa`, `mova`, streaming `ld1`/`st1`,
`fmov`/`umov` next to ZA, and stack spills of `z`/`za` (the last is a
step-4 violation).

---

## Report shape (short)

```markdown
# SME engine report — <workload>

FEAT_SME=1. perflevel0=<name>, perflevel1=<name>.
QoS=<…>. Class latencies from latency-lookup.md; numbers not copied.

## Signals
- SMSTART/SMSTOP density, ZA live, FMOPA in the hot kernel: …
- CORE_WAITING / 4 KiB blocked events (if picker had them): …
- Cluster frequency interaction with sibling threads: …

## Violations of the playbook
- [ ] ZA+grid (not SSVE-only, not core ASIMD rewrite of a tile)
- [ ] no dependent core↔engine bounce (~20 cyc + trail)
- [ ] 4 KiB isolation; no engine stack traffic
- [ ] targeted at SME-8; SME-2 accepted as slower same code
- [ ] ZA filled enough to cover ~4-cycle grid latency
- [ ] packed compute (delivery fusion)
- [ ] aligned engine LSU traffic
- [ ] no latency-sensitive sibling on the same cluster in SM/ZA

## Handoff
- patterns/sme-engine-bounce.md
- apple-silicon-codegen/references/sme-engine.md
- apple-silicon-codegen/references/sme-ssve.md
```
