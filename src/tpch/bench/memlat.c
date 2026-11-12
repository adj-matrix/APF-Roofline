/* Dependent random-access latency vs working-set size (pointer chase, lmbench lat_mem_rd style).
 *
 *   memlat SIZE_BYTES [ACCESSES]   -> prints "SIZE_BYTES NS_PER_ACCESS"
 *
 * The buffer is cut into 64-byte cache lines linked in one random cycle, so every load depends on
 * the previous one (no overlap, no prefetch). Pages are ordinary malloc memory (4 KiB, or THP if
 * the system gives it), like a PostgreSQL backend's private memory, so TLB misses are included.
 * This is the cost model of a hash-table probe whose bucket or tuple is not in cache.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

static uint64_t rng = 88172645463325252ULL;
static uint64_t next(void) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return rng; }

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: memlat SIZE_BYTES [ACCESSES]\n"); return 2; }
    size_t size = strtoull(argv[1], 0, 10), n = size / 64;
    long accesses = argc > 2 ? atol(argv[2]) : 10000000;
    char *buf = aligned_alloc(4096, (n * 64 + 4095) / 4096 * 4096);
    size_t *perm = malloc(n * sizeof *perm);
    if (!buf || !perm || n < 2) { fprintf(stderr, "alloc failed\n"); return 1; }
    for (size_t i = 0; i < n; i++) perm[i] = i;
    for (size_t i = n - 1; i > 0; i--) { size_t j = next() % (i + 1), t = perm[i]; perm[i] = perm[j]; perm[j] = t; }
    for (size_t i = 0; i < n; i++) *(void **)(buf + perm[i] * 64) = buf + perm[(i + 1) % n] * 64;
    void **p = (void **)(buf + perm[0] * 64);
    free(perm);
    for (size_t i = 0; i < n; i++) p = (void **)*p;          /* warm: touch every line once */
    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    for (long i = 0; i < accesses; i++) p = (void **)*p;
    clock_gettime(CLOCK_MONOTONIC, &t1);
    double ns = (t1.tv_sec - t0.tv_sec) * 1e9 + (t1.tv_nsec - t0.tv_nsec);
    printf("%zu %.3f\n", size, ns / accesses);
    return p == 0;                                            /* keep the chain live */
}
