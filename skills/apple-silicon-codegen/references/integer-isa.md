<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Integer ISA and platform

Owns recs **2–9**. Compiler targeting and ABI live here even though they
are not boxed recs. Fused integer `MADD` is the integer-unit counterpart of
rec 15 (home: `asimd.md`); this file only shows the GPR form and links.

ISA ladder, family aliases, `apple-latest`: `chip-families.md`. Class
latencies: `latency-lookup.md`. Rec 65 full text: `amp.md` (ISA-level LSE
stays in rec 7 below).

AArch64 only. Do not emit AArch32 or `-arch armv7`.

---

## Rec 2 — Platform technologies first (High/High)

Before writing NEON, SME, or a custom inner loop, call a platform-tuned
library. Accelerate already uses ASIMD and, from M4, SME. On earlier chips
it uses **private matrix hardware you cannot emit**. Call the library; do
not invent a matrix ISA.

| Need | Call this, not a custom kernel |
|------|--------------------------------|
| Dot product, FFT, convolution | `vDSP_*` (`vDSP_dotpr`, `vDSP_mmul`, …) |
| GEMM / LAPACK | `cblas_sgemm` / `cblas_dgemm`, LAPACK via Accelerate |
| Sparse linear algebra | Accelerate Sparse Solvers |
| Small vectors / 4×4 | `<simd/simd.h>` |
| Image | `vImage` |
| Transcendentals on arrays | `vForce` |
| Neural net primitives | BNNS |
| Compress | Compression.framework (LZFSE/LZ4/LZMA/ZLIB), Apple Archive (threaded) |
| Copy / fill | `memcpy` / `memset` (tuned form: rec 41 in `core-microarch.md`) |
| Tasks | GCD (rec 60 in `amp.md`); Background Task for deferred work |
| GPU 3D / GPU data-parallel | **Metal** — a GPU path, not a substitute for CPU ASIMD/SME |
| Image / media pipelines | Core Image, Core Media, Image I/O, Create ML, ML Compute |

Original sketch — try the library before the intrinsic:

```c
#include <Accelerate/Accelerate.h>

// Prefer this:
float vdsp_dot(const float *a, const float *b, unsigned n)
{
    float r = 0.f;
    vDSP_dotpr(a, 1, b, 1, &r, n);
    return r;
}

// Not this, unless Instruments showed vDSP is the bottleneck
// and the shape is one the library cannot cover:
//   vfmaq_f32 kernel in asimd.md
```

Same rule for `cblas_sgemm`: one call, not a handwritten `FMOPA` loop, unless
the tile sizes / fused epilogue are outside BLAS.

If the user asked for “a fast GEMM/dot/FFT/codec on Apple Silicon” and has not
ruled out Accelerate, **emit the library call**. Custom ISA comes after rec 2
fails the goal.

Time the library against a hand kernel on the input you ship. Libc
`memmem` can lose on pathological repeats; keep the measurement.
GEMM / image: `cblas_sgemm` / `vImage` first.

---

## Compiler targeting

- Pass `-mcpu=apple-m1`, `apple-m2`, `apple-m3`, `apple-m4`, or `apple-m5`.
  That is the Clang name for the **chip family**, and it enables the right
  ISA subset for that family.
- **Never** target by Arm ISA version (`-march=armv8.5-a`, `armv9.2-a`, …).
  The “base Arm ISA” label overstates Apple’s feature set (M1 is not “v8.5
  including BTI”; M5 is not “v9.3 including MOPS”).
- `clang --print-supported-cpus` lists the names this toolchain actually
  accepts. Use those strings; do not invent `apple-m1-ultra`. Family vs
  SKU aliases: `chip-families.md`.
- `+feature` modifiers exist (`-mcpu=apple-m1+bf16`) but the portable pattern
  is: compile a baseline with `-mcpu=apple-m1`, then specialize hot functions
  with `__attribute__((target("...")))` plus a `sysctl` guard. This replaces
  `target_clones` / `__builtin_cpu_supports`.
- **`apple-latest` is dangerous** (moving CPU, may exceed the deployment
  chip, disagrees across compiler versions / TUs). Do not ship it.
  Details: `chip-families.md`.
- Specifying a newer `-mcpu` for the **whole binary** silently drops older
  chips. Keep the binary’s baseline at the oldest supported family; dispatch
  the hot kernel.
- Hot files: `-O3`. `-Ofast` / `-ffast-math` change numerics. Process
  order: `SKILL.md`.

```c
#if defined(__ARM_NEON)
__attribute__((target("i8mm")))
static void gemm_i8mm_hot(/* ... */) { /* I8MM path; caller checked FEAT_I8MM */ }

__attribute__((target("bf16")))
static void gemm_bf16_hot(/* ... */) { /* BF16 path; caller checked FEAT_BF16 */ }
#endif
```

Query `hw.optional.arm.FEAT_*` at runtime before entering a specialized path.
Helpers: `sysctl.md`. **Dispatch on FEAT, not a compiled-in chip name.**

---

## Feature baseline

Query `FEAT_*` before emitting a specialized insn. The family ladder
(always-on from M1, then M2/M3/M4/M5 additions) lives in
`chip-families.md`. S/P/E share ISA — one FEAT answer is enough; cache
sizes are per perflevel.

