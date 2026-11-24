<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Named PMU events and Bottleneck taxonomy

Home of event **names**, family availability, and footnotes that change
ratios. Pick the name in Instruments **CPU Counters**. Do not invent
encodings. A missing picker name means the event is not on this chip —
skip it.

Family nicknames: **M1** = M1+A14, **M2** = M2+A15, **M3** = M3+A16+A17
Pro, **M4** = M4+A18, **M5** = M5+A19. Topology/ISA: `sysctl.md` /
`chip-families.md`.

Naming:

- `INST_*` — retired matching ISA instructions. Nonspeculative by definition.
- `*_NONSPEC` — counted only if the causing insn retired.
- Other — counts when the event occurs; sampled PC may **not** be the
  causer (speculative).

**Time Profiler is not the ISA tool.** Wall-clock, simultaneous across
CPUs, **not** adjusted for frequency or residency.

**CPU Profiler** is the hotspot tool: per-CPU cycle PMI.

**CPU Counters** is named events. Instruments 26+ **CPU Bottlenecks** is
the guided counting mode of that template.

**Processor Trace** (M4 / A18+, Instruments 16.3+, iOS 18.4+ / macOS
15.4+): hardware EL0 (unprivileged) execution trace. Record with the
**Processor Trace** template. Reports **duration, cycles, and retired
insns per function**. Timeline is a flame graph; detail is a call tree
with min / max / count / sum **by function**. Unlike sampling, it sees
**every** EL0 function including compiler-generated ARC / C++
constructors. Use it for exact sequences and synthesized frames, not as
a replacement for CPU Profiler cycle PMI. On-device needs extra System
Settings. If the template is missing, the chip/OS is below the floor —
fall back to CPU Profiler.

Thread Sanitizer is a write-time correctness habit (`amp.md` rec 64),
not a named event and not a cycle profile.

---

## Bottleneck taxonomy (fractions of sustainable μop bandwidth)

Not raw IPC. Most insns → 1 μop; some crack. Sustainable bandwidth
**rises** by family, so a higher “inefficiency” fraction can still be
faster. **Do not compare fractions across families.**

**Useful** — retired correct-path work. Measured μPC **divided by the
maximum μPC for that core type**. High Useful + still slow → algorithm /
ISA, not more counters.

**Discarded** — mispredict / restart waste (wrong-path μops + recovery).
High → control flow or memory dependence is hard to predict. Fix with
`CSEL` / more stable structures. Bigger windows on later families can
**raise** discarded fraction at the same mispredict rate. Accounting is
**more accurate beginning M4 / A18**; the same code may show a **larger**
discarded fraction than on M1–M3.

**Delivery** — Processing has window room but Delivery did not feed μops.
Fix: hot-function locality; inline / unroll / straighten; cold `-Os` /
Swift `-Osize`. Typical bandwidth cause: **too many taken branches**
(incl. calls/returns); danger near **1 taken in 8**. Replace
skip-assignment branches with `CSEL`/`CSET`.

Delivery **subsets, M3 / A16+**: latency from **I-cache**; from **I-TLB**;
from **other** (often an artifact of the named two). Delivery mode
**zooms into** lost delivery only.

**Processing** — window full (not enough ILP, or memory delay). **Mode
itself is M3 / A16+.** It categorizes **every cycle the window is
non-empty**.

Processing sub-categories:

- **Executing** — ≥1 μop issued and oldest LD/ST is **not** an L1D miss.
  High Executing + high Processing → not enough parallelism.
- **Critical L1D miss while executing** — issuing ≥1 μop **and** oldest
  LD/ST is an L1D miss.
- **Critical L1D miss** — issued 0, oldest LD/ST is L1D miss.
- Combined critical-miss = while-executing + stalling. **This** is
  memory-bound, not Linux `cache-misses/refs`.
- **Non-critical L1D miss** — issued 0, a **younger** load is the L1D miss.
- **Execution latency** — issued 0, critical path is instruction latency
  (including L1D **hits**).
- **Critical MTE allocation tag check (M5 / A19+)** — issued 0, oldest
  LD/ST limited by tag-check delay (for loads, data already there). Does
  not equal “MTE made it slow”.

Extra track: whether the **SME engine** is used (M4 / A18+).

---

## Footnotes that change interpretation

1. **`INST_BRANCH_COND` is not stable.** M3-and-prior: `B.<cond>` only.
   M4 adds `CBZ`/`CBNZ`/`TBZ`/`TBNZ`. M5 adds `BC.<cond>` (`FEAT_HBC`;
   Apple predictors **ignore the hint**). Do not compare across families.
