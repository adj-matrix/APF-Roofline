/* E2 (parallel), M8: a parallel scan's two participants in io_method = worker, without PostgreSQL
 * (docs/ext/parallel.md, M8).
 *
 *   leapq @LIST DIRECT PHASE_MIB READERS DUR_S [CHUNK_MIB [START_GIB [OWN_FD [BATCH]]]]
 * Two participant threads leapfrog like a parallel scan: participant p reads chunks p, p + 2, p + 4, ... of
 * CHUNK_MIB (default 64) from START_GIB (default 4); participant 1 starts PHASE_MIB into its first chunk, so the
 * two stay PHASE_MIB out of phase. Each keeps 16 reads of 128 KiB in flight (effective_io_concurrency 16) and
 * consumes them in order, with no work. The reads go into one shared FIFO queue served by READERS reader threads
 * (the I/O workers): a submit wakes one idle reader. Each reader has its own file descriptors (OWN_FD 1, default,
 * as PostgreSQL's workers do; 0: all share one set). DIRECT 1: O_DIRECT; 0: buffered (the caller drops the page
 * cache first). BATCH (default 1): a participant submits only when BATCH of its slots are free, then BATCH reads
 * at once, as PostgreSQL's read stream does in batch mode (READ_STREAM_USE_BATCHING). Prints one JSON line: MiB/s,
 * reads per reader.
 * Build: cc -O2 -pthread -o leapq leapq.c
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define CHUNK (128 * 1024)
#define MAXFILES 256
#define EIC 16
#define QCAP 64
#define MAXR 16

static char names[MAXFILES][512];
static int nfiles, direct, nreaders, own_fd = 1, batch = 1, shared_fds[MAXFILES];
static off_t starts[MAXFILES + 1], total, chunk, base, phase;
static double t_stop;

static double now(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec * 1e-9;
}

struct req { off_t pos; int part, slot; };
static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t q_cv = PTHREAD_COND_INITIALIZER, done_cv[2];
static struct req queue[QCAP];
static int qh, qt, stop, done_flag[2][EIC];
static long rreads[MAXR], bytes;

static void submit(struct req r) {
    pthread_mutex_lock(&mu);
    queue[qt++ % QCAP] = r;
    pthread_cond_signal(&q_cv);                  /* wake one idle reader */
    pthread_mutex_unlock(&mu);
}

static int open_all(int *fds) {
    for (int i = 0; i < nfiles; i++)
        if ((fds[i] = open(names[i], O_RDONLY | (direct ? O_DIRECT : 0))) < 0) { perror(names[i]); return -1; }
    return 0;
}

static void *reader(void *arg) {
    int me = (int)(long)arg, mine[MAXFILES], *fds = own_fd ? mine : shared_fds;
    if (own_fd && open_all(mine)) exit(1);
    char *buf;
    if (posix_memalign((void **)&buf, 4096, CHUNK)) exit(1);
    pthread_mutex_lock(&mu);
    for (;;) {
        while (qh == qt && !stop) pthread_cond_wait(&q_cv, &mu);
        if (stop) break;
        struct req r = queue[qh++ % QCAP];
        pthread_mutex_unlock(&mu);
        int f = 0;
        while (f + 1 < nfiles && r.pos >= starts[f + 1]) f++;
        if (pread(fds[f], buf, CHUNK, r.pos - starts[f]) != CHUNK) { perror("pread"); exit(1); }
        pthread_mutex_lock(&mu);
        rreads[me]++;
        bytes += CHUNK;
        done_flag[r.part][r.slot] = 1;
        pthread_cond_signal(&done_cv[r.part]);
    }
    pthread_mutex_unlock(&mu);
    return NULL;
}

/* leapfrog position stream of participant p */
static off_t next_pos(int p, off_t *cur) {
    off_t pos = *cur;
    *cur += CHUNK;
    if ((*cur - base) % chunk == 0) *cur += chunk;   /* end of own chunk: skip the other's */
    (void)p;
    return pos;
}

static void *participant(void *arg) {
    int p = (int)(long)arg, oldest = 0;
    off_t cur = base + (p ? chunk + phase : 0);
    for (int s = 0; s < EIC; s++) submit((struct req){next_pos(p, &cur), p, s});
    while (now() < t_stop && cur < total) {
        int slots[EIC];
        for (int b = 0; b < batch; b++) {                     /* consume BATCH completed reads in order */
            pthread_mutex_lock(&mu);
            while (!done_flag[p][oldest] && !stop) pthread_cond_wait(&done_cv[p], &mu);
            done_flag[p][oldest] = 0;
            pthread_mutex_unlock(&mu);
            slots[b] = oldest;
            oldest = (oldest + 1) % EIC;
        }
        for (int b = 0; b < batch; b++) submit((struct req){next_pos(p, &cur), p, slots[b]});
    }
    return NULL;
}

int main(int argc, char **argv) {
    if (argc < 6) {
        fprintf(stderr, "usage: %s @LIST DIRECT PHASE_MIB READERS DUR_S [CHUNK_MIB [START_GIB [OWN_FD]]]\n", argv[0]);
        return 2;
    }
    direct = atoi(argv[2]);
    phase = (off_t)(atof(argv[3]) * 1048576) / CHUNK * CHUNK;
    nreaders = atoi(argv[4]);
    double dur = atof(argv[5]);
    chunk = (off_t)(atof(argc > 6 ? argv[6] : "64") * 1048576) / CHUNK * CHUNK;
    base = (off_t)(atof(argc > 7 ? argv[7] : "4") * 1073741824.0) / CHUNK * CHUNK;
    if (argc > 8) own_fd = atoi(argv[8]);
    if (argc > 9) batch = atoi(argv[9]);
    if (batch < 1 || batch > EIC || EIC % batch) return 2;
    if (nreaders < 1 || nreaders > MAXR) return 2;
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
        starts[i] = total;
        total += st.st_size / CHUNK * CHUNK;
    }
    starts[nfiles] = total;
    if (!own_fd && open_all(shared_fds)) return 1;
    pthread_cond_init(&done_cv[0], NULL);
    pthread_cond_init(&done_cv[1], NULL);
    pthread_t rt[MAXR], pt[2];
    for (int r = 0; r < nreaders; r++) pthread_create(&rt[r], NULL, reader, (void *)(long)r);
    usleep(100000);
    double t0 = now();
    t_stop = t0 + dur;
    for (int p = 0; p < 2; p++) pthread_create(&pt[p], NULL, participant, (void *)(long)p);
    for (int p = 0; p < 2; p++) pthread_join(pt[p], NULL);
    double el = now() - t0;
    pthread_mutex_lock(&mu);
    long b = bytes;
    stop = 1;
    pthread_cond_broadcast(&q_cv);
    pthread_mutex_unlock(&mu);
    for (int r = 0; r < nreaders; r++) pthread_join(rt[r], NULL);
    printf("{\"direct\": %d, \"batch\": %d, \"phase_MiB\": %.0f, \"readers\": %d, \"own_fd\": %d, \"elapsed_s\": %.3f, \"MiBps\": %.1f, "
           "\"reader_reads\": [", direct, batch, phase / 1048576.0, nreaders, own_fd, el, b / 1048576.0 / el);
    for (int r = 0; r < nreaders; r++) printf("%s%ld", r ? ", " : "", rreads[r]);
    printf("]}\n");
    return 0;
}
