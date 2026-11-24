# Apple Silicon Performance Skills — AI Agent Skills

A collection of AI agent **skills** for CPU performance analysis and optimization on
Apple silicon (macOS / iOS, arm64). The skills give an agent structured, multi-step
workflows for profiling with Instruments / `xctrace`, identifying performance
anti-patterns in source code and profiling output, writing ASIMD / SSVE / SME
kernels, and optimizing benchmarks from the Phoronix Test Suite.

These skills are **not** affiliated with Apple or Intel. They are the
agent knowledge base for Apple silicon CPU optimization. They do not
redistribute Apple's CPU Optimization Guide. See [COPYRIGHT.md](COPYRIGHT.md).

## Example prompts

Once installed, you can talk to the agent naturally. A few examples:

**Profile a workload with Instruments**
> Please profile `./myapp --bench` in Instruments (CPU Profiler / CPU Bottlenecks) and tell me where the time goes.

**Identify why a loop is slow**
> This function is Discarded-bound in CPU Bottlenecks despite low cache misses. What's wrong, and how do I fix it?

**Optimize from source code alone**
> Review this dot-product loop for performance issues:
> ```c
> for (int i = 0; i < n; i++) sum += a[i] * b[i];
> ```
> Check Accelerate / `vDSP_dotpr` before writing NEON.

**128 B false sharing**
> My server gets slower as I add threads. Check whether independent per-thread fields share a 128-byte cache line.

**P/E cores and QoS**
> This OpenMP loop uses `schedule(static)` and does not scale on M-series. Are we stalling on E cores? How should we hint QoS?

**SME engine bounce**
> This GEMM does `SMSTART` and then immediately consumes the result with core ASIMD. What's wrong?

**Phoronix benchmark optimization**
> Profile `pts/compress-zstd` and optimize it. Try Compression.framework before a hand kernel. Report the before/after improvement.

**Write new SIMD code correctly**
> Write a vectorized float array scale with `vfmaq_f32`, 128-byte padding, and `sysctl` feature checks — not AVX2.

**Hangs / launch without clicking Instruments**
> Wire MetricKit, add an XCTest `XCTClockMetric` on first draw, and record `xctrace` App Launch. If the main thread does `String(contentsOf: url)`, move it off main.

---

## Contents

| Directory | Purpose |
|-----------|---------|
| `skills/apple-silicon-profile/` | Data collection skill: Instruments, `xctrace`, CPU Counters, CPU Bottlenecks, hotspot reporting |
| `skills/performance-patterns/` | Pattern detection and fix playbooks: source code and profiling signals |
| `skills/apple-silicon-codegen/` | Writing new integer / ASIMD / SSVE / SME kernels (ISA, families, latencies, engine) |
| `skills/phoronix-test-suite/` | Supporting skill: install, run, and optimize PTS benchmarks on native arm64 |

---

## Skills

### `apple-silicon-profile` — profiling data collection

**Profile Apple silicon workloads with Instruments / `xctrace` and identify performance bottlenecks.**

When the user asks why code is slow, this skill guides data collection from hardware
counters through to annotated hotspot reports. It replaces Linux `perf` on this
platform. It provides CPU Counters, CPU Profiler (cycle PMI — Time Profiler is
wall-clock and is not the ISA tool), CPU Bottlenecks (Useful / Discarded / Delivery /
Processing; Instruments 26+), AMP scaling across S/P/E cores, contention without
`perf c2c`, and SME-engine views.

It also owns shipping-app loops that an agent can run without clicking
Xcode: MetricKit subscribe-and-JSON (Flow G), XCTest `measure` via
`xcodebuild` (Flow H), app-level `xctrace` templates for hangs / hitches /
launch / Allocations / File Activity / Processor Trace / SwiftUI (Flow I),
and crash/jetsam/watchdog symbolication with `atos` / `xcrun crashlog`
(Flow J).

When a pattern is identified, `apple-silicon-profile` hands off to
`performance-patterns` for the fix. When the fix is a new kernel, hand off to
`apple-silicon-codegen`.

