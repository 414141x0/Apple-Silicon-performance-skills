<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# mutex-to-rwlock benchmark — how to collect on Apple silicon

This file is a **procedure**, not a scoreboard. Numbers depend on S/P/E
counts, frequency, QoS, and what else is running. Do not paste results
from another machine, and do not invent scores.

## 1. Record the machine

```bash
sysctl hw.logicalcpu hw.physicalcpu hw.nperflevels
sysctl hw.perflevel0.physicalcpu hw.perflevel0.name
sysctl hw.perflevel1.physicalcpu hw.perflevel1.name   # if present
sysctl hw.cachelinesize hw.pagesize
sw_vers -productVersion
```

Note QoS: the bench calls `pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0)`.
The OS still places threads; there is no `taskset`.

## 2. Build and run

```bash
cd patterns/tests
chmod +x run-mutex-to-rwlock-bench.sh
./run-mutex-to-rwlock-bench.sh 3
```

Or by hand:

```bash
clang -O2 -pthread -o mutex-to-rwlock-bench mutex-to-rwlock-bench.c
./mutex-to-rwlock-bench
```

Collect at least write_pct = 0, 1, and 5. Cap threads at `hw.logicalcpu`.

## 3. What to look for in the table

- **Mutex, 0% writes:** throughput flattens or falls as threads grow —
  readers serialize on one lock.
- **rwlock, 0% writes:** throughput rises with thread count until
  reader-count atomics or AMP imbalance (E-core stragglers) cap it.
- **1–5% writes:** rwlock still wins, but writers cap scaling because a
  writer waits for all readers and readers wait for the writer.

Paste *your* table under a heading with the sysctl dump from step 1.

## 4. Optional Instruments / xctrace

If `xctrace` is installed (Xcode):

```bash
xctrace record --template 'CPU Profiler' --output mutex.trace -- \
    ./mutex-to-rwlock-bench 8 5 0 mutex
xctrace record --template 'CPU Profiler' --output rwlock.trace -- \
    ./mutex-to-rwlock-bench 8 5 0 rwlock
```

Open the `.trace` packages in Instruments.

| Lock | Typical hot symbols |
|------|---------------------|
| mutex | `pthread_mutex_lock`, `pthread_mutex_unlock`, `psynch_mutexwait`, `psynch_mutexdrop` |
| rwlock | `pthread_rwlock_rdlock` / `_unlock`, user-space atomics, `do_read_work` |

Kernel time should dominate mutex at high thread counts and be near-absent
for rwlock on a read-only run. Time Profiler is wall-clock — use **CPU
Profiler** for ISA/lock work.

## 5. AMP caveat

A 4-thread run may be 4×P, mixed P+E, or 4×E depending on QoS and load.
If rwlock scaling flattens early, check whether E cores are in the
worker set (Instruments CPU / `thread_info`) before blaming the lock.
See `patterns/amp-work-partition.md`.
