/*
 * OpenMV boot stub -- slot verification.
 *
 * Order (the same as boot.py's evaluate_slot): framing + CRC, certificate against the
 * embedded root, certificate epoch against the floor, image signature against the
 * certificate's key, product id, min_stub, component bounds, then the status marks
 * (rollback floor for an unproven slot, the attempt budget for a trial). Nothing in
 * the trailer is believed before the signature checks out; the status sector is
 * writable flash and only ever chooses AMONG slots that verify.
 */
#include "stub.h"
#include "port.h"
#include "sha256.h"
#include "p256-m.h"

// sha256(b"openmv-ota.status." + label)[:16] -- openmv_ota.ota.status
const uint8_t STATUS_PENDING[MARKER_SIZE] = {
    0x67, 0xdd, 0x72, 0xb0, 0x82, 0x13, 0x71, 0x71, 0x9a, 0x89, 0x56, 0x21, 0x12, 0x6b, 0xeb, 0x9f
};
const uint8_t STATUS_CONFIRMED[MARKER_SIZE] = {
    0xba, 0x04, 0xb7, 0x71, 0x44, 0x7a, 0x13, 0xa3, 0xe7, 0xe8, 0x28, 0xb2, 0xbb, 0x68, 0x62, 0xf2
};
const uint8_t STATUS_ATTEMPT[MARKER_SIZE] = {
    0x79, 0x77, 0xcd, 0xae, 0xc7, 0xe1, 0x68, 0xbe, 0x26, 0xef, 0x8c, 0x6e, 0xfe, 0xae, 0xde, 0x3d
};

#define HASH_CHUNK  (4096)
static uint8_t hash_chunk[HASH_CHUNK] __attribute__((section(".buffers"), aligned(32)));

void sha256_of(const uint8_t *data, size_t len, uint8_t out[32]) {
    CRYAL_SHA256_CTX ctx;
    sha256_init(&ctx);
    sha256_update(&ctx, data, len);
    sha256_final(&ctx, out);
}

bool sha256_flash(uint32_t addr, uint32_t len, uint8_t out[32]) {
    CRYAL_SHA256_CTX ctx;
    sha256_init(&ctx);
    while (len) {
        uint32_t n = (len < HASH_CHUNK) ? len : HASH_CHUNK;
        if (port_flash_read(addr, hash_chunk, n) != 0) {
            return false;
        }
        sha256_update(&ctx, hash_chunk, n);
        addr += n;
        len -= n;
    }
    sha256_final(&ctx, out);
    return true;
}

uint32_t crc32_of(const uint8_t *data, size_t len) {
    uint32_t crc = 0xFFFFFFFFU;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int b = 0; b < 8; b++) {
            crc = (crc >> 1) ^ (0xEDB88320U & (0U - (crc & 1U)));
        }
    }
    return ~crc;
}

static uint32_t rd32(const uint8_t *p) {
    return (uint32_t) p[0] | ((uint32_t) p[1] << 8) | ((uint32_t) p[2] << 16) | ((uint32_t) p[3] << 24);
}

static uint64_t rd64(const uint8_t *p) {
    return (uint64_t) rd32(p) | ((uint64_t) rd32(p + 4) << 32);
}

// The highest valid `u64 key || u64 ~key` entry in [start, end) of a status sector.
static uint64_t floor_of(const uint8_t *status, uint32_t start, uint32_t end) {
    uint64_t floor = 0;
    for (uint32_t off = start; off + 16 <= end; off += STATUS_ATTEMPT_UNIT) {
        uint64_t key = rd64(status + off);
        uint64_t check = rd64(status + off + 8);
        if ((key ^ 0xFFFFFFFFFFFFFFFFULL) == check && key > floor) {
            floor = key;
        }
    }
    return floor;
}

void slot_read_floors(slot_t *s) {
    s->rollback_floor = floor_of(s->status, STATUS_FLOOR_OFF, STATUS_EPOCH_OFF);
    s->epoch_floor = floor_of(s->status, STATUS_EPOCH_OFF, STATUS_EPOCH_END);
}

static bool is_blank(const uint8_t *p, uint32_t n) {
    for (uint32_t i = 0; i < n; i++) {
        if (p[i] != 0xFF) {
            return false;
        }
    }
    return true;
}

static void read_marks(slot_t *s) {
    s->pending = (memcmp(s->status + STATUS_PENDING_OFF, STATUS_PENDING, MARKER_SIZE) == 0);
    s->confirmed = (memcmp(s->status + STATUS_CONFIRMED_OFF, STATUS_CONFIRMED, MARKER_SIZE) == 0);
    uint32_t v = rd32(s->status + STATUS_COUNTER_OFF);
    uint32_t c = rd32(s->status + STATUS_COUNTER_OFF + 4);
    s->counter_ok = ((v ^ 0xFFFFFFFFU) == c);
    s->counter = s->counter_ok ? v : 0;
    s->attempts_used = 0;
    for (uint32_t i = 0; i < STATUS_ATTEMPTS_MAX; i++) {
        const uint8_t *unit = s->status + STATUS_ATTEMPTS_OFF + i * STATUS_ATTEMPT_UNIT;
        if (is_blank(unit, STATUS_ATTEMPT_UNIT)) {
            break;
        }
        s->attempts_used++;
    }
}

