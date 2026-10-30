/*
 * OpenMV boot stub -- shared definitions.
 *
 * The on-flash formats here mirror the Python codecs in openmv_ota.ota (trailer,
 * certificate, install_record, status) byte for byte. They are the stub's permanent
 * interface: the fleet carries mixed stub versions forever, so a change is a new
 * header/record version, never an edit.
 */
#ifndef STUB_H
#define STUB_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>

#include "stub_config.h"
#include "stub_keys.h"

#define STUB_VERSION                (1)

// Where the firmware's vector table is (the stub jumps there): the start of the install
// window unless the board puts a second text region below the firmware.
#ifndef STUB_FIRMWARE_ADDR
#define STUB_FIRMWARE_ADDR          STUB_INSTALL_START
#endif

// A destination range that touches the stub's own sector is never installed: erasing it
// would take the stub with it. A window that starts above the stub never trips this.
static inline bool stub_sector_overlaps(uint32_t dest, uint32_t size) {
    return dest < STUB_FLASH_ORIGIN + STUB_FLASH_LENGTH && STUB_FLASH_ORIGIN < dest + size;
}

// The smallest unit the install window is programmed in (the N6's octal flash takes 16
// bytes, the H7's internal flash a 32-byte flash word); a block's tail is padded to it.
#ifndef STUB_WRITE_UNIT
#define STUB_WRITE_UNIT             (16)
#endif

// Scratch placement: the big buffers (decoder dictionary, flash block) and the small ones
// (slot images, log ring, read-back chunks), which a board may put in a different SRAM.
#define STUB_BUFFER                 __attribute__((section(".buffers"), aligned(32)))
#define STUB_BUFFER_SMALL           __attribute__((section(".buffers.small"), aligned(32)))

// ---- Trailer v4 (openmv_ota.ota.trailer) ------------------------------------
#define TRAILER_MAGIC               (0x52564D4FU)   // "OMVR"
#define TRAILER_VERSION             (4)
#define TRAILER_MAX_COMPONENTS      (16)
#define TRAILER_BLOCK               (4096)          // the slot's trailer / status sector
#define SIG_SIZE                    (64)            // ES256 r || s

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint32_t header_version;
    uint32_t body_size;
    uint32_t comp_count;
    uint32_t meta_size;
    uint32_t min_stub;
    uint32_t payload_version;
    uint8_t uart_en;
    uint8_t uart_id;
    uint8_t wdt_en;
    uint8_t wdt_id;
    uint32_t baud;
    uint32_t wdt_timeout_ms;
    uint64_t product_id;
    uint64_t publish_seq;
    uint64_t reserved;
    uint8_t body_sha256[32];
} trailer_header_t;

typedef struct __attribute__((packed)) {
    uint32_t dest;              // absolute flash address the decompressed bytes go to
    uint32_t offset;            // blob start, relative to the slot start
    uint32_t size;              // compressed size
    uint32_t installed_size;    // decompressed size
    uint8_t sha256[32];         // of the compressed blob
    uint8_t installed_sha256[32];   // of the decompressed bytes
} component_t;

// ---- Certificate (openmv_ota.ota.certificate) --------------------------------
#define CERT_MAGIC                  (0x43564D4FU)   // "OMVC"
#define CERT_VERSION                (1)
#define CERT_KIND_OTA               (0)
#define CERT_KIND_FACTORY           (1)
#define CERT_ALG_ES256              (-7)
#define CERT_SIGNED_SIZE            (96)

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint32_t version;
    uint32_t kind;
    uint32_t root_id;
    uint32_t epoch;
    int32_t alg;
    uint8_t pubkey[65];         // 0x04 || X || Y
    uint8_t pad[7];
    uint8_t sig[64];            // root's ECDSA over bytes 0..95
} certificate_t;

// ---- Install record (openmv_ota.ota.install_record) --------------------------
#define RECORD_MAGIC                (0x49564D4FU)   // "OMVI"
#define RECORD_VERSION              (1)
#define RECORD_BLOCK                (4096)

#define REASON_NORMAL               (0)
#define REASON_TRIAL                (1)
#define REASON_REVERTED             (2)
#define REASON_REINSTALLED          (3)

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint32_t record_version;
    uint32_t stub_version;
    uint32_t slot;              // 0 = A, 1 = B
    uint32_t comp_count;
    uint32_t reason;
    uint64_t reserved;
} record_header_t;

typedef struct __attribute__((packed)) {
    uint32_t dest;
    uint32_t installed_size;
    uint8_t installed_sha256[32];
} record_entry_t;

