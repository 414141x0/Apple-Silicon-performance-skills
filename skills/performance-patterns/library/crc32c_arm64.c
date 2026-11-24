/*
 * Copyright (c) 2026. MIT license.
 *
 * Original Apple Silicon / AArch64 CRC32C implementation.
 *
 * CRC32C (Castagnoli) polynomial 0x1EDC6F41, reflected 0x82F63B78 —
 * the same polynomial as library/crc32c_portable.c.
 *
 * Path: 3 independent CRC32CX streams (__crc32cd) on sequential thirds of
 * the buffer, then GF(2) combine. A 64-byte PMULL fold (vmull_p64) can be
 * added later; this file prefers a combine that matches the portable table.
 *
 * Requires CRC32 (hw.optional.armv8_crc32, all M1 / A14+). Compile with
 * -mcpu=apple-m1 (or later) or __attribute__((target("crc"))).
 *
 * Self-test (crc32c_castagnoli):
 *   crc32c(0, "", 0)            == 0x00000000
 *   crc32c(0, "123456789", 9)   == 0xE3069283
 */

#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <arm_acle.h>

/* Reflected Castagnoli polynomial — matches crc32c_portable.c's table. */
#define CRC32C_POLY_REFL 0x82F63B78u
#define GF2_DIM 32

static uint32_t gf2_matrix_times(const uint32_t *mat, uint32_t vec)
{
    uint32_t sum = 0;
    while (vec) {
        if (vec & 1)
            sum ^= *mat;
        vec >>= 1;
        mat++;
    }
    return sum;
}

static void gf2_matrix_square(uint32_t *square, const uint32_t *mat)
{
    int n;
    for (n = 0; n < GF2_DIM; n++)
        square[n] = gf2_matrix_times(mat, mat[n]);
}

/*
 * Combine public-domain CRC32C values of concat(A, B) given CRC(A), CRC(B),
 * and len(B). Same construction as zlib crc32_combine, with the CRC32C poly.
 */
static uint32_t crc32c_combine(uint32_t crc1, uint32_t crc2, size_t len2)
{
    uint32_t even[GF2_DIM];
    uint32_t odd[GF2_DIM];
    uint32_t row;
    int n;

    if (len2 == 0)
        return crc1;

    odd[0] = CRC32C_POLY_REFL;
    row = 1;
    for (n = 1; n < GF2_DIM; n++) {
        odd[n] = row;
        row <<= 1;
    }

    gf2_matrix_square(even, odd);   /* two zero bits */
    gf2_matrix_square(odd, even);   /* four zero bits = one zero byte */

    do {
        gf2_matrix_square(even, odd);
        if (len2 & 1)
            crc1 = gf2_matrix_times(even, crc1);
        len2 >>= 1;
        if (len2 == 0)
            break;
        gf2_matrix_square(odd, even);
        if (len2 & 1)
            crc1 = gf2_matrix_times(odd, crc1);
        len2 >>= 1;
    } while (len2 != 0);

    return crc1 ^ crc2;
}

/* crc is already inverted (hardware domain). Does not invert at start/end. */
static uint32_t crc32c_hw_raw(uint32_t crc, const uint8_t *p, size_t len)
{
    while (len && ((uintptr_t)p & 7u)) {
        crc = __crc32cb(crc, *p++);
        len--;
    }
    while (len >= 8) {
        uint64_t v;
        memcpy(&v, p, 8);
        crc = __crc32cd(crc, v);
        p += 8;
        len -= 8;
    }
    if (len >= 4) {
        uint32_t v;
        memcpy(&v, p, 4);
        crc = __crc32cw(crc, v);
        p += 4;
        len -= 4;
    }
    if (len >= 2) {
        uint16_t v;
        memcpy(&v, p, 2);
        crc = __crc32ch(crc, v);
        p += 2;
        len -= 2;
    }
    if (len)
        crc = __crc32cb(crc, *p);
    return crc;
}

uint32_t crc32c_arm64_impl(uint32_t crc, const char *buf, size_t len)
{
    const uint8_t *p = (const uint8_t *)buf;
    size_t block;

    crc = ~crc;

    /* Three sequential thirds, each a multiple of 8 bytes. Small buffers
     * stay on one stream so combine overhead does not dominate. */
    if (len >= 192) {
        block = (len / 24) * 8;
        {
            uint32_t r0 = crc32c_hw_raw(crc, p, block);
            uint32_t r1 = crc32c_hw_raw(0xffffffffu, p + block, block);
            uint32_t r2 = crc32c_hw_raw(0xffffffffu, p + 2 * block, block);
            uint32_t pub = crc32c_combine(~r0, ~r1, block);
            pub = crc32c_combine(pub, ~r2, block);
            crc = ~pub;
        }
        p += 3 * block;
        len -= 3 * block;
    }

    crc = crc32c_hw_raw(crc, p, len);
    return ~crc;
}

#ifdef CRC32C_SELFTEST
#include <stdio.h>

extern uint32_t crc32c_portable_impl(uint32_t crc, const char *buf, size_t len);

int main(void)
{
    static const unsigned char vec[] = "123456789";
    uint32_t a, b;
    size_t n;
    unsigned char buf[1024];
    int i, fail = 0;

    a = crc32c_arm64_impl(0, "", 0);
    b = crc32c_portable_impl(0, "", 0);
    if (a != 0 || b != 0) {
        fprintf(stderr, "empty: arm=%08x portable=%08x expected 0\n", a, b);
        fail = 1;
    }

    a = crc32c_arm64_impl(0, (const char *)vec, 9);
    b = crc32c_portable_impl(0, (const char *)vec, 9);
    if (a != 0xE3069283u || b != 0xE3069283u) {
        fprintf(stderr, "123456789: arm=%08x portable=%08x expected E3069283\n",
                a, b);
        fail = 1;
    }

    for (i = 0; i < 1024; i++)
        buf[i] = (unsigned char)(i * 37u);

    for (n = 0; n <= 1024; n++) {
        a = crc32c_arm64_impl(0, (const char *)buf, n);
        b = crc32c_portable_impl(0, (const char *)buf, n);
        if (a != b) {
            fprintf(stderr, "len %zu: arm=%08x portable=%08x\n", n, a, b);
            fail = 1;
            break;
        }
        if (n >= 3) {
            uint32_t p0 = crc32c_arm64_impl(0, (const char *)buf, n / 3);
            uint32_t p1 = crc32c_arm64_impl(p0, (const char *)buf + n / 3,
                                            n - n / 3);
            if (p1 != a) {
                fprintf(stderr, "split len %zu: one-shot=%08x split=%08x\n",
                        n, a, p1);
                fail = 1;
                break;
            }
        }
    }

    if (fail)
        return 1;
    puts("crc32c_arm64 self-test ok");
    return 0;
}
#endif
