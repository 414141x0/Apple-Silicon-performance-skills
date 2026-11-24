<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# SSVE / SME ISA

Owns recs **17–18**. Engine microarchitecture (bounce, 4 KiB isolation,
SME-8, fusion) is `sme-engine.md`. Class latencies and Shared-LL→Z
bandwidths: `latency-lookup.md`. SME-8 vs SME-2 placement:
`chip-families.md`. Do not dump mnemonic catalogs.

Query `hw.optional.arm.FEAT_SME` before emitting any of this
(`sysctl.md`). SME debuts at **M4 / A18+**. A binary whose baseline is M1
must keep an ASIMD path. Rosetta (`sysctl.proc_translated == 1`): no SME.

---

## What SME is, and what SSVE is

- **SME** is the 2D `ZA` array plus the Processing Grid. Tiles and ZA
  vectors live here. This is the high-throughput path (`FMOPA`, other MOP
  outer products, tile-wide ops).
- **SSVE** is the supporting vector ISA: 32 × SVL-bit `Z` registers and 16
  `P`/`PN` predicates. `Z` is the staging file into and out of `ZA`. In
  streaming mode it **replaces ASIMD**. Some Z/P-only ops (`MOVT` into
  `ZT0`) are still `FEAT_SME`, not non-streaming SVE.
- **SSFP** is scalar FP while streaming; it aliases the **low bits** of
  `Z` (high bits ignored on read, zeroed on write) and runs on the
  engine’s SSVE unit (narrower than core ASIMD FP). Names: `Bn`
  (limited), `Hn`, `Sn`, `Dn`. No ASIMD-style half-width view; length is
  SVL; predication selects elements.

Apple silicon does **not** implement non-streaming SVE. `FEAT_SVE` /
`FEAT_SVE2` are unset. There is no “SVE while SM=0” path. Do not emit
non-streaming SVE, and do not check `FEAT_SVE` as a proxy for SME.

`FEAT_SME_FA64` is **not** implemented — many ASIMD encodings stay
illegal in streaming mode. Exit SM to use `V`.

---

## Rec 17 — Target ZA + Processing Grid; SSVE supports; else core ASIMD (High)

Design the algorithm around **outer products / tiles that accumulate into
ZA**. Use SSVE to:

- load / store / rearrange tiles into `Z`
- predicate tails
- extract / convert after the grid

Do **not** rewrite a reduction that never touches ZA as “SSVE because it is
wider”. Algorithms that cannot use the grid belong on **core ASIMD** (four
vector ports on recent P cores vs one SSVE issue). SSVE-only is rarely a
win — `sme-engine.md` rec 51.

SSVE `Z` is 4× the bits of a `V` register, but the engine SSVE unit issues
**one** vector μop per cycle (and many mul/add μops take multiple issues).
Net: a 128-bit 4-accumulator `vfmaq_f32` kernel on the core often beats a
512-bit SSVE kernel that never uses ZA. Use SSVE without ZA only for a
short staging sequence next to a grid loop, or when SM=1 is already paid.

Tiles that still fit in **L1D** can be faster as core ASIMD: the engine
LSU talks to Shared LL, not L1D (`sme-engine.md`).

If Accelerate already has the GEMM (`cblas_sgemm`) or a BNNS op, rec 2
still applies (`integer-isa.md`).

Inner iteration = MOP into ZA (`FMOPA`/`SMOPA`/…), not SSVE `FMLA` that
never lands in ZA.

---

## Rec 18 — Apple SVL is 512b / 64 B, ZA is 4 KiB; do not hardcode SVL

On current Apple silicon:

- Streaming vector length (SVL) = **512 bits = 64 bytes**
- `ZA` = SVL × SVL bits = **4 KiB** per core
- `ZT0` lookup table lives beside the Z file
- 32 × SVL-bit `Z`; 16 × `P`/`PN` (64-bit predicates, one bit per SVL byte)
- Apple ZA vectors = **64** rows

Arm SVL range is 128b–2048b. Apple may change.

**Portable code must not bake `64` in as “the vector length.”** Read it:

```asm
    rdsvl   x8, #1              // x8 = SVL in bytes (even if SM=0, needs FEAT_SME)
    cntb    x9                  // element count in bytes, all lanes
    cnth    x10
    cntw    x11
    cntd    x12
```

Use VL-scaled addressing and **`ADDSVL` / `INC*` / `CNT*` / `DEC*`**, not
`add x0, x0, #64`. Local comments may say “64 B on this chip”; immediates
in the kernel must come from `RDSVL`/`CNTB` unless the function is
explicitly Apple-SVL-only and named that way.

**Never emit `ADDVL`.** `ADDVL` is non-streaming SVE. Apple has no
`FEAT_SVE`/`FEAT_SVE2`. `ADDVL` is illegal when SM=0. `ADDSVL`/`RDSVL`
are `FEAT_SME` and work even outside SM.

`svfloat32_t` is **SVL-sized** (16× f32 on Apple, not a compile-time 16).

---

## FEAT gates

Chips with `FEAT_SME` also have `FEAT_SME2`, `FEAT_SME_F64F64`, and
`FEAT_I16I64`. Later families add types. Subfeats that “ride along” on
Apple must still be **queried** (`sysctl.md`), not assumed from “has SME”
on a hypothetical non-Apple SME CPU.

| Feat | From | Agent action |
|------|------|--------------|
| `FEAT_SME` | M4 / A18+ | Gate the TU. Implies **SSVE**, not non-streaming SVE. |
| `FEAT_SME2` | M4 / A18+ | Always on with Apple SME. |
| `FEAT_SME_F64F64` | M4 / A18+ | D-tile FP64 outer product. |
| `FEAT_I16I64` / `FEAT_SME_I16I64` | M4 / A18+ | 16b→64b integer widening outer product. |
| `SME_B16F32` / `SME_F16F32` / `SME_F32F32` / `SME_I8I32` | M4 / A18+ | Bundled with `FEAT_SME`. |
| `SME_I16I32` / `SME_BI32I32` | M4 / A18+ | Bundled with `FEAT_SME2`. Binary MOP is `BMOPA`. |
| `FEAT_I8MM` | M2+ ASIMD; present whenever SME is | Mixed-sign ZA-vector DOT (`SUDOT`/`USDOT`). |
| `FEAT_SME2p1` | **M5 / A19+** | `MOVAZ`, ZERO of ZA **vector** forms, `PMOV`, Q/QV 16 B-segment ops, strided LUT dest. |
| `FEAT_SME_F16F16` | **M5 / A19+** | Non-lengthening FP16→FP16 ZA (`FMOPA` H-to-H, `FMLA` H-to-H). |
| `FEAT_SME_B16B16` | **M5 / A19+** | Non-lengthening BF16→BF16 ZA (`BFMOPA`, `BFMLA`/`BFADD`/`BFSUB`). |
| `FEAT_SVE_B16B16` | **M5 / A19+** | Non-widening BF16 **SSVE** (not core ASIMD). |
| `FEAT_SVE` / `FEAT_SVE2` | **never** | Unset. Do not emit non-streaming SVE. Do not use as an SME proxy. |
| `FEAT_SME_FA64` | **never** | Many ASIMD encodings stay **illegal** in SM. |
| `FEAT_MOPS` | **not** on M5 | Unrelated memcpy ISA. Do not emit MOPS memcpy as “M5 SME”. |

