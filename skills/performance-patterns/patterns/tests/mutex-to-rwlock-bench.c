/*
 * Copyright (c) 2026. MIT license.
 *
 * Original Apple Silicon / Darwin example.
 *
 * mutex-to-rwlock-bench.c — Verify claims in mutex-to-rwlock.md
 *
 * Demonstrates:
 *   1. Mutex serializes readers → throughput degrades with thread count
 *   2. rwlock allows concurrent readers → throughput scales with thread count
 *
 * Build:
 *   clang -O2 -pthread -o mutex-to-rwlock-bench mutex-to-rwlock-bench.c
 *
 * Run:
 *   ./mutex-to-rwlock-bench [max_threads] [duration_sec] [write_pct] [mode]
 *
 *   mode: "mutex" | "rwlock" | omit for comparative run
 *
 * Profile with xctrace / CPU Profiler (not perf):
 *   xctrace record --template 'CPU Profiler' --output mutex.trace -- \
 *       ./mutex-to-rwlock-bench 8 5 0 mutex
 *
 * Cannot pin P vs E cores. Threads request QOS_CLASS_USER_INTERACTIVE so
 * the system prefers S/P cores; the OS still chooses placement.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <pthread/qos.h>
#include <stdatomic.h>
#include <time.h>
#include <unistd.h>

/* Align per-thread data to 128 B — Apple silicon cache line. */
#define CACHE_LINE 128

/* 64K entries — large enough that random accesses miss L1, simulating a
 * realistic lookup table (e.g., routing table, config cache, hash map) */
#define TABLE_SIZE (64 * 1024)

/* 256 random reads per critical section — makes the critical section long
 * enough that rwlock's user-space overhead is negligible relative to the work,
 * while mutex serialization becomes the dominant bottleneck */
#define READ_ITERS 256

/* Thread-local PRNG — xorshift32 has no shared state, so it doesn't introduce
 * contention that would mask the mutex/rwlock scaling difference. */
static inline unsigned int xorshift32(unsigned int *state) {
    unsigned int x = *state;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    *state = x;
    return x;
}

struct lookup_table {
    int entries[TABLE_SIZE];
};

static struct lookup_table g_table __attribute__((aligned(CACHE_LINE)));

static pthread_mutex_t g_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_rwlock_t g_rwlock = PTHREAD_RWLOCK_INITIALIZER;

static atomic_int g_running;

/* Public Darwin headers do not export pthread_barrier_t. Tiny rendezvous. */
struct bench_barrier {
    pthread_mutex_t mu;
    pthread_cond_t cv;
    unsigned needed;
    unsigned arrived;
    unsigned generation;
};
static struct bench_barrier g_barrier;

static void barrier_init(unsigned count)
{
    pthread_mutex_init(&g_barrier.mu, NULL);
    pthread_cond_init(&g_barrier.cv, NULL);
    g_barrier.needed = count;
    g_barrier.arrived = 0;
    g_barrier.generation = 0;
}

static void barrier_wait(void)
{
    pthread_mutex_lock(&g_barrier.mu);
    unsigned gen = g_barrier.generation;
    if (++g_barrier.arrived == g_barrier.needed) {
        g_barrier.arrived = 0;
        g_barrier.generation++;
        pthread_cond_broadcast(&g_barrier.cv);
    } else {
        while (gen == g_barrier.generation)
            pthread_cond_wait(&g_barrier.cv, &g_barrier.mu);
    }
    pthread_mutex_unlock(&g_barrier.mu);
}

static void barrier_destroy(void)
{
    pthread_cond_destroy(&g_barrier.cv);
    pthread_mutex_destroy(&g_barrier.mu);
}

struct thread_result {
    unsigned long ops;
    char pad[CACHE_LINE - sizeof(unsigned long)];
} __attribute__((aligned(CACHE_LINE)));

struct bench_config {
    int thread_id;
    int write_pct;
    struct thread_result *result;
};

static int do_read_work(unsigned int *state) {
    int sum = 0;
    for (int i = 0; i < READ_ITERS; i++) {
        int idx = xorshift32(state) % TABLE_SIZE;
        sum += g_table.entries[idx];
    }
    return sum;
}

static void do_write_work(int tid) {
    g_table.entries[tid % TABLE_SIZE] = tid;
}

static void apply_qos(void) {
    /* Hint S/P cores. The OS still chooses placement; there is no taskset. */
    (void)pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
}

static void *worker_mutex(void *arg) {
    struct bench_config *cfg = arg;
    unsigned long ops = 0;
    unsigned int rng = cfg->thread_id + 1;
    int sink = 0;

    apply_qos();
    barrier_wait();

    while (atomic_load_explicit(&g_running, memory_order_relaxed)) {
        int is_write = (cfg->write_pct > 0) &&
                       ((xorshift32(&rng) % 100) < cfg->write_pct);

        pthread_mutex_lock(&g_mutex);
        if (is_write)
            do_write_work(cfg->thread_id);
        else
            sink += do_read_work(&rng);
        pthread_mutex_unlock(&g_mutex);
        ops++;
    }

    cfg->result->ops = ops;
    return (void *)(long)sink;
}

static void *worker_rwlock(void *arg) {
    struct bench_config *cfg = arg;
    unsigned long ops = 0;
    unsigned int rng = cfg->thread_id + 1;
    int sink = 0;

    apply_qos();
    barrier_wait();

    while (atomic_load_explicit(&g_running, memory_order_relaxed)) {
        int is_write = (cfg->write_pct > 0) &&
                       ((xorshift32(&rng) % 100) < cfg->write_pct);

        if (is_write) {
            pthread_rwlock_wrlock(&g_rwlock);
            do_write_work(cfg->thread_id);
            pthread_rwlock_unlock(&g_rwlock);
        } else {
            pthread_rwlock_rdlock(&g_rwlock);
            sink += do_read_work(&rng);
            pthread_rwlock_unlock(&g_rwlock);
        }
        ops++;
    }

    cfg->result->ops = ops;
    return (void *)(long)sink;
}

