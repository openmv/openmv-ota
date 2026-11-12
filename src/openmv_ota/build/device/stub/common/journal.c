/*
 * OpenMV boot stub -- the boot journal: what the stub decided, kept for the server.
 *
 * The journal is read once, with the install record, before anything can erase it. Through
 * the boot the stub notes events and causes in RAM; journal_commit(), at the jump, writes
 * one entry if the boot was worth one: it installed, something happened, or what it found
 * is not what the last entry says. An install erases the record's block and the journal
 * with it, so the commit after one writes the last JOURNAL_CARRY entries back first. A
 * journal that cannot be written never stops a boot.
 */
#include "journal.h"
#include "port.h"

// The names verify.c and main.c reject a slot by, by number.
static const char *const cause_names[CAUSE_COUNT] = {
    [CAUSE_OK] = "ok",
    [CAUSE_READ] = "read",
    [CAUSE_MAGIC] = "magic",
    [CAUSE_CRC] = "crc",
    [CAUSE_FRAMING] = "framing",
    [CAUSE_VERSION] = "version",
    [CAUSE_SIZE] = "size",
    [CAUSE_BOARD] = "board",
    [CAUSE_COMPONENTS] = "components",
    [CAUSE_COMP_BOUNDS] = "comp-bounds",
    [CAUSE_COMP_WINDOW] = "comp-window",
    [CAUSE_COMP_OVERLAP] = "comp-overlap",
    [CAUSE_CERT] = "cert",
    [CAUSE_CERT_ALG] = "cert-alg",
    [CAUSE_CERT_SIG] = "cert-sig",
    [CAUSE_ROOT] = "root",
    [CAUSE_SIG] = "sig",
    [CAUSE_EPOCH] = "epoch",
    [CAUSE_MIN_STUB] = "min-stub",
    [CAUSE_ROLLBACK] = "rollback",
    [CAUSE_STATUS] = "status",
    [CAUSE_TRIAL_FAILED] = "trial-failed",
    [CAUSE_BODY] = "body",
    [CAUSE_CONVERT] = "convert",
    [CAUSE_ERASED] = "erased",
    [CAUSE_ATTEMPT_MARK] = "attempt-mark",
    [CAUSE_BLOB] = "blob",
    [CAUSE_INSTALL] = "install",
    [CAUSE_INSTALL_VERIFY] = "install-verify",
};

static struct {
    bool loaded;
    bool erased;                // the block was erased this boot: nothing is on flash
    bool have_last;             // a valid entry was read
    uint8_t last[4];            // its slot, reason, cause A, cause B
    uint16_t seq;               // the next entry's
    uint32_t next;              // the next free entry position
    uint32_t carried;
    uint8_t carry[JOURNAL_CARRY][JOURNAL_ENTRY];    // the newest entries, oldest first
    uint32_t events;
    uint8_t causes[2];
} j;

static bool blank(const uint8_t *p, uint32_t n) {
    for (uint32_t i = 0; i < n; i++) {
        if (p[i] != 0xFF) {
            return false;
        }
    }
    return true;
}

static uint32_t rd32(const uint8_t *p) {
    return (uint32_t) p[0] | ((uint32_t) p[1] << 8) | ((uint32_t) p[2] << 16) | ((uint32_t) p[3] << 24);
}

static void keep(const uint8_t *e) {
    if (j.carried == JOURNAL_CARRY) {
        memmove(j.carry[0], j.carry[1], (JOURNAL_CARRY - 1) * JOURNAL_ENTRY);
        j.carried--;
    }
    memcpy(j.carry[j.carried++], e, JOURNAL_ENTRY);
    memcpy(j.last, e + 1, sizeof(j.last));
    j.seq = (uint16_t) (((uint16_t) e[6] | ((uint16_t) e[7] << 8)) + 1);
    j.have_last = true;
}

