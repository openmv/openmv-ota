/*
 * OpenMV boot stub -- installing a slot's components, and the install record.
 *
 * A component is a raw LZMA1 stream (256 KiB dictionary, lc3 lp0 pb2) in the slot.
 * Its compressed hash is checked BEFORE a byte reaches the decoder; the decoder's
 * output is hashed and checked against the signed installed hash before the record
 * is written. Output is flashed one erase block at a time: a block that already
 * holds the right bytes is skipped, anything else is erased, programmed and read
 * back. The record is erased first and written last, so a power cut anywhere in
 * between leaves "no record" -- the next boot installs again.
 */
#include "stub.h"
#include "port.h"
#include "sha256.h"
#include "LzmaDec.h"

#define IN_CHUNK            (4096)
#define LZMA_DICT           (256 * 1024)
#define LZMA_PROBS_BYTES    (32 * 1024)     // 1846 + (0x300 << (lc + lp)) probs of 2 bytes = 15980

// lc=3 lp=0 pb=2 -> (pb * 5 + lp) * 9 + lc = 93; dictionary 0x00040000 little-endian.
static const uint8_t lzma_props[LZMA_PROPS_SIZE] = { 93, 0x00, 0x00, 0x04, 0x00 };

static uint8_t lzma_dict[LZMA_DICT] __attribute__((section(".buffers"), aligned(32)));
static uint8_t lzma_probs[LZMA_PROBS_BYTES] __attribute__((section(".buffers"), aligned(32)));
static uint8_t out_block[STUB_FLASH_BLOCK] __attribute__((section(".buffers"), aligned(32)));
static uint8_t in_chunk[IN_CHUNK] __attribute__((section(".buffers"), aligned(32)));
static uint8_t cmp_chunk[IN_CHUNK] __attribute__((section(".buffers"), aligned(32)));
static uint8_t record_block[RECORD_BLOCK] __attribute__((section(".buffers"), aligned(32)));

// LzmaDec asks for two buffers: the probability model, then the dictionary.
static void *lzma_alloc(ISzAllocPtr p, size_t size) {
    (void) p;
    if (size <= sizeof(lzma_probs)) {
        return lzma_probs;
    }
    if (size <= sizeof(lzma_dict)) {
        return lzma_dict;
    }
    return NULL;
}

static void lzma_free(ISzAllocPtr p, void *address) {
    (void) p;
    (void) address;
}

static const ISzAlloc lzma_allocator = { lzma_alloc, lzma_free };

static uint32_t rd32(const uint8_t *p) {
    return (uint32_t) p[0] | ((uint32_t) p[1] << 8) | ((uint32_t) p[2] << 16) | ((uint32_t) p[3] << 24);
}

void record_read(record_t *r) {
    memset(r, 0, sizeof(*r));
    if (port_flash_read(STUB_RECORD_ADDR, record_block, RECORD_BLOCK) != 0) {
        return;
    }
    const record_header_t *h = (const record_header_t *) record_block;
    if (h->magic != RECORD_MAGIC || h->record_version != RECORD_VERSION ||
        h->comp_count > TRAILER_MAX_COMPONENTS || h->slot > 1 || h->reason > REASON_REINSTALLED) {
        return;
    }
    uint32_t end = sizeof(record_header_t) + h->comp_count * sizeof(record_entry_t);
    if (crc32_of(record_block, end) != rd32(record_block + end)) {
        return;
    }
    r->hdr = *h;
    memcpy(r->entries, record_block + sizeof(record_header_t), h->comp_count * sizeof(record_entry_t));
    r->valid = true;
}

// The record names this slot's components exactly (destination, size, installed hash).
bool record_matches(const record_t *r, const slot_t *s) {
    if (!r->valid || !s->ok || r->hdr.slot != s->index || r->hdr.comp_count != s->hdr->comp_count) {
        return false;
    }
    for (uint32_t i = 0; i < r->hdr.comp_count; i++) {
        const record_entry_t *e = &r->entries[i];
        const component_t *c = &s->comps[i];
        if (e->dest != c->dest || e->installed_size != c->installed_size ||
            memcmp(e->installed_sha256, c->installed_sha256, 32) != 0) {
            return false;
        }
    }
    return true;
}

