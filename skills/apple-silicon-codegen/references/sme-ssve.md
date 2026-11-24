<!-- Structure inspired by Intel Performance Skills (MIT, 2026).
     Content is original agent instructions paraphrasing Apple Silicon CPU
     Optimization Guide 5.0. Do not copy Guide tables. MIT license. -->

# SSVE / SME ISA (Guide Ch. 4)

Owns recs **17–18**. Engine microarchitecture (bounce, 4 KiB isolation,
SME-8, fusion) is Ch. 6 — `sme-engine.md`. Do not dump Guide 4.6 mnemonic
catalogs.

Query `hw.optional.arm.FEAT_SME` before emitting any of this
(`sysctl.md`). SME debuts at **M4 / A18+**. A binary whose baseline is M1
must keep an ASIMD path.

---

## What SME is, and what SSVE is

- **SME** is the 2D `ZA` array plus the Processing Grid. Tiles and ZA
  vectors live here. This is the high-throughput path (`FMOPA`, other MOP
  outer products, tile-wide ops).
- **SSVE** is the supporting vector ISA: 32 × SVL-bit `Z` registers and 16
  `P`/`PN` predicates. `Z` is the staging file into and out of `ZA`.
- **SSFP** is scalar FP while streaming; it aliases the low bits of `Z` and
  runs on the engine’s SSVE unit (narrower than core ASIMD FP).

Apple silicon does **not** implement non-streaming SVE. `FEAT_SVE` /
`FEAT_SVE2` are unset. There is no “SVE while SM=0” path. Do not emit
non-streaming SVE, and do not check `FEAT_SVE` as a proxy for SME.

Chips with `FEAT_SME` also have `FEAT_SME2`, `FEAT_SME_F64F64`, and
`FEAT_I16I64`. Later families add more data types (`sysctl.md` “from”
table). `FEAT_SME_FA64` is **not** implemented — many ASIMD encodings stay
illegal in streaming mode.

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
win — details in `sme-engine.md` rec 51.

If Accelerate already has the GEMM (`cblas_sgemm`) or a BNNS op, rec 2
still applies (`integer-isa.md`).

---

## Rec 18 — Apple SVL is 512b / 64 B, ZA is 4 KiB; do not hardcode SVL

On current Apple silicon:

- Streaming vector length (SVL) = **512 bits = 64 bytes**
- `ZA` = SVL × SVL bits = **4 KiB** per core
- `ZT0` lookup table lives beside the Z file

**Portable code must not bake `64` in as “the vector length.”** Read it:

```asm
    rdsvl   x8, #1              // x8 = SVL in bytes (even if SM=0, needs FEAT_SME)
    cntb    x9                  // element count in bytes, all lanes
    cnth    x10
    cntw    x11
    cntd    x12
```

Use the ISA’s VL-scaled addressing and `ADDSVL`/`INC*` rather than
`add x0, x0, #64`. A future Apple SVL, or a non-Apple SME CPU, will then
keep working. Local comments may say “64 B on this chip”; immediates in
the kernel must come from `RDSVL`/`CNTB` unless the function is explicitly
Apple-SVL-only and named that way.

---

## Streaming mode and ZA mode (Guide 4.2)

`SMSTART` / `SMSTOP` are aliases of `MSR SVCR*`.

| Instruction | Effect |
|-------------|--------|
| `SMSTART` | SM=1 and ZA=1 |
| `SMSTART SM` | SM=1 only |
| `SMSTART ZA` | ZA=1 only |
| `SMSTOP` | SM=0 and ZA=0 |
| `SMSTOP SM` | SM=0 (ZA held) |
| `SMSTOP ZA` | ZA=0 |

While **SM=1**:

- `V0–V31` are gone (contents **zeroed** on entry and exit). ASIMD
  encodings are illegal.
- `Z0–Z31` and `P0–P15` are available (also zeroed on the mode switch).
- Scalar FP continues as SSFP on the low bits of `Z`.
- SME ops that touch ZA also need **ZA=1**.

While **ZA=1** and **SM=0**, only `LDR`/`STR` of ZA vectors are legal. That
mode exists so a short core excursion can keep ZA live (`sme-engine.md`).
Do not leave ZA on across unrelated work: it powers the engine, can drop
cluster frequency, and fattens context switches.