Trigger phrases: *"profile"*, *"Instruments"*, *"xctrace"*, *"hotspot"*, *"CPU Bottlenecks"*,
*"MetricKit"*, *"hang"*, *"hitch"*, *"launch time"*, *"dSYM"*, *"0x8badf00d"*,
*"false sharing"*, *"P cores"*, *"E cores"*, *"does not scale"*, *"why is this slow"*,
*"where does time go"*.

---

### `performance-patterns` — pattern detection and fixes

**Detect and fix well-known performance anti-patterns — from source code or profiling output.**

This skill owns the full catalog of patterns with detection signals and fix playbooks.
It works standalone (source review only) or in combination with `apple-silicon-profile`.
Patterns include serial accumulators, 128 B false sharing, taken-branch density and
`CSEL`, GPR↔vector moves, fused `FMLA`/`MADD`, Accelerate-first, AMP work partition
and QoS, spin-wait, weakly ordered memory, `sysctl` hardcoding, store-to-load
forwarding, unaligned streams, software prefetch, JIT I-cache invalidation,
core↔SME engine bounce, main-thread blocking / watchdog, dirty 16 KiB pages and
disk writes, and SwiftUI update cost.

Also provides guidelines for writing new performance-sensitive C/C++ and SIMD code
correctly from the start.

Trigger phrases: *"optimize"*, *"vectorize"*, *"why is this slow"*, *"review for
performance"*, *"improve throughput"*, *"write a fast…"*, any `vld1q` / `vfmaq` /
`FMLA` / `SMSTART` / `vDSP_` / GCD / QoS code.

---

### `apple-silicon-codegen` — writing new kernels

**Write new performance-sensitive integer, ASIMD, SSVE, and SME kernels**
(ISA selection, addressing, register pressure, ZA + Processing Grid, 4 KiB
isolation, chip-family dispatch, class latencies, sysctl constants). Also owns the
Apple ARM64 ABI (`X18`, `long double` == `double`, stack variadics), DIT
(`<timingsafe.h>`), and PAC / `arm64e` / `ptrauth.h`. Invoke when the
user asks to write a kernel, or when `performance-patterns` has ruled out a platform
library and a hand implementation is justified.

Trigger phrases: *"write a NEON…"*, *"SME kernel"*, *"FMOPA"*, *"arm_neon.h"*,
*"intrinsics for…"*, *"arm64e"*, *"PAC"*, *"DIT"*.

---

### `phoronix-test-suite` — supporting skill

**Install, run, and optimize benchmarks from the Phoronix Test Suite on native arm64.**

Invoked automatically when the profiling target is a `pts/<name>` benchmark. Handles
the full lifecycle: install, source extraction, rebuild with `-g` + dSYM, binary
deployment, and result recording. Optimization hands off to `apple-silicon-profile`,
`performance-patterns`, and `apple-silicon-codegen`. Tries Accelerate / Compression
before a BLAS or gzip rewrite. Aborts if `sysctl.proc_translated == 1`.

Trigger: any `pts/<name>` reference, or the words *"phoronix"* / *"phoronix-test-suite"*.

---

## Installation

