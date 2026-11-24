<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->

# Flow H: XCTest performance tests (`xcodebuild`)

Use when the user wants a **repeatable, CI-able** measurement of a
function or feature: CPU, wall-clock, memory, disk writes, or a
signpost interval. This is the regression gate. Flows A–F remain the
ISA microscope.

## Trigger

- “performance test”, “XCTCPUMetric”, “set a baseline”, “catch a
  regression”
- Disk-write or memory growth in a known feature
- Need to fail CI when a kernel gets slower

## Configuration (required)

Measure **Release**. Debug, coverage, and sanitizers distort the
numbers.

```bash
xcodebuild test \
  -scheme <Scheme> \
  -destination 'platform=macOS' \
  -configuration Release \
  -enableCodeCoverage NO \
  ENABLE_TESTABILITY=NO
```

For iOS use a physical device destination when the metric needs
hardware (CPU Counters, storage). Simulator is acceptable for a first
`XCTClockMetric` smoke test only.

Scheme / test-plan flags the agent sets via `xcodebuild` or the
`.xctestplan` JSON, not by clicking:

| Setting | Measure | Profile (`xctrace`) |
|---------|---------|---------------------|
| Debug executable | **Off** | Off |
| Code coverage | **Off** | Off |
| ASan / TSan / UBSan | **Off** | Off (use a separate test run) |
| Thread Performance Checker | **On** (as failure) for hang/QoS tests | **Off** — it perturbs `xctrace` |

A failing Thread Performance Checker hit (priority inversion from
`dispatch_semaphore_wait` / `dispatch_group_wait` QoS mismatch, or
main-thread I/O) is a `main-thread-blocking.md` / `amp-work-partition.md`
signal, not a CPU Bottlenecks signal.

## Metrics

```swift
import XCTest

final class KernelPerfTests: XCTestCase {
    func testDot() {
        let opts = XCTMeasureOptions()
        opts.iterationCount = 10
        measure(metrics: [XCTCPUMetric(), XCTClockMetric()], options: opts) {
            dot(a, b, n)
        }
    }

    func testMemory() {
        measure(metrics: [XCTMemoryMetric()]) {
            loadFeature()
        }
    }

    func testDisk() {
        measure(metrics: [XCTStorageMetric()]) {
            persistDocuments()
        }
    }

    func testSignpostInterval() {
        measure(metrics: [XCTOSSignpostMetric(
            subsystem: "app", category: "launch", name: "FirstScreen")]) {
            loadFirstScreen()
        }
    }
}
```

| Metric | What it records | Next if it regresses |
|--------|-----------------|----------------------|
| `XCTClockMetric` | wall time | Flow I / `main-thread-blocking.md` if it is UI |
| `XCTCPUMetric` | CPU time / instructions | Flow A |
| `XCTMemoryMetric` | peak + growth | `disk-and-dirty-memory.md` |
| `XCTStorageMetric` | filesystem blocks written | `disk-and-dirty-memory.md` |
| `XCTOSSignpostMetric` | one `os_signpost` / `mxSignpost` interval | Flow I around that interval |

Set a baseline from the test report (`xcresult`). Subsequent runs fail
when the metric moves significantly. Do not set the baseline from a
Debug run.

## Isolate the interval

Prefer a signpost around the kernel, not a whole `app.launch()`. CPU
Profiler / Processor Trace then Charge to that interval (Flow I).

```swift
import os
let log = OSLog(subsystem: "bench", category: "dot")

func testIsolatedDot() {
    measure(metrics: [XCTCPUMetric(), XCTClockMetric()]) {
        os_signpost(.begin, log: log, name: "dot")
        dot(a, b, n)
        os_signpost(.end, log: log, name: "dot")
    }
}
```

## Hand-off

A failing performance test is a **trigger**, not a diagnosis. Record
CPU Bottlenecks (Flow A) or CPU Profiler (Flow B) on the same Release
binary with `-g` + dSYM. Then `performance-patterns`.
