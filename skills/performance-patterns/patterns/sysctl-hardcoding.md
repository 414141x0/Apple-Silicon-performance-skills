<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->
# Pattern: never hardcode cache/page/core/ISA constants

## When to apply

Apply whenever source contains a **magic 64**, **4096**, **8 cores**,
or `#ifdef __ARM_FEATURE_SME` treated as “this Mac has SME” (Guide
rec 66, High / Low). Apple silicon today is **128-byte** cache lines
and **16 KiB** pages; core counts and ISA features vary by SKU. Query
`sysctl`. Do not compile for “Armv8.5” and assume the chip matches.

### Source code signals

- `alignas(64)`, `__attribute__((aligned(64)))`, `char pad[64]`,
  `#define CACHELINE 64` (false-sharing padding must be 128 on these
  chips — query, don’t guess)
- `sysconf(_SC_PAGESIZE)` assumed 4096, or `mmap` hints with `1 << 12`
- `worker_count = 8;`, `for (cpu = 0; cpu < 8; cpu++)`, “M1 has 4+4”
- `has_sme = 1;` / dispatch on `__ARM_ARCH` / `-march=armv8.6-a`
- `memcpy` replacements using `FEAT_MOPS` without a feature check
  (MOPS is **not** on M5)
- Clearing `SSBS` “for speed” (M1–M3 only; do not clear)
- No `sysctl.proc_translated` check in a binary that might run under
  Rosetta

### Profiling signals

- False sharing that `alignas(64)` did not fix (two writers still
  share a 128 B line)
- TLB pressure from assuming 4 KiB pages when the machine is 16 KiB
- A “SME” kernel `ILL` or silently taking the scalar path on M1–M3
- Instruments on a Rosetta process (`sysctl.proc_translated == 1`) —
  you are not measuring Apple silicon

---

## Why this is slow

Hardcoded constants are **wrong on this ISA family**, not just brittle:

| Assumption | Reality on Apple silicon (query, don’t paste) |
|------------|-----------------------------------------------|
| Cache line 64 B | `hw.cachelinesize` is 128 |
| Page 4 KiB | `hw.pagesize` is 16 KiB (ADRP’s architectural page is still 4 KiB) |
| 8 cores, symmetric | `hw.logicalcpu` + `hw.nperflevels`; `perflevel0` is fastest |
| “Has SME” | `hw.optional.arm.FEAT_SME` — M4 / A18+ only |
| Target by Arm version | `-mcpu=apple-m1` / `apple-m4` / … never `-march=armv8.x` |
| `FEAT_MOPS` | **Not** on M5; use `memcpy` |
| `SSBS` | M1–M3 only; Arm deprecated `PSTATE.SSBS=0` for speed — do not clear |

A 64 B pad leaves two “independent” atomics on the same 128 B line.
A 4 KiB arena isolation for SME (see `sme-engine-bounce.md`) is a
*different* granule from the VM page — query both, mix up neither.

---

## The fix

### 1. Query, then use

```c
#include <sys/sysctl.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

typedef struct {
    size_t cacheline;     /* hw.cachelinesize, typically 128 */
    size_t pagesize;      /* hw.pagesize, typically 16384 */
    int    logicalcpu;    /* enabled logical cores */
    int    nperflevels;   /* 2 on most M-series (P + E), etc. */
    int    feat_sme;
    int    feat_sme2;
    int    feat_wfxt;
    int    feat_mte;
    int    feat_dotprod;
    int    feat_i8mm;
    int    feat_mops;     /* 0 on M5; treat ENOENT as 0 */
    int    translated;    /* sysctl.proc_translated: 1 = Rosetta */
} as_hw_t;

static int as_sysctl_int(const char *name, int *out)
{
    int v = 0;
    size_t n = sizeof(v);
    if (sysctlbyname(name, &v, &n, NULL, 0) != 0) {
        *out = 0;          /* ENOENT → feature absent */
        return -1;
    }
    *out = v;
    return 0;
}

static int as_sysctl_size(const char *name, size_t *out)
{
    size_t v = 0, n = sizeof(v);
    if (sysctlbyname(name, &v, &n, NULL, 0) != 0) {
        *out = 0;
        return -1;
    }
    *out = v;
    return 0;
}

int as_hw_query(as_hw_t *h)
{
    as_hw_t z = {0};
    *h = z;
    as_sysctl_size("hw.cachelinesize", &h->cacheline);
    as_sysctl_size("hw.pagesize",      &h->pagesize);
    as_sysctl_int("hw.logicalcpu",     &h->logicalcpu);
    as_sysctl_int("hw.nperflevels",    &h->nperflevels);
    as_sysctl_int("hw.optional.arm.FEAT_SME",     &h->feat_sme);
    as_sysctl_int("hw.optional.arm.FEAT_SME2",    &h->feat_sme2);
    as_sysctl_int("hw.optional.arm.FEAT_WFxT",    &h->feat_wfxt);
    as_sysctl_int("hw.optional.arm.FEAT_MTE",     &h->feat_mte);
    as_sysctl_int("hw.optional.arm.FEAT_DotProd", &h->feat_dotprod);
    as_sysctl_int("hw.optional.arm.FEAT_I8MM",    &h->feat_i8mm);
    as_sysctl_int("hw.optional.arm.FEAT_MOPS",    &h->feat_mops);
    as_sysctl_int("sysctl.proc_translated",       &h->translated);

    if (h->cacheline == 0) h->cacheline = 128;   /* last-ditch, still not 64 */
    if (h->pagesize  == 0) h->pagesize  = 16384;
    if (h->logicalcpu <= 0) h->logicalcpu = 1;
    return 0;
}
```

