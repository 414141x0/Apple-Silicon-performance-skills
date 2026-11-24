<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Known algorithms — function name index

When you see any of the function names below, a fully-vectorized
ASIMD / DotProd / I8MM / SME implementation is well-established. Inline
the table in trigger files so detection is free; load
`references/known-algorithms-impl.md` **only after a function name is
confirmed present** in the code or profile being reviewed.

| Algorithm | Common function names in code |
|-----------|-------------------------------|
| Cosine Similarity | `cosine_similarity`, `cosine_sim`, `cos_sim`, `cosine_distance`, `angular_similarity`, `dot_normalized` |
| Hamming Distance | `hamming_distance`, `hamming_dist`, `hamming`, `count_differing_bits`, `bit_diff_count`, `popcount_xor` |
| Jaccard Distance | `jaccard_distance`, `jaccard_similarity`, `jaccard_sim`, `jaccard_index`, `jaccard_coeff`, `iou` |