An Apple-Silicon-only binary whose deployment target is M1+ may assume
ASIMD, CRC32, FP (incl. FP16 convert), LSE, LSE2, DotProd, AES, SHA,
PMULL. Checking those keys is harmless. Still query everything later
(I8MM, BF16, SME, MTE, CSSC, …). Always-on extras that pick an insn
(FlagM, LRCPC/LDAPUR, DPB, FRINTTS, JSCVT, RDM): `chip-families.md`.
M5 CSSC GPR ops (`ABS`/`CNT`/`CTZ`/`SMAX`/`SMIN`/`UMAX`/`UMIN`): query
`FEAT_CSSC`; do not emit them in an M1–M4 object.

**Exclusions that trap codegen:**

- **`FEAT_MOPS` is not present** (not on M1–M4; excluded from M5’s Armv9.3
  baseline). Never emit `CPY*` / `SET*` as memcpy. Call `memcpy` (rec 41,
  `core-microarch.md`).
- **`FEAT_SSBS` is M1–M3 only.** Arm deprecated `PSTATE.SSBS=0` as a
  performance knob. Do not clear SSBS “for speed” on any family; the bit
  is gone on M4/M5.
- **`FEAT_BTI` is not in the M1 base ISA.** Do not emit BTI landing pads
  or assume BTI on M1; require M2+ or query.
- **`WSP` (32-bit SP) is unused.** Do not emit it.
- **`FEAT_CMOW` is not on M5.** Do not emit cache-maintenance-from-EL0
  that assumes CMOW.
- Async MTE fault reporting is **not** supported (rec 8).

Names and `sysctl` keys: `sysctl.md`. No non-streaming SVE
(`FEAT_SVE`/`SVE2` unset) and no `FEAT_SME_FA64`: `chip-families.md` /
`sme-ssve.md`.

---

## ISA traits

- Instructions are always **32-bit**, **4 B aligned**. Immediate fields
  are small — wide constants and far offsets need sequences (rec 6). Do
  not emit an unaligned I-stream.
- RISC **load-store**: compute stays in GPRs / V-regs. Exceptions: `CAS`
  and `LD<op>` / `ST<op>` atomics. Do not invent x86 memory-ALU forms.
- IEEE-754 **binary16 / binary32 / binary64** only. No 80-bit x87. No
  256-bit ASIMD (`asimd.md`).
- **Little-endian** for instructions and data. Never set BE; do not emit
  endian-swaps “just in case” on native data.
- **PC** is not software-writable except via branch / exception entry /
  exception return. Do not `MOV` into PC; return with `RET` from X30.
  Calls write `PC+4` to X30 (`BL`/`BLR`). Leaf functions may skip
  spilling X30; non-leaf must spill. **All indirect branches are
  unconditional** — no conditional `BR`.
- Writing `Wn` **zeros the high 32** of `Xn`. Encoding 31 is SP or ZR
  depending on the insn. GPRs / X30 / X18 / WSP: ABI below.
- 32 V-regs `V0–V31` for scalar FP and SIMD — `asimd.md`. Writes clear
  unused high bits (no `vzeroupper`). Scalar views `Bn`/`Hn`/`Sn`/`Dn`/
  `Qn`. Apple assembler may hang the layout on the mnemonic (`ADD.4S
  Vd, …`); on a mismatch the **destination** layout wins. Multi-reg
  LD/ST/table lists may **wrap** (`{V31.S, V0.S, V1.S, V2.S}` is legal).
- `PSTATE` holds NZCV (below). SME chips add SM/ZA (`sme-ssve.md`;
  first `SMSTOP` exits, no nest). MTE chips add TCO (rec 8).
- NT accesses and software prefetch exist; do not sprinkle `PRFM` from
  this file — recs 40/47 in `core-microarch.md`.
- SSVE/SME is a **world switch** (extra Z/P/ZA; SM disables 128-bit
  ASIMD/FP), not “wider NEON”: `sme-ssve.md`.

Typical integer/FP ops are **nondestructive**: `ADD Xd, Xn, Xm` does not
clobber sources. Four-operand `MADD Xd, Xn, Xm, Xa` / `FMADD Dd, Dn, Dm,
Da` have a separate dest. When porting `dst += …` templates, prefer a
fresh dest.

---

## Destructive destinations

Dest is also a source. Do **not** assume a free dest; only reuse dest when
the insn is an accumulate / insert.

- Bitfield: `BFM`, `BIT`, `BIF`, `BSL`, `SLI`, `SRI`
- Integer/FP accumulates: `SABA`/`SABAL`/`SADALP`/`SRSRA`/`SSRA`,
  `UABA`/`UABAL`/`UADALP`/`URSRA`/`USRA`, `FMLA`/`FMLS`,
  `FMLAL(2)`/`FMLSL(2)`, `BFMLALB`/`BFMLALT`, `MLA`/`MLS`,
  `SMLAL(2)`/`SMLSL(2)`/`SQDMLAL(2)`/`SQDMLSL(2)`, `UMLAL(2)`/`UMLSL(2)`,
  `SQRDMLAH`/`SQRDMLSH`
