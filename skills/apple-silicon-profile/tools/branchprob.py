#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Original work. Table shape inspired by Intel Performance Skills (MIT, 2026).
"""
branchprob.py — measure branch probabilities on Darwin / Apple Silicon.

Intel BR_INST_RETIRED.NEAR_TAKEN / AT&T ja/jne / x86 macrofusion are not
used here. This tool accepts:

  (a) a CSV of (file, line, taken, not_taken) exported from Instruments
      or any script;
  (b) a Clang/LLVM .profdata (optionally with the instrumented binary, so
      llvm-cov export can map counters onto source branches);
  (c) a pre-rendered `llvm-profdata show --all-functions --counts` dump;
  (d) llvm-cov export JSON;
  (e) a .trace — prints how to export branch samples and still emits the
      table schema (full xctrace XML parse is intentionally not attempted);
  (f) --from-annotate BINARY — greps llvm-objdump/otool for AArch64
      b.<cond> / cbz / tbz. CPU Profiler sample density on the branch vs
      fall-through is the measurement; objdump alone cannot invent Taken%.

Output is a Markdown table on stdout for direct agent ingestion. Progress
goes to stderr.

Do NOT invent Apple PMU event encodings. For a Discarded / mispredict
cross-check, open Apple Silicon CPU Optimization Guide 5.0 Chapter 8
Table 8.1 and pick the branch-mispredict / taken-branch names in the
Instruments CPU Counters picker.

Collection recipe (also printed by --help):

    clang -O2 -g -fprofile-instr-generate -fcoverage-mapping \\
          -mcpu=apple-m4 -o binary source.c
    LLVM_PROFILE_FILE=default.profraw ./binary <representative args>
    xcrun llvm-profdata merge -o default.profdata default.profraw
    python3 branchprob.py --profdata default.profdata --binary ./binary source.c

    # Counters cross-check (picker, no encodings):
    #   Guide Table 8.1 → BRANCH_COND_MISPRED_NONSPEC, INST_BRANCH_TAKEN, …
    #   plus Flow A Discarded fraction.

A Taken% near 0% with enough samples marks the branch *target* as a
cold-path candidate: [[gnu::cold]] / __attribute__((cold)).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COLD_THRESHOLD_DEFAULT = 0.1  # Taken% below this → cold-path candidate
MIN_SAMPLES_DEFAULT = 5

COLLECTION_HELP = """
How to collect branch probabilities on Darwin / Apple Silicon
=============================================================

Preferred — Clang PGO (Darwin-native companion to gccbranchprob.py):

    clang -O2 -g -fprofile-instr-generate -fcoverage-mapping \\
          -mcpu=apple-m1   # or apple-m2 / m3 / m4 / m5
          -o binary source.c
    LLVM_PROFILE_FILE=default.profraw ./binary <representative args>
    xcrun llvm-profdata merge -o default.profdata default.profraw
    python3 branchprob.py --profdata default.profdata --binary ./binary source.c

    # Inspect raw counters without this script:
    xcrun llvm-profdata show --all-functions --counts default.profdata
    xcrun llvm-cov show ./binary -instr-profile=default.profdata

CSV path — if you exported (file, line, taken, not_taken) from Instruments
or a one-off script:

    python3 branchprob.py --csv branches.csv source.c

    CSV header (case-insensitive, extra columns ignored):
        file,line,taken,not_taken[,function[,target_line[,assembly]]]

.trace path — this script will NOT parse xctrace XML (it is large and
brittle). It prints the export recipe and still emits the table schema:

    python3 branchprob.py --trace cpu.trace source.c

    Then, in Instruments / xctrace:
        xcrun xctrace toc cpu.trace
        xcrun xctrace show cpu.trace --track 'CPU Profiler' \\
            --calltree 'Profile' --source NODE --source-mode interleave
      Sample density on a b.<cond>/cbz vs its fall-through is Taken%.
      Write those pairs as CSV and re-run with --csv.

--from-annotate — list AArch64 conditional branches in a binary. Taken%
is left as "—" unless you also pass --samples addr,count CSV. Measurement
is CPU Profiler sample density on the branch vs fall-through, not the
objdump text.

