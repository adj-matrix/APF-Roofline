/* E1 fix (M7d): io_method = worker reproduced: a "backend" thread that scans with a fixed busy wait per 128 KiB
 * read, and K I/O worker threads fed by one shared queue with PostgreSQL 18's dispatch (method_worker.c):
 *   - the submitter wakes the lowest-numbered idle worker (pgaio_worker_choose_idle, line 165), one per submit;
 *   - a worker that takes an IO wakes up to min(queue depth, 2) more idle workers (fan-out, line 489);
 *   - a worker without work marks itself idle and sleeps until woken.
 * Each worker reads with its own file descriptors (PostgreSQL reopens the file in the worker), then runs the
 * completion callback: a busy wait of CB_US (page verification with checksums, dU of the fix), and marks the IO
 * done; the backend keeps EIC reads in flight and waits only when the oldest is not done.
 *
 *   replica_pool PATH|@LIST DIRECT K EIC THINK_US CB_US DUR_S
 * Prints one JSON line: reads, elapsed, the backend's wait, per-worker IO counts.
 * Build: cc -O2 -pthread -o replica_pool replica_pool.c
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>

#define CHUNK (128 * 1024)
#define NIOV 16
#define MAXW 32
#define MAXQ 64
#define MAXFILES 256

static double now(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec * 1e-9;
}
static void spin(double us) {
    if (us <= 0) return;
    double end = now() + us * 1e-6;
    while (now() < end) {}
}

/* THINK "<n>k": n thousand iterations of a dependent add (fixed work, M9); a plain number: us of busy wait */
static double think_us = 0;
static long think_iters = 0;
static void work(void) {
    if (think_iters) {
        unsigned long x = 0;
        for (long i = 0; i < think_iters; i++) __asm__ volatile("add $1, %0" : "+r"(x));
    } else spin(think_us);
}

static char names[MAXFILES][512];
static int nfiles, direct, nworkers, eic;
static off_t starts[MAXFILES + 1], total;
static double cb_us;
static char *buf;

static int file_of(off_t pos) {
    int i = 0;
    while (i + 1 < nfiles && pos >= starts[i + 1]) i++;
    return i;
}

/* shared state, one mutex (AioWorkerSubmissionQueueLock + the IO handles' state) */
static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t backend_cv = PTHREAD_COND_INITIALIZER;
static pthread_cond_t worker_cv[MAXW];
static int woken[MAXW];                 /* latch set */
static unsigned long idle_mask;
static int queue[MAXQ], qhead, qtail;   /* slot numbers */
static off_t slot_pos[MAXQ];
static int slot_done[MAXQ], backend_waiting, stop;
static long wreads[MAXW];

static int choose_idle(void) {          /* lowest idle worker, cleared from the mask */
    if (!idle_mask) return -1;
    int w = __builtin_ctzl(idle_mask);
    idle_mask &= ~(1UL << w);
    return w;
}
static void set_latch(int w) {
    woken[w] = 1;
    pthread_cond_signal(&worker_cv[w]);
}

static void submit(int slot, off_t pos) {   /* backend: enqueue one IO, wake one idle worker */
    pthread_mutex_lock(&mu);
    slot_pos[slot] = pos;
    slot_done[slot] = 0;
    queue[qtail++ % MAXQ] = slot;
    int w = choose_idle();
    if (w >= 0) set_latch(w);
    pthread_mutex_unlock(&mu);
}

static void *worker(void *arg) {
    int me = (int)(long)arg;
    int fds[MAXFILES];
    for (int i = 0; i < nfiles; i++) {
        fds[i] = open(names[i], O_RDONLY | (direct ? O_DIRECT : 0));
        if (fds[i] < 0) { perror(names[i]); exit(1); }
    }
    struct iovec iov[NIOV];
    pthread_mutex_lock(&mu);
    for (;;) {
        if (stop) break;
        if (qhead == qtail) {                         /* nothing to do: mark idle, sleep on the latch */
            idle_mask |= 1UL << me;
            while (!woken[me] && !stop) pthread_cond_wait(&worker_cv[me], &mu);
            woken[me] = 0;
            continue;
        }
        int slot = queue[qhead++ % MAXQ];
        idle_mask &= ~(1UL << me);
        int depth = qtail - qhead, nw = depth < 2 ? depth : 2;   /* fan-out */
        for (int i = 0; i < nw; i++) {
            int w = choose_idle();
            if (w < 0) break;
            set_latch(w);
        }
        off_t pos = slot_pos[slot];
        pthread_mutex_unlock(&mu);
        int fi = file_of(pos);
        for (int i = 0; i < NIOV; i++) iov[i] = (struct iovec){buf + (size_t)slot * CHUNK + i * 8192, 8192};
        if (preadv(fds[fi], iov, NIOV, pos - starts[fi]) < 0) { perror("preadv"); exit(1); }
        spin(cb_us);                                  /* completion callback: page verification */
        pthread_mutex_lock(&mu);
        slot_done[slot] = 1;
        wreads[me]++;
        if (backend_waiting) pthread_cond_signal(&backend_cv);
    }
    pthread_mutex_unlock(&mu);
    return NULL;
}