- Dots: `UDOT`/`SDOT`/`USDOT`/`SUDOT`/`BFDOT`
- Matrix: `SMMLA`/`UMMLA`/`USMMLA`/`BFMMLA`
- Partial immediate: `BIC` (vector imm), `MOVK`, `ORR` (vector imm)
- Table insert: `TBX`
- Insert: `INS`
- Second-half narrow (`*2`): `FCVTN2`/`FCVTXN2`/`BFCVTN2`,
  `ADDHN2`/`RADDHN2`/`RSHRN2`/`RSUBHN2`/`SHRN2`/`SQRSHRN2`/`SQRSHRUN2`/
  `SQSHRN2`/`SQSHRUN2`/`SUBHN2`/`UQRSHRN2`/`UQSHRN2`,
  `SQXTN2`/`SQXTUN2`/`UQXTN2`/`XTN2`

BF16/I8MM forms are M2+. SSVE/SME is more often destructive —
`MOVPRFX` (`sme-ssve.md`).

---

## ABI

Apple AArch64 ABI the compiler already implements — emit it correctly in
assembly and when crossing C / C++ / ObjC++.

- **X18 is reserved.** Do not use it as a GPR, scratch, or asm clobber you
  expect to own. Omit it from inline-asm clobber lists unless you are writing
  a runtime that the platform documents as the X18 owner.
- **X29 must always address a valid frame record** when the function has a
  frame. Leaf / tail-call functions may omit a record; otherwise:

```asm
    stp     x29, x30, [sp, #-16]!
    mov     x29, sp
    // ...
    ldp     x29, x30, [sp], #16
    ret
```

  Do not point X29 at an arbitrary stack slot. Stack traces must work without
  debug info.

- X30 is the link register. Pair `BL`/`BLR` with `RET` (rec 25 in
  `core-microarch.md`). Leaf functions may skip spilling X30; non-leaf
  must spill.
- 31 GPRs (W/X 0–30) plus XZR/SP. Writing Wn zeros the high 32 bits of Xn.
  Encoding 31 is SP or ZR depending on the insn. **WSP** is unused — do
  not emit it.
- Apple silicon is **little-endian** for instructions and data.
- **SP** is 16-byte aligned at every public-function boundary.
- **Red zone** is 128 bytes below SP. Leaf functions may use it; still
  keep X29 valid if the function has a frame. Do not assume the red zone
  survives calls or signals.
- Stacks grow down; a process may only store in `[SP, stack_base)`.
  Allocate first, deallocate last; never store below the new SP.

### Type sizes (Apple, not generic AAPCS64)

| Type | Size | Align | Notes |
|------|------|-------|-------|
| `char` | 1 | 1 | **signed** |
| `wchar_t` | 4 | 4 | signed |
| `long` | 8 | 8 | LP64 |
| `long long` | 8 | 8 | |
| `long double` | 8 | 8 | **IEEE754 binary64, same as `double`.** Not quad. |
| pointers | 8 | 8 | |

Never emit `__float128` / 16 B `long double` for this ABI.

### Calling-convention divergences from generic AAPCS64

- Stack-passed arguments are **not** rounded up to multiples of 8 bytes.
- A 16-byte-aligned argument may start in an **odd** `xN`.
- The **caller** sign- or zero-extends arguments narrower than 32 bits.
  Callees do not re-extend. In asm callees, do not `SXTB`/`UXTH` incoming
  W args.
- Empty structs are ignored in the calling convention.
- **Variadic arguments all go on the stack.** After named args, NSRN is
  rounded up to 8 and every variadic slot is 8 bytes. `va_list` is
  `char *`, not a struct (C++ mangles it `Pc`). Never pass variadics in
  V-regs.
- C++: C1/C2/D1/D2 return `this`; `uint64_t` guard variables; `extern "C"`
  and C++ are not interchangeable. NEON vector names follow the 32-bit
  mangling scheme.

Do not pass `OS_OBJECT` types (`dispatch_queue_t`, `xpc_connection_t`)
between C++ and Objective-C++ translation units — they are different
types under ARC and PAC will catch the mismatch (`arm64e` below).

---

## DIT — data-independent timing (crypto only)

Use DIT **only** around constant-time crypto. It is not a general
performance knob. It forces worst-case latency and may disable other
µarch shortcuts. Never leave it on. Apple CryptoKit already enables it
internally.

DIT does **not** make a secret-dependent `LDR` constant-time vs cache.
Write constant-time code (no secret-dependent branches or indexes), then
enable DIT.

Preferred API (`<timingsafe.h>`, macOS 15.2+ / iOS 18.2+). Enable
returns a token; restore consumes it. Not usable from Swift `async`.

```c
#include <timingsafe.h>

void hmac_secret(void)
{
    timingsafe_token_t tok = timingsafe_enable_if_supported();
    /* constant-time body: no secret-dependent branches or indexes */
    timingsafe_restore_if_supported(tok);
}
```

Fallback when those symbols are missing: query
`hw.optional.arm.FEAT_DIT` and `hw.optional.arm.FEAT_SB` (`sysctl.md`).
If DIT is present, `msr dit, #1` around the body. Restore afterwards.
If `FEAT_SB` is present, close with `sb`; otherwise `dsb nsh` +
`isb sy`.

---

## PAC / arm64e

`arm64e` signs pointers (PAC) before they land in memory and
authenticates them on reload. The compiler emits this; **do not**
invent PAC sequences in application kernels.

- Build an `arm64e` slice (Architectures += `arm64e`) to adopt PAC.
  Test on A12+ / Apple silicon; Simulator cannot.
