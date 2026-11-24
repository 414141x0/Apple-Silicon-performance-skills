<!-- Structure inspired by Intel Performance Skills (MIT, 2026).
     Content is original agent instructions paraphrasing Apple Silicon CPU
     Optimization Guide 5.0. Do not copy Guide tables. MIT license. -->

# Integer ISA and platform (Guide Ch. 2)

Owns recs **2–9**. Compiler targeting and ABI live here even though they are
not boxed recs. Fused integer `MADD` is the integer-unit counterpart of rec 15
(home: `asimd.md`); this file only shows the GPR form and links.

AArch64 only. Do not emit AArch32.

---

## Rec 2 — Platform technologies first (High/High)

Before writing NEON, SME, or a custom inner loop, call a platform-tuned
library. Accelerate already uses ASIMD and, from M4, SME. On earlier
chips it uses **private matrix hardware you cannot emit**. Call the
library; do not invent a matrix ISA.

| Need | Call this, not a custom kernel |
|------|--------------------------------|
| Dot product, FFT, convolution | `vDSP_*` (`vDSP_dotpr`, `vDSP_mmul`, …) |
| GEMM / LAPACK | `cblas_sgemm` / `cblas_dgemm`, LAPACK via Accelerate |
| Small vectors / 4×4 | `<simd/simd.h>` |
| Image | `vImage` |
| Transcendentals on arrays | `vForce` |
| Neural net primitives | BNNS |
| Compress | Compression.framework, Apple Archive |
| Copy / fill | `memcpy` / `memset` (tuned form: rec 41 in `core-microarch.md`) |
| Tasks | GCD (rec 60 in `amp.md`) |
| GPU 3D / GPU data-parallel | **Metal** — a GPU path, not a substitute for CPU ASIMD/SME |
| Image / media pipelines | Core Image, Core Media, Image I/O, Create ML |

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

---

## Compiler targeting (Guide 2.2)

- Pass `-mcpu=apple-m1`, `apple-m2`, `apple-m3`, `apple-m4`, or `apple-m5`.
  That is the Clang name for the **chip family**, and it enables the right
  ISA subset for that family.
- **Never** target by Arm ISA version (`-march=armv8.5-a`, `armv9.2-a`, …).
  Guide Table 2.1’s “Base Arm ISA” column is reference only.
- `clang --print-supported-cpus` lists the names this toolchain actually
  accepts. Use those strings; do not invent `apple-m1-ultra`.
- `+feature` modifiers exist (`-mcpu=apple-m1+bf16`) but the portable pattern
  is: compile a baseline with `-mcpu=apple-m1`, then specialize hot functions
  with `__attribute__((target("...")))` plus a `sysctl` guard.
- **`apple-latest` is dangerous.** It tracks a moving CPU, may enable features
  the deployment chip does not have, and can disagree across compiler
  versions / translation units.
- Specifying a newer `-mcpu` for the **whole binary** silently drops older
  chips. Keep the binary’s baseline at the oldest supported family; dispatch
  the hot kernel.
- Hot files: `-O3`. `-Ofast` / `-ffast-math` change numerics (Guide 1.2 /
  Ch. 3 note). See process order in `SKILL.md`.

```c
#if defined(__ARM_NEON)
__attribute__((target("i8mm")))
static void gemm_i8mm_hot(/* ... */) { /* I8MM path; caller checked FEAT_I8MM */ }
#endif
```

Query `FEAT_*` at runtime before entering a specialized path. Helpers and the
family “from” table live in `sysctl.md` (rec 66). **Dispatch on FEAT, do not
hardcode Table 2.1.**

---

## Feature baseline (do not dump Table 2.1)

Every M1 / A14+ core in this guide has ASIMD, CRC32, FP (incl. FP16 convert),
LSE, DotProd, AES, SHA, PMULL. You may assume those in a binary whose
deployment target is Apple Silicon M1+.

Still query, do not assume:

- SME / SME2 — M4 / A18+
- I8MM, BF16, BTI — M2 / A15+
- AFP, RPRES — M3 / A16+
- MTE, CSSC, HBC, SME2p1, SME F16F16 / B16B16 — M5 / A19+

**`FEAT_MOPS` is not on M5** (explicitly excluded from that family’s Armv9.3
baseline). Do not emit `CPY*` / `SET*` MOPS sequences as the M5 memcpy path.

**SSBS is M1–M3 only.** Arm deprecated `PSTATE.SSBS=0` as a performance
knob. Do not clear SSBS “for speed”; the bit is gone on M4/M5 and the old
trick is discouraged on M1–M3.