CPU Counters (no encodings):
    Open Guide Chapter 8 Table 8.1. In the Instruments CPU Counters
    picker, select the branch-mispredict / taken-branch *names* this chip
    lists (examples of picker labels, confirm before use:
    BRANCH_COND_MISPRED_NONSPEC, INST_BRANCH_TAKEN, INST_BRANCH,
    BRANCH_MISPRED_NONSPEC). A high Flow A Discarded fraction plus a
    near-50% PGO Taken% on a hot b.cond is the CSEL candidate.

gccbranchprob.py is ISA-agnostic (GCC profile_estimate dumps). Use it
when you cannot run the binary; use this tool when you can.
""".strip()


# AArch64 conditional-branch mnemonics (llvm-objdump / otool, lower-case).
# b.cond plus cbz/cbnz/tbz/tbnz plus FEAT_HBC bc.cond (M5 / A19).
_COND_SUFFIXES = (
    "eq", "ne", "cs", "hs", "cc", "lo", "mi", "pl",
    "vs", "vc", "hi", "ls", "ge", "lt", "gt", "le",
    "al", "nv",
)
COND_BRANCH_MNEMONICS = frozenset(
    [f"b.{s}" for s in _COND_SUFFIXES]
    + [f"bc.{s}" for s in _COND_SUFFIXES]
    + ["cbz", "cbnz", "tbz", "tbnz"]
)

# llvm-objdump function header: "00000001000045c0 <_foo>:" or "<foo>:"
_FUNC_HEADER_RE = re.compile(
    r"^([0-9a-fA-F]+)\s+<([^>+]+)>\s*:"
)
# otool -tv function label: "_foo:" (no address on the same line)
_OTOOL_FUNC_RE = re.compile(r"^([A-Za-z_$.][\w$.]*):$")
# instruction: "    1000045c4:  b.eq    0x1000045e0  ; foo + 32"
_OBJDUMP_INSN_RE = re.compile(
    r"^\s*([0-9a-fA-F]+):\s+(\S+)(?:\s+(.*))?$"
)
# otool -tv: "00000001000045c4    b.eq    0x1000045e0"
_OTOOL_INSN_RE = re.compile(
    r"^([0-9a-fA-F]+)\s+(\S+)(?:\s+(.*))?$"
)

_LLVM_FUNC_RE = re.compile(r"^(\S[^:]*)\s*:\s*$")
_LLVM_FNCOUNT_RE = re.compile(r"Function count:\s*(\d+)")
_LLVM_BLOCKS_RE = re.compile(r"Block counts:\s*\[([^\]]*)\]")


# ---------------------------------------------------------------------------
# Row type
# ---------------------------------------------------------------------------

def _cold_flag(taken_pct, samples, threshold):
    if taken_pct is None:
        return "measure — PGO or CPU Profiler sample density"
    if samples < 1:
        return "no"
    if taken_pct <= threshold:
        return "yes — __attribute__((cold)) / [[gnu::cold]]"
    return "no"


def format_table(rows, src_basename, cold_threshold, min_samples):
    """rows: iterable of dicts with keys
    taken, not_taken, function, file, line, target_line, assembly
    taken/not_taken may be None for --from-annotate / --trace.
    """
    grouped = defaultdict(list)
    for r in rows:
        file_base = os.path.basename(r.get("file") or "") if r.get("file") else ""
        if src_basename and file_base and file_base != src_basename:
            continue
        taken = r.get("taken")
        not_taken = r.get("not_taken")
        if taken is None and not_taken is None:
            samples = r.get("samples") or 0
            taken_pct = None
        else:
            taken = int(taken or 0)
            not_taken = int(not_taken or 0)
            samples = taken + not_taken
            if samples == 0:
                continue
            taken_pct = 100.0 * taken / samples
        if min_samples and samples < min_samples and taken_pct is not None:
            continue
        func = r.get("function") or "?"
        grouped[func].append(
            {
                "taken_pct": taken_pct,
                "samples": samples,
                "line": r.get("line"),
                "target": r.get("target_line"),
                "assembly": (r.get("assembly") or "").strip(),
                "function": func,
            }
        )

    lines = []
    any_data = False
    for func, items in grouped.items():
        items.sort(key=lambda x: -(x["samples"] or 0))
        any_data = True
        lines.append(f"## `{func}`\n")
        lines.append(
            "| Taken% | Samples | Function | Source line | Branch target |"
            " Assembly | Cold-path candidate |\n"
        )
        lines.append(
            "|-------:|--------:|----------|------------:|--------------:|"
            "----------|---------------------|\n"
        )
        for it in items:
            if it["taken_pct"] is None:
                pct_col = "     —"
            else:
                pct_col = f"{it['taken_pct']:6.1f}%"
            src_col = f":{it['line']}" if it["line"] not in (None, "") else "—"
            tgt_col = f":{it['target']}" if it["target"] not in (None, "") else "—"
            asm = it["assembly"] or "—"
            if len(asm) > 60:
                asm = asm[:57] + "..."
            cold = _cold_flag(it["taken_pct"], it["samples"] or 0, cold_threshold)
            lines.append(
                f"| {pct_col} | {it['samples']:>7} | `{it['function']}` |"
                f" {src_col:>11} | {tgt_col:>13} | `{asm}` | {cold} |\n"
            )
        lines.append("\n")

    if not any_data:
        return (
            "*(no sampled branches found — try a longer run, a representative "
            "PGO workload, or lower --min-samples)*\n"
        )
    return "".join(lines)


def schema_only_table(reason):
    body = (
        "## *(no rows — export then re-run)*\n\n"
        "| Taken% | Samples | Function | Source line | Branch target |"
        " Assembly | Cold-path candidate |\n"
        "|-------:|--------:|----------|------------:|--------------:|"
        "----------|---------------------|\n"
        "|      — |       — | — | — | — | — | — |\n\n"
        f"_{reason}_\n"
    )
    return body


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

_CSV_ALIASES = {
    "file": "file",
    "filename": "file",
    "src": "file",
    "source": "file",
    "line": "line",
    "lineno": "line",
    "source_line": "line",
    "taken": "taken",
    "not_taken": "not_taken",
    "nottaken": "not_taken",
    "ntaken": "not_taken",
    "false": "not_taken",
    "function": "function",
    "func": "function",
    "symbol": "function",
    "target": "target_line",
    "target_line": "target_line",
    "branch_target": "target_line",
    "assembly": "assembly",
    "asm": "assembly",
    "samples": "samples",
}


def parse_csv(path):
    rows = []
    with open(path, newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(fh, dialect)
        try:
            header = next(reader)
        except StopIteration:
            return rows
        lowered = [h.strip().lower() for h in header]
        if any(h in _CSV_ALIASES for h in lowered):
            idx = {}
            for i, h in enumerate(lowered):
                if h in _CSV_ALIASES:
                    idx[_CSV_ALIASES[h]] = i

            def get(cols, key, default=None):
                i = idx.get(key)
                if i is None or i >= len(cols):
                    return default
                v = cols[i].strip()
                return v if v != "" else default

            for cols in reader:
                if not cols or all(not c.strip() for c in cols):
                    continue
                rows.append(
                    {
                        "file": get(cols, "file"),
                        "line": _maybe_int(get(cols, "line")),
                        "taken": _maybe_int(get(cols, "taken")),
                        "not_taken": _maybe_int(get(cols, "not_taken")),
                        "function": get(cols, "function"),
                        "target_line": _maybe_int(get(cols, "target_line")),
                        "assembly": get(cols, "assembly"),
                    }
                )
        else:
            # file, line, taken, not_taken [, function [, target_line [, assembly]]]
            fh.seek(0)
            reader = csv.reader(fh, dialect)
            for cols in reader:
                if not cols or cols[0].strip().startswith("#"):
                    continue
                if len(cols) < 4:
                    continue
                rows.append(
                    {
                        "file": cols[0].strip(),
                        "line": _maybe_int(cols[1]),
                        "taken": _maybe_int(cols[2]),
                        "not_taken": _maybe_int(cols[3]),
                        "function": cols[4].strip() if len(cols) > 4 else None,
                        "target_line": _maybe_int(cols[5]) if len(cols) > 5 else None,
                        "assembly": cols[6].strip() if len(cols) > 6 else None,
                    }
                )
    return rows


def _maybe_int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# llvm-profdata show
# ---------------------------------------------------------------------------

def parse_llvm_profdata_show(text):
    """Parse `llvm-profdata show --all-functions --counts`.

    Block counts are *not* source-level taken/not-taken. We emit one
    synthetic row per block so the agent can see relative heat, with
    assembly noting 'llvm block N'. Taken% is block_count / function_count
    (a heat fraction, labelled in assembly so it is not confused with a
    real branch probability).
    """
    rows = []
    current_func = None
    fn_count = None
    for line in text.splitlines():
        m = _LLVM_FNCOUNT_RE.search(line)
        if m and current_func:
            fn_count = int(m.group(1))
            continue
        m = _LLVM_BLOCKS_RE.search(line)
        if m and current_func:
            raw = m.group(1).strip()
            blocks = []
            if raw:
                for part in raw.split(","):
                    part = part.strip()
                    if part:
                        try:
                            blocks.append(int(part))
                        except ValueError:
                            pass
            denom = fn_count or sum(blocks) or 1
            for i, c in enumerate(blocks):
                # Treat "taken" as this block, "not_taken" as the rest of
                # the function count so Taken% ≈ share of function entries
                # that hit the block. Documented as a proxy in assembly.
                rest = max(denom - c, 0)
                rows.append(
                    {
                        "file": None,
                        "line": None,
                        "taken": c,
                        "not_taken": rest,
                        "function": current_func,
                        "target_line": None,
                        "assembly": f"llvm-block[{i}] (proxy; not a source branch)",
                    }
                )
            current_func = None
            fn_count = None
            continue
        # Function header: skip well-known summary keys.
        if not line or line.startswith(" ") or line.startswith("\t"):
            continue
        if line.startswith("Instrumentation") or line.startswith("Functions") \
                or line.startswith("Total functions") \
                or line.startswith("Maximum "):
            continue
        m = _LLVM_FUNC_RE.match(line)
        if m:
            name = m.group(1).strip()
            if name in {"Counters", "Hash", "Block counts"}:
                continue
            current_func = name
            fn_count = None
    return rows


# ---------------------------------------------------------------------------
# llvm-cov export JSON (real per-branch taken / false counts)
# ---------------------------------------------------------------------------

def parse_cov_json(obj, src_basename=None):
    """Parse `llvm-cov export` JSON.

    Each branch is
      [lineStart, colStart, lineEnd, colEnd,
       trueCount, falseCount, fileID, expandedFileID, kind]
    """
    rows = []
    data = obj.get("data") if isinstance(obj, dict) else None
    if not data:
        return rows

    # Map function name → filenames for a best-effort function column.
    func_by_file_line = []
    for unit in data:
        for fn in unit.get("functions") or []:
            name = fn.get("name") or "?"
            filenames = fn.get("filenames") or []
            regions = fn.get("regions") or []
            for reg in regions:
                # region: [startLine, startCol, endLine, endCol, count, fileID, expID, kind]
                if len(reg) >= 1:
                    func_by_file_line.append(
                        (filenames[0] if filenames else None, reg[0], name)
                    )

    def lookup_func(filename, line):
        best = None
        for f, ln, name in func_by_file_line:
            if filename and f and os.path.basename(f) != os.path.basename(filename):
                continue
            if ln is None:
                continue
            if line is not None and ln <= line:
                if best is None or ln > best[0]:
                    best = (ln, name)
        return best[1] if best else None

    for unit in data:
        files = unit.get("files") or []
        for fi in files:
            filename = fi.get("filename") or ""
            if src_basename and os.path.basename(filename) != src_basename:
                # still allow if the user did not pass a source filter later
                pass
            for br in fi.get("branches") or []:
                if len(br) < 6:
                    continue
                line = br[0]
                true_c = int(br[4] or 0)
                false_c = int(br[5] or 0)
                if true_c == 0 and false_c == 0:
                    continue
                rows.append(
                    {
                        "file": filename,
                        "line": line,
                        "taken": true_c,
                        "not_taken": false_c,
                        "function": lookup_func(filename, line),
                        "target_line": None,
                        "assembly": "llvm-cov branch (true=taken, false=not-taken)",
                    }
                )
    return rows


def _tool_argv(name):
    """Return argv prefix to invoke `name`, preferring xcrun."""
    xcrun = shutil.which("xcrun")
    if xcrun:
        return [xcrun, name]
    found = shutil.which(name)
    if found:
        return [found]
    return None


def run_llvm_profdata_show(profdata):
    argv = _tool_argv("llvm-profdata")
    if not argv:
        raise SystemExit(
            "llvm-profdata not found (tried xcrun llvm-profdata and PATH). "
            "Install Xcode / Command Line Tools, or pass "
            "--llvm-profdata-show DUMP.txt."
        )
    proc = subprocess.run(
        argv + ["show", "--all-functions", "--counts", profdata],
        check=False, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"llvm-profdata show failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def run_llvm_cov_export(binary, profdata):
    argv = _tool_argv("llvm-cov")
    if not argv:
        raise SystemExit(
            "llvm-cov not found (tried xcrun llvm-cov and PATH). "
            "Pass --cov-json FILE, or omit --binary to use llvm-profdata show."
        )
    proc = subprocess.run(
        argv + ["export", binary, f"-instr-profile={profdata}"],
        check=False, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"llvm-cov export failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise SystemExit(f"llvm-cov export produced invalid JSON: {e}") from e


# ---------------------------------------------------------------------------
# --from-annotate (AArch64 objdump / otool)
# ---------------------------------------------------------------------------

def parse_objdump(text):
    """Return list of {function, addr, mnemonic, rest, assembly} for
    conditional branches."""
    current_func = None
    branches = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        hm = _FUNC_HEADER_RE.match(stripped)
        if hm:
            current_func = hm.group(2)
            continue
        om = _OTOOL_FUNC_RE.match(stripped)
        if om and not stripped[0].isdigit():
            current_func = om.group(1)
            continue
        im = _OBJDUMP_INSN_RE.match(line) or _OTOOL_INSN_RE.match(line.strip())
        if not im or current_func is None:
            continue
        addr, mnemonic, rest = im.group(1), im.group(2).lower(), (im.group(3) or "")
        # otool sometimes puts the mnemonic as the second token after addr
        # with no colon; _OTOOL_INSN_RE handles that.
        if mnemonic.endswith(":"):
            continue
        if mnemonic not in COND_BRANCH_MNEMONICS:
            continue
        rest_s = rest.strip()
        branches.append(
            {
                "function": current_func,
                "addr": addr.lower(),
                "mnemonic": mnemonic,
                "rest": rest_s,
                "assembly": f"{mnemonic} {rest_s}".strip(),
                "file": None,
                "line": None,
                "taken": None,
                "not_taken": None,
                "target_line": None,
            }
        )
    return branches


def run_objdump(binary):
    argv = _tool_argv("llvm-objdump")
    if argv:
        proc = subprocess.run(
            argv + ["-d", "-C", "--no-show-raw-insn", binary],
            check=False, capture_output=True, text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout
    otool = shutil.which("otool")
    if otool:
        proc = subprocess.run(
            [otool, "-tv", binary],
            check=False, capture_output=True, text=True,
        )
        if proc.returncode == 0:
            return proc.stdout
    raise SystemExit(
        "Neither llvm-objdump nor otool produced disassembly. "
        "Install Xcode / Command Line Tools."
    )


def parse_samples_csv(path):
    """addr,count CSV → {addr_without_0x_lower: count}."""
    out = {}
    with open(path, newline="") as fh:
        reader = csv.reader(fh)
        for cols in reader:
            if not cols or cols[0].strip().startswith("#"):
                continue
            if cols[0].strip().lower() in {"addr", "address"}:
                continue
            if len(cols) < 2:
                continue
            addr = cols[0].strip().lower().replace("0x", "")
            try:
                count = int(float(cols[1]))
            except ValueError:
                continue
            out[addr] = out.get(addr, 0) + count
    return out


def apply_samples_to_branches(branches, samples_by_addr):
    """If we have sample counts on the branch instruction, we still do
    not know taken vs not-taken. Leave Taken% as — and stash samples so
    the table ranks hot branches. A second CSV with taken,not_taken is
    the real measurement (--csv).
    """
    for b in branches:
        addr = b["addr"].replace("0x", "")
        if addr in samples_by_addr:
            b["samples"] = samples_by_addr[addr]
    return branches


# ---------------------------------------------------------------------------
# --profile annotated source
# ---------------------------------------------------------------------------

def write_profile(src_path, rows, cold_threshold):
    """Insert /* BRANCH PROB: X.XX% */ above lines that are cold targets
    or cold source lines."""
    src_lines = Path(src_path).read_text().splitlines(keepends=True)
    # Map lineno → lowest taken% we saw for that line (as a target or source)
    lineno_probs = {}
    for r in rows:
        taken = r.get("taken")
        not_taken = r.get("not_taken")
        if taken is None and not_taken is None:
            continue
        taken = int(taken or 0)
        not_taken = int(not_taken or 0)
        total = taken + not_taken
        if total == 0:
            continue
        pct = 100.0 * taken / total
        for key in ("target_line", "line"):
            ln = r.get(key)
            if ln:
                prev = lineno_probs.get(ln)
                if prev is None or pct < prev:
                    lineno_probs[ln] = pct
    # Only annotate clearly cold lines.
    lineno_probs = {ln: p for ln, p in lineno_probs.items() if p <= cold_threshold}

    result = list(src_lines)
    for lineno in sorted(lineno_probs, reverse=True):
        idx = lineno - 1
        if 0 <= idx < len(result):
            indent = len(result[idx]) - len(result[idx].lstrip(" \t"))
            pad = result[idx][:indent]
            comment = f"{pad}/* BRANCH PROB: {lineno_probs[lineno]:.2f}% */\n"
            result.insert(idx, comment)
    return result


# ---------------------------------------------------------------------------
# --trace recipe
# ---------------------------------------------------------------------------

TRACE_RECIPE = """
# .trace export recipe (this tool does not parse xctrace XML)