- Crash text `(possible pointer authentication failure)` plus a
  stripped vs raw address. High bits set by **corruption** look the
  same — confirm with ASan before blaming PAC. Flow J owns decode.
- Low-level stack walkers / crash reporters that read return
  addresses must strip PAC:

```c
#include <ptrauth.h>

static uintptr_t strip_lr(uintptr_t lr)
{
    return (uintptr_t)ptrauth_strip((void *)lr, ptrauth_key_return_address);
}
```

- Function pointers use a process-shared key; return addresses use a
  per-process key salted by SP; vtable entries are salted by the
  method signature. Crossing those contexts without `ptrauth.h` is a
  PAC failure. Do not reuse a stripped pointer as a function pointer.
- **`arm64e.x1` / PAuth_LR** (A20 Pro+ / M6+ / S11+): LR is also
  salted with PC. Unwinders that **authenticate** saved LR must read
  compact-unwind / DWARF to pick the scheme. Tools that only
  **strip** PAC bits (symbolication) stay as they are. No ABI change
  for application code.

Virtual calls through a mismatched C++ declaration, and C++ ↔ ObjC++
`OS_OBJECT` passing, are the usual latent bugs PAC exposes.

---

## Preferred addressing

Numbers are architectural ranges, not encodings. Scale amount is
`log2(transfer size)`: B→`#0`, H→`#1`, W→`#2`, X→`#3`; ASIMD Q often
`#4`. Assembly immediates are **bytes**; the assembler scales the
12-bit unsigned and 7-bit pair fields by transfer size.

### Prefer

- **Base + unsigned scaled imm** `[Xn,#imm]` — 12-bit, 0..4095
  **elements**, scale = transfer size (max 4095×size bytes). Sequential
  access from the original pointer **upward**. Example: `LDR X1, [X0, #8]`.
- **Base + scaled 64-bit index** `[Xn,Xm,LSL #log2(size)]`. Rec 3.
  `LDR X1, [X0, X2, LSL #3]` for `uint64_t a[i]`; `#2` for 32-bit.
- **Base + extended 32-bit index** `[Xn,Wm,UXTW|SXTW #log2(size)]`.
  Extend **then** shift. `LDR X1, [X0, W2, SXTW #3]` for a sign-
  extended 32-bit subscript.
- **LDP/STP** — 7-bit signed, −64..63 **elements**, scale = transfer
  size. Prefer for GPR pairs and frame records (density).
- **LDUR/STUR** — 9-bit signed, −256..255 **bytes**. Use **only** when
  the offset is not a multiple of the element size. Do not `LDUR` a
  naturally aligned 8 B load.
- **Pre-/post-index writeback** — singles: 9-bit signed **−256..255
  bytes** (same field as `LDUR`, **not** the 12-bit unsigned form).
  Pairs: 7-bit signed **−64..63 elements** (same field as `LDP`/`STP`).
  **One** pre-index to allocate or **one** post-index to free, then
  `base+#imm` for the rest (rec 5).
- **Base-only** `[Xn]` for exclusives, atomics, `LDAR`/`STLR` (and
  `LDSET`, `CAS`, `LDXR`). Do not add an offset to those; compute the
  address first if needed. `LDAPUR`/`STLUR` (LRCPC2, always on M1+) take
  a 9-bit unscaled offset; still RCpc, not seq_cst (rec 7).

### Slow — avoid as the default

- **Chain of pre/post-index** (`LDR Xn, [X0], #8` repeated). Each insn
  updates the base; later loads wait; extra addr-update µop. Rec 5.
- **PC-relative literal `LDR`** — ±1 MiB (19-bit signed **word**
  scale). Constant islands sit in D-cache even though they live in
  TEXT, are **not I-prefetched**, scatter so D-prefetchers miss, and
  steal a load pipe. Rec 6: `MOVZ`/`MOVN`/`MOVK` as the default.
- **Walking backward** on unsigned 12-bit `[Xn,#imm]`. That form cannot
  encode negative. Consume from the original pointer first, then bump;
  or `LDUR` signed 9-bit; or `SUB` the base.
- **Chained `STR Xt,[SP,#-8]!`**. Same writeback chain + ABI interval.
  One pre-index / `SUB` to allocate, then positive-offset `STP`/`STR`.
- ASIMD LD/ST with `LSL`/`extend` amount **≥ 4** (16 B scale): extra
  AGU µop. Still use when you need scaled V-reg addressing (rec 26);
  do not add a separate shift to “avoid” it without measuring.
- ASIMD post-index register `[Xn], Xm` and structure-size post-index
  (`LD1 {Q1.16B,Q2.16B}, [X0], #32`): writeback µops. One bump per
  structure stream; the structure-size imm must equal the structure
  size.

Writeback (pre/post-index) = extra addr-update µop — **one** per LD/ST
run. Paired integer loads crack then re-fuse unless Q-size: still emit
`LDP`. Class numbers: `latency-lookup.md`. Cracked / microcoded still
the right op when you need it: rec 26 in `core-microarch.md`.

### Equivalent spellings (same op; extend before shift)

Pick one spelling. Do not emit a separate `UXTW` then `[Xn, Xm]`.

- Zero-extend 32-bit: `Wm` / `Wm, UXTW` / `Wm, UXTW #0` / `Xm, UXTW` /
  `Xm, UXTW #0`
