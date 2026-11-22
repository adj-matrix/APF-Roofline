/* E2 (parallel): a parallel sequential scan of a table's segment files, as PostgreSQL 18 runs one, without
 * PostgreSQL. docs/ext/parallel.md, M1.
 *
 *   replica_par @LIST P ENGINE DIRECT QD WORK_K DUR_S
 *     P        participants (processes; PostgreSQL: the leader + P - 1 workers)
 *     ENGINE   psync: preadv of up to 16 x 8 KiB, blocking (io_method = sync)
 *              uring: io_uring READV, QD reads in flight per participant, consumed in order; buffered reads with
 *                     more than 4 in flight get IOSQE_ASYNC (method_io_uring.c:442)
 *              none : no I/O, only the work (K busy cores: the clock alone)
 *     DIRECT   1: O_DIRECT; 0: buffered (the caller drops the page cache first)
 *     WORK_K   work per 128 KiB read, thousands of iterations of a dependent add (~1 cycle each); fixed cycles,
 *              not fixed time (E1, M9). Per block: WORK_K * 1000 / 16.
 *     DUR_S    stop after DUR_S s if the table is not done (psync / uring); the time to run (none)
 * Blocks are handed out as tableam.c does (table_block_parallelscan_nextpage): chunks of
 * min(nextpow2(nblocks / 2048), 8192) blocks from one shared counter (fetch-and-add), halved whenever a
 * participant has allocated past nblocks - chunk * 64 (ramp-down), down to single blocks at the end. Each
 * participant combines consecutive blocks into reads of up to 16 blocks (read_stream, io_combine_limit 16).
 * Prints one JSON line: reads, blocks, elapsed, per participant: blocks, end time.
 * Build: cc -O2 -o replica_par replica_par.c  (raw io_uring syscalls; no liburing headers here)
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
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define BLCKSZ 8192
#define COMBINE 16
#define MAXQD 64
#define MAXFILES 256
#define MAXP 64
#define NCHUNKS 2048
#define RAMPDOWN 64
#define MAXCHUNK 8192

static char names[MAXFILES][512];
static int nfiles, fds[MAXFILES], direct, qd;
static uint64_t seg_start[MAXFILES + 1], nblocks;     /* in blocks */
static long iters_per_block;

struct shared {
    _Atomic uint64_t nallocated;
    double t0;
    uint64_t blocks[MAXP], reads[MAXP];
    double t_end[MAXP];
};
static struct shared *sh;

static double now(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec * 1e-9;
}
static void work(long blocks) {
    unsigned long x = 0;
    for (long i = 0, n = blocks * iters_per_block; i < n; i++) __asm__ volatile("add $1, %0" : "+r"(x));
}
static uint32_t nextpow2(uint32_t v) {
    uint32_t p = 1;
    while (p < v) p <<= 1;
    return p;
}

/* table_block_parallelscan_nextpage, per participant */
struct cursor { uint64_t local_alloc, chunk_size, remaining; };
static int64_t next_block(struct cursor *c) {
    uint64_t n;
    if (c->remaining > 0) {
        n = ++c->local_alloc;
        c->remaining--;
    } else {
        if (c->chunk_size > 1 && c->local_alloc > (nblocks > c->chunk_size * RAMPDOWN ? nblocks - c->chunk_size * RAMPDOWN : 0))
            c->chunk_size >>= 1;
        n = c->local_alloc = atomic_fetch_add(&sh->nallocated, c->chunk_size);
        c->remaining = c->chunk_size - 1;
    }
    return n >= nblocks ? -1 : (int64_t)n;
}

static int file_of(uint64_t blk) {
    int i = 0;
    while (i + 1 < nfiles && blk >= seg_start[i + 1]) i++;
    return i;
}

/* a read = up to COMBINE consecutive blocks inside one segment file */
struct rd { uint64_t blk; int n; };
static int64_t pending = -2;                 /* one block fetched ahead that did not fit the last read */
static int next_read(struct cursor *c, struct rd *r) {
    int64_t b = pending != -2 ? pending : next_block(c);
    pending = -2;
    if (b < 0) return 0;
    r->blk = (uint64_t)b;
    r->n = 1;
    int f = file_of(r->blk);
    while (r->n < COMBINE) {
        int64_t nb = next_block(c);
        if (nb < 0 || (uint64_t)nb != r->blk + r->n || file_of((uint64_t)nb) != f) { pending = nb < 0 ? -1 : nb; break; }
        r->n++;
    }
    return 1;
}

