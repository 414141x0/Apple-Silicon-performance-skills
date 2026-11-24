<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# ASIMD / FP

Owns recs **10–16**. One-home facts that live elsewhere:

- GPR↔vector recs **28–29**: `core-microarch.md` (write-time habit below)
- Unroll ≈ class latency / throughput: `latency-lookup.md`
- ISA ladder / S vs P vs E: `chip-families.md`
- Rec **2** (platform libraries): `integer-isa.md`

Fused integer `MADD` is shown in `integer-isa.md`; the boxed rec is here.

There is **no 256-bit ASIMD**. Width options:

- 128-bit `V0–V31` (`float32x4_t` and friends)
- two independent 128-bit vectors in ILP
- SSVE 512-bit `Z` in streaming mode (supporting role)
- SME `ZA` 4 KiB (throughput path for tiles)

Do not invent AVX2-style 256-bit NEON. Do not emit `vzeroupper` — it is an
x86 tax that does not exist here. A 64-bit / scalar dest already zeros the
high half.

Pick the instruction that matches the rule, then look up its class in
`latency-lookup.md`. Platform libraries still win (rec 2). This file is
for the kernel Accelerate cannot cover.

---

## Register file and types

32 × 128-bit vector registers `V0–V31`. Views: `Bn`/`Hn`/`Sn`/`Dn`/`Qn`.
SIMD layout is element-size × lanes equaling 64 or 128 bits (`V0.4S`,
`V0.8H`, `V0.16B`, `V0.2D`, …). Lane syntax `Vn.4S[k]`. A scalar view is
lane 0 of the same bits: `S20` ≡ `V20.4S[0]`; `V13.8B` is a **64-bit**
view (write zeros the high 64). One-lane 64-bit C types (`float64x1_t`,
`int64x1_t`, …) usually collapse to that scalar view.

Type lives in the **instruction**, not the register: untyped LD/ST /
shuffle / bitwise on any bits; do not reinterpret for math. Bare math
(no `F`/`BF`/`S`/`U`) is **integer wrap** — signed or unsigned two’s
complement, **not** FP. Saturating `Q` always carries `S` or `U`. LD/ST,
logic, and permute have **no** class letter.

Parallelism is 16× i8, 8× i16/f16, 4× i32/f32, 2× i64/f64. Keep I/O
narrow; compute wide only as needed (one `float16x8_t` often becomes two
`float32x4_t`). Scalar ASIMD is primarily **FP** — GPR integer pipes are
cheaper for scalar int. Do not emit scalar integer ASIMD except as a
GPR↔V bounce avoider on lane 0.

C types from `<arm_neon.h>`:

| C type | Meaning |
|--------|---------|
| `float16x4_t` / `float16x8_t` | 4/8 × f16 (`H`) |
| `float32x2_t` / `float32x4_t` | 2/4 × f32 (`S`) |
| `float64x2_t` | 2 × f64 (`D`) |
| `bfloat16x4_t` / `bfloat16x8_t` | 4/8 × bf16 (M2+) |
| `int8x16_t` / `uint32x4_t` / … | integer lanes (`B`/`H`/`S`/`D`) |
| `poly8x16_t` / `poly64x2_t` / `poly128_t` | Galois / `PMULL` / TBL `_p8` |
| `float32x4x2_t` / `x3` / `x4` | `vld2`–`vld4` / `vqtbl2`–`4` (`.val[0..n-1]`) |

`xN` on the element type is lane count (implies 64 vs 128). Multi-vector
`x2`/`x3`/`x4` is a struct of that many Q (or D) regs. **n=1** LD/ST/TBL
uses a **plain** vector, not `float32x4x1_t`.

Asm letters vs ACLE suffixes: `B` → `_s8`/`_u8`/`_p8`; `H` →
`_s16`/`_u16`/`_p16`/`_f16`/`_bf16`; `S` → `_s32`/`_u32`/`_p32`/`_f32`;
`D` → `_s64`/`_u64`/`_p64`/`_f64`. `q` on the intrinsic = 128-bit.

`<simd/simd.h>` (`simd_float4`, `simd_mul`, …) is the small-vector module;
it is not a substitute for a hot `vfmaq_f32` loop, and it is not 256-bit.

Compile the TU with `-mcpu=apple-mN` (`integer-isa.md`,
`chip-families.md`). Never `-march=armv8.*` as the targeting story; never
`apple-latest` in shipping code. Include `<arm_neon.h>` for NEON ACLE.

---

## Rec 12 — Intrinsics for the hot kernel; compiler owns ABI/loops/RA (High/High)

Write the **math** as ACLE intrinsics. Leave to Clang:

- AAPCS64 / Apple ABI (X18, X29 frame record)
- pointer bump, trip count, tails
- register allocation and spill
- ordinary `ldr`/`str` around the kernel

Compiler typically emits >50% of a SIMD kernel’s asm. Full assembly is
for instructions ACLE cannot express, or for a verified hot-path the
compiler systematically mis-schedules. Even then, wrap it in C with
`restrict` and a scalar tail.

Destructive FMLA/MLA: accumulator is the **first** intrinsic argument,
result assigned back:

```c
acc = vfmaq_f32(acc, a, b);     // yes — acc is dest/src
// vfmaq_f32(a, b, acc)         // no
```

Immediates (lane, `vext` byte, shift count) must be compile-time
constants. Unroll any loop whose body is `vextq` / `vmlal_lane` /
`vfmaq_laneq`; a runtime `v` in those args will not compile.

Do not sprinkle `vld1`/`vst1` around ordinary vectors — explicit LD/ST
defeat RA motion. Let Clang insert `LDR`/`STR`. Exception: structured
`vld2`–`vld4` / `vst2`–`vst4` (rec 10).

`-O3` on this file. `-ffast-math` / `-Ofast` let the compiler form `FMLA`
from `a += b * c` but they change rounding and Inf/NaN. Prefer explicit
`vfmaq_f32` so you get FMLA without silently flipping the TU’s FP
contract.

Try compiler auto-vectorization before a hand kernel. If `-O3` already
emits `FMLA.4S`, do not rewrite; if it emits `FMUL`+`FADD`, write
`vfmaq_*`.

ACLE name: `v<OP>[q|s|d][_high]_<input_type>`. `OP` **drops** `S`/`U`/`F`
and the asm `1`/`2`; other letters stay (`h` in `vhaddq` = `SHADD`; `q`
in `vqaddq` = saturate). No width letter = 64-bit (or a half of 128);
`q` = 128-bit; `s`/`d` = scalar 32/64 when needed to disambiguate.
`_high` ≡ asm `2`. Dest type may differ (narrow/long). Widening: the
suffix is the **narrower (second) source**. Untyped ops exist for every
C type — do not cast to pick a bitwise/LD/ST/shuffle flavor.

Long/wide **base** forms take **64-bit** C types even when they are the
low half of a 128-bit value — `vget_low_*` (or a 64-bit view) into
`vaddl` / `vaddw`; `_high` takes the full 128. Narrow base returns
64-bit; feed it as the first arg of `_high` to merge. Horizontal `V`:
one vector in, **scalar** out (`vaddvq_s32`, `vaddlvq_s32`). Nesting is
legal. Sources are listed in **asm source order**. Immediate is **last**
and must be a compile-time constant.

ACLE traps:

- `vreinterpret[q]_<dst>_<src>` is a **zero-insn** bitcast. Bitcast for
  shuffles/TBL; do not bitcast to change numeric type for math. A C
  cast with the **full** type name (`(int16x8_t)x`) sometimes works;
  the short `_s16` tag does not.
- `vcombine` / `vget_low` / `vget_high` may or may not emit insns —
  specify *what*, not *how*.
