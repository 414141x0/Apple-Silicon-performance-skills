<!-- Structure inspired by Intel Performance Skills (MIT, 2026).
     Content is original agent instructions paraphrasing Apple Silicon CPU
     Optimization Guide 5.0. Do not copy Guide tables. MIT license. -->

# sysctl: ISA and microarch constants (Guide Appendix B)

Owns rec **66**. This is the home of the feature table at **family**
granularity (not Guide Table 2.1). Do not copy the M1 Ultra numeric example
from the PDF. Show **how to query and print**.

Hardcoded L1D sizes, page sizes, or `FEAT_SME == 1` will be wrong on the
next chip (or on an E core vs a P core). Fetch them.

**Units (Guide 1.6):** bandwidths in **decimal** SI (`MB/s`, `GB/s`);
sizes and footprints in **binary** prefixes (`KiB`, `MiB`). `sysctl`
cache keys are **bytes**. Do not write “L1 is 128KB”.

**Mx family** = Mx + Pro + Max + Ultra (core *design* is shared; core
counts, topology, and bandwidths differ). Dispatch on `hw.perflevel{N}.*`,
not on a SKU name (`apple-m1-ultra` is not a `-mcpu`). Same idea for an
Ax family (Ax / Bionic / Pro).

---

## Rec 66 — Query sysctl for ISA and microarch constants (High/Low)

Use `sysctlbyname(3)` in code and `sysctl -a` / `sysctl -d <name>` in the
shell. Missing keys return `-1` with `errno == ENOENT` (feature not
implemented). Present keys are `0` or `1` for `FEAT_*`, or a byte count /
integer for topology.

`sysctl -d hw.pagesize` prints the C type; size the buffer accordingly.
`ENOMEM` means your buffer was too small.

---

## Family “from” table (dispatch baseline, still query)

| Feature | From |
|---------|------|
| ASIMD, CRC32, PMULL, DotProd, FP16, LSE, SHA, AES | All M1 / A14+ |
| BF16, I8MM, BTI | M2 / A15+ |
| AFP, RPRES | M3 / A16+ |
| SME, SME2, WFxT | M4 / A18+ |
| MTE, CSSC, HBC, SME2p1, SME F16F16/B16B16 | M5 / A19+ |
| `FEAT_MOPS` | **Not** on M5 (explicitly excluded) |
| SSBS | M1–M3 only; do not clear SSBS for speed |
| `FEAT_DIT`, `FEAT_SB` | query before crypto DIT fallback (`integer-isa.md`) |

Even on a “from” family, **query** before emitting the instruction. E cores
and P cores share ISA, so one `FEAT_*` answer is enough; cache sizes are
**per perflevel**.

Standardized names: `hw.optional.arm.FEAT_DotProd`, `FEAT_SME`,
`FEAT_I8MM`, `FEAT_BF16`, `FEAT_MTE`, `FEAT_LSE`, `FEAT_WFxT`, …

Always-on from M1/A14 (need not be checked in an Apple-Silicon-only
binary, but checking is harmless): `hw.optional.arm64`,
`hw.optional.AdvSIMD`, `hw.optional.floatingpoint`,
`hw.optional.armv8_crc32`.

---

## Legacy vs standardized names

Use the new names in new code.

| Legacy | Use instead |
|--------|-------------|
| `hw.optional.neon` | `hw.optional.AdvSIMD` |
| `hw.optional.neon_hpfp` | `hw.optional.AdvSIMD_HPFPCvt` |
| `hw.optional.neon_fp16` | `hw.optional.arm.FEAT_FP16` |
| `hw.optional.armv8_1_atomics` | `hw.optional.arm.FEAT_LSE` |
| `hw.optional.armv8_2_fhm` | `hw.optional.arm.FEAT_FHM` |
| `hw.optional.armv8_2_sha512` | `hw.optional.arm.FEAT_SHA512` |
| `hw.optional.armv8_2_sha3` | `hw.optional.arm.FEAT_SHA3` |
| `hw.optional.armv8_3_compnum` | `hw.optional.arm.FEAT_FCMA` |

---

## Topology and cache keys

`perflevel0` = **fastest** cores (S, or P if there is no S). Higher
`perflevel` numbers are slower types. `hw.nperflevels` is the count.

| Key | Meaning |
|-----|---------|
| `hw.nperflevels` | number of core types |
| `hw.perflevel{N}.name` | `"Super"` / `"Performance"` / `"Efficiency"` |
| `hw.perflevel{N}.logicalcpu` | enabled logical CPUs of that type |
| `hw.perflevel{N}.physicalcpu` | enabled physical CPUs of that type |
| `hw.perflevel{N}.l1dcachesize` | L1D bytes |
| `hw.perflevel{N}.l1icachesize` | L1I bytes |
| `hw.perflevel{N}.l2cachesize` | **Shared LL** bytes (legacy name) |
| `hw.perflevel{N}.cpusperl2` | cores sharing that LL |
| `hw.logicalcpu` / `hw.logicalcpu_max` | all types together |
| `hw.physicalcpu` / `hw.physicalcpu_max` | physical cores |
| `hw.cachelinesize` | coherence line (**128**) |
| `hw.pagesize` | VM page (**16384**) |
| `hw.l1dcachesize` etc. | **lowest-performing** cores (since macOS 12 / iOS 15) |