struct ring {
    int fd;
    unsigned *sq_tail, *sq_mask, *sq_array, *cq_head, *cq_tail, *cq_mask;
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
    r->sq_tail = (unsigned *)(sq + p.sq_off.tail);
    r->sq_mask = (unsigned *)(sq + p.sq_off.ring_mask);
    r->sq_array = (unsigned *)(sq + p.sq_off.array);
    r->cq_head = (unsigned *)(cq + p.cq_off.head);
    r->cq_tail = (unsigned *)(cq + p.cq_off.tail);
    r->cq_mask = (unsigned *)(cq + p.cq_off.ring_mask);
    r->cqes = (struct io_uring_cqe *)(cq + p.cq_off.cqes);
    return 0;
}
static void ring_submit(struct ring *r, int fd, struct iovec *iov, int niov, off_t off, uint64_t tag, int async) {
    unsigned tail = *r->sq_tail, idx = tail & *r->sq_mask;
    struct io_uring_sqe *s = &r->sqes[idx];
    memset(s, 0, sizeof *s);
    s->opcode = IORING_OP_READV;
    s->fd = fd;
    s->addr = (uint64_t)(uintptr_t)iov;
    s->len = niov;
    s->off = off;
    s->user_data = tag;
    if (async) s->flags |= IOSQE_ASYNC;
    r->sq_array[idx] = idx;
    atomic_store_explicit((_Atomic unsigned *)r->sq_tail, tail + 1, memory_order_release);
    syscall(__NR_io_uring_enter, r->fd, 1, 0, 0, NULL, 0);
}
static void ring_reap(struct ring *r, int *done, int *res) {
    unsigned head = *r->cq_head;
    while (head != atomic_load_explicit((_Atomic unsigned *)r->cq_tail, memory_order_acquire)) {
        struct io_uring_cqe *c = &r->cqes[head & *r->cq_mask];
        done[c->user_data] = 1;
        res[c->user_data] = c->res;
        head++;
    }
    atomic_store_explicit((_Atomic unsigned *)r->cq_head, head, memory_order_release);
}

static void participant(int me, int uring, double t_stop) {
    for (int i = 0; i < nfiles; i++) {
        fds[i] = open(names[i], O_RDONLY | (direct ? O_DIRECT : 0));
        if (fds[i] < 0) { perror(names[i]); exit(1); }
    }
    char *buf;
    if (posix_memalign((void **)&buf, 4096, (size_t)qd * COMBINE * BLCKSZ)) exit(1);
    memset(buf, 0, (size_t)qd * COMBINE * BLCKSZ);
    struct iovec iov[MAXQD][COMBINE];
    for (int q = 0; q < qd; q++)
        for (int i = 0; i < COMBINE; i++) iov[q][i] = (struct iovec){buf + ((size_t)q * COMBINE + i) * BLCKSZ, BLCKSZ};
    struct cursor c = {0, nextpow2((uint32_t)(nblocks / NCHUNKS > 1 ? nblocks / NCHUNKS : 1)), 0};
    if (c.chunk_size > MAXCHUNK) c.chunk_size = MAXCHUNK;
    uint64_t blocks = 0, reads = 0;
    struct rd r;
    if (!uring) {
        while (now() < t_stop && next_read(&c, &r)) {
            int f = file_of(r.blk);
            if (preadv(fds[f], iov[0], r.n, (off_t)(r.blk - seg_start[f]) * BLCKSZ) < 0) { perror("preadv"); exit(1); }
            work(r.n);
            blocks += r.n, reads++;
        }
    } else {
        struct ring ring;
        if (ring_init(&ring, (unsigned)qd)) { perror("io_uring_setup"); exit(1); }
        int done[MAXQD] = {0}, res[MAXQD] = {0}, nb[MAXQD] = {0}, inflight = 0, oldest = 0, more = 1;
        for (int q = 0; q < qd && more; q++) {
            if (!(more = next_read(&c, &r))) break;
            int f = file_of(r.blk);
            nb[q] = r.n;
            ring_submit(&ring, fds[f], iov[q], r.n, (off_t)(r.blk - seg_start[f]) * BLCKSZ, q, !direct && inflight > 4);
            inflight++;
        }
        while (inflight > 0) {
            ring_reap(&ring, done, res);
            while (!done[oldest]) {
                syscall(__NR_io_uring_enter, ring.fd, 0, 1, IORING_ENTER_GETEVENTS, NULL, 0);
                ring_reap(&ring, done, res);
            }
            if (res[oldest] < 0) { fprintf(stderr, "read error %d\n", res[oldest]); exit(1); }
            done[oldest] = 0;
            inflight--;
            work(nb[oldest]);
            blocks += nb[oldest], reads++;
            if (more && now() < t_stop && (more = next_read(&c, &r))) {
                int f = file_of(r.blk);
                nb[oldest] = r.n;
                ring_submit(&ring, fds[f], iov[oldest], r.n, (off_t)(r.blk - seg_start[f]) * BLCKSZ, oldest,
                            !direct && inflight > 4);
                inflight++;
            }
            oldest = (oldest + 1) % qd;
        }
    }
    sh->blocks[me] = blocks;
    sh->reads[me] = reads;
    sh->t_end[me] = now() - sh->t0;
}