- `vcreate_<type>(scalar)` → 64-bit vector, lane 0 set, rest 0; **no
  128-bit form**; often not even an insn.
- `vset[q]_lane_*` = `INS` from a scalar; `vcopy[q]_lane[q]_*` = `INS`
  from another vector lane. The two `q`s independently mark dest/src
  width and **need not match**. `vget[q]_lane_*` = vector→scalar `MOV`.
- Do not use `vdup{b|h|s|d}_lane_*` (letter-mandatory synonyms that
  collide with true `DUP`). Use `vdupq_lane_*` / `vdupq_n_*`.
  `vmov[q]_n_*` ≡ `vdup[q]_n_*` — prefer `vdup`. `vdup[q]_lane[q]_*`
  widths need not match.
- Old `vtrn[q]_*` returning `x2` structs just emit TRN1+TRN2 — prefer
  explicit `vtrn1q`/`vtrn2q`. Same for uzp/zip.
- Paired ops: historical **`vp<op>` prefix**, not a suffix
  (`vpaddq_s32`, `vpaddlq_s32`). Do not invent `vaddpq`.
- By-scalar mul: `v<mul>[q]_n_*` (compiler usually `DUP`s into lane 0).
  By-lane: `v<mul>[q]_lane[q]_*` with extra `q` on `lane` = 128-bit
  selector. Lengthening: `_high` sits **between** the mul name and
  `_lane` (`vmull_high_laneq_s16`). Lane is an immediate → unroll.
- Shifts: immediate = `v<shift>[q]_n_*` count last; variable shift takes
  a vector and has **no** `_n`.
- Size-changing CVT: `_high` for the second half (types imply L/N — no
  `L`/`N` letters on the intrinsic). Pair `vcvt_f32_f64` then
  `vcvt_high_f32_f64` to pack two `float64x2` into one `float32x4`;
  reverse: `vcvt_f64_f32` then `vcvt_high_f64_f32` to split one
  `float32x4` into two `float64x2`.
- `v[q]mov[u]n_*` / `_high` = `XTN` / `SQXTN` / `UQXTN` / `SQXTUN`.
  Leading `q` = saturating; `u` before `n` = signed→unsigned saturate
  (`SQXTUN`, `vqmovun_*`). `vmovl_*` / `vmovl_high_*` = `SXTL`/`UXTL`
  (`SHLL #0`) — **does not shift**. Binary-point-preserving
  widen/narrow uses compute `SHLL`/`SHRN`, not `vmovl`.
- Fixed-point `vcvt[q]_n_<out>_<in>` takes a binary-point immediate;
  rounding is **always Z**. `FRINT*` is `vrnd[a|i|m|n|p|x|z][q]_*` —
  same FP type in and out, not a `vcvt`.

---

## Rec 10 — LD2/3/4 and ST2/3/4 for interleaved structures (Medium/Low)

`LD2`/`LD3`/`LD4` deinterleave while loading; `ST2`/`ST3`/`ST4`
reinterleave while storing. Use them when you touch **more than a few**
elements of an AoS stream (RGB, RGBA, complex pairs, XYZ). For two or
three scalars, a plain `ldr` plus `ZIP`/`UZP` is cheaper than a
multi-structure load.

`LD1` is a plain (possibly multi-reg) vector load — **no** deinterleave.
`LD1` of 2/3/4 just loads consecutive vectors. Do not pick `LD1`
expecting planar RGB. `LDR` and `LD1-to-1` are equivalent except
addressing: `LD1` has the multi-reg and post-index-by-structure forms.
**n=1** uses a plain vector, not an `x1` struct. The `1`/`2`/`3`/`4`/`R`
on `LD`/`ST` is the **interleave factor**, not the N/L lower/upper-half
`1`/`2` — load/store have no other numeric suffix.

Suffix `N` on `LDNP`/`STNP` is the **non-temporal** hint (rec 40,
`core-microarch.md`), not an interleave factor.

- `LD3` RGB → three planar Q regs (`uint8x16x3_t` / `.val[0..2]`);
  `vld3q_u8` / `vst3q_u8`
- `LD4` RGBA; `vld4q_*`
- `LD2` stereo or complex pairs; `vld2q_f32`

`LD2R`–`LD4R` (`vldnq_dup_*`) broadcast a structure field into all lanes
of n dest vectors (`n` = how many dest vectors get the splat). Lane
forms `vldn_lane` / `vstn_lane` ≡ scalar LD + `INS` / `MOV`+ST —
convenient, not always faster than split insns.

`LD3`/`ST3` (and friends) are mostly **microcoded** (rec 26,
`core-microarch.md`); they are still the right op for a long interleaved
stream. Structured LD2–4 cost extra μops vs `LD1` — do not assume `LD4`
always wins. If the interleave *is* the inner-loop tax, re-layout to
planar once and use `LD1`.

Color FIR: planarize with LD3/ST3 or LD4/ST4, **or** keep interleaved
and use lanes if the same op hits every channel. Planar if channels
diverge (alpha). Complex: stay interleaved for `FCMLA`, or `LD2` if you
want planar.

```c
#include <arm_neon.h>

// planarize interleaved stereo: [L0 R0 L1 R1 ...] -> two float32x4_t
void deinterleave_stereo(const float *in, float *left, float *right, size_t n)
{
    size_t i = 0;
    for (; i + 8 <= n; i += 8) {          // 8 samples per side = 16 floats
        float32x4x2_t p0 = vld2q_f32(in + 2 * i);
        float32x4x2_t p1 = vld2q_f32(in + 2 * i + 8);
        vst1q_f32(left  + i,     p0.val[0]);
        vst1q_f32(right + i,     p0.val[1]);
        vst1q_f32(left  + i + 4, p1.val[0]);
        vst1q_f32(right + i + 4, p1.val[1]);
    }
    for (; i < n; ++i) {
        left[i]  = in[2 * i];
        right[i] = in[2 * i + 1];
    }
}
```

---

## Rec 11 — Math ops that include conversions (Medium/Medium)

If the arithmetic **widens**, use the widening op (`SADDL`/`UADDL`/
`SMLAL`/`SADDL2`, …), not `SXTL`/`UXTL` followed by `ADD`/`MLA`.

```c
#include <arm_neon.h>

// Sum adjacent int8 pairs into int16 — SADDL, not SXTL + ADD.
int16x8_t add_widen_s8(int8x16_t a, int8x16_t b)
{
    int16x8_t lo = vaddl_s8(vget_low_s8(a),  vget_low_s8(b));   // SADDL
    int16x8_t hi = vaddl_s8(vget_high_s8(a), vget_high_s8(b));  // SADDL2
    return vaddq_s16(lo, hi);
}
```

Same pattern: `SMULL` not `SXTL`+`MUL`; `SABAL` not `SABD`+`UXTL`+`ADD`;
`SADDW`/`UADDW` when one src is already wide.

Canonical integer DSP pipeline: load narrow → lengthen as you compute
(`SADDL`/`SADDW`) → `RSHRN`/`RSHRN2` → store. Each lengthen doubles
element width; 8-bit → 32-bit takes two passes. Compute in the wide
type; only the last step scales back. `vmovl` is `SHLL #0` (extend,
**no** shift) — keep the binary point with compute `SHLL`/`SHRN`, not
`vmovl` then a separate shift. Packed i8/i16 live in V; GPRs have no
packed 8/16 SIMD.

`SHRN` truncates toward 0 (zero bias over many stages). Prefer
**`RSHRN`/`RSHRN2`/`vqrshrn_*`** (round-to-nearest, ties away from 0)
whenever the ISA offers it. `XTN` drops high bits; `SHRN*`
shift-right-and-narrow (low-bit rounding + high-bit saturation
variants). Pick the variant that matches lost low bits (R) and overflow
(Q). Store type usually matches input type.

