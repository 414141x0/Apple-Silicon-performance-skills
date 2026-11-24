<!-- Structure derived from Intel Performance Skills (MIT, 2026). Original agent instructions. MIT license. -->

# Pattern: SwiftUI update cost

## When to apply

Apply when a SwiftUI `View.body` does real work, when a view stores an
escaping closure that captures `self`, or when an `ObservableObject`
invalidates views that did not read the changed property.

ISA-level SIMD inside a view body is the wrong fix — move the work
out, then optimize it with the rest of this catalog.

### Source signals

- Non-trivial compute, I/O, or layout math in `body`, `init`,
  `onAppear`, `onChange`, `onGeometryChange`, gesture `onChanged`
- `@ObservedObject` / `ObservableObject` with many published fields
  of which `body` reads one
- Stored `@escaping` closure on the view that captures `self` or
  several `@State` values (child builder passed in and held)
- `GeometryReader` / `ScrollViewReader` wrapping a large subtree
  whose state is unrelated to layout
- Frequent `objectWillChange` / assignment that does not change
  pixels

### Profiling signals

- SwiftUI template: **Long View Body Updates** orange >500 µs, red
  >1000 µs
- Update Groups long-lived without long bodies (too many cheap
  updates)
- Hitches track coincident with those updates (Flow I)
- Cause graph: blue (your type) → gray (SwiftUI) → blue (your type)
  — you caused an update you then had to handle

```bash
xcrun xctrace list templates   # expect SwiftUI
xcrun xctrace record --template 'SwiftUI' --attach <App> \
  --time-limit 20s --output swiftui.trace
xcrun xctrace show swiftui.trace --format markdown
```

## Why this is slow

SwiftUI may recreate the view value and re-run `body` many times per
frame. Work in `body` is on the update critical path; at 120 Hz the
frame is 8.3 ms and animation should leave the main thread **<5 ms**
(`main-thread-blocking.md`). An `ObservableObject` that publishes
every field invalidates every reader. An escaping closure that
captures `self` makes the view depend on **every** stored property.

## The fix

### 1. Keep `body` a description

Move compute to a model type. Cache. Run it asynchronously; the view
reads the cached value.

```swift
// Before
var body: some View {
    Text(expensiveDigest(items))     // runs every invalidation
}

// After
@Observable final class Digest {
    private(set) var text = ""
    func refresh(_ items: [Item]) {
        text = expensiveDigest(items)
    }
}
```

Do not put that work in `init`, `onAppear`, or a gesture `onChanged`
either — same update path, same hitch.

### 2. `Observable()` tracks reads

Migrate `ObservableObject` / `@Published` to `@Observable`. The view
then invalidates only when a **read** property changes.

### 3. Do not store escaping view-builder closures

```swift
// Before — captures self; any property change rebuilds Child
struct Parent<Content: View>: View {
    let content: () -> Content
    init(@ViewBuilder content: @escaping () -> Content) {
        self.content = content
    }
    var body: some View { content() }
}

// After — call the builder in init; store the value
struct Parent<Content: View>: View {
    let content: Content
    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }
    var body: some View { content }
}
```

Action closures (`Button`) and `ForEach` id closures stay escaping
by API; still check they do not close over the entire parent.

### 4. Narrow layout readers

Put `GeometryReader` around the views whose layout actually depends
on the size. Threshold `onGeometryChange` so sub-pixel noise does
not rebuild the tree.

## Verification

1. SwiftUI template: long-body lanes empty; update-group duration
   down.
2. Hitch rate ≤10 ms/s (Flow I / Flow G).
3. XCTest `XCTOSSignpostMetric` around the screen (Flow H).

If a remaining long body is a numeric kernel, hand that kernel to
`apple-silicon-codegen` / the SIMD patterns — after it is no longer
in `body`.

## Presenting this to the user

1. Show `body` / stored escaping closure / `@Published` blast radius.
2. One paragraph: updates must fit the frame budget; 500 µs is
   already a long body.
3. Show `@Observable` + stored child value + async cache.