- Zero-extend × transfer size: `Wm, UXTW #2|#3` (or `Xm, UXTW #2|#3`);
  `#2` = ×4, `#3` = ×8
- Sign-extend 32-bit: `Wm, SXTW` / `#0` variants / `Xm, SXTW`
- 64-bit: `Xm` / `Xm, LSL #0` / `Xm, SXTX` / `Xm, SXTX #0`
- 64-bit × transfer size: `Xm, LSL #2|#3` or `Xm, SXTX #2|#3`

### ALU can write the same addresses

ALU forms: base-only, base+imm, base+reg, literal. Still prefer mem-op
scale over `ADD`+`LDR` when the load can scale. ALU extended-register
also has **B** and **H** base sizes and shift **0–4** (not only transfer
size): `(S|U)XT(W|H|B|X)`. Shifted-register ALU: `LSL`/`LSR`/`ASR`
amount **0–63** (rec 4).

`ADD` into SP is legal on imm/extend forms, **not** on shifted-reg. Do
not `ADD` shifted-reg into SP.

### ADRP

`ADRP` adds a 21-bit signed imm to the **architectural 4 KiB page** of
PC (clears low 12 bits). Range **±4 GiB**. Then `ADD` (or a load with
imm) supplies the page offset. Linker often hides the split behind a
symbol.

OS pages are **16 KiB** (`hw.pagesize`). `ADRP`’s page is still **4 KiB**.
Never treat them as the same number; do not assume 4 KiB VM pages.

### Long addresses (`ADR` vs `ADRP` vs literal)

- Nearby label within **±1 MiB**: `ADR Xd, label` — one insn, no load.
- Farther, within **±4 GiB**: `ADRP` + `ADD` (or a load with page-offset
  imm). Linker often hides the split behind a symbol.
- Do **not** materialize an address with PC-relative literal `LDR`
  (rec 6).

---

## Rec 3 — LSL in addressing (Low/High)

Use the scaled-register addressing mode so an **element index** becomes a
byte offset without a separate shift:

```asm
    // a[i] as uint64_t:  ldr x1, [x0, x2, lsl #3]
    ldr     x1, [x0, x2, lsl #3]

    // a[i] as uint32_t:  ldr w1, [x0, x2, lsl #2]
    ldr     w1, [x0, x2, lsl #2]

    // sign-extended 32-bit index
    ldr     x1, [x0, w2, sxtw #3]
```

Do not write `lsl x2, x2, #3` then `ldr x1, [x0, x2]` when the load can scale.

---

## Rec 4 — Fold immediate shifts into the 2nd operand (Low/Medium)

ALU ops accept a shifted/extended Rm. Emit one instruction, not shift + op:

```asm
    // want x0 = x1 + (x2 << 2)
    add     x0, x1, x2, lsl #2      // yes
    // lsl x3, x2, #2 / add x0, x1, x3   // no, unless you need x3 live
```

Same idea for `AND`/`ORR`/`EOR`/`SUB` with `LSL`/`LSR`/`ASR`. Shift
amount 0–63 on the shifted-register form. Keep a standalone shift only
when the shifted value must stay live.

---

## Rec 5 — One pre/post-index per load/store sequence (Medium/Medium)

A chain of pre/post-index ops serializes on the base register. Use **one**
adjust, and access the rest with **positive** `base+imm`. Unsigned imm
offsets are positive, so consume from the original pointer first, then bump.

```asm
    // no — each ldr waits on x0
    ldr     x1, [x0], #8
    ldr     x2, [x0], #8
    ldr     x3, [x0], #8

    // yes — independent loads, one bump
    ldr     x1, [x0]
    ldr     x2, [x0, #8]
    ldr     x3, [x0, #16]
    add     x0, x0, #24
```

Stack (grows down; only `[SP, stack_base)` is yours):

- Push: one pre-index `STP` (or `SUB sp, sp, #n`) to allocate, then
  `base+imm` stores.
- Pop: `base+imm` loads, then one post-index `LDP` (or `ADD`) to free.
- Prefer `LDP`/`STP` for pairs (density).
- Do not emit chained `STR Xt, [SP, #-8]!`.

```asm
    stp     x20, x19, [sp, #-32]!
    stp     x29, x30, [sp, #16]
    mov     x29, sp
    // body
    ldp     x29, x30, [sp, #16]
    ldp     x20, x19, [sp], #32
    ret
```

---

## Rec 6 — Synthesize constants with MOVZ/MOVK (Low/Medium)

Do **not** hide constants in literal pools (`LDR` from a nearby island) as the
default. Islands miss the I-side prefetcher, occupy D-cache, and steal a load
pipe. Build immediates with `MOVZ`/`MOVN`/`MOVK` (and ALU) even if that is a
few extra bytes.

`MOVZ`/`MOVN`/`MOVK` take a 16-bit chunk, optional `LSL` 16/32/48.

```asm
    // 0x0000DEAD0000BEEF
    movz    x0, #0xBEEF
    movk    x0, #0xDEAD, lsl #32
```

Worst case (four 16-bit chunks) matches a load’s latency; typical constants
are faster and do not pressure the load unit. Do not “optimize” a 2-insn
MOV sequence back into a pool load.

Fast-constant preferred pairs (rec 32 in `core-microarch.md`):
`MOVZ` / `MOVN` / `ORR Xd,XZR,#imm` / `ADR` / `ADRP`. Do **not** invent
a clever `ORR`/`EOR` immediate sequence that misses the fast path.