Narrow `OpN` writes the low half and **zeros high**; `OpN2` writes the
high half and **keeps** old low (merge). Same dest for `OpN` then
`OpN2` so halves concatenate. ACLE: pass the base result as first arg
of `_high`:

```c
int16x8_t n = vrshrn_n_s32(lo, 1);       // OpN: low, high zeroed
n = vrshrn_high_n_s32(n, hi, 1);         // OpN2: merge high
```

Long omitted = low halves of both srcs; `2` = high. Wide omitted/`2`
select low/high of the **narrow** source only. Pair `vaddl` +
`vaddl_high` (or `vget_low`/`vget_high` into 64-bit forms).

`B`/`T` take **even/odd elements** and lengthen (interleaved order),
unlike `1`/`2` (whole-vector halves, original order). Few insns; mostly
BF16 `BFMLALB`/`BFMLALT`. Even lanes → B, odd → T. Do **not** confuse
with `SADDL2`.

Integer add needs +1 bit; mul needs 2n bits. `MUL` returns low n;
`SMULL`/`UMULL` return 2n; acc is n (`MLA`) or 2n (`SMLAL`). Pick per
multiply, not once per kernel; widen just enough to not overflow
(16×i8 vs 8×i16 vs 4×i32).

Mnemonic grammar that changes the op (not a catalog). `SQRDMLAH` =
signed + saturating + rounding + doubling + MLA + high-half — read the
letters, then pick the fused mnemonic.

- **Type `BF`/`F`/`S`/`U`** — BF16 / FP / signed / unsigned. Bare math
  is integer wrap, both signs; **not** FP. Untyped LD/ST/logic/permute
  work on any bits.
- **`Q` saturate** — clamps instead of wrap (u8 200+60 → 255, not 4).
  Always with S or U (clamp points differ). Oddball: `SUQADD` signed
  acc + unsigned addend. Pixels / DSP peaks: `UQADD`/`SQADD`/
  `vqaddq_*`. Wrap is default.
- **`R` round** — round up if the bit shifted out is 1 (DSP-friendly,
  less zero-bias). `RSHRN`/`SRSRA`/`SQRDMLAH` in long chains.
- **`D` doubling** — extra ×2 after mul before acc. **Q1.15-style
  signed fixed-point** in [−1,+1) only. Two sign bits after mul;
  doubling restores one. **Never** use `D` on ordinary integers;
  `SQRDMLAH` is for Q1.15.
- **`H` after add** — arithmetic mean, cannot overflow. `SHADD`/
  `UHADD`/`vhaddq_*` when you wanted `(a+b)/2`.
- **High-half `H`** — top of a 2×-wide result (DSP discard-low). Pair
  H + non-H can reconstruct full wide; usually **`L` lengthening is
  cleaner**. `SQDMULH`/`RADDHN`/`RADDHN2` when low bits will be thrown
  away; otherwise lengthen.
- **`N`/`L`/`W` width** — default is same-width. **N**arrow dest is
  half the source (`D→S`, `S→H`, `H→B`). **L**ong dest is 2× both
  sources (`B→H`, `H→S`, `S→D`). **W**ide dest and first source are 2×
  the second source. Mostly integer/logical; a few FP converts. Always
  combined with `1`/`2` or `P`/`V` so lanes line up. Lengthen on
  ingest, round/saturate-narrow on egress.
- **Output `F`/`S`/`U`** on converts (in ≠ out). A few `SHR`/`XT`
  signed→unsigned steal the sign bit for extra range (`U` suffix) —
  that is a numeric type change, not a bitcast.
- **`P` polynomial** — Galois mul (CRC/GCM-class), not pairwise. Pairwise
  is a **different** `P` (`ADDP`); ACLE spells pairwise `vp*`.
- **FP `N`** — negates the **product** (`FMLS` / `FNMUL`). Not
  narrowing `N`.
- **`C` complex** — interleaved (real, imag); rotation immediate.
- **`I` immediate** — `MOVI`/`MVNI` only. `ORR`/`BIC` immediates exist
  without `I`. `FMOV` immediate has no `I` and is an 8-bit mini-float.
- **NT `N`** on `LDNP`/`STNP` — non-temporal hint (rec 40), not
  narrow and not interleave.

---

## Rec 13 — Balance ILP vs 32 vector registers (~25 live) (Medium/Medium)

Inner SIMD loops are short, high-IPC (**5–8 insns/cycle** across ASIMD
+ LSU), high latency (**2–5 cycles** per ASIMD/load). Worst case
~**20 independent streams** to fill the units. OoO helps but
loop-carried chains and a finite window still need software ILP.

Unroll and interleave so the mapper sees independent FMLAs, but stop
before spills. **Keep about 25 live V-regs**; the other ~7 are for
transients and the compiler. Constants (broadcast weights, permute
indices, TBL masks, zeros) count as live and do not replicate with
unroll.

If `objdump` / Instruments shows spill `str`/`ldr` of Q registers in
the inner loop, **cut the unroll**. Spill-fill can become the critical
path. Reloading a constant is cheaper than spill+fill but still burns
LSU. Shorten live ranges (rearrange the body) before adding another
accumulator. Architectural-register unroll can be **less** than the
latency/throughput unroll — the register file wins. Trial-and-error is
expected.

Four accumulators × one vector of inputs is usually enough for FMLA
latency on the core; eight accumulators is the point to start counting
live regs. Unroll ≈ class latency / throughput: `latency-lookup.md`.

Rec 34 (limited unroll + software pipeline, `core-microarch.md`)
combines with rec 13. After raising unroll, re-check L1I / I-TLB miss
(recs 19–20).

---

## Rec 14 — Horizontal UMINV/UMAXV for any/all; not in the hot loop (Medium/Medium)

Any-lane / all-lane tests:

| Test | Sequence |
|------|----------|
| any lane zero | `UMINV` then compare with 0 |
| any lane nonzero | `UMAXV` then compare with 0 |
| all lanes zero | `UMAXV` == 0 |
| all lanes nonzero | `UMINV` != 0 |

ACLE:

```c
int any_zero_u8(uint8x16_t v)  { return vminvq_u8(v) == 0; }
int any_nonzero_u8(uint8x16_t v) { return vmaxvq_u8(v) != 0; }
```

Move the scalar with `FMOV` (not `UMOV`) when you need it in a GPR —
rec 29 in `core-microarch.md`.

**Do not horizontal-reduce inside the hot loop.** Keep lane-wise
accumulators (`vfmaq_f32`) and call `vaddvq_f32` / `vminvq_*` **once**
at the end. A horizontal op every iteration is the ASIMD analogue of a
serial scalar accumulator.

There is **no `FADDV`**. Synthesize with a `FADDP` tree: 2S → one
`FADDP.2S`; 4S → `FADDP.4S` then `FADDP.2S`; 2D → one `FADDP.2D`.
`vaddvq_f32` is this tree. Dummy second-source lanes of `FADDP` are
ignored — feed **zeros** so denorms/NaNs do not leak into the
reduction (`vdupq_n_f32(0)`), and so you do not pick up a false
register dep on whatever sat in that V.

Pairwise (`ADDP`, `SADALP`/`SADDLP`, min/max P, `FADDP`, `FMAXP`/
`FMINP` and NM) vs across-vector (`ADDV`, `SADDLV`/`UADDLV`, `SMAXV`/
`UMINV`, `FMAXV`/`FMINV` and NM). Vector `P`: dest lane n =
src[2n] op src[2n+1] from **two concatenated** sources. Scalar `P`:
dest[0] = src[0] op src[1]. Trees of `P` reduce to lane 0; `P` exists
for every type. `V` is not all types (esp. some FP) — synthesize with
`P`. Combine with `L` for pairwise/horizontal that lengthen (overflow
headroom). `vpaddq_*` trees for types with no `V`; `vaddlvq_s32` when
you need a wide horizontal sum.

