<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Class latencies and unroll

Home of class latency / throughput numbers used while writing kernels.
Family *why* is `chip-families.md`. Rec 65 (LSE atomics) lives in `amp.md`.

**Unroll ≈ latency / throughput_per_cycle.** Throughput = how many units of
that class the **core type the kernel will run on** (S / P / E) can issue.
`[x]` is an **alternate path** (shifted Rm, MAC acc bypass, flag path,
MV2VEC tax), not a second independent op.

Consumer ops do **not** always start the cycle after the producer (window
entry, other operands, missing bypass). Extra undocumented bypasses exist
and **change by family**. Treat these as a **floor**, then measure
(`apple-silicon-profile`). Do not chase undocumented bypasses.

**E is not a slow P.** Same ISA, different width. M5 P is **not** M4 P
(`chip-families.md`).

---

## Integer

**ALU 1 [2].** Flag-less GPR arith/logic, `MOVZ`/`MOVK`/`MOVN`/`ORR XZR`,
shifts/bitfields (**not `BFM`**), writeback of pre/post-index, op half of
LD-op/ST-op (incl. acquire/release). **[2]** = shifted or extended Rm:
**depipelined 2-cycle occupy**. Fold the shift into the insn (one fetch);
do not count a shifted chain as latency-1 for unroll. M4+ S/P: this class
also takes the **add half of `MADD`**. `EXTR` with two different inputs
adds a 1-cycle MISC μop. M5 CSSC (`ABS`/`CNT`/`CTZ`/`SMAX`/…) is ALU 1.
MTE `GMI` is ALU 1. MTE `SUBP` dest **2** (two ALU sign-extends + the
sub). Flag-producing `SUBPS` is ALUf with the same extra extends.

**ALUf / ALU/f 1 [2].** Flag producers/consumers: `CSEL` family, `ADDS`,
`CCMP`, `ADC`, …. Same **[2]** for shifted/extended.

**BRud — no execution unit.** Unconditional direct `B`/`BL`. They still
cost **fetch / taken-branch delivery** (danger near 1 taken in 8).

**BRc 1.** `B.<cond>`, `CBZ`/`CBNZ`/`TBZ`/`TBNZ`, M5 `BC.<cond>` (hint
ignored). **BRi 1.** `BLR`/`BR`/`RET`. Combined **BRc/i** pipes exist; E
often splits BRc vs BRi.

**MAC 3 [1].** Integer `MADD`/`MSUB`/`SMADDL`/…. **[1]** = accumulator
from a previous MAC is 1-cycle. **Removed on M4+ S/P** (A18+ P): those
`MADD` **crack into MUL + ALU**. **Still emit `MADD`.** E keeps MAC
through M5. M1–M3 P keep MAC.

**MUL 3.** `MUL`/`SMULL`/`UMULH`/…. On M4+ S/P this class also takes the
**multiply half of `MADD`**.

**DIV.** S/P: 32-bit **7–8**, 64-bit **7–9**, new divide **every other
cycle**. E: 32-bit **7–13**, 64-bit **7–21**, **not pipelined**. Do not
hide a divide chain on E and expect P-like throughput.

**MISC.** `BFM` **1**. `CRC32*` **3**.

**PRED/f 3 [6][7].** SSVE/SME predicate logic, `WHILE*`/`BRK*`,
`CNT*`/`RDVL`/`ADDSVL`, `PTEST`/`PSEL`. Typical 3. S/P extra μops on
certain `Pg/Z` vs `Pg` forms (`Pd, Pg/Z, Pn, Pm`: 2 μops, 6 through
Pn/Pm and 3 through Pg; `Pd, Pg, Pn, Pm`: 3 μops, 7/6). `WHILE*` /
`PEXT` writing `{Pd1,Pd2}`: 2 independent μops each latency 3. `PSEL`
`Pd, Pn, Pm[Wv,imm]` on S/P: 2 μops, 6 through Pm/Wv and 3 through Pn.
`IRG` (MTE) **4 [3]** (4 through dest, 3 through `RGSR_EL1`). Pipe
present M4+ S/P (and M4+ E) and M5 S.

**SPEC.** MTE `ADDG`/`SUBG`. S/P **2** (depipelined), **E 1**. M5.

**`ISB` / `SB` serialize instruction delivery.** Do not sprinkle them in
kernels. Crypto DIT close is the exception (`integer-isa.md`).

### Integer pipe shape — why unroll differs