Do **not** use the non-perflevel `hw.l1dcachesize` to block a P-core kernel.
Do **not** look for an M Cache size; it is not in this interface and you
must not block to it (rec 36, `core-microarch.md`).

---

## Original helpers

```c
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/sysctl.h>

int sysctl_u32(const char *name, uint32_t *out)
{
    size_t n = sizeof(*out);
    if (sysctlbyname(name, out, &n, NULL, 0) != 0)
        return -1;
    return 0;
}

int sysctl_u64(const char *name, uint64_t *out)
{
    size_t n = sizeof(*out);
    if (sysctlbyname(name, out, &n, NULL, 0) != 0)
        return -1;
    return 0;
}

int sysctl_string(const char *name, char *buf, size_t buflen)
{
    if (sysctlbyname(name, buf, &buflen, NULL, 0) != 0)
        return -1;
    return 0;
}

/* 0 = native, 1 = Rosetta, -1 = error. ENOENT => treat as native. */
int processIsTranslated(void)
{
    int ret = 0;
    size_t size = sizeof(ret);
    if (sysctlbyname("sysctl.proc_translated", &ret, &size, NULL, 0) != 0) {
        if (errno == ENOENT)
            return 0;
        return -1;
    }
    return ret;
}

int feat_present(const char *feat_key /* e.g. "hw.optional.arm.FEAT_SME" */)
{
    uint32_t v = 0;
    if (sysctl_u32(feat_key, &v) != 0)
        return 0;                 /* ENOENT or other error => unavailable */
    return v == 1;
}
```

Print topology without baking in Ultra numbers:

```c
void print_cpu_geometry(void)
{
    uint32_t nlevels = 0, pagesz = 0, linesz = 0;
    sysctl_u32("hw.nperflevels", &nlevels);
    sysctl_u32("hw.pagesize", &pagesz);
    sysctl_u32("hw.cachelinesize", &linesz);
    printf("pagesize=%u cacheline=%u nperflevels=%u translated=%d\n",
           pagesz, linesz, nlevels, processIsTranslated());

    for (uint32_t i = 0; i < nlevels; ++i) {
        char key[64], name[32];
        uint32_t l1d = 0, l1i = 0, ll = 0, ncpu = 0, per_ll = 0;
        snprintf(key, sizeof key, "hw.perflevel%u.name", i);
        if (sysctl_string(key, name, sizeof name) != 0)
            strcpy(name, "?");
        snprintf(key, sizeof key, "hw.perflevel%u.l1dcachesize", i);
        sysctl_u32(key, &l1d);
        snprintf(key, sizeof key, "hw.perflevel%u.l1icachesize", i);
        sysctl_u32(key, &l1i);
        snprintf(key, sizeof key, "hw.perflevel%u.l2cachesize", i);
        sysctl_u32(key, &ll);
        snprintf(key, sizeof key, "hw.perflevel%u.logicalcpu", i);
        sysctl_u32(key, &ncpu);
        snprintf(key, sizeof key, "hw.perflevel%u.cpusperl2", i);
        sysctl_u32(key, &per_ll);
        printf("perflevel%u (%s): ncpu=%u L1D=%u L1I=%u LL=%u cpusperl2=%u\n",
               i, name, ncpu, l1d, l1i, ll, per_ll);
    }
}
```

Use `perflevel0.l1dcachesize` / `l2cachesize` as the blocking constants for
a high-QoS kernel (rec 36). Use `hw.logicalcpu` as the GCD worker count
starting point (rec 60, `amp.md`).

---

## Dispatch pattern

```c
typedef void (*dot_fn)(const float *, const float *, size_t, float *);

static dot_fn resolve_dot(void)
{
    if (feat_present("hw.optional.arm.FEAT_SME"))
        return dot_sme;          /* defined only in an SME TU */
    return dot_neon;             /* asimd.md kernel */
}
```

Compile the SME TU with `-mcpu=apple-m4` (or gate with
`__attribute__((target(...)))`). The process baseline stays `-mcpu=apple-m1`
if you still ship to M1. Compiler targeting: `integer-isa.md`.

Rosetta: if `processIsTranslated() == 1`, do not trust Apple-Silicon PMU
paths or SME; you are on the translator. Native AArch64 is this skill’s
world.

---

## Shell

```text
sysctl hw.pagesize hw.cachelinesize hw.nperflevels hw.logicalcpu
sysctl hw.perflevel0.l1dcachesize hw.perflevel0.l2cachesize
sysctl hw.optional.arm.FEAT_SME hw.optional.arm.FEAT_I8MM
sysctl hw.optional.arm.FEAT_BF16 hw.optional.arm.FEAT_MTE
sysctl hw.optional.arm.FEAT_DIT hw.optional.arm.FEAT_SB
sysctl -d hw.optional.arm.FEAT_SME
```

Do not paste a chip’s numeric dump into a header and call it portable.