Synthesize `SADALV`/`UADALV`: `SADDLV`/`UADDLV` into a temp then `ADD`
into the acc. Interpret the signed result at the **SADDLV size**
(H/S/D), not as D — do not treat `SADDLV` output as a full 64-bit
signed sum unless you sign-extend.

DOT/BFDOT also reduce across the mul group (rec 15). Do not add a
separate horizontal after `SDOT` unless you still need to fold the
four i32 lanes.

---

## Rec 15 — Op-add / FMLA / MADD (High/Medium)

Use fused op-accumulate: `FMLA`/`FMLS`, integer `MLA`/`MLS`,
`SDOT`/`UDOT`, `SABA`, `SSRA`, `SMMLA` (I8MM, M2+), `BFDOT` (BF16,
M2+), `FCMLA`, `FMLAL` (FHM, M1+), `BFMLALB`/`T`, `BFMMLA`. Benefits:
fewer instructions, shorter latency, and for FP **one less rounding**
(more accurate than `fmul`+`fadd`, not bit-identical).

ASIMD/FP op-adds stay **one μop**. Integer `MADD` is one μop on P
through M3 / A through A17 / all E; **cracked to two μops (MUL+ALU) on
S cores and on P from M4/A18** so the mul can use extra mul pipes
while still fetching as one insn (rec 26). Still emit `MADD`/`FMLA`;
do not split to “simpler” mul+add. GPR form and crack details:
`integer-isa.md`.

Clang will not form `FMLA` under IEEE rules without `-ffast-math`.
Write `vfmaq_f32` / `vfmsq_f32` yourself in the kernel.

Cover FMLA latency with **four independent accumulator chains**, then
reduce once (the kernel below). Through the **mul** inputs, FMLA
latency is about `FMUL`; through the **add** input it can be *longer*
than a standalone `FADD` — that is why a single fused accumulator
still serializes. Exact class numbers: `latency-lookup.md`.

FP `N` prefix negates the **product** (kills many `FNEG`s in a MAC
chain). Vector: `FMLA` = `acc + a×b` (`vfmaq_*`); `FMLS` = `acc − a×b`
(`vfmsq_*`). Do not emit `FNEG`+`FMLA` / `vsubq(acc, vmulq(a,b))`.
Scalar dest-negate fused is `FNMADD`/`FNMSUB` (`−acc ± a×b`); product-
only negate is `FNMUL`. Integer analogue: `MLA`/`MLS`, GPR
`MADD`/`MSUB` (separate dest).

Other fused families (emit these, do not split): `SABA`/`UABA`/
`SABAL`/`UABAL`, `SADALP`/`UADALP`, `SSRA`/`USRA`/`SRSRA`/`URSRA`,
`MLA`/`MLS`, `SQRDMLAH`/`SQRDMLSH` (RDM, M1+), `SMLAL`/`SMLSL`/
`UMLAL`/`UMLSL`/`SQDMLAL`/`SQDMLSL`, `SMMLA`/`UMMLA`/`USMMLA`,
`FMLAL`/`FMLSL`, `BFDOT`/`BFMLALB`/`T`/`BFMMLA`. If the algorithm is
abs-diff-acc, shift-acc, or 8-bit dot, use that op-add, not a DIY
chain. GPR long: `SMADDL`/`UMADDL`/`SMSUBL`/`UMSUBL`.

By-lane FMLA (`vfmaq_laneq_f32`) scans a broadcast A element across B
rows — GEMM without an explicit transpose. Lane is an immediate:
unroll.

```c
// c += a[lane] * b_row  — neither matrix needs a transpose
c = vfmaq_laneq_f32(c, b_row, a, 0);
c = vfmaq_laneq_f32(c, b_row, a, 1);
c = vfmaq_laneq_f32(c, b_row, a, 2);
c = vfmaq_laneq_f32(c, b_row, a, 3);
```

### Canonical kernel: 4-accumulator `vfmaq_f32` dot product

```c
#include <arm_neon.h>
#include <stddef.h>

float dot_f32_neon(const float *restrict a,
                   const float *restrict b,
                   size_t n)
{
    float32x4_t acc0 = vdupq_n_f32(0.f);
    float32x4_t acc1 = vdupq_n_f32(0.f);
    float32x4_t acc2 = vdupq_n_f32(0.f);
    float32x4_t acc3 = vdupq_n_f32(0.f);

    size_t i = 0;
    for (; i + 16 <= n; i += 16) {
        acc0 = vfmaq_f32(acc0, vld1q_f32(a + i),      vld1q_f32(b + i));
        acc1 = vfmaq_f32(acc1, vld1q_f32(a + i + 4),  vld1q_f32(b + i + 4));
        acc2 = vfmaq_f32(acc2, vld1q_f32(a + i + 8),  vld1q_f32(b + i + 8));
        acc3 = vfmaq_f32(acc3, vld1q_f32(a + i + 12), vld1q_f32(b + i + 12));
    }

    float32x4_t acc = vaddq_f32(vaddq_f32(acc0, acc1), vaddq_f32(acc2, acc3));
    float sum = vaddvq_f32(acc);   // horizontal once, after the loop

    for (; i < n; ++i)
        sum += a[i] * b[i];
    return sum;
}
```

Notes:

- Accumulator is the **first** intrinsic arg: `acc = vfmaq_f32(acc, a, b)`.
- `restrict` on both inputs so Clang does not emit overlap checks.
- Loads go **straight into V**, never `ldr xN` + `fmov`.
- FP associativity: document at the API if callers need bit-exact serial sums.
- If `n` is huge and this is a BLAS-shaped problem, rec 2 (`vDSP_dotpr`)
  still wins.

---

## Rec 16 — EXT/REV/TRN/UZP/ZIP over TBL/TBX (Medium/Medium)

Pick the dedicated permute; `TBL`/`TBX` last. Dedicated ops are 1
μop-class; TBL is 1 μop / cracked / **microcoded by table size** (rec
26) and burns a live V-reg for the control vector. Use TBL only when
the pattern is irregular (arbitrary gather of bytes), and then the
**smallest table that works**.

Mnemonic → transform (no lane catalogs):

| Op | Transform |
|----|-----------|
| `DUP` / `vdupq_n_*` / `vdupq_lane_*` | one src lane → **all** dest lanes (splat). Prefer `vdup` over `vmov_n`. |
| `INS` / `vset_lane` / `vcopy_lane` | one src lane → **one** dest lane, **RMW destructive**. Rare lane patch, not a full permute. |
| `EXT` / `vextq_*` | concat lo\|\|hi, rotate **n bytes**, extract one vector. Unaligned windows, sliding FIR taps, byte rotate. `amt=0` is a copy of `lo`; you **cannot** select all of `hi`. Intrinsics scale `amt` to the element type (H 0–7, S 0–3, D 0–1). FIR/window: `vextq_u8(cur, next, k)` and unroll `k`. |
| `REV16` | reverse bytes inside each 16-bit lane (endian of H). Not a full vector reverse. |
| `REV32` | reverse inside each 32-bit lane (endian of S; also byte-swap pairs of H). |
| `REV64` | reverse inside each 64-bit lane (endian of D). On `.4S` swaps the two S in each D. |
| `TRN1` / `TRN2` | interleave **even** or **odd** numbered elements of two srcs. Primary use: **transpose blocks**. Emit as a pair; assemblers do **not** default to 1. |
| `UZP1` / `UZP2` | pack even or odd elements from two srcs (deinterleave). Primary use: **pixels → planar**. |
| `ZIP1` / `ZIP2` | interleave low or high **halves** of two srcs. Primary use: **planar → packed**. Inverse of UZP for many layouts. |

