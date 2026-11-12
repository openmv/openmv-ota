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
#include "slot_tweak.h"
#include "journal.h"
#include "port.h"
#include "sha256.h"
#include "LzmaDec.h"

#define IN_CHUNK            (4096)
#define LZMA_DICT           (256 * 1024)
#define LZMA_PROBS_BYTES    (32 * 1024)     // 1846 + (0x300 << (lc + lp)) probs of 2 bytes = 15980

// lc=3 lp=0 pb=2 -> (pb * 5 + lp) * 9 + lc = 93; dictionary 0x00040000 little-endian.
static const uint8_t lzma_props[LZMA_PROPS_SIZE] = { 93, 0x00, 0x00, 0x04, 0x00 };

static uint8_t lzma_dict[LZMA_DICT] STUB_BUFFER;
static uint8_t lzma_probs[LZMA_PROBS_BYTES] STUB_BUFFER;
static uint8_t out_block[STUB_FLASH_BLOCK] STUB_BUFFER;
static uint8_t in_chunk[IN_CHUNK] STUB_BUFFER_SMALL;
static uint8_t cmp_chunk[IN_CHUNK] STUB_BUFFER_SMALL;
static uint8_t record_block[RECORD_BLOCK] STUB_BUFFER_SMALL;

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
    journal_load(record_block);                 // once: before an install can erase it
    const record_header_t *h = (const record_header_t *) record_block;
    if (h->magic != RECORD_MAGIC || h->record_version != RECORD_VERSION ||
        h->comp_count > TRAILER_MAX_COMPONENTS || h->slot > 1) {     // any reason: the list grows
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

// The tweak the firmware area is ciphered under, on a port that keeps it encrypted: 80
// bits of a hash over what is installed -- each component's destination, size and
// installed hash, exactly as the record lists them. Derived, not stored, so the record's
// format is what it was; and derived from the content, so two different installs never
// share a keystream (the same firmware installed twice is the same ciphertext, which
// tells nobody anything), a cut-off install resumes where it stopped, and an area an
// older stub installed in the clear simply stops matching its record and is installed
// again.
static const uint8_t crypt_label[16] = { 'o', 'm', 'v', '-', 'f', 'w', '-', 'c', 'r', 'y', 'p', 't', '-', 'v', '1', 0 };

static int crypt_select(const record_entry_t *entries, uint32_t count) {
    CRYAL_SHA256_CTX ctx;
    uint8_t hash[32];
    sha256_init(&ctx);
    sha256_update(&ctx, crypt_label, sizeof(crypt_label));
    sha256_update(&ctx, (const uint8_t *) entries, count * sizeof(record_entry_t));
    sha256_final(&ctx, hash);
    if (port_crypt_select(hash) != 0) {         // its first PORT_CRYPT_TWEAK_SIZE bytes
        return -1;
    }
    #ifdef STUB_CRYPT_START
    log_line("flash cipher on");
    #endif
    return 0;
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

// The firmware area hashes to what the record says was installed: as the firmware will
// read it (under the cipher, on a port that has one), or as it is stored.
static bool area_hashes(const record_t *r, bool stored) {
    if (!r->valid) {
        return false;
    }
    for (uint32_t i = 0; i < r->hdr.comp_count; i++) {
        const record_entry_t *e = &r->entries[i];
        uint8_t hash[32];
        if (e->installed_size == 0 || e->dest < STUB_INSTALL_START || e->dest > STUB_INSTALL_END ||
            e->installed_size > STUB_INSTALL_END - e->dest ||
            stub_sector_overlaps(e->dest, e->installed_size)) {
            return false;
        }
        if (i == 0 && (stored ? port_crypt_select(NULL) : crypt_select(r->entries, r->hdr.comp_count)) != 0) {
            return false;
        }
        if (!sha256_flash(e->dest, e->installed_size, hash) || memcmp(hash, e->installed_sha256, 32) != 0) {
            return false;
        }
    }
    return true;
}

// The firmware area holds what the record says was installed.
bool record_area_ok(const record_t *r) {
    return area_hashes(r, false);
}

// On a port that keeps the area encrypted: it holds what the record names, but in the
// clear. That is firmware downloaded over USB (the bootloader writes what it is given),
// not a damaged area; the install that follows stores it encrypted.
bool record_area_in_clear(const record_t *r) {
    #ifdef STUB_CRYPT_START
    return area_hashes(r, true);
    #else
    (void) r;
    return false;
    #endif
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
    // Program in whole write units: round the tail up (the pad is erased 0xFF already).
    uint32_t m = (n + STUB_WRITE_UNIT - 1) & ~(uint32_t) (STUB_WRITE_UNIT - 1);
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
    uint32_t n = (end + 4 + STUB_WRITE_UNIT - 1) & ~(uint32_t) (STUB_WRITE_UNIT - 1);
    if (port_flash_write(STUB_RECORD_ADDR, record_block, n) != 0) {
        return -1;
    }
    return flash_equals(STUB_RECORD_ADDR, record_block, n) ? 0 : -1;
}


#ifdef STUB_CRYPT_START
_Static_assert((STUB_CRYPT_START % STUB_FLASH_BLOCK) == 0, "the encrypted area starts on an erase block");

// Is the erase block at addr blank, as stored?
static bool block_blank(uint32_t addr) {
    for (uint32_t off = 0; off < STUB_FLASH_BLOCK; off += IN_CHUNK) {
        if (port_flash_read_stored(addr + off, cmp_chunk, IN_CHUNK) != 0) {
            return false;
        }
        for (uint32_t i = 0; i < IN_CHUNK; i++) {
            if (cmp_chunk[i] != 0xFF) {
                return false;
            }
        }
    }
    return true;
}


// An encrypted area holds nothing but this install. Whatever else is in it -- the tail of
// a longer firmware an earlier install left, or of one downloaded over USB in the clear --
// is erased: every whole block no component touches that is not blank already. The area's
// last block is the record's, erased before the first component went in.
static int scrub_area(const slot_t *s) {
    uint32_t erased = 0;
    for (uint32_t addr = STUB_CRYPT_START; addr + STUB_FLASH_BLOCK <= STUB_CRYPT_END; addr += STUB_FLASH_BLOCK) {
        bool used = false;
        for (uint32_t i = 0; i < s->hdr->comp_count; i++) {
            const component_t *c = &s->comps[i];
            if (c->dest < addr + STUB_FLASH_BLOCK && addr < c->dest + c->installed_size) {
                used = true;
            }
        }
        if (used) {
            continue;
        }
        if (!block_blank(addr)) {
            port_led_blink(50);
            if (port_flash_erase(addr) != 0) {
                return -1;
            }
            erased++;
        }
    }
    log_kv("stale blocks erased", erased);
    if (erased) {
        journal_event(EVENT_AREA_SCRUBBED);
    }
    return 0;
}
#endif

int install_slot(const slot_t *s, uint32_t reason) {
    // Gate the decoder: every blob's compressed hash first, through the memory map.
    for (uint32_t i = 0; i < s->hdr->comp_count; i++) {
        if (!verify_blob(s, &s->comps[i])) {
            log_line("blob hash mismatch");
            journal_cause(s->index, CAUSE_BLOB);
            return -1;
        }
    }
    // The area as this install will leave it: what is already there under the same tweak
    // is kept, anything else is rewritten.
    static record_entry_t entries[TRAILER_MAX_COMPONENTS] STUB_BUFFER_SMALL;
    for (uint32_t i = 0; i < s->hdr->comp_count; i++) {
        entries[i].dest = s->comps[i].dest;
        entries[i].installed_size = s->comps[i].installed_size;
        memcpy(entries[i].installed_sha256, s->comps[i].installed_sha256, 32);
    }
    if (crypt_select(entries, s->hdr->comp_count) != 0) {
        log_line("flash cipher setup failed");
        return -1;
    }
    if (port_flash_prog_begin() != 0) {
        return -1;
    }
    int ret = -1;
    // No record until the install is complete.
    if (port_flash_erase(STUB_RECORD_ADDR & ~(STUB_FLASH_BLOCK - 1)) != 0) {
        goto done;
    }
    journal_erased();
    for (uint32_t i = 0; i < s->hdr->comp_count; i++) {
        log_kv("installing component", i);
        if (install_component(s, &s->comps[i]) != 0) {
            log_line("component install failed");
            goto done;
        }
    }
    #ifdef STUB_CRYPT_START
    if (scrub_area(s) != 0) {
        log_line("area scrub failed");
        journal_event(EVENT_SCRUB_FAILED);
        goto done;
    }
    #endif
    ret = write_record(s, reason);
    if (ret == 0) {
        log_line("record written");
    } else {
        log_line("record write failed");
    }
done:
    if (port_flash_prog_end() != 0) {
        log_line("memory map restore failed");
        journal_event(EVENT_MAP_RESTORE_FAILED);
        return -1;
    }
    return ret;
}

#ifdef STUB_SLOT_CRYPT
// Erase a slot that verified, control block first so it stops verifying at once, then
// every block of its body that is not blank. Programming mode must be on.
static int erase_slot_blocks(const slot_t *s) {
    uint32_t last = s->start + s->size - STUB_FLASH_BLOCK;
    if (port_flash_erase(last) != 0) {
        return -1;
    }
    for (uint32_t addr = s->start; addr < s->start + s->crypt_size && addr < last; addr += STUB_FLASH_BLOCK) {
        if (!block_blank(addr)) {
            port_led_blink(50);
            if (port_flash_erase(addr) != 0) {
                return -1;
            }
        }
    }
    return 0;
}

int erase_slot(slot_t *s) {
    if (port_flash_prog_begin() != 0) {
        return -1;
    }
    int ret = erase_slot_blocks(s);
    if (port_flash_prog_end() != 0) {
        ret = -1;
    }
    s->ok = false;
    s->reject = "erased";
    return ret;
}

// A slot stored in the clear becomes the other slot, ciphered: the body block by block
// through the other slot's stream, then the control block (status sector and trailer,
// verbatim, in the clear), then the copy is verified through the engine, and only then is
// the clear slot erased. The other slot's control block goes first, so the copy does not
// verify until it is complete; a power cut anywhere leaves a clear slot that verifies and
// a copy that does not, and the next boot does this again, skipping the blocks that match.
int convert_slot(slot_t *src, slot_t *dst) {
    uint8_t tweak[10];
    slot_tweak_derive(dst->index, src->trailer_sha256, tweak);
    if (port_crypt_slot_select(tweak, dst->start, dst->start + src->crypt_size) != 0) {
        return -1;
    }
    if (port_flash_prog_begin() != 0) {
        return -1;
    }
    int ret = -1;
    uint32_t last = dst->start + dst->size - STUB_FLASH_BLOCK;
    if (port_flash_erase(last) != 0) {
        goto done;
    }
    for (uint32_t off = 0; off < src->crypt_size; off += STUB_FLASH_BLOCK) {
        uint32_t n = src->crypt_size - off;
        if (n > STUB_FLASH_BLOCK) {
            n = STUB_FLASH_BLOCK;
        }
        if (dst->start + off == last) {
            break;                              // the control block carries the tail
        }
        if (port_flash_read(src->start + off, out_block, n) != 0 || flush_block(dst->start + off, out_block, n) != 0) {
            goto done;
        }
    }
    // Nothing else may be left in the copy: the pad between the body and the control
    // block, from whatever the slot held before -- every whole block after the last one the
    // copy wrote (the body's last block carries pad too, blank from its erase).
    uint32_t pad = (dst->start + src->crypt_size + STUB_FLASH_BLOCK - 1) & ~(uint32_t) (STUB_FLASH_BLOCK - 1);
    for (uint32_t addr = pad; addr < last; addr += STUB_FLASH_BLOCK) {
        if (!block_blank(addr)) {
            port_led_blink(50);
            if (port_flash_erase(addr) != 0) {
                goto done;
            }
        }
    }
    if (port_flash_read(src->start + src->size - STUB_FLASH_BLOCK, out_block, STUB_FLASH_BLOCK) != 0 ||
        flush_block(last, out_block, STUB_FLASH_BLOCK) != 0) {
        goto done;
    }
    ret = 0;
done:
    if (port_flash_prog_end() != 0 || ret != 0) {
        return -1;
    }
    log_line("slot converted");
    journal_event(EVENT_SLOT_CONVERTED);
    return 0;
}
#endif

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
