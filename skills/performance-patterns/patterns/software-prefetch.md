<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->
# Pattern: software prefetch last (PRFM sparingly)

## When to apply

Apply when a memory-bound kernel has **`PRFM` / `__builtin_prefetch`
sprinkled through the inner loop**, or when someone wants to add them
before the access pattern is sequential or blocked (recs 47–48).

The order is:

1. Recode the **layout and walk** so the **hardware prefetcher** can
   see a stream (sequential, or a 2D block that is sequential inside
   the tile).
2. Profile. Insert `PRFM` only if **Critical L1D miss while executing**
   is still the limiter.
3. One prefetch per cache line, tuned, and compared against a no-PRFM
   build. Remove it if it does not win on **both** P/S and E cores.

### Source code signals

- `__builtin_prefetch(p + i + K)` on every iteration of a loop that
  already walks `p[i], p[i+1], …`
- `PRFM PLDL1KEEP` (or `PLD`) copied from an AArch64 blog into every
  hot loop
- Prefetch distance `#define PF 8` used on both P and E cores
- Gather / pointer-chasing with a prefetch of `p->next` as the *first*
  attempted fix, before SOA layout or blocking
- Prefetch of every `float` rather than once per 128 B line
  (`hw.cachelinesize`)

### Profiling signals

- CPU Bottlenecks: **Critical L1D miss** *or* Delivery/Processing bound
  with extra load-unit traffic from the prefetches themselves
- Hardware prefetcher would have covered the stream: sequential
  addresses, dense rows — yet `PRFM` is present
- P-core win, E-core loss (or the reverse): the distance is tuned to
  one core type
- Load-unit issue slots saturated; a chunk of them are `PRFM`

No Critical L1D miss in the bottleneck view → **do not** add `PRFM`.
That is not this pattern’s fix; look at algorithm, fusion, AMP.

---

## Why this is slow

The hardware prefetcher watches demand misses and issues ahead on
spare cycles. It retunes for **this core type** and for this chip’s
latencies. S/P cores often want data many lines ahead; E cores want
less. Software prefetch cannot make that adjustment.

`PRFM` is a real load-class op: it occupies decode, ROB, and a load
issue slot, and it is **mandatory** even when the memory system is
already saturated. It also **does not train** the hardware prefetcher,
so a loop whose only misses are software-prefetched can *hide* the
pattern the HW prefetcher needed.

Hence rec 47 (Medium / Low): use software prefetch **sparingly**,
after the layout is friendly. Rec 48: recode the access pattern
first.

---

## The fix

### 1. Recode for the hardware prefetcher

**Before** (column walk of a row-major matrix — hostile, then papered
over with `PRFM`):

```c
void col_sum(const float *A, int n, int lda, float *out)
{
    for (int j = 0; j < n; j++) {
        float s = 0.0f;
        for (int i = 0; i < n; i++) {
            __builtin_prefetch(&A[(i + 8) * lda + j]);  /* does not fix stride */
            s += A[i * lda + j];
        }
        out[j] = s;
    }
}
```

**After** — sequential walk, or a 2D block whose inner index is
unit-stride:

```c
void col_sum_blocked(const float *A, int n, int lda, float *out)
{
    for (int j = 0; j < n; j++)
        out[j] = 0.0f;
    const int BS = 64;                 /* pick from cache query, not magic */
    for (int ii = 0; ii < n; ii += BS) {
        int i1 = ii + BS < n ? ii + BS : n;
        for (int j = 0; j < n; j++) {
            float s = out[j];
            for (int i = ii; i < i1; i++)
                s += A[i * lda + j];   /* still strided; see next block */
            out[j] = s;
        }
    }
}
```

Better: **transpose or tile so the inner loop is `A[i * lda + k]`
with `k` consecutive**, pack a panel into a sequential scratch, or
call `cblas` / vDSP (`platform-libs-first.md`). Pad `lda` so a
necessary column walk at least hits distinct Shared LL banks
(`sme-engine-bounce.md` for engine; same idea on the core).

Pointer chasing: convert to SOA / array-of-ids with a linear scan
before considering prefetch of `node->next`.

### 2. Only then, and only on a measured Critical L1D miss, add PRFM

```c
#include <arm_neon.h>

void axpy_pf(const float * restrict x, float * restrict y,
             float a, int n)
{
    int i = 0;
    /* Distance is in *cache lines*, not elements. Query
       hw.cachelinesize (128). One PRFM per line. */
    enum { LINE = 128 };
    enum { ELEMS = LINE / (int)sizeof(float) };

    for (; i + ELEMS < n; i += ELEMS) {
        /* Optional: only if Instruments still shows Critical L1D miss. */
        __builtin_prefetch(x + i + 4 * ELEMS, 0, 3);  /* read, high temporal */
        __builtin_prefetch(y + i + 4 * ELEMS, 1, 3);  /* write */
        for (int k = 0; k < ELEMS; k++)
            y[i + k] += a * x[i + k];
    }
    for (; i < n; i++)
        y[i] += a * x[i];
}
```

Rules of thumb (not a table of chip latencies):

- **One** `PRFM` per line (`hw.cachelinesize`), not per element.
- Pick load vs store, L1 vs LL, temporal vs streaming to match the
  demand op. The ISA names are `PLDL1KEEP` / `PLDL1STRM` / `PSTL1*` /
  `PLDL2*` (`PRFM` vs `PRFUM` only changes addressing).
- Do not prefetch into “L2” expecting the private L2; the L2 encoding
  targets the **Shared LL Cache**. You **cannot target PL2**.
- Do **not mix two miss-streams in the same 16 KiB page**. Adjacent 2D
  rows look like one irregular stream. Pair far-apart rows, or rearrange
  into one linear stream, before adding `PRFM`.
- Engine streams: HW prefetch fills Shared LL only (`sme-engine-bounce.md`).
- `PRFM` does not fault — safe past the end of an array — so do not
  add a bounds branch “to protect” it.
- Time **with and without**. Keep the without path.

If the same binary must run on E and P cores, a distance that is right
for P is often too aggressive for E. That is a reason to **delete**
the `PRFM` and keep the HW prefetcher.

### 3. Do not sprinkle

A `PRFM` in every loop in a file is a failed review. Prefetch is a
surgical response to a named Critical L1D miss on a named address
stream.

---

## Verification

1. **CPU Bottlenecks** — Critical L1D miss while executing, before any
   `PRFM`. If that counter is quiet, stop.
2. **A/B** — same binary flags, with and without the prefetch. Wall-
   clock on P-heavy and on E-heavy QoS (`amp-work-partition.md`). Both
   must not regress.
3. **Load-unit** — `PRFM` density should be ~1 per 128 B of the stream,
   not 1 per element.
4. **Layout diff** — the commit that recodes sequential/2D-block
   access should stand on its own and usually is the win. `PRFM` is a
   follow-up commit or it does not land.

---

## Presenting this to the user

1. If they already have `PRFM` in a sequential loop, propose
   **deleting** it first and re-profiling.
2. If the walk is strided / pointer-chasing, show the sequential or
   tiled rewrite. Rec 48 before rec 47.
3. Only after Critical L1D miss remains, show a single-prefetch-per-
   line sketch and insist on an A/B.
4. Do not quote a “prefetch 16 lines ahead” number from a blog. The
   hardware prefetcher exists because that number is core-type
   dependent; `apple-silicon-codegen/references/latency-lookup.md`
   (rec 47) for this chip if you must tune one.