// The firmware area holds what the record says was installed.
bool record_area_ok(const record_t *r) {
    if (!r->valid) {
        return false;
    }
    for (uint32_t i = 0; i < r->hdr.comp_count; i++) {
        const record_entry_t *e = &r->entries[i];
        uint8_t hash[32];
        if (e->installed_size == 0 || e->dest < STUB_INSTALL_START || e->dest > STUB_INSTALL_END ||
            e->installed_size > STUB_INSTALL_END - e->dest) {
            return false;
        }
        if (!sha256_flash(e->dest, e->installed_size, hash) || memcmp(hash, e->installed_sha256, 32) != 0) {
            return false;
        }
    }
    return true;
}

// Does flash at addr already hold these n bytes?
static bool flash_equals(uint32_t addr, const uint8_t *data, uint32_t n) {
    while (n) {
        uint32_t m = (n < IN_CHUNK) ? n : IN_CHUNK;
        if (port_flash_read(addr, cmp_chunk, m) != 0 || memcmp(cmp_chunk, data, m) != 0) {
            return false;
        }
        addr += m;
        data += m;
        n -= m;
    }
    return true;
}

// One decoded block (n <= STUB_FLASH_BLOCK bytes) at a block-aligned address.
static int flush_block(uint32_t addr, const uint8_t *data, uint32_t n) {
    port_led_blink(50);
    if (flash_equals(addr, data, n)) {
        return 0;
    }
    if (port_flash_erase(addr) != 0) {
        return -1;
    }
    // Program in 16-byte units: round the tail up (the pad is erased 0xFF already).
    uint32_t m = (n + 15) & ~15U;
    if (m > n) {
        memset((uint8_t *) data + n, 0xFF, m - n);
    }
    if (port_flash_write(addr, data, m) != 0) {
        return -1;
    }
    if (!flash_equals(addr, data, n)) {
        return -1;
    }
    return 0;
}

static int install_component(const slot_t *s, const component_t *c) {
    CLzmaDec dec;
    LzmaDec_Construct(&dec);
    if (LzmaDec_Allocate(&dec, lzma_props, LZMA_PROPS_SIZE, &lzma_allocator) != SZ_OK) {
        return -1;
    }
    LzmaDec_Init(&dec);

    CRYAL_SHA256_CTX blob_ctx, out_ctx;
    sha256_init(&blob_ctx);
    sha256_init(&out_ctx);

    uint32_t in_pos = 0;        // consumed from the blob
    uint32_t in_off = 0, in_avail = 0;
    uint32_t out_total = 0;     // produced
    uint32_t block_addr = c->dest;
    uint32_t block_fill = 0;

    while (out_total < c->installed_size) {
        if (in_avail == 0) {
            uint32_t n = c->size - in_pos;
            if (n == 0) {
                return -1;      // the stream ended before installed_size bytes came out
            }
            if (n > IN_CHUNK) {
                n = IN_CHUNK;
            }
            if (port_flash_read(s->start + c->offset + in_pos, in_chunk, n) != 0) {
                return -1;
            }
            sha256_update(&blob_ctx, in_chunk, n);
            in_pos += n;
            in_off = 0;
            in_avail = n;
        }
        SizeT dest_len = STUB_FLASH_BLOCK - block_fill;
        if (dest_len > c->installed_size - out_total) {
            dest_len = c->installed_size - out_total;
        }
        SizeT src_len = in_avail;
        ELzmaStatus status;
        SRes res = LzmaDec_DecodeToBuf(&dec, out_block + block_fill, &dest_len,
                                       in_chunk + in_off, &src_len, LZMA_FINISH_ANY, &status);
        if (res != SZ_OK) {
            return -1;
        }
        if (dest_len == 0 && src_len == 0) {
            return -1;          // no progress: a truncated or malformed stream
        }
        in_off += src_len;
        in_avail -= src_len;
        block_fill += dest_len;
        out_total += dest_len;
        if (block_fill == STUB_FLASH_BLOCK || out_total == c->installed_size) {
            sha256_update(&out_ctx, out_block, block_fill);
            if (flush_block(block_addr, out_block, block_fill) != 0) {
                return -1;
            }
            block_addr += STUB_FLASH_BLOCK;
            block_fill = 0;
        }
    }

    // What went in was the blob we verified; what came out is what the trailer signed.
    uint8_t hash[32];
    if (in_pos != c->size) {
        // The blob is longer than the stream needed: hash the rest so the check below
        // still covers every byte the trailer describes.
        while (in_pos < c->size) {
            uint32_t n = c->size - in_pos;
            if (n > IN_CHUNK) {
                n = IN_CHUNK;
            }
            if (port_flash_read(s->start + c->offset + in_pos, in_chunk, n) != 0) {
                return -1;
            }
            sha256_update(&blob_ctx, in_chunk, n);
            in_pos += n;
        }
    }
    sha256_final(&blob_ctx, hash);
    if (memcmp(hash, c->sha256, 32) != 0) {
        return -1;
    }
    sha256_final(&out_ctx, hash);
    if (memcmp(hash, c->installed_sha256, 32) != 0) {
        return -1;
    }
    return 0;
}