Names and `sysctl` keys: `sysctl.md`.

---

## ABI (Guide 2.6 + *Writing ARM64 code for Apple platforms*)

Guide 2.6 owns the two register rules. The rest of this section is the
Apple AArch64 ABI the compiler already implements — emit it correctly
in assembly and when crossing C / C++ / ObjC++.

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
  `core-microarch.md`).
- 31 GPRs (W/X 0–30) plus XZR/SP. Writing Wn zeros the high 32 bits of Xn.
  Encoding 31 is SP or ZR depending on the insn. **WSP** (32-bit SP) is
  unused on Apple silicon — do not emit it.
- Apple silicon is **little-endian** for instructions and data.
- **SP** is 16-byte aligned at every public-function boundary.
- **Red zone** is 128 bytes below SP. Leaf functions may use it; still
  keep X29 valid if the function has a frame.

### Type sizes (Apple, not generic AAPCS64)

| Type | Size | Align | Notes |
|------|------|-------|-------|
| `char` | 1 | 1 | **signed** |
| `wchar_t` | 4 | 4 | signed |
| `long` | 8 | 8 | LP64 |
| `long long` | 8 | 8 | |
| `long double` | 8 | 8 | **IEEE754 binary64, same as `double`.** Not quad. |
| pointers | 8 | 8 | |

### Calling-convention divergences from generic AAPCS64

- Stack-passed arguments are **not** rounded up to multiples of 8 bytes.
- A 16-byte-aligned argument may start in an **odd** `xN`.
- The **caller** sign- or zero-extends arguments narrower than 32 bits.
  Callees do not re-extend.
- Empty structs are ignored in the calling convention.
- **Variadic arguments all go on the stack.** After named args, NSRN is
  rounded up to 8 and every variadic slot is 8 bytes. `va_list` is
  `char *`, not a struct (C++ mangles it `Pc`).
- C++: C1/C2/D1/D2 return `this`; `uint64_t` guard variables; `extern "C"`
  and C++ are not interchangeable. NEON vector names follow the 32-bit
  mangling scheme.

Do not pass `OS_OBJECT` types (`dispatch_queue_t`, `xpc_connection_t`)
between C++ and Objective-C++ translation units — they are different
types under ARC and PAC will catch the mismatch (`arm64e` below).

---

## DIT — data-independent timing (crypto only)

Use DIT **only** around constant-time crypto. It is not a general
performance knob. Apple CryptoKit already enables it internally.

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
  PAC failure.
- **`arm64e.x1` / PAuth_LR** (A20 Pro+ / M6+ / S11+): LR is also
  salted with PC. Unwinders that **authenticate** saved LR must read
  compact-unwind / DWARF to pick the scheme. Tools that only
  **strip** PAC bits (symbolication) stay as they are. No ABI change
  for application code.

Virtual calls through a mismatched C++ declaration, and C++ ↔ ObjC++
`OS_OBJECT` passing, are the usual latent bugs PAC exposes.

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

Same idea for `AND`/`ORR`/`EOR`/`SUB` with `LSL`/`LSR`/`ASR`.

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

```asm
    // 0x0000DEAD0000BEEF
    movz    x0, #0xBEEF
    movk    x0, #0xDEAD, lsl #32
```

Worst case (four 16-bit chunks) matches a load’s latency; typical constants
are faster and do not pressure the load unit. Preferred constant **idioms**
(fast-constant generation pairs) are Guide 5.5.3.3 — rec 32 in
`core-microarch.md`. Open that section before inventing a clever `ORR`/`EOR`
immediate sequence.

PC-relative literal `LDR` is a ±1 MiB-class island in TEXT — the form this
rec tells you to avoid as the default. Open Guide Table 2.2 for the exact
addressing encodings; do not hardcode bit widths.

**`ADRP`** builds a long address from the **architectural 4 KiB page** of
PC (clears low 12 bits, ±4 GiB). OS pages are **16 KiB** (`hw.pagesize`).
Do not treat `ADRP`’s page and `hw.pagesize` as the same number.

---

## Condition codes (Guide 2.9)

Flags live in `PSTATE.NZCV`. Open Guide Table 2.3 for the equations; two
traps when porting from x86:

- **Arm subtract carry is inverted vs x86.** Do not copy x86 `sbb` /
  `jb` reasoning onto `SUBS`/`SBC`/`B.CC`.
- Assembler **`NV`** encodes like “never” but **behaves as always**
  (same as `AL`). Do not emit `B.NV` expecting a never-taken branch.