- P M1–M2 / A14–A16: ~6 integer pipes (ALU/f dense, MUL×2, MAC, DIV).
- P M3 / A17: 8 pipes, extra ALU/f.
- P M4 / A18: 8 pipes, **no MAC**, PRED/f, MUL×3, DIV.
- P M5: **4 integer pipes, not 8** (ALU/f ×4, MUL×2, DIV, MISC, PRED/f,
  SPEC). Do not copy an M4-P integer unroll of 8 onto M5 P.
- S M5: 8 pipes (ALU/f ×4 plus ALU ×4, MUL×3, DIV, PRED/f, SPEC, MISC).
  No MAC.
- E: **4 pipes**. One MUL, **keeps MAC** even on M4/M5, unpipelined DIV.
  M4+ E adds PRED/f; M5 / A19 E adds SPEC. A14 E is the narrowest
  (3 ALU/f + holes); A15+/M2+ E fills the fourth ALU/f.
- A19 P is still an 8-pipe P (M4-P shape + SPEC), not M5 P.

WHY “unroll 8 on P, ship it to E” fails: E has half the ALU/f, unpipelined
DIV, and a MAC pipe you should still use (`MADD`).

---

## ASIMD / FP

No 256-bit ASIMD. 32 × 128-bit `V`. Element letters B/H/S/D = 8/16/32/64.

**FMLA / FP MUL: typical 4 [3].** `FMLA`/`FMADD`/`FMUL`/`FMLS`/`FCMLA`/
`FMLAL*`/`FRECPS`/`FRSQRTS` / BF16 MLAL. **[3]** from **M5/A19** on
single- and double-precision mul and mul-add. Through **mul inputs**
latency ≈ `FMUL`; through the **add/acc input** can be *longer* than
standalone `FADD` (planning 4 vs 3). Cover with **~4 independent FMLA
accumulators**, reduce at the end (`asimd.md` rec 15).

**Integer SIMD MUL 3.** `MLA`/`MUL`/`SDOT`/`UDOT`/`SMLAL`/`I8MM` MMLA
(M2+). **`BFDOT` is not 3:** S/P **10**, E **12** (cracked). **`BFMMLA`:**
S/P **13**, E **16** (cracked). Still the right op when you need that
reduction.

**GEN 2–8** — pick the subtype:

| Subtype | Latency |
|---------|---------|
| MOVE (`DUP`/`INS`/`TRN`/`UZP`/`ZIP`/`MOVI`, vec `FMOV`) | **2** |
| `TBL`/`TBX` | **2–8**, cracked by table size |
| INT simple arith/logic/shift (`ADD`/`AND`/`EXT`/`SHL`/`XTN`, SHA3 `BCAX`/`EOR3`/`RAX1`/`XAR`) | **2** |
| INT compares / MIN/MAX per-element | S/P **2**, E **3** |
| INT absdiff / horizontals (`ADDV`/`UMINV`) / saturating / rounding / accumulate | **3** |
| INT simple+narrowing (`ADDHN`/`SHRN`/`SQXTN`) | S/P **3**, E **4** |
| FP/BF16 compares, `FABS`/`FNEG`/`FMAX`/`FMIN` (no flags) | **2** |
| FP add/sub (`FADD`/`FADDP`/`FABD`/`FCADD`) and `FMAXV`/`FMINV` | **3** |

**MV2GP S/P 3–4, E 3.** Vector→GPR (`FMOV`/`SMOV`/`UMOV`). S/P: S, D, and
D[1] `FMOV`/`FCVT*` = **3**; others **4**. Dual of MV2VEC: both directions
expensive vs staying in V. Prefer `FMOV` over `UMOV`/`SMOV` when no
extend is needed (rec 29).

**FCMPf S/P 5 [9], E 4 [8].** `FCCMP`/`FCMP`. **[9]/[8]** = +4 through
**input condition flags** for `FCCMP*`. A CMP→FCCMP→FCSEL chain is not
5+2.

**FCSELf 2 [6].** `FCSEL`. Data path 2; **flag path 6**.

**FDIV.** S/P: **1/cycle**; H **7**, S **8**, D **10**; **M5/A19** S **7**,
D **9**. E: same for scalars; **128-bit vectors 1 per 2 cycles and +1
latency**.

**FSQRT.** S/P: **1 per 2 cycles**; H **8**, S **10**, D **13**; **M5/A19**
S **9**, D **12**. E 128-bit: **1 per 4 cycles and +2 latency**.