This skill collection follows the open [Agent Skills standard](https://agentskills.io).
Each skill directory contains a `SKILL.md` file that any compatible agent loads on
demand. The same directories work across GitHub Copilot (CLI and VS Code), Claude Code,
OpenAI Codex, Gemini CLI, and other compatible agents.

| Agent | Project-level path | User-level path |
|---|---|---|
| GitHub Copilot CLI / VS Code | `.github/skills/` | `~/.copilot/skills/` |
| Claude Code | `.claude/skills/` | `~/.claude/skills/` |
| OpenAI Codex | `.agents/skills/` | `~/.agents/skills/` |
| Gemini CLI | `.gemini/skills/` | — |
| OpenCode | `.opencode/skills/` | `~/.config/opencode/skills/` |

### GitHub CLI (`gh skill`) — recommended

The easiest way to install across any supported agent. Requires
[GitHub CLI v2.90.0](https://github.com/cli/cli/releases/tag/v2.90.0) or later.

```bash
# Install all skills (user-level). Replace OWNER/REPO with this repository.
gh skill install OWNER/REPO apple-silicon-profile
gh skill install OWNER/REPO performance-patterns
gh skill install OWNER/REPO apple-silicon-codegen
gh skill install OWNER/REPO phoronix-test-suite
```

Keep them up to date:

```bash
gh skill update apple-silicon-profile
gh skill update performance-patterns
gh skill update apple-silicon-codegen
gh skill update phoronix-test-suite
```

### GitHub Copilot CLI

Skills are installed per-user under `~/.copilot/skills/`:

```bash
cp -r skills/apple-silicon-profile ~/.copilot/skills/
cp -r skills/performance-patterns ~/.copilot/skills/
cp -r skills/apple-silicon-codegen ~/.copilot/skills/
cp -r skills/phoronix-test-suite ~/.copilot/skills/
```

### GitHub Copilot in VS Code

GitHub Copilot in VS Code loads skills from `.github/skills/` at project level or
`~/.copilot/skills/` at user level. Project-level skills can be committed so the
whole team benefits automatically:

```bash
# Project-level (commit to your repository)
mkdir -p .github/skills
cp -r skills/apple-silicon-profile skills/performance-patterns \
      skills/apple-silicon-codegen skills/phoronix-test-suite \
    .github/skills/

# User-level (available in every project)
cp -r skills/apple-silicon-profile skills/performance-patterns \
      skills/apple-silicon-codegen skills/phoronix-test-suite \
    ~/.copilot/skills/
```

VS Code also scans `.claude/skills/` and `.agents/skills/`, so a single project-level
installation covers GitHub Copilot, Claude Code, and Codex simultaneously.

### Claude Code

Claude Code discovers skills in `.claude/skills/` (project) or `~/.claude/skills/`
(user):

```bash
# Project-level
mkdir -p .claude/skills
cp -r skills/apple-silicon-profile skills/performance-patterns \
      skills/apple-silicon-codegen skills/phoronix-test-suite \
    .claude/skills/

# User-level
cp -r skills/apple-silicon-profile skills/performance-patterns \
      skills/apple-silicon-codegen skills/phoronix-test-suite \
    ~/.claude/skills/
```

### OpenAI Codex

Codex reads skills from `.agents/skills/` at project level and `~/.agents/skills/`
at user level:

```bash
# Project-level
mkdir -p .agents/skills
cp -r skills/apple-silicon-profile skills/performance-patterns \
      skills/apple-silicon-codegen skills/phoronix-test-suite \
    .agents/skills/

# User-level
cp -r skills/apple-silicon-profile skills/performance-patterns \
      skills/apple-silicon-codegen skills/phoronix-test-suite \
    ~/.agents/skills/
```

### Gemini CLI

Gemini CLI reads project skills from `.gemini/skills/`:

```bash
mkdir -p .gemini/skills
cp -r skills/apple-silicon-profile skills/performance-patterns \
      skills/apple-silicon-codegen skills/phoronix-test-suite \
    .gemini/skills/
```

### OpenCode

OpenCode has native skill support and reads skills from `.opencode/skills/`:

```bash
mkdir -p .opencode/skills
cp -r skills/apple-silicon-profile skills/performance-patterns \
      skills/apple-silicon-codegen skills/phoronix-test-suite \
    .opencode/skills/
```

For user-level installation, copy to `~/.config/opencode/skills/` instead.

---

## License

Released under the [MIT License](COPYRIGHT.md). Copyright (c) 2026 the skill authors.

Directory layout and workflow structure are derived from Intel Corporation's
"Intel Performance Skills" (MIT License, 2026). Optimization rules are original
agent instructions. Apple's CPU Optimization Guide is not redistributed here.
This project does not claim Apple copyright on the skills.