int main(int argc, char **argv) {
    if (argc != 8) {
        fprintf(stderr, "usage: %s @LIST P psync|uring|none DIRECT QD WORK_K DUR_S\n", argv[0]);
        return 2;
    }
    int P = atoi(argv[2]);
    const char *engine = argv[3];
    direct = atoi(argv[4]), qd = atoi(argv[5]);
    double work_k = atof(argv[6]), dur = atof(argv[7]);
    iters_per_block = (long)(work_k * 1000 / COMBINE);
    int uring = strcmp(engine, "uring") == 0, none = strcmp(engine, "none") == 0;
    if (!uring) qd = 1;
    if (P < 1 || P > MAXP || qd < 1 || qd > MAXQD) return 2;
    sh = mmap(0, sizeof *sh, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    if (sh == MAP_FAILED) return 1;
    if (!none) {
        if (argv[1][0] != '@') return 2;
        FILE *l = fopen(argv[1] + 1, "r");
        if (!l) { perror("list"); return 1; }
        while (nfiles < MAXFILES && fgets(names[nfiles], sizeof names[nfiles], l)) {
            names[nfiles][strcspn(names[nfiles], "\n")] = 0;
            if (names[nfiles][0]) nfiles++;
        }
        fclose(l);
        for (int i = 0; i < nfiles; i++) {
            struct stat st;
            if (stat(names[i], &st)) { perror(names[i]); return 1; }
            seg_start[i] = nblocks;
            nblocks += (uint64_t)st.st_size / BLCKSZ;
        }
        seg_start[nfiles] = nblocks;
    }
    sh->t0 = now();
    double t_stop = sh->t0 + dur;
    for (int p = 0; p < P; p++) {
        pid_t pid = fork();
        if (pid == 0) {
            if (none) {                                   /* K busy cores: work in slices until DUR_S */
                uint64_t n = 0;
                while (now() < t_stop) { work(COMBINE); n++; }
                sh->reads[p] = n;
                sh->t_end[p] = now() - sh->t0;
            } else
                participant(p, uring, t_stop);
            _exit(0);
        }
    }
    for (int p = 0; p < P; p++) wait(NULL);
    double el = now() - sh->t0;
    uint64_t blocks = 0, reads = 0;
    for (int p = 0; p < P; p++) blocks += sh->blocks[p], reads += sh->reads[p];
    printf("{\"P\": %d, \"engine\": \"%s\", \"direct\": %d, \"reads\": %lu, \"blocks\": %lu, \"nblocks\": %lu, "
           "\"elapsed_s\": %.4f, \"MiBps\": %.1f, \"part_blocks\": [", P, engine, direct, reads, blocks, nblocks, el,
           blocks * (double)BLCKSZ / 1048576 / el);
    for (int p = 0; p < P; p++) printf("%s%lu", p ? ", " : "", sh->blocks[p]);
    printf("], \"part_end_s\": [");
    for (int p = 0; p < P; p++) printf("%s%.3f", p ? ", " : "", sh->t_end[p]);
    printf("]}\n");
    return 0;
}
