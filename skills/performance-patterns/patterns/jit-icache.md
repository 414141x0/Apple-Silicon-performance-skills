<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->
# Pattern: batched JIT I-cache invalidation

## When to apply

Apply **only when the workload is a JIT** (or other self-modifying
code): a language VM, a regex/query compiler, a tracing JIT, a shader
compiler running on the CPU (Guide rec 9, **Low / Low**).

Do not apply this pattern to ordinary applications. If there is no
`mmap(MAP_JIT)`, no `pthread_jit_write_protect_np`, and no
`sys_icache_invalidate`, stop.

The cost is **I-cache invalidation**. Each publish of new bytes must
make the Instruction Fetch Unit see them, which evicts *other* useful
instructions. Frequent tiny publishes (one stub, one invalidation)
thrash L1I and the I-TLB.

### Source code signals

- `sys_icache_invalidate(p, n)` / `__builtin___clear_cache` inside a
  per-stub or per-bytecode emit path
- `pthread_jit_write_protect_np(0)` … write 8–32 bytes …
  `pthread_jit_write_protect_np(1)` in a tight loop
- A new `mmap(MAP_JIT)` per function
- No stub reuse: every call site gets a freshly emitted trampoline
- Invalidating a whole region for a one-instruction patch

### Profiling signals

- CPU Bottlenecks **Delivery** with L1I / I-TLB pressure correlated
  with JIT publishes, not with the guest loop body
- Time Profiler: `sys_icache_invalidate`, `commpage` JIT write-
  protect, or `mprotect` dominating a “run the script” workload
- Guest code that should be hot is cold in CPU Profiler because it
  was just evicted by an invalidate
- Instruments System Trace: a high rate of JIT write-protect
  transitions

---

## Why this is slow

On Apple silicon, newly written instructions are **not** visible to
fetch until software follows the I-cache protocol (see Apple’s
*Porting Just-In-Time Compilers to Apple Silicon*). One of those steps
invalidates I-cache lines covering the written bytes. Invalidation is
not free: those lines, and often their neighbors, stop being the hot
guest loop.

A JIT that emits a 16-byte stub and immediately invalidates, thousands
of times per second, spends its L1I budget on protocol rather than on
the compiled code. Rec 9 is Low/Low because most programs are not
JITs; when you *are* a JIT it is still a real limiter.

---

## The fix

Batch code generation. Reuse stubs. Follow Apple’s protocol **once
per published region**, not once per instruction.

### 1. Apple JIT I-cache protocol (shape)

```c
#include <libkern/OSCacheControl.h>
#include <pthread.h>
#include <sys/mman.h>
#include <stddef.h>
#include <stdint.h>

enum { JIT_CAP = 1 << 20 };

typedef struct {
    uint8_t *base;
    size_t   used;
    size_t   cap;
} jit_buf_t;

int jit_buf_init(jit_buf_t *j)
{
    void *p = mmap(NULL, JIT_CAP, PROT_READ | PROT_WRITE | PROT_EXEC,
                   MAP_ANON | MAP_PRIVATE | MAP_JIT, -1, 0);
    if (p == MAP_FAILED)
        return -1;
    j->base = p;
    j->used = 0;
    j->cap  = JIT_CAP;
    return 0;
}

/* Emit many stubs, then publish once. */
void *jit_emit_batch(jit_buf_t *j, const uint8_t *bytes, size_t n)
{
    if (j->used + n > j->cap)
        return NULL;                   /* real JITs keep a second buffer */

    pthread_jit_write_protect_np(0);   /* writable, not executable */
    memcpy(j->base + j->used, bytes, n);
    size_t start = j->used;
    j->used += n;
    /* stay writable; more emits may follow in this batch */
    return j->base + start;
}

void jit_publish(jit_buf_t *j, void *start, size_t n)
{
    sys_icache_invalidate(start, n);   /* fetch may now see the bytes */
    pthread_jit_write_protect_np(1);   /* executable, not writable */
}
```

Call `jit_publish` at a **batch boundary** (end of a compilation
unit, end of a trace family), not after each `emit_mov`.

The exact required sequence can change; **follow the current Apple
JIT I-cache protocol** rather than this sketch if they disagree. Do
not invent a `DMB`/`ISB` salad that skips `sys_icache_invalidate`.

### 2. Reuse stubs

```c
/* Before: new trampoline per call site */
void *emit_call_a(jit_buf_t *j) { return jit_emit_batch(j, stub_a, sizeof stub_a); }
void *emit_call_a_again(jit_buf_t *j) { return jit_emit_batch(j, stub_a, sizeof stub_a); }

/* After: intern */
void *stub_a_addr;

void jit_intern_builtins(jit_buf_t *j)
{
    stub_a_addr = jit_emit_batch(j, stub_a, sizeof stub_a);
    /* …more builtins… */
    jit_publish(j, j->base, j->used);
}
```

Patch call sites to `BL stub_a_addr` instead of re-emitting.

### 3. Patch in place, invalidate the patch only

A one-instruction counter patch invalidates **that line**, not the
whole code heap. Do not `sys_icache_invalidate(base, cap)` for an
8-byte write.

### 4. Do not toggle write-protect per store

`pthread_jit_write_protect_np` is a domain switch. Open writable,
emit the whole batch, invalidate, close. Nested toggles in helpers
are a common accidental source of “frequent JIT I-cache
invalidation”.

---

## Verification

1. **This is a JIT** — if the binary has no `MAP_JIT`, you applied
   the wrong pattern. Revert.
2. **Rate** — Instruments: `sys_icache_invalidate` and write-protect
   transitions drop by the batching factor (e.g. 100 stubs → 1
   publish).
3. **Delivery** — L1I / I-TLB pressure in CPU Bottlenecks recedes;
   the guest kernel stays resident.
4. **Correctness** — fetch must never execute a region that has been
   written since the last invalidate. Toggling protect the wrong way
   is a crash, not a slowdown.

---

## Presenting this to the user

1. Confirm the program is a JIT. If not, stop.
2. Show the per-stub `sys_icache_invalidate` marked
   `/* ◄ evicts L1I */`.
3. Describe the protocol: `MAP_JIT` → write-protect off → emit batch
   → `sys_icache_invalidate` on that range → write-protect on. Point
   at Apple’s *Porting Just-In-Time Compilers to Apple Silicon* for
   the authoritative sequence.
4. Show stub reuse / interned builtins so the invalidation rate stays
   low after warmup.
5. Rec 9 is Low/Low — do not drag this into a vDSP or GEMM review.
