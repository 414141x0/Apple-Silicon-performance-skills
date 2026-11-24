<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. Do not copy Guide tables. MIT license. -->

# Flow C: execution steps — contention (no `perf c2c`)

Use to diagnose **false sharing** or **true sharing** of **128-byte**
cache lines in multi-threaded code.

Apple Silicon has **no HITM sampler** and **no `perf c2c`**. Data sources
are:

1. Dual-profile jumpers (1 thread vs `hw.logicalcpu`, same QoS)
2. `ATOMIC_OR_EXCLUSIVE_FAIL` density if the CPU Counters picker offers
   it (Guide Table 8.1 — do not invent encodings)
3. 128 B layout inspection of hot structs
4. System Trace / Points of Interest (blocking, migration)
5. Xcode Thread Sanitizer (Guide 8.3) for *correctness* of the sharing,
   not for cycle attribution

Cross-cluster modified sharing is expensive: the fabric is on the order
of **10×** intra-cluster Shared-LL latency (Guide 7.5; look up Appendix A
for the numbers — do not paste the table). Independent data pools across
clusters are fine; a hot atomic bouncing across clusters is not.

---

## Phase 1 — Collect contention signals

Use **Building block: Contention scan** (Part 4).

This produces:

- 1-thread and N-thread CPU Profiler traces (and, if the picker has
  them, CPU Counters traces with `ATOMIC_OR_EXCLUSIVE_*`)
- a fail-density pair
- a dual-profile jumper list

Pass the user's threshold and N into the building block. Default:
ignore fail-density that does not grow with thread count; ignore
layout suspects whose dual-profile absolute % Δ is below 2 points.

If the workload is not already threaded, **stop** and say so — there is
nothing for this flow to diagnose. Offer Flow D Phase 0 if they expected
it to be threaded.

---

## Phase 2 — Map accessors per suspect line

There is no Pareto-by-HITM table. For each jumper function from Phase 1
whose disassembly or source touches a small shared object:

1. Identify the struct (or global) and **dump field offsets**.
2. Mark every hot access `/* ◄ write */` or `/* ◄ read */`.
3. Classify using the **offset discriminator**:
   - **Same offset** on the 128 B line → **true sharing**
   - **Different offsets** on the same 128 B line → **false sharing**

Use **Building block: resolve address to source** if CPU Profiler did
not populate file:line.

Complement with System Trace when jumpers look like *blocking* (`kevent`,
`ulock_wait`, `os_unfair_lock` in `libsystem`) rather than *bouncing
lines*. TSan (Xcode Product → Scheme → Diagnostics → Thread Sanitizer)
catches unsynchronized true sharing that a cycle profile will not name.

---

## Phase 3 — Gather source code

Gather:

- the functions that access the contended line
- the definition of the data structure (struct, class, global)

Then verify the Phase 2 conclusion by inspecting the **field name**:

- Same field name (or a non-struct variable) → true sharing
- Different field names → false sharing

Offsets must be shown. Compute them (record layouts, `offsetof`,
ctypes); do not guess. Line size is `sysctl -n hw.cachelinesize`
(128 on current chips). Two fields false-share iff
`field_a_offset / 128 == field_b_offset / 128` and the fields are
independent.

---

## Phase 4 — Report findings

Produce a Markdown report. Start with a one-line summary table, then
one detailed section per hot cache line.

### Annotation conventions used throughout the report

**Struct field offsets** — annotate each field with its byte offset
using `/* +0xNN */`. This lets the reader cross-reference fields against
the Offset column without arithmetic. Always show the **complete**
struct definition — no truncation, even for large structs:

```c
struct foo {
    int written_a_lot;      /* +0x00 */
    int innocent_bystander; /* +0x04 */
    int read_a_lot;         /* +0x08 */
};
```

**Access markers** — mark the specific line performing the contested
access with `/* ◄ write */` or `/* ◄ read */`:

```c
    s->written_a_lot++;   /* ◄ write */
    if (s->read_a_lot)    /* ◄ read  */
```

**Function display rules:**

- **≤ 20 lines**: show the entire function body, no truncation.
- **> 20 lines**: show the function signature and opening brace with
  1–2 lines of setup context, then `...`, then at least 3 lines of
  context around each key line (the `/* ◄ */` markers), then `...` if
  more code follows, and always end with the closing `}`:

```c
void long_function(struct example *s, int n)
{
    int i, result = 0;
    ...
    for (i = 0; i < n; i++) {
        s->written_a_lot++;   /* ◄ write */
        result += s->read_a_lot;
    }
    ...
}
```

The goal is that the reader sees enough to understand the calling
convention and the context of the hot access without wading through
unrelated code.

### Report template

````markdown
# Contention analysis — 128 B cache-line report

This report covers the test run with `<command>`.
QoS: `<QOS_CLASS_… or unset>`. Line size: `hw.cachelinesize` = 128 B.
Data sources: dual-profile (1 vs N threads) + ATOMIC_OR_EXCLUSIVE_FAIL
density (if the picker offered it) + layout. **No HITM / no perf c2c.**

## Summary