Reciprocal *estimates* (`FRECPE`/`FRSQRTE`): S/P **3**, E **4**. Reciprocal
*steps* (`FRECPS`/`FRSQRTS`) are **MUL-class** (the 4-cycle FMLA bucket).

**SHA.** 2: `SHA1H`, `SHA1SU0/1`, `SHA256SU0`, `SHA512SU0/1`. 3:
`SHA256SU1`, `SHA512H/H2`. 5: `SHA1C/M/P`, `SHA256H/H2`. SHA3 logic is
GEN INT 2.

**CVT 3 [2] [+4/5] [+4/+3].** Int↔FP and `FRINT*`. **[2]** = simple
lengthens on **E** are 2. **[+4/5]** extra if GPR source (MV2VEC).
**[+4/+3]** extra if GPR dest (MV2GP). M5/A19: conversion is **inline
with the move**; only MV2VEC/MV2GP latency remains.

**AESCRYPT.** S/P **3**. E **5**, 1 per 2 cycles.

### ASIMD pipe shape — why SSVE-only usually loses

P/S typically **4 GEN pipes**; E **2** (M1–M3 / A14–A16) or **3**
(M3 Max E, A17 Pro E, M4+ E, A18+ E). FMLA MUL-class usually **2 on P,
1 on E** (M3 Max / A17 / M4+ E get a second MUL). FDIV usually **1**.
SHA **1**. MV2GP **2** on P.

- **M4 P / A18 P:** FCMPf on a third pipe; still 4 GEN, 2 MUL.
- **A19 P:** still 4 GEN (A18-P shape + SPEC on integer), not M5 P.
- **M5 S:** 4 GEN, fully populated FCMPf/FCSELf/FDIV/MUL/SHA/MV2GP.
- **M5 P: 3 GEN pipes, not 4.** FDIV and SHA still 1 each. Do not copy
  an M4-P FMLA unroll onto M5 P.
- **M5 E / A19 E:** 3 GEN, 2 MUL, FDIV still 1.

Core ASIMD can issue **several** 128-bit FMLAs per cycle on P/S. SSVE is
**one issue** of 512-bit. Width ≠ throughput (`sme-ssve.md` rec 17).

---

## Load / store

**Loads have dest latency. Ordinary stores do not.**

**L1D hit ~4 cycles** for integer and vector `LDR`/`LDP`/`LDUR` (all
sizes, including pairs). Planning figure for pointer-chasing and
load-bound unroll. These numbers are **ready-to-complete**; they do
**not** add extra wait from over-subscribing the load pipes.

Pre/post-index adds an **ALU** μop for writeback — **one** per sequence,
then `base+#imm` (rec 5). Fast load-to-load **~3 cycles** when the result
is the next **base Xn** (rec 39). MTE `LDG` **4–10**.

Vector structure: **LD1 4** (4-register dests over-subscribe a 3-load-pipe
core — at least one μop issues later; **E is worse**). **LD2/3/4 6**.
Lane `LD{1–4} {Vd}[x]`: **6** load path, **2** pass-through Vd
(4-lane 128-bit over-subscribes). **LD{1–4}R 6**. Structured LD3/ST3 are
mostly **microcoded** — still right for a long AoS stream (rec 10).

**Stores: dest N/A** except exclusives. Address compute / shuffle /
multi-address / writeback extra μops match the load counterparts, but
there is **no destination-register latency** for `STR`/`STP`/`STLR`.
Exclusive store **status 4** (`STXR`/`STLXR`/`STXP`). Execution of the
exclusive store may still **wait on ordering**. MTE tag stores (M5):
1 μop per tag write + 1 μop per 16 B granule data write; `DC GVA` /
`GZVA`: 1 μop per 4-tag write + 1 μop per 64 B data.

### Atomics / sync

**LSE dest 4** (`CAS`/`CASP`/`SWP`/`LDADD` and the other LD-op flavors,
with or without acquire/release) **after order**, assuming L1D hit.
Latency is typically through the **load and the address**, not the data
register. ST-op (`STADD`/…) has **no dest**. These numbers express
**likelihood after the memory-order constraint**, not a best-case ALU 1.
Barriers (`DMB`/`DSB`) and acquire/release can **delay issue** until
order is met. The CPU **keeps the line** through the RMW — that is why
rec 65 (home: `amp.md`) beats `LDXR`/`STXR`.

