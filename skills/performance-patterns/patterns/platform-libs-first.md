<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->
# Pattern: platform libraries first (Accelerate, Compression, memcpy, GCD)

## When to apply

Apply **before writing a single ASIMD intrinsic, SSVE/SME kernel, or
homegrown memcpy/gzip**. Recs 2, 41, 49 are **High / High**.
Apple ships tuned implementations that already use ASIMD, SME, NT
stores, and the right allocators. A hand NEON dot product is almost
always a regression against `vDSP_dotpr`.

Agents must run this checklist **before** `fused-mac.md`,
`parallel-accumulator.md`, `sme-engine-bounce.md`, or any codegen
kernel.

### Source code signals

- A handwritten loop that is a dot product, FIR, FFT, GEMM, conv,
  softmax, L2 norm, image convert, or transcendental over arrays
- `for (...) dst[i] = src[i];` / element-wise copy instead of `memcpy`
- Homemade zlib/gzip/lzfse/lz4, or a bundled copy of those libraries
  compiled without Apple’s SIMD
- `malloc` + a custom bump allocator “for speed” or “for MTE”
- A thread pool built to run the above, instead of GCD
- `#include <arm_neon.h>` around a kernel whose name is `dot`, `gemm`,
  `fft`, `conv`, `crc`, `compress`

### Profiling signals

- CPU Profiler: the hotspot is a user `dot`/`gemm`/`copy`/`deflate`
  rather than `vDSP_*`, `cblas_*`, `BNNS*`, `compression_*`, `_platform_memmove`
- The binary links Accelerate but never calls it
- Instruments Allocations: a custom allocator next to MTE faults, or
  sparse layouts with high L1 tag-miss style symptoms on M5

---

## Why this is slow

The platform libraries are maintained per chip family. They already:

- Pick ASIMD vs SME vs scalar at runtime. From M4, Accelerate GEMM uses
  SME. On earlier chips it uses **private matrix hardware you cannot
  emit** — still call the library; do not invent a matrix ISA.
- Align, prefetch, and NT-store where it pays
- Fuse `FMLA` / outer products
- Use the Apple allocator’s MTE policy and dense layouts (rec 49)
- Expose thread-safe, QoS-aware entry points (GCD)

**Metal is GPU**, not a substitute for CPU ASIMD/SME. Core Image / Core
Media / Image I/O / Create ML own image and media pipelines. Do not
rewrite those as `float32x4_t` loops.

A custom NEON kernel has to rediscover all of that, then keep up when
M4→M5 changes the engine. Rec 2 is High/High specifically to stop that
work before it starts.

`memcpy` / `memmove` / `memset` are the data-movement analogue (rec 41).
A 16-byte `ld1`/`st1` loop loses on overlap, NT, and alignment cases
the libc already handles.

---

## The fix

Call the platform function. Write intrinsics only when (a) you have
timed both, and (b) the platform has no entry point for *this* shape.

### 1. Dot product → `vDSP_dotpr`

**Before:**

```c
float dot(const float *a, const float *b, int n)
{
    float s = 0.0f;
    for (int i = 0; i < n; i++)
        s += a[i] * b[i];
    return s;
}
```

**After:**

```c
#include <Accelerate/Accelerate.h>

float dot(const float *a, const float *b, int n)
{
    float s = 0.0f;
    vDSP_dotpr(a, 1, b, 1, &s, (vDSP_Length)n);
    return s;
}
```

Double: `vDSP_dotprD`. Strided inputs are the second/fourth arguments.

### 2. GEMM / BLAS → `cblas_*`

```c
#include <Accelerate/Accelerate.h>

void gemm_nn(int M, int N, int K,
             const float *A, const float *B, float *C)
{
    cblas_sgemm(CblasRowMajor, CblasNoTrans, CblasNoTrans,
                M, N, K, 1.0f, A, K, B, N, 0.0f, C, N);
}
```

Sparse / LAPACK: same header. Do not start an SME outer-product kernel
until `cblas_sgemm` has been timed on this shape.

### 3. Element-wise math → vForce; pixels → vImage; nets → BNNS

```c
#include <Accelerate/Accelerate.h>

void rsqrt_f(const float *in, float *out, int n)
{
    int nn = n;
    vvrsqrtf(out, in, &nn);            /* vForce */
}

void scale_planar_f(const float *in, float *out, int n, float k)
{
    vDSP_vsmul(in, 1, &k, out, 1, (vDSP_Length)n);
}
```

Small 2×2 / 4×4 / 4-lane geometry: `<simd/simd.h>` (`simd_float4`,
`simd_muladd`). Image buffers: vImage. Convolutions / fully-connected:
BNNS.

### 4. Copy / fill → `memcpy` / `memset`

```c
#include <string.h>

void copy_f(const float *src, float *dst, int n)
{
    memcpy(dst, src, (size_t)n * sizeof(float));
}
```

Do not write a NEON copy “because memcpy is scalar”. It is not.

### 5. Compression → Compression.framework (and Apple Archive)

**Before:** a vendored `deflate()` inner loop.

**After:**

```c
#include <compression.h>
#include <stddef.h>

size_t zlib_deflate_buf(const uint8_t *src, size_t src_n,
                        uint8_t *dst, size_t dst_n)
{
    return compression_encode_buffer(dst, dst_n, src, src_n,
                                     NULL, COMPRESSION_ZLIB);
}

size_t zlib_inflate_buf(const uint8_t *src, size_t src_n,
                        uint8_t *dst, size_t dst_n)
{
    return compression_decode_buffer(dst, dst_n, src, src_n,
                                     NULL, COMPRESSION_ZLIB);
}
```

Also `COMPRESSION_LZFSE`, `COMPRESSION_LZ4`, `COMPRESSION_LZMA`.
Multi-file archives: Apple Archive. Streamed / very large buffers:
`compression_stream_*`.

### 6. Threads / allocators

- Work partition: GCD (`amp-work-partition.md`), not a custom pool.
- Allocation: `malloc` / `posix_memalign` / `aligned_alloc` from
  libSystem. Rec 49: **do not write a custom allocator**; keep layouts
  dense for MTE.

### Order of operations for an agent

1. Identify the algorithm (dot, GEMM, FFT, copy, compress, …).
2. Find the Accelerate / simd / Compression / libc entry point.
3. Time it against the current code (`mach_absolute_time` /
   `clock_gettime_nsec_np`).
4. Only if it loses **and** the shape is truly uncovered, open
   `apple-silicon-codegen` and the SIMD patterns.

---

## Verification

1. **Time both** on representative sizes, including small-N (where
   call overhead might win for the hand loop) and large-N (where
   Accelerate should win by a lot).
2. **Numerics** — vDSP/BLAS may fuse and reorder. Compare with an ε,
   not bit-identity. Do not pass `-Ofast` to “make the hand loop
   match”.
3. **Instruments** — the hotspot should move into
   `vDSP_*` / `cblas_*` / `compression_*` / `_platform_memmove`.
4. **Link** — `-framework Accelerate` and, for Compression,
   `-framework Compression`.

---

## Presenting this to the user

1. Name the algorithm and the platform call in one sentence:
   *“This is a dot product; call `vDSP_dotpr` before writing `vfmaq`.”*
2. Show the 5–10 line replacement next to the loop.
3. State that this is High/High — custom NEON is the fallback, not
   the start.
4. Show the before/after times. If Accelerate loses on a weird shape,
   *then* propose a kernel and `apple-silicon-codegen`.
5. For copies and compression, refuse a hand rewrite unless the user
   has a measured gap.
