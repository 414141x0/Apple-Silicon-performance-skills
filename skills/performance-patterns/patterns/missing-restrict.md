<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->

# Pattern: missing `restrict` on pointer parameters

## When to apply

Apply to **C functions** (not C++) that take two or more pointer parameters
where at least one is written, and those pointers are guaranteed by the caller
to refer to non-overlapping memory. Without `restrict`, the compiler must assume
that a write through one pointer might alias a read through another, which forces
it to either:

- emit a **runtime alias check** (versioned loop: overlap test → scalar fallback
  or vectorized path), adding preamble code and i-cache pressure, or
- **give up on auto-vectorization entirely** for that loop.

`restrict` is a contract: you promise the compiler the pointers do not overlap.
In return it eliminates the alias check and may produce a tighter, fully
vectorized loop.

This pattern applies **only to C**. C++ does not have a standard `restrict`
keyword; `__restrict__` (GCC/Clang extension) can be used in C++ but is not
portable.

### Source code signals

- A C function takes two or more pointer parameters, at least one is written
- The function processes independent input and output buffers:
  `void process(float *in, float *out, int n)`,
  `void add(const float *a, const float *b, float *dst, int n)`
- No `restrict` qualifier on any of the pointer parameters
- The loop body reads from one pointer and writes to a different one
- Caller documentation or usage clearly shows the buffers never overlap

### Profiling signals

- CPU Profiler / `llvm-objdump -d` shows a **scalar preamble** before the
  main loop — pointer subtraction, comparison, and `B.cond` to a scalar
  fallback — even though the loop body is otherwise simple and vectorizable
- Two versions of the same loop appear: one packed (`.4S` / `.2D`), one
  scalar; the scalar version is the aliasing fallback
- A hot loop is fully scalar (`FADDS`, `FMULS`) even though there are no
  dependencies, branches, or calls in the body — Clang abandoned
  vectorization due to aliasing uncertainty

---

## Why this is slow

Without `restrict`, the compiler must assume `dst` could alias `a` or `b`, so it
either emits a runtime overlap check plus two loop versions (~10–20 instructions
of preamble, extra i-cache pressure) or abandons vectorization entirely — cycles
spent on alias bookkeeping the programmer already knows is unnecessary.

---

## The fix

Add `restrict` to each pointer parameter that the caller guarantees will not
overlap any other pointer parameter:

```c
/* Before */
void add(float *a, float *b, float *dst, int n);

/* After — input buffers marked const restrict, output marked restrict */
void add(const float * restrict a,
         const float * restrict b,
         float       * restrict dst,
         int n);
```

With `restrict`:
- The compiler eliminates the runtime overlap check entirely.
- The loop becomes a single straight-line vectorized body — no scalar fallback,
  no branching preamble.
- `const` on input pointers is a separate but complementary improvement: it
  signals read-only access and prevents accidental writes.

### Guidelines for applying `restrict`

| Pointer role | Qualifier |
|---|---|
| Read-only input | `const float * restrict` |
| Write-only output | `float * restrict` |
| Read-write in-place (e.g. `scale(float *buf, ...)`) | `float * restrict` |
| Pointer to a scalar parameter (e.g. `int *count`) — never aliased | `restrict` rarely needed; omit unless profiler confirms overhead |

### Confirming the alias check is gone

```bash
# Before: look for pointer subtraction + B.cond before the loop
clang -O3 -S -o before.s add.c
# After: preamble gone; packed ASIMD body
clang -O3 -S -o after.s add.c

clang -O3 -Rpass=loop-vectorize add.c
```

Do not use `gcc -fopt-info-vec` as the primary check on Darwin — Apple's
toolchain is Clang.

---

## Caveats and when not to apply

- **`restrict` is a correctness contract.** If you add it and the caller ever
  passes overlapping pointers, the behavior is undefined — not just slow.
  Confirm the no-overlap invariant with code review, documentation, or an
  assert before applying.
- **Do not apply to aliased parameters by design.** An in-place filter
  (`filter(float *buf, float *buf, int n)`) is intentionally aliased; adding
  `restrict` would be incorrect.
- **C++ only:** use `__restrict__` (GCC/Clang) as a non-standard extension if
  needed. It has the same semantics but is not guaranteed portable across all
  compilers.
- **Inline functions and LTO**: with link-time optimization (`-flto`), the
  compiler may already prove non-aliasing at call sites. `restrict` is still
  useful for the header/declaration alone without LTO.

---

## Verification

1. **Assembly check** — compare `clang -O3 -S` output before and after; confirm
   the scalar aliasing fallback and preamble overhead are gone. Confirm with
   `clang -O3 -Rpass=loop-vectorize`.
2. **Correctness** — run the full test suite. If any test fails after adding
   `restrict`, the caller was passing overlapping pointers — remove `restrict`
   and fix the caller instead.
3. **Profiling** — rerun CPU Profiler; the scalar preamble should disappear
   from the hot path.

---

## Presenting this to the user

1. Show the function signature without `restrict` and the aliasing preamble
   in the assembly.
2. Explain that `restrict` is a no-overlap contract, not a hint the compiler
   can ignore.
3. Show the new signature with `const` + `restrict`.
4. Note C vs C++ (`__restrict__`).
5. Ask for a test-suite run before calling the change done.
