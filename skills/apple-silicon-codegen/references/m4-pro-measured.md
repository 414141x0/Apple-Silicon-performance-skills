# Measured on one Apple M4 Pro (this machine, not general)

Home of **SKU wall-clock** on one Apple M4 Pro (Mac16,11). Book class
latencies and the unloaded hierarchy floor stay in `latency-lookup.md`
(DRAM **~95 ns**). Family *why* stays in `chip-families.md`. Rec text
stays in its rec home. This file does not replace those.

Do **not** copy these numbers onto M1/M2/M3/M5, onto a different M4
SKU, or from P onto E. E is not a slow P (`amp.md`). Time the chip
you ship.

## This machine

`sysctl` snapshot:

| Key | Value |
|-----|--------|
| brand / model | Apple M4 Pro / Mac16,11 |
| cores | 12 physical = 12 logical (no SMT) |
| page / line | 16384 B / 128 B |
| DRAM | 48 GiB |
| P `perflevel0` | 8 cpu, L1I 192 KiB, L1D 128 KiB, Shared LL 16 MiB / 4 cores |
| E `perflevel1` | 4 cpu, L1I 128 KiB, L1D 64 KiB, Shared LL 4 MiB / 4 cores |
| FEAT | SME=1 SME2=1 SME_F64F64=1 I8MM=1 BF16=1 AFP=1 WFxT=1 SSBS=0 MTE=0 |
| `sme_max_svl_b` | 64 |
| `proc_translated` | 0 |

Compile: `clang++ -O3 -mcpu=apple-m4 -std=c++20 -ffp-contract=fast
-fno-exceptions -fno-rtti -DACCELERATE_NEW_LAPACK -framework Accelerate`.
Time: `clock_gettime_nsec_np(CLOCK_UPTIME_RAW)`, **median**. QoS:
`QOS_CLASS_USER_INTERACTIVE` (P) vs `QOS_CLASS_BACKGROUND` (E), 80 ms
dummy so the scheduler can migrate. QoS is a hint, not a pin.

## Hierarchy — 128 B pointer chase (rec 35, book in latency-lookup)

ns/chase, 12e6 steps. P L1D 128 KiB holds through 128 KiB. E L1D 64 KiB
breaks between 64 and 128 KiB. P 16 MiB Shared LL / 4 cores already
shows 34 ns at 16 MiB. E 4 MiB Shared LL is DRAM-ish at 4 MiB.

| working set | P ns/chase | E ns/chase |
|-------------|------------|------------|
| 8 KiB | 0.694 | 2.08 |
| 32 KiB | 0.695 | 1.82 |
| 64 KiB | 0.709 | 2.50 |
| 128 KiB | 0.707 | 11.0 |
| 256 KiB | 6.14 | 11.8 |
| 1 MiB | 6.27 | 13.0 |
| 4 MiB | 7.54 | 109 |
| 16 MiB | 34.1 | 227 |
| 64 MiB | 115 | 236 |
| 256 MiB | 122 | 250 |

Loaded random DRAM chase on **this** SKU: ~122 ns P, ~250 ns E. Keep
the book **~95 ns** as the unloaded planning floor. P L1D ~0.70 ns/chase
is ~3.1 c at ~4.5 GHz vs the book L1D **~4 c** ready-to-complete — same
order, this load, this clock.

## Access pattern (rec 48)

P, 256 MiB `uint64` sum, ns/elem. Stride-16 is one 128 B line.

| access | ns/elem |
|--------|---------|
| stride-1 | 0.0932 |
| stride-2 | 0.265 |
| stride-8 | 0.683 |
| stride-16 (128 B) | 2.43 |
| stride-32 | 2.78 |
| stride-64 | 3.07 |
| stride-128 | 3.28 |
| random-gather, 1M into 256 MiB | 3.23 |

Unit stride is ~26× the line-stride walk.

## Layout (rec 38)

P ns/particle, 1.5M, hot floats in the loop.

| layout | ns/particle |
|--------|-------------|
| AoS-256B-fat | 5.97 |
| AoS-24B-compact | 0.626 |
| hot-cold-split | 0.624 |
| AoSoA-8 | 0.512 |
| SoA | 0.272 |

SoA ~22× vs fat AoS. Compact 24 B ≈ hot/cold. AoSoA-8 sits between.