static int write_record(const slot_t *s, uint32_t reason) {
    memset(record_block, 0xFF, RECORD_BLOCK);
    record_header_t *h = (record_header_t *) record_block;
    h->magic = RECORD_MAGIC;
    h->record_version = RECORD_VERSION;
    h->stub_version = STUB_VERSION;
    h->slot = s->index;
    h->comp_count = s->hdr->comp_count;
    h->reason = reason;
    h->reserved = 0;
    record_entry_t *e = (record_entry_t *) (record_block + sizeof(record_header_t));
    for (uint32_t i = 0; i < h->comp_count; i++) {
        e[i].dest = s->comps[i].dest;
        e[i].installed_size = s->comps[i].installed_size;
        memcpy(e[i].installed_sha256, s->comps[i].installed_sha256, 32);
    }
    uint32_t end = sizeof(record_header_t) + h->comp_count * sizeof(record_entry_t);
    uint32_t crc = crc32_of(record_block, end);
    record_block[end] = crc & 0xFF;
    record_block[end + 1] = (crc >> 8) & 0xFF;
    record_block[end + 2] = (crc >> 16) & 0xFF;
    record_block[end + 3] = (crc >> 24) & 0xFF;
    uint32_t n = (end + 4 + 15) & ~15U;
    if (port_flash_write(STUB_RECORD_ADDR, record_block, n) != 0) {
        return -1;
    }
    return flash_equals(STUB_RECORD_ADDR, record_block, n) ? 0 : -1;
}

int install_slot(const slot_t *s, uint32_t reason) {
    // Gate the decoder: every blob's compressed hash first, through the memory map.
    for (uint32_t i = 0; i < s->hdr->comp_count; i++) {
        if (!verify_blob(s, &s->comps[i])) {
            log_line("blob hash mismatch");
            return -1;
        }
    }
    if (port_flash_prog_begin() != 0) {
        return -1;
    }
    int ret = -1;
    // No record until the install is complete.
    if (port_flash_erase(STUB_RECORD_ADDR & ~(STUB_FLASH_BLOCK - 1)) != 0) {
        goto done;
    }
    for (uint32_t i = 0; i < s->hdr->comp_count; i++) {
        log_kv("installing component", i);
        if (install_component(s, &s->comps[i]) != 0) {
            log_line("component install failed");
            goto done;
        }
    }
    ret = write_record(s, reason);
done:
    if (port_flash_prog_end() != 0) {
        return -1;
    }
    return ret;
}

// One 16-byte attempt mark into the slot's status sector (no erase).
int burn_attempt(slot_t *s) {
    if (s->attempts_used >= STATUS_ATTEMPTS_MAX) {
        return -1;
    }
    uint8_t unit[STATUS_ATTEMPT_UNIT];
    memset(unit, 0xFF, sizeof(unit));
    memcpy(unit, STATUS_ATTEMPT, MARKER_SIZE);
    uint32_t addr = s->start + slot_status_offset(s) + STATUS_ATTEMPTS_OFF +
                    s->attempts_used * STATUS_ATTEMPT_UNIT;
    if (port_flash_prog_begin() != 0) {
        return -1;
    }
    int ret = port_flash_write(addr, unit, sizeof(unit));
    if (port_flash_prog_end() != 0 || ret != 0) {
        return -1;
    }
    if (!flash_equals(addr, unit, sizeof(unit))) {
        return -1;
    }
    s->attempts_used++;
    return 0;
}