Preferred copy / zero: recs 30–31 in `core-microarch.md`. **`EOR Xd, Xd,
Xd` does not break the dependence** on Arm (rec 31) — the EOR waits on
the producer of `Xd`. Never import `xor eax, eax`.

PC-relative literal `LDR` is a ±1 MiB-class island in TEXT — the form this
rec tells you to avoid as the default. Nearby **labels** in that same
±1 MiB window: `ADR` (one insn, no load). Farther, within ±4 GiB:
`ADRP` + `ADD`. `ADRP` page is 4 KiB architectural; OS page is 16 KiB
(addressing section).

---

## Condition codes / NZCV

Flags live in `PSTATE.NZCV`. Set by `CMP`/`FCMP` or flag-setting ALU
(`SUBS`, `ADDS`, …). Branches and `CSEL` read them. Clobber `"cc"` in
inline asm if you write flags.

**Arm subtract carry is inverted vs x86.** After subtract, Arm `C` is
**no borrow**; x86 `CF` after `SUB` is borrow. Do not copy x86 `sbb` /
`jb` reasoning onto `SUBS`/`SBC`/`B.CC`.

- Unsigned **lower** after subtract: `B.CC` / `B.LO` (`C==0`)
- Unsigned **higher-or-same**: `B.CS` / `B.HS` (`C==1`)
- Wide subtract: `SBC`/`SBCS` with Arm’s inverted carry, not an x86-style
  borrow flag

Assembler **`NV` behaves as ALWAYS** (same as `AL`). Never emit `B.NV`
expecting a never-taken branch. Use an always-taken `B` or `AL`.

Integer mnemonics (do not re-derive flag equations at write time):
`EQ`/`NE`, `CS`/`HS`, `CC`/`LO`, `MI`/`PL`, `VS`/`VC`, `HI`/`LS`,
`GE`/`LT`, `GT`/`LE`, `AL`/`NV`. `HS`≡`CS`, `LO`≡`CC`.

FP (`FCMP`): **`VS` = unordered (NaN)**; **`VC` = not unordered**.
`NE`, `CS`/`HS`, `PL`, `HI`, `LT`, `LE` also include unordered. Do not
port x86 `comisd` branch logic. Do not re-derive flag equations.

`CSEL` / `CSET` / `CSETM` / `CINC` / `CINV` / `CNEG` / `CSINC` /
`CSINV` / `CSNEG`: rec 24 in `core-microarch.md`. If-convert cheap
both-sides unpredictable branches; keep a branch when the unused side is
expensive.

FlagM / FlagM2 (`CFINV`, `RMIF`, `SETF8`/`SETF16`, `AXFLAG`/`XAFLAG`)
are always on from M1. Prefer `CSEL`/`CSET` over flag-glue
(`chip-families.md`, rec 24).

M5 `FEAT_HBC` `BC.<cond>`: Apple predictors **ignore** the hint. Emit
`B.cond` (rec 24).

---

## Rec 7 — Weak memory: barriers, acquire/release, LSE (High/Low)

Apple silicon is **weakly ordered** (Normal memory). A thread sees its own
loads/stores in program order; other cores may not. This is not x86 TSO.
Never port “the store is visible because I stored it.”

Prefer GCD / `os_unfair_lock` / pthreads / C11 atomics (rec 62 in `amp.md`).
When you must roll ordering yourself:

| Need | Tool |
|------|------|
| Full fence | `DMB ISH` (data), `DSB` when the next op is a system effect |
| Publish payload then flag | `STLR` (store-release) on the flag |
| Read flag then payload | `LDAR` (load-acquire) on the flag |
| RMW | LSE (`LDADD`, `CAS`, …) — rec 65 in `amp.md` |
| Exclusive loop | `LDXR`/`STXR` only if LSE cannot express it |

Do not `DSB SY` for a flag publish. `DMB` orders; `DSB` waits for
completion. Other Apple memory types (Device / nGnRnE) are out of EL0
app scope — do not emit Device barriers for Normal heap.

`STLR`/`LDAR` are **base-only**. Compute `X0+8` into a tmp if the flag
is not at `[Xn]`.

`FEAT_LRCPC` / `FEAT_LRCPC2` are always on from M1: `LDAPR` (base-only
RCpc acquire) and `LDAPUR`/`STLUR` (9-bit unscaled offset, same RCpc).
C11 `memory_order_acquire` / `release` may emit these. Prefer `LDAPUR`
over `ADD`+`LDAR` when the flag has a small unscaled offset. Do **not**
substitute `LDAPR`/`LDAPUR` for `memory_order_seq_cst` — that still
needs `LDAR`. Conservative pair remains `LDAR`/`STLR`.

Original publication sketch (ordering only — **do not spin in production**,
see rec 61 in `amp.md`):

```c
#include <stdatomic.h>
#include <stdint.h>

typedef struct {
    uint64_t payload;
    atomic_uint_fast64_t ready;   // 0 = empty, 1 = published
} Mailbox;

void mailbox_publish(Mailbox *m, uint64_t value)
{
    m->payload = value;                                  // relaxed data
    atomic_store_explicit(&m->ready, 1, memory_order_release); // STLR
}

int mailbox_try_read(const Mailbox *m, uint64_t *out)
{
    if (atomic_load_explicit(&m->ready, memory_order_acquire) == 0) // LDAR
        return 0;
    *out = m->payload;
    return 1;
}
```

