<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->

# Pattern: Fast CRC32C

## When to apply

### From profiling output
- `CRC32B` / `CRC32CX` / `CRC32CW` with a **single** accumulator dominates
  a hot function in CPU Profiler
- A function named `crc32c` / `crc32_c` / `compute_crc32c` is prominent
- A byte-table CRC is hot (portable fallback running on a machine that
  has CRC32)

### From source code review
- A loop with a single `__crc32cd` / `__crc32cw` accumulator
- A byte-by-byte or word-by-word table-lookup CRC32C loop
- Any function whose name strongly suggests CRC32C (`crc32c`,
  `calc_crc32c`, `hash_crc32c`) — the name alone is enough to inspect

### When writing new code
- You need CRC32C (Castagnoli, iSCSI RFC 3720). Do not write a new one —
  use this pattern. If any checksum will do, Compression.framework /
  zlib in libSystem first (`patterns/platform-libs-first.md`). zlib's
  `crc32()` is a **different polynomial**.

---

## Why naive implementations are slow

A single `CRC32CX` (`__crc32cd`) has ~3-cycle latency (Appendix A) and
processes 8 bytes per instruction. One accumulator is limited to
roughly 8 bytes / 3 cycles regardless of memory bandwidth.

A 256-entry byte table is worse: ~1–3 GB/s, one byte per dependent load.

Three independent CRC32CX streams hide that latency; a PMULL
(`vmull_p64`) fold of 64-byte blocks is the further step when someone
adds it. Correctness first: the library ships a 3-accumulator CRC32CX
loop plus the portable table.

**Polynomial.** CRC32C is Castagnoli `0x1EDC6F41` (reflected
`0x82F63B78`) — the same polynomial as `library/crc32c_portable.c`.
Do not mix with IEEE CRC-32 (`0x04C11DB7` / reflected `0xEDB88320`).

---

## The fix

1. If a generic checksum is enough: Compression.framework or zlib in
   libSystem. → `patterns/platform-libs-first.md`
2. If CRC32C is required: read `patterns/fast-crc32c-impl.md`. It
   provides `library/crc32c_arm64.c` (CRC32CX, 3 streams),
   `library/crc32c_portable.c` (table fallback), and a function-pointer
   dispatch wrapper (`apple_feat` / `hw.optional.armv8_crc32`).

---

## Verification

### Correctness

```
crc32c(0, "", 0)              == 0x00000000
crc32c(0, "123456789", 9)     == 0xE3069283
```

Split-buffer combine of the same bytes must match a one-shot CRC.

### Performance

```c
uint32_t crc = 0;
for (int i = 0; i < 10000; i++)
    crc = crc32c(crc, buf, buf_len);
```

Time with `clock_gettime_nsec_np(CLOCK_UPTIME_RAW)`. Divide total bytes
by elapsed time to get GB/s. Expect the ARM path well above the portable
~1–3 GB/s table.

---

## Presenting this to the user

- Report before/after throughput in GB/s if measurable.
- Note that CRC32C is a **standard** checksum (iSCSI RFC 3720) — this
  implementation does not change the checksum value.
- Point at `library/crc32c_arm64.c` as the Apple silicon path and
  `crc32c_portable.c` as the ISA-neutral fallback (Rosetta, or a build
  without CRC32).
- If they did not need Castagnoli specifically, offer Compression /
  libSystem instead.
