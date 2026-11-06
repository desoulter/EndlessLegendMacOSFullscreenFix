/* Loader smoke test only: Unity hooks are not installed in this host process. */
#include <dlfcn.h>
#include <stdio.h>

int main(int argc, char **argv)
{
    if (argc != 2) return 2;
    void *library = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!library) {
        fprintf(stderr, "%s\n", dlerror());
        return 1;
    }
    puts("x86_64 native helper loaded");
    return 0;
}
