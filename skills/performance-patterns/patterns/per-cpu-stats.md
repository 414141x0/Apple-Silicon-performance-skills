<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->

# Pattern: shared statistics counter → per-thread / per-group aggregation

## When to apply

Apply when a single global counter (hits, errors, bytes, packets, total) is
incremented atomically from many threads in a hot path. This is **true sharing**
(all threads write the *same* field) — distinguished from false sharing, where
threads write different fields on the same 128 B line.

### Source code signals

- `global_counter++` or `atomic_fetch_add` in a hot, frequently-called path
- Field name suggests accounting: `count`, `total`, `hits`, `misses`, `errors`,
  `stat`, `bytes`, `packets`
- No CAS retry loop (if present, `patterns/ttas.md` applies instead)
- The counter is updated from many call sites or a highly-parallelized loop

### Profiling signals

- `LDADD` / `STADD` / `atomic_fetch_add` prominent in CPU Profiler on the
  hot path
- Dual-profile: the function is a scaling bottleneck at N threads, barely
  visible at 1 — contention scales with thread count
- Same byte offset written by many threads (true sharing)
- No `CAS` retry loop (differentiates from TAS)

---

## Why frequent shared stats hurt under scaling

Every atomic increment needs exclusive ownership of its 128 B line. With N
threads updating it, N−1 wait for each exclusive transfer; the line bounces
through Shared LL and, across clusters, the fabric. `memory_order_relaxed`
does not help: the hardware still enforces exclusive ownership for any write.

---

## Choosing between options

Apply in order of preference:

| Signal | Preferred option |
|--------|-----------------|
| Increment is **inside a loop local to one function** AND ≤ 2 hot profile sites | **Option A: Local batching** |
| More than 2 hot profile sites, or increments scattered across call sites | **Option B: thread-group / bucket array** |
| Shared library that cannot own a global array, or an isolated counter | **Option C: TLS (fallback)** |

Prefer sharding associated with **thread groups** (GCD queue, worker pool
index, `pthread_t` slot). Core-index sharding is a last resort:
`pthread_cpu_number_np` is a heuristic and the thread **may migrate**
(Guide 7.5).

When multiple counters update in the same loop, A and B still win: A flushes
all of them in one block; B indexes the bucket once.

---

## Option A: Local batching

Accumulate into a local (stack) variable and flush to the global at a batch
boundary and at function exit:

```c
#include <stdatomic.h>

_Atomic uint64_t global_counter;

#define COUNTER_BATCH_SIZE 16    /* tune to workload */

void update_items(struct item *array, int n)
{
    uint64_t local_counter = 0;
    for (int i = 0; i < n; i++) {
        perform_update(&array[i]);
        local_counter++;
        if (local_counter > COUNTER_BATCH_SIZE) {
            atomic_fetch_add_explicit(&global_counter, local_counter,
                                      memory_order_relaxed);
            local_counter = 0;
        }
    }
    atomic_fetch_add_explicit(&global_counter, local_counter,
                              memory_order_relaxed);
}
```

If the loop is purely CPU-bound (no I/O, no blocking), drop the mid-loop
threshold and flush once at the end.

Use `memory_order_relaxed` for the flush — no ordering dependency exists.
C11 `atomic_fetch_add` compiles to LSE `LDADD` on Apple silicon.

---

## Option B: thread-group / bucket array

A fixed-size array of 128 B-padded structs, indexed by worker slot or (as a
heuristic) CPU number. Zero-overhead on the read side (sum the buckets).

**Group all statistics fields into one struct** so padding is paid once.

### Step 1 — Define the per-bucket struct

```c
#include <stdint.h>
#include <assert.h>

struct statistics {
    uint64_t read_counter;
    uint64_t insert_counter;
    uint64_t error_counter;
    uint64_t byte_counter;
    uint64_t _padding[12];    /* 4*8 + 12*8 = 128 */
};

_Static_assert(sizeof(struct statistics) % 128 == 0,
               "struct statistics must be a multiple of 128");

#define STATISTICS_COUNT  64
#define STATISTICS_MASK   (STATISTICS_COUNT - 1)

extern struct statistics statistics[STATISTICS_COUNT]
    __attribute__((aligned(128)));
```

