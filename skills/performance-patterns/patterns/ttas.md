<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Pattern: test-and-set spinlock → block first, TTAS only for short waits

## When to apply

Apply when a lock or flag wait is implemented as a bare Test-and-Set loop —
a `CAS` / `LDXR`+`STXR` with no preceding ordinary read — or as any spin
that occupies a core while idle.

**Default on Apple silicon is to block, not to spin** (rec 61). TTAS is
the repair for the *rare* wait whose duration is on the order of a thread
switch. Longer waits: GCD, `os_unfair_lock`, pthreads, C11. Homemade queued
locks are not tuned for cluster topology and will mis-behave across families.

Also read `patterns/spin-wait.md` whenever the loop is idle-but-runnable.

### Source code signals

- `while (!atomic_compare_exchange_weak(&lock, &unlocked, locked)) {}` with
  no prior relaxed load
- `while (__sync_lock_test_and_set(&lock, 1)) { /* spin */ }`
- Any spinlock body that performs only an atomic RMW without a shared read
  of the lock variable
- `while (!flag) ;` / `WFE` / `YIELD` backoff loops

### Profiling signals

- `CAS` or `LDXR`/`STXR` clusters in CPU Profiler with high sample density
  and no ordinary load of the lock first
- Throughput **drops** (not just plateaus) as thread count increases
- S/P cores busy in a wait loop while other work could run
- `ATOMIC_OR_EXCLUSIVE_FAIL` elevated on the lock line

---

## Why this is slow

`CAS` / exclusive pairs acquire the line **exclusively** even on failure, so
every spinning waiter fights the holder and the other waiters. Traffic grows
badly under contention, and any protected data sharing the lock's 128 B line
gets caught in the same bounce.

Spinning also occupies S/P cores. The system may then leave real work on E
cores, and it cannot use that idle time for other threads (rec 61).

`LDXR`/`STXR` loops are the slow path next to single-instruction LSE
(`LDADD`, `CAS`) — rec 65.

---

## The fix

### 0. Prefer a tuned API (almost always)

| Need | API |
|------|-----|
| Short exclusive critical section | `os_unfair_lock` |
| Mutex / condvar | pthreads or C11 |
| Task / queue | GCD (`dispatch_sync`, serial queue, `dispatch_semaphore`) |
| Atomic add / or / and | C11 `atomic_fetch_*` → LSE `LDADD` / `LDSET` |
| Read-mostly shared data | `pthread_rwlock` or GCD concurrent queue — `patterns/mutex-to-rwlock.md` |

Do not write a queued spinlock from exclusive pairs. Those APIs are tuned
per chip for topology, protocol, and latency.

### 1. If you must wait a known-short time: TTAS

Read the lock non-atomically first (shared line, no exclusive RMW) and only
attempt `CAS` when it looks free. Use LSE `CAS`, not `LDXR`/`STXR`.

```c
#include <stdatomic.h>

enum { UNLOCKED = 0, LOCKED = 1 };

void lock_ttas(_Atomic int *lock)
{
    for (;;) {
        while (atomic_load_explicit(lock, memory_order_relaxed) == LOCKED) {
            /* Wait is ~thread-switch. Longer → block instead (spin-wait.md). */
            __asm__ __volatile__("yield");
        }
        int expected = UNLOCKED;
        if (atomic_compare_exchange_weak_explicit(
                lock, &expected, LOCKED,
                memory_order_acquire, memory_order_relaxed))
            break;
    }
}
```

Waiters hold the line in **shared** mode during the spin. Exclusive RMW
happens only on a real unlock→lock transition.

On M4+ (`hw.optional.arm.FEAT_WFxT`), a timed `WFET`/`WFIT` can replace a
naked `yield` for sub-quantum waits. If the wait is *not* known-short, do
not spin at all — see `patterns/spin-wait.md`.

### 2. Try-first (uncontended fast path)

```c
void lock_ttas_tryfirst(_Atomic int *lock)
{
    int expected = UNLOCKED;
    if (atomic_compare_exchange_strong_explicit(
            lock, &expected, LOCKED,
            memory_order_acquire, memory_order_relaxed))
        return;
    lock_ttas(lock);
}
```

### 3. Lock and data on different 128 B lines

```c
struct my_lock {
    _Atomic int lock;
    char _pad[124];
} __attribute__((aligned(128)));
```

---

## Verification

1. **Correctness** — existing tests; lock semantics unchanged.
2. **Profiling** — `CAS` / exclusive-fail density drops; a blocking API
   should show `psynch_*` / GCD sleeps, not a hot spin.
3. **Scaling** — throughput at 1, 2, 4, … threads no longer declines.
4. **QoS** — confirm S/P cores are not pinned in the wait loop
   (`thread_info` / Instruments System Trace).

---

## Presenting this to the user

1. Show the spin with `/* exclusive acquire every fail */` on the `CAS`.
2. Lead with **block, do not spin**. Recommend GCD / `os_unfair_lock` /
   pthreads.
3. Offer TTAS only if the wait is known to be ~one thread switch. Show
   relaxed-load then LSE `CAS`, `yield` (or WFxT), 128 B padding.
4. Point at `patterns/spin-wait.md` for the idle-thread rule.
5. Note LSE (`LDADD`/`CAS`) over `LDXR`/`STXR`.