2. **`MAP_LDST_UOP` includes GPR→vector converts and every insn shipped
   to the SME engine** (store unit). Not “loads+stores of the core kernel.”
3. **`ST_NT_UOP` includes SSVE/SME loads.** **`LD_NT_UOP` excludes them.**
4. **`INST_INT_ST` / `INST_LDST` do not count `DC ZVA`.**
5. **`ATOMIC_OR_EXCLUSIVE_{SUCC,FAIL}`:** M1–M4 exclusives (`LDXR`/`STXR`)
   **undercount** when the line starts Shared; LSE atomics count correctly.
   **M5 both sides are correct.** Fail density = FAIL / `INST_ALL`. A
   density that **rises with thread count** is contention. Zero
   exclusive-fail is not proof of no `LDXR` contention on M1–M4.
6. **`DECODE_UOP` is only accurate beginning M5.**
7. **`INST_MICROCODED` (M5):** samples may attribute a few insns **early**.
8. **`LDST_SME_PRED_INACTIVE`:** a page-crossing LD/ST may count when all
   elements of the **low** page are predicated off even if some on the
   high page are active. Rare if predication is structure-edge only.
9. **`LD_SRC_*` (M5):** count loads between line **arrival** and **L1D
   fill** → multiple loads per arriving line. Store-partial
   (`LD_SRC_STORE_NONSPEC`) **wins** over any location event. Cracked
   insns (e.g. LD3) may count **per μop**. Line/page cross generally
   attributes the **higher** address. A load that misses but does not
   execute until after the fill will not get a source event. Use
   event-based sampling to find the loads.
10. **`ARM_*` spellings** are M4+ (some M5-only). Several Apple names
    **map to** ARM names beginning M4. Do not mix Apple and ARM names in
    one ratio unless they are the same event.
11. Cross-page SME: an access is cross-page if **any** byte in the high
    16 KiB page is touched **after predication**.

---

## ARM architected names (picker)

| Name | From | Meaning |
|------|------|---------|
| `ARM_BR_MIS_PRED` | M4+ | speculatively executed mispredicted / not-predicted branch |
| `ARM_BR_PRED` | M4+ | speculatively executed predictable branch |
| `ARM_L1D_CACHE` | M4+ | L1D access; **does not count software prefetches** |
| `ARM_L1D_CACHE_RD` | M4+ | attributable L1D read |
| `ARM_L1D_CACHE_REFILL` | M4+ | L1D refill |
| `ARM_L1D_CACHE_LMISS_RD` | M4+ | long-latency read miss; no merge into existing requests; 1 per missed line |
| `ARM_L1I_CACHE` | M5 | L1I access |
| `ARM_MEM_ACCESS` / `_RD` / `_WR` | M5 | data memory access |
| `ARM_MEM_ACCESS_CHECKED` / `_RD` / `_WR` | M5 | MTE-checked access |
| `ARM_STALL` / `_FRONTEND` / `_BACKEND` | M4+ | no op sent for execution |
| `ARM_STALL_SLOT` / `_FRONTEND` / `_BACKEND` | M4+ | no op on a slot |

Apple names that **map** to ARM names beginning M4 (same counter):

- `BRANCH_MISPRED_NONSPEC` → `ARM_BR_MIS_PRED_RETIRED`
- `CORE_ACTIVE_CYCLE` → `ARM_CPU_CYCLE`
- `INST_ALL` → `ARM_INST_RETIRED`
- `INST_BRANCH` → `ARM_BR_RETIRED`
- `L1I_CACHE_MISS_DEMAND` → `ARM_L1I_CACHE_LMISS`
- `MAP_UOP` → `ARM_OP_SPEC` (`MAP_UOP` itself is M3+)
- `RETIRE_UOP` → `ARM_OP_RETIRED`

---

## Event catalog

Notation: **all** = M1–M5. **M2+** = M2–M5. **M3+** = M3–M5. **M4+** =
M4–M5. **M5** = M5 only.

### Mix / retired ISA

- `INST_ALL` — all. Denominator for densities.
- `INST_BARRIER` — all. Retired data barriers (`DSB`/`DMB`).
- `INST_BRANCH` — all. Incl. calls and returns.
- `INST_BRANCH_TAKEN` — all. Taken-branch density = this / `INST_ALL`.
  Danger near 1/8.