_Static_assert(sizeof(trailer_header_t) == 96, "trailer header is 96 bytes");
_Static_assert(sizeof(component_t) == 80, "component entry is 80 bytes");
_Static_assert(sizeof(certificate_t) == 160, "certificate is 160 bytes");
_Static_assert(sizeof(record_header_t) == 32, "record header is 32 bytes");
_Static_assert(sizeof(record_entry_t) == 40, "record entry is 40 bytes");

// ---- Status sector (openmv_ota.ota.status) -----------------------------------
// 16-byte markers at stride-spaced offsets, programmed over erased 0xFF; a marker
// counts only on an exact match, so a torn write reads as "not set".
#ifndef STUB_CONTROL_STRIDE
#define STUB_CONTROL_STRIDE         (16)
#endif
#define MARKER_SIZE                 (16)
#define STATUS_PENDING_OFF          (0)
#define STATUS_CONFIRMED_OFF        (2 * STUB_CONTROL_STRIDE)
#define STATUS_COUNTER_OFF          (4 * STUB_CONTROL_STRIDE)
#define STATUS_ATTEMPTS_OFF         (5 * STUB_CONTROL_STRIDE)
#define STATUS_ATTEMPT_UNIT         ((STUB_CONTROL_STRIDE > 16) ? STUB_CONTROL_STRIDE : 16)
#define STATUS_ATTEMPTS_MAX         (64)
#define STATUS_FLOOR_OFF            (STATUS_ATTEMPTS_OFF + STATUS_ATTEMPTS_MAX * STATUS_ATTEMPT_UNIT)
#define STATUS_FLOOR_ENTRIES_FIT    ((TRAILER_BLOCK - STATUS_FLOOR_OFF) / (2 * STATUS_ATTEMPT_UNIT))
#define STATUS_FLOOR_ENTRIES        ((STATUS_FLOOR_ENTRIES_FIT < 64) ? STATUS_FLOOR_ENTRIES_FIT : 64)
#define STATUS_EPOCH_OFF            (STATUS_FLOOR_OFF + STATUS_FLOOR_ENTRIES * STATUS_ATTEMPT_UNIT)
#define STATUS_EPOCH_END            (STATUS_EPOCH_OFF + STATUS_FLOOR_ENTRIES * STATUS_ATTEMPT_UNIT)

extern const uint8_t STATUS_PENDING[MARKER_SIZE];
extern const uint8_t STATUS_CONFIRMED[MARKER_SIZE];
extern const uint8_t STATUS_ATTEMPT[MARKER_SIZE];

// ---- A slot, as the stub sees it ---------------------------------------------
typedef struct {
    uint32_t index;             // 0 = A, 1 = B
    uint32_t start;             // absolute address of the slot
    uint32_t size;              // slot size; status at size - 2*TRAILER_BLOCK, trailer after it
    uint8_t status[TRAILER_BLOCK];
    uint8_t trailer[TRAILER_BLOCK];
    // Filled by verify_slot():
    bool ok;
    const char *reject;         // why not, when !ok
    const trailer_header_t *hdr;
    const component_t *comps;
    const certificate_t *cert;
    bool pending;
    bool confirmed;
    bool trial;                 // ok && pending && !confirmed: boot it, burning an attempt
    bool counter_ok;
    uint32_t counter;
    uint32_t attempts_used;
    uint64_t rollback_floor;    // the highest entries this slot's sector records
    uint64_t epoch_floor;
} slot_t;

static inline uint32_t slot_status_offset(const slot_t *s) {
    return s->size - 2 * TRAILER_BLOCK;
}

// verify.c
void slot_read_floors(slot_t *s);
void verify_slot(slot_t *s, uint64_t rollback_floor, uint64_t epoch_floor);
bool verify_blob(const slot_t *s, const component_t *c);
void sha256_of(const uint8_t *data, size_t len, uint8_t out[32]);
bool sha256_flash(uint32_t addr, uint32_t len, uint8_t out[32]);
uint32_t crc32_of(const uint8_t *data, size_t len);

// install.c
typedef struct {
    bool valid;
    record_header_t hdr;
    record_entry_t entries[TRAILER_MAX_COMPONENTS];
} record_t;

void record_read(record_t *r);
bool record_matches(const record_t *r, const slot_t *s);
bool record_area_ok(const record_t *r);
int install_slot(const slot_t *s, uint32_t reason);
int burn_attempt(slot_t *s);

// log.c
void log_open(uint32_t uart_id, uint32_t baud);
void log_str(const char *s);
void log_hex(uint32_t v);
void log_dec(uint32_t v);
void log_line(const char *s);
void log_kv(const char *k, uint32_t v);

#endif // STUB_H