static bool ecdsa_ok(const uint8_t sig[64], const uint8_t pub[64], const uint8_t *msg, size_t len) {
    uint8_t hash[32];
    sha256_of(msg, len, hash);
    return p256_ecdsa_verify(sig, pub, hash, sizeof(hash)) == P256_SUCCESS;
}

static void reject(slot_t *s, const char *why) {
    s->ok = false;
    s->reject = why;
}

void verify_slot(slot_t *s, uint64_t rollback_floor, uint64_t epoch_floor) {
    const uint8_t *t = s->trailer;
    s->ok = false;
    s->reject = NULL;
    s->trial = false;
    s->hdr = NULL;
    s->comps = NULL;
    s->cert = NULL;
    read_marks(s);

    // Framing. Everything is bounds-checked against the 4 KiB block before use.
    const trailer_header_t *h = (const trailer_header_t *) t;
    if (h->magic != TRAILER_MAGIC) {
        return reject(s, "magic");
    }
    if (h->header_version != TRAILER_VERSION) {
        return reject(s, "version");
    }
    if (h->comp_count > TRAILER_MAX_COMPONENTS) {
        return reject(s, "components");
    }
    uint32_t cert_off = sizeof(trailer_header_t) + h->comp_count * sizeof(component_t);
    uint32_t meta_end = cert_off + sizeof(certificate_t) + h->meta_size;
    uint32_t sig_end = meta_end + SIG_SIZE;
    if (h->meta_size > TRAILER_BLOCK || meta_end > TRAILER_BLOCK || sig_end + 4 > TRAILER_BLOCK) {
        return reject(s, "framing");
    }
    if (crc32_of(t, sig_end) != rd32(t + sig_end)) {
        return reject(s, "crc");
    }

    // Certificate: issued by one of the embedded roots, for a key we can use.
    const certificate_t *cert = (const certificate_t *) (t + cert_off);
    if (cert->magic != CERT_MAGIC || cert->version != CERT_VERSION) {
        return reject(s, "cert");
    }
    if (cert->root_id >= STUB_ROOT_KEY_COUNT) {
        return reject(s, "root");
    }
    if (cert->alg != CERT_ALG_ES256 || cert->pubkey[0] != 0x04) {
        return reject(s, "cert-alg");
    }
    if (!ecdsa_ok(cert->sig, STUB_ROOT_KEYS[cert->root_id], (const uint8_t *) cert, CERT_SIGNED_SIZE)) {
        return reject(s, "cert-sig");
    }
    if (cert->epoch < epoch_floor) {
        return reject(s, "epoch");
    }

    // The image signature, with the certified key.
    if (!ecdsa_ok(t + meta_end, cert->pubkey + 1, t, meta_end)) {
        return reject(s, "sig");
    }

    // Signed fields.
    if (STUB_PRODUCT_ID != 0 && h->product_id != STUB_PRODUCT_ID) {
        return reject(s, "board");
    }
    if (h->min_stub > STUB_VERSION) {
        return reject(s, "min-stub");
    }
    uint32_t body_end = slot_status_offset(s);
    if (h->body_size > body_end) {
        return reject(s, "size");
    }
    const component_t *comps = (const component_t *) (t + sizeof(trailer_header_t));
    for (uint32_t i = 0; i < h->comp_count; i++) {
        const component_t *c = &comps[i];
        if (c->size == 0 || c->offset > body_end || c->size > body_end - c->offset) {
            return reject(s, "comp-bounds");
        }
        if (c->installed_size == 0 || (c->dest % STUB_FLASH_BLOCK) != 0 ||
            c->dest < STUB_INSTALL_START || c->dest > STUB_INSTALL_END ||
            c->installed_size > STUB_INSTALL_END - c->dest) {
            return reject(s, "comp-window");
        }
        for (uint32_t j = 0; j < i; j++) {
            if (c->dest < comps[j].dest + comps[j].installed_size &&
                comps[j].dest < c->dest + c->installed_size) {
                return reject(s, "comp-overlap");
            }
        }
    }

    s->hdr = h;
    s->comps = comps;
    s->cert = cert;

    // Status marks. A confirmed slot ran and was kept: bootable, floors do not apply
    // (the floor rises on confirm, so the fallback is below it by construction).
    if (s->confirmed) {
        s->ok = true;
        return;
    }
    uint64_t key = (STUB_PRODUCT_ID != 0) ? h->payload_version : h->publish_seq;
    if (key < rollback_floor) {
        return reject(s, "rollback");
    }
    if (s->pending) {
        if (s->attempts_used >= STUB_MAX_ATTEMPTS) {
            return reject(s, "trial-failed");
        }
        s->ok = true;
        s->trial = true;
        return;
    }
    return reject(s, "status");
}

bool verify_blob(const slot_t *s, const component_t *c) {
    uint8_t hash[32];
    if (!sha256_flash(s->start + c->offset, c->size, hash)) {
        return false;
    }
    return memcmp(hash, c->sha256, 32) == 0;
}

// p256-m's signing/keygen paths want entropy; the stub only verifies and never
// links them, but the reference must resolve.
int p256_generate_random(uint8_t *output, unsigned output_size) {
    (void) output;
    (void) output_size;
    return -1;
}
