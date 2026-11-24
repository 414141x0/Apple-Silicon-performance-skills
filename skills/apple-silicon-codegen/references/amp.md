<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Asymmetric multiprocessing

Owns recs **59–65**. Rec 64 (Thread Sanitizer) and rec 65 (single-instruction
LSE atomics) are write-time AMP rules.

S / P / E cores implement the **same ISA**, see the **same coherent
memory**, and the OS **migrates** threads among them. Software cannot pin
a thread to a core type. `taskset` / affinity-to-P is not an Apple Silicon
API. Similar microarchitectures with different performance/efficiency
trade-offs: a kernel that is correct on one type is expected to run well
on the others — one binary. Family aliases, S/P/E *why*, and the ISA
ladder: `chip-families.md`.

**No SMT.** Each core runs one thread; that thread owns the core’s L1 and
TLBs. Do not size pools as `2 × hw.logicalcpu`. `hw.logicalcpu` already is
the enabled physical GPR cores.

E cores are strong enough that some work can stay on E and **free S/P**
for harder work. E also adds multithreaded throughput when used with S/P.
Chips with only S+P: the OS puts demanding work on S, spreads MT work
across all cores, and runs P in efficient modes for background.

Core counts and cluster width: `sysctl.md` (`hw.nperflevels`,
`hw.perflevel{N}.logicalcpu`, `hw.logicalcpu`). `perflevel0` is the
fastest type. An **Mx family** is Mx + Pro + Max + Ultra — same core
*design*, different topology. Dispatch on those sysctl keys, not on a
SKU name. Same idea for an Ax family (Ax / Bionic / Pro).

QoS is a **hint**, not a partition. High-QoS work still lands on E when
S/P are busy; background work can run on P in efficient modes.

---

## Rec 59 — QoS to hint S/P vs E (Medium/Low)

You cannot pick the core. You **can** hint with Quality of Service:

| Class | Intent |
|-------|--------|
| `QOS_CLASS_USER_INTERACTIVE` | UI / immediate; favor S/P |
| `QOS_CLASS_USER_INITIATED` | user-started work, still latency-sensitive |
| `QOS_CLASS_DEFAULT` | unspecified; OS watches the process and may promote CPU-heavy threads to S/P, light threads to E |
| `QOS_CLASS_UTILITY` | progress the user will wait for, not immediately |
| `QOS_CLASS_BACKGROUND` | maintenance; favor E |

The system may still run a high-QoS thread on E when there are more
such threads than S/P cores, and may run background work on P in
efficient modes. Unspecified (`DEFAULT`) is the case where the OS
watches and promotes.

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

The same kernel can rank differently on P vs E. Time both QoS classes
before shipping a width / ILP choice. E is not a slow P.

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

## Rec 60 — GCD + dynamic partitioning, ≥3× tasks per core (Medium/Low)

**Static equal chunks fail on AMP** (and often on SMP too): E cores finish
late, S/P sit idle, the OS may migrate a lagging E thread to S/P **after**
it is already behind, a sibling process on one core stretches one static
slice and the barrier waits. S+P-only chips still imbalance under static.
The OS also rotates non-background threads among cores across quanta.

Using **all** cores (including E) with static still beats P-only. Dynamic
is shorter: faster cores pull more work. Too-fine dynamic loses to queue
overhead and contention. Barriers after static phases fail when some
cores are much faster.

Dynamic partitioning: many tasks on a queue; workers **pull** the next
task when they finish. Faster cores naturally take more tasks; migration
is absorbed.

Starting point:

1. Spawn about **one worker per logical CPU** (`hw.logicalcpu` /
   `hw.logicalcpu_max` — `sysctl.md`).
2. Cut work into **≥3× tasks per core** (more is fine until queue overhead
   dominates). 3× is a starting grain, not a prescription.
3. Tune. Too-small tasks lose to queue contention and management code.
   Stragglers ⇒ finer grain. Queue hot in Instruments ⇒ coarser grain.
   Grain **must be re-tuned across families**: single-thread CPU grows
   faster than queue-management cost (A14 2P+4E … M2 Ultra 16P+8E …
   M5 Max 6S+12P). Keep the queue-management path **short** — it is on
   the scaling critical path.

Prefer **Grand Central Dispatch** (retuned per chip for performance **and
fairness**). C++ threads / pthreads are the portable alternative; you then
own the queue on locks / condvars.

If static is the only option, query sysctl for live core counts — **never
hardcode M1 4+4**.

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

Spin-wait occupies an S/P core, pins lagging E-core work onto E, steals
the core from unrelated system work, and can force a context switch of
*useful* work at a worse time. **Block** (`dispatch_semaphore_wait`,
`pthread_cond_wait`, GCD, `os_unfair_lock` which will sleep).

Spin only when the wait is known to be on the order of a **thread switch**
(a few microseconds, not “until the next network packet”). Most waits are
unknown and longer than that. Cap a legitimate spin and fall back to a
real block. Do not invent a `while (!flag) ;` loop.

