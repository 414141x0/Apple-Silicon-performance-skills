<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->
# Pattern: weakly-ordered memory (not TSO)

## When to apply

Apply whenever two threads (or a thread and a signal/interrupt-style
callback) communicate through **plain stores and loads** of a flag plus
payload (rec 7, High / Low). Apple silicon is **weakly ordered**.
It is not x86 TSO. A store to `payload` followed by a store to `ready`
may become visible in the opposite order.

Also apply when a custom lock or counter is built from `LDXR`/`STXR`
instead of LSE (`CAS`, `LDADD`, …) (rec 65), and whenever TSan is not
part of the test plan (rec 64).

### Source code signals

- `payload = x; ready = 1;` on one thread; `if (ready) use(payload);`
  on another, with `ready` a plain `int` or `volatile int`
- C11 atomics using `memory_order_relaxed` on a publication flag
- Comments like “volatile is a barrier” or “this was fine on Intel”
- Handwritten `LDXR` / `LDAXR` / `STXR` / `STLXR` retry loops
- `DMB` used as a reflex around every access, or never used at all
- A ported x86 lock-free queue that never mentions acquire/release

### Profiling signals

- This is primarily a **correctness** pattern. The performance hook is
  *using the wrong primitive*: exclusive-monitor loops (`LDXR`/`STXR`)
  fail under contention where a single-instruction LSE atomic would
  not lose the line between load and store.
- TSan reports a data race on the payload or the flag
- Heisenbugs that vanish under `-O0` or appear only on M-series, not
  under Rosetta

---

## Why this is slow

Weak ordering lets the core, the cluster fabric, and the compiler
reorder independent-looking accesses. Without a **release** store (or
`DMB`/`DSB`) on the producer and an **acquire** load (or matching
barrier) on the consumer, the consumer can see `ready == 1` and still
read the old `payload`.

`volatile` prevents *compiler* tearing of that one object; it does not
order two objects against each other on this ISA.

Performance: `LDXR`/`STXR` can lose exclusive ownership of the line
between the two instructions. LSE (`LDADD`, `CAS`, `STADD`, …) performs
the RMW as one instruction, which is what rec 65 asks for. Prefer the
C11/C++11 atomic that lowers to LSE over a hand exclusive loop.

---

## The fix

### 1. Publish payload with release; consume with acquire

**Before** (racy on Apple silicon, accidentally fine on TSO):

```c
static int payload;
static int ready;                 /* or volatile int — still not ordered */

void producer(int x)
{
    payload = x;
    ready = 1;                    /* may become visible first */
}

void consumer(void)
{
    if (ready)
        use(payload);             /* payload may still be stale */
}
```

**After — C11 atomics (preferred in C/C++):**

```c
#include <stdatomic.h>

static int payload;
static atomic_int ready = 0;

void producer(int x)
{
    payload = x;
    atomic_store_explicit(&ready, 1, memory_order_release);
}

void consumer(void)
{
    if (atomic_load_explicit(&ready, memory_order_acquire))
        use(payload);
}
```

The release store orders all prior writes (the payload) before `ready`
becomes visible. The acquire load orders all subsequent reads (the
payload) after observing `ready`.

**After — raw ISA** when you are in asm and cannot use C11:

```text
    /* producer */
    STR   W1, [X0]            /* payload */
    STLR  W2, [X3]            /* ready, release */

    /* consumer */
    LDAR  W2, [X3]            /* ready, acquire */
    CBZ   W2, not_ready
    LDR   W1, [X0]            /* payload */
```

`DMB ISH` (data memory barrier, inner shareable) is the heavier hammer
when you need to order more than one location and acquire/release on a
single flag is not enough. `DSB` waits for completion; use it only when
you need that (JIT icache, some device MMIO) — see `jit-icache.md`.

### 2. Prefer LSE to `LDXR`/`STXR`

**Before:**

```c
int atomic_add_llsc(_Atomic int *p, int d)
{
    int old, tmp;
    do {
        __asm__ volatile(
            "ldxr %w0, [%2]\n"
            "add  %w1, %w0, %w3\n"
            "stxr %w1, %w1, [%2]\n"
            : "=&r"(old), "=&r"(tmp) : "r"(p), "r"(d) : "memory");
    } while (tmp);
    return old;
}
```

**After:**

```c
#include <stdatomic.h>

int atomic_add_lse(_Atomic int *p, int d)
{
    return atomic_fetch_add_explicit(p, d, memory_order_relaxed);
    /* lowers to LDADD / STADD on Apple silicon (FEAT_LSE, all M1+) */
}
```

CAS: `atomic_compare_exchange_strong` → `CAS`. Do not roll your own
monitor loop unless you are in a language that cannot emit LSE.

### 3. Prefer a platform API when there is one

Flags-plus-payload is often a condvar or GCD semaphore in disguise
(`spin-wait.md`). Use those when you would also be waiting. Use
atomics when you are only publishing.

### 4. TSan

Build a test configuration with Thread Sanitizer (Xcode or
`-fsanitize=thread`). Rec 64: TSan is how you confirm the acquire/
release actually covers the payload. A “green” TSan run is part of
the fix, not an optional extra.

---

## Verification

1. **TSan** — no race on `payload` or `ready`. If TSan still fires,
   the barrier is on the wrong object or the wrong side.
2. **Disassembly** — producer ends in `STLR` / `SWPL` / `STADD` with
   release semantics, not a plain `STR` to the flag. Consumer starts
   with `LDAR` / `LDADD` acquire, not a plain `LDR`.
3. **Stress** — run the producer/consumer on mixed P/E cores (different
   clusters) many millions of times. Cross-cluster visibility is where
   TSO-port bugs show up.
4. **LSE** — `llvm-objdump -d` shows `LDADD`/`CAS`, not `LDXR`/`STXR`.

---

## Presenting this to the user

1. Show `payload = x; ready = 1;` marked `/* ◄ not TSO; may reorder */`.
2. One paragraph: Apple silicon is weakly ordered; `volatile` is not
   acquire/release; use `atomic_store_explicit(..., release)` then
   `atomic_load_explicit(..., acquire)`.
3. Show the C11 pair and, if they are in asm, `STLR`/`LDAR`.
4. Replace any `LDXR`/`STXR` loop with LSE / C11 atomics.
5. Insist on a TSan run (rec 64) before calling the race fixed.