## Class size (rec 38)

P ns/obj, 2M objects, read 8 hot bytes. Jump is at the **128 B
coherence line**, not 64 B.

| sizeof | ns/obj |
|--------|--------|
| 16 B | 0.247 |
| 32 B | 0.361 |
| 64 B | 0.660 |
| 128 B | 2.44 |
| 256 B | 2.84 |

## False sharing (rec 37)

40M increments × 2 threads, ns-total. 64 B pad still shares the 128 B
line.

| pad | P ns-total | E ns-total |
|-----|------------|------------|
| pad-64 (same 128 B line) | 102e6 | 89.9e6 |
| pad-128 (split lines) | 65.2e6 | 66.3e6 |

P 1.56×, E 1.36×.

## memcpy vs NT (recs 40–41)

16 MiB copy, P GB/s. Rec 41: call `memcpy`. The builtin does not
guarantee `STNP`.

| path | GB/s |
|------|------|
| element-loop | 116 |
| `__builtin_nontemporal_store` | 120 |
| memcpy | 135 |

## Hash / prefetch (recs 47–48)

P ns/lookup. Open addressing, inline keys, EMPTY=0. Swiss = 14 keys +
ctrl in one 128 B line. `sort-merge-presorted-table` keeps the table
sorted **outside** the timed region (a timing that sorted the 16M
table inside the lambda is discarded). E DRAM hash was not run.

| variant | L1-16K | LL-1M | DRAM-16M |
|---------|--------|-------|----------|
| oa-no-prefetch | 6.51 | 8.35 | 15.0 |
| oa-alt-pf16 | 6.63 (hurt) | 7.41 | 8.58 |
| oa-alt-pf32 | 7.05 (hurt) | 7.76 | 8.32 |
| oa-nano-pf16 | 6.82 | 7.49 | 13.5 |
| swiss128-no-pf | 16.0 | 19.1 | 56.1 |
| swiss128-alt-pf16 | 16.5 | 17.4 | 16.9 |
| oa-sorted-queries | 18.3 | 23.3 | 28.3 |
| sort-merge-presorted-table | 14.7 | 18.4 | 96.1 |
| binsearch-scalar | 11.2 | 55.8 | 275 |
| binsearch-4way-ilp | 10.2 | 43.2 | 178 |
| stl-unordered_set | 6.84 | 11.4 | — |

On this SKU: **do not PRFM an L1-resident table**. DRAM-sized OA +
alternating PRFM 16 or 32 is ~1.75–1.80× vs no-prefetch. Nano-threads
lose to alternating. 128 B Swiss groups lost to simple OA even after
prefetch. Rec 48 sort-merge of 200k queries lost to hash at this volume.
4-way ILP binary search beats scalar binary search and still loses to
hash.

## List / tree / alloc (recs 39, 49)

P.

| structure | n | ns/elem or ns/node |
|-----------|---|---------------------|
| list malloc-scattered | 3.2M | 18.8 |
| list unrolled-14-in-128B | 3.2M | 0.436 |
| list arena-packed | 3.2M | 0.270 |
| array (do not use a list) | 3.2M | 0.0644 |
| BST `new` scattered | 600k | 63.8 |
| BST arena level-order | 600k | 3.57 |
| vector no-reserve push | 4M | 0.368 |
| vector reserve push | 4M | 0.224 |
| malloc-then-fill | 4M | 0.0660 |

Unrolled-in-a-line list lost to a packed arena and to an array. Arena
BST ~18× scattered `new`.

## Polymorphic dispatch (rec 25)

P ns/call, 1.2M, four types. Sort-by-type uses a tag array (no RTTI;
this skill compiles `-fno-rtti`).

| variant | ns/call |
|---------|---------|
| virtual-shuffled | 4.84 |
| enum-switch | 1.21 |
| virtual-sorted-by-type | 0.680 |
| SOA-per-type | 0.396 |

Sort-by-type ~7.1× vs shuffled vtables. SOA-per-type still faster.

## ILP / width ladder (rec 15, parallel-accumulator) — P ≠ E

Dot product, ns/elem. `serial-1acc` was auto-vectorized by clang
(width 4, interleaved 4) — it is not a true 1-acc chain. Hand
`neon-4vec-FMLA` inner loop was “not vectorized” (already vector).