**Pre-M5 SME MAC is lengthening-only** (H/BF16 → S, i8 → i32, …). Gate
`MOVAZ`/`PMOV`/Q/QV/strided LUT/ZERO-vector-range on `FEAT_SME2p1`. Gate
H-to-H / BF-to-BF compute on `FEAT_SME_F16F16` / `FEAT_SME_B16B16`.

---

## Registers

### Z (32)

`Z0–Z31`. Element views: `.B` / `.H` / `.S` / `.D` / `.Q` (Q limited).
Same type family as ASIMD; smaller types more often **lengthen**.

Multi-vector Z patterns (check the insn; do not guess — the two
four-vector spellings are **not** interchangeable):

- Consecutive arbitrary, wrap (ASIMD-like): e.g. `LD4H`, some `ADD`.
  Comma list `{Z1.T, Z2.T, Z3.T, Z4.T}`.
- Consecutive **aligned**: range `{Z0.T–Z3.T}`. ×2 starts even
  (`{Z0,Z1}`, `{Z2,Z3}`, …); ×4 starts a multiple of 4 (`{Z0–Z3}`,
  `{Z4–Z7}`, …).
- **Strided**: comma list. ×2 starts `Z0–Z7` or `Z16–Z23`, stride 8;
  ×4 starts `Z0–Z3` or `Z16–Z19`, stride 4. Four-vector row LD/ST is
  usually this form + counted `PN`.

Limited specifiers force copies + careful RA. Indexed VGx4 `FDOT`/`FMLA`
(and friends): `Zn1` multiple of 4, `Zm` in **`Z0–Z15`**. ZA dest is
still **W8–W11** (do not move it to W12). Encoding is 32-bit → many
SSVE ops are **destructive** even without an accumulator.

### P / PN (16) — same file, two encodings

Length scales with SVL: **one bit per SVL byte** → 64 bits on Apple.
FALSE lanes skip the op. Intrinsics: `svbool_t` / `svcount_t`.

| Style | Registers on most ops | Use |
|-------|----------------------|-----|
| Bitmask `Pn` | **P0–P7** | One bit per byte; larger elements use the **low byte’s** bit. Independent per-lane if/then **and** length. **Single vectors only.** A few ops can use any P. |
| Counted `PNn` | **PN8–PN15** | Contiguous TRUE run from **one end or the other** + element size; HW expands to a bitmask. Multi-vector / remainder. `WHILE` needs `VLx2` or `VLx4` to match consumer width. Count+size always in the **low 16 bits**, independent of SVL. |

- All-FALSE is `0` for both. All-TRUE encodings **differ** and are **not
  interchangeable**. Do not feed a counted all-TRUE into a bitmask
  consumer or vice versa.
- Consumers do **not** check that the PN element size matches the op.
- Predicate logic `AND`/`BIC`/`EOR`/… is **not** for counted predicates.
- `PEXT`: counted PN → up to four bitmask P (two `PEXT` to extract all
  four). GEMM: counted `WHILE … VLx2` for two-vector LD/ST, then `PEXT`
  to two bitmask P for the 2D MOP.
- `PMOV` (M5 `FEAT_SME2p1`): bitmask P ↔ packed bitfield in a Z lane.
- `MOV[S]` / `LDR` / `STR` of predicates. `MOVS` sets NZCV.
- `PSEL`: pick a predicate or all-FALSE from one bit of another P.
- Rearrange `PUNPK`/`REV`/`TRN`/`UZP`/`ZIP` **in lockstep** with the
  matching data shuffle.

`/M` merge vs `/Z` zero on FALSE dest lanes. `/M` must read dest; `/Z`
need not. **Loads are always `/Z`.** Stores skip FALSE lanes (merge-like;
no `/M`/`/Z` notation). Compute is often unpredicated over load-zeros,
with the store predicated. Predicate-manipulation with a governing
predicate uses `/Z`. FALSE load lanes must not fault or read device
memory; dest those lanes zero.

**2D MOP takes two predicates** (row AND column), both `/M`. Usual use is
matrix-edge tails. The AND can also **checkerboard** independent vector
streams into the tile. ZA-vector compute is generally **unpredicated**.

Write-time split (do not ping-pong):

| Core (from GPRs / P file) | Engine |
|---------------------------|--------|
| `PTRUE`/`PFALSE`, `WHILE*`, `INC*`/`DEC*`/`CNT*`, `ADDSVL`/`RDSVL`/`ADDSPL`, `PEXT`, `PSEL`, predicate `MOV`/`LDR`/`STR`, bitmask logic, P-shuffles, loop branches | `CM`/`FAC`/`FCM` (and friends) that **write** predicates; **governing** predicates on SSVE/SME compute and LD/ST |

A compare that must come back to the core (`PTEST` / `WHILE` / a GPR
decision) to issue the next engine insn pays rec-50 transit
(`sme-engine.md`). Some compares set NZCV; those flag reads are the
same tax. Per-element if/then via compare-generated predicates is **not
generally recommended** on Apple.

Tails: `WHILE<cc>` against the remaining count, not a hardcoded
`0xFFFF`. **Match the C comparison** — `WHILELT` for `i < n`, `WHILELE`
for `i <= n`. `RW`/`WR` forms also test two address ranges for a
same-iteration memory dependence. Single-vector → `P0–P7`. Multi-vector
LD/ST → `PN8–PN15` + `WHILELT … VLx2/VLx4`.

Predicate NZCV: **N** first-active TRUE; **Z** none TRUE; **C** not-last;
**V** unused (0). `CTERM` uses a unique NZCV (`N`/`V` encode continue /
last-selected / compare-succeeded).

### ZA tiles `ZA<n>.<size>`