- `INST_BRANCH_COND` — all. **Def changes (footnote 1).**
- `INST_BRANCH_INDIR` — all. Indirect incl. indirect calls, **not** returns.
- `INST_BRANCH_CALL` — all.
- `INST_BRANCH_CALL_INDIR` — **M5**. Subset of `INST_BRANCH_CALL`.
- `INST_BRANCH_RET` — all.
- `INST_INT_ALU` — all. Non-branch non-LDST integer.
- `INST_INT_LD` — all.
- `INST_INT_ST` — all. **Not `DC ZVA`.**
- `INST_LDST` — all. **Not `DC ZVA`.**
- `INST_SIMD_ALU` — all. Non-LDST ASIMD **and** scalar FP.
- `INST_SIMD_ALU_VEC` — **M2+**. Packed SIMD only (not scalar FP). Picker
  name is `VEC`, not `VECTOR`.
- `INST_SIMD_LD` / `INST_SIMD_ST` — all.
- `INST_MICROCODED` — **M5**. Samples may sit a few insns early.

Mix / branch / MTE **densities are ratios** over these names — see
[Ratios](#ratios-agents-actually-compute). Do not invent encodings.

### Branch mispredict (retired)

- `BRANCH_MISPRED_NONSPEC` — all. Incl. calls and returns.
- `BRANCH_COND_MISPRED_NONSPEC` — all.
- `BRANCH_INDIR_MISPRED_NONSPEC` — all. Indirect incl. calls and returns.
- `BRANCH_CALL_INDIR_MISPRED_NONSPEC` — all.
- `BRANCH_RET_INDIR_MISPRED_NONSPEC` — all.
- `BRANCH_BR_INDIR_MISPRED_NONSPEC` — **M5**. Indirect **excluding** call
  and return.

### Atomic / exclusive

- `ATOMIC_OR_EXCLUSIVE_SUCC` / `FAIL` — all. Footnote 5. Contention
  without HITM.

### Core activity / frontend

- `CORE_ACTIVE_CYCLE` — all.
- `DECODE_UOP` — all. **Accurate M5+ only.**
- `FETCH_RESTART` — all. Internal fetch restarts; **does not include
  branch mispredicts**.
- `FLUSH_RESTART_OTHER_NONSPEC` — all. Flush/restart not due to branch
  mispredict or memory-order violation.
- `INTERRUPT_PENDING` — all. Cycles an interrupt was pending because it
  was masked.

### L1D / TLB / I-side / L2 TLB

- `L1D_CACHE_MISS_LD` / `_LD_NONSPEC` / `_ST` / `_ST_NONSPEC` — all.
- `L1D_CACHE_WRITEBACK` — all.
- `L1D_TLB_ACCESS` / `_FILL` / `_MISS` / `_MISS_NONSPEC` — all.
- `L1I_CACHE_MISS_DEMAND` — all.
- `L1I_TLB_FILL` / `L1I_TLB_MISS_DEMAND` — all.
- `L2_TLB_MISS_DATA` / `_INSTRUCTION` — all.
- `MMU_TABLE_WALK_DATA` / `_INSTRUCTION` — all.

### NT / unit flow / alignment

- `LD_NT_UOP` — all. **Excludes SSVE/SME loads.**
- `ST_NT_UOP` — all. **Includes SSVE/SME loads.**
- `LD_UNIT_UOP` / `ST_UNIT_UOP` — all.
- `LDST_X64_UOP` — all. Crossed a **64 B** boundary.
- `LDST_XPG_UOP` — all. Crossed a **16 KiB** page (SME: after predication).
- `ST_MEM_ORDER_VIOL_LD_NONSPEC` — all. Retired **core** stores vs core loads.

### Map / schedule

- `MAP_INT_UOP` / `MAP_SIMD_UOP` — all.
- `MAP_LDST_UOP` — all. **Footnote 2.**
- `MAP_DISPATCH_BUBBLE` — all. Map not stalled, Decode sent no μops.
- `MAP_DISPATCH_BUBBLE_IC` — **M3+**. L1I.
- `MAP_DISPATCH_BUBBLE_ITLB` — **M3+**. L1I TLB.
- `MAP_DISPATCH_BUBBLE_SLOT` — **M4+**.
- `MAP_DISPATCH_BUBBLE_TAKENBR_SLOT` — **M5**. Taken-branch **delivery**.
- `MAP_REWIND` — all.
- `MAP_RECOVERY` — **M4+**.
- `MAP_STALL` / `MAP_STALL_DISPATCH` — all.
- `MAP_STALL_NONRECOVERY` — **M4+**.
- `MAP_UOP` — **M3+**.
- `RETIRE_UOP` — all.
- `SCHEDULE_EMPTY` / `SCHEDULE_UOP` — all.
- `SCHEDULE_UOP_ANY` — **M3+**. Cycles the scheduler issued ≥1 μop.
- `LDST_UNIT_OLD_L1D_CACHE_MISS` — **M3+**.
- `LDST_UNIT_WAITING_OLD_L1D_CACHE_MISS` — **M3+**. No μop issued, prioritized.
- `LD_UNIT_WAITING_YOUNG_L1D_CACHE_MISS` — **M4+**. Younger load waiting
  after L1D miss, no issue, no critical miss, prioritized.

### SME engine (M4+ unless noted)

- `INST_SME_ENGINE_ALU` / `_LD` / `_ST` / `_SCALARFP`
- `INST_SME_ENGINE_PACKING_FUSED` — non-LDST engine insns **packed with
  another** to cut delivery bandwidth
- `MAP_INT_SME_UOP` — mapped **core integer** μops **for SME engine insns**
- `LD_BLOCKED_BY_SME_LDST` — core loads blocked by SME on the **same 4 KiB**
- `ST_BARRIER_BLOCKED_BY_SME_LDST` — core **stores** blocked on the same
  4 KiB, **and** barriers / store-release to **any** address blocked by SME
- `LD_SME_NORMAL_UOP` / `LD_SME_NT_UOP` / `ST_SME_NORMAL_UOP` / `ST_SME_NT_UOP`
- `LD_SME_MEM_ORDER_VIOL_LD_NONSPEC` / `ST_SME_MEM_ORDER_VIOL_LD_NONSPEC`
- `LDST_SME_PRED_INACTIVE` — footnote 8
- `LDST_SME_XPG_UOP` — engine LD/ST that crossed a **16 KiB** page after
  predication
- `LDST_UNIT_WAITING_SME_ENGINE_INST_QUEUE_FULL`
- `LDST_UNIT_WAITING_SME_ENGINE_MEM_DATA`
- `SCHEDULE_WAITING_SME_ENGINE_REG_DATA` — waiting for register /
  predicate / flag from the engine
- `SME_ENGINE_SM_ENABLE` — `PSTATE.SM` 0→1
- `SME_ENGINE_SM_ZA_ENABLE` — simultaneous SM and ZA 0→1
- `SME_ENGINE_ZA_ENABLED_SM_DISABLED` — cycles ZA=1 and SM=0
- `CORE_WAITING_SME_ENGINE_CYCLE` — **M5**. Core waiting to **push** to
  the engine **or** waiting for data from it, no μop issued, prioritized

### M5 data-source (`LD_SRC_*`, footnote 9)

- `LD_SRC_LL_CACHE_NONSPEC` — this core’s Shared LL
- `LD_SRC_PL2_CACHE_NONSPEC` — this core’s PL2 (S only; 0 elsewhere is expected)
- `LD_SRC_CORE_SAMECLUSTER_NONSPEC` — another core in the **same cluster**
- `LD_SRC_OTHERCLUSTER_NONSPEC` — another cluster. **Does not include M
  Cache or DRAM.**
- `LD_SRC_OTHERCLUSTER_OTHERDIE_NONSPEC` — other cluster on **another die**
- `LD_SRC_MEMSYS_NONSPEC` — **Memory Cache or DRAM**
- `LD_SRC_MEMSYS_OTHERDIE_NONSPEC` — M Cache or DRAM on **another die**
- `LD_SRC_STORE_NONSPEC` — older uncommitted stores (store-forward). Wins
  over location events

Use these to distinguish intra-cluster sharing vs fabric (~50 ns) vs M
Cache (~35 ns) vs DRAM (~95 ns). Multiple loads per fill; do not treat
counts as “number of missed lines.”

### M5 MTE / PL2

- `LDST_MEM_ACCESS_CHECKED_X2K` — tag-checked R/W that needed tags from
  **2 different 64 B tag lines**
- `LDST_OLDEST_MTE_TAG_CHECK_CYCLE` — old LD/ST waiting for MTE tag check
- `PL2_CACHE_ACCESS` / `_INSTRUCTION` / `_LD` / `_ST` / `_MMU`
- `PL2_CACHE_MISS` / `_INSTRUCTION` / `_LD` / `_ST` / `_MMU`

---

## Ratios agents actually compute

All over `INST_ALL` unless noted. Pick the **numerator name** in the
CPU Counters picker. A missing picker name means skip that row.

### Instruction mix

| Density | Ratio | Notes |
|---------|-------|-------|
| Branch | `INST_BRANCH / INST_ALL` | Incl. calls and returns |
| Integer ALU | `INST_INT_ALU / INST_ALL` | Non-branch, non-LDST integer |
| SIMD+FP ALU | `INST_SIMD_ALU / INST_ALL` | ASIMD **and** scalar FP |
| Packed SIMD ALU | `INST_SIMD_ALU_VEC / INST_ALL` | **M2+**. Packed only. Picker `VEC`, not `VECTOR` |
| Load+store | `INST_LDST / INST_ALL` | **Not `DC ZVA`** |
| Integer load | `INST_INT_LD / INST_ALL` | |
| Integer store | `INST_INT_ST / INST_ALL` | **Not `DC ZVA`** |
| SIMD+FP load | `INST_SIMD_LD / INST_ALL` | |
| SIMD+FP store | `INST_SIMD_ST / INST_ALL` | |
| Data barrier | `INST_BARRIER / INST_ALL` | Retired `DSB`/`DMB` |

### Branch mix and direction

| Density | Ratio | Notes |
|---------|-------|-------|
| Taken (of insns) | `INST_BRANCH_TAKEN / INST_ALL` | Rec 23. Danger near **1 in 8** |
| Taken (of branches) | `INST_BRANCH_TAKEN / INST_BRANCH` | |
| Call | `INST_BRANCH_CALL / INST_ALL` | Direct + indirect |
| Return | `INST_BRANCH_RET / INST_ALL` | |
| Indirect | `INST_BRANCH_INDIR / INST_ALL` | Incl. indirect calls, **not** returns |
| Conditional | `INST_BRANCH_COND / INST_ALL` | **Footnote 1 — def changes** |
| Mispredict (retired) | `BRANCH_MISPRED_NONSPEC / INST_ALL` | Incl. calls/returns |
| Cond mispredict | `BRANCH_COND_MISPRED_NONSPEC / INST_ALL` | Pair with footnote 1 |
| Indirect mispredict | `BRANCH_INDIR_MISPRED_NONSPEC / INST_ALL` | Incl. calls/returns |

Do not compare `INST_BRANCH_COND` or Discarded fractions across families.

### Atomic / SME / delivery / memory

- Atomic fail density: `ATOMIC_OR_EXCLUSIVE_FAIL / INST_ALL` at 1 thread
  vs `hw.logicalcpu` threads, **same QoS**. Footnote 5. A density that
  **rises with thread count** is contention. Pair with `SUCC / INST_ALL`.
- SME 4 KiB isolation: `LD_BLOCKED_BY_SME_LDST` growing ⇒ core and engine
  share a 4 KiB page. Pair with `ST_BARRIER_BLOCKED_BY_SME_LDST`.
- SME packing: `INST_SME_ENGINE_PACKING_FUSED / INST_SME_ENGINE_ALU`
  (M4+). Low fused fraction + mixed core stores in the engine stream ⇒
  packing rec.
- Delivery taken-branch: M5 `MAP_DISPATCH_BUBBLE_TAKENBR_SLOT`.
- Delivery I-cache / I-TLB (M3+): `MAP_DISPATCH_BUBBLE_IC` /
  `MAP_DISPATCH_BUBBLE_ITLB` (Map not stalled).
- Memory-bound: Bottlenecks **combined Critical L1D miss** (while
  executing + stalling), not `ARM_L1D_CACHE_REFILL` alone. Counters
  cross-check: `LDST_UNIT_WAITING_OLD_L1D_CACHE_MISS` (M3+).
- Raw μPC ≈ `RETIRE_UOP / CORE_ACTIVE_CYCLE`. **Useful** is that μPC
  **divided by the maximum μPC for this core type** — do not treat raw
  IPC as the goal.

### MTE check densities (M5)

| Density | Ratio |
|---------|-------|
| Tag-checked of memory accesses | `ARM_MEM_ACCESS_CHECKED / ARM_MEM_ACCESS` |
| Reads of tag-checked | `ARM_MEM_ACCESS_CHECKED_RD / ARM_MEM_ACCESS_CHECKED` |
| Writes of tag-checked | `ARM_MEM_ACCESS_CHECKED_WR / ARM_MEM_ACCESS_CHECKED` |
| Tag-checked of reads | `ARM_MEM_ACCESS_CHECKED_RD / ARM_MEM_ACCESS_RD` |
| Tag-checked of writes | `ARM_MEM_ACCESS_CHECKED_WR / ARM_MEM_ACCESS_WR` |
| Tag-line crossing of tag-checked | `LDST_MEM_ACCESS_CHECKED_X2K / ARM_MEM_ACCESS_CHECKED` |

Oldest-access tag-check stall: `LDST_OLDEST_MTE_TAG_CHECK_CYCLE` (pairs
with Processing **Critical MTE allocation tag check**). High crossing
density ⇒ split 16 B granules / unaligned SME; rec 8 / rec 49, not a
homemade tag pool.
