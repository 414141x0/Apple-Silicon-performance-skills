#!/bin/bash
# run-mutex-to-rwlock-bench.sh — Build and run the mutex-to-rwlock benchmark
# on Apple silicon / Darwin.
#
# Usage:
#   ./run-mutex-to-rwlock-bench.sh [duration_sec]
#
# Detects topology via sysctl (not lscpu). Does not pin cores (no taskset;
# the OS places threads; the bench requests USER_INTERACTIVE QoS).
# Profiles with xctrace CPU Profiler when available; does not require perf.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$SCRIPT_DIR/mutex-to-rwlock-bench.c"
BIN="$SCRIPT_DIR/mutex-to-rwlock-bench"
DURATION="${1:-3}"

# --- Detect CPU topology (sysctl; no lscpu / taskset) ---

detect_topology() {
    LOGICAL=$(sysctl -n hw.logicalcpu)
    PHYSICAL=$(sysctl -n hw.physicalcpu)
    NPERF=$(sysctl -n hw.nperflevels 2>/dev/null || echo 1)
    P0=$(sysctl -n hw.perflevel0.physicalcpu 2>/dev/null || echo "$PHYSICAL")
    P0_NAME=$(sysctl -n hw.perflevel0.name 2>/dev/null || echo "unknown")
    P1=$(sysctl -n hw.perflevel1.physicalcpu 2>/dev/null || echo 0)
    P1_NAME=$(sysctl -n hw.perflevel1.name 2>/dev/null || echo "")
    LINE=$(sysctl -n hw.cachelinesize)
    PAGE=$(sysctl -n hw.pagesize)

    echo "=== CPU Topology (sysctl) ==="
    echo "  hw.logicalcpu:              $LOGICAL"
    echo "  hw.physicalcpu:             $PHYSICAL"
    echo "  hw.nperflevels:             $NPERF"
    echo "  hw.perflevel0.physicalcpu:  $P0  ($P0_NAME)"
    if [[ "$P1" != "0" ]]; then
        echo "  hw.perflevel1.physicalcpu:  $P1  ($P1_NAME)"
    fi
    echo "  hw.cachelinesize:           $LINE"
    echo "  hw.pagesize:                $PAGE"
    echo "  (no core pinning — QoS USER_INTERACTIVE is a hint only)"
    echo ""

    MAX_THREADS=$LOGICAL
}

# --- Build ---

build() {
    echo "=== Building ==="
    if ! command -v clang &>/dev/null; then
        echo "ERROR: clang not found" >&2
        exit 1
    fi

    clang -O2 -pthread -o "$BIN" "$SRC"
    echo "  Built: $BIN"
    echo ""
}

run_bench() {
    local write_pct=$1
    echo "--- Write percentage: ${write_pct}% (duration: ${DURATION}s per data point) ---"
    echo ""
    "$BIN" "$MAX_THREADS" "$DURATION" "$write_pct"
    echo ""
}

# --- Optional xctrace CPU Profiler (skip if missing) ---

run_xctrace_profile() {
    if ! command -v xctrace &>/dev/null; then
        echo "=== xctrace not available, skipping CPU Profiler ==="
        echo ""
        return
    fi

    local out_dir
    out_dir=$(mktemp -d /tmp/mutex-rwlock-trace.XXXXXX)

    echo "=== xctrace CPU Profiler (${MAX_THREADS} threads, 0% writes, ${DURATION}s) ==="
    echo ""

    echo "--- Mutex ---"
    if xctrace record --template 'CPU Profiler' \
            --output "$out_dir/mutex.trace" \
            -- "$BIN" "$MAX_THREADS" "$DURATION" 0 mutex; then
        echo "  wrote $out_dir/mutex.trace"
    else
        echo "  WARNING: xctrace record (mutex) failed; skipping"
    fi
    echo ""

    echo "--- rwlock ---"
    if xctrace record --template 'CPU Profiler' \
            --output "$out_dir/rwlock.trace" \
            -- "$BIN" "$MAX_THREADS" "$DURATION" 0 rwlock; then
        echo "  wrote $out_dir/rwlock.trace"
    else
        echo "  WARNING: xctrace record (rwlock) failed; skipping"
    fi
    echo ""
    echo "Open the .trace packages in Instruments. Expect mutex time in"
    echo "pthread_mutex_lock / psynch_mutexwait; rwlock in user-space"
    echo "reader-count atomics plus do_read_work."
    echo ""
}

# --- Main ---

echo "========================================"
echo " mutex-to-rwlock verification benchmark"
echo "========================================"
echo ""

detect_topology
build

echo "=== Throughput scaling ==="
echo ""

run_bench 0
run_bench 1
run_bench 5

run_xctrace_profile

echo "=== Done ==="
echo "Record the table plus hw.nperflevels / perflevel*.physicalcpu in"
echo "mutex-to-rwlock-results.md — do not copy numbers from another machine."