Square **SVL(bytes)×SVL(bytes)** → SVL² bytes = **4 KiB** on Apple. Tile
**count depends on element size, not SVL**:

| Type | Tiles | Shape (Apple 64-row) |
|------|-------|----------------------|
| B | 1 (`ZA0.B`) | 64×64 B |
| H | 2 (`ZA0.H`–`ZA1.H`) | 32-row, **row-interleaved** |
| S | 4 (`ZA0.S`–`ZA3.S`) | 16-row, interleaved |
| D | 8 (`ZA0.D`–`ZA7.D`) | 8-row, interleaved |
| Q | 16 | **no compute**; ignore |

Tiles **interleave by rows** across the 64-row array, not packed
contiguously. Mixed-type ZA is **aliased storage**: `ZA3.D`/`ZA7.D`
cannot coexist with `ZA3.S` (overlapping byte rows). Horizontal access
is default. Vertical is in the mnemonic (few compute ops: `ADDVA`,
`VDOT`) or the ZA operand (`ZA2H[4]` vs `ZA2V[3]` for MOV).

### ZA vectors `ZA.<type>[row(s)]`

Row select = GPR `Wv` + small imm, **modulo** vector count. Apple:
**64 vectors**. Portable ZA-vector code is harder than tiles: min SVL =
16 B → only 16 vectors; indices **0 and 16 alias** at min SVL, distinct
at Apple 64. Highest Apple throughput uses all 64 rows. Multi-platform
ZA-vector code must keep row count ≤ hardware SVL.

Few ops (mainly LD/ST) treat ZA as 64×64 B vectors: `LDR`/`STR ZA[Wv, off]`.
For 64 B-only compute, SSVE Z is more efficient. SME vector compute
accelerates by touching up to **16×64 B rows** via lengthening
(double/quad consecutive ZA vectors; even / multiple-of-4 start) and via
VGx groups.

**VGx2 / VGx4:** indexable vector count halves / quarters. VGx2 splits ZA
horizontally (low 64 B at row, high at **+32**, `% 32`). VGx4 uses
**+0/+16/+32/+48**, `% 16`. Baseline `% 64`. Lengthening **and** VGx can
combine.

- Double (L): `imm:imm+1`, even start; if odd, `access_row = row - (row % 2)`.
- Quad (LL): `imm:imm+3`, start multiple of 4; `access_row = row - (row % 4)`.

Tiles and vectors **alias the same rows** — correctness + reuse-hazard
(`sme-engine.md` rec 53).

**No** arithmetic, logic, or copy **between** ZA tiles or between ZA
vectors. Restage through Z, or rename with `ZERO { ZA }`. M5 `MOVAZ` is
extract+zero, not a tile-to-tile copy.

### ZT0

Dedicated LUT source. Index vector is packed 2b or 4b codes; the
**segment** of that vector is the insn immediate. Segment size depends
on index width, dest element size, and dest-vector count.

- `LUTI2` — 2b index → 4 entries from `ZT0`
- `LUTI4` — 4b index → 16 entries from `ZT0`

Consecutive aligned 1/2/4 dest vectors; **`LUTI4` 8b × 4 dest is not
supported**. M5 `FEAT_SME2p1`: 2/4-dest also in **strided** patterns
(stride 8 / 4) — strided LUT does **not** fuse (rec 58). Move / load /
store / zero: `LDR`/`STR ZT0`, `MOVT` 8 B GPR↔`ZT0[off]`, `ZERO { ZT0 }`.
Typical ML dequant (expand quantized weights). LUTI→compute fusion:
`sme-engine.md` rec 58.

---

## Streaming mode and ZA mode

`SMSTART` / `SMSTOP` are aliases of `MSR SVCR{SM,ZA,SMZA}`
(`MSR SVCRSM, #0/#1`, `SVCRZA`, `SVCRSMZA`).

| Instruction | Effect |
|-------------|--------|
| `SMSTART` | SM=1 **and** ZA=1 |
| `SMSTART SM` | SM=1 only |
| `SMSTART ZA` | ZA=1 only |
| `SMSTOP` | SM=0 **and** ZA=0 |
| `SMSTOP SM` | SM=0 (**ZA held**) |
| `SMSTOP ZA` | ZA=0 |

**There is no nested SM/ZA pair. An extra `SMSTART` is safe. The first
`SMSTOP` exits.** Compiler wraps streaming callees with SMSTART/SMSTOP at
the ABI boundary; a nested pair inside will **exit the caller’s streaming
region**. Inline streaming callees so the compiler does not save/restore
**4 KiB of ZA** around a call. Custom code may treat SSVE/SME regs as live
across calls. Dual **n** and **s** versions of a helper are useful when
the same math must run in both worlds.

Clang: `__arm_streaming` (`s`), `__arm_streaming_compatible` (`sc`);
non-streaming is `n`. Do **not** invent ACLE names. Include `<arm_sme.h>`
and use this toolchain’s spellings (`__arm_streaming`, ZA new/commit
attributes). If ACLE spelling is uncertain, emit a C wrapper around an
`asm` outline.

### SM=0 vs SM=1 availability

Apple has **no** non-streaming SVE (`FEAT_SVE` / `SVE2` unset).
`FEAT_SME_FA64` unset → many ASIMD encodings stay illegal in SM.

| | SM=0 (non-streaming) | SM=1 (streaming) |
|--|----------------------|------------------|
| Scalar FP ISA | yes (V subsets) | yes, as **SSFP** (Z low bits; same encodings) |
| ASIMD `V` | yes | **no** — illegal exception |
| SSVE `Z`/`P` | **no** | yes |
| SME (ZA compute / tile LDST / MOVA) | **no** | yes, and **ZA=1** |
| ZA `LDR`/`STR` vector | only if **ZA=1** | if ZA=1 |
| Integer GPR | yes (survives the switch) | yes |
| Scalar FP / `V` / `Z` / `P` contents | `V` live | **zeroed on enter and on exit** |

While **ZA=1 and SM=0**: only **`LDR`/`STR` of ZA vectors** are legal.
Mode exists so a short core excursion can keep ZA live (`sme-engine.md`
rec 51). ZA is **zeroed on ZA-mode exit**. SSVE/SME outside SM → illegal
exception.

**Scalar FP registers are not preserved across SM enter/exit** (they are
V subsets out of SM and Z subsets in SM). Spill scalar FP **before** SM
enter if the value must survive. GPRs (including W8–W15 ZA cursors)
**do** survive. Move a scalar-FP epilogue with `SMSTOP SM` (ZA held)
onto the core, then re-enter — rec 51. SSFP is narrower than core FP;
do not run a long SSFP chain.