`YLD` frees pipeline resources; it does **not** deschedule.
`pthread_yield_np` keeps the thread runnable — not a wait
(`patterns/spin-wait.md`). There is no `_mm_pause` analogue to sprinkle
in C.

**WFxT (M4 / A18+):** `hw.optional.arm.FEAT_WFxT`. `WFET` / `WFIT`
wait-for-event/interrupt **with timeout**, so a missed wakeup cannot pin
the core. Legitimate only inside a **runtime you own**, for
**sub-quantum** waits. Not a replacement for a missing condvar.
Unbounded `YLD`/`WFET` is still this rec’s anti-pattern. Do not emit
them in application kernels.

---

## Rec 62 — Tuned sync APIs, not home-grown locks (Medium/Low)

Do not write a queued spinlock from `LDXR`/`STXR` “because you know AMP”.
Fairness, cluster topology, protocol, and latency change by family **and**
by chip inside a family. A lock tuned on one SKU monopolizes a cluster on
the next.

Use, in roughly this order of preference:

- GCD serial queues / barriers for task-level mutual exclusion
- `os_unfair_lock` for a short critical section
- pthreads mutex / cond
- Foundation mutexes, conditions, and other locks
- `@synchronized` in ObjC
- C11 `<stdatomic.h>` for **data**, not for a homemade lock

Platform locks already contain the **short-spin-then-block** policy.
They are retuned per chip for topology, protocol, and latency.
Cross-cluster fairness is why they exist.

---

## Rec 63 — Minimize cross-cluster modified sharing (Medium/Low)

Intra-cluster producer/consumer rides **Shared LL** (~15 cycles own LL).
If thread count ≤ cores in a cluster (`hw.perflevel{N}.cpusperl2`), the
OS tries to keep them together.

Cross-cluster rides the **fabric, ~10×** (order of magnitude: own LL
~15 c vs other-cluster LL ~50 ns). Apps with more threads than one
cluster will see this: the OS keeps as many threads active as possible
and will employ multiple clusters. Independent working sets are fine.
Heavy modified sharing of queues / semaphores / atomics is not: a core
snoops the line away for RMW; a non-atomic peek before the RMW adds
traffic. Homemade sequences add more — use the rec 62 APIs.

When thread count exceeds one cluster:

- Prefer independent working sets.
- Pack true-shared fields (without false sharing — rec 37,
  `core-microarch.md`, **128 B**).
- Shard hot atomics / queues if contention shows up. Counter shards are
  easy (sum at the end). Queue shards can imbalance; may need steal.
  Prefer shards associated with **threads or thread groups**. Core-ID
  shards (`int pthread_cpu_number_np(size_t *cpu_number_out)`) only
  when thread count is **much larger** than core count, to cap memory.
  The ID is a **mostly-correct heuristic** — the OS may migrate the
  thread between the load of the ID and the access. Use sparingly.
- Increase work-item size to cut shared-structure frequency; tune
  against imbalance.

Symptom: MT does not scale up to core count.

Contention metric: `ATOMIC_OR_EXCLUSIVE_SUCC` / `FAIL` over `INST_ALL`
(`named-events.md`). Exclusives **undercount** M1–M4 when the line starts
Shared; LSE counts correctly; M5 both correct. A zero exclusive-fail
count is not proof of no `LDXR` contention.

False sharing of 128 B lines is rec 37, not this rec. This rec is
**true** sharing volume at scale.

---

## Rec 64 — Thread Sanitizer (Medium/Low)

Correctness before AMP speedups. Enable Xcode Thread Sanitizer on the
parallel code you just wrote. TSan inserts diagnostics on every memory
R/W with timestamps and locations, then reports concurrent same-location
ops, uninitialized mutexes, and thread leaks. Also use the other Xcode
tools for sync, ordering, and leaks. It is a **write-time correctness
habit**, not a cycle profile and not a substitute for rec 62.

---

## Rec 65 — Single-instruction LSE atomics over LDXR/STXR (Low/Low)

All M1 / A14+ have `FEAT_LSE` (`hw.optional.arm.FEAT_LSE`; legacy
`hw.optional.armv8_1_atomics`). A single `LDADD` / `CAS` / `SWP` (and
the rest of the LSE family, optional acquire/release) **keeps the line
through the RMW**: once ordering is met and the core has the line, both
the load and the store complete before the line is sent to another core.
`LDXR`/`STXR` loops can lose the line between the load and the store.

Latency of LSE dest is typically through the **load and the address**,
after order, L1D hit: **4** for CAS / SWP / LD-op. Store-op has no dest.
Class book: `latency-lookup.md`. Do not dump the mnemonic catalog.

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

Measure fail density with `ATOMIC_OR_EXCLUSIVE_*` (`named-events.md`) if
the atomic is hot.

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

---

## SKU snapshot

One-SKU wall-clock: `references/m4-pro-measured.md`.
