<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# sysctl: ISA and microarch constants

Owns rec **66**. Home of **how to query**, the compact family “from”
table for dispatch, the legacy name map, and the C helpers. ISA ladder,
family aliases, and S/P/E *why*: `chip-families.md`. PMU names:
`named-events.md`.

Hardcoded L1D sizes, page sizes, core counts, or `FEAT_SME == 1` will be
wrong on the next chip (or on an E core vs a P core). Fetch them.

**Units:** bandwidths in **decimal** SI (`MB/s`, `GB/s`); sizes and
footprints in **binary** prefixes (`KiB`, `MiB`). `sysctl` cache keys are
**bytes**. Do not write “L1 is 128KB”. Line is 128 B; VM page is 16 KiB
(query, do not assume). Architectural `ADRP` page is still 4 KiB —
unrelated to `hw.pagesize`.

An **Mx family** is Mx + Pro + Max + Ultra (core *design* is shared; core
counts, topology, and bandwidths differ). Dispatch on `hw.perflevel{N}.*`
and `hw.optional.arm.FEAT_*`, not on a SKU name (`apple-m1-ultra` is not a
`-mcpu`). Same idea for an Ax family (Ax / Bionic / Pro).

---

## Rec 66 — Query sysctl for ISA and microarch constants (High/Low)

Hardcoded cache bytes, core counts, or “this FEAT is always on” break on
the next SKU. Fetch ISA availability, worker counts, and cache sizes
from the live system.

Shell: `sysctl` / `sysctl -a` / `sysctl -d <name>`. `sysctl -a` lists
everything on this chip; subsets include `hw`, `kern`, `user`,
`machdep`. `sysctl -d <name>` prints the C type; size the buffer
accordingly.

Code: `sysctlbyname(const char *name, void *oldp, size_t *oldlenp,
void *newp, size_t newlen)` reads `name` into `*oldp`. Failure: return
`-1`, `errno` set. Missing name → `ENOENT`. Short buffer → `ENOMEM`.
Pass `newp = NULL`, `newlen = 0` for a read.

Example keys (values come from the live system, never from a baked
dump): `hw.pagesize`, `hw.cachelinesize`, `hw.physicalcpu_max`
(physical GPR cores), `hw.optional.arm.FEAT_DotProd`.

`FEAT_*` present = **1**, absent = **0**, unimplemented name =
`ENOENT`. Prefer `hw.optional.arm.FEAT_*` over legacy names.

### Rosetta — `sysctl.proc_translated`

`sysctlbyname("sysctl.proc_translated", …)` → **0** native, **1**
translated, **-1** error. `ENOENT` (key missing) ⇒ treat as native.
Translated processes are not Apple-Silicon PMU / SME paths. Helper
below. Also see *Addressing Architectural Differences in Your macOS
Code* on the Apple Developer site.

---

## ISA keys

Apple silicon is A-profile AArch64 (Armv8 and follow-on Armv9 by
generation). Compiled software already has a base ISA; dynamically
checking “are we AArch64?” is generally unnecessary.

### Base ISA

| Key | Meaning |
|-----|---------|
| `hw.optional.arm64` | A-profile AArch64 Armv8/Armv9 |

### Unofficial always-on names (M1 / A14+)

These have **no official Arm `FEAT_*` name**. Parameters exist; they
are present on all Apple silicon from M1 / A14 and do **not** need a
runtime check in an Apple-Silicon-only binary.

| Key | Meaning |
|-----|---------|
| `hw.optional.armv8_crc32` | CRC32 instructions |
| `hw.optional.floatingpoint` | floating-point instructions |
| `hw.optional.AdvSIMD` | Advanced SIMD |
| `hw.optional.AdvSIMD_HPFPCvt` | Advanced SIMD half-precision convert |

### Standardized `FEAT_*` keys

