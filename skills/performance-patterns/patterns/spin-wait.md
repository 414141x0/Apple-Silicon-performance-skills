<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->
# Pattern: spin-wait → block idle threads

## When to apply

Apply when a thread **burns a core waiting** (Guide rec 61). On Apple
silicon that core is often a P or S core the OS could have given to
runnable work — including the work this thread is waiting *on*, which
may be stuck on an E core because this spinner is occupying the fast
one.

Almost never spin. The exception is a wait known to be on the order of
a **thread-switch** (a few microseconds at most), and even then prefer
a platform primitive that already has a short spin inside it.

### Source code signals

- `while (!flag) ;` / `while (!atomic_load(&flag)) ;` / `while (queue.empty());`
- `while (!try_lock()) ;` without a backoff that eventually blocks
- `_mm_pause()`, `cpu_relax()`, `usleep(0)`, `pthread_yield_np()` in a
  poll loop (the x86 pause does not belong here; `usleep(0)` and
  `pthread_yield_np` keep the thread runnable)
- Homegrown ticket / TAS / MCS spinlocks in user space
- A worker thread that never calls `dispatch_semaphore_wait`,
  `pthread_cond_wait`, or `kevent` — it polls shared memory instead

### Profiling signals

- CPU Profiler: a hot function that is a load + compare + branch with
  almost no retired useful work
- System Trace: a thread in Running on a P core while its partner is
  Runnable or waiting on E cores
- CPU Bottlenecks Useful-bandwidth looks “fine” (the spinner retires
  plenty of µops) but wall-clock is idle-bound
- Energy / thermal: a “quiet” wait that still keeps a cluster awake

---

## Why this is slow

A spin-wait occupies a core. On AMP that is worse than on a symmetric
chip:

- The spinner often sits on a **P/S core**. Work that needed that core
  stays on E cores.
- The OS cannot use the idle time for *other* processes or for
  rebalancing.
- A context switch of the *useful* thread may happen at a worse moment
  because the spinner never blocked.

`PAUSE` / `YIELD` / `pthread_yield_np` does not fix this. It only
makes the spin slightly less cache-hostile. The thread is still
runnable. Apple’s tuning guide calls out `pthread_yield_np`
specifically: do not use it as a wait.

Platform locks (`os_unfair_lock`, pthread mutex/cond, GCD semaphore)
are **tuned per chip** — topology, fairness between clusters, the
point at which they stop spinning and block. A homegrown spinlock is
not.

---

## The fix

Block. Use a primitive the platform already tuned.

### 1. Replace `while (!flag)` with a condition wait

**Before:**

```c
static volatile int ready = 0;
static int payload;

void producer(int value)
{
    payload = value;
    ready = 1;                 /* also a weak-memory bug: see weakly-ordered-memory.md */
}

void consumer(void)
{
    while (!ready)
        ;                      /* ◄ burns a core */
    use(payload);
}
```

**After — pthread cond (blocks, and pairs with release/acquire):**

```c
#include <pthread.h>
#include <stdatomic.h>

static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  cv = PTHREAD_COND_INITIALIZER;
static int ready = 0;
static int payload;

void producer(int value)
{
    pthread_mutex_lock(&mu);
    payload = value;
    ready = 1;
    pthread_cond_signal(&cv);
    pthread_mutex_unlock(&mu);
}

void consumer(void)
{
    pthread_mutex_lock(&mu);
    while (!ready)
        pthread_cond_wait(&cv, &mu);   /* blocks; OS can schedule others */
    int v = payload;
    pthread_mutex_unlock(&mu);
    use(v);
}
```

### 2. GCD: semaphore or group

**Before:** a worker polls a counter.

**After:**

```c
#include <dispatch/dispatch.h>

void run_then_join(dispatch_queue_t q, void (^work)(void))
{
    dispatch_group_t g = dispatch_group_create();
    dispatch_group_async(g, q, work);
    dispatch_group_wait(g, DISPATCH_TIME_FOREVER);  /* blocks */
}

void handoff(dispatch_semaphore_t sem)
{
    /* producer */ dispatch_semaphore_signal(sem);
    /* consumer */ dispatch_semaphore_wait(sem, DISPATCH_TIME_FOREVER);
}
```

### 3. Short critical sections: `os_unfair_lock`, not a spin

```c
#include <os/lock.h>

static os_unfair_lock lock = OS_UNFAIR_LOCK_INIT;

void bump(int *p)
{
    os_unfair_lock_lock(&lock);
    (*p)++;
    os_unfair_lock_unlock(&lock);
}
```

Do not write a TAS/TTAS loop around `atomic_compare_exchange`. If you
are in the rare “wait is definitely shorter than a switch” case, the
unfair lock already spins briefly internally. See also `ttas.md` — on
this platform the default is **block**, not a better spin.

### 4. The only legitimate spin: sub-scheduling-quantum waits (M4+)

If — and only if — a profile proves the wait is a handful of tens or
hundreds of cycles (producer is the sibling hyper-operation on this
cluster, not a syscall):

- `YLD` (`yield`) to free pipeline resources.
- On M4 / A18 and later (`hw.optional.arm.FEAT_WFxT`), `WFET` / `WFI`
  with a timeout so a missed event cannot pin the core.

```c
#include <stdatomic.h>

void wait_brief(_Atomic int *flag)
{
    /* Still a last resort. Prefer os_unfair_lock / cond. */
    while (!atomic_load_explicit(flag, memory_order_acquire)) {
        __asm__ volatile("yield");
        /* M4+: WFET with a small timeout, then block if still not ready. */
    }
}
```

Cap the spin and **fall back to a cond / semaphore**. An unbounded
`YLD` loop is still rec 61’s anti-pattern.

---

## Verification

1. **Correctness** — no lost wakeups. Cond waits stay in a `while`
   (not `if`) around the predicate. Pair with
   `weakly-ordered-memory.md` and TSan (Guide 8.3).
2. **System Trace** — the waiting thread should show Blocked / waiting
   on a semaphore or cond, not Running. P cores should be free for
   runnable work.
3. **Wall-clock** — especially under background load, the blocking
   version should complete sooner because the producer can get a P
   core.
4. **Energy** — a quiet wait should let the cluster idle.

---

## Presenting this to the user

1. Show `while (!flag) ;` marked `/* ◄ burns a P core */`.
2. One paragraph: AMP makes spinning uniquely harmful; the OS cannot
   steal that core for the producer or for anyone else.
3. Offer `pthread_cond_wait` or `dispatch_group_wait` / semaphore as
   the default fix; `os_unfair_lock` for short mutual exclusion.
4. Mention `YLD` / `WFxT` only as a bounded, profile-justified inner
   loop on M4+, always falling back to a real block.
5. Do not ship a custom queued lock “because GCD has overhead”. Rec 62
   is to use the tuned APIs.
