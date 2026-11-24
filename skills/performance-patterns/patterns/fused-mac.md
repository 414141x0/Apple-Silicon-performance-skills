<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->
# Pattern: fused multiply-accumulate (FMLA / MADD)

## When to apply

Apply when a hot loop computes `acc = acc + a * b` (or `acc - a * b`, or
an integer `a * b + c`) as **two instructions** — a multiply writing a
temporary, then a separate add. Guide rec 15 (High / Medium): use the
fused op-add form.

This is the integer `MADD`/`MSUB` family and the FP/ASIMD `FMLA`/`FMLS`
family. Related fused op-adds (`ABA`, shift-accumulate, `DOT`) follow the
same rule: do not split them.

### Source code signals

- `sum += a[i] * b[i];` with the compiler emitting `FMUL` + `FADD` (or
  `MUL` + `ADD`) rather than `FMLA` / `MADD`
- Explicit temporaries that kill fusion: `t = a * b; acc = acc + t;`
  across a store, a function call, or a sequence compiled with
  `#pragma STDC FP_CONTRACT OFF`
- Hand ASIMD that calls `vmulq_f32` then `vaddq_f32` instead of
  `vfmaq_f32`
- Scalar `a * b + c` built with `mulss`-style thinking, or `fma` not used
  on a chain that needs fused rounding
- Integer address / index math: `base + i * scale` written as `MUL`+`ADD`
  instead of `MADD` (or an addressing mode — codegen owns that)

### Profiling signals

- CPU Profiler annotate: `FMUL.4S` immediately followed by `FADD.4S` on
  the same registers, or `MUL` + `ADD` for the integer analogue
- Two-instruction dependent chain on the accumulator where one `FMLA.4S`
  would have covered it
- Processing-bound hotspot that is a reduction, dot, FIR, or matmul
  kernel — fusion is density, latency, *and* (for FP) accuracy

Check `platform-libs-first.md` before rewriting a dot or GEMM by hand.
`vDSP_dotpr` / `cblas_sgemm` already emit fused kernels.

---

## Why this is slow

A separate mul and add:

1. **Uses two issue slots and two result registers** instead of one
   instruction that writes the accumulator.
2. **Serializes latency** — the add cannot start until the mul writes the
   temp. Fused forms have a single (shorter) dependent latency into `acc`.
3. **Rounds twice** for FP — the mul rounds, then the add rounds. `FMLA`
   keeps the extra bits. That is a correctness *and* a performance feature:
   you get a better result from the instruction you wanted anyway.

Integer `MADD Wd, Wn, Wm, Wa` is `Wd = Wa + Wn*Wm`. Same density argument,
without the rounding angle. On S cores and on P from M4 / A18 it is
**cracked** into MUL + ALU μops — still emit `MADD` (codegen rec 26).

---

## The fix

Write the operation so the compiler (or the intrinsic) can emit `FMLA` /
`FMLS` / `MADD`. Prefer the fused intrinsic when the surrounding code
would otherwise block contraction.

### Floating-point / ASIMD

**Before** (splits into `FMUL` + `FADD`):

```c
float dot_split(const float *a, const float *b, int n)
{
    float acc = 0.0f;
    for (int i = 0; i < n; i++) {
        float t = a[i] * b[i];
        acc = acc + t;                 /* two rounding steps, two insns */
    }
    return acc;
}
```

**After — scalar FMA:**

```c
#include <math.h>

float dot_fma(const float *a, const float *b, int n)
{
    float acc = 0.0f;
    for (int i = 0; i < n; i++)
        acc = fmaf(a[i], b[i], acc);   /* FMADD / FMLA */
    return acc;
}
```

**After — ASIMD `vfmaq_f32` (four lanes, still fuse):**

```c
#include <arm_neon.h>

float dot_neon(const float * restrict a, const float * restrict b, int n)
{
    float32x4_t s0 = vdupq_n_f32(0.0f);
    float32x4_t s1 = vdupq_n_f32(0.0f);
    float32x4_t s2 = vdupq_n_f32(0.0f);
    float32x4_t s3 = vdupq_n_f32(0.0f);
    int i = 0;
    for (; i + 15 < n; i += 16) {
        s0 = vfmaq_f32(s0, vld1q_f32(a + i +  0), vld1q_f32(b + i +  0));
        s1 = vfmaq_f32(s1, vld1q_f32(a + i +  4), vld1q_f32(b + i +  4));
        s2 = vfmaq_f32(s2, vld1q_f32(a + i +  8), vld1q_f32(b + i +  8));
        s3 = vfmaq_f32(s3, vld1q_f32(a + i + 12), vld1q_f32(b + i + 12));
    }
    float32x4_t s = vaddq_f32(vaddq_f32(s0, s1), vaddq_f32(s2, s3));
    float acc = vaddvq_f32(s);
    for (; i < n; i++)
        acc = fmaf(a[i], b[i], acc);
    return acc;
}
```

`vfmsq_f32` is the fused subtract (`FMLS`). Do not write
`vsubq_f32(acc, vmulq_f32(a, b))`.

A single accumulator is still a serial chain — combine this pattern with
`parallel-accumulator.md` (four independent `FMLA`s) when the loop is
latency-bound.

If you stay in scalar C, `acc += a[i] * b[i]` fuses only when FP
contraction is on (`-ffp-contract=on` / default on Apple Clang for NEON,
or `#pragma STDC FP_CONTRACT ON`). An intervening store, `volatile`, or
`-ffp-contract=off` splits it again. `fmaf` / `vfmaq_f32` do not depend
on that.

### Integer `MADD`

**Before:**

```c
int axpy_i(int acc, int a, int x)
{
    int t = a * x;
    return acc + t;                    /* MUL + ADD */
}
```

**After:**

```c
int axpy_i(int acc, int a, int x)
{
    return acc + a * x;                /* MADD Wd, Wn, Wm, Wa */
}
```

Keep the mul and add adjacent in the same expression. A stored temporary
or a call in between will split `MADD` the same way it splits `FMLA`.

### Accuracy note (FP only)

Fused FP multiply-add does **not** match the two-rounding result of
`FMUL`+`FADD`. That is usually desirable. If a caller requires the
split rounding (bit-identical to an old x87/SSE dump), do not apply this
pattern; document why.

`-Ofast` / `-ffast-math` also change numerics in other ways (NaN, signed
zero, associativity). Prefer explicit `fmaf`/`vfmaq_f32` over a global
fast-math flag.

---

## Verification

1. **Annotate** — `clang -O3 -mcpu=apple-m4 -S` or CPU Profiler
   disassembly. The kernel should show `FMLA.4S` / `FMLA.2D` / `FMADD`
   / `MADD`, **not** `FMUL`+`FADD` or `MUL`+`ADD` on the same values.
2. **Correctness** — for FP, compare against the old split form with an
   ε that allows fused rounding. A bit-exact match is not required and
   often will not happen.
3. **CPU Bottlenecks** — Processing pressure from two dependent ops
   should drop; instruction count in the loop body should drop by ~one
   per fused slot.

---

## Presenting this to the user

1. Show `acc = acc + a * b` (or the `vmulq`+`vaddq` pair) marked
   `/* ◄ split mul+add */`.
2. One paragraph: density, latency, and fused FP accuracy. Mention rec 15.
3. Show `vfmaq_f32` / `fmaf` / `MADD` next to the original.
4. If this is a dot, GEMM, or FIR, ask whether Accelerate already covers
   it (`platform-libs-first.md`) *before* committing a hand kernel.
5. Rebuild and show the mnemonic: `FMLA.4S` is the proof.