Single-thread memory: SSVE/SME loads/stores appear in **program order**
against older ASIMD/integer stores and vice versa. Across threads the
model is still **weak** (barriers). Predicated-off lanes: no device
read, no fault, dest zero.

Do not flicker `SMSTART`/`SMSTOP` around a handful of instructions. Batch
a whole tile kernel inside one streaming region. Do not leave ZA on
across unrelated work (`sme-engine.md`: power, cluster frequency, CS).

---

## ZA addressing: W8–W11 vs W12–W15

Eight GPRs hold ZA positions: **compute + VGx MOVA use W8–W11; LDR/STR +
tile-slice movement use W12–W15.** Mixing these is a silent wrong-row /
illegal-encoding bug.

### Compute — index GPR **W8–W11**

- Single vector: `ZA.T[Wv, imm{, VGx2/4}]` — `imm=0–7`. Ops: vector
  `ADD`/`SUB`, `FADD`/`FSUB`, `FMLA`/`FMLS`, `DOT`, M5
  `BFADD`/`BFSUB`/`BFMLA`/`BFMLS`, M5 `ZERO` of these forms.
- Double: `ZA.T[Wv, imm:imm+1{, VGx2/4}]` — even `imm`; max
  `{14, 6, 6}` for 1 / VGx2 / VGx4 groups. `MLAL`/`MLSL`, M5 `ZERO`.
- Quad: `ZA.T[Wv, imm:imm+3{, VGx2/4}]` — `imm` multiple of 4; max
  `{12, 4, 4}` for 1 / VGx2 / VGx4 groups. `MLALL`/`MLSLL`, M5 `ZERO`.
- **Whole tile:** `ZAn.T` — **this is the MOP dest**. `<T>=S`: `n=0–3`;
  `<T>=D`: `n=0–7`. Ops: `ADDHA`/`ADDVA`, **`MOP` (`FMOPA`/`SMOPA`/…)**.
- ZERO list / `{ZA}`.

**Tile MOPs dest is `ZAn.T` (`ZA0.S`–`ZA3.S`, `ZA0.D`–`ZA7.D`), NOT
`ZA.T[Wv]`.** `fmopa za.s[w12, 0]` is the **wrong form and the wrong Wv**.
Correct:

```asm
    fmopa   za0.s, p0/m, p1/m, z0.s, z4.s
```

Vector FMLA/DOT/MLA **into ZA** uses `ZA.T[W8–W11, imm{, VGx2/4}]`. That
is a different instruction class from tile MOP.

### Movement

| Form | Wv | Ops |
|------|----|-----|
| `ZA[Wv, imm]` 1 row, `imm=0–15` | **W12–W15** | `LDR` / `STR` ZA vector |
| `ZA.D[Wv, imm{, VGx2/4}]` 1/2/4 ZA vectors | **W8–W11** | **`MOV` / `MOVA` / M5 `MOVAZ` VGx** |
| `ZAn<HV>.T[Ws, imm]` 1 tile slice | **W12–W15** | `LD1*` / `ST1*` / `MOV` / `MOVA` / M5 `MOVAZ` |
| 2 contiguous slices `imm:imm+1` (even) | **W12–W15** | tile-slice `MOVA`/`MOVAZ` |
| 4 contiguous slices `imm:imm+3` (×4) | **W12–W15** | tile-slice `MOVA`/`MOVAZ` |

Tile-slice **imm ranges by type**:

| Slices | B (`n=0`) | H (`n=0–1`) | S (`n=0–3`) | D (`n=0–7`) | Q (`n=0–15`) |
|--------|-----------|-------------|-------------|-------------|--------------|
| 1 | `imm=0–15` | `0–7` | `0–3` | `0–1` | `imm=0` |
| 2 (`imm` even) | `0–14` | `0–6` | `0–2` | `imm=0` | — |
| 4 (`imm` ×4) | `0–12` | `0–4` | `imm=0` | `imm=0` | — |

**Rec 58 VGx MOVA keeps W8. Do not “fix” it to W12.**

```asm
    mova    {z0.d, z1.d, z2.d, z3.d}, za.d[w8, #0, vgx4]
```

`ZA.D[w8, …, vgx4]` is **compute-file addressing**, same W8–W11 as vector
FMLA. Tile-slice `MOVA ZA0H.S[w12, 0]` is the other file. ZIP/convert
fusion after extract: `sme-engine.md` rec 58.

Movement cannot see full-grid parallelism: only small groups of 64 B
vectors or 64 B tile slices. **No grouped access of lengthened
double/quad as a group** — index them as larger-type singles. `ZA0H.B`
tile slices work well to read lengthened results (adjacent rows).

Vertical **MOVA** extracts a matrix **already transposed**. Prefer
extracting C as CT rather than a later transpose. Vertical **VDOT** can
compute on data that exists only transposed. Dedicated transpose (load
via `ZA0H`, extract via `ZA0V`) is almost as expensive as real compute.
Small matrices that fit L1D may be faster in ASIMD.

MTE: SME accesses to tagged memory **are checked** (`integer-isa.md` rec
8). Predicated-off **16 B granules are not checked**.

Extract with the **family you accumulated into** (tiles and vectors
alias the same rows, but the **layout** differs):

- Tile MOP → horizontal slices `ZAnH` for C, vertical `ZAnV` for CT
  (**W12–W15**). Vertical extract **is** the transpose.
- ZA-vector FMLA/DOT → VGx `MOVA` (**W8–W11**). Not a transpose.

Write-time:

1. Tile MOP dest = `za0.s`…`za3.s` / `za0.d`…`za7.d`. Never `za.s[wN]`.
2. Vector FMLA/DOT/MLA/ZERO-vector into ZA = `za.s[w8, #imm, vgx4]`
   (W8–W11). Indexed VGx4: `Zm` in `Z0–Z15`, `Zn1` multiple of 4.
3. VGx `MOVA`/`MOVAZ` ZA-vector = W8–W11. Rec 58 example is correct.
4. `LDR`/`STR ZA` and tile-slice `LD1*`/`ST1*`/`MOVA` = W12–W15.
5. If an agent “fixes” rec 58 `w8` → `w12`, revert it.

---

## MOP (tile) vs vector FMLA into ZA

Two different ISA families, both write ZA:

| | Tile MOP (`FMOPA`/`FMOPS`/`SMOPA`/…) | ZA-vector FMLA/DOT/MLA |
|--|--------------------------------------|------------------------|
| Dest | **`ZAn.T` whole tile** | `ZA.T[W8–W11, imm{,VGx}]` |
| Shape | Outer product: Zn across **columns**, Zm across **rows**; every Zn×Zm pair into the tile | Lane-wise / indexed / multi-vector MAC into ZA rows |
| Predicates | **Two** (row AND col), `/M` | Generally **unpredicated** — predicate LD before / ST after |
| Role | **GEMM inner kernel** (rec 17) | Matvec / row-wise MAC when tiles are the wrong shape |
| MOPA vs MOPS | multiply then **add** vs **subtract** | FMLA vs FMLS |

MOP cells: D = one row×col per cell, 8 D-tiles; S = 2×2 elements per
cell, 4 S-tiles. 8b/16b forms are 4-way / 2-way **diagonal** dots inside
the 32/64b cell. With interleaved operands, one MOP does 1/2/4 inner-loop
k-steps.

`ADDHA` / `ADDVA`: broadcast a Z vector across rows or columns and add
into a tile. H/V in the mnemonic.

ZA vector ops: interleave/deinterleave is **SSVE, not ZA**. Lengthening
MLA/DOT write consecutive ZA rows in **interleaved** order. After
extract, **ZIP** to restore input order (rec 58 fusion pair,
`sme-engine.md`).

FP ZA tile **and** vector ops **support denormals**.

`ZERO { ZA }` at the start of a grid — whole-ZA **rename** (rec 53). Mask
can be one tile, a list, or `{ZA}`. M5: vector ranges too. Prefer
whole-ZA zero over piecemeal row zeros when starting a new accumulation.

GEMM inner loop = four (or eight) back-to-back `FMOPA zaN.s, pRow/m,
pCol/m, zA, zB`. Matvec = VGx4 `FMLA ZA.S[w8, 0, vgx4]` indexed, not a
fake MOP. Never emit SSVE `FMLA` into `Z` as the inner kernel of a
matmul.

---

## Type matrices

Pick the densest type that covers range. `<32b` usually L (2-way) or LL
(4-way) into a wider acc. **Pre-M5 SME MAC is lengthening-only.** M5
F16F16/B16B16 = same-size 16b dest. Do not invent a type the feat does
not list.

### ZA tiles (MOP) — dest is `ZAn.T`

| Src → dest | Widen | Insn | Gate |
|------------|-------|------|------|
| BF16 → BF16 | none | `BFMOPA`/`BFMOPS` | M5 `FEAT_SME_B16B16` |
| FP16 → FP16 | none | `FMOPA`/`FMOPS` | M5 `FEAT_SME_F16F16` |
| BF16 → FP32 | L, 2-way | `BFMOPA`/`BFMOPS` | M4+ |
| FP16 → FP32 | L, 2-way | `FMOPA`/`FMOPS` | M4+ |
| FP32 → FP32 | none | `FMOPA`/`FMOPS` | M4+ |
| FP64 → FP64 | none | `FMOPA`/`FMOPS` | `FEAT_SME_F64F64` |
| i8 s/u/mixed → i32 | LL, 4-way | `SMOPA`/`UMOPA`/`SUMOPA`/`USMOPA` (+ `S` subtract) | M4+ |
| i16 s/u/mixed → i32 | L, 2-way | same family | M4+ |
| i16 s/u/mixed → i64 | LL | same family | `FEAT_SME_I16I64` |
| i32 / i64 → same | none | `ADDHA`/`ADDVA` | M4+ |
| 1b × i32 | none | `BMOPA`/`BMOPS` | SME2 |

MOP is **row- and column-predicated** (`/M`, AND). 8b/16b cells are
**diagonal dots** inside the 32/64b lane — ZIP inputs so a column’s
k-elements become adjacent (lengthening GEMM below).

### ZA vectors — dest is `ZA.T[W8–W11, imm{,VGx}]`, generally unpredicated

| Src → dest | Widen | Insn class | Gate |
|------------|-------|------------|------|
| BF16 → BF16 | none | `BFADD`/`BFSUB`/`BFMLA`/`BFMLS` | M5 B16B16 |
| FP16 → FP16 | none | `FADD`/`FSUB`/`FMLA`/`FMLS` | M5 F16F16 |
| BF16 → FP32 | L | `BFDOT`/`BFVDOT`, `BFMLAL`/`BFMLSL` | M4+ |
| FP16 → FP32 | L | `FDOT`/`FVDOT`, `FMLAL`/`FMLSL` | M4+ |
| FP32 / FP64 → same | none | `FADD`/`FSUB`/`FMLA`/`FMLS` | M4+ |
| i8 → i32 | LL | `SDOT`/`UDOT`/`SUDOT`/`USDOT`, `SMLALL`/`UMLALL`/`SMLSLL`/`UMLSLL`, `SVDOT`/`UVDOT`/`SUVDOT`/`USVDOT` | mixed-sign = I8MM |
| i16 → i32 | L | `SDOT`/`UDOT`, `SMLAL`/`SMLSL`, `SVDOT`/`UVDOT` | M4+ |
| i16 → i64 | LL | `SDOT`/`UDOT`, `SMLALL`/`SMLSLL`, `SVDOT`/`UVDOT` | I16I64 |
| i32 / i64 → same | none | `ADD`/`SUB` | M4+ |

V prefix = **vertical DOT**: reads the first operand **across** the
multi-vector (ZIP-then-DOT). VDOT is **quadword-indexed Zm only**.

ZA VGx2/VGx4 FMLA three varieties:

1. **Indexed** — one element per 16 B segment (`Zm.T[index]`).
2. **Single vector** — each Zm element vs each multi-vector.
3. **Multi-vector** — elementwise across two multi-vectors.

Integer SSVE: full 8/16/32/64 signed+unsigned; some 8b and 64b multiplies
omitted. Lengthen one (sometimes two) steps; narrow the reverse. `LL` is
new: integer SME MAC that **quadruples** element width (mul widen + acc
headroom), skipping a preliminary lengthen.

Pre-M5: **no** BF16-in/BF16-out compute; **no** FP16 accumulators on SME
MAC (SSVE still has a full half op set + lengthening MAC to FP32).
Converts exist.

---

## Complex, MOVPRFX, pairwise, lengthen/narrow, indexing

SSVE has interleaved real/imag FP and integer (`FCMLA` / `FCADD`, prefix
`C`). **SME generally does not.** Keep complex on SSVE staging or on core
ASIMD. Do **not** invent a ZA complex MOP.