`DMB`/`DSB`/`PSSBB`/`SSBB` serialize the memory EUs under their
conditions. `ISB`/`SB` serialize **instruction delivery**, not this unit.

**MV2VEC latency 4, consumes load issue slots.** `DUP`/`FMOV`/`INS`/`MOV`
from GPR, scalar `SCVTF`/`UCVTF`. A kernel that `FMOV`s GPR→V in the inner
loop fights `LD1` for the same pipes. Load directly into V (recs 28–29).

Stores split **STA** (address) + **STD** (data). SSFP/SSVE/SME insns
(including **non-memory**) and predicate updates ship through this unit
**as a store**. **L1D / PL2 are not accessed** for those engine mem ops.

**Write-complete:** two completed writes must be the **same type** (data
store, tag store, or SME op) else one. E: **one** completed write.

### Memory EU width

- A-series, M S, M4-and-prior P: 3 load-or-MV2VEC, 2 STA, 2 STD.
  Sustained **4** μops. Sustained **2** completed writes (same type).
- **M5 P: 2 load**, 2 STA, 2 STD. Sustained **3** μops. FMLA kernels go
  load-bound sooner.
- E: burst 2 LD **or** 2 STA **or** 1 of each + 2 STD. Sustained **2**
  μops, **1** completed write.

SME ops compete with stores for the write pipe — packing engine compute
(rec 57) is a store-pipe fact.

---

## Hierarchy ballpark (M1 unloaded; order of magnitude)

| Path | Number | Unit |
|------|--------|------|
| Own L1D | **~4** | **cycles** |
| Own Shared LL | **~15** | **cycles** |
| Other-cluster Shared LL | **~50** | **ns** |
| Memory Cache | **~35** | **ns** |
| DRAM | **~95** | **ns** |

Later families’ larger LL may add a few cycles; A-series own-LL may be
slightly faster (smaller cache). Queuing and traffic add. DRAM is
**nanoseconds**, not cycles. Do not block to M Cache (`sysctl` has no
key for it). Cross-cluster modified sharing is ~**10×** intra-cluster
(`amp.md` rec 63). Loaded random chase on a live SKU is higher than
this unloaded floor. One Apple M4 Pro snapshot: `m4-pro-measured.md`.
Keep **~95 ns** here as the planning number.

---

## Engine (M4 / A18+)

Default ISA: `FEAT_SME` / `SME2` / `SME_F64F64` / `I16I64`. M5 adds
SME2p1 / F16F16 / B16B16. Match by **class**, not a mnemonic catalog.

`*` extra **core** μop when shipping GPR/predicate **to** the engine.
`**` extra **engine LSU** μop + **undefined** latency back to the core
when returning data / flags / predicates (trailing execution). Do not
bounce compares (rec 50). Opportunistic second issue during multi-issue
μops exists and is **too complex to target**.

### SSVE / SSFP classes (typically one μop/cycle)

Some occupy the issue slot extra cycles. Dest latency is per 1 / 2 / 4
output vectors. 4-vector forms are sometimes two independent 2-vector
ops that may not issue back-to-back (plan as if they did).

| Class | Dest | Issue | Notes |
|-------|------|-------|-------|
| INT 1-dest | **3** | 1 | Arith / logic / shift / permute / unfused LUTI. Compares that return pred are `**` |
| INT 2-dest | **3, 4** | 2 | |
| INT 4-dest | **3–6** | 4 | Sometimes two independent 2-dest μops |
| INT across-vector | **4** | 1 | `ANDV` / `ORV` / `SMAXV` / … |
| INT MUL | **8** | 4 | 2-dest **8, 12** at 8 issue; 4-dest up to **20** at 16 |
| INT DIV | **17** | 8 | `SDIV` / `UDIV` |
| INT CLAMP | **4** | 2 | 2-dest **4, 6** at 4; 4-dest **4–10** at 8 |
| FP abs/neg/minmax | **3** | 1 | 2-dest **3, 4** at 2; 4-dest **3–6** at 4. `**` if flags/pred return |
| Scalar FP add/mul | **5** | 1 | Incl. scalar FMLA / FRECPS |
| Vector FMLA-like | **8** | 4 | **Not** the grid class |
| Reciprocal estimate vec | **10** | 8 | |
| `FADDQV` | **11** | 3 | M5 |
| `BFDOT`/`FDOT` | **28** | 16 | **Plus 4 core→engine slots** |
| FCLAMP | **4** | 2 | Same 2-/4-dest scale-up as INT CLAMP |
| `FADDV` | H **26** (6 issue), S **21** (5), D **16** (4) | Do not inner-loop a horizontal |
| FDIV | scalar H/S/D **7/8/10**; vector **14/15/17** at 8 issue | |
| FSQRT | scalar H/S/D **8/10/13** at 2 issue; vector **22/24/27** at 16 | Subnormals extra |
| CVT | **3** | 1 | 2-dest **3, 4** at 2; 4-dest **3–6** at 4 |