Pattern: `hw.optional.arm.FEAT_<Name>`. Value **1** / **0** /
`ENOENT`. Query before emitting an insn that is not on every
deployment chip. Do not paste a feature matrix. Ladder *why* and
agent actions: `chip-families.md`.

Keys agents actually query:

`hw.optional.arm.FEAT_DotProd`, `FEAT_LSE`, `FEAT_LSE2`, `FEAT_FP16`,
`FEAT_FHM`, `FEAT_SHA512`, `FEAT_SHA3`, `FEAT_FCMA`, `FEAT_I8MM`,
`FEAT_BF16`, `FEAT_BTI`, `FEAT_ECV`, `FEAT_AFP`, `FEAT_RPRES`,
`FEAT_SME`, `FEAT_SME2`, `FEAT_WFxT`, `FEAT_MTE`, `FEAT_CSSC`,
`FEAT_HBC`, `FEAT_DIT`, `FEAT_SB`, `FEAT_FlagM`, `FEAT_FlagM2`,
`FEAT_FRINTTS`, `FEAT_JSCVT`, `FEAT_LRCPC`, `FEAT_LRCPC2`, `FEAT_RDM`,
`FEAT_DPB`, `FEAT_DPB2`, `FEAT_AES`, `FEAT_SHA1`, `FEAT_SHA256`,
`FEAT_PMULL`, `FEAT_EBF16`, `FEAT_SME2p1`. SME outer-product subfeats
(`FEAT_SME_F32F32`, `FEAT_SME_F64F64`, `FEAT_SME_I16I64`, …) must be
**queried**, not assumed from `FEAT_SME=1`.

`FEAT_MOPS` and `FEAT_CMOW` are **not** on M5 (`ENOENT` or 0). Do not
emit `CPY*`/`SET*` as the M5 memcpy path. `FEAT_SSBS` is M1–M3 only;
do not clear `PSTATE.SSBS=0` for speed.

E and P share ISA — one `FEAT_*` answer is enough. Cache sizes are
**per perflevel**.

---

## Family “from” table (dispatch baseline, still query)

Compact dispatch aid. ISA details: `chip-families.md`. Even on a
“from” family, **query** before emitting.

Always-on from M1/A14 (Apple-Silicon-only binary need not check;
checking is harmless): ASIMD, CRC32, floating-point, AdvSIMD HPFP
convert, FP16, LSE, LSE2, DotProd, AES, SHA1/256/3/512, PMULL, RDM,
FCMA, FHM, FlagM/FlagM2, FRINTTS, JSCVT, LRCPC/LRCPC2, DIT, DPB/DPB2,
SB.

| Feature | From |
|---------|------|
| baseline above | All M1 / A14+ |
| BF16, I8MM, BTI, ECV | M2 / A15+ |
| AFP, RPRES | M3 / A16+ |
| SME, SME2, WFxT, SME outer-product subfeats | M4 / A18+ |
| MTE, CSSC, HBC, SME2p1, SME F16F16/B16B16, SVE_B16B16, EBF16 | M5 / A19+ |
| `FEAT_MOPS`, `FEAT_CMOW` | **Not** on M5 |
| SSBS | M1–M3 only |
| `FEAT_DIT`, `FEAT_SB` | query before crypto DIT fallback (`integer-isa.md`) |

---

## Legacy vs standardized names

Legacy names remain for old software. **Use the new names** in new
code. “NEON” is no longer Arm’s term.

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

## Per-perflevel keys

`hw.nperflevels` = number of general-purpose core types. **Lower N =
higher-performing type.** `perflevel0` is Super, or Performance if
there is no S. Available macOS 12 / iOS 15 / iPadOS 15 and later.

`l2cachesize` here is **Shared LL** (formerly Shared L2) — cluster
last-level, not a private L2. **M Cache is not in this interface.** Do
not block to it (rec 36, `core-microarch.md`). Private L2 exists on
some S cores only; it is also not a sysctl blocking target.

