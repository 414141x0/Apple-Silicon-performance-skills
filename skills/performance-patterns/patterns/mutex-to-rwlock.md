<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Pattern: mutex protecting read-heavy operations → rwlock / GCD

## When to apply

Apply when a user-space mutex (`pthread_mutex`, `os_unfair_lock`, a serial
GCD queue used as a lock) protects a critical section where the majority of
acquisitions perform only reads. Under contention, all readers serialize even
though they do not conflict.

### Source code signals

- `pthread_mutex_lock()` / `os_unfair_lock_lock()` guarding a lookup, search,
  or status check that rarely modifies the protected data
- Critical section is read operations (cache lookup, tree search, config
  read) in the common path, with writes only on a rare branch
- Read-to-write ratio exceeds ~75% (the crossover where a reader-writer
  lock wins)
- The protected structure is long-lived and accessed by many threads
  (unit table, connection pool, routing table, config cache)

### Profiling signals

- `pthread_mutex_lock`, `psynch_mutexwait`, `psynch_mutexdrop`, or
  `os_unfair_lock` prominent in CPU Profiler
- Dual-profile: Useful / throughput drops as thread count grows while
  user-space work stays constant — incremental cycles are lock
  serialization
- Call chain traces to a mutex around a lookup/search that rarely writes

---

## Why this is slow

A mutex forces every acquisition — even read-only ones — to take exclusive
ownership of its 128 B line. N concurrent readers fully serialize. At high
thread counts this is O(N) wait per reader, plus `psynch_mutexwait` in the
kernel when the uncontended CAS fails. Readers blocking each other is pure
overhead.

---

## The fix

Pick the Darwin primitive that matches the section:

| Situation | Primitive |
|-----------|-----------|
| Many concurrent readers, rare writers | `pthread_rwlock` |
| Same, in Apple-only code | GCD concurrent queue; `dispatch_barrier_*` for writes |
| Short exclusive section, not read-mostly | keep `os_unfair_lock` — do not invent a lock |
| Cross-platform C++ | `std::shared_mutex` |

### User space (pthreads)

```c
pthread_rwlock_t lock = PTHREAD_RWLOCK_INITIALIZER;

void lookup(int key) {
    pthread_rwlock_rdlock(&lock);
    result = search(table, key);
    pthread_rwlock_unlock(&lock);
}

void insert(int key, void *value) {
    pthread_rwlock_wrlock(&lock);
    add(table, key, value);
    pthread_rwlock_unlock(&lock);
}
```

### GCD concurrent vs serial queue

```c
#include <dispatch/dispatch.h>

static dispatch_queue_t q;

void init_q(void) {
    q = dispatch_queue_create("table",
            DISPATCH_QUEUE_CONCURRENT);
}

void lookup(int key) {
    dispatch_sync(q, ^{
        result = search(table, key);
    });
}

void insert(int key, void *value) {
    dispatch_barrier_sync(q, ^{
        add(table, key, value);
    });
}
```

Readers run concurrently; barrier writes drain them first. QoS on the
queue hints S/P vs E.

### Short exclusive: `os_unfair_lock`

If the critical section is a handful of instructions and *not* read-mostly,
`os_unfair_lock` is the right tool — it is tuned per chip. Do not replace
it with a homemade rwlock.

```c
#include <os/lock.h>
static os_unfair_lock lock = OS_UNFAIR_LOCK_INIT;

os_unfair_lock_lock(&lock);
/* short exclusive work */
os_unfair_lock_unlock(&lock);
```

### Double-check on the write path

When the read path discovers a write is needed (cache miss requiring
insertion), double-check after acquiring the write lock:

```c
pthread_rwlock_rdlock(&lock);
unit = search(table, key);
pthread_rwlock_unlock(&lock);

if (!unit) {
    pthread_rwlock_wrlock(&lock);
    unit = search(table, key);  /* another thread may have inserted */
    if (!unit)
        unit = create_and_insert(table, key);
    pthread_rwlock_unlock(&lock);
}
```

### When NOT to apply

- **Write-heavy** (>50% writes): rwlock bookkeeping loses to a mutex;
  writer acquisition must invalidate Shared copies
- **Very short critical sections**: rwlock overhead may exceed mutex
  serialization — use `os_unfair_lock`
- **Writer starvation**: if writes are time-sensitive, prefer a GCD
  barrier (fairer) or accept mutex

---

## Verification

After applying the fix:

1. **Correctness** — existing tests; pay attention to the double-check on
   the write path.
2. **Profiling** — CPU Profiler: `psynch_mutexwait` / `pthread_mutex_lock`
   drop; rwlock stays in user-space atomics or GCD.
3. **Scaling** — throughput vs thread count now scales with readers rather
   than flattening. Record QoS and `hw.nperflevels` with the numbers.
4. Microbenchmark: `patterns/tests/mutex-to-rwlock-bench.c` and
   `run-mutex-to-rwlock-bench.sh`. Profile with `xctrace` CPU Profiler,
   not `perf`.

---

## Presenting this to the user

1. Show the CPU Profiler output with `pthread_mutex_*` / `psynch_mutexwait`
   highlighted.
2. Identify the mutex from the call chain and show the critical section.
3. Annotate reads vs writes and the estimated ratio.
4. Explain in one paragraph: a mutex serializes readers that do not
   conflict; an rwlock or GCD concurrent queue lets them proceed, with
   some remaining coherence traffic on the reader-count line.
5. Present before/after with separated read and write paths.
6. Note `os_unfair_lock` stays correct for short exclusive sections.