SSVE-only vs 4-wide core ASIMD: one engine issue of 512-bit vs several
128-bit FMLA pipes. SSVE wins when the algorithm is ZA/grid or the extra
registers pay.

### Extract (parallel with the grid)

The extract scheduler issues **one** op/cycle, non-renamed OOO. Grid
ops that extract **block extract issue that cycle**.

Unpredicated ZA→Z `MOVA` (no convert, do not modify ZA):

| Dests | Dest latency (Z) | Slots |
|-------|------------------|-------|
| 1 | **15** | 1 |
| 2 | **15, 16** | 2 |
| 4 | **15, 16, 17, 18** | 4 (incl. MOVA–MOVA fusion) |

Extraction portion of `ST1*` / `STR` ZA is this unit (even with a
governing predicate — predicate applies on the temp→memory move).
128 B / 256 B forms cut **insn count, not extract latency**. Predicated
single-vector `MOVA` is typically **grid**, not extract.

### Grid move-from-storage (blocks extract)

Predicated / converting / modifying-ZA moves are **grid, not extract**,
and **block extract issue the same cycle** (those slots also occupy
extract).

- `MOVAZ` (M5 `FEAT_SME2p1`): Z **15**, ZA **4** (1 dest, 1 slot, also
  extract). Extra dests add 1 each (2 dest: Z 15,16 / ZA 4,5 at 2 slots;
  4 dest: Z 15–18 / ZA 4–7 at 4 slots).
- Predicated 1-dest `MOVA`: dest **16**, 1 slot, also extract.
- Fused extract + narrow/convert: dest **~16–17** (2-src, 1 slot) to
  **~17–19** (4-src). Fused extract + `ZIP`/`UZP`: **18–23** at 2–4
  slots.

Prefer rec 58 fusion over extract-on-the-side + extra convert. Direct
ZA `LDR` / `LD1Q` is a Z-load into a temp then a **grid insert that
blocks other grid issue that cycle** — default path is multi-vector
`MOVA` to Z, then store from Z.

### Grid compute and move-to-storage

Typically **one μop/cycle**, **~4-cycle accumulate**. Hide it with the
**whole 4 KiB ZA** (rec 53). Critical latency is through the
**accumulator**; end-to-end (including the multiplies) is higher.
LUTI2/4 fusion with vector/tile compute adds **negligible** latency
through the mul sources.

`[+n, +m, +p]` extra to a consumer that is a **[different compute,
horizontal extract, vertical extract]**. Scheduler multi-issue **n**:
the μop occupies the grid issue port **n** cycles. PE multi-issue
**1 (m on PE row)**: one scheduler issue, extra cycles inside a PE
row — can drop to **one μop every other cycle** if everything hits the
same PE rows; distribute ZA vectors.

Move-to-storage (`MOVA` Z→ZA): acc **4**, 1 slot typical. SME-2 wider
forms **4, 5** at 2 slots; vertical 4-src **4–7** at 4 slots. `ZERO`
portions of ZA: **4–7** at 1–4 slots. Whole-ZA `ZERO { ZA }` is the
rename (rec 53).

Tile outer-product (acc through ZA; P = SME-8, E = SME-2):

| Tile class | P acc [+n,+m,+p] | P slots | E acc | E slots |
|------------|------------------|---------|-------|---------|
| INT BMOPA / double-lengthen `SMOPA` (H→D) | **4** [+0,+1,+2] | 1 | **4** [+3,+4,+5] | 4 |
| INT H→S / B→S `SMOPA` | **4** [+1,+2,+3] | 2 | **8** [+3,+4,+5] | 8 |
| FP S→S / D→D `FMOPA` | **4** [+1,+2,+3] | 1 | **4** [+4,+5,+6] | 4 |
| FP H→S `BFMOPA`/`FMOPA` | **8** [+1,+2,+3] | 2 | **8** [+4,+5,+6] | 8 |
| FP H→H (M5 F16F16 / B16B16) | **4** [+1,+2,+3] | 2 | **8** [+3,+4,+5] | 8 |

