<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->

# Flow G: MetricKit in-process collection

Use when the user needs **shipping-device** metrics (CPU, hang, hitch,
launch, memory, disk writes, terminations, energy) without sitting in
Instruments. MetricKit is code. It does **not** deliver on Simulator.

ISA / Useful-bandwidth work stays in Flows A–F. This flow is the
app-level counterpart.

## Trigger

- “MetricKit”, “Organizer metrics”, “hang rate in the field”, “disk write
  exception”, “cpu exception”, “launch time on devices”
- Need a JSON payload an agent can parse or a backend can ingest
- Need per-app-state breakdown (`StateReporter`)

## Constraints

- Physical device. Simulator produces nothing.
- `metricReports` arrive about **once per day**. `diagnosticReports`
  (crash / hang / CPU exception / disk-write exception / memory
  exception / app-launch diagnostic) arrive when the system has one.
- Do not use `OSSignposter` for MetricKit interval metrics. Use
  `mxSignpost`.
- Keep payloads free of privacy-sensitive strings.

## Step 1 — subscribe at launch

```swift
import MetricKit

final class MetricsGate: NSObject, MXMetricManagerSubscriber {
    static let shared = MetricsGate()

    func start() {
        MXMetricManager.shared.add(self)
    }

    func didReceive(_ payloads: [MXMetricPayload]) {
        for payload in payloads {
            encodeAndUpload(payload)          // Codable
        }
    }

    func didReceive(_ payloads: [MXDiagnosticPayload]) {
        for payload in payloads {
            encodeAndUpload(payload)
        }
    }
}
```

On modern SDKs the types are `MetricManager` / `MetricReport` /
`DiagnosticResult` with `metricReports` and `diagnosticReports`. Use
whatever the deployment SDK exports; the subscribe-and-encode loop is
the same.

Call `start()` from `application(_:didFinishLaunchingWithOptions:)` or
the SwiftUI `App` init. Do not wait for a user gesture.

## Step 2 — what to read from a payload

| Need | MetricKit field (names vary by SDK; match the header) |
|------|--------------------------------------------------------|
| CPU | `cpuMetrics` / `CPUTimeMetric`, `CPUInstructionsCountMetric` |
| GPU | `GPUTimeMetric` |
| Display | `PixelLuminanceMetric` |
| Network | `TotalWiFi{Up,Down}loadMetric`, `TotalCellular{Up,Down}loadMetric` |
| Location | `LocationActivityTimeMetric` |
| Launch | `TimeToFirstDrawMetric`, `ApplicationResumeTimeMetric` |
| Hang | hang rate (seconds/hour of main-run-loop busy **>250 ms**) |
| Hitch | hitch rate (ms/s). Thresholds: good ≤10, warn ≤25, critical ≤50 |
| Memory | peak + memory at suspension |
| Disk | logical writes / day; disk-write exception stacks |
| Terminations | `ForegroundTerminationMetric`, `BackgroundTerminationMetric` |
| Diagnostics | crash, hang, cpuException, diskWriteException, appLaunch, memoryException |

Hand a **hang** or **cpuException** stack to Flow J (symbolicate) then
to `performance-patterns`. High CPU time + high Useful in a later
Instruments session is an algorithm problem (Guide rec 1), not a
MetricKit problem.

## Step 3 — signposts the payload can see

```swift
import MetricKit
import os

let log = MXMetricManager.makeLogHandle(category: "launch")

func loadFirstScreen() {
    mxSignpost(.begin, log: log, name: "FirstScreen")
    defer { mxSignpost(.end, log: log, name: "FirstScreen") }
    // work
}
```

`SignpostIntervalMetric` then reports CPU, writes, memory, hitch for
that interval. For **extended launch** (work after first draw) wrap the
task with `MXMetricManager.makeTaskingSupport().trackLaunchTask { … }`
or the current `trackLaunchTask` spelling.

## Step 4 — app state (optional)

If the app has a small set of modes (logged-out / browsing / playback),
report transitions so daily metrics split by state:

```swift
StateReporter.reportTransition(domain: "session", state: "playback")
```

Cap the number of states. If `hasExceededStateLimit` is true, stop
adding dimensions. Encode with
`JSONEncoder.MetricKit.encodingFormatKey = .byStateReportingDomain`
when the SDK offers it.

## Step 5 — agent output

Write the JSON to a file the user named, or print a one-screen table:

```markdown
## MetricKit — last payload

| Metric | Value | Action |
|--------|-------|--------|
| Time to first draw (p50) | … ms | Flow I App Launch if > budget |
| Hang rate | … s/h | `main-thread-blocking.md` if > 0 |
| Hitch rate | … ms/s | Flow I Animation Hitches if > 10 |
| Peak memory | … B | `disk-and-dirty-memory.md` |
| Disk writes / day | … B | `disk-and-dirty-memory.md` |
```

Do not tell the user to open Organizer. If they already have Organizer
numbers, treat them as the same metrics and proceed to a local
`xctrace` / XCTest reproduction.

## Hand-off

| Payload says | Next |
|--------------|------|
| High CPU time / instruction count | Flow A, then `performance-patterns` |
| Hang / hitch / slow launch | Flow I + `patterns/main-thread-blocking.md` |
| Memory / jetsam / termination | Flow J jetsam + `patterns/disk-and-dirty-memory.md` |
| Disk-write exception | `patterns/disk-and-dirty-memory.md` |
| Crash diagnostic | Flow J |
