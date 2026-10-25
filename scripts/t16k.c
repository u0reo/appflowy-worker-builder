/* jemalloc 起動対照テスト: 16K ページでは lg-page=14 ビルドのみ OK を返す */
#include <stdio.h>
#include <string.h>

extern void *_rjem_malloc(unsigned long);
extern void _rjem_free(void *);
extern void *_rjem_mallocx(unsigned long, int);
extern void _rjem_sdallocx(void *, unsigned long, int);

int main(void) {
    void *p[64];
    char *big;
    int i;
    for (i = 0; i < 64; i++) {
        unsigned long sz = (unsigned long)((i + 1) * 91u + (i % 7) * 4093u);
        p[i] = _rjem_malloc(sz);
        if (!p[i]) return 2;
        memset(p[i], i, sz);
    }
    big = _rjem_mallocx(4UL << 20, 0);
    if (!big) return 3;
    memset(big, 0x5a, 4UL << 20);
    _rjem_sdallocx(big, 4UL << 20, 0);
    for (i = 0; i < 64; i++) _rjem_free(p[i]);
    puts("OK");
    return 0;
}
