<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->

# Flow I: App-level xctrace (hangs, hitches, launch, memory, disk, power)

Use when the question is **responsiveness or resource use of an app**,
not “why is this inner loop slow”. Inner loops stay on Flows A–F.

Always list templates on this machine first:

```bash
xcrun xctrace list templates
xcrun xctrace list instruments
```

Use the **exact** string `list templates` printed. Names below are the
public Apple names; if this Xcode spells one differently, use that.

## Which template

| Question | Template / instrument | ISA tool? |
|----------|----------------------|-----------|
| Where do **cycles** go? | **CPU Profiler** (Flow B) | yes |
| Useful / Discarded / Delivery / Processing | **CPU Counters** bottlenecks mode (Flow A) | yes |
| Exact call sequence, compiler-generated / ARC | **Processor Trace** | yes, complementary |
| Why is the UI hitching / hanging? | **Animation Hitches**, **Time Profiler**, Hangs (often bundled in CPU Profiler) | no |
| Why is launch slow? | **App Launch** | no |
| SwiftUI body cost | **SwiftUI** | no |
| Heap / dirty growth | **Allocations** | no |
| Logical / physical writes | **File Activity** | no |
| Power by subsystem | **Power Profiler** (iOS 26 / iPadOS 26+; confirm with `list instruments`) | no |

Thread Performance Checker must be **off** for these recordings (it
perturbs scheduling). It stays on for Flow H tests.

## Thresholds (public Apple docs; not Guide Table 8.1)

| Signal | Number | Meaning |
|--------|--------|---------|
| Hang (user) | <100 ms | rarely noticeable |
| Hang (system accounting) | main run loop busy **>250 ms** | counts toward hang rate (seconds/hour) |
| Hang report with stacks | **≥1 s** | what MetricKit / Organizer attach stacks to |
| Hitch rate | ≤10 ms/s good, ≤25 warn, ≤50 critical | milliseconds of missed frames per second |
| Frame budget | 120 Hz = **8.3 ms**, 60 Hz = **16.7 ms** | |
| Animation main-thread work | aim **<5 ms** | leave time for render |
| SwiftUI long body | orange >**500 µs**, red >**1000 µs** | SwiftUI template lanes |
| visionOS motion | RealityKit Trace, not Hitches | |

## Processor Trace (hardware floor)

- Hardware: M4 / A18 / iPhone 16 / iPad Pro M4 and later.
- OS: iOS 18.4+ / macOS 15.4+.
- Instruments 16.3+.
- Overhead typically **<1%**.
- **dSYM required.** Charge/Prune/Flatten are GUI; the agent uses
  `xctrace show --calltree` and `--source`.
- Reports IPC + total instructions + total cycles. Includes ARC and
  C++ synthesized frames.

```bash
xcrun xctrace record \
  --template 'Processor Trace' \
  --output pt.trace \
  --launch -- ./<binary> <args>
xcrun xctrace toc pt.trace
xcrun xctrace show pt.trace --calltree --format markdown
```

If `list templates` has no Processor Trace, the Mac/device is below the
floor. Fall back to CPU Profiler.

## Record recipes

```bash
# Launch (cold). Time to first draw is the budget.
xcrun xctrace record --template 'App Launch' \
  --output launch.trace --launch -- ./<App>.app/Contents/MacOS/<App>

# Animation / scrolling
xcrun xctrace record --template 'Animation Hitches' \
  --attach <App> --time-limit 20s --output hitches.trace

# SwiftUI
xcrun xctrace record --template 'SwiftUI' \
  --attach <App> --time-limit 20s --output swiftui.trace

# Heap
xcrun xctrace record --template 'Allocations' \
  --launch --output alloc.trace -- ./<binary> <args>

# Filesystem
xcrun xctrace record --template 'File Activity' \
  --launch --output files.trace -- ./<binary> <args>

# Overview wall-clock (not for ISA)
xcrun xctrace record --template 'Time Profiler' \
  --attach <App> --time-limit 15s --output time.trace
```

Export:

```bash
xcrun xctrace toc <file>.trace
xcrun xctrace show <file>.trace --format markdown
xcrun xctrace show <file>.trace --calltree --format markdown
```

If a dedicated template is missing, record **Time Profiler** or
**CPU Profiler** (Hangs / Points of Interest are already in that
template on current Xcode) and read those tracks from `toc`.

## App size (CLI, not Instruments)

IPA / xcarchive / APP files are **not** the App Store size. Generate
the thinning report:

```bash
# exportOptions.plist must contain:
#   <key>thinning</key>
#   <string>&lt;thin-for-all-variants&gt;</string>
xcodebuild -exportArchive \
  -archivePath App.xcarchive \
  -exportPath /tmp/thinned \
  -exportOptionsPlist exportOptions.plist
cat /tmp/thinned/App\ Thinning\ Size\ Report.txt
```

Compressed ≈ download; uncompressed ≈ on-device.

## Power

Prefer MetricKit CPU/GPU/display/network metrics (Flow G). If
`list instruments` shows **Power Profiler**:

```bash
xcrun xctrace record --template 'Blank'   # then only if the CLI lets you add Power Profiler
```

On many Xcodes Power Profiler is GUI-added to a Blank template. If the
CLI cannot select it, stop; do not invent recording-options keys. Do
not compare power-impact numbers across device models. A cable-charging
device reports overall system power as 0 — use wireless debugging if
the user needs that number.

## Hand-off

| Recording shows | Pattern / next |
|-----------------|----------------|
| Main thread busy >250 ms, or `0x8badf00d` | `patterns/main-thread-blocking.md` |
| Hitch rate >10 ms/s, commit/render hitch | `main-thread-blocking.md` / `swiftui-update-cost.md` |
| SwiftUI long body / update storm | `swiftui-update-cost.md` |
| Allocations dirty growth, 16 KiB pages | `disk-and-dirty-memory.md` |
| File Activity write storm, `fsync`, plist rewrite | `disk-and-dirty-memory.md` |
| Hot C/ASIMD loop inside the hang | Flow A/B then `performance-patterns` ISA catalog |