ZA-vector FMLA / ADD (S-to-S / D-to-D / H-to-H): acc **4**, 1 slot on
SME-8. ZA-vector H-to-S DOT (`BFDOT`/`FDOT` on ZA): acc **8**, PE-row
multi-issue (1 scheduler / 2 on the PE row on SME-8; 2 slots on SME-2).
Lengthening vector MLA (INT and FP H-to-S): PE-row multi-issue, acc **4**.
SME-2 4-vector forms often split into two independent μops.

### Tile MOP throughput (full tile, insn/cyc)

Scheduler multi-issue **n** with latency 4 **and** slots 4 on SME-2
full-tile **consumes 100% of the grid**. That is why SME-2 is a
**fraction** of SME-8 on full tiles, and why you still write for SME-8
(rec 52).

| Engine | (B)Float H-to-* | Integer *-to-S | Other (S-to-S / D-to-D) |
|--------|-----------------|----------------|-------------------------|
| SME-2 | **1/8** | **1/8** | **1/4** |
| SME-8 | **1/2** | **1/2** | **1** |

VGx2 / VGx4 fewer-row forms: SME-2 can match or sit below SME-8. Do
**not** special-case a “slow engine” path that issues fewer MOPs.

### Engine LD/ST (Shared LL, not L1D)

Nominal load **~19 cycles** (queued by bank; misses add). Crack into
**64 B / 128 B** μops (one per cache line touched; size/alignment can
make them partial), then merge: up to **3 vector load** and **2 vector
store** μops across insns. Store merge exists to kill partial-64 B
**RMW**. **128 B store issues twice** (once per 64 B half-line).
**64 B unaligned store always cracks into two partial 64 B** so each
half can merge. SME-8 / SME-2 can issue **two** (merged) LD/ST μops per
cycle. Bandwidth Shared-LL → Z: SME-8 **~256 B/cyc**, E SME-2 **~128**,
M5 P SME-2 **~180**. Shared LL accepts **64 B store/cycle**.

Contiguous `LD1` / SSFP / ZT0: dest **~19–20**. Replicating `LD1R`
**~22**. Structured engine `LD2`/`LD3`/`LD4` add SSVE shuffle μops
(dest **~22–29**); prefer contiguous `LD1`. Direct ZA `LDR` **~23**
(64 B load + shuffle). Store from ZA: **1 extract + 64 B store**.
Extend addressing / writeback: extra **core ALU**. Ineligible for
merge: SSFP LD/ST, broadcast / replicating SSVE loads, SSVE LD/ST
whose **memory** element size < **register** element size.

Unaligned stores want merge; isolate core vs engine on aligned **4 KiB**
(`sme-engine.md` rec 56).

---

## Picking unroll

1. Recurring inner-loop op → its **class**.
2. Latency L here; throughput T = units of that class on **this** S/P/E.
   Start with **ceil(L/T)** independent accumulators / tiles.
3. Recheck the **load class**. A 4-acc FMLA kernel starved for `LD1` is
   load-bound. M5 P has 2 load pipes; E has 2 and sustained 2 μops.
4. Integer `MADD`: emit `MADD` always; on M4+ S/P count it as MUL 3 + ALU 1
   for port pressure, not MAC 3[1].
5. FMLA: ~4 independent accs on P (L=4, T≈2 MUL pipes) before spill
   (~25 live of 32 V). M5 S/D FMLA [3] can drop one acc if you measure it.
6. Grid: cover ~4-cycle acc with the **whole ZA**. Full-tile T is 1 on
   SME-8 (S-to-S) vs 1/4 on SME-2 — occupancy, not fewer MOPs.
7. GPR→V in the loop: MV2VEC 4 **and** a load slot. Don’t.
8. SSVE: one issue. Dest-8 vector FMLA needs ILP, but the unit is
   **1-wide**. Cover with ZA/grid or stay on core ASIMD.
9. Stores have no dest to unroll against except exclusives / LSE (4).
10. Floor, then measure (CPU Bottlenecks Useful vs Critical L1D miss vs
    Execution latency).

---

## SKU snapshot

One-SKU loaded chase and width-ladder wall-clock:
`references/m4-pro-measured.md`. Keep the table above as the unloaded
planning floor. Unroll ≈ latency/throughput is a floor; time both
core types.