`& STATISTICS_MASK` is a cheap modulo. If live workers exceed
`STATISTICS_COUNT`, some sharing remains, but contention drops from N
threads on 1 line to at most N/`STATISTICS_COUNT`.

| Situation | Recommended STATISTICS_COUNT |
|-----------|------------------------------|
| General use / unsure | 64 |
| Low thread count or infrequent updates | 16 or 32 |
| Contention still visible after 32 | 64 |
| Read cost too high | Decrease by one tier |

### Step 2 — Increment helpers

Prefer a worker index you own (GCD `dispatch_apply` index, thread-pool slot).
`pthread_cpu_number_np` is acceptable as a heuristic only.

```c
#include <pthread.h>
#include <stdatomic.h>
#include "statistics.h"

struct statistics statistics[STATISTICS_COUNT] __attribute__((aligned(128)));

static inline unsigned bucket_index(void)
{
    size_t cpu = 0;
    /* Heuristic: the thread may migrate. Prefer a stable worker id if you have one. */
    if (pthread_cpu_number_np(&cpu) != 0)
        cpu = 0;
    return (unsigned)cpu & STATISTICS_MASK;
}

static inline void inc_read_counter(void)
{
    atomic_fetch_add_explicit(
        &statistics[bucket_index()].read_counter,
        1, memory_order_relaxed);
}
```

**Atomic API priority**

| Priority | API | When to use |
|----------|-----|-------------|
| 1st | `<stdatomic.h>` `atomic_fetch_add_explicit(..., memory_order_relaxed)` | C11 |
| 2nd | `<atomic>` `fetch_add(1, std::memory_order_relaxed)` | C++11 |
| 3rd | `__atomic_add_fetch(&x, 1, __ATOMIC_RELAXED)` | pre-C11 Clang |

### Step 3 — Aggregation

```c
uint64_t get_read_counter(void)
{
    uint64_t sum = 0;
    for (int i = 0; i < STATISTICS_COUNT; i++)
        sum += atomic_load_explicit(
            &statistics[i].read_counter, memory_order_relaxed);
    return sum;
}
```

Display-only statistics are inherently approximate.

### Step 4 — Existing per-CPU stats still contend

If buckets already exist but contention persists, the flush is inside the
hot path. Increase the interval (batch N requests, timer, or thread exit)
and re-check the dual-profile.

---

## Option C (fallback): TLS

```c
static _Thread_local uint64_t local_hit_count;
static _Atomic uint64_t       global_hit_count;

static inline void inc_hit_count(void) { local_hit_count++; }

static void flush_hit_count(void) {
    atomic_fetch_add_explicit(&global_hit_count, local_hit_count,
                              memory_order_relaxed);
    local_hit_count = 0;
}
/* Call flush_hit_count() at thread exit or periodically */
```

TLS init in dylibs is not free. Prefer the bucket array when you can own it.

---

## Verification

1. **Correctness** — totals are now approximate until flush; confirm that is
   acceptable (usually yes for stats).
2. **Profiling** — `LDADD`/`STADD` disappears or drops from the hot path.
3. **Dual-profile** — the linear scaling jumper is gone.
4. **Layout** — `_Static_assert` multiple of 128.

---

## Presenting this to the user

1. Show the shared counter and the hot accessor with `/* contention */`.
2. Explain 128 B exclusive bounce in one paragraph.
3. Pick A, B, or C in one sentence (thread-group over core index).
4. For B: show the 128 B struct, `_Static_assert`, and one increment helper.
   Note that `pthread_cpu_number_np` may migrate.
5. Recommend a dual-profile after the change, not a coherence sampler.
