<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->

# Pattern: Library version upgrade

## When to apply

A function from a **system dylib / framework** (not owned by the application)
appears in the hot path of a CPU Profiler / `sample` profile, AND that
symbol appears in the table in `references/library-versions.md`.

This is a low-effort performance win: the optimized code already exists and
is production-tested — the only question is whether the process is linking
the OS copy (and a new enough OS / SDK) rather than a bundled substitute.

If the hot symbol is *homemade* and Accelerate / libSystem would do the
same job, that is `patterns/platform-libs-first.md`, not this pattern.

## Why it matters

Darwin system libraries accumulate chip-specific paths (ASIMD, SME on
M4+, private matrix hardware you cannot emit on earlier chips, tuned
`memcpy`) that are invisible to application code.
A bundled OpenBLAS, FFTW, zlib, or an old OpenSSL `.dylib` can silently
run a generic kernel on hardware that already has a faster one in
Accelerate / libSystem / Compression / CommonCrypto.

## Detection

### Step 1 — Identify the hot library symbol

From CPU Profiler or `sample`, find symbols whose binary / dylib column
is a system path rather than the application:

```
  12.3%  myapp  /usr/lib/libBLAS.dylib  cblas_sgemm
  8.1%   myapp  libopenblas.0.dylib     sgemm_nn
```

Note both the symbol and the dylib. `otool -L` on the process binary
shows what was actually linked:

```bash
otool -L ./myapp
dyld_info -dependents ./myapp
```

### Step 2 — Look up the symbol

Read `references/library-versions.md` and check whether the symbol (or
its platform equivalent) appears in the version table.

- **Match found** → proceed to Step 3.
- **No match** → this pattern does not apply; continue with other analysis.

### Step 3 — Detect what is installed / linked

Use the detection primitives in `references/library-versions.md`. Try, in
order:

1. `otool -L` / `dyld_info -dependents` — is the OS framework linked, or a
   bundled `.dylib`?
2. `sw_vers -productVersion` and `xcrun --show-sdk-version` — OS / SDK
   floor for the framework's current implementation.
3. Framework-specific version APIs listed in that file.

Do **not** use `ldconfig`, `readlink -f`, or `ldd`.

### Step 4 — Compare and report

Report to the user:

**When a bundled / old path is in use:**

```
Library version gap detected
────────────────────────────
Hot symbol : sgemm_nn  (libopenblas.0.dylib)
Platform   : Accelerate cblas_sgemm / BLAS
Linked     : bundled OpenBLAS
OS / SDK   : macOS 15.x  (SDK 15.x)
Reason     : OS Accelerate uses SME (M4+) or private matrix hardware
             you cannot emit (earlier chips); the bundled BLAS does not.
```

**When the OS library is already current:**

```
Library version check: OK
Hot symbol : cblas_sgemm  (Accelerate)
OS / SDK   : macOS 15.x
The installed framework already includes the known chip-tuned path.
If this symbol is still hot, the bottleneck is elsewhere — continue
with apple-silicon-profile inside the caller (working-set, AMP, …).
```

## Presenting this to the user

- State the gap precisely (bundled vs OS, OS/SDK vs suggested floor),
  not just "update."
- Include the reason so the user understands which implementation they
  are missing (SME / platform GEMM, tuned memcpy, Compression, …).
- Do not prescribe *how* to upgrade — Homebrew vs. Xcode vs. dropping a
  submodule depends on their environment.
- If the OS library already meets the floor, say so and redirect to
  further profiling.
- If they are not linking the platform library at all, switch to
  `patterns/platform-libs-first.md`.