On **2×64-bit** elements, `TRN`/`UZP`/`ZIP` are interchangeable
(`TRN1=UZP1=ZIP1`, `TRN2=UZP2=ZIP2`). Pick any; `vtrn1q_f64` is the
2×2 transpose idiom. ZIP of 64-bit elements also **packs halves**.

`1`/`2` on TRN/UZP/ZIP pick primary/secondary half — assemblers do
**not** default to 1. A lone `TRN1` (or UZP1/ZIP1) is still useful;
you do not always need the pair.

Move neighboring elements as a **larger element size** (two bytes as
one H, two S as one D) before reaching for TBL. DUP+INS cover patterns
structured shuffles miss (one-lane splat / one-lane insert) — try them
after EXT/REV/TRN/UZP/ZIP fail, before TBL.

### Wanted transform → insn (4S recipes; same shape at H/B)

Do not dump lane catalogs. Read this table, then coarsen the element
size if two neighbors should move together.

| Want (two srcs `a`,`b`; dest one Q) | Insn |
|-------------------------------------|------|
| even **positions** interleaved: `a0,b0,a2,b2` | `TRN1` |
| odd positions interleaved: `a1,b1,a3,b3` | `TRN2` |
| evens **packed**: `a0,a2,b0,b2` | `UZP1` |
| odds packed: `a1,a3,b1,b3` | `UZP2` |
| low **halves** interleaved: `a0,b0,a1,b1` | `ZIP1` |
| high halves interleaved: `a2,b2,a3,b3` | `ZIP2` |
| rotate concat `a\|\|b` by `k` **elements** | `EXT` / `vextq(a,b,k)` |
| rotate one vector by `k` elements | `vextq(a,a,k)` |
| swap the two 64-bit halves of one Q | `EXT #8` (bytes) / `vextq_f64(a,a,1)` |
| copy of `a` | `EXT #0` — **cannot** select all of `b` (no `k` = lane-count) |
| endian of each H (swap adjacent bytes) | `REV16` |
| endian of each S (also swap H pairs) | `REV32` |
| endian of each D; on `.4S` swap the two S in each D | `REV64` |
| one lane → all lanes | `DUP` |
| one lane → one dest lane, rest kept (RMW) | `INS` |
| irregular byte gather | `TBL` (smallest table); `TBX` to merge |

Same three-way split at 8H / 16B: TRN = even/odd **index** interleave,
UZP = even/odd **pack**, ZIP = low/high **half** interleave. EXT amount
is in **bytes** in asm (0–15); ACLE scales to the element type
(H 0–7, S 0–3, D 0–1). `vextq_f32(a,b,2)` = `EXT #8` on `.4S`.

Several irregular maps in one loop ⇒ several live control vectors.
That is why TBL loses to a dedicated op even when both “work.”

`TBL`: arbitrary byte gather from 1–4 table vectors via a control
vector; out-of-range → 0. `TBX`: same, merge into existing dest. Only
byte types (`_s8`/`_u8`/`_p8`); other widths need `vreinterpret`.
Control is `uint8x*` (or `int8x*` for `_s8`). Table of 1 = plain
vector; 2–4 = `x2`/`x3`/`x4` struct. Leading `q` in `vqtblNq_*` means
**table vectors are 128-bit**; Armv7 64-bit-table names still exist —
**do not use them**. Smallest table that works (1 before 2 before 3
before 4).

```c
#include <arm_neon.h>

// 2×2 transpose of 64-bit lanes (two float64x2_t). One-pass TRN.
void transpose2x2_f64(float64x2_t r0, float64x2_t r1,
                      float64x2_t *o0, float64x2_t *o1)
{
    *o0 = vtrn1q_f64(r0, r1);
    *o1 = vtrn2q_f64(r0, r1);
}

// Deinterleave evens/odds: UZP, not TBL.
void split_even_odd_f32(float32x4_t a, float32x4_t b,
                        float32x4_t *even, float32x4_t *odd)
{
    *even = vuzp1q_f32(a, b);
    *odd  = vuzp2q_f32(a, b);
}
```

### Transpose recipes (LSU-bound; TRN, not TBL)

Coarsen-then-refine. Do not try a single same-width TRN pair on a
4-wide vector and call it a 4×4 transpose.

**64-bit / `float64x2_t`:** 2×2 block, **one pass** `vtrn1q_*64` /
`vtrn2q_*64`. Two LD, two ST, two TRN vs four scalar LD+ST. ~25%
fewer insns, **50% fewer LD/ST**, ~2× because transposes are
LSU-bound.

**32-bit:** 4×4, **two-pass** — first 64-bit TRN on pairs `(a0,a2)`
and `(a1,a3)`, then 32-bit TRN on those. Four LD, four ST, eight TRN
vs 16+16 scalar: **50% fewer insns, 75% fewer LD/ST**. Order of
TRN1/TRN2 and pairing matters:

```c
void transpose4x4_f32(float32x4_t a0, float32x4_t a1,
                      float32x4_t a2, float32x4_t a3,
                      float32x4_t *o0, float32x4_t *o1,
                      float32x4_t *o2, float32x4_t *o3)
{
    float64x2_t t0 = vtrn1q_f64(vreinterpretq_f64_f32(a0),
                                vreinterpretq_f64_f32(a2));
    float64x2_t t1 = vtrn1q_f64(vreinterpretq_f64_f32(a1),
                                vreinterpretq_f64_f32(a3));
    float64x2_t t2 = vtrn2q_f64(vreinterpretq_f64_f32(a0),
                                vreinterpretq_f64_f32(a2));
    float64x2_t t3 = vtrn2q_f64(vreinterpretq_f64_f32(a1),
                                vreinterpretq_f64_f32(a3));
    *o0 = vtrn1q_f32(vreinterpretq_f32_f64(t0), vreinterpretq_f32_f64(t1));
    *o1 = vtrn2q_f32(vreinterpretq_f32_f64(t0), vreinterpretq_f32_f64(t1));
    *o2 = vtrn1q_f32(vreinterpretq_f32_f64(t2), vreinterpretq_f32_f64(t3));
    *o3 = vtrn2q_f32(vreinterpretq_f32_f64(t2), vreinterpretq_f32_f64(t3));
}
```

**16-bit:** 8×8, **three** passes (64 / 32 / 16). 128 scalar LD/ST →
16 vector LD/ST + 24 TRN. Same coarsen-then-refine.

**8-bit:** 16×16, **four** passes (64 / 32 / 16 / 8). 512 scalar LD/ST
→ 32 vector LD/ST + 64 TRN. Bottleneck moves from LSU to vector pipes.
TBL is not the transpose.

---

## Load into V, not X then FMOV

Write-time habit. Full recs **28–29** (MV2VEC steals a **load issue
slot**): `core-microarch.md`. Class numbers: `latency-lookup.md`. Do
not restate the move latencies here.

```asm
    // no
    ldr     w0, [x1]
    fmov    s0, w0

    // yes — even if the bits are integer
    ldr     s0, [x1]
```

Short integer sequences that will feed SIMD stay in V (lane 0; waste
1–3). A long integer chain belongs in GPRs. Vector→GPR without
convert: `FMOV` even for integers (`fmov w0, s0` not `umov w0,
v0.s[0]`). `FCVT*` is a convert plus a move. Never store-to-GPR then
load-to-V as a “move.”