| # | Object / line | Fail density Δ (1→N) | Dual-profile jumper | Sharing type |
|---|---------------|----------------------|---------------------|--------------|
| 0 | `stats` @ +0x00 / +0x08 | 0.01% → 1.8% | `bump_writes` +10.2% | False sharing |
| 1 | `obj.refcount` @ +0x00 | 0.00% → 0.9% | `retain` +4.1% | True sharing |

---

## Case N: False sharing  (fail-density Δ, jumper %)

### The data structure

`<path/header.h, line NN>`
```c
struct example {
    int written_a_lot;      /* +0x00 */
    int innocent_bystander; /* +0x04 */
    int read_a_lot;         /* +0x08 */
};
```

`written_a_lot` (+0x00) and `read_a_lot` (+0x08) sit on the **same
128 B line** (line 0). Independent variables; writers and readers on
different cores bounce the line through Shared LL (intra-cluster) or
the fabric (cross-cluster, ~10×).

### Accessor 1: `<function_name>` at `<path/file.c:NN>`  (N-thread %)

The field `written_a_lot` (+0x00) is **written** here (function is
short — shown in full):
```c
void write_me(struct example *s)
{
    s->written_a_lot++;   /* ◄ write */
}
```

Called from:

| Caller | Location | % of calls |
|--------|----------|-----------|
| `try_write()` | `path/file2.c:42` | 80% |
| `do_write()` | `path/file2.c:80` | 20% |

### Accessor 2: `<function_name>` at `<path/file.c:NN>`  (N-thread %)

The field `read_a_lot` (+0x08) is **read** here (function exceeds 20
lines — summarized):
```c
bool complex_reader(struct example *s, int flags)
{
    bool result = false;
    ...
    if (flags & FLAG_CHECK) {
        if (s->read_a_lot)    /* ◄ read  */
            result = true;
        else
            result = false;
    }
    ...
}
```

Called from:

| Caller | Location | % of calls |
|--------|----------|-----------|
| `try_read()` | `path/file3.c:12` | 100% |

Different fields (`written_a_lot` vs `read_a_lot`) at different offsets
(0x00 vs 0x08) on the same 128 B line → **false sharing**.

Apply **Resolution strategy: Structured false-sharing fix**
(`patterns/false-sharing.md`). Pad to 128 B, not 64 B:

```c
struct example {
    int written_a_lot;      /* +0x00 */
    char _pad[124];         /* +0x04 .. +0x7F */
    int read_a_lot;         /* +0x80 */
};
/* independently shared fields: alignas(128) each */
```

The dual of false sharing still applies: **pack hot variables that are
used together onto the fewest lines**. Do not pad everything.

---

## Case N: True sharing  (fail-density Δ, jumper %)

### The data structure

`<path/header.h, line NN>`
```c
struct example {
    atomic_int refcount;    /* +0x00 */
};
```

### Accessor 1 & 2: `<function_name>` at `<path/file.c:NN>`  (both at +0x00)

The same field `refcount` (+0x00) is accessed from multiple threads:
```c
void do_something(struct object *obj)
{
    atomic_fetch_add(&obj->refcount, 1);   /* ◄ write */
    if (atomic_fetch_sub(&obj->refcount, 1) == 1)   /* ◄ write */
        free_object(obj);
}
```

Same field (`refcount`) and offset (0x00) → genuine contention on the
same datum → **true sharing**.

**Test-and-Set spin detection** — if the accessor functions contain
`ldxr`/`stxr` or a `cas` retry loop, this may be a Test-and-Set spin
rather than plain data contention. Every waiter competes for exclusive
ownership of the line, including threads that currently have no chance
of acquiring the lock — which also steals the line from the holder.
Apply **TTAS** (`patterns/ttas.md`) **and** Guide 7.3 **block-not-spin**
(`patterns/spin-wait.md`) before considering other strategies. Prefer
`os_unfair_lock`, GCD, pthreads — they are tuned per chip. Home-grown
queued locks are not.

Common resolution strategies for true sharing without the spin pattern:

- **Atomic / lock-free** — `std::atomic` / `_Atomic` with LSE (`ldadd`,
  `cas`) rather than `ldxr`/`stxr`
- **Per-CPU / per-thread sharding** — each thread owns its copy; merge
  at the end (`patterns/per-cpu-stats.md`).
  `pthread_cpu_number_np` is a **heuristic** — the OS may migrate the
  thread at any time
- **Finer lock granularity** — split a coarse lock so fewer threads
  compete
- **Convert to a R/W primitive** — if reads dominate
- **GCD serial queue** — for a single writer-ish object, often cheaper
  than a custom lock on AMP

Recommend the best fit based on data type and access pattern, then stop
and ask the user for further direction.
````

---

## Phase 5 — If no significant entries found

Report explicitly: no contended 128 B lines above the threshold were
found; the workload may not be contention-bound, the recording was too
short, QoS parked everything on E-cores, or `ATOMIC_OR_EXCLUSIVE_*` is
unavailable on this chip. Suggest recording longer, under higher load,
with an explicit `QOS_CLASS_USER_INTERACTIVE`, and continue with Flow B
or Flow D.