Prefer a dual form when it exists: predicated destructive vs
unpredicated non-destructive (predicate bits reused as dest specifier).
Example: predicated `ADD Zd, Pg/M, Zd, Zm` vs unpredicated
`ADD Zd, Zn, Zm`. Predicated `FMUL` is destructive `/M`; unpredicated
`FMUL` has a distinct dest.

**`MOVPRFX` immediately before** a destructive SSVE op synthesizes a
non-destructive dest when no dual form exists; the pair may fuse. Place
it **immediately** before the op — nothing in between.

Reversed-input **R** forms for non-commutative destructive ops
(`SUB`/`DIV`) so either src can be dest.

**SSVE pairwise (`ADDP` and friends) INTERLEAVES.** Dest keeps each
adjacent source pair local (`A0+A1` next to `B0+B1`), not ASIMD’s
pack-all-of-B-then-all-of-A. Do **not** port an ASIMD `ADDP` packing
picture onto SSVE. Broad rule: sources feed same or nearby lanes.
Lengthening pairwise (`LP`) occupies the same byte positions as the
paired sources. `V` = reduce whole vector to a scalar (or `QV` to 16 B).

N/L/LL/W are **lane-local** because long shuffles are expensive.

- SSVE narrow: **B/T** fill even/odd dest lanes (interleaved like ASIMD
  B/T, **not** deinterleaved 1/2). `NB`/`NT` low half even/odd;
  `HNB`/`HNT` high half. Truncate / saturating `Q` / rounding `R`. Any
  long `FCVT` without `LT` acts as `LB`; any narrow `FCVT` without `NT`
  acts as `NB`.
- SME narrowing: **deinterleaved** (`FCVT`/`SQCVT`/`SQRSHR`) vs
  **interleaved** (`N` suffix). Different from SSVE B/T.
- SSVE widen: B/T/BT select even/odd **sources** (interleaved, unlike
  ASIMD 1/2). `LB`/`LT`/`LBT`/`WB`/`WT`.
- SME lengthen: one Z in, two Z out (called SME-ISA even though dest is
  Z). Deinterleaved `FCVT` vs interleaved `FCVTL`. `L` H-to-S writes two
  consecutive ZA rows; `LL` H-to-D writes four.
- ZA lengthen/narrow **may use a different order** than SSVE B/T — pick
  the order that matches the rest of the kernel, then ZIP/UZP once.
- Extra Z conversion-only narrow ops exist to feed SME.
- ASIMD 1/2 (low/high half) is rare in SSVE/SME (`TRN`, `UNPK` HI/LO,
  `UZP`, `ZIP`). Porting 1/2 ASIMD to B/T needs data-layout work: B
  writes even+zeros, T merges odds; UZP1/2 pack.

Indexing:

- Full-vector: `TBL`/`TBX`.
- Per-doubleword shift amount (wide `LSR`).
- **Per-quadword** indexed (one scalar per 16 B, like ASIMD). Many
  algorithms want one scalar everywhere: replicate across the four 16 B
  segments.
- `LD1RQ{B,H,W,D}` loads 16 B and replicates through the Z vector so each
  segment’s indexed lane is the same value. Element size is for the
  predicate.
- Alternate: `DUP yIndex.Q, yIn.Q[k]` then ops, or rotate with `EXT #16`
  and always `DUP.Q[0]` to avoid unroll.
- `LD1R` broadcasts one element (widening as needed) through Z.

M5 `FEAT_SME2p1` **Q / QV** (16 B segments). Not on M4.

- `QV`: reduce **equivalent positions** across the four 16 B segments
  into one 16 B result (S lanes 0,4,8,12 → dest lane 0, and so on). Four
  ASIMD vectors worth.
- `Q`: ASIMD-style shuffle **inside each 16 B** (`DUPQ`/`EXTQ`/`TBLQ`/
  `TBXQ`/`UZPQ`/`ZIPQ`) — four ASIMD shuffles in parallel.
- Prefer **full-vector** horizontal ops when they work; Q/QV help **port
  ASIMD** kernels onto SSVE.

Compares write **predicates**, not ASIMD mask vectors. Three flavors:
vector, **wide-element** (each lane vs overlapping 64b), immediate (FP
imm only `#0.0`). `UO` = FP unordered/NaN. `LE`/`LO`/`LS`/`LT` often
aliases of swapped `GE`/`GT`/`HS`/`HI`.

---

## Loop control / pointer math

SVL-agnostic software uses predicate gen, pointer adjust, and induction
by element (or active-element) count instead of scalar `i++`.

- `INC<size>` / `DEC<size>` (scalar): induction by element count or
  bytes; P-form counts TRUE elements. Pattern constraints `ALL`/`MUL3`/…
  like `PTRUE`.
- `CNT<size>`: the increment value.
- `ADDSVL` / `RDSVL`: pointer/VL in **bytes**. **Not `ADDVL`.**
- `ADDSPL`: pointer by predicate-register size in bytes.
- `PTRUE`/`PFALSE` (bitmask vs counted all-TRUE differ). Patterns:
  `POW2`, `VL<n>`, `MUL3/4`, `ALL`. `PTRUES` sets flags.
- `WHILE<cc>` from GPR induction. **Match the C comparison** (`WHILELT`
  for `<`, `WHILELE` for `<=`). `RW`/`WR` variants check two address
  ranges for same-iteration memory dependence. Bitmask and counted forms
  (`VLx2`/`VLx4` must match the consumer).
- `BRK` = vectorized break (TRUE before first match). `PFIRST`/`PNEXT`
  walk TRUE bits. `PTEST` → NZCV for branches.
- `INDEX` builds `{0,1,2,…}`.
- `CTERM EQ/NE`: serial walk.
- Branch mnemonics `NONE`/`ANY`/`NLAST`/`LAST`/`FIRST`/`NFRST`/`PMORE`/
  `PLAST`/`TCONT`/`TSTOP` alias integer NZCV names (`EQ`/`NE`/…). Integer
  names also work.

Almost all LD/ST are predicated. Edge recipe: `WHILELT` + predicated
`LD1*` `/Z` + unpredicated compute over zeros + predicated `ST1*`.
Avoids clobbering past the row **and** spurious page faults. Counted ↔
multi-vector; bitmask ↔ single-vector.

Broadcast a scalar through Z: `PTRUE … ALL` then `LD1RW` `/Z` (one
element, all lanes). `LD1R` widens as needed; `LD1RQ` replicates 16 B
through every quadword.

Quad-vector edge: `CNTW … MUL #4`, `WHILELT PNrow.S, j, P, VLx4`,
**strided** four-vector LD/ST (`{z0.s, z8.s, z16.s, z24.s}` or starts
`Z0–Z3`/`Z16–Z19`). Consecutive-aligned four-vector uses range
`{z0.s-z3.s}` (start ×4) — different insn, do not mix. Compiler puts
counted predicates in `PN8–PN15`.

