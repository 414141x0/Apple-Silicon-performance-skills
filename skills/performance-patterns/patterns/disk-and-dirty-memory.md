<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Pattern: dirty pages and disk writes

## When to apply

Apply when the app **touches RAM it does not need** (jetsam, peak
memory, memory-at-suspension) or **writes SSD more than the data
changes** (disk-write exceptions, `XCTStorageMetric` growth).

VM page size is **16 KiB** (`hw.pagesize`, jetsam `pageSize`). SSD
device blocks are often **4 KiB**. Both numbers matter: a 1-byte
store can dirty 16 KiB of RAM; a 1-byte file change can program a
4 KiB NAND page.

### Source signals

- `malloc` / `Data` / images kept after the view that needed them
- Whole-file rewrite of JSON / plist / XML on every keystroke
- `write(atomically: true)` / atomic Foundation writers in a loop
- Rapid create/delete of temp files (iOS: ~8 KB metadata per
  create/delete; rename/move ~16 KB)
- `fsync`, `F_FULLFSYNC` on a path that only needed a write barrier
- SQLite without WAL; `VACUUM`; open/close the DB per query
- Core Data / SwiftData context saved never, or saved every field

### Profiling / crash signals

- MetricKit peak memory / memory at suspension; `EXC_RESOURCE` MEMORY
- Jetsam `rpages * pageSize`; `reason` `per-process-limit` or
  `vm-pageshortage` (Flow J)
- Allocations instrument: dirty anonymous VM growing across a
  generation
- File Activity: many small writes, `fsync`, whole-file replaces
- `XCTMemoryMetric` / `XCTStorageMetric` over baseline (Flow H)

## Why this is slow

Clean anonymous memory is not billed until it is written. The first
store makes the page **dirty**; iOS then owns 16 KiB of RAM for it.
At suspension, dirty memory is what the system uses to rank you for
jetsam.

On SSD, a write stalls subsequent reads of that block. Rewriting a
serialized document copies the unchanged 99%. `fsync` / `F_FULLFSYNC`
flushes the unified buffer cache and wears the device. Atomic writes
create, write, unlink, rename — extra metadata writes.

## The fix

### 1. Do not dirty pages you will not read

- Allocate, then fill what you use. Do not `memset` a 64 MiB scratch
  buffer “to be safe” if the kernel will only touch 4 KiB.
- Drop view-backed images / scenes in `applicationDidEnterBackground`.
- Transform network / photo images with Image I/O to the displayed
  size **before** they become dirty bitmaps.
- Purge caches the user has scrolled past; do not keep an unbounded
  dictionary of every loaded record.

### 2. Batch writes; do not rewrite the universe

```c
#include <fcntl.h>
#include <unistd.h>

void commit_barrier(int fd)
{
    /* persistence barrier — not a full media flush */
    fcntl(fd, F_BARRIERFSYNC);
}

void commit_media(int fd)
{
    /* only when the user would lose money on power loss */
    fcntl(fd, F_FULLFSYNC);
}
```

Prefer SwiftData / Core Data / SQLite for documents that change.
Keep a static plist for bundle metadata.

### 3. SQLite that does not thrash NAND

```sql
PRAGMA journal_mode=WAL;
PRAGMA auto_vacuum=2;          -- incremental
-- later:
PRAGMA incremental_vacuum;
```

- Leave the connection open. Open/close checkpoints the world.
- Wrap related `INSERT`/`UPDATE`/`DELETE` in one transaction.
- Index columns you `ORDER BY` / `WHERE`; `EXPLAIN QUERY PLAN`
  must not say `USE TEMP B-TREE FOR ORDER BY` on a hot query.
- Do not `VACUUM` a live database from the app.

### 4. Atomic file APIs are not free

Use atomic writes when a torn file is worse than extra NAND. For a
log or a cache, a single `write` + `F_BARRIERFSYNC` is enough.

Temp files: reuse one scratch file or a RAM cache
(`NSTemporaryDirectory` still hits SSD; `Data` in memory does not).

## Verification

```swift
measure(metrics: [XCTMemoryMetric()]) { loadFeature() }
measure(metrics: [XCTStorageMetric()]) { persist() }
```

```bash
xcrun xctrace record --template 'Allocations' --launch -- ./app
xcrun xctrace record --template 'File Activity' --launch -- ./app
sysctl -n hw.pagesize    # expect 16384; still query
```

Jetsam after the fix: `rpages * pageSize` down; `reason` no longer
your process. MetricKit disk-write exceptions gone.

## Presenting this to the user

1. Show the 1-byte store → 16 KiB dirty, or the JSON rewrite → whole
   file.
2. Quote `hw.pagesize` from sysctl, not a hardcoded 4096.
3. Offer SQLite/WAL or a batched write + `F_BARRIERFSYNC`.
4. Memory and disk trade off — say which one the profile asked to
   spend.
