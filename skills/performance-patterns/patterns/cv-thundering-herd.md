<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Pattern: condition variable thundering herd

## When to apply

Apply when a condition variable (`pthread_cond_broadcast` / `notify_all`, or a
`notify_one` loop) wakes far more threads than can make progress. Wasted
wakeups dominate: woken threads race for the mutex, find no work, and
immediately re-block.

**Preferred Apple fix: GCD** (recs 60, 62) — serial/concurrent queues,
groups, and semaphores are tuned per chip. Hand-rolled tree wakeup is the
last resort.

### Source code signals

- `pthread_cond_broadcast(&cv)` or `cv.notify_all()` used to wake a worker
  pool where only a subset of waiters have work
- `notify_one()` called in a loop to wake N threads sequentially
- A dispatcher that wakes all threads unconditionally, regardless of pending
  jobs: `for (i = 0; i < nthreads; i++) wake(thread[i])`
- Workers that `sched_yield()` or immediately re-block after wakeup

### Profiling signals

- `psynch_cvbroad`, `__psynch_cvsignal`, `kevent`,
  `dispatch_kevent_worker` hot in CPU Profiler / System Trace — wakeup
  path, not lock hold
- Context-switch rate scales with N waiters per broadcast
- Useful work collapses while CPU utilization stays high — cores busy with
  scheduler dispatch and mutex re-acquisition
- Differentiator from lock contention: mutex hold/wait looks normal; the
  cost is N useless wakes

| Symptom | Lock contention | CV thundering herd |
|---------|----------------|-------------------|
| Hot symbol | `psynch_mutexwait`, `os_unfair_lock` | `psynch_cvbroad`, `kevent` |
| Hold time | High | Normal |
| Context-switch rate | Moderate | Very high |
| Wake frequency | Low | High |

---

## Why this is slow

`notify_all()` wakes all N waiters, but CV semantics force them to serialize
on mutex re-acquisition — only one wins, the rest pay a full kernel
round-trip (wake → schedule → fail → sleep) for nothing. Cost per call:
O(N) context switches, O(N) `kevent`/`psynch` syscalls, O(N) IPIs. If only
J ≪ N threads have work, the wasted fraction grows with core count — and
on AMP, those useless runnables occupy S/P cores.

---

## The fix

Choose the strategy that matches the wakeup topology. **Try GCD first.**

### Fix 0: Grand Central Dispatch (preferred)

```c
#include <dispatch/dispatch.h>

static dispatch_queue_t work_q;

void start_pool(void)
{
    work_q = dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0);
}

void submit_jobs(const job_t *jobs, int n)
{
    dispatch_apply((size_t)n, work_q, ^(size_t i) {
        process(&jobs[i]);
    });
}
```

GCD wakes as many workers as it needs, respects QoS (P vs E hints), and
does not broadcast to idle threads. For producer/consumer, use
`dispatch_semaphore` or a serial queue rather than a CV.

### Fix 1: Precise wakeup count (dispatcher → workers)

When a dispatcher knows how many jobs are available, wake exactly that many
threads:

```cpp
int threads_to_wake = std::min(num_threads, num_jobs);
for (int i = 0; i < threads_to_wake; i++) {
    if (all_jobs_claimed()) break;
    cv.notify_one();
}
```

### Fix 2: Replace `notify_all` with `notify_one` + hand-off

When only one thread can make progress at a time:

```cpp
{
    std::lock_guard lock(mtx);
    queue.push(item);
}
cv.notify_one();  // notify after releasing the mutex

// Consumer: after processing, pass the baton if work remains
{
    std::unique_lock lock(mtx);
    cv.wait(lock, [&]{ return !queue.empty(); });
    auto item = queue.pop();
    bool more = !queue.empty();
    lock.unlock();
    if (more)
        cv.notify_one();
    process(item);
}
```

### Fix 3: Semaphore (counter-based predicates)

When the condition is simply "N units are available":

```c
dispatch_semaphore_t sem = dispatch_semaphore_create(0);
dispatch_semaphore_signal(sem);   // producer
dispatch_semaphore_wait(sem, DISPATCH_TIME_FOREVER);  // consumer
```

POSIX `sem_wait` / `sem_post` is the portable equivalent. Either avoids the
mutex re-acquisition storm.

### Fix 4: Tree wakeup (last resort)

When a leader must wake all N followers and GCD is unavailable, replace the
O(N) sequential loop with an O(√N) tree. Keep this behind the GCD option;
custom wakeup trees are exactly the homemade primitive rec 62 warns
against.

### When NOT to apply

- **All waiters genuinely need to wake** (barrier) — use `pthread_barrier_t`
  or `dispatch_group`, not a manual CV
- **The predicate check is the expensive part** — if re-checking the
  predicate (not the wakeup machinery) is the bottleneck, this pattern
  does not apply

---

## Verification

1. **Correctness** — no lost wakeups; every job is processed; no thread
   sleeps forever. Stress with varying job and thread counts.
2. **Wake path** — CPU Profiler / System Trace: `psynch_cvbroad` / `kevent`
   rate drops from O(N) per dispatch to O(J).
3. **Context switches** — Instruments Thread State / CPU Profiler should
   show a proportional reduction.
4. **Useful work** — CPU Bottlenecks Useful rises as cores leave scheduler
   thrash.
5. **Scaling** — "worse past N cores" disappears or moves out.

---

## Presenting this to the user

1. Show the profile: `psynch_cvbroad` / `kevent` percentage, context-switch
   rate.
2. Identify the `notify_all` / wake-all loop and state the ratio:
   "N threads woken for J jobs means N−J wasted wakeups per round."
3. Lead with GCD. Then precise wakeup, hand-off, or semaphore depending on
   topology.
4. Explain in one paragraph: each wasted wakeup costs a syscall + IPI +
   context switch + mutex re-acquisition, and on AMP those runnables steal
   S/P cores.