---

## MOVAZ — `FEAT_SME2p1`, M5 / A19

`MOVAZ` = `MOVA` **plus zero the source portion of ZA** after the move.
Same addressing as `MOVA`:

- VGx ZA-vector: **W8–W11**
- tile-slice: **W12–W15**

Query `hw.optional.arm.FEAT_SME2p1` before emitting. Rec 53: extract-and-
zero for row reuse; not a ZA-to-ZA copy. Rec 58: `MOVAZ`+ZIP/UZP fusion
exists on M5 (dest must overwrite source) — `sme-engine.md`.

Also M5 `FEAT_SME2p1`: `ZERO` on ZA **vector** forms (`ZA.T[W8–W11, …]`),
`PMOV`, Q/QV, strided LUT dest.

---

## Dataflow (typical kernel)

mem → load Z (staging) → optional SSVE type/layout/decompress → **ZA MAC
(bulk of time)** → extract Z → optional SSVE post (downcast/ZIP; fuse per
rec 58) → store. Acc feedback stays in ZA. Preload a tile via `MOVA` from
Z or `LD1*` into a tile slice (W12); default still `ZERO { ZA }` then MOP.

`ZERO { ZA }` once per output tile block. Pack MOPs back-to-back (rec
57). Cover grid latency with the whole ZA (`sme-engine.md` rec 53).
Two-vector LD/ST cut issue count more than bandwidth.

These three shapes **change codegen**. Do not emit one as if it were
another.

| Shape | ZA | Inner op | Memory / layout | Predicates |
|-------|----|----------|-----------------|------------|
| **Same-size GEMM** (FP32, FP64, M5 H-to-H / BF-to-BF) | **tiles** `ZA0–3.S` (or D) | 2×2 `FMOPA` | AT **down columns**, B **down rows**; k-loop two 64 B vectors | counted `WHILE … VLx2` on LD/ST, `PEXT` to two bitmask P for the MOP |
| **Lengthening GEMM** (i8→i32, H→S, i16→i64, …) | **tiles** | `SMOPA`/`FMOPA` after **ZIP** | load **four** rows of AT and of B, ZIP so a column’s k-elements become adjacent (feeds 2-way/4-way diagonal dots); inner k steps by 4 | same Prow/Pcol; leftover zipped halves unused |
| **Matvec** | **ZA vectors, not tiles** | VGx4 `FMLA ZA.S[w8, 0, vgx4]` **indexed** | entire D participates in every E element; `DUP.Q[0]` + `EXT #16` to rotate D (no unroll) | `WHILELT PNcol VLx4`; bitmask Prow for D. Core k needs P **multiple of 4** |

**Same-size extract:** tile-slice `ZAnH` (W12) for C, `ZAnV` for CT.
Optionally write both. Four S-tiles cover a 2×SVL × 2×SVL block.

**Lengthening leftovers:** four S-tiles cannot consume all four zipped
vectors. Unused halves are **by-products of the 64 B min load**. Do
**not** predicate those loads off. Do **not** save/restore ZA to chew
leftovers. Finish the current outputs down the column. After extract,
**ZIP** — lengthening MLA/DOT/MOP write ZA rows interleaved.

**Matvec extras:** spread FMLA dests across ZA vectors (partial accs, or
taller E / wider AT). Teaching kernel uses one `ZA.S[0, VGx4]` vs GEMM’s
four S-tiles. Small matvec: **ASIMD may be faster**.

**Transpose.** Load rows into ZA via **`ZA0H`**, extract columns via
**`ZA0V`**, store as rows. Prefer producing the matrix already extracted
transposed. Full load-insert-extract-store ≈ a real compute.

Small matrices that still fit **L1D**: core ASIMD may win (engine LSU
does not talk to L1D).

---

## Original skeleton: outer-product matmul (asm outline + C wrap)

Do **not** invent ACLE names. Include `<arm_sme.h>` and use the compiler’s
ACLE (`__arm_streaming`, ZA new/commit attributes — **as spelled in this
toolchain’s `arm_sme.h` and Clang ACLE docs**). If the ACLE spelling is
uncertain, emit a C wrapper that calls an `asm` outline like the following
and tell the user to replace the `asm` with ACLE once the names are
confirmed.

```c
#include <stddef.h>
#include <stdint.h>

// Caller:
//  - hw.optional.arm.FEAT_SME is 1 (sysctl.md)
//  - a, b, c live in *separate 4 KiB-aligned* buffers from any core-touched
//    scratch / stack (sme-engine.md rec 56)
//  - M, N, K are multiples of the tile the outline uses, or the caller
//    handled the scalar/ASIMD fringe
//
// This outline is a structure, not a complete GEMM.

void sme_f32_outer_product_tile(const float *a, const float *b, float *c,
                                size_t lda, size_t ldb, size_t ldc,
                                size_t k)
{
    // Use compiler ACLE from <arm_sme.h> in production.
    // Outline of the instruction stream the ACLE should produce:
    //
    //   smstart                      // SM=1, ZA=1
    //   rdsvl   x9, #1               // bytes per vector; do not #64
    //   zero    {za}                 // whole-ZA rename (sme-engine rec 53)
    //
    //   // SSVE supporting role: load a panel into z0.., b panel into z4..
    //   // p0/p1 govern valid lanes (WHILELT on remaining K / N)
    //
    //   // tile MOPs — dest is ZAn.S, not ZA.S[Wv] (that form is vector FMLA)
    //   // pack these back-to-back (rec 57):
    //   fmopa   za0.s, p0/m, p1/m, z0.s, z4.s
    //   fmopa   za1.s, p0/m, p1/m, z1.s, z5.s
    //   fmopa   za2.s, p0/m, p1/m, z2.s, z6.s
    //   fmopa   za3.s, p0/m, p1/m, z3.s, z7.s
    //   // enough independent tiles to cover ~4-cycle grid latency
    //   // with the whole ZA (rec 53)
    //
    //   // extract: ZA-vector VGx MOVA uses W8–W11 (do not change to W12)
    //   // fuse extract with downcast/ZIP if the store needs it (rec 58)
    //   mova    {z16.s-z19.s}, za.s[w8, 0, vgx4]
    //   st1w    {z16.s-z19.s}, pn8, [x_c]
    //
    //   smstop
    (void)a; (void)b; (void)c;
    (void)lda; (void)ldb; (void)ldc; (void)k;
}
```

