<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Library: runtime CPU dispatch

A reusable implementation guide for selecting the fastest available variant of a
function at runtime, based on what the chip supports. This technique applies
whenever a pattern fix produces multiple performance-level implementations of
the same function.

**Used by:** `patterns/simd-upconversion.md`, `patterns/parallel-accumulator.md`,
`patterns/fast-crc32c.md`, `references/known-algorithms-impl.md`, and any
future pattern that generates multiple CPU-specific variants.

**Never dispatch on "Arm version"** (`armv8.6`, `armv9.2`). Dispatch on
`hw.optional.arm.FEAT_*` and compile variants with `-mcpu=apple-m*` /
`__attribute__((target(...)))`. See `apple-silicon-codegen/references/sysctl.md`
and `chip-families.md`.

---

## Decision guide: which mechanism to use?

| Situation | Use |
|-----------|-----|
| Hand-written ASIMD / SME / CRC variants | **Mechanism B: function pointer + `apple_feat()`** (Darwin default) |
| Plain C loop, no intrinsics, you want the compiler to clone it | **Mechanism A: `target_clones`** — mention only; not the default |
| Rosetta (`sysctl.proc_translated == 1`) | Portable / ASIMD path only. **Refuse SME.** |
| Shared library | Mechanism B — ifunc ordering is less familiar on Darwin |

When in doubt, use **Mechanism B**. It is explicit, auditable, and matches
how Darwin projects actually ship ISA variants.

---

## Helper: `apple_feat` / `apple_is_translated`

```c
#ifndef APPLE_CPU_DISPATCH_H
#define APPLE_CPU_DISPATCH_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/sysctl.h>

/* apple_feat("FEAT_SME")       → hw.optional.arm.FEAT_SME
 * apple_feat("armv8_crc32")    → hw.optional.armv8_crc32
 * apple_feat("hw.optional.arm.FEAT_I8MM") → used as-is
 * Missing key (ENOENT) or value 0 → false.
 */
static inline bool apple_feat(const char *name)
{
    char key[128];
    if (strchr(name, '.') != NULL) {
        snprintf(key, sizeof(key), "%s", name);
    } else if (strncmp(name, "FEAT_", 5) == 0) {
        snprintf(key, sizeof(key), "hw.optional.arm.%s", name);
    } else {
        snprintf(key, sizeof(key), "hw.optional.%s", name);
    }

    int val = 0;
    size_t len = sizeof(val);
    if (sysctlbyname(key, &val, &len, NULL, 0) != 0)
        return false;
    return val != 0;
}

/* 1 = Rosetta translation; 0 = native; missing key → native. */
static inline bool apple_is_translated(void)
{
    int ret = 0;
    size_t size = sizeof(ret);
    if (sysctlbyname("sysctl.proc_translated", &ret, &size, NULL, 0) == -1)
        return false;
    return ret == 1;
}

#endif
```

Useful keys (query; do not hardcode the answers):

| Key | Meaning |
|-----|---------|
| `hw.optional.arm.FEAT_SME` / `FEAT_SME2` | SME / SME2 (M4 / A18+) |
| `hw.optional.arm.FEAT_DotProd` | Int8 dot product (all M1+) |
| `hw.optional.arm.FEAT_I8MM` | Int8 matrix (M2 / A15+) |
| `hw.optional.arm.FEAT_BF16` | BFloat16 (M2+) |
| `hw.optional.arm.FEAT_MTE` | MTE (M5 / A19+) |
| `hw.optional.armv8_crc32` | CRC32 (all M1+) |
| `hw.optional.AdvSIMD` | ASIMD (all M1+) |
| `hw.cachelinesize` | 128 on Apple silicon |
| `hw.pagesize` | 16384 |
| `hw.nperflevels`, `hw.perflevel{N}.*` | S / P / E topology |

`sysctl -a hw.optional.arm` lists FEAT keys on the current chip. Unimplemented
features return `ENOENT`, not 0 — `apple_feat` treats that as false.

---

## Mechanism B: function pointer + sysctl (default)

The resolver runs once on first call, stores a `static` function pointer,
and every subsequent call is an indirect branch.

```c
typedef float (*dot_fn_t)(const float * restrict, const float * restrict, int);

static dot_fn_t dot_dispatch(void)
{
    if (apple_is_translated())
        return dot_scalar;           /* no SME under Rosetta */
    if (apple_feat("FEAT_SME"))
        return dot_sme;
    return dot_asimd;                /* ASIMD is baseline on every M1+ */
}

float dot(const float * restrict a, const float * restrict b, int n)
{
    static dot_fn_t fn;
    if (!fn)
        fn = dot_dispatch();
    return fn(a, b, n);
}
```

The `static` pointer is written once and never changes. Two threads racing
on first call both write the same value — no lock required.

### `__attribute__((target(...)))` on each variant

Guard each variant's definition with a matching `target` attribute so the
compiler emits the encoding even when the translation unit's `-mcpu` is
lower:

```c
__attribute__((target("sme2")))
static float dot_sme(const float * restrict a, const float * restrict b, int n)
{
    /* SME ZA kernel; compile this file or function for apple-m4 / apple-m5 */
}

__attribute__((target("dotprod")))
static int hamming_dotprod(const uint8_t * restrict a,
                           const uint8_t * restrict b, int n)
{
    /* UDOT path */
}
```

Clang target strings used here: `"dotprod"`, `"i8mm"`, `"bf16"`, `"crc"`,
`"sme"`, `"sme2"`. Pair with `-mcpu=apple-m1` (m2/m3/m4/m5) for the
baseline TU. `apple-latest` is unstable across compiler versions — avoid
it for shipped binaries.

Without `target` on the definition, the compiler may not encode SME/DotProd
when the file-level `-mcpu` is `apple-m1`.

### Correctness rule: the guard must cover every required feature

`apple_feat` must be a superset of every instruction in the variant.
Under-specification is an **illegal instruction** on a CPU that passes the
guard but lacks a feature (M1 running an I8MM body, M3 running SME). After
any widening, re-check the FEAT comments and the resolver.

---

## Mechanism A: `target_clones` (optional)

Clang on Darwin can emit `target_clones` / ifunc resolvers for some AArch64
features (`+dotprod`, `+sme2`). Support is less common in shipped Darwin
code than function pointers, and SME clones are easy to get wrong.

```c
__attribute__((noinline, target_clones("dotprod", "default")))
void process(float *src, float *dst, int n)
{
    for (int i = 0; i < n; i++)
        dst[i] = src[i] * 2.0f;
}
```

Use this only for plain C that the compiler can auto-vectorize. It does
**not** rewrite hand-written `float32x4_t` / SME intrinsics into a wider
ISA. Prefer Mechanism B for anything this skill generates.

If you emit `target_clones`, verify a resolver symbol with
`nm -gU ./binary | grep process` and test on a machine *without* the extra
feature, plus Rosetta.

---

## Topology is not an ISA feature

Core counts, cache sizes, and line/page size are `hw.perflevel{N}.*`,
`hw.cachelinesize`, `hw.pagesize`. Query them for blocking and thread
pools; do not mix them into the ISA resolver. See
`patterns/sysctl-hardcoding.md` and `patterns/amp-work-partition.md`.
