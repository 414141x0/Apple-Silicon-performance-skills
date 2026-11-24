<!-- Structure inspired by Intel Performance Skills (MIT, 2026).
     Content is original agent instructions paraphrasing Apple Silicon CPU
     Optimization Guide 5.0. Do not copy Guide tables. MIT license. -->

# Asymmetric multiprocessing (Guide Ch. 7)

Owns recs **59–65**. Rec 64 is the boxed Thread Sanitizer rec from Ch. 8;
rec 65 is the boxed single-instruction atomics rec from Appendix A. Both
are write-time AMP rules, so they live here.

S / P / E cores implement the **same ISA**, see the **same coherent
memory**, and the OS **migrates** threads among them. Software cannot pin
a thread to a core type. `taskset` / affinity-to-P is not an Apple Silicon
API.

**No SMT.** Each core runs one thread; that thread owns the core’s L1 and
TLBs (Guide 5.1). Do not size pools as “2× logical CPUs because of
hyperthreads”.

Core counts and cluster width: `sysctl.md` (`hw.nperflevels`,
`hw.perflevel{N}.logicalcpu`, `hw.logicalcpu`). `perflevel0` is the
fastest type. An **Mx family** is Mx + Pro + Max + Ultra — same core
*design*, different topology. Dispatch on those sysctl keys, not on a
SKU name.

---

## Rec 59 — QoS to hint S/P vs E (Medium/Low)

You cannot pick the core. You **can** hint with Quality of Service:

| Class | Intent |
|-------|--------|
| `QOS_CLASS_USER_INTERACTIVE` | UI / immediate; favor S/P |
| `QOS_CLASS_USER_INITIATED` | user-started work, still latency-sensitive |
| `QOS_CLASS_DEFAULT` | unspecified; OS may promote CPU-heavy threads |
| `QOS_CLASS_UTILITY` | progress the user will wait for, not immediately |
| `QOS_CLASS_BACKGROUND` | maintenance; favor E |

The system may still run a high-QoS thread on E when S/P are busy, and
may run background work on P in efficient modes. QoS is a hint, not a
partition.

```c
#include <dispatch/dispatch.h>
#include <pthread/qos.h>

void start_interactive_work(void)
{
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
    dispatch_queue_t q = dispatch_get_global_queue(QOS_CLASS_USER_INTERACTIVE, 0);
    dispatch_async(q, ^{ /* ... */ });
}
```

---

## Rec 60 — GCD + dynamic partitioning, ≥3× tasks per core (Medium/Low)

**Static equal chunks fail on AMP** (and often on SMP too): E cores finish
late, S/P sit idle, a sibling process on one core stretches one chunk and
the barrier waits.

Dynamic partitioning: many tasks on a queue; workers **pull** the next
task when they finish. Faster cores naturally take more tasks.

Starting point:

1. Spawn about **one worker per logical CPU** (`hw.logicalcpu` /
   `hw.logicalcpu_max` — `sysctl.md`).
2. Cut work into **≥3× tasks per core** (more is fine until queue overhead
   dominates).
3. Tune. Too-small tasks lose to queue contention and management code.
   Stragglers ⇒ finer grain. Queue hot in Instruments ⇒ coarser grain.
   Future families get faster single-thread relative to queue overhead;
   re-tune.

Prefer **Grand Central Dispatch** (tuned per chip). C++ threads / pthreads
are the portable alternative; you then own the queue.

### Original GCD example

```c
#include <dispatch/dispatch.h>
#include <stddef.h>

void map_chunks(size_t nitems, size_t chunk,
                void (*fn)(size_t begin, size_t end, void *ctx),
                void *ctx)
{
    if (chunk == 0)
        chunk = 1;
    const size_t nchunks = (nitems + chunk - 1) / chunk;

    dispatch_queue_t q =
        dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0);

    dispatch_apply(nchunks, q, ^(size_t i) {
        size_t begin = i * chunk;
        size_t end   = begin + chunk;
        if (end > nitems)
            end = nitems;
        fn(begin, end, ctx);
    });
}
```

Choose `chunk` so `nchunks >= 3 * hw.logicalcpu`. Do not write
`nchunks == ncores` with equal-sized static ranges.

A private concurrent queue with an explicit QoS is the same idea when you
need a named queue:

```c
dispatch_queue_attr_t attr =
    dispatch_queue_attr_make_with_qos_class(DISPATCH_QUEUE_CONCURRENT,
                                            QOS_CLASS_UTILITY, 0);
dispatch_queue_t pool = dispatch_queue_create("scan.pool", attr);
```

---

## Rec 61 — Block idle threads; almost never spin-wait (Medium/Low)

Spin-wait occupies an S/P core, pins E-core work onto E, and delays
unrelated system work. **Block** (`dispatch_semaphore_wait`,
`pthread_cond_wait`, GCD, `os_unfair_lock` which will sleep).

