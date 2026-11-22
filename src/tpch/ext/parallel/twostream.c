/* E2 (parallel), M7: device rate of two sequential streams a fixed distance apart (docs/ext/parallel.md, M7).
 *
 *   twostream @LIST GAP_MIB DIRECT DUR_S [START_GIB [CHUNK_MIB]]
 * Two processes read the concatenation of LIST's files (lineitem's segments, read-only) sequentially in 128 KiB
 * preads, one from START_GIB (default 4) and one GAP_MIB further on, until DUR_S. DIRECT 1: O_DIRECT; 0: buffered
 * (the caller drops the page cache first; the kernel's read-ahead applies). Prints one JSON line: MiB/s total and
 * per stream. Nothing but the distance between the two streams changes between cells.
 * With CHUNK_MIB > 0 the streams leapfrog like a parallel scan's two participants: stream s reads chunks s, s + 2,
 * s + 4, ... of CHUNK_MIB each (from START_GIB), and stream 1 starts GAP_MIB into its first chunk, so the two stay
 * GAP_MIB out of phase within their chunks for the whole cell (GAP 0: in phase).
 * Build: cc -O2 -o twostream twostream.c
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define CHUNK (128 * 1024)
#define MAXFILES 256

static char names[MAXFILES][512];
static int nfiles;
static off_t starts[MAXFILES + 1], total;

static double now(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec * 1e-9;
}

int main(int argc, char **argv) {
    if (argc < 5) {
        fprintf(stderr, "usage: %s @LIST GAP_MIB DIRECT DUR_S [START_GIB]\n", argv[0]);
        return 2;
    }
    double gap = atof(argv[2]), dur = atof(argv[4]), start_gib = argc > 5 ? atof(argv[5]) : 4;
    off_t chunk = argc > 6 ? (off_t)(atof(argv[6]) * 1024 * 1024) / CHUNK * CHUNK : 0;
    int direct = atoi(argv[3]);
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
    uint64_t *bytes = mmap(0, 2 * sizeof(uint64_t), PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    off_t pos0[2];
    pos0[0] = (off_t)(start_gib * 1024) * 1024 * 1024 / CHUNK * CHUNK;
    pos0[1] = pos0[0] + (chunk ? chunk : 0) + (off_t)(gap * 1024 * 1024) / CHUNK * CHUNK;
    double t0 = now();
    for (int s = 0; s < 2; s++) {
        if (fork() == 0) {
            int fds[MAXFILES];
            for (int i = 0; i < nfiles; i++)
                if ((fds[i] = open(names[i], O_RDONLY | (direct ? O_DIRECT : 0))) < 0) { perror(names[i]); _exit(1); }
            char *buf;
            if (posix_memalign((void **)&buf, 4096, CHUNK)) _exit(1);
            off_t pos = pos0[s];
            while (now() < t0 + dur && pos < total) {
                int f = 0;
                while (f + 1 < nfiles && pos >= starts[f + 1]) f++;
                if (pread(fds[f], buf, CHUNK, pos - starts[f]) != CHUNK) { perror("pread"); _exit(1); }
                pos += CHUNK;
                bytes[s] += CHUNK;
                if (chunk && (pos - pos0[0]) % chunk == 0) pos += chunk;     /* skip the other stream's chunk */
            }
            _exit(0);
        }
    }
    wait(NULL);
    wait(NULL);
    double el = now() - t0;
    printf("{\"chunk_MiB\": %ld, \"gap_MiB\": %.1f, \"direct\": %d, \"elapsed_s\": %.3f, \"MiBps\": %.1f, \"stream_MiBps\": [%.1f, %.1f]}\n",
           (long)(chunk >> 20), gap, direct, el, (bytes[0] + bytes[1]) / 1048576.0 / el, bytes[0] / 1048576.0 / el, bytes[1] / 1048576.0 / el);
    return 0;
}
