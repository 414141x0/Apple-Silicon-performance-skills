<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->

# Library version reference

This file is loaded by `patterns/library-version-upgrade.md` when a hot
symbol from a system dylib / framework has been identified in profiling
output.

Each row maps a symbol (as it appears in CPU Profiler / `sample` /
`nm`) to the Darwin library that should own it, how to detect the
linked copy, and why the OS path is faster. When a symbol matches,
detect the installed / linked version using the primitives at the
bottom of this file and report the gap.

Accelerate, vDSP, and BLAS implementations track **OS version**, not a
`.so` soname. A bundled OpenBLAS on macOS 15 is still a gap.

---

## Version table

| Symbol (as seen in a profile) | Dylib / framework | Library | Suggested path | Reason |
|-------------------------------|-------------------|---------|----------------|--------|
| `cblas_sgemm`, `cblas_dgemm`, `sgemm_`, `dgemm_` | `Accelerate.framework` / `libBLAS.dylib` | Accelerate BLAS | OS Accelerate, not bundled OpenBLAS/BLIS | Chip-tuned GEMM: ASIMD, SME on M4+, private matrix hardware you cannot emit on earlier chips |
| `vDSP_dotpr`, `vDSP_mmul`, `vDSP_fft*`, `vDSP_vsort` | `Accelerate.framework` | vDSP | OS Accelerate | ASIMD / SME internally; do not ship a homemade FFT/dot |
| `BNNSFilterApply`, `BNNSGraph` | `Accelerate.framework` | BNNS | OS Accelerate | Neural paths maintained per OS |
| `memcpy`, `memmove`, `memset` | `libsystem_platform.dylib` / `libSystem.B.dylib` | libSystem | OS `memcpy` | Chip-tuned; homemade SIMD copies lose |
| `compression_encode_buffer`, `compression_decode_buffer` | `Compression.framework` | Compression | OS Compression | LZFSE / LZ4 / ZLIB / LZMA, maintained per chip |
| `AAArchiveStreamCreate`, `AAByteStream` | `AppleArchive.framework` | Apple Archive | OS Apple Archive | Tuned archive + compression |
| `cccrc32`, `crc32` (zlib) | `libSystem` / `libz.dylib` | libSystem zlib | OS zlib if IEEE CRC-32 is enough | Not CRC32C (Castagnoli) — different polynomial |
| `CCCrypt`, `CC_SHA256`, AES | `libcommonCrypto` / `Security.framework` | CommonCrypto | OS CommonCrypto, not an old OpenSSL `.dylib` | Hardware AES / SHA |
| `vImage*` | `Accelerate.framework` | vImage | OS Accelerate | Image pipelines with ASIMD |

If the hot symbol is homemade (`dot_neon`, `my_fft`) and the platform
equivalent is **not linked**, switch to
`patterns/platform-libs-first.md`.

---

## Version detection primitives

When a symbol matches a row above, detect in this order. Do **not** use
`ldconfig`, `ldd`, or `readlink -f`.

### 1. What did we actually link?

```bash
otool -L ./myapp
dyld_info -dependents ./myapp
```

OS copies live under `/System/Library/Frameworks/`,
`/System/Library/PrivateFrameworks/`, or `/usr/lib/`. Bundled copies
show as `@rpath/libopenblas.dylib`, `@executable_path/…`, Homebrew
prefixes, etc.

To see which slice ran:

```bash
otool -arch arm64 -L ./myapp
sysctl sysctl.proc_translated   # 1 = Rosetta; refuse custom ISA
```

### 2. OS / SDK floor

```bash
sw_vers -productVersion
xcrun --sdk macosx --show-sdk-version
```

Accelerate / Compression / libSystem ship with the OS. There is no
useful soname bump. Report **macOS version + SDK** next to the symbol.
A gap is: bundled third-party lib on a machine whose OS already has
the platform equivalent, or an SDK older than the APIs you want
(BNNS graph, Apple Archive, …).

### 3. Framework-specific

```bash
# Accelerate is a framework, not a versioned .so
ls /System/Library/Frameworks/Accelerate.framework
nm -gU /System/Library/Frameworks/Accelerate.framework/Accelerate | grep vDSP_dotpr

# Compression
otool -L /usr/lib/libcompression.dylib

# libSystem memcpy
nm -gU /usr/lib/system/libsystem_platform.dylib | grep memcpy
```

### Reporting

State: hot symbol, dylib that actually ran, platform equivalent, OS/SDK,
and the reason from the table. Do not invent a "minimum Accelerate 14.5"
number — the OS *is* the version.