int main(int argc, char **argv) {
    if (argc != 8) {
        fprintf(stderr, "usage: %s PATH|@LIST DIRECT K EIC THINK_US CB_US DUR_S\n", argv[0]);
        return 2;
    }
    const char *path = argv[1];
    direct = atoi(argv[2]), nworkers = atoi(argv[3]), eic = atoi(argv[4]);
    double dur = atof(argv[7]);
    size_t tl = strlen(argv[5]);
    if (tl && argv[5][tl - 1] == 'k') think_iters = (long)(atof(argv[5]) * 1000);
    else think_us = atof(argv[5]);
    cb_us = atof(argv[6]);
    if (nworkers < 1 || nworkers > MAXW || eic < 1 || eic > MAXQ) return 2;
    if (path[0] == '@') {
        FILE *l = fopen(path + 1, "r");
        if (!l) { perror("list"); return 1; }
        while (nfiles < MAXFILES && fgets(names[nfiles], sizeof names[nfiles], l)) {
            names[nfiles][strcspn(names[nfiles], "\n")] = 0;
            if (names[nfiles][0]) nfiles++;
        }
        fclose(l);
    } else {
        snprintf(names[0], sizeof names[0], "%s", path);
        nfiles = 1;
    }
    for (int i = 0; i < nfiles; i++) {
        int fd = open(names[i], O_RDONLY);
        struct stat st;
        if (fd < 0 || fstat(fd, &st)) { perror(names[i]); return 1; }
        if (!direct) posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED);
        close(fd);
        starts[i] = total;
        total += st.st_size / CHUNK * CHUNK;
    }
    starts[nfiles] = total;
    if (posix_memalign((void **)&buf, 4096, (size_t)eic * CHUNK)) return 1;
    memset(buf, 0, (size_t)eic * CHUNK);
    pthread_t th[MAXW];
    for (int w = 0; w < nworkers; w++) pthread_cond_init(&worker_cv[w], NULL);
    for (int w = 0; w < nworkers; w++) pthread_create(&th[w], NULL, worker, (void *)(long)w);
    usleep(100000);                                   /* workers idle */
    off_t next = 0;
    long reads = 0;
    double t0 = now(), t_end = t0 + dur, wait_s = 0;
    volatile unsigned long sink = 0;
    int oldest = 0, inflight = 0, eof = 0;
    for (int s = 0; s < eic && next < total; s++, inflight++, next += CHUNK) submit(s, next);
    while (now() < t_end && inflight > 0) {
        pthread_mutex_lock(&mu);
        if (!slot_done[oldest]) {
            double a = now();
            backend_waiting = 1;
            while (!slot_done[oldest]) pthread_cond_wait(&backend_cv, &mu);
            backend_waiting = 0;
            wait_s += now() - a;
        }
        pthread_mutex_unlock(&mu);
        sink += (unsigned char)buf[(size_t)oldest * CHUNK + 4096];
        reads++;
        inflight--;
        work();
        if (next >= total) { if (direct) next = 0; else eof = 1; }
        if (!eof) { submit(oldest, next); next += CHUNK; inflight++; }
        oldest = (oldest + 1) % eic;
    }
    double el = now() - t0;
    pthread_mutex_lock(&mu);
    stop = 1;
    for (int w = 0; w < nworkers; w++) set_latch(w);
    pthread_mutex_unlock(&mu);
    for (int w = 0; w < nworkers; w++) pthread_join(th[w], NULL);
    printf("{\"reads\": %ld, \"elapsed_s\": %.4f, \"wait_s\": %.4f, \"MiBps\": %.1f, \"eof\": %d, \"worker_reads\": [",
           reads, el, wait_s, reads * (double)CHUNK / 1048576 / el, eof);
    for (int w = 0; w < nworkers; w++) printf("%s%ld", w ? ", " : "", wreads[w]);
    printf("]}\n");
    return 0;
}