Command line, same facts:

```bash
sysctl hw.cachelinesize hw.pagesize hw.logicalcpu hw.nperflevels \
       hw.perflevel0.l1dcachesize hw.optional.arm.FEAT_SME \
       sysctl.proc_translated
```

`perflevel0` is the **fastest** core type. Cache sizes used for
blocking come from `hw.perflevel{N}.*`, not from a comment that says
“M1 L1D is …”. Open Guide Appendix B and `sysctl -d hw.perflevel0`
rather than pasting a per-chip table.

### 2. Compile for a chip, not an Arm version

```text
# yes
clang -O3 -mcpu=apple-m1 …
clang -O3 -mcpu=apple-m4 …
clang --print-supported-cpus     # see what this toolchain knows

# no
clang -march=armv8.5-a …
clang -mcpu=apple-latest …       # moves under your feet
```

Dispatch **inside** the binary with the `FEAT_*` queries above, not
with `#if __ARM_ARCH >= 9`.

### 3. Rosetta is a stop, not a path

```c
    as_hw_t hw;
    as_hw_query(&hw);
    if (hw.translated == 1) {
        fprintf(stderr, "translated process; rebuild native arm64\n");
        return 1;
    }
```

Do not profile, tune, or enable SME under translation.

### 4. SSBS and MOPS

- **SSBS** exists on M1–M3. Do not clear `PSTATE.SSBS` to “go faster”.
  Arm deprecated that; it is gone on M4+.
- **FEAT_MOPS** is not on M5. Call `memcpy` / `memset`
  (`platform-libs-first.md`) instead of emitting `CPY*` / `SET*`.

### 5. Padding and alignment use the queried line

```c
/* false sharing: pad to *hw.cacheline, not 64 */
size_t pad = hw.cacheline;   /* 128 today */

/* SME core vs engine isolation uses 4 KiB granules — that is an
   engine-hazard size, not hw.pagesize. Do not substitute 16384
   “to be safe” without reading sme-engine-bounce.md. */
```

---

## Verification

1. `sysctl hw.cachelinesize hw.pagesize` on the target Mac matches
   what the binary prints from `as_hw_query`.
2. `otool -tv` / source grep: no `64` used as a line size, no
   `4096` used as a VM page, no `nthreads = 8`.
3. On an M1, `feat_sme == 0` and the SME path is not entered. On an
   M4, it is 1. On an M5, `feat_mops == 0`.
4. `sysctl.proc_translated` is 0 in any Instruments session you
   intend to trust.

---

## Presenting this to the user

1. Quote the magic `64` / `4096` / `8` / “has SME” with
   `/* ◄ query sysctl */`.
2. Show `as_hw_query` (or the two-line `sysctlbyname`) and the
   `-mcpu=apple-m*` flag.
3. Mention Rosetta, SSBS (do not clear), and MOPS-not-on-M5 in one
   sentence each so they do not get re-introduced.
4. Point at Appendix B of the Guide for the full parameter list —
   do not paste chip-family numeric tables into the repo.