A `DMB ISH` between payload store and flag store is the heavy equivalent of
the release; prefer `STLR`/`LDAR` (or C11 `release`/`acquire`) over a full
fence.

Inline asm form of the same pair:

```asm
    str     x1, [x0]            // payload at [mailbox]
    add     x3, x0, #8
    stlr    x2, [x3]            // flag, store-release; base-only
    // reader:
    add     x3, x0, #8
    ldar    x2, [x3]
    cbz     x2, 1f
    ldr     x1, [x0]
1:
```

All M1/A14+ have `FEAT_LSE` and `FEAT_LSE2`. Single-insn RMW (`LDADD`,
`CAS`, `SWP`, `LDSET`, …) keeps the line through the op. Full rec 65
text and C11 examples: `amp.md`. Exclusive loops only if LSE cannot
express the RMW. Atomics replace barrier sequences and are cracked
except `CAS` — still emit them (rec 26). Align hot atomics; do not
invent 16 B atomic loads without checking the op.

---

## Rec 8 — Apple allocators for MTE (Low/Medium)

MTE (Memory Tagging Extension) is the ISA under Memory Integrity Enforcement.
It is present from M5 / A19+. Query `hw.optional.arm.FEAT_MTE`; do not emit
MTE insns in an M1–M4 object. Allocation tags are 4 bits per 16 B granule in
pointer bits `[59:56]`. Apple platforms use **synchronous** tag check; async
MTE is not supported.

Apple MTE baseline: `FEAT_MTE`, `MTE2`, `MTE4`, `CANONICAL_TAGS`,
`NO_ADDRESS_TAGS`, `STORE_ONLY`. Do not re-derive from a generic Arm
white paper.

**Use `malloc` / `posix_memalign` / the system zone. Do not write a custom
allocator.** Apple’s allocator chooses tagging density and canonical tags;
a homemade arena will either miss MTE coverage or thrash the L1 Tag Cache.

Let the compiler and libc own `IRG`/`GMI`/`STG`/`LDG`. If those mnemonics
show up in a **hot** profile, the bug is almost always a custom allocator.
If you see tagged pointers in a debugger, do not strip bits `[59:56]` and
reuse the pointer.

`malloc` uses `IRG` / `GMI` / `ST(Z)G` to mint a tag and fill the table.
Taking a C `&` on a tagged object needs **`LDG`** to stamp the right tag.
Ordinary `ADD`/`SUB` on tagged pointers leave tag bits alone — do not
`AND` away `[59:56]`. Use `SUBP`/`SUBPS` to compare addresses when tags
differ (`SUBP` subtracts `[55:0]`, ignores tags; `S` sets NZCV).
Use-after-free: libc retags on `free()`; do not cache a malloc pointer
across free.

Apple-defined MTE facts:

- **SP-based** and **PC-relative** accesses are **not** tag-checked.
  Apple does **not** allocation-tag the stack. Do not emit `ADDG`/`SUBG`
  for app stack frames (`ADDG`/`SUBG` adjust address **and** tag;
  intended for stack objects Apple does not tag).
- SSVE/SME/SSFP loads and stores in streaming mode **are** tag-checked;
  ZA `LDR`/`STR` **are** tag-checked. An unaligned 256 B SME access can
  need up to **17** granule checks; predicated-off 16 B granules are
  **not** checked. Large SME buffers often use **canonical** tags so the
  table cost disappears.
- A split 16 B boundary ⇒ two checks; a page-cross may see mixed
  tagged/untagged.
- Canonical tags: pointer `[59:56]` copies bit `[55]` — **0x0** in low
  VA, **0xF** in high VA. No table walk. Allocation tags exclude 0x0/0xF
  so the two worlds cannot alias. Never mint tag 0x0 or 0xF for heap
  objects.
- Tag table lives in a **separate physical region**, mapped cacheable
  Normal, but **only MTE insns** may touch it (not coherent with ordinary
  data accesses). Do not `LDR` the tag table.
- `PSTATE.TCO` disables checks for tag-management routines, not for
  app kernels (rec 49).

Recognize, do not hand-roll in app kernels:

- `GMI` — exclude-mask of tags. `IRG` — random tag from the allowed set.
- `LDG` — merge table tag into pointer.
- `STG`/`STZG`/`ST2G`/`STZ2G` — store tag (Z = also zero data, no tag
  check; 2 = two granules). Not a memcpy.
- `STGP` — tag + two data regs, no check. Not the fast path (rec 49).
- Privileged bulk: `LDGM`/`STGM`/`STZGM` — never from EL0 app kernels.
- `DC GVA` / `DC GZVA` — four granules, aligned 64 B. Leave large
  tag+zero to libc (rec 49 prefers `DC GVA` + `DC ZVA` split).

Dense layouts (so one tag cache line covers many live data lines) and “do
not write a custom allocator” as a **microarch** rule are rec 49 in
`core-microarch.md`. This rec is the ISA-level “do not roll MTE yourself”.
MTE check-density metric names: `named-events.md`.

---

## Rec 9 — Avoid frequent JIT I-cache invalidation (Low/Low)