xcrun xctrace toc {trace}
xcrun xctrace show {trace} --track 'CPU Profiler' --calltree 'Profile' \\
    --include-source-info --format markdown
# For a hot function node-id from the tree:
xcrun xctrace show {trace} --track 'CPU Profiler' --calltree 'Profile' \\
    --source NODE --source-mode interleave

# Taken% ≈ samples on the taken target vs samples on the fall-through
# instruction after a b.<cond> / cbz / tbz. Write the pairs as CSV:

# file,line,taken,not_taken,function,target_line,assembly
# foo.c,27,12,4100,process_data,42,cbz w0, 0x...

python3 tools/branchprob.py --csv branches.csv {source}

# Preferred: Clang PGO (no XML at all)
#   clang -O2 -g -fprofile-instr-generate -fcoverage-mapping -mcpu=apple-m4 -o binary source.c
#   LLVM_PROFILE_FILE=default.profraw ./binary
#   xcrun llvm-profdata merge -o default.profdata default.profraw
#   python3 tools/branchprob.py --profdata default.profdata --binary ./binary source.c

# CPU Counters cross-check: Guide Chapter 8 Table 8.1 → picker names
# (BRANCH_COND_MISPRED_NONSPEC, INST_BRANCH_TAKEN, …). Do not invent encodings.
""".strip()


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def _func_matches(fn, names, fuzzy):
    """Match `foo` to `_foo` (Mach-O) and optionally substrings."""
    if not fn:
        return False
    variants = {fn, fn.lstrip("_")}
    for n in names:
        if fuzzy:
            if any(n in v for v in variants) or n.lstrip("_") in fn:
                return True
        else:
            if n in variants or n.lstrip("_") in variants:
                return True
    return False


def filter_functions(rows, names, fuzzy):
    if not names:
        return rows
    return [r for r in rows if _func_matches(r.get("function"), names, fuzzy)]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Measure branch probabilities on Darwin / Apple Silicon "
            "(Clang PGO, CSV, llvm-profdata, .trace recipe, AArch64 objdump). "
            "Does not use Intel PMU events or encodings."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=COLLECTION_HELP,
    )
    src_mode = parser.add_mutually_exclusive_group(required=True)
    src_mode.add_argument("--csv", metavar="FILE",
                          help="CSV of file,line,taken,not_taken[,function,...]")
    src_mode.add_argument("--profdata", metavar="FILE",
                          help="LLVM .profdata from llvm-profdata merge")
    src_mode.add_argument("--llvm-profdata-show", metavar="FILE",
                          dest="pshow",
                          help="Pre-rendered `llvm-profdata show --all-functions --counts` dump")
    src_mode.add_argument("--cov-json", metavar="FILE",
                          help="Pre-rendered `llvm-cov export` JSON")
    src_mode.add_argument("--trace", metavar="FILE",
                          help=".trace from xctrace; print export recipe + empty schema")
    src_mode.add_argument("--from-annotate", metavar="BINARY",
                          dest="annotate_bin",
                          help="Disassemble BINARY; list AArch64 conditional branches")

    parser.add_argument("source", nargs="?",
                        help="C/C++/ObjC source file (filters rows; required for --profile)")
    parser.add_argument("functions", nargs="*", metavar="function",
                        help="Function name(s) to report (default: all)")
    parser.add_argument("--binary", metavar="BIN",
                        help="Instrumented binary; with --profdata, run llvm-cov export")
    parser.add_argument("--samples", metavar="FILE",
                        help="With --from-annotate: CSV of addr,count (ranks hot branches; "
                             "does not invent Taken%%)")
    parser.add_argument("--min-samples", type=int, default=None, metavar="N",
                        help=f"Hide branches with fewer than N samples "
                             f"(default: {MIN_SAMPLES_DEFAULT}; 0 when functions are named)")
    parser.add_argument("--cold-threshold", type=float,
                        default=COLD_THRESHOLD_DEFAULT, metavar="PCT",
                        help=f"Taken%% ≤ this → cold-path candidate "
                             f"(default: {COLD_THRESHOLD_DEFAULT})")
    parser.add_argument("--fuzzy", action="store_true",
                        help="Match function names as substrings")
    parser.add_argument("--profile", metavar="FILE",
                        help="Write annotated source copy with /* BRANCH PROB: X.XX%% */")
    parser.add_argument("--debug", action="store_true",
                        help="Write intermediate dumps next to the source / cwd")
    args = parser.parse_args()

    # A first positional that is not a source file is a function filter
    # (`branchprob.py --csv f.csv process_data`).
    if args.source and not os.path.isfile(args.source):
        lower = args.source.lower()
        looks_like_source = lower.endswith((
            ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp",
            ".m", ".mm", ".s",
        ))
        if not looks_like_source:
            args.functions = [args.source] + list(args.functions)
            args.source = None

    src_path = Path(args.source) if args.source else None
    src_basename = src_path.name if src_path else None

    if args.functions:
        min_samples = 0 if args.min_samples is None else args.min_samples
    else:
        min_samples = MIN_SAMPLES_DEFAULT if args.min_samples is None else args.min_samples

    header_bits = ["# Branch probabilities"]
    if src_basename:
        header_bits[0] += f" — `{src_basename}`"
    header_bits.append("")
    header_bits.append(
        "Darwin / Apple Silicon. Not Intel `BR_INST_RETIRED.*`. "
        "Cold-path candidate: Taken% ≤ "
        f"{args.cold_threshold:g}% with samples → "
        "`__attribute__((cold))` / `[[gnu::cold]]`."
    )
    header_bits.append("")

    rows = []
    extra_notes = []

    if args.csv:
        header_bits.append(f"Source: CSV `{args.csv}`.")
        rows = parse_csv(args.csv)
        if not rows:
            extra_notes.append("CSV parsed but produced no rows — check the header.")
    elif args.cov_json:
        header_bits.append(f"Source: llvm-cov export JSON `{args.cov_json}`.")
        with open(args.cov_json) as fh:
            rows = parse_cov_json(json.load(fh), src_basename)
    elif args.profdata:
        header_bits.append(f"Source: LLVM profdata `{args.profdata}`.")
        if args.binary:
            header_bits.append(
                f"Mapped through `llvm-cov export` on `{args.binary}` "
                "(trueCount / falseCount per source branch)."
            )
            print("[1/2] llvm-cov export...", file=sys.stderr, flush=True)
            obj = run_llvm_cov_export(args.binary, args.profdata)
            if args.debug:
                debug_path = Path("branchprob.cov.json")
                debug_path.write_text(json.dumps(obj)[:2_000_000])
                print(f"[debug] llvm-cov JSON → {debug_path}", file=sys.stderr)
            rows = parse_cov_json(obj, src_basename)
            if not rows:
                extra_notes.append(
                    "llvm-cov export contained no `branches` arrays. "
                    "Rebuild with `-fcoverage-mapping` as well as "
                    "`-fprofile-instr-generate`, or fall back to block counts."
                )
                print("[2/2] falling back to llvm-profdata show...",
                      file=sys.stderr, flush=True)
                dump = run_llvm_profdata_show(args.profdata)
                rows = parse_llvm_profdata_show(dump)
        else:
            header_bits.append(
                "No `--binary`: using `llvm-profdata show --all-functions "
                "--counts` block-count *proxy* (not a source-level Taken%). "
                "Re-run with `--binary` for llvm-cov branch mapping."
            )
            print("[1/1] llvm-profdata show...", file=sys.stderr, flush=True)
            dump = run_llvm_profdata_show(args.profdata)
            if args.debug:
                Path("branchprob.pshow.txt").write_text(dump)
            rows = parse_llvm_profdata_show(dump)
    elif args.pshow:
        header_bits.append(
            f"Source: llvm-profdata show dump `{args.pshow}` "
            "(block-count proxy, not source-level Taken%)."
        )
        dump = Path(args.pshow).read_text()
        rows = parse_llvm_profdata_show(dump)
    elif args.trace:
        source_hint = src_basename or "source.c"
        recipe = TRACE_RECIPE.format(trace=args.trace, source=source_hint)
        print(recipe, file=sys.stderr)
        header_bits.append(f"Source: `.trace` `{args.trace}` — XML not parsed.")
        table = schema_only_table(
            "Export recipe printed on stderr. Fill a CSV of "
            "(file,line,taken,not_taken) from CPU Profiler sample density "
            "on each `b.<cond>` / `cbz` / `tbz` vs fall-through, or use "
            "Clang `-fprofile-instr-generate` (preferred)."
        )
        if args.functions:
            header_bits.append(
                "Functions: " + ", ".join(f"`{f}`" for f in args.functions)
            )
        print("\n".join(header_bits) + "\n\n" + table)
        return
    elif args.annotate_bin:
        header_bits.append(
            f"Source: AArch64 disassembly of `{args.annotate_bin}`. "
            "Taken% is not invented from objdump. CPU Profiler sample "
            "density on the branch vs fall-through is the measurement; "
            "pass a CSV via `--csv` once you have counts, or use PGO."
        )
        print("[1/1] disassembling...", file=sys.stderr, flush=True)
        text = run_objdump(args.annotate_bin)
        if args.debug:
            Path("branchprob.annotate.out").write_text(text)
            print("[debug] disassembly → branchprob.annotate.out", file=sys.stderr)
        branches = parse_objdump(text)
        if args.samples:
            samples = parse_samples_csv(args.samples)
            apply_samples_to_branches(branches, samples)
        rows = branches

    rows = filter_functions(rows, args.functions, args.fuzzy)
    if args.functions and not rows:
        names = ", ".join(f"`{f}`" for f in args.functions)
        print(f"Warning: none of {names} found in the input.", file=sys.stderr)

    if extra_notes:
        header_bits.extend(extra_notes)
    if args.functions:
        header_bits.append(
            "Functions: " + ", ".join(f"`{f}`" for f in args.functions)
        )
    header_bits.append("")
    header = "\n".join(header_bits) + "\n"

    print("[done] formatting table...", file=sys.stderr, flush=True)
    table = format_table(rows, src_basename, args.cold_threshold, min_samples)
    print(header + table)

    if args.profile:
        if not src_path:
            raise SystemExit("--profile requires a source.c positional argument")
        annotated = write_profile(str(src_path), rows, args.cold_threshold)
        Path(args.profile).write_text("".join(annotated))
        print(f"[profile] Annotated source → {args.profile}", file=sys.stderr)


if __name__ == "__main__":
    main()