static double run_bench(int num_threads, int duration_sec, int write_pct,
                        int use_rwlock) {
    pthread_t *threads = calloc((size_t)num_threads, sizeof(pthread_t));
    struct bench_config *cfgs = calloc((size_t)num_threads, sizeof(struct bench_config));
    struct thread_result *results = aligned_alloc(CACHE_LINE,
        (size_t)num_threads * sizeof(struct thread_result));
    memset(results, 0, (size_t)num_threads * sizeof(struct thread_result));

    barrier_init((unsigned)num_threads + 1);
    atomic_store(&g_running, 1);

    for (int i = 0; i < num_threads; i++) {
        cfgs[i].thread_id = i;
        cfgs[i].write_pct = write_pct;
        cfgs[i].result = &results[i];
        pthread_create(&threads[i], NULL,
                       use_rwlock ? worker_rwlock : worker_mutex, &cfgs[i]);
    }

    barrier_wait();

    struct timespec start, end;
    clock_gettime(CLOCK_MONOTONIC, &start);
    sleep((unsigned)duration_sec);
    atomic_store(&g_running, 0);
    clock_gettime(CLOCK_MONOTONIC, &end);

    double elapsed = (end.tv_sec - start.tv_sec) +
                     (end.tv_nsec - start.tv_nsec) / 1e9;

    unsigned long total_ops = 0;
    for (int i = 0; i < num_threads; i++) {
        pthread_join(threads[i], NULL);
        total_ops += results[i].ops;
    }

    barrier_destroy();
    free(threads);
    free(cfgs);
    free(results);

    return total_ops / elapsed;
}

enum run_mode { MODE_BOTH, MODE_MUTEX, MODE_RWLOCK, MODE_MUTEX_OPS, MODE_RWLOCK_OPS };

static enum run_mode parse_mode(const char *s) {
    if (strcmp(s, "mutex") == 0) return MODE_MUTEX;
    if (strcmp(s, "rwlock") == 0) return MODE_RWLOCK;
    if (strcmp(s, "mutex-ops") == 0) return MODE_MUTEX_OPS;
    if (strcmp(s, "rwlock-ops") == 0) return MODE_RWLOCK_OPS;
    return MODE_BOTH;
}

int main(int argc, char **argv) {
    int max_threads = 16;
    int duration_sec = 2;
    int write_pct = 5;
    enum run_mode mode = MODE_BOTH;

    if (argc > 1) max_threads = atoi(argv[1]);
    if (argc > 2) duration_sec = atoi(argv[2]);
    if (argc > 3) write_pct = atoi(argv[3]);
    if (argc > 4) mode = parse_mode(argv[4]);

    if (max_threads < 1) max_threads = 1;
    if (max_threads > 1024) max_threads = 1024;

    int cores = (int)sysconf(_SC_NPROCESSORS_ONLN);
    if (cores < 1) cores = 1;
    if (max_threads > cores) max_threads = cores;

    if (mode == MODE_MUTEX) {
        run_bench(max_threads, duration_sec, write_pct, 0);
        return 0;
    }
    if (mode == MODE_RWLOCK) {
        run_bench(max_threads, duration_sec, write_pct, 1);
        return 0;
    }
    if (mode == MODE_MUTEX_OPS) {
        printf("%.0f\n", run_bench(max_threads, duration_sec, write_pct, 0));
        return 0;
    }
    if (mode == MODE_RWLOCK_OPS) {
        printf("%.0f\n", run_bench(max_threads, duration_sec, write_pct, 1));
        return 0;
    }

    printf("mutex-to-rwlock benchmark (Apple silicon)\n");
    printf("  max_threads=%d  duration=%ds  write_pct=%d%%  logicalcpu=%d\n",
           max_threads, duration_sec, write_pct, cores);
    printf("  QoS=USER_INTERACTIVE (hint only; OS places threads on S/P/E)\n\n");
    printf("%6s  %15s  %15s  %10s\n",
           "threads", "mutex (Mops/s)", "rwlock (Mops/s)", "speedup");
    printf("%6s  %15s  %15s  %10s\n",
           "------", "---------------", "---------------", "----------");

    int thread_counts[] = {1, 2, 4, 6, 8, 10, 12, 14, 16, 20, 24, 32};
    int n_counts = (int)(sizeof(thread_counts) / sizeof(thread_counts[0]));

    for (int i = 0; i < n_counts; i++) {
        int t = thread_counts[i];
        if (t > max_threads) break;

        double mutex_ops = run_bench(t, duration_sec, write_pct, 0);
        double rwlock_ops = run_bench(t, duration_sec, write_pct, 1);
        double speedup = rwlock_ops / mutex_ops;

        printf("%6d  %15.2f  %15.2f  %9.1fx\n",
               t, mutex_ops / 1e6, rwlock_ops / 1e6, speedup);
    }

    printf("\nKey claims to verify on this machine:\n");
    printf("  - Mutex throughput should degrade or plateau as thread count grows\n");
    printf("  - rwlock throughput should scale with thread count (read-heavy)\n");
    printf("  - Record hw.nperflevels / perflevel*.physicalcpu with the table\n");

    return 0;
}