Spin only when the wait is known to be on the order of a **thread switch**
(a few microseconds, not “until the next network packet”). Even then, do
not invent a `while (!flag) ;` loop.

`YLD` and WFxT (`WFET`/`WFIT`, `FEAT_WFxT`, M4 / A18+) are for
**sub-quantum** waits inside a runtime you are qualified to write. They are
not a replacement for a missing condition variable. Do not emit them in
application kernels.

There is no `_mm_pause` analogue you should be sprinkling in C.
`pthread_yield_np` is also not a wait — it keeps the thread runnable
(see `patterns/spin-wait.md`).

---

## Daemons: launchd `ProcessType` + XPC

A daemon the user never sees still competes for S/P cores. Set
`ProcessType` in the launchd plist (`Standard` / `Background` /
`Adaptive` / `Interactive`) so the OS can pick a QoS floor. Prefer
**XPC** so the system associates the daemon with the app that
triggered the work; otherwise the daemon is anonymous background
CPU.

If you must hop onto a custom thread, wrap the body in
`dispatch_block_create` so the XPC / QoS context propagates:

```c
dispatch_block_t work = dispatch_block_create(0, ^{ /* … */ });
dispatch_async(own_queue, work);
```

Application threads: rec 59 QoS table above. Do not pin.

---

## Rec 62 — Tuned sync APIs, not home-grown locks (Medium/Low)

Do not write a queued spinlock from `LDXR`/`STXR` “because you know AMP”.
Fairness, cluster topology, and latencies change by family and by chip
inside a family.

Use, in roughly this order of preference:

- GCD serial queues / barriers for task-level mutual exclusion
- `os_unfair_lock` for a short critical section
- pthreads mutex / cond
- Foundation / `@synchronized` in ObjC
- C11 `<stdatomic.h>` for **data**, not for a homemade lock

Cross-cluster fairness is why the platform locks exist.

---

## Rec 63 — Minimize cross-cluster modified sharing (Medium/Low)

Inside a cluster, modified sharing rides the Shared LL (fast). Between
clusters it rides the **fabric, ~10×** that latency (order-of-magnitude;
App. A quotes ~15 cycles own LL vs tens of ns remote / DRAM).

When thread count exceeds one cluster (`hw.perflevel{N}.cpusperl2` is
cores per Shared LL):

- Prefer independent working sets.
- Pack true-shared fields (without false sharing — rec 37,
  `core-microarch.md`, **128 B**).
- Shard hot atomics / queues if contention shows up; accumulate at the
  end. Core-ID sharding via `pthread_cpu_number_np` is a **heuristic** —
  the thread can migrate between the load of the ID and the access. Use
  sparingly.

False sharing of 128 B lines is rec 37, not this rec. This rec is
**true** sharing volume at scale.

---

## Rec 64 — Thread Sanitizer (Medium/Low)

Correctness before AMP speedups. Enable Xcode Thread Sanitizer (Guide 8.3)
on the parallel code you just wrote. TSan is a write-time habit, not a
substitute for rec 62.

---

## Rec 65 — Single-instruction LSE atomics over LDXR/STXR (Low/Low)

All M1 / A14+ have `FEAT_LSE`. A single `LDADD` / `CAS` / `SWP` keeps the
line through the RMW. `LDXR`/`STXR` loops can lose the line between the
load and the store.

```c
#include <stdatomic.h>

static atomic_uint_fast64_t hits;

void account_hit(void)
{
    atomic_fetch_add_explicit(&hits, 1, memory_order_relaxed); // LDADD
}

int try_claim(atomic_uint_fast32_t *slot, uint32_t expect, uint32_t desire)
{
    return atomic_compare_exchange_strong_explicit(
        slot, &expect, desire,
        memory_order_acq_rel, memory_order_acquire);           // CAS
}
```

Use `memory_order_release` / `acquire` when publishing (rec 7,
`integer-isa.md`). Do not drop to `__atomic_load` + store and call it a
lock. Do not write an exclusive loop unless LSE cannot express the RMW.

Measure fail density with `ATOMIC_OR_EXCLUSIVE_*` in
`apple-silicon-profile` if the atomic is hot.

---

## Putting a pool together

```c
#include <dispatch/dispatch.h>
#include <sys/sysctl.h>
#include <stdio.h>

static int logical_cpus(void)
{
    int n = 0;
    size_t sz = sizeof(n);
    if (sysctlbyname("hw.logicalcpu", &n, &sz, NULL, 0) != 0 || n < 1)
        return 1;
    return n;
}

void run_amp_pool(size_t nwork, void (^body)(size_t i))
{
    int cores = logical_cpus();
    size_t ntasks = nwork;
    if (ntasks < (size_t)cores * 3)
        ntasks = (size_t)cores * 3;     // starting point; tune

    dispatch_queue_t q =
        dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0);
    dispatch_apply(ntasks, q, body);
}
```

Workers block when idle (GCD). No spin. No pin. No SMT doubling.