Write-time checklist for this skeleton:

1. Rec 2: `cblas_sgemm` first.
2. Rec 17: every inner iteration should be a MOP into ZA, not a SSVE FMLA
   that never lands in ZA.
3. Rec 18: `RDSVL`/`CNTB`/`ADDSVL`/`INC*`, not `#64`, not `ADDVL`.
4. Recs 50–58: bounce, 4 KiB isolation, SME-8, whole-ZA hiding, extract,
   aligned stores, back-to-back MOPs, fusion pairs — `sme-engine.md`.

---

## Dispatch

```c
#include <sys/sysctl.h>

static int feat_sme(void)
{
    uint32_t v = 0;
    size_t n = sizeof(v);
    if (sysctlbyname("hw.optional.arm.FEAT_SME", &v, &n, NULL, 0) != 0)
        return 0;                 // ENOENT => no SME
    return v == 1;
}
```

Full helpers: `sysctl.md`. ASIMD fallback: `asimd.md`.

---

## Traps (agents have emitted these)

| Wrong | Right |
|-------|--------|
| `fmopa za.s[w12, 0], …` | `fmopa za0.s, p0/m, p1/m, z0.s, z4.s` |
| Rec 58 `mova … za.d[w12, #0, vgx4]` | **`w8`** (VGx MOVA = compute file W8–W11) |
| Nested `SMSTART`/`SMSTOP` inside `__arm_streaming` | Extra `SMSTART` ok; **first `SMSTOP` exits the caller** |
| `ADDVL` / `add x0, x0, #64` | `ADDSVL` / `RDSVL` / `INC*` |
| `FEAT_SVE` as SME proxy | `FEAT_SME`. Apple has no non-streaming SVE |
| ASIMD in SM | Illegal (no FA64). Exit SM to use `V` |
| Assume `S0` survives `SMSTART` | Scalar FP **not** preserved |
| Bitmask all-TRUE into PN consumer | Encodings differ |
| Loads `/M` | Loads always `/Z` |
| Port ASIMD `ADDP` packing | SSVE pairwise **interleaves** |
| SSVE-only GEMM | Rec 17: ZA MOP or core ASIMD |
| ZA complex `FCMLA` | Does not exist; use SSVE/ASIMD |
| `MOVAZ` on M4 | M5 `FEAT_SME2p1` only |
| Hardcode 16 floats / 64 B | Rec 18: `CNTW`/`RDSVL` |
| Non-streaming SVE while SM=0 | Illegal on Apple |
| Tile-to-tile `MOV` | No such insn; restage via Z or `ZERO { ZA }` |
| Counted `AND` of predicates | Logic ops are bitmask-only |
| Flicker SM around 3 insns | One streaming region for the whole kernel |
| `WHILELE` for a `<` loop | `WHILELT` — match the C comparison |
| Indexed VGx4 `Zm` in `Z16–Z31` | `Zm` in `Z0–Z15`; `Zn1` multiple of 4; dest still W8–W11 |
| Matvec as `fmopa za0.s` | Matvec = `FMLA ZA.S[w8, 0, vgx4]` indexed, not a tile MOP |
| GEMM extract CT via `ZAnH` | C = horizontal `ZAnH`; CT = vertical `ZAnV` (W12) |
| Q/QV or `MOVAZ` on M4 | M5 `FEAT_SME2p1` |
| Strided `{z0,z1,z2,z3}` for a consecutive-aligned insn | Range `{z0.s-z3.s}` start ×4, **or** strided start `Z0–Z3`/`Z16–Z19` stride 4 — match the insn |
| `ADDVL` inside SM “because a snippet did” | `ADDSVL` / `INC*` always |

---

## Write-time checklist

1. Rec **2**: `cblas_sgemm` / BNNS first (`integer-isa.md`).
2. Rec **66**: `sysctlbyname("hw.optional.arm.FEAT_SME")`. Absent → ASIMD
   path. M1 baseline binary keeps ASIMD.
3. Rec **17**: every inner iteration is a **MOP into ZA**, not SSVE FMLA
   that never lands in ZA. Else stay on core ASIMD.
4. Rec **18**: `RDSVL`/`CNTB`/`ADDSVL`/`INC*`. Never `#64`. Never `ADDVL`.
5. `SMSTART` once; extra `SMSTART` safe; **first `SMSTOP` exits**. Inline
   streaming callees. Dual n/s versions if both worlds need the helper.
   Scalar FP **not** live across the switch; GPRs are. `SMSTOP SM` to
   keep ZA for a core FP epilogue. No `FEAT_SVE`. No `FEAT_SME_FA64`.
6. Predicates: `P0–P7` bitmask single-vector; `PN8–PN15` counted
   multi-vector. Loads **`/Z`**. Stores skip FALSE. Compute often
   unpredicated over zeros. `WHILELT` for `i < n`, not `0xFFFF`. Two
   preds on 2D MOP (AND). `PEXT` counted → bitmask for GEMM. Predicate
   manip/loop control on **core**; compares/governing preds on
   **engine**.
7. Addressing: tile MOP dest = `ZAn.T`. Vector compute + **VGx MOVA =
   W8–W11**. `LDR`/`STR`/tile-slice = **W12–W15**. Rec **58**
   `mova … za.d[w8, #0, vgx4]` is correct — do not change to W12.
   C = `ZAnH`, CT = `ZAnV`.
8. `MOVPRFX` immediately before destructive SSVE ops (after trying the
   dual unpredicated form). Pairwise **interleaves**. Complex stays on
   SSVE/ASIMD, not ZA. Q/QV and `MOVAZ` are M5.
9. Types: pre-M5 lengthening-only SME MAC (L 2-way / LL 4-way). M5:
   query `FEAT_SME2p1` / `F16F16` / `B16B16` before `MOVAZ` / H-to-H /
   BF-to-BF. Same-size GEMM = tiles; lengthening GEMM = ZIP then tiles;
   matvec = ZA-vector indexed FMLA. No ZA complex MOP. No ZA-tile/vector
   copy insn.
10. `ZERO { ZA }` once per block. ZIP after lengthening extract (rec 58).
    ACLE from `<arm_sme.h>` — do not invent names. Never `ADDVL`.
11. Recs **50–58** (engine): no dependent scalar ping-pong, 4 KiB
    isolation, optimize as if SME-8, whole-ZA latency hiding, extract
    unit, aligned engine stores, back-to-back MOPs, fusion pairs.
    Isolate engine buffers on separate aligned 4 KiB regions from
    core/stack — all in `sme-engine.md`.
