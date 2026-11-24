<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->
# Pattern: core ↔ SME engine bounce

## When to apply

Apply when a kernel uses **Streaming SVE / SME** and then immediately
depends on the result on the **core** (or the reverse) (recs 17,
50–58). This file is the *anti-pattern*: bouncing dependent data across
the ~20-cycle transit, mixing core ASIMD with ZA on one chain, ping-pong
buffers that alias the same 4 KiB page, unaligned engine stores, and
leaving ZA idle while the grid’s ~4-cycle latency goes uncovered.

The full instruction playbook — ZA tiles, SSVE staging, fusion sequences
— lives in `apple-silicon-codegen`. Load those files when you are
*writing* the kernel:

- `../../apple-silicon-codegen/references/sme-engine.md`
- `../../apple-silicon-codegen/references/sme-ssve.md`

Do **not** copy fusion tables into a patch. Use the codegen refs for
mnemonics.

### Source code signals

- `SMSTART` (or an SME intrinsic / `__arm_streaming`) followed, in the
  same dependent chain, by core ASIMD (`vfmaq_f32`, `vld1q_*`) that
  consumes the engine result
- A loop that alternates “one SSVE/SME op, one core op” on the same
  values (register or memory ping-pong)
- Engine loads/stores to stack slots, or to a buffer the core is also
  touching this iteration
- Engine stores that are not aligned to the engine LSU’s needs; mixed
  core+engine traffic into one array
- Tiny ZA occupancy: one tile/vector busy, the rest of the 4 KiB ZA
  idle, loop not unrolled
- `SMSTART` around a few ops rather than a whole tile of independent
  work
- SSVE-only kernel for a problem that is an outer product / matmul /
  convolution (the grid is the throughput path; SSVE is the support
  unit)

### Profiling signals

- CPU Counters: core waiting on SME register data; SM/ZA enable density
  high relative to useful grid issue
- CPU Bottlenecks: long stalls after `SMSTART`/`SMSTOP` or after the
  first core instruction that reads engine output
- Disassembly: `SMSTART` then, within a handful of insns, `FMOV` /
  `UMOV` / core `LDR` of the same bytes the engine just produced
- Cluster-wide: sibling threads on the same cluster lose frequency
  while any core is in SM/ZA mode — latency-sensitive work running
  next to a streaming kernel

Query `hw.optional.arm.FEAT_SME` first (`sysctl-hardcoding.md`). No SME
→ this pattern does not apply; stay on core ASIMD.

---

## Why this is slow

The SME engine is a **separate** execution complex fed through the
core’s store datapath. Transit is on the order of **20 cycles each
way**. Engine execution is **nonspeculative**. In-flight engine work
therefore trails the core by **tens to thousands of cycles** once
memory and grid latency pile on. A core instruction that consumes an
engine result waits an indeterminate, long time.

Consequences:

- Ping-ponging one vector at a time through that transit is a pipeline
  flush you paid to install.
- Mixing core ASIMD and ZA on the **same dependent chain** serializes
  the slow transit into every iteration.
- Core and engine memory ops to the **same aligned 4 KiB region** are
  ordered conservatively; the core will block.
- Unaligned engine stores turn into read-modify-write on the Shared LL
  Cache.
- A cluster shares frequency. SM/ZA may drop that frequency for
  **every** core in the cluster — do not colocate a latency-critical
  sibling with a streaming kernel.
- SSVE is a wide but modest-throughput support unit (about one 64-byte
  vector per cycle). The **Processing Grid + ZA** is the throughput
  path. SSVE-only is rarely a win against four-wide core ASIMD.

Target **SME-8**. The same binary runs on SME-2, just slower. Do not
write a second kernel “for SME-2” unless codegen says otherwise.

---

## The fix

### Rule of the road

| Role | What belongs there |
|------|--------------------|
| **ZA + Processing Grid** | The throughput path: outer products, tile MMA, long accumulate |
| **SSVE (Z, predicates)** | Staging into/out of ZA, predication, support ops |
| **Core ASIMD** | Everything that cannot use the grid; also pre/post that is *not* on the engine’s critical path |
| **Core integer** | Addresses, predicates, loop control — not the tile math |

Isolate, batch, and do not bounce.

### 1. Two 4 KiB-aligned arenas — no shared page, no stack

**Before** (core and engine share a buffer, then the core reads the
result on the next line):

```c
float buf[N];                 /* also used as scratch on the stack */
smstart_and_accumulate(buf);  /* engine LSU */
float s = 0;
for (int i = 0; i < N; i++)   /* core immediately consumes */
    s += buf[i];
```

**After** — separate arenas, engine produces a whole tile, core
consumes only after `SMSTOP` (or after a large batched store):