| variant | P L1-64KiB | P DRAM-32MiB | E L1-64KiB | E DRAM-32MiB |
|---------|------------|--------------|------------|--------------|
| serial-1acc (auto-vec) | 0.478 | 0.511 | 1.35 | 1.91 |
| scalar-4acc | 0.211 | 0.177 | 1.09 | 0.993 |
| neon-1vec | 0.234 | 0.200 | 0.743 | 0.739 |
| neon-4vec-FMLA | 0.0636 | 0.0630 | 0.506 | 0.586 |
| vDSP_dotpr | 0.0509 | 0.0642 | 0.511 | 0.593 |

On **P**, 4-acc scalar beats 1-vector NEON. On **E**, 1-vector NEON
beats 4-acc scalar. 4-vector FMLA ≈ `vDSP_dotpr` on both. Always time
both QoS classes. Rec 2 still comes first for a plain dot.

Unroll of a float sum, P 8M, ns/elem: clang-unroll-disable 0.229,
`#pragma unroll 8` 0.198, hand-4acc **0.107**. Independent accumulators
beat unroll count (rec 34).

## Vector constants (rec 13)

P ns/elem, 4M. 32 V-regs. Packing 16 constants into DUP-lanes did not
beat 16 `vdup`. The 6-constant kernel is less work, not a spill win.
Do not import AVX spill packing onto a file that is not spilling.

| variant | ns/elem |
|---------|---------|
| 16-vdup-constants | 0.184 |
| packed-lane-dup | 0.183 |
| 6-constants-only | 0.0607 |

## Image / GEMM (recs 2, 36)

2048² 3×3 box, P ns/px. `vImageBoxConvolve_Planar8` is **8-bit**; the
other three rows are float. Rec 36 tiling **hurt** this already-streaming
tiny stencil (extra loop overhead). Clang vectorized the scalar nest
(width 4).

| variant | ns/px |
|---------|-------|
| scalar-nested-9 | 0.281 |
| neon-row | 0.288 |
| tiled-64 | 0.458 (hurt) |
| vImageBoxConvolve_Planar8 | 0.0265 |

384³ `sgemm`, P GFLOP/s. Accelerate on M4 uses the SME grid; do not
re-emit it (rec 2).

| variant | GFLOP/s |
|---------|---------|
| naive-ijk | 4.69 |
| tiled-64 | 57.9 |
| cblas_sgemm | 1563 |

## Copy / substring

P. Substring: 8 MiB haystack of `a`, pattern `aaaaaaaaaaaz`. On this
pathological repeat the nested scan won; `memmem` and a head-8-in-regs
scan lost. Rec 2 still says try `memmem` first; this row is the honest
miss. `std::string::find` includes construction.

| copy 2M × 8 B | ns/elem |
|---------------|---------|
| pass-by-value | 0.198 |
| pass-by-cref | 0.0864 |
| ptr-plus-len | 0.0660 |

| substring | ns-total |
|-----------|----------|
| naive-nested | 9.08e6 |
| head8-in-regs | 10.1e6 |
| memmem | 12.0e6 |
| std::string::find | 46.9e6 |

## Agent takeaways (this SKU only)

- Rec 47: PRFM of L1-resident OA **hurt**. DRAM OA + alternating 16/32
  won. Nano-threads lost to alternating. One PRFM per 128 B Swiss group
  did not beat simple OA.
- Rec 48: unit stride is the hardware-prefetcher win. Sort-merge
  membership is not free at this query volume. Do not tile an already
  sequential 3×3 (rec 36 is for sets that miss).
- Rec 15: 4 FMLA vector accs. On P, 4-acc scalar already beats 1-vec;
  on E, 1-vec already beats 4-acc scalar. Time both.
- Rec 37: pad **128 B**. 64 B still shares.
- Rec 38: SoA / compact hot fields. Object size jump at 128 B.
- Rec 39/49: array ≫ packed arena ≫ unrolled-line list ≫ scattered `new`.
- Rec 25: sort-by-type ~7× vs shuffled virtual; SOA-per-type still faster.
- Rec 2/41: `cblas_sgemm`, `vDSP_dotpr`, `memcpy`, `vImage` first.
- Rec 13: 32 V-regs. Do not pack constants until a spill shows up.
- Clang auto-vec of a naive dot is real and is not the ceiling.