Self-modifying / JIT code is not coherent with the I-cache until you run the
prescribed barrier + I-cache invalidation sequence. That flush evicts useful
instructions. JIT APIs are **macOS-only**.

- Do not compile a handful of bytes and publish them at a high rate.
- Batch translations; reuse already-published code.
- Protocol: `mmap(…, MAP_JIT)` → `pthread_jit_write_protect_np(0)` →
  emit the batch → `sys_icache_invalidate` on that range →
  `pthread_jit_write_protect_np(1)`. Headers: `<sys/mman.h>`,
  `<pthread.h>`, `<libkern/OSCacheControl.h>`. Full sketch:
  `performance-patterns/patterns/jit-icache.md`.
- Never emit `IC IVAU` from EL0 (privileged). Do not invent a
  `DMB`/`ISB` salad that skips `sys_icache_invalidate`.

If a profile shows I-cache invalidation in a loop, stop publishing.

---

## Fused integer MADD

Integer `MADD`/`MSUB`/`SMADDL`/`UMADDL`/`SMSUBL`/`UMSUBL` fold mul±add
into one instruction (separate destination, four operands). Rec **15**
(home: `asimd.md`) is the boxed “use op-add” rule covering integer **and**
ASIMD/FP. When writing GPR code:

```asm
    madd    x0, x1, x2, x3      // x0 = x3 + x1 * x2
    msub    x0, x1, x2, x3      // x0 = x3 - x1 * x2
    umaddl  x0, w1, w2, x3      // x0 = x3 + (u32)w1 * (u32)w2
    smsubl  x0, w1, w2, x3      // x0 = x3 - (s32)w1 * (s32)w2
```

Do not emit `MUL` + `ADD` when `MADD` applies. On S cores, and on P cores
from M4 / A18, integer `MADD` is **cracked** into a MUL-class μop plus an
ALU-class μop (rec 26). Still emit `MADD`: one architected insn, full
mul+add bandwidth. Do not split to “help” the crack. Older P/E keep it as
one μop. Class numbers: `latency-lookup.md`.

FP/ASIMD `FMLA` / `MLA` stay in `asimd.md`.

---

## Inline-asm hygiene (write-time)

```c
// Scale an index without a separate shift. X18 never appears.
static inline uint64_t load_u64_idx(const uint64_t *base, size_t i)
{
    uint64_t v;
    __asm__("ldr %0, [%1, %2, lsl #3]"
            : "=r"(v)
            : "r"(base), "r"(i)
            : /* no x18 */);
    return v;
}
```

Clobber `"cc"` if you write flags (`CMP`/`SUBS`/`ADDS`/`FCMP`). Do not
list `x18`. Keep X29 as a valid frame if the surrounding function has
one. Do not use `x29` as a temp in a framed function. Writing `Wn` zeros
the high 32 of `Xn` — use `=r` carefully with 32 vs 64. Prefer scaled
addressing in the template (rec 3).

---

## Write-time checklist

1. Rec 2 library before any custom ISA.
2. Baseline `-mcpu=apple-m1` if shipping to M1; `-O3`; no `apple-latest`;
   no `-march=armv*`. Hot clones: `__attribute__((target(...)))` plus
   `sysctl` `FEAT_*` (`chip-families.md`, `sysctl.md`).
3. Recs 3–6 on every asm/GPR loop: scale in the mem op, fold shifts, one
   writeback, `MOVZ`/`MOVK` constants, `LDP`/`STP` pairs. Fast-constant
   pairs only; no `EOR`-self zero.
4. Rec 7 / rec 65 (`amp.md`): `STLR`/`LDAR` or C11 acq/rel; `LDAPUR` for
   a 9-bit unscaled acquire offset; LSE not `LDXR`. Not `LDAPR` for
   seq_cst.
5. Rec 8: system malloc; never custom MTE; never strip `[59:56]`.
6. Rec 9: batch JIT; `MAP_JIT` / `pthread_jit_write_protect_np` /
   `sys_icache_invalidate`; never `IC IVAU` from EL0.
7. Rec 15: `MADD`/`MSUB`/`SMADDL`/`UMADDL`/`SMSUBL`/`UMSUBL`, not
   `MUL`+`ADD`.
8. ABI: no X18, valid X29, 16 B SP, `long double==double`, variadics on
   stack, caller extends <32-bit, no WSP; leaf may skip spilling X30.
9. NZCV: subtract carry inverted vs x86; `NV`=always; FP `VS`=unordered
   (`NE`/`CS`/`PL`/`HI`/`LT`/`LE` include unordered). FlagM: prefer
   `CSEL`.
10. Nearby ±1 MiB: `ADR`. Far ±4 GiB: `ADRP`+`ADD`. ADRP page 4 KiB ≠
    `hw.pagesize` 16 KiB.
11. Destructive dest: do not assume a free dest on bitfield / acc / DOT /
    MMLA / `TBX` / `INS` / `*2` narrow / `MOVK`.
12. Mix: high `INST_BRANCH` → rec 23; high `INST_INT_ALU` → rec 27;
    high `INST_LDST` → rec 5 / recs 39–42; high `INST_BARRIER` → rec 7.
    Ratios and picker names: `named-events.md`.
13. Unroll from `latency-lookup.md` for this S/P/E. Verify with
    `apple-silicon-profile`. Named anti-pattern: `performance-patterns`.
