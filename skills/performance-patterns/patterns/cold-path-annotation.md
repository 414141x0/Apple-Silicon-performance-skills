<!-- Structure derived from Intel Performance Skills (MIT, 2026).
     Optimization rules are original paraphrases of Apple Silicon CPU
     Optimization Guide 5.0. MIT license. -->

# Pattern: cold-path annotation

## When to apply

Apply when a **hot function** calls other functions that are only reached on
rarely-taken branches. Marking those callees `[[gnu::cold]]` (or
`__attribute__((cold))`) tells the compiler that the branch leading to them is
almost never taken. The compiler reorders code so the cold callees sit far
from the hot path — improving instruction-cache density and generating
branch-predictor-friendly constructs for the caller.

The benefit is **indirect**: the cold function itself does not speed up. The
caller gets faster because its hot-path instructions are no longer interleaved
with rarely-executed cold-path instructions.

Guide 5.4.2: keep hot portions of functions in a small set of cache lines.
Pages are **16 KiB**; L1I set conflicts are more likely for page-aligned
dylibs. Do **not** NOP-align branch targets (Guide rec 22) as a substitute
for this annotation.

### When is a callee cold?

A callee qualifies as cold when it is reached on a **rarely-taken branch** at
its call site — a branch probability below roughly 0.1%:

```c
if (likely_condition) {         /* taken ~99.9%+ of the time */
    hot_path();
} else {
    cold_callee();              /* ← cold: rarely reached */
}
```

### Source code signals

Scan the callees of any function identified as hot. A callee is a cold candidate
when it matches any of these patterns:

**By purpose:**
- Error reporters: functions whose entire job is to log or surface a failure
  (`report_error`, `log_warn`, `fatal`, `die`, `panic`, `oom_handler`, …)
- Impossible-state handlers: assertions, contract violations, `abort()` wrappers
- Setup/teardown paths called once per process or once per connection
  (`init_once`, `cleanup`, `teardown`, …)
- Very rare corner-case handlers with a clear "this should almost never happen"
  comment

**By body:**
- Body contains only (or primarily): `fprintf`, `fputs`, `perror`, `syslog`,
  `os_log`, `exit`, `abort`, `throw`, or a combination of these
- Body constructs an error message string (`snprintf`)
- Body is a single call to another cold candidate

**By call site:**
- Called only inside `if (ret < 0)`, `if (errno != 0)`, `if (!ptr)`,
  `if (n > max_allowed)`, or similar guard conditions
- Call is inside a `default:` case of a `switch` that handles unexpected values
- Call is guarded by an assertion macro or a bounds check

---

## Why the hot path suffers without it

Without the annotation, the compiler interleaves cold-path instructions with
hot-path instructions in program order, which pollutes the instruction cache
(cold code evicts useful hot-path lines) and denies the compiler the chance to
emit a branch sequence the CPU's static predictor recognizes as "almost never
taken."

On 16 KiB pages, a page-aligned dylib can land many functions in the same L1I
sets (Guide 5.4.2). Pulling cold code out of the hot function's lines is the
layout lever you actually have; NOP-padding branch targets is not.

---

## The fix

### Syntax

Prefer `[[gnu::cold]]` in modern C++ (C++11 and later); use
`__attribute__((cold))` for older standards or plain C:

```cpp
// Modern C++
[[gnu::cold]]
void report_error(std::string_view message)
{
    std::cerr << "error: " << message << '\n';
}

// Older C or C++ standards
__attribute__((cold))
static void report_error(const char *message)
{
    fprintf(stderr, "error: %s\n", message);
}
```

Place the annotation on the **function definition**. Adding it to a declaration
is allowed but has no effect unless the definition is also annotated.

### Applying to a hot function's callees

When you have identified a hot function, scan each of its direct callees:

1. Identify all callees that match the cold-candidate signals above.
2. Annotate each cold callee at its definition.
3. If a cold callee itself calls further error-only helpers, annotate those too
   (the annotation does not automatically propagate to callees of callees).

**Example — before:**

```cpp
void process(const Record *records, int n) {
    for (int i = 0; i < n; i++) {
        if (records[i].magic != MAGIC) {
            report_corruption(records[i]);   // cold callee, not annotated
            continue;
        }
        do_work(records[i]);                 // hot path
    }
}

void report_corruption(const Record &r) {
    fprintf(stderr, "bad magic 0x%x at record %d\n", r.magic, r.id);
}
```

**After:**

```cpp
[[gnu::cold]]
void report_corruption(const Record &r) {
    fprintf(stderr, "bad magic 0x%x at record %d\n", r.magic, r.id);
}

/* process() is unchanged; the benefit comes from annotating the callee */
```

---

## Verification

### Confirm candidate branches are truly cold (data-driven)

If you have a binary compiled with `-g`, use CPU Profiler samples on the
branch, or the GCC dump parser shipped with `apple-silicon-profile`:

```bash
python3 skills/apple-silicon-profile/tools/gccbranchprob.py foo.c foo process
```

Look at the `Taken%` / `GCC Est%` column for branches whose target is the
candidate callee. A taken rate below ~0.1% is strong evidence the callee is
cold.

**No workload? Clang PGO or GCC's static estimate.**

```bash
# Clang PGO
clang -O3 -fprofile-instr-generate -g -o foo foo.c
# run a representative workload, then:
xcrun llvm-profdata merge default.profraw -o foo.profdata
clang -O3 -fprofile-instr-use=foo.profdata -S -o after.s foo.c

# GCC static estimate (ISA-neutral dump)
gcc -g -fdump-tree-profile_estimate-lineno -dumpdir dump/ ... foo.c
python3 skills/apple-silicon-profile/tools/gccbranchprob.py foo.c dump/ process
```

A `GCC Est%` near 0% means the compiler already expects the branch to be
almost never taken.

### Confirm the annotation moved code out of the hot path

Build with `-O2` (or `-O3`) and compare assembly:

```bash
clang++ -O2 -S -o before.s before.cpp
clang++ -O2 -S -o after.s after.cpp
llvm-objdump -d after.o
```

In `after.s`, the hot path of `process()` should be a tight loop with the
cold branch jumping forward, and `report_corruption` should appear after
the function's main return — not interleaved within the loop.

---

## Presenting this to the user

1. Show the hot function with its callees identified and the cold candidates
   highlighted.
2. Explain that the annotation benefits the **caller**, not the cold function
   itself — the compiler will move the cold code out of the hot loop's
   instruction stream.
3. Show the annotated version of each cold callee (the hot function body is
   unchanged).
4. Mention 16 KiB pages / L1I set conflicts and that NOP-aligning branch
   targets is the wrong tool.
5. Optionally show the before/after assembly diff for the hot function.