```c
#include <stdlib.h>
#include <stddef.h>

enum { ARENA_ALIGN = 4096 };  /* engine hazard granule — not the 16 KiB VM page */

typedef struct {
    void *core;     /* core ASIMD / scalar only */
    void *engine;   /* ZA / SSVE LSU only */
    size_t bytes;
} sme_arenas_t;

int sme_arenas_init(sme_arenas_t *a, size_t bytes)
{
    a->bytes = (bytes + ARENA_ALIGN - 1) & ~(size_t)(ARENA_ALIGN - 1);
    if (posix_memalign(&a->core,   ARENA_ALIGN, a->bytes) != 0)
        return -1;
    if (posix_memalign(&a->engine, ARENA_ALIGN, a->bytes) != 0) {
        free(a->core);
        return -1;
    }
    return 0;
}

void sme_arenas_free(sme_arenas_t *a)
{
    free(a->core);
    free(a->engine);
    a->core = a->engine = NULL;
}
```

- Fill `engine` from the core **once** (initialization), then do not
  touch it from the core until the engine has stored final results.
- Do not point the engine LSU at the stack (`SM=1` loads/stores of ZA/Z
  to SP-relative slots).
- Pad rows so strided (column) engine accesses spread across Shared LL
  banks; prefer padding that keeps **stores** aligned. Do **not** make
  row length a multiple of **256 B**. Padding to `(k × 256 B) + 128 B`
  can also kill unaligned stores; it grows the footprint — experiment
  (`sme-engine.md`).

### 2. No bounce — batch the grid, then extract

```c
/* Shape of a correct kernel (intrinsics/ISA live in codegen):

   SMSTART ZA
   load tiles from engine_arena into Z / ZA     // SSVE supports
   for many k:
       FMOPA / FMLA into as much of ZA as possible
       // pack these compute insns back-to-back — delivery fusion
   extract ZA → Z (extraction unit in parallel with the grid)
   store Z to engine_arena                      // aligned
   SMSTOP

   // only now may the core read engine_arena
*/
```

Do **not**:

```c
SMSTART
one_fmopa
SMSTOP
core_vfmaq_using_that_vector     /* ◄ bounce */
SMSTART
…
```

If you must pipeline core setup with engine compute, overlap *independent*
data: core prepares tile *k+1* in `core` arena while the engine runs tile
*k* in `engine` arena. Never wire them as acc_core = f(acc_engine) inside
the inner loop.

### 3. Cover grid latency with the whole ZA

Nominal grid accumulate latency is ~4 cycles (rec 53;
`apple-silicon-codegen/references/latency-lookup.md`). Unroll and occupy **as many ZA tiles/vectors as the ISA lets
you** so independent FMOPA/FMLA hide that latency. One live accumulator
in ZA is the engine analogue of a serial scalar `sum +=`.

Pack eligible compute instructions **back-to-back** so the core can
delivery-fuse them into the engine. Do not sprinkle core `LDR`/`STR`
between grid ops.

### 4. Alignment, SME-8, frequency

- Align engine data to what the engine LSU wants; **avoid unaligned
  engine stores** even if unaligned loads are tolerated
  (`unaligned-access.md`).
- Compile/tune as if the machine is **SME-8**. SME-2 will run the same
  code at a lower rate.
- Do not schedule a latency-sensitive thread on the same cluster as a
  long SM/ZA kernel; the cluster clock may drop.

### 5. If the algorithm cannot use the grid

Stay on **core ASIMD**. SSVE-only is not a consolation prize for “we
have SME”. Tiles that still fit in **L1D** can be faster as core ASIMD:
the engine LSU talks to Shared LL, not L1D. See `platform-libs-first.md`
— Accelerate already routes GEMM/conv to SME when it pays off.

---

## Verification

1. **Disassembly** — a single `SMSTART`/`SMSTOP` (or equivalent)
   around a dense block of grid ops; no core `FMLA.4S` on the same
   values in the middle. Engine stores aligned.
2. **Arenas** — `otool` / a quick assert:
   `((uintptr_t)engine | (uintptr_t)core) % 4096 == 0` and the ranges
   do not overlap a 4 KiB boundary with each other.
3. **Counters** — waiting-for-SME-register samples drop; grid issue
   rises; SM enable count is once per tile, not once per vector.
4. **Correctness** — compare against a core ASIMD reference. Do not
   require bit-identity with a fused vs unfused core path.
5. Open codegen `sme-engine.md` / `sme-ssve.md` for
   fusion sequences rather than inventing a LUTI chain here.

---

## Presenting this to the user

1. Mark the bounce: `SMSTART` … core consume, or shared buffer, with
   `/* ◄ ~20 cyc transit, nonspeculative trail */`.
2. One paragraph: ZA+grid is throughput; SSVE supports; isolate 4 KiB;
   do not bounce; target SME-8; cover ZA; pack compute; no unaligned
   engine stores; SM/ZA can pull cluster frequency down.
3. Show the two-arena sketch and the “batch then extract” control
   flow. Point at `apple-silicon-codegen` for the actual ISA.
4. If this is GEMM/conv/dot of any size, ask whether Accelerate
   already owns it (`platform-libs-first.md`) before writing SME by
   hand.
5. Query `FEAT_SME` at runtime. No feature bit → no kernel.
