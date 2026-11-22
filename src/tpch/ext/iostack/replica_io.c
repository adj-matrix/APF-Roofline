/* E1 fix (M7): one "backend" that scans a file the way a PostgreSQL sequential scan does, with a fixed
 * busy wait per 128 KiB read standing in for the query's CPU work.  docs/ext/iostack.md, "Fix".
 *
 *   replica_io PATH|@LIST ENGINE DIRECT QD THINK DUR_S     (@LIST: files read one after the other;
 *                                                          THINK: us of busy wait, or <n>k iterations of work)
 *     ENGINE psync : preadv of 16 x 8 KiB iovecs (one per shared buffer), synchronous (sync method)
 *            uring : io_uring READV, QD reads in flight, consumed in order (read stream + io_uring):
 *                    one io_uring_enter per submission, wait (GETEVENTS) only when the oldest is not done;
 *                    buffered reads submitted with more than 4 in flight get IOSQE_ASYNC (run by io-wq
 *                    workers), as PostgreSQL 18 does (method_io_uring.c:442)
 *     DIRECT 1     : O_DIRECT (debug_io_direct = data); the file is read round and round
 *            0     : buffered; the file's page cache is dropped first and it is read once (<= DUR_S)
 *   Prints one JSON line: reads, elapsed, time spent waiting inside read / wait calls.
 * Raw syscalls (linux/io_uring.h); no liburing headers on this machine.  Build: cc -O2 -o replica_io replica_io.c
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <linux/io_uring.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>

#define CHUNK (128 * 1024)
#define NIOV 16
#define MAXQD 64
#define MAXFILES 256

static int fds[MAXFILES], nfiles;
static off_t starts[MAXFILES + 1], size;

static int file_of(off_t pos) {           /* which file holds byte pos of the concatenation */
    int i = 0;
    while (i + 1 < nfiles && pos >= starts[i + 1]) i++;
    return i;
}

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

/* THINK given as "<n>k": fixed work of n thousand iterations of a dependent add (~1 cycle each), as a query's
 * work per read is fixed in cycles: a faster clock finishes it sooner and lowers the on-CPU share, which is the
 * feedback schedutil sees (M9). A plain number is a busy wait of that many us (fixed time, M7). */
static double think_us = 0;
static long think_iters = 0;
static void parse_think(const char *a) {
    size_t n = strlen(a);
    if (n && a[n - 1] == 'k') think_iters = (long)(atof(a) * 1000);
    else think_us = atof(a);
}
static void work(void) {
    if (think_iters) {
        unsigned long x = 0;
        for (long i = 0; i < think_iters; i++) __asm__ volatile("add $1, %0" : "+r"(x));
    } else spin(think_us);
}

struct ring {
    int fd;
    unsigned *sq_head, *sq_tail, *sq_mask, *sq_array, *cq_head, *cq_tail, *cq_mask;
    struct io_uring_sqe *sqes;
    struct io_uring_cqe *cqes;
};

static int ring_init(struct ring *r, unsigned entries) {
    struct io_uring_params p;
    memset(&p, 0, sizeof p);
    r->fd = (int)syscall(__NR_io_uring_setup, entries, &p);
    if (r->fd < 0) return -1;
    size_t sq_sz = p.sq_off.array + p.sq_entries * sizeof(unsigned);
    size_t cq_sz = p.cq_off.cqes + p.cq_entries * sizeof(struct io_uring_cqe);
    if (p.features & IORING_FEAT_SINGLE_MMAP) sq_sz = cq_sz = sq_sz > cq_sz ? sq_sz : cq_sz;
    char *sq = mmap(0, sq_sz, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_POPULATE, r->fd, IORING_OFF_SQ_RING);
    char *cq = (p.features & IORING_FEAT_SINGLE_MMAP) ? sq
             : mmap(0, cq_sz, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_POPULATE, r->fd, IORING_OFF_CQ_RING);
    r->sqes = mmap(0, p.sq_entries * sizeof(struct io_uring_sqe), PROT_READ | PROT_WRITE, MAP_SHARED | MAP_POPULATE,
                   r->fd, IORING_OFF_SQES);
    if (sq == MAP_FAILED || cq == MAP_FAILED || r->sqes == MAP_FAILED) return -1;
    r->sq_head = (unsigned *)(sq + p.sq_off.head);
    r->sq_tail = (unsigned *)(sq + p.sq_off.tail);
    r->sq_mask = (unsigned *)(sq + p.sq_off.ring_mask);
    r->sq_array = (unsigned *)(sq + p.sq_off.array);
    r->cq_head = (unsigned *)(cq + p.cq_off.head);
    r->cq_tail = (unsigned *)(cq + p.cq_off.tail);
    r->cq_mask = (unsigned *)(cq + p.cq_off.ring_mask);
    r->cqes = (struct io_uring_cqe *)(cq + p.cq_off.cqes);
    return 0;
}

static void ring_submit_readv(struct ring *r, int fd, struct iovec *iov, off_t off, uint64_t tag, int async) {
    unsigned tail = *r->sq_tail, idx = tail & *r->sq_mask;
    struct io_uring_sqe *s = &r->sqes[idx];
    memset(s, 0, sizeof *s);
    s->opcode = IORING_OP_READV;
    s->fd = fd;
    s->addr = (uint64_t)(uintptr_t)iov;
    s->len = NIOV;
    s->off = off;
    s->user_data = tag;
    if (async) s->flags |= IOSQE_ASYNC;
    r->sq_array[idx] = idx;
    atomic_store_explicit((_Atomic unsigned *)r->sq_tail, tail + 1, memory_order_release);
    syscall(__NR_io_uring_enter, r->fd, 1, 0, 0, NULL, 0);
}

