/* E4: hold GIB GiB of locked anonymous memory on the node the caller binds it to (numactl --membind), so the page
 * cache of processes bound to that node has that much less room. Prints "ready" once the memory is resident and
 * sleeps until killed. Run as root (mlock). Build: cc -O2 -o balloon balloon.c */
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc != 2) { fprintf(stderr, "usage: %s GIB\n", argv[0]); return 2; }
    size_t n = (size_t)(atof(argv[1]) * 1073741824.0);
    char *p = mmap(0, n, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE | MAP_LOCKED, -1, 0);
    if (p == MAP_FAILED) { perror("mmap"); return 1; }
    for (size_t i = 0; i < n; i += 4096) p[i] = 1;
    printf("ready %zu\n", n);
    fflush(stdout);
    for (;;) pause();
}
