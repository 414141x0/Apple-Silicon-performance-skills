<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->
# Pattern: unaligned large-array access

## When to apply

Apply when a hot loop streams a **large array** with a load or store
width that does not match the address alignment (Guide recs 42–43).
Occasional unaligned hits are cheap. A memcpy-shaped stream of them is
not: every 16 B store that crosses a 16 B boundary uses two write
ports, and every Nth load that crosses a 64 B boundary can serialize
two misses.

If only one side of a copy or a pipeline can be aligned, **align the
stores** and let the loads be unaligned (rec 43, Medium / Low).

### Source code signals

- `float *` / `uint8_t *` buffers from `malloc` used with `vld1q_*` /
  `vst1q_*` without a 16-byte guarantee
- `memcpy`-like loops over `src + 1` or packed structs with odd
  `offsetof`
- Pointers advanced by 3, 5, 6, 12, … bytes in an inner loop
- SME/SSVE kernels storing through Z/ZA into a buffer that is only
  8-byte aligned (see `sme-engine-bounce.md` for engine LSU needs)
- `packed` structs dumped as 16 B stores

### Profiling signals

- CPU Counters: high density of 64 B-line-crossing or page-crossing
  load/store µops relative to all LD/ST
- CPU Bottlenecks memory-bound on a stream that “should” be L1/LL
  bandwidth
- A hand `memcpy` slower than libc on unaligned dest, fine on aligned
  dest
- Engine path: unaligned **stores** from SM/ZA mode, often with
  Shared LL RMW

---

## Why this is slow

These cores execute unaligned **loads** of cacheable memory at full
speed when they do not cross a 64 B boundary. A load that *does*
cross, and misses on both sides, is handled **serially** — worst case
about 2× a normal miss. A load that crosses a **virtual page** waits
until it is the oldest load.

Unaligned **stores** that cross a **16 B** boundary consume two write
ports. A stream of them can run at half the aligned store rate. Page-
spanning stores also wait until they are oldest.

A 16 B streaming copy with both ends unaligned therefore hits *every*
store (16 B crossing) and every fourth load (64 B crossing), plus
occasional 16 KiB page spans. That is why libc `memcpy` cares about
alignment and why you should call it (`platform-libs-first.md`) instead
of a naive `ld1`/`st1` loop.

---

## The fix

### 1. Align the buffers to the access width

| Path | Align to |
|------|----------|
| Scalar / libc | natural type width |
| Core ASIMD (`vld1q` / `vst1q`, 16 B) | **16 bytes** |
| SSVE (`LD1` Z, SVL) | **SVL** — 64 B on current Apple silicon; do not hardcode SVL in portable code, query/ISA (`sysctl-hardcoding.md`) |
| SME engine LSU | engine needs + **4 KiB isolation** from core data — `sme-engine-bounce.md` |

```c
#include <stdlib.h>
#include <stddef.h>

enum { ASIMD_ALIGN = 16 };

float *alloc_f32_asimd(size_t n)
{
    void *p = NULL;
    if (posix_memalign(&p, ASIMD_ALIGN, n * sizeof(float)) != 0)
        return NULL;
    return p;
}

/* C11 equivalent: size must be a multiple of alignment */
float *alloc_f32_asimd_c11(size_t n)
{
    size_t bytes = n * sizeof(float);
    bytes = (bytes + ASIMD_ALIGN - 1) & ~(size_t)(ASIMD_ALIGN - 1);
    return aligned_alloc(ASIMD_ALIGN, bytes);
}
```

Over-aligned `malloc` on Darwin is **not** guaranteed 16 B for small
sizes. Use `posix_memalign` / `aligned_alloc`.

### 2. If unaligned is unavoidable, unaligned **loads** beat unaligned stores

**Before** (unaligned dest, aligned src — the expensive choice):

```c
void copy_16(const uint8_t *src, uint8_t *dst, size_t n)
{
    /* dst is 1-byte aligned; every vst1q crosses 16 B */
    for (size_t i = 0; i + 16 <= n; i += 16) {
        uint8x16_t v = vld1q_u8(src + i);
        vst1q_u8(dst + i, v);
    }
}
```

**After** — peel until **destination** is 16 B aligned; src may stay
unaligned:

```c
#include <arm_neon.h>
#include <stdint.h>
#include <string.h>

void copy_16(const uint8_t *src, uint8_t *dst, size_t n)
{
    size_t i = 0;
    while (i < n && ((uintptr_t)(dst + i) & 15u)) {
        dst[i] = src[i];               /* scalar peel to align stores */
        i++;
    }
    for (; i + 16 <= n; i += 16) {
        uint8x16_t v = vld1q_u8(src + i); /* load may be unaligned */
        vst1q_u8(dst + i, v);             /* store is aligned */
    }
    for (; i < n; i++)
        dst[i] = src[i];
}
```

Better still: `memcpy(dst, src, n)` and let libc peel.

### 3. Layout: packed arrays of the width you actually use

Do not store `float` at offset 2 in a packed struct and then stream
it with `vld1q_f32`. Either pad the struct or load with `vld1q_u8` +
permute once into aligned working storage.

For 2D arrays, pad the row stride to 16 B (ASIMD) / SVL (SSVE) so
column walks do not also destroy store alignment. Engine column walks
need Shared LL bank padding — that is `sme-engine-bounce.md`, not a
16 B tweak.

---

## Verification

1. **Assert alignment** at the kernel entry:
   `assert(((uintptr_t)p & 15) == 0)` for ASIMD; SVL for SSVE.
2. **Counters** — line-crossing and page-crossing LD/ST density drop
   on the stream. CPU Bottlenecks memory bound should ease if
   alignment was the limiter.
3. **Time** aligned vs deliberately misaligned dest on a large `n`.
   If they match, this pattern was not the bottleneck.
4. Engine kernels: confirm the store addresses meet the engine LSU
   guidance in the codegen SME refs; unaligned engine **stores** are
   the first thing to delete.

---

## Presenting this to the user

1. Show the 16 B load/store loop and the actual alignment of `src` /
   `dst` (`/* ◄ dest & 15 == 1 */`).
2. Rec 42: stop streaming unaligned large arrays. Rec 43: if one
   side must be unaligned, make it the **load**.
3. Show `posix_memalign` / `aligned_alloc` and the dest-aligning peel.
4. Offer `memcpy` first (`platform-libs-first.md`). Point at
   `sme-engine-bounce.md` for ZA/Z stores and at
   `store-forwarding.md` if the unaligned access is also a
   store-then-wide-load.
