<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Pattern: main-thread blocking (hangs, hitches, launch, watchdog)

## When to apply

Apply when the **main run loop** does work that belongs on a queue:
sync I/O, sync JSON, `Task { blocking() }` on the MainActor, `+load` /
constructor work before first draw, or a QoS inversion that stalls UI.

This is an app-level pattern. ISA rewrites of a kernel that already
runs off the main thread belong in the other catalog entries.

### Source signals

- `Data(contentsOf: url)`, `String(contentsOf: url)`,
  `NSData(contentsOf:)`, `XMLParser(contentsOf:)` with an `https` URL
- `NSURLConnection.sendSynchronousRequest`, `URLSession` used via
  a semaphore wait on the main thread
- `gethostbyname`, `gethostbyaddr`, `getaddrinfo` without
  `AI_NUMERICHOST`, `SCNetworkReachabilityGetFlags`
- `dispatch_sync` onto the main queue from work the main thread is
  waiting on (deadlock or hang)
- `dispatch_semaphore_wait` / `dispatch_group_wait` on main, or a
  high-QoS waiter blocked on a low-QoS worker (priority inversion)
- Swift: `Task { syncWork() }` from a MainActor context; missing
  `Task.detached` / `nonisolated` async
- `__attribute__((constructor))`, `+load`, heavy `AppDelegate` /
  `App.init` work, static initializers touching disk or the network
- Vision / RealityKit / Core Data **sync** loaders on main
  (`Entity.load` instead of `loadAsync`, lightweight migration)

### Profiling / crash signals

- Hang rate: main run loop busy **>250 ms** (MetricKit / Hangs track)
- Hang reports with stacks: busy **≥1 s**
- Hitch rate >10 ms/s (warn 25, critical 50). 120 Hz budget 8.3 ms,
  60 Hz 16.7 ms. Animation: keep main-thread work **<5 ms**
- Crash: `EXC_CRASH (SIGKILL)` + Termination Reason `0x8badf00d`.
  `scene-create` = first frame; `scene-update` = later UI
- Thread Performance Checker: main-thread I/O / networking; QoS
  inversion
- App Launch template: time-to-first-draw dominated by
  `+load` / dyld / your app-delegate, not by the first view body

## Why this is slow

The main thread is the only thread that can commit UI. While it is in
`recv`, `fsync`, JSON parse, or a semaphore, touches and display
links queue. The watchdog (iOS; macOS has no launch-time watchdog)
kills the process after a wall-clock allowance; the stack at kill
time is often **not** the work that burned the budget (that work
already returned).

AMP makes a QoS inversion worse: a `userInteractive` waiter on
`dispatch_semaphore_wait` will not donate as expected to a
`background` worker, so the UI thread sits on a P core **blocked**
while the work runs on E — or does not run.

## The fix

### 1. Async I/O, never sync-on-main

```swift
// Before — hidden synchronous network on main
let text = try String(contentsOf: url)      // hang / 0x8badf00d

// After
let (data, _) = try await URLSession.shared.data(from: url)
let text = String(decoding: data, as: UTF8.self)
```

Reachability: `NWPathMonitor.start(queue:)` on a background queue,
not `SCNetworkReachabilityGetFlags`. DNS: let `URLSession` do it.

### 2. Swift concurrency: do not hop blocking work onto MainActor

```swift
// Before — Task {} inherits MainActor; syncWork() hangs later
Task { syncWork() }

// After
Task.detached(priority: .userInitiated) {
    let value = syncWork()
    await MainActor.run { apply(value) }
}
```

`nonisolated` async methods (Swift 5.7+) already hop off MainActor.
A `Task { await nonisolatedAsync() }` from UI is fine; a `Task` that
calls **sync** MainActor work is not.

### 3. First draw: defer static work

- Delete `+load` and `__attribute__((constructor))` unless dyld
  truly must run them. Move to first use or `trackLaunchTask`.
- App-delegate / `App.init`: register, then return. Load caches after
  first frame (`MXMetricManager` `trackLaunchTask` or Points of
  Interest signpost).
- Prefer mergeable libraries over a pile of dylibs that all run
  initializers at launch.

### 4. Priority inversion

Do not wait on a lower-QoS worker from a higher-QoS thread. Lift the
worker’s QoS, use a GCD queue whose QoS matches the waiter, or use
`os_unfair_lock` for a short critical section (`spin-wait.md`,
`amp-work-partition.md`).

## Verification

1. XCTest `XCTClockMetric` / `XCTOSSignpostMetric` around the
   interaction (Flow H), Release configuration.
2. `xcrun xctrace record --template 'App Launch'` and
   `'Animation Hitches'` (Flow I).
3. MetricKit hang rate → 0 s/h; hitch rate ≤10 ms/s (Flow G).
4. Thread Performance Checker test-plan run: no main-thread I/O, no
   inversion.
5. Watchdog crashes gone; if one remains, read elapsed application
   CPU (high = too much work, low = blocked) from the report (Flow J).

## Presenting this to the user

1. Mark the sync call / `Task { syncWork() }` / `+load`.
2. One paragraph: main thread is the frame budget; 250 ms is a hang;
   0x8badf00d is the watchdog.
3. Show the async / `Task.detached` / deferred-init replacement.
4. Do not dive into NEON until the work is off the main thread.
