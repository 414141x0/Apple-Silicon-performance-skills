<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Known algorithms — implementation details

Per-algorithm ISA levels, dispatch guards, and implementation notes.
Load this file **only after confirming** that a function name from
`references/known-algorithms.md` is present in the code or profile.

For runtime dispatch wiring, see `library/cpu-dispatch.md`. Check
Accelerate / vDSP before writing NEON (`patterns/platform-libs-first.md`).

---

## Cosine Similarity

**What it computes:** The cosine of the angle between two vectors:
`cos(θ) = dot(A, B) / (|A| * |B|)`, where `dot(A,B) = Σ a[i]*b[i]` and
`|A| = sqrt(Σ a[i]²)`. Returns 1.0 for identical direction, 0.0 for
orthogonal, −1.0 for opposite. Widely used in ML embeddings, NLP, and
recommendation systems.

**Why scalar is slow:** Three serial accumulator loops (dot product, two
norms) each have loop-carried FP dependencies. A naive implementation
makes three passes; a smart one makes a single pass but still serializes
on one accumulator.

**Key insight — single-pass, multi-accumulator:** Compute `dot_ab`,
`dot_aa`, and `dot_bb` in one loop with independent SIMD accumulators
for each. Combine at the end: `result = dot_ab / sqrt(dot_aa * dot_bb)`.
Reads each array once and overlaps three FMLA streams.

**ISA levels and approach:**

| Path | Technique |
|------|-----------|
| Platform | `vDSP_dotpr` + two `vDSP_svesq` (or one fused pass if you already have both norms). First choice. |
| ASIMD FMLA | 4 independent `float32x4_t` per stream; `vfmaq_f32`; `vaddvq_f32` at the end |
| SME (huge N / tiled) | ZA DOT / MLA / `FMOPA` for large tiles; SSVE stages. Only if `FEAT_SME` and N is large enough that engine round-trip is amortized |

**Dispatch guards:**

```c
if (apple_is_translated())          → scalar 4-acc / vDSP
else if (apple_feat("FEAT_SME") && n is huge)
                                    → SME tiled
else                                → ASIMD vfmaq 4-acc
/* vDSP is always legal on Darwin; prefer it over all of the above
   when the call is a plain cosine of float arrays. */
```

**Key implementation notes:**
- Pre-normalized inputs (`|A| = |B| = 1`) reduce to a plain dot —
  detect this case and skip the norm computation (`vDSP_dotpr`).
- Guard against division by zero: if `dot_aa * dot_bb < epsilon`, return 0.
- FP associativity: multi-accumulator results may differ from a serial
  sum by rounding ε; document this at the function boundary.
- All custom paths need a scalar 4-acc tail.
- The final `sqrt` and division are negligible for n ≳ 16; optimize the
  loop, not the epilogue.
- Do not bounce a running dot through GPR (`UMOV`) every iteration.

---

## Hamming Distance

**What it computes:** The number of positions where two equal-length
sequences differ. For bit strings: `popcount(a XOR b)`. For byte arrays:
the sum of `popcount(a[i] ^ b[i])` over all bytes.

**Why scalar is slow:** A single-accumulator loop has a loop-carried
dependency on the count, and processes one byte (or word) at a time.

**ISA levels and approach:**

| Path | Technique |
|------|-----------|
| ASIMD CNT | `veorq_u8` → `vcntq_u8` → `vaddlvq_u8` / widening `vpadalq`. 16 bytes/iter. |
| DotProd (all M1+) | XOR then `UDOT` (`vdotq_u32`) against a ones vector to sum popcounts |
| I8MM (M2 / A15+) | `USDOT` / `UMMLA` on packed bytes when the layout fits 8-bit matrix tiles |
| SME (huge N) | ZA integer DOT / I8I32 for large packed bitsets |

**Dispatch guards:**

```c
if (apple_feat("FEAT_SME") && n is huge)     → SME integer DOT
else if (apple_feat("FEAT_I8MM"))            → I8MM (M2+)
else if (apple_feat("FEAT_DotProd"))         → UDOT  (all M1+)
else                                         → CNT + UADDLV
```

DotProd is present on every M1 / A14+ (`hw.optional.arm.FEAT_DotProd`).
I8MM from M2 / A15 (`FEAT_I8MM`). Query; do not assume from "Apple
silicon".

**Key implementation notes:**
- All paths need a scalar tail for `n % vector_width` remaining bytes.
- `__builtin_popcountll` is fine for the tail; do not implement popcount
  by hand.
- Accumulate partial 64-bit sums to avoid overflow on large arrays.
- Keep XOR and CNT in V registers; do not `UMOV` each byte.

---

## Jaccard Distance

**What it computes:** For two bit vectors A and B:
`Jaccard similarity = popcount(A AND B) / popcount(A OR B)`;
`Jaccard distance = 1 - similarity`. Measures set overlap — 0 means
identical, 1 means disjoint. Also known as intersection-over-union
(`iou`) in image/ML contexts.

**Why scalar is slow:** A naive loop computes AND and OR popcount with
two serial accumulators, one byte or word at a time. AND and OR are
independent on the same two input words — one pass, interleaved SIMD,
no extra bandwidth versus Hamming.

**Single-pass trick:** If `popcount(A)` and `popcount(B)` are already
known, the union is
`popcount(A OR B) = popcount(A) + popcount(B) - popcount(A AND B)`.
The loop reduces to a single AND + popcount.

**ISA levels and approach:**

| Path | Technique |
|------|-----------|
| ASIMD CNT | `vandq_u8` + `vorrq_u8`, then the Hamming CNT reduction on both |
| DotProd | `UDOT` on AND and OR streams in the same loop |
| I8MM (M2+) | same as Hamming, two streams |
| SME (huge N) | ZA integer DOT on both streams |

**Dispatch guards:** identical to Hamming Distance (same ISA
requirements).

**Key implementation notes:**
- Guard against division by zero: if `popcount(A OR B) == 0` both
  vectors are all-zero; return similarity 1.0 (or distance 0.0) by
  convention.
- The final division is one float op on two `uint64_t` accumulators —
  negligible; optimize the loop.
- All paths need a scalar tail.
- Accumulate partial 64-bit sums for arrays longer than ~4 GB.
