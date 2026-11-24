<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Fast CRC32C — implementation guide

## Library files

Two C files live in `library/`. Pick based on the project's target and
portability requirements.

| File | Requires | Typical speed | When to use |
|------|----------|--------------|-------------|
| `crc32c_arm64.c` | CRC32 (`hw.optional.armv8_crc32`, all M1 / A14+) | multi-GB/s, 3 independent CRC32CX streams | Default on Apple silicon |
| `crc32c_portable.c` | None — C99 table | ~1–3 GB/s | Fallback: Rosetta, or a build without CRC32 |

`crc32c_arm64.c` uses `__crc32cd` / `__crc32cw` / `__crc32ch` / `__crc32cb`
from `<arm_acle.h>`. Three sequential chunks run in parallel, then combine
in GF(2) with the reflected Castagnoli polynomial `0x82F63B78`. A 64-byte
PMULL fold (`vmull_p64`, analogue of a carry-less fold) can be added later;
correctness of the 3-accumulator loop comes first.

If a generic checksum is enough, do not ship this kernel —
Compression.framework / zlib in libSystem first.
→ `patterns/platform-libs-first.md`

---

## Dispatch wrapper

Add this to a `.c` file in the project (e.g. `crc32c.c`). Include the
`library/crc32c_*.c` files you ship. Detection uses
`library/cpu-dispatch.md` (`apple_feat`, `apple_is_translated`).

```c
/* CRC32C dispatch wrapper.
 * MIT license. Original Apple Silicon / AArch64 example.
 */

#include <stddef.h>
#include <stdint.h>

extern uint32_t crc32c_arm64_impl(uint32_t crc, const char *buf, size_t len);
extern uint32_t crc32c_portable_impl(uint32_t crc, const char *buf, size_t len);

typedef uint32_t (*crc32c_fn)(uint32_t, const char *, size_t);

static crc32c_fn resolve_crc32c(void)
{
    if (apple_is_translated())
        return crc32c_portable_impl;
    /* CRC32 is present on every M1 / A14+; still query — do not hardcode. */
    if (apple_feat("armv8_crc32") || apple_feat("FEAT_CRC32"))
        return crc32c_arm64_impl;
    return crc32c_portable_impl;
}

uint32_t crc32c(uint32_t crc, const void *buf, size_t len)
{
    static crc32c_fn impl;
    if (!impl)
        impl = resolve_crc32c();
    return impl(crc, (const char *)buf, len);
}
```

`apple_feat("armv8_crc32")` reads `hw.optional.armv8_crc32` (legacy name
in `apple-silicon-codegen/references/sysctl.md`). There is no `FEAT_CRC32` sysctl on all OS versions; the
helper treats a missing key as false and falls through to portable.

### Adapting to an existing API

| Existing signature | Notes |
|---|---|
| `uint32_t crc32c(const void *buf, size_t len)` | Pass `crc = 0` to the impl |
| `uint32_t crc32c(uint32_t seed, const uint8_t *buf, size_t len)` | Cast buf to `const char *` |
| `uint64_t crc32c_hash(const void *data, size_t len, uint64_t seed)` | Narrow seed to `uint32_t`, return zero-extended |

---

## Compile flags

```make
crc32c_arm64.o: crc32c_arm64.c
	$(CC) $(CFLAGS) -O2 -mcpu=apple-m1 -c $< -o $@

crc32c_portable.o: crc32c_portable.c
	$(CC) $(CFLAGS) -O2 -c $< -o $@
```

`-mcpu=apple-m1` (or m2/m3/m4/m5) enables CRC32 encodings. The dispatch
wrapper must still run the portable path on Rosetta and on a CPU that
fails the sysctl. Alternatively, compile `crc32c_arm64.c` with
`__attribute__((target("crc")))` on the impl function and a lower file
baseline.

Self-test (optional):

```bash
clang -O2 -mcpu=apple-m1 -DCRC32C_SELFTEST \
    library/crc32c_arm64.c library/crc32c_portable.c -o crc32c_test
./crc32c_test
```

Known vectors are in the source comments:
`crc32c("", 0) == 0`, `crc32c("123456789", 9) == 0xE3069283`.

---

## PMULL fold (optional follow-up)

`FEAT_PMULL` is present on every M1 / A14+. A 64-byte fold with
`vmull_p64` / `pmull` is the ARM analogue of a carry-less 128-bit fold
and can raise throughput further. Constants are easy to get wrong; do
**not** ship a fold until it matches the portable implementation on:

- empty buffer, length 1..128, unaligned pointers
- the `"123456789"` vector
- split-buffer combine vs one-shot

Until then, the 3-accumulator CRC32CX loop is the production path.