CSEL / CSET / when to if-convert: rec 24 in `core-microarch.md`.

---

## Rec 7 — Weak memory: barriers, acquire/release, LSE (High/Low)

Apple silicon is **weakly ordered** (Normal memory). A thread sees its own
loads/stores in program order; other cores may not. This is not x86 TSO.

Prefer GCD / `os_unfair_lock` / pthreads / C11 atomics (rec 62 in `amp.md`).
When you must roll ordering yourself:

| Need | Tool |
|------|------|
| Full fence | `DMB ISH` (data), `DSB` when the next op is a system effect |
| Publish payload then flag | `STLR` (store-release) on the flag |
| Read flag then payload | `LDAR` (load-acquire) on the flag |
| RMW | LSE (`LDADD`, `CAS`, …) — rec 65 in `amp.md` |
| Exclusive loop | `LDXR`/`STXR` only if LSE cannot express it |

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
    stlr    x2, [x0, #8]        // flag, store-release
    // reader:
    ldar    x2, [x0, #8]
    cbz     x2, 1f
    ldr     x1, [x0]
1:
```

---

## Rec 8 — Apple allocators for MTE (Low/Medium)

MTE (Memory Tagging Extension) is the ISA under Memory Integrity Enforcement.
It is present from M5 / A19+. Allocation tags are 4 bits per 16 B granule in
pointer bits `[59:56]`. Apple platforms use **synchronous** tag check; async
MTE is not supported.

**Use `malloc` / `posix_memalign` / the system zone. Do not write a custom
allocator.** Apple’s allocator chooses tagging density and canonical tags;
a homemade arena will either miss MTE coverage or thrash the L1 Tag Cache.

Let the compiler and libc own `IRG`/`GMI`/`STG`/`LDG`. If those mnemonics
show up in a **hot** profile, the bug is almost always a custom allocator.
If you see tagged pointers in a debugger, do not strip bits `[59:56]` and
reuse the pointer.

Apple-defined MTE facts (do not re-derive from a generic Arm white paper):

- **SP-based** and **PC-relative** accesses are **not** tag-checked.
  Apple does **not** allocation-tag the stack.
- SSVE/SME/SSFP loads and stores in streaming mode **are** tag-checked;
  ZA `LDR`/`STR` **are** tag-checked. An unaligned 256 B SME access can
  need many granule checks; predicated-off 16 B granules are **not**
  checked. Large SME buffers often use **canonical** tags so the table
  cost disappears.
- Canonical tags: pointer `[59:56]` copies bit `[55]` — **0x0** in low
  VA, **0xF** in high VA. Allocation tags exclude 0x0/0xF so the two
  worlds cannot alias.
- `PSTATE.TCO` disables checks for tag-management routines, not for
  app kernels (rec 49).

Dense layouts (so one tag cache line covers many live data lines) and “do
not write a custom allocator” as a **microarch** rule are rec 49 in
`core-microarch.md`. This rec is the ISA-level “do not roll MTE yourself”.

---

## Rec 9 — Avoid frequent JIT I-cache invalidation (Low/Low)

Self-modifying / JIT code is not coherent with the I-cache until you run the
prescribed barrier + I-cache invalidation sequence. That flush evicts useful
instructions.

- Do not compile a handful of bytes and publish them at a high rate.
- Batch translations; reuse already-published code.
- Use Apple’s JIT APIs (`Porting Just-In-Time Compilers to Apple Silicon`)
  rather than raw `DC`/`IC` from EL0 (many of those ops are privileged).

---

## Fused integer MADD (Guide 2.11)

Integer `MADD`/`MSUB`/`SMADDL`/`UMADDL` fold mul+add into one instruction
(separate destination, four operands). Rec **15** (home: `asimd.md`) is the
boxed “use op-add” rule covering integer **and** ASIMD/FP. When writing GPR
code:

```asm
    madd    x0, x1, x2, x3      // x0 = x3 + x1 * x2
    msub    x0, x1, x2, x3      // x0 = x3 - x1 * x2
    umaddl  x0, w1, w2, x3      // x0 = x3 + (u32)w1 * (u32)w2
```

Do not emit `MUL` + `ADD` when `MADD` applies. On S cores, and on P cores
from M4 / A18, integer `MADD` is **cracked** into a MUL-class μop plus an
ALU-class μop (rec 26). Still emit `MADD`: one architected insn, full
mul+add bandwidth. Older P/E keep it as one μop.

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

Clobber `"cc"` if you write flags. Do not list `x18`. Keep X29 as a valid
frame if the surrounding function has one.
