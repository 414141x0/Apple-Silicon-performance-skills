<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->

# Pattern: false sharing → structured false-sharing fix

## When to apply

Apply when a cache line is heavily written by multiple threads, but each thread
accesses **different fields** at different byte offsets within that line. The
fields are logically independent — no thread needs the data another thread writes
— but the cache-coherence protocol treats the entire **128-byte** line as the
unit of ownership, causing spurious invalidations.

The dual of this pattern is packing hot fields onto the **fewest** lines
(Guide rec 38). Step 3 below is that dual: after padding proves the diagnosis,
group by access so same-thread hot fields share a line.

### Source code signals

- A struct with multiple fields, where different fields are written by different
  threads (per-thread counters, state flags, work items packed into a shared
  struct)
- No `alignas(128)` or `__attribute__((aligned(128)))` between thread-private
  fields
- Fields written at high frequency in a tight per-thread loop share a struct
  with other frequently-written fields

### Profiling signals

- There is **no HITM / c2c sampler**. Diagnose with:
  1. Dual-profile jumper: the function is hot at N threads, invisible at 1.
  2. Layout: different byte offsets of the same 128 B line written by
     different threads (Instruments Points of Interest, or inspect the struct).
  3. Elevated `ATOMIC_OR_EXCLUSIVE_FAIL` when the fields are atomic.
- Regression scales with thread count.
- Cross-cluster modified sharing is worse (~10× intra-cluster Shared LL
  via the fabric). Keep independent writers inside one cluster when possible.

---

## Why false sharing hurts under scaling

A cache line is **128 bytes** (`hw.cachelinesize` — query it, do not hardcode
in portable code). A write to unrelated field X by thread A and field Y by
thread B still invalidates the whole line for the other thread, forcing a
reload from Shared LL or the fabric. Traffic scales with thread count, which
is why it only shows up as prominent in a multi-core profile.

---

## The fix: structured three-step protocol

### Step 1 — Generous padding (proof of concept)

Add `alignas(128)` to the struct and insert explicit `char _pad[N]` fields
between the contested fields. Use *more padding than you think you need* at
this stage — proving the diagnosis is more important than minimizing size:

```c
struct example {
    int written_by_thread_a;        /* thread A's field */
    char _pad0[124];                /* 4 + 124 = 128 */
    int written_by_thread_b;        /* thread B's field */
    char _pad1[124];
    int read_a_lot;                 /* shared read-mostly field */
} __attribute__((aligned(128)));
```

### Step 2 — Verify the fix

Rebuild and rerun the same dual-profile. Confirm that:

- The jumper function drops out of the N-thread profile (or
  `ATOMIC_OR_EXCLUSIVE_FAIL` on that line collapses)
- Throughput at the same thread count improves

If it does not drop, the diagnosis was wrong — the sharing may be true
sharing, or additional fields are involved. Re-inspect offsets before
proceeding. Do not skip to Step 3 without this confirmation.

### Step 3 — Optimize struct layout (pack hot fields)

Once the fix is confirmed, minimize size by grouping members by **access
pattern**. This is Guide rec 38 — pack hot variables onto the fewest cache
lines — the dual of the padding in Step 1:

| Group | What to put here |
|-------|-----------------|
| **Write-often (thread A)** | Fields written frequently from thread A only — **one 128 B line** |
| **Write-often (thread B)** | Fields written frequently from thread B only — **next 128 B line** |
| **Read-mostly** | Fields read by many threads but rarely written (config, thresholds) — may share a line |
| **Rarely touched** | Initialization-time fields; fill remaining space |

Each **write-often** group for a different thread must occupy its own 128 B
line. **Read-mostly** fields from all threads can share a line — shared reads
do not bounce ownership. **Rarely-touched** fields fill the rest.

```c
struct example {
    /* Thread A's write-often fields — one 128 B line */
    int written_by_thread_a;
    uint64_t thread_a_counter;
    char _pad0[116];            /* 4 + 8 + 116 = 128 */

    /* Thread B's write-often fields — next 128 B line */
    int written_by_thread_b;
    uint64_t thread_b_counter;
    char _pad1[116];

    /* Read-mostly shared fields — can share a line */
    int config_threshold;
    int config_limit;
} __attribute__((aligned(128)));

_Static_assert(sizeof(struct example) % 128 == 0,
               "struct example must be a multiple of 128");
```

After each layout change, re-verify with a dual-profile. Stop when padding
is minimal and the jumper is still gone.

### Alternative: per-thread structs

If the false sharing stems from a global-but-sharded object, move
thread-private fields into a true per-thread struct (allocated separately
or TLS) and remove them from the shared struct. That eliminates the problem
structurally.

---

## Verification

1. **Step 2 is the primary verification** — dual-profile + layout, not a
   coherence sampler.
2. **Benchmark** — throughput at N threads; the regression should be gone.
3. **Size check** — `sizeof` is a multiple of 128; `_Static_assert` it.
4. **Cross-cluster** — if the machine has multiple clusters
   (`hw.nperflevels`, `hw.perflevel{N}.physicalcpu`), prefer not to have
   modified sharing of the same line across the fabric.

---

## Presenting this to the user

1. Show the struct with byte offsets (`/* +0x00 */`, `/* +0x04 */`) so the
   128 B conflict is obvious.
2. Explain that the line is 128 B, not 64 B, and that independent fields
   still bounce as one unit.
3. Apply Step 1 (generous padding) and show the modified struct.
4. Ask the user to rebuild and rerun the dual-profile — **do not skip to
   Step 3 without Step 2**.
5. After confirmation, propose the packed layout (hot fields on the fewest
   lines) and show the new struct with grouping comments.