// The walk ends at the first position that is erased. An entry with another tag (a newer
// stub's) or a bad checksum (a torn write) is stepped over, never written over.
void journal_load(const uint8_t *block) {
    if (j.loaded) {
        return;
    }
    j.loaded = true;
    j.next = JOURNAL_ENTRIES;
    for (uint32_t i = 0; i < JOURNAL_ENTRIES; i++) {
        const uint8_t *e = block + JOURNAL_OFFSET + i * JOURNAL_STRIDE;
        if (blank(e, JOURNAL_STRIDE)) {
            j.next = i;
            break;
        }
        if (e[0] == JOURNAL_TAG && crc32_of(e, JOURNAL_ENTRY - 4) == rd32(e + JOURNAL_ENTRY - 4)) {
            keep(e);
        }
    }
}

void journal_erased(void) {
    j.erased = true;
    j.next = 0;
}

void journal_event(uint32_t bit) {
    j.events |= 1U << bit;
}

void journal_cause(uint32_t slot, uint8_t cause) {
    if (slot < 2 && j.causes[slot] == CAUSE_OK) {
        j.causes[slot] = cause;
    }
}

static uint8_t cause_of(const slot_t *s) {
    if (j.causes[s->index] != CAUSE_OK) {
        return j.causes[s->index];
    }
    if (s->ok || s->reject == NULL) {
        return CAUSE_OK;
    }
    for (uint32_t i = 1; i < CAUSE_COUNT; i++) {
        if (strcmp(s->reject, cause_names[i]) == 0) {
            return (uint8_t) i;
        }
    }
    return CAUSE_STATUS;        // a reject with no number of its own: the slot's marks said no
}

void journal_commit(const slot_t *slots, uint8_t booted, uint8_t reason) {
    static uint8_t out[(JOURNAL_CARRY + 1) * JOURNAL_STRIDE] STUB_BUFFER_SMALL;
    uint8_t now[4] = { booted, reason, cause_of(&slots[0]), cause_of(&slots[1]) };
    if (!j.loaded) {
        return;                 // the block was never read: where the journal ends is not known
    }
    if (!j.erased && j.events == 0 && j.have_last && memcmp(now, j.last, sizeof(now)) == 0) {
        return;                 // an ordinary boot
    }
    uint32_t first = j.erased ? 0 : j.next;
    uint32_t count = j.erased ? j.carried : 0;
    if (first + count >= JOURNAL_ENTRIES) {
        return;                 // full: the next install makes room
    }
    if (!j.have_last) {
        j.events |= 1U << EVENT_JOURNAL_RESTARTED;
    }
    if (first + count == JOURNAL_ENTRIES - 1) {
        j.events |= 1U << EVENT_JOURNAL_FULL;
    }
    memset(out, 0xFF, sizeof(out));
    for (uint32_t i = 0; i < count; i++) {
        memcpy(out + i * JOURNAL_STRIDE, j.carry[i], JOURNAL_ENTRY);
    }
    uint8_t *e = out + count * JOURNAL_STRIDE;
    e[0] = JOURNAL_TAG;
    memcpy(e + 1, now, sizeof(now));
    e[5] = 0;
    e[6] = j.seq & 0xFF;
    e[7] = (j.seq >> 8) & 0xFF;
    e[8] = j.events & 0xFF;
    e[9] = (j.events >> 8) & 0xFF;
    e[10] = (j.events >> 16) & 0xFF;
    e[11] = (j.events >> 24) & 0xFF;
    uint32_t crc = crc32_of(e, JOURNAL_ENTRY - 4);
    e[12] = crc & 0xFF;
    e[13] = (crc >> 8) & 0xFF;
    e[14] = (crc >> 16) & 0xFF;
    e[15] = (crc >> 24) & 0xFF;

    log_kv("journal entry", j.seq);
    int ret = -1;
    if (port_flash_prog_begin() == 0) {
        ret = port_flash_write(STUB_RECORD_ADDR + JOURNAL_OFFSET + first * JOURNAL_STRIDE, out,
                               (count + 1) * JOURNAL_STRIDE);
    }
    if (port_flash_prog_end() != 0 || ret != 0) {
        log_line("journal write failed");
    }
    // Whatever reached the flash, this boot has had its say.
    j.erased = false;
    j.next = first + count + 1;
    keep(e);
    j.events = 0;
}