---

## Compare → all-1s/0s + BSL/BIT/BIF (no k-masks)

ASIMD compares write a **lane mask of all-1s or all-0s**, not an
AVX-512 k-register. Blend with `BSL`/`BIT`/`BIF` (`vbslq_*`). Fresh
blend: `BSL` (`vbslq(mask, t, f)`). Merge into an existing dest:
`BIT` (insert where mask is 1) / `BIF` (insert where mask is 0) —
destructive, saves a `MOV`. Unsigned two-reg: `HI`/`HS` (`cmhi`/
`cmhs`); signed/FP use `GT`/`GE`. Two-reg `CM` omits LT/LE — **swap
operands** and use GE/GT. `#0` forms have the full set. Flag-setting
`FCMP` has no cond suffix. `FAC`/`FCM`/`CM`: `vca<cond>` for
abs-compare; compare-vs-zero is `vc<cond>[q]z_*` (one vector, no `#0`
arg).

```c
#include <arm_neon.h>

float32x4_t blend_if_gt(float32x4_t pred, float32x4_t a, float32x4_t b)
{
    uint32x4_t m = vcgtq_f32(pred, vdupq_n_f32(0.f));
    return vbslq_f32(m, a, b);
}
```

Do not try to materialize a scalar bitmask each iteration. Keep the
mask in a V register. SSVE predicates (`P`/`PN`) are the SME-mode
analogue — `sme-ssve.md`.

Rare FP suffixes, seldom used: `E` on `FCMP`/`FCCMP` = signal on NaN
(needs a handler); `NM` on `FMAX`/`FMIN` = numeric-over-NaN (IEEE) vs
default NaN-wins; `X` on `FMUL` = 0×∞ → ±2.0 (sign from exactly one
negative). `FRECPX`’s `X` means **exponent**, unrelated to `FMULX`.
Default NaN-wins max/min unless the algorithm needs IEEE `NM`.

---

## FP16 (`FEAT_FP16`, all M1 / A14+)

Native `float16x8_t` / `FMLA.8H` is legal on every Apple Silicon
deployment target. Still query `hw.optional.arm.FEAT_FP16` if the
binary might leave Apple Silicon (`sysctl.md`). FZ16 (flush FP16
denormal results) is available everywhere here.

FP16: 5-bit exp, 10+1 mantissa, ~**3.31** decimal digits, max 65504,
min normal ~6.10e-5. Smaller range, more precision than BF16. Pick
FP16 when range fits (~1e±4 to 65504) and you want extra mantissa vs
BF16. Intermediate compute is often wider than I/O: lengthen on ingest
(`FCVTL` / `FMLAL`) if the chain would overflow FP16 or accumulate
rounding.

`FEAT_FHM` (`FMLAL`/`FMLSL`, FP16×FP16 acc into FP32) from **M1/A14+**.
`vfmlal_low_f16` / `vfmlal_high_f16` rather than `FCVTL`+`FMLA.4S`
when the acc is FP32 and inputs are FP16.

`FMOV` immediate is an 8-bit mini-float (1 sign + 3 exp + 4 mant),
range ~**0.125–31**, **not including 0**. Zero is `MOVI #0` aliased as
`FMOV #0.0`. Splat zero with `vdupq_n_f32(0)` / `movi v0.2d, #0`, not
a literal-pool load.

`MOVI`/`MVNI`: splat an 8-bit chunk; `LSL` places it; `MSL` fills ones
below (partial 32-bit masks). `MOVI` + `MSL #24` ≡ `MVNI` + `LSL #24`.
64-bit form expands each of the 8 immediate bits to a whole byte
(`0x00` or `0xFF` per bit — byte masks). Synthesize lane masks with
`MOVI`/`MVNI`/`ORR`/`BIC` immediates, not a constant load. `ORR`/`BIC`
immediates have **no** `I` suffix.

---

## BF16 (`FEAT_BF16`, M2 / A15+)

16-bit, **FP32 exponent (8 bits)**, 7-bit mantissa + sign. Same range
as FP32 (~1.18e-38..3.39e38), ~**2.40** decimal digits (less than
FP16’s ~3.31). Query `hw.optional.arm.FEAT_BF16`. Dispatch M2+; M1
path converts via FP32 or stays FP16/FP32.

Accumulate is always **FP32**. Arithmetic that lengthens (`BFDOT`,
`BFMLALB`/`T`, `BFMMLA`) is preferred over an explicit convert plus
compute. Emit `vbfdotq_f32` / `vbfmlalbq_f32` / `vbfmmlaq_f32` in the
hot loop.

- **BF16 → FP32:** there is no convert insn. `SHLL` / `SHLL2` `#16`
  (high 16 bits of BF16 are FP32 sign/exp/top mantissa; low mantissa
  zeros). `vshll_n_u16` / `vshll_high_n_u16` `#16` then
  `vreinterpretq_f32_u32` only when you cannot use BFDOT/BFMLAL; never
  invent a `BFCVT` in that direction.
- **FP32 → BF16:** **`BFCVTN` / `BFCVTN2`** (proper FP round), not
  `SHRN`. Scalar `BFCVT Hd, Sn` exists. `vcvt_bf16_f32` /
  `vcvt_high_bf16_f32`.

Any other BF16 math: convert to FP32, compute, convert back. Prefer
the lengthening arithmetic so the convert is free.

```c
// BF16 → FP32 when BFDOT/BFMLAL cannot: SHLL #16, not a convert insn.
float32x4_t bf16_lo_as_f32(uint16x8_t bits)
{
    return vreinterpretq_f32_u32(vshll_n_u16(vget_low_u16(bits), 16));
}
```

B/T = even/odd lengthen. By-vec and by-element forms exist for
`BFMLALB`/`T` and `BFDOT`. Pair B then T to consume a full
`bfloat16x8_t` into FP32 accs. `BFDOT` folds **two** BF16 products
into each of four FP32 acc lanes (not four products — that is `SDOT`).

`BFDOT`/`BFMMLA` are cracked, long-latency (`latency-lookup.md`). Still
the right op; do not DIY 8 BF16 muls in ASIMD as a “simpler” sequence.

`FEAT_EBF16` (FPCR.EBF, numeric behavior of BFDOT/BFMMLA/BFMOPA…) is
**M5/A19**. Also applies to SSVE. Do not assume M2 BF16 numerics match
M5 EBF; query `FEAT_EBF16` if the kernel depends on the new mode.

No BF16-in/BF16-out **core** compute; SME `FEAT_SME_B16B16` (M5) is a
different world. Core kernels lengthen to FP32.

---

## Complex (`FEAT_FCMA`, M1 / A14+)