There is **no nested** SM/ZA pair: an extra `SMSTART` is safe, the **first
`SMSTOP` exits**. Inline streaming callees so the compiler does not
save/restore 4 KiB of ZA around a call.

Scalar FP registers are **not** preserved across SM enter/exit (they are
V subsets out of SM and Z subsets in SM). Move a scalar-FP epilogue with
`SMSTOP SM` (ZA held) onto the core, then re-enter — rec 51.

Do not flicker `SMSTART`/`SMSTOP` around a handful of instructions. Batch a
whole tile kernel inside one streaming region.

---

## Predicates P / PN

16 predicate registers, one bit per byte of SVL (64 bits on Apple). They
govern which lanes of a `Z` op are active.

Two encodings share the file. Open Guide §4.1.2 / Table 4.1; do not copy
it.

| Style | Registers | Use |
|-------|-----------|-----|
| Bitmask `Pn` | **P0–P7** on most ops | per-lane if/then, single vectors |
| Counted `PNn` | **PN8–PN15** on most ops | contiguous TRUE run; multi-vector / remainder |

All-FALSE is 0 for both. All-TRUE encodings **differ** and are not
interchangeable. Loads are always `/Z`. Stores skip FALSE lanes. Compute
is often unpredicated over load-zeros, with the store predicated.

Write-time rules:

- **Predicate manipulation and loop control run on the CPU core**
  (`PTRUE`, `WHILELT`, `INC*`). **Comparison-based predicate generation
  and governing predicates run on the engine.** A compare that must
  come back to the core to decide the next engine insn pays rec-50
  transit.
- Tails: `WHILELT` against the remaining count, not a hardcoded `0xFFFF`.
- Destructive SSVE ops can be wrapped with **`MOVPRFX`** immediately
  before the op to synthesize a non-destructive dest; the pair may fuse.

SSVE pairwise (`ADDP` and friends) **interleaves** results so sources
stay near dests. Do not port an ASIMD `ADDP` packing picture onto SSVE.

---

## SSVE-only vs 4-wide ASIMD

SSVE `Z` is 4× the bits of a `V` register, but the engine SSVE unit issues
**one** vector μop per cycle (and many mul/add μops take multiple issues).
Core ASIMD has multiple FP/ASIMD ports (four on M4 P). Net: a 128-bit
4-accumulator `vfmaq_f32` kernel on the core often beats a 512-bit SSVE
kernel that never uses ZA.

Use SSVE without ZA only for a short staging sequence next to a grid loop,
or when you already pay for SM=1.

Tiles that fit in L1D can be **faster as core ASIMD**: the engine LSU
talks to Shared LL, not L1D (`sme-engine.md`).

SSVE has interleaved complex (`FCMLA` / `FCADD`). **SME generally does
not** — keep complex on SSVE staging or on core ASIMD; do not invent a
ZA complex MOP.

---

## ZA addressing: W8–W11 vs W12–W15

Open Guide Tables 4.2–4.3. Do not dump them.

- **Compute** (vector FMLA/DOT/MLA into ZA, ZERO of those forms):
  index GPR is **W8–W11**.
- **Tile MOPs** (`FMOPA` / `SMOPA` / …): dest is a **tile** `ZAn.T`
  (`ZA0.S`–`ZA3.S`, `ZA0.D`–`ZA7.D`), not `ZA.T[Wv]`.
- **Movement**: ZA `LDR`/`STR` and tile-slice `MOVA`/`LD1*`/`ST1*` use
  **W12–W15**. ZA-**vector** VGx `MOVA` uses **W8–W11** (same file as
  compute). That split lets eight GPRs hold ZA positions.

There is no arithmetic between ZA tiles or between ZA vectors. Vertical
`MOVA` extracts already transposed.

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
    //   // extract: ZA-vector VGx MOVA uses W8–W11 (Guide Table 4.3)
    //   // fuse extract with downcast/ZIP if the store needs it
    //   // (rec 58, Guide 6.8 pairs)
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
3. Rec 18: `RDSVL`/`CNTB`, not `#64`.
4. Recs 50–58: no dependent scalar ping-pong, 4 KiB isolation, optimize as
   if SME-8, whole-ZA latency hiding, extract unit, aligned engine stores,
   back-to-back MOPs, fusion pairs from Guide 6.8 — all in `sme-engine.md`.

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
