<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Flow J: Crash reports, jetsam, watchdog — CLI symbolication

Use when the input is a `.crash` / `.ips` / jetsam JSON, a MetricKit
crash diagnostic, or “the app was killed”. This flow is
**programmatic**: `xcodebuild` dSYM flags, `xcrun crashlog`,
`CrashSymbolicator.py`, `atos`. Do not send the user to Organizer.

## Step 0 — Release must have dSYMs

```bash
# per target, Release
xcodebuild … DEBUG_INFORMATION_FORMAT=dwarf-with-dsym

dsymutil <binary>
# UUID of binary and dSYM must match
dwarfdump -u <binary>
dwarfdump -u <binary>.dSYM
```

Retain the `.xcarchive` for every build you ship. A dSYM from a
different Xcode or different flags will not match, even from the same
sources.

## Step 1 — symbolicate the whole report

Prefer the LLDB helper:

```bash
xcrun crashlog /path/to/report.ips
# already in lldb:
#   (lldb) command script import lldb.macosx.crashlog
#   (lldb) crashlog /path/to/report.ips
```

JSON reports with inlines:

```bash
SYM=$(xcode-select -p)/../SharedFrameworks/CoreSymbolicationDT.framework/Resources
python3 "$SYM/CrashSymbolicator.py" report.ips -d App.app.dSYM -o report.symbolicated.ips
```

Single address (load address from the Binary Images section):

```bash
atos -arch arm64 \
  -o App.app.dSYM/Contents/Resources/DWARF/App \
  -l 0x<loadaddr> -i 0x<pc>
# -i expands inlines; -dedup expands <deduplicated_symbol>
```

On arm64, a bad **instruction** fetch has `pc` equal to the exception
address; `lr` is the caller to `atos`. A bad **data** fetch has `pc` ≠
exception address.

## Step 2 — match the exception (fully symbolicated first)

| Pattern | What it is | Next |
|---------|------------|------|
| `EXC_BREAKPOINT (SIGTRAP)` / ARM `Trace/BPT trap: 5` | Swift runtime trap (force unwrap, overflow, …) | Fix the line in frame 0 |
| `EXC_BAD_INSTRUCTION (SIGILL)` on Intel / Simulator | same Swift trap | same |
| `EXC_CRASH (SIGABRT)` + `Last Exception Backtrace` | ObjC/Swift language exception | read the exception message |
| `EXC_CRASH (SIGKILL)` + `0x8badf00d` | watchdog. `scene-create` = first frame; `scene-update` = later UI | `main-thread-blocking.md` |
| `EXC_BAD_ACCESS` + `KERN_INVALID_ADDRESS` / `KERN_PROTECTION_FAILURE` | bad fetch. `VM Region Info` locates STACK GUARD vs NULL vs PAC | ASan; `integer-isa.md` PAC |
| `… (possible pointer authentication failure)` | PAC fail **or** high bits set by corruption | `ptrauth.h`; do not assume a compiler bug |
| `KERN_INVALID_ADDRESS` at `0x…000bad0` | ObjC concurrent strong-property race | TSan; `atomic` property or serialize |
| `objc_msgSend` / `objc_retain` / `objc_release` / `doesNotRecognizeSelector:` at frame 0 | zombie | Zombies instrument; fix lifetime |
| `EXC_CRASH (SIGABRT)` + `DYLD, … dylib not found` | missing embed | embed the framework; check `ARCHS` after thinning |
| `EXC_RESOURCE` / `MEMORY` | memory limit warning or jetsam cousin | `disk-and-dirty-memory.md` |
| `EXC_ARM_DA_ALIGN` | misaligned **and** unmapped; rare on 64-bit ARM | treat as the underlying bad address |

Watchdog CPU hint: **high** elapsed application CPU → too much work
(all threads). **Low** application CPU + long wall clock → blocked
(network, lock, I/O). Hidden sync I/O:
`String(contentsOf: httpsURL)`, `Data(contentsOf:)`,
`gethostbyname`, `SCNetworkReachabilityGetFlags`,
`NSURLConnection.sendSynchronousRequest`.

## Step 3 — jetsam JSON (no backtrace)

Jetsam files are named `JetsamEvent_<DateTime>`. They have **no**
thread stacks.

```text
memoryStatus.pageSize   # 16384 on Apple silicon — confirm, do not hardcode
processes[].rpages      # resident pages
processes[].reason      # only on the killed process
processes[].lifetimeMax
largestProcess
```

Bytes ≈ `rpages * pageSize`. Reasons:

| `reason` | Meaning |
|----------|---------|
| `per-process-limit` | crossed the per-app (or much smaller extension) cap |
| `vm-pageshortage` | system needed RAM for the foreground app |
| `vnode-limit` | too many open files system-wide |
| `highwater` | a daemon exceeded its expected footprint |
| `fc-thrashing` | file-cache thrash from random mmap I/O |
| `jettisoned` | other |

If `reason` is on **another** process, this crash is not memory
pressure — go back to the `.ips`.

## Step 4 — reproduce with sanitizers (separate from perf tests)

```bash
xcodebuild test -scheme <Scheme> \
  -enableAddressSanitizer YES \
  ENABLE_UNDEFINED_BEHAVIOR_SANITIZER=YES
# TSan is a different run:
xcodebuild test -scheme <Scheme> -enableThreadSanitizer YES
```

Do not combine sanitizers with Flow H performance metrics. Guard
Malloc / Zombies are scheme diagnostics; enable them for a debug
repro, not for `xctrace` ISA work.

## Step 5 — lldb without the GUI (optional)

If the process is already under lldb:

```
(lldb) v self.field          # frame variable; no expressions
(lldb) p expr                # compiles; use for computed properties
(lldb) po object             # debugDescription
(lldb) detach                # let the OS write the crash report
```

Protocol-typed values: `v` succeeds when `p`/`po` fail.

## Hand-off

| After symbolication | Skill |
|---------------------|--------|
| PAC / arm64e / `ptrauth_strip` | `apple-silicon-codegen` `integer-isa.md` |
| Watchdog / main-thread I/O | `patterns/main-thread-blocking.md` |
| Dirty growth / jetsam | `patterns/disk-and-dirty-memory.md` |
| Hot inner loop in the stack | Flows A–B + `performance-patterns` |
