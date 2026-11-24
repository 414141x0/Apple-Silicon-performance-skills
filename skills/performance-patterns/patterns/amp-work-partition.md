<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->
# Pattern: AMP work partition (QoS + dynamic queues)

## When to apply

Apply when a parallel workload on Apple silicon **finishes late**, **leaves
E cores idle**, or **pins the wrong work on P cores** (recs 59–60).
You cannot pin a thread to “the P cores” or “the E cores”. The OS picks
the core type from **QoS hints** and from how much runnable work exists.

Static equal chunks — OpenMP `schedule(static)`, `parallel_for` over
`n / nthreads` slices, “one task per core” — assume every worker is as
fast as every other. On S/P/E they are not. E cores finish their equal
slice later; P cores sit idle at the barrier.

### Source code signals

- `#pragma omp parallel for schedule(static)` or a hand-rolled
  `start = tid * (n / T); end = start + n / T`
- A worker pool sized to a hardcoded `8`, `10`, or `nproc()` captured
  once at process start
- `thread_affinity` / `thread_policy_set` attempts to bind to a core
  index because “0–3 are P cores”
- Background / maintenance work running at default QoS (it will compete
  for P cores)
- Interactive work dispatched at `QOS_CLASS_BACKGROUND` (it will languish
  on E cores)
- Barriers after every small phase (`omp barrier`, `dispatch_barrier` on
  a concurrent queue used as if it were static)

### Profiling signals

- Dual run: wall-clock with all logical CPUs vs a run that is clearly
  P-core bound. Scaling dies well before `hw.logicalcpu`
- Instruments System Trace / Thread State: some worker threads in
  busy-wait or blocked on a barrier while others still have a large
  remaining slice
- CPU Counters sampled per core type (`hw.perflevel0` = fastest) show
  E cores still retiring the last static chunk after P cores go idle
- QoS of the hot threads is `DEFAULT` or `BACKGROUND` for user-facing
  work, or `USER_INTERACTIVE` for bulk numeric work that should share

---

## Why this is slow

Apple silicon is an **asymmetric multiprocessor**. `perflevel0` is the
fastest core type (P, or S on chips that have them); higher `perflevel`
numbers are slower. A static partition gives every worker the same number
of items. The workers on E cores take longer; the barrier waits for the
slowest. The OS may rotate threads across core types, which makes a
pre-computed “P-core slice” even more wrong.

Dynamic queues invert that: a worker that landed on a P core pulls the
next task sooner and therefore does more of the work. That is the point.

A second failure mode is QoS. Software **cannot** request “run on P
core 2”. It can say “this is user-interactive” vs “this is background”
and the scheduler will prefer P/S vs E. Wrong QoS either steals P cores
from the UI or strand the actual hot kernel on E cores.

---

## The fix

### 1. Hint with QoS — do not pin

```c
#include <pthread.h>
#include <sys/qos.h>

void start_interactive_worker(void)
{
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INITIATED, 0);
    /* bulk numeric that should still prefer P/S cores */
}

void start_background_worker(void)
{
    pthread_set_qos_class_self_np(QOS_CLASS_BACKGROUND, 0);
}
```

GCD carries QoS on the queue:

```c
dispatch_queue_t bulk =
    dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0);
dispatch_queue_t housekeep =
    dispatch_get_global_queue(QOS_CLASS_BACKGROUND, 0);
```

Never use `thread_policy_set` affinity as a substitute. Core indices
move; clusters differ by SKU; the contract is QoS.

### 2. Dynamic queues, ≥ 3× tasks per core

Query the worker count — do not hardcode it (`sysctl-hardcoding.md`):

```c
#include <sys/sysctl.h>

int logical_cpus(void)
{
    int n = 1;
    size_t sz = sizeof(n);
    if (sysctlbyname("hw.logicalcpu", &n, &sz, NULL, 0) != 0)
        return 1;
    return n;
}
```

`perflevel0` is the fastest type; `hw.nperflevels` is how many types
exist. Use those only to *understand* the machine, not to pin.

**Before — static OpenMP chunks (E cores finish last):**

```c
#pragma omp parallel for schedule(static)
for (int i = 0; i < n; i++)
    work_item(i);
```

**After — GCD `dispatch_apply` (workers pull from a shared queue):**

```c
#include <dispatch/dispatch.h>

void run_items(int n)
{
    /* AUTO picks a width; each iteration is one stealable task.
       Keep n ≳ 3 × hw.logicalcpu. If n is huge, group into chunks
       so each task is still small relative to the whole job. */
    dispatch_apply((size_t)n, DISPATCH_APPLY_AUTO, ^(size_t i) {
        work_item((int)i);
    });
}
```

**After — concurrent queue with explicit grouping** when each item is
tiny and you want ~3× oversubscription:

```c
void run_chunked(int n)
{
    int workers = logical_cpus();
    int tasks = workers * 3;                 /* starting point; more is OK */
    if (tasks > n) tasks = n;
    int chunk = (n + tasks - 1) / tasks;

    dispatch_queue_t q =
        dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0);
    dispatch_apply((size_t)tasks, q, ^(size_t t) {
        int begin = (int)t * chunk;
        int end = begin + chunk;
        if (begin >= n) return;
        if (end > n) end = n;
        for (int i = begin; i < end; i++)
            work_item(i);
    });
}
```

If you must stay on OpenMP, `schedule(dynamic, k)` with a small `k` so
there are at least ~3× as many chunks as cores. Static equal chunks are
the bug.

### 3. Do not spin waiting for the queue to drain

Block (`spin-wait.md`). `dispatch_group_wait`, `dispatch_sync` on a
barrier group, or a condvar — not `while (outstanding) ;`.

---

## Verification

1. **Correctness** — same results as the serial / static version.
   Dynamic order is not deterministic; reductions need atomics, thread
   locals, or a final serial combine.
2. **Wall-clock vs core types** — System Trace: P cores should keep
   pulling work until the queue is empty; E cores should not be the
   unique stragglers at a barrier.
3. **Task count** — `n_tasks / hw.logicalcpu ≥ 3`. If you have eight
   logical CPUs and eight giant slices, you have not applied this pattern.
4. **QoS** — `thread_info` / Instruments QoS column matches the work’s
   importance. Background maintenance must not show
   `USER_INTERACTIVE`.

---

## Presenting this to the user

1. Show the static split (`schedule(static)`, `tid * n / T`) marked
   `/* ◄ equal chunks; E cores finish last */`.
2. One paragraph: you cannot pin P vs E; QoS hints; faster cores must
   be allowed to steal.
3. Show `dispatch_apply` (or `schedule(dynamic)`) and the
   `hw.logicalcpu` query. State the ≥ 3× rule as a starting point, not
   a law of physics.
4. Call out any hardcoded `8` or affinity mask — that is also
   `sysctl-hardcoding.md`.
5. Rebuild native arm64; if `sysctl.proc_translated == 1`, stop — a
   Rosetta run is not an AMP measurement.