Query these. Do **not** paste a worked Ultra (or any SKU) numeric dump
into a header.

| Key | Meaning |
|-----|---------|
| `hw.nperflevels` | number of core types |
| `hw.perflevel{N}.cpusperl2` | cores of type N that share a Shared LL (cluster width) |
| `hw.perflevel{N}.l1dcachesize` | L1D bytes for type N |
| `hw.perflevel{N}.l1icachesize` | L1I bytes for type N |
| `hw.perflevel{N}.l2cachesize` | Shared LL bytes for type N |
| `hw.perflevel{N}.logicalcpu` | **enabled** logical cores of type N |
| `hw.perflevel{N}.logicalcpu_max` | logical cores of type N in the chip |
| `hw.perflevel{N}.name` | `"Super"` / `"Performance"` / `"Efficiency"` |
| `hw.perflevel{N}.physicalcpu` | **enabled** physical cores of type N |
| `hw.perflevel{N}.physicalcpu_max` | physical cores of type N in the chip |

---

## Updated existing (chip-wide) keys

From macOS 12 / iOS 15 / iPadOS 15, unqualified cache sizes describe
the **lowest-performing** cores. Older OSes reported the **boot core**
(P or E). Do **not** block a P-core kernel to the unqualified L1D.

| Key | Meaning |
|-----|---------|
| `hw.l1dcachesize` | L1D bytes of the **lowest-performing** cores |
| `hw.l1icachesize` | L1I bytes of the **lowest-performing** cores |
| `hw.l2cachesize` | Shared LL bytes of the **lowest-performing** cores |
| `hw.cachelinesize` | system coherence line (**128**) |
| `hw.memsize` | DRAM bytes |
| `hw.pagesize` | VM page (**16384**) |
| `hw.logicalcpu` | enabled logical cores, all types (alias of `hw.activecpu`) |
| `hw.logicalcpu_max` | logical cores in the chip (alias of `hw.ncpu`) |
| `hw.activecpu` | alias of `hw.logicalcpu` |
| `hw.ncpu` | alias of `hw.logicalcpu_max` |
| `hw.physicalcpu` | enabled physical cores, all types |
| `hw.physicalcpu_max` | physical GPR cores in the chip |

Use:

- `hw.logicalcpu` — GCD worker starting point (rec 60, `amp.md`)
- `hw.perflevel0.l1dcachesize` / `l2cachesize` — high-QoS cache blocking
- `hw.perflevel{N}.cpusperl2` — cluster-aware sharing (rec 63, `amp.md`)

---

## Helpers

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
if you still ship to M1. Compiler targeting: `integer-isa.md`. Family
ladder: `chip-families.md`.

Rosetta: if `processIsTranslated() == 1`, do not trust Apple-Silicon PMU
paths or SME; you are on the translator. Native AArch64 is this skill’s
world.

---

## Shell

```text
sysctl hw.pagesize hw.cachelinesize hw.nperflevels hw.logicalcpu
sysctl hw.physicalcpu_max hw.memsize
sysctl hw.perflevel0.name hw.perflevel0.l1dcachesize hw.perflevel0.l2cachesize
sysctl hw.perflevel0.logicalcpu hw.perflevel0.cpusperl2
sysctl hw.optional.arm64 hw.optional.AdvSIMD hw.optional.armv8_crc32
sysctl hw.optional.arm.FEAT_SME hw.optional.arm.FEAT_I8MM
sysctl hw.optional.arm.FEAT_BF16 hw.optional.arm.FEAT_MTE
sysctl hw.optional.arm.FEAT_DIT hw.optional.arm.FEAT_SB
sysctl hw.optional.arm.FEAT_WFxT hw.optional.arm.FEAT_LSE
sysctl sysctl.proc_translated
sysctl -d hw.optional.arm.FEAT_SME
sysctl -a hw
```

Do not paste a chip’s numeric dump into a header and call it portable.