`FCADD`/`FCMLA` on interleaved (real, imag) pairs: **even lane = real,
odd = imag**. One Q holds two `float` pairs or one `double` pair.
Avoids UZP/ZIP to planar. Extra immediate = **rotation of signs**
(#0 / #90 / #180 / #270). Keep AoS complex packed; do not planarize
just to use `FMLA`.

Rotation recipes (two `FCMLA` each):

| Want | Pair |
|------|------|
| `A += B×C` | `#0` then `#90` |
| `A += conj(B)×C` | `#0` then `#270` |
| `A -= B×C` | `#180` then `#270` |
| `A -= conj(B)×C` | `#180` then `#90` |

```c
// A += B * C  — two complex pairs in each float32x4_t
acc = vcmlaq_f32(acc, b, c);            // #0
acc = vcmlaq_rot90_f32(acc, b, c);      // #90
```

Other mixes of `FCADD`/`FCMLA` (`vcaddq_rot90_f32`, …) cover the rest.
Query `hw.optional.arm.FEAT_FCMA` (legacy `hw.optional.armv8_3_compnum`).
Present on all M1/A14+. Assume FCMA in an Apple-Silicon-only binary;
still query if the binary is fat/portable.

---

## DotProd and I8MM

`SDOT`/`UDOT` from **M1/A14** (`FEAT_DotProd`). Two i8 vectors, 16×
(8×8) muls, acc into **four i32** lanes: each lane sums 4 adjacent
products + old acc (five values, unrounded):

```
C[0] += A[0..3]·B[0..3]
C[1] += A[4..7]·B[4..7]
C[2] += A[8..11]·B[8..11]
C[3] += A[12..15]·B[12..15]
```

Great for 8-bit pixel filters. `vdotq_u32` / `vdotq_s32` instead of
`vmovl`+two `vmlal`.

By-element form reuses one 4-byte group of B (`i` in 0–3) across all
four acc lanes — four patterns packed in one vector. Pack four 4-byte
templates into one Q and `SDOT` by lane to save regs.

Mixed-sign `SUDOT`/`USDOT` and MMLA with `FEAT_I8MM` from **M2/A15+**.
`SUDOT` by element; `USDOT` by element **and** by vector. Unsigned
pixels × signed weights → `USDOT`/`SUDOT`, not `UDOT`. Query
`hw.optional.arm.FEAT_I8MM`.

`SMMLA`/`UMMLA`/`USMMLA`: 2×8 i8 × 8×2 i8 → 2×2 i32 acc (four i32
lanes laid out as that grid). Equivalent to four 8-wide dots of the
high/low halves of A against the high/low halves of B. **No
by-element form** (the vectors *are* the matrices). `USMMLA`: 2×8
unsigned × 8×2 signed. Small 2×2 i8 GEMM tile on M2+; SME grid for
large tiles on M4+.

FIR per-pixel DOT: replace `EXT`+`SHLL`+2×`MLAL` with `vextq_u8` +
`vdotq_u32` — **~2× compute** if everything stays in regs; otherwise
LSU binds first. `UDOT` is **unsigned weights only**. Unsigned FIR →
UDOT; signed edge filters → `USDOT`/`SUDOT` on M2+.

DotProd is on every M1/A14+; I8MM is not. M1 path = SDOT/UDOT; M2+
path may add SUDOT/USDOT/SMMLA.

---

## Crypto

Specialized AES / SHA / SHA3 / SHA512 / `PMULL` / `BCAX` / `RAX1` /
`XAR` insns, **not** mode bits. AES, SHA1/256/3/512, PMULL from
**M1/A14+**. Polynomial `P` prefix = Galois mul (CRC/GCM-class).

**Do not hand-write AES/SHA inner loops.** Call CryptoKit /
CommonCrypto / Security.framework — they already use these insns.

DIT is for constant-time crypto only, not a speed knob
(`integer-isa.md`). `timingsafe_enable_if_supported` around
secret-dependent bodies; never as a general SIMD hint.

`PMULL` (64-bit polynomial, GCM/CRC-class) from M1. Use for
carry-less mul when not calling a platform GHASH; still prefer
platform AEAD.

---

## GEMM / GEMV / FIR (Accelerate first)

Rec 2 (`cblas_sgemm` / `vDSP_*` / vImage) still wins for BLAS-shaped
work. Shapes below are for the kernel the library cannot cover. Do not
copy teaching `LD_F32V` aliasing macros — `vld1q_f32` + `restrict`.

**FP32 GEMM (teaching):** one `float32x4_t` C tile, `vfmaq_laneq_f32`
against strided B rows, A consumed by lane so neither matrix needs an
explicit transpose. Clang ~5 LDR + 4 FMLA + pointer math.
Column-stride B is hard on cache.

**GEMV:** 1×P × P×N can reuse GEMM with M=1. M×P × P×1 cannot use the
same B pattern: `c = vfmaq_f32(c, B[k], A[i*P+k])` then a `FADDP`
tree whose dummy second src is **zeros**, then `vgetq_lane_f32(..., 0)`
— or `vaddvq_f32(c)`, which is that tree. 4× fewer MAC insns than
scalar, one scalar C[i] per outer. Parallel-output (GEMM-like) when
you can fill a vector of C; per-output (GEMV-like) at edges.

Production ASIMD GEMM extras (SME is the real throughput path for
large tiles, M4+):

- **Swap loop order** if A vs B stride disagrees with cache.
- **Block** so a B panel stays in L1D.
- **SW prefetch** for the large-stride B walk if HW prefetch does not
  lock on (distance hundreds of cycles, rec 47).
- **Column grouping** — 4 columns wastes most of a **128 B** line;
  compute **16/32** C columns so each B line is fully used.
- **Transpose-caching** a B panel in L1 in vector-transpose order,
  only if reused enough.

Do not ship the 4-column teaching kernel as production.

**Window / parallel-output FIR:** one vector of output pixels; same
tap across N staggered windows (GEMM-like). `vextq_u8` align,
`vmovl_u8` to i16 (`SXTL`/`UXTL`, **no** shift — scale on egress),
`vmlal_laneq_s16` / `_high` into **four `int32x4_t` accs**. 32-bit acc
= 4× 8-bit input width. EDGE≤3 ⇒ max 7×7 because weights live in one
`int16x8` and the window in two `uint8x16`. Image masks are small vs
1-D audio FIR but still MASKSIZE² taps. Egress `vqrshrn_n_s32` then
`vqmovun_s16` / `_high` (`SQXTUN` — signed acc, unsigned pixel store).
Must unroll (`vext`/`vmlal` immediates). This shape when MASKSIZE is
small and you can fill 16 outputs; watch rec 13 if MASKSIZE grows
(filters + `nextInputs` live).

**Per-pixel FIR:** lanes = taps of **one** pixel (GEMV-like), then
`vaddvq_s32` + narrow/store **one** pixel. Must unroll for `vextq`.
MASKSIZE=7 wastes **12.5%** (1 unused i16 lane); MASKSIZE=5 → 37.5%;
MASKSIZE=3 → 62.5%. MASKSIZE=7 ≈ RA limit, else reload weights every
pixel. Extra horizontal + scalar stores. Use when MASKSIZE ≈ vector
width, or at **image edges** where you cannot fill 16 outputs;
otherwise parallel-output. DOT variant: rec 15 / DotProd above
(`vextq_u8` + `vdotq_u32` instead of `EXT`+`SHLL`+2×`MLAL`).

Color: planarize with LD3/ST3 or LD4/ST4, or keep interleaved when the
kernel is uniform (rec 10).

---

## FPCR / rounding

FPCR controls rounding, NaN, denormals. Prefer `fenv.h` over raw
`MSR`/`MRS`. Non-default is rare; **read/write is expensive** (rec 33:
at most every few thousand insns). Set mode once at the kernel
boundary; never poll FPSR per iteration.

FIZ/AH need `FEAT_AFP` (M3+); NEP also AFP; EBF needs `FEAT_EBF16`
(M5); FZ16 all chips. Do not poke FIZ/AH on M1/M2. Write-time meaning
if you *do* set a mode at the kernel boundary:

- **FIZ** — flush denormal **inputs** (S/D/BF16) to zero when FZ is
  on (FZ alone flushes **results**).
- **FZ** — flush denormal **results** to zero.
- **FZ16** — same as FZ, **FP16** data-processing only.
- **AH** — alternate denormal/NaN handling.
- **NEP** — ASIMD **scalar** ops: set ⇒ *preserve* unused high dest
  bits; default NEP=0 **zeros** them.
- **DN** — default-NaN propagation.
- **AHP** — alternative half-precision (not IEEE FP16). Do not enable
  unless the kernel is that format.
- **RMode** — rounding for ops that do not take a rounding letter.

Rounding letters on `CVT`/`INT`: A/I/M/N/P/X/Z. `I` = current FPCR
(default for suffix-less `CVT`). `X` = exact, exception if any
rounding (handler required). Do not confuse rounding `N` with
narrowing `N` (rounding N is followed by an output-type letter).
`vcvtnq_*` / `vcvtzq_*` by name; fixed-point `vcvtq_n_*` rounding is
**always Z**. `FRINT*` family is `vrnd[a|i|m|n|p|x|z][q]_*` — same FP
type in and out.

Cancellation (near-equal subtract) destroys precision — not unique to
Arm. Do not “fix” it with extra conversions in the hot loop. Hidden 1
in the mantissa is lost for denormals; if the kernel lives in denorms,
FZ/FZ16/FIZ are a policy choice, not a speed hack to sprinkle.

---

## Cracked / microcoded (rec 26) that hit ASIMD

Cracked = 2–3 μops, seamless. Microcoded = 4+ and extra insert
overhead. Either is right when you **need that op**. Do not split a
needed LD3/TBL/BFDOT into a “simpler” longer sequence unless measured;
conversely do not reach for microcode when two ALU ops suffice. Home:
`core-microarch.md`.

- `TBL`/`TBX`: 1 μop / cracked / microcoded **by table size** →
  smallest table (rec 16).
- Structured `LD3`/`ST3` (and LD2/4 friends): mostly microcoded. Long
  AoS: yes (rec 10). Frequent inner-loop interleave: re-layout planar.
- BF16 math (`BFDOT`, `BFMMLA`): cracked, long latency
  (`latency-lookup.md`). Still emit when the algorithm is BF16-dot/
  MMLA.
- Integer `MADD` cracked on S and on P from M4/A18 — still emit
  (rec 15, `integer-isa.md`).
- GPR↔vector plus convert: cracked — prefer in-file load (rec 28).

---

## Alignment / LDST (write against these)

Home: recs 42–45, `core-microarch.md`. Align 16 B ASIMD to 16 B; NT
stores to 128 B when you can. Crossing 64 B, 128 B, or 16 KiB costs
extra μops. A load that crosses a **16 KiB** page waits until it is
the **oldest load**. `alignas(16)` tiles; do not page-split Q loads.

If unaligned, prefer unaligned **loads** over stores. Keep `ST1`/`ST3`
aligned; let `LD1` be the unaligned side.

Do not `STRB` a buffer together and immediately `LD1` 16 B of it (rec
45) — the dependence predictor may serialize the wide load behind
**all** older stores. Write the wide type you intend to read, or
finish byte stores in a different phase before the vector load.

Coherence / false-sharing line is **128 B**; L1 fill is 64 B. GEMM
column grouping against 128 B, not 64 B.

---

## Availability (query, do not hardcode)

Full ISA ladder: `chip-families.md`. Query `hw.optional.arm.FEAT_*`
(`sysctl.md`) before emitting the insn. E and P share ISA, so one
`FEAT_*` answer is enough.

| Feature | From | Core ASIMD use |
|---------|------|----------------|
| ASIMD 128-bit, FP16, FHM (`FMLAL`), FCMA, DotProd (`SDOT`/`UDOT`), RDM (`SQRDMLAH`), AES/SHA*/PMULL | M1 / A14+ | default kernel |
| BF16 (`BFDOT`/`BFMLAL*`/`BFMMLA`/`BFCVTN`), I8MM (`SMMLA`/`SUDOT`/`USDOT`) | M2 / A15+ | dispatch |
| AFP (`FIZ`/`AH`/`NEP` in FPCR) | M3 / A16+ | rare FPCR modes |
| SME / SME2 (ZA 4 KiB, SSVE 512-bit supporting) | M4 / A18+ | not a wider NEON; world switch |
| EBF16, SME F16F16 / B16B16 | M5 / A19+ | EBF numerics; non-lengthening 16-bit on **engine**, not core |

`-mcpu=apple-m1` baseline + `__attribute__((target("i8mm")))` (etc.) +
runtime FEAT check for specialized TUs. Do not raise the whole
binary’s `-mcpu` to m4 and silently drop M1. Legacy sysctl names:
`sysctl.md`.

---

## Anti-patterns

- AVX2 256-bit NEON / `vzeroupper`. → 128-bit V, two ILP streams.
- `vmulq`+`vaddq` / C `a*b+c` without contraction. → `vfmaq_*` (rec 15).
- One serial FMLA acc. → 4 chains (rec 15) within ~25 live (rec 13).
- `vaddvq_f32` / `vminvq` inside the trip. → once at the end (rec 14);
  FADDP dummy = zeros.
- `SHRN` in a long DSP chain. → `RSHRN` (rec 11).
- `SXTL`+`ADD` / `SXTL`+`MUL`. → `SADDL`/`SMULL` (rec 11).
- `TBL` for a regular rotate/interleave/transpose. → EXT/REV/TRN/UZP/ZIP
  (rec 16); smallest TBL if irregular.
- `SHRN` for FP32→BF16. → `BFCVTN`. Invented BF16→FP32 convert. →
  `SHLL #16` or BFDOT/BFMLAL.
- `ldr x` + `fmov` / `umov` for a horizontal. → `ldr s` / `FMOV`
  (recs 28–29).
- `LD3` for two pixels. → `ldr`+`ZIP`/`UZP`. DIY UZP loop for a long
  RGB stream. → `vld3q`.
- Explicit `vld1`/`vst1` fighting RA. → omit, except structured.
- `-Ofast` as the FMLA switch. → explicit `vfmaq_*`.
- Hand AES/SHA. → platform crypto.
- SSVE-only as “wider NEON” without ZA. → core 4-wide FMLA or SME
  grid; SSVE is supporting (`sme-ssve.md`).
- Horizontal any/all via a scalar loop over lanes. → `UMINV`/`UMAXV` +
  `FMOV` (rec 14).
- k-mask / scalar bitmask per iteration. → V-reg all-1s/0s + `BSL`.
- Two-reg compare with LT/LE. → swap + GE/GT.
- `D` doubling on ordinary integers. → Q1.15 only.
- B/T confused with `SADDL2`. → even/odd lengthen, mostly BF16.
- `vmovl` as a binary-point shift. → `SHLL`/`SHRN`; `vmovl` is `#0`.
- `vaddpq` / paired as a suffix. → `vpaddq_*`.
- `float32x4x1_t` for `vld1`. → plain `float32x4_t`.
- `vextq(lo, hi, lanes)` to take all of `hi`. → use `hi`; `amt=0` is `lo`.
- `FNEG`+`FMLA` / `vsubq(acc, vmulq(a,b))`. → `vfmsq_*` (`FMLS`).
- `TRN` when you wanted even/odd **pack** or half **interleave**. →
  `UZP` / `ZIP` (recipes in rec 16).
- `vqmovn` for signed-acc → unsigned pixel. → `vqmovun_*` (`SQXTUN`).
- `BIT`/`BIF` for a fresh blend. → `BSL`; BIT/BIF merge into dest.
- Two-reg unsigned compare with `GT`. → `HI`/`HS`.
- `FRECPX` `X` as `FMULX`. → exponent vs 0×∞.
- `LDNP` `N` as narrow or as LD-interleave. → NT hint (rec 40).
- Bare `ADD` on FP bits. → `FADD`; bare math is integer wrap.

---

## After the kernel

Unroll factor: class latency / throughput in `latency-lookup.md`.
Verify: `apple-silicon-profile` (CPU Bottlenecks + CPU Profiler).
Named anti-pattern still present: `performance-patterns`.
