<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Pattern: SIMD Sort

## When to apply

**From source code** — any of the following on arrays or `std::vector` of
`float`, `double`, `int32_t`, `uint32_t`, `int64_t`, or `uint64_t`:

- `std::sort(arr, arr + n)` or `std::sort(vec.begin(), vec.end())`
- `std::nth_element` (partial partition)
- `std::partial_sort`
- `qsort()` with a numeric comparator
- A hand-written quicksort, merge sort, or introsort over numeric primitives

The data type is the key signal. This pattern applies to the six numeric
primitive types listed above. It does not apply to sorts of strings, pointers,
or complex objects without a numeric key.

**From a profile** — `std::sort`, introsort, or `qsort` appearing prominently
in CPU Profiler on a workload that sorts numeric data. Discarded /
branch-mispredict events are often elevated — introsort's comparator
branches are hard to predict.

---

## Why this is slow

`std::sort` uses introsort — a hybrid of quicksort, heapsort, and insertion
sort — operating on one element at a time. Every comparison is a branch, and
partitioning n elements takes O(n log n) comparisons with no data
parallelism. ASIMD can compare four floats (or two doubles) per instruction;
Accelerate and Highway use that. An x86 SIMD-sort GitHub repo does not.

---

## The fix

Apply in this order:

### 1. Accelerate / vDSP where it fits

`vDSP_vsort` / `vDSP_vsorti` sort `float` and `double` in place (and with
indices). Use them when the array is exactly that type and stability is
not required.

```c
#include <Accelerate/Accelerate.h>

/* order: 1 = ascending, -1 = descending */
vDSP_vsort(arr, n, 1);
```

Check Accelerate first even for "just a sort of floats" —
`patterns/platform-libs-first.md`.

### 2. `std::sort` for general objects

For structs, strings, or anything without a packed numeric key,
`std::sort` is acceptable. Do not vectorize a comparator of arbitrary
objects.

### 3. Highway or an ARM-ported simd-sort

If a vectorized numeric sort is required (int32/int64, or float when
vDSP is unavailable), use [Highway](https://github.com/google/highway)
`vqsort` or an ARM-ported simd-sort. Highway dispatches on the running
CPU; compile with `-mcpu=apple-m1` (or later) and still keep a scalar
fallback.

```cpp
#include "hwy/contrib/sort/vqsort.h"
hwy::VQSort(arr, n, hwy::SortAscending());
```

### 4. Do not recommend numpy / x86-simd-sort on M-series

Those trees are AVX-512 `vcompressps` code. They will not build usefully
for AArch64 and are the wrong dependency.

### Replacing call sites

| Need | Use |
|------|-----|
| `float`/`double` array, unstable | `vDSP_vsort` |
| `std::sort` on general objects | keep `std::sort` |
| numeric primitives, vectorized | Highway `vqsort` |
| `std::stable_sort` | keep it — no drop-in vectorized stable sort here |
| `std::nth_element` / `partial_sort` | `std::nth_element` / Highway if available |

**NaN.** Document the policy. vDSP does not treat NaN the way IEEE
`std::sort` with a NaN-aware comparator does. If NaN is possible, either
filter first or stay on `std::sort`.

---

## Verification

```cpp
std::vector<float> ref = arr;
std::sort(ref.begin(), ref.end());
vDSP_vsort(arr.data(), arr.size(), 1);
assert(arr == ref);
```

Time both with `clock_gettime_nsec_np` on identical input. Compile C++
with `-std=c++17 -O3 -mcpu=apple-m1`.

---

## Caveats summary

| Caveat | Detail |
|--------|--------|
| **Not a stable sort** | vDSP and Highway vqsort do not preserve equal-element order |
| **NaN** | Define a policy; vDSP ≠ IEEE `std::sort` |
| **float/double vs integers** | vDSP covers float/double; integers need Highway or `std::sort` |
| **No x86-simd-sort** | Do not add that repo on Apple silicon |

---

## Presenting this to the user

Lead with the replacement table and a concrete before/after for their
call site. Then:

1. **Why it's faster** (packed compares / Accelerate, not "SIMD" as a slogan)
2. **Stability** — only if the existing code uses `std::stable_sort`
3. **NaN** — address it when the type is FP
4. **Do not** clone an x86 sort library onto M-series