/* reap all available completions into done[]; returns how many */
static int ring_reap(struct ring *r, int *done, int *res) {
    int n = 0;
    unsigned head = *r->cq_head;
    while (head != atomic_load_explicit((_Atomic unsigned *)r->cq_tail, memory_order_acquire)) {
        struct io_uring_cqe *c = &r->cqes[head & *r->cq_mask];
        done[c->user_data] = 1;
        res[c->user_data] = c->res;
        head++, n++;
    }
    atomic_store_explicit((_Atomic unsigned *)r->cq_head, head, memory_order_release);
    return n;
}

int main(int argc, char **argv) {
    if (argc != 7) {
        fprintf(stderr, "usage: %s PATH psync|uring DIRECT QD THINK_US DUR_S\n", argv[0]);
        return 2;
    }
    const char *path = argv[1];
    int uring = strcmp(argv[2], "uring") == 0, direct = atoi(argv[3]), qd = atoi(argv[4]);
    double dur = atof(argv[6]);
    parse_think(argv[5]);
    if (!uring) qd = 1;
    if (qd < 1 || qd > MAXQD) return 2;
    /* PATH, or @LIST: a file naming several files read one after the other (a table's 1 GiB segments) */
    char names[MAXFILES][512];
    int nf = 0;
    if (path[0] == '@') {
        FILE *l = fopen(path + 1, "r");
        if (!l) { perror("list"); return 1; }
        while (nf < MAXFILES && fgets(names[nf], sizeof names[nf], l)) {
            names[nf][strcspn(names[nf], "\n")] = 0;
            if (names[nf][0]) nf++;
        }
        fclose(l);
    } else {
        snprintf(names[0], sizeof names[0], "%s", path);
        nf = 1;
    }
    for (int i = 0; i < nf; i++) {
        fds[i] = open(names[i], O_RDONLY | (direct ? O_DIRECT : 0));
        if (fds[i] < 0) { perror(names[i]); return 1; }
        struct stat st;
        fstat(fds[i], &st);
        starts[i] = size;
        size += st.st_size / CHUNK * CHUNK;
        if (!direct) posix_fadvise(fds[i], 0, 0, POSIX_FADV_DONTNEED);
    }
    starts[nf] = size;
    nfiles = nf;
    off_t next = 0;
    char *buf;
    if (posix_memalign((void **)&buf, 4096, (size_t)qd * CHUNK)) return 1;
    memset(buf, 0, (size_t)qd * CHUNK);
    struct iovec iov[MAXQD][NIOV];
    for (int q = 0; q < qd; q++)
        for (int i = 0; i < NIOV; i++) iov[q][i] = (struct iovec){buf + (size_t)q * CHUNK + i * 8192, 8192};
    long reads = 0;
    double t0 = now(), t_end = t0 + dur, wait_s = 0;
    volatile unsigned long sink = 0;
    int eof = 0;
    if (!uring) {
        while (now() < t_end && !eof) {
            double a = now();
            int fi = file_of(next);
            ssize_t n = preadv(fds[fi], iov[0], NIOV, next - starts[fi]);
            wait_s += now() - a;
            if (n != CHUNK) { if (n < 0) { perror("preadv"); return 1; } }
            sink += (unsigned char)buf[4096];
            reads++;
            next += CHUNK;
            if (next >= size) { if (direct) next = 0; else eof = 1; }
            work();
        }
    } else {
        struct ring r;
        if (ring_init(&r, (unsigned)qd)) { perror("io_uring_setup"); return 1; }
        int done[MAXQD] = {0}, res[MAXQD] = {0}, inflight = 0, oldest = 0;
        for (int q = 0; q < qd && next < size; q++, inflight++) {
            int fi = file_of(next);
            ring_submit_readv(&r, fds[fi], iov[q], next - starts[fi], (uint64_t)q, !direct && inflight > 4);
            next += CHUNK;
        }
        while (now() < t_end && inflight > 0) {
            ring_reap(&r, done, res);
            if (!done[oldest]) {
                double a = now();
                while (!done[oldest]) {
                    syscall(__NR_io_uring_enter, r.fd, 0, 1, IORING_ENTER_GETEVENTS, NULL, 0);
                    ring_reap(&r, done, res);
                }
                wait_s += now() - a;
            }
            if (res[oldest] < 0) { fprintf(stderr, "read error %d\n", res[oldest]); return 1; }
            done[oldest] = 0;
            inflight--;
            sink += (unsigned char)buf[(size_t)oldest * CHUNK + 4096];
            reads++;
            work();
            if (next >= size && direct) next = 0;
            if (next < size) {
                int fi = file_of(next);
                ring_submit_readv(&r, fds[fi], iov[oldest], next - starts[fi], (uint64_t)oldest, !direct && inflight > 4);
                next += CHUNK;
                inflight++;
            }
            oldest = (oldest + 1) % qd;
        }
    }
    double el = now() - t0;
    printf("{\"reads\": %ld, \"elapsed_s\": %.4f, \"wait_s\": %.4f, \"MiBps\": %.1f, \"eof\": %d}\n", reads, el, wait_s,
           reads * (double)CHUNK / 1048576 / el, eof);
    return 0;
}
