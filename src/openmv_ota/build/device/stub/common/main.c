/*
 * OpenMV boot stub.
 *
 * Verify both ROMFS slots, pick one the way boot.py would (newest install counter
 * first; a pending slot with attempts left is a trial and costs an attempt, a
 * confirmed slot is known good), install its components into the firmware area if
 * the install record does not already say they are there, write the record, arm
 * the watchdog the trailer asks for, and jump. boot.py then reads the record to
 * learn which slot runs and why. The stub decides nothing the slots' marks do not
 * say; it only refuses what does not verify.
 */
#include "stub.h"
#include "port.h"
#include "journal.h"

#define LED_VERIFY_MS   (50)
#define LED_TRIAL_MS    (250)
#define LED_STOP_MS     (25)

static slot_t slots[2] STUB_BUFFER_SMALL;
static record_t record;

static bool read_slot(slot_t *s, uint32_t index) {
    memset(s, 0, sizeof(*s));
    s->index = index;
    s->size = STUB_ROMFS_LENGTH / 2;
    s->start = STUB_ROMFS_ORIGIN + index * s->size;
    uint32_t status_at = s->start + slot_status_offset(s);
    return port_flash_read(status_at, s->status, TRAILER_BLOCK) == 0 &&
           port_flash_read(status_at + TRAILER_BLOCK, s->trailer, TRAILER_BLOCK) == 0;
}

// Newest install first: a higher counter installed later. An unreadable counter is
// the oldest there is.
static int newer(const slot_t *a, const slot_t *b) {
    if (a->counter_ok != b->counter_ok) {
        return a->counter_ok ? 1 : -1;
    }
    return (a->counter > b->counter) ? 1 : (a->counter < b->counter) ? -1 : 0;
}

// Whole literals per line (no runtime words): the HIL keys its markers on them.
static void jump(uint32_t reason) {
    switch (reason) {
        case REASON_TRIAL: log_line("boot trial"); break;
        case REASON_REVERTED: log_line("boot reverted"); break;
        case REASON_REINSTALLED: log_line("boot reinstalled"); break;
        case REASON_REPAIRED: log_line("boot repaired"); break;
        default: log_line("boot normal"); break;
    }
    port_jump(STUB_FIRMWARE_ADDR);
}

// Boot a slot: the journal hears of it first.
static void boot(const slot_t *s, uint32_t reason) {
    journal_commit(slots, (uint8_t) s->index, (uint8_t) reason);
    jump(reason);
}

static void log_slot(const slot_t *s) {
    log_kv("slot", s->index);
    if (s->ok && s->trial) {
        log_line("slot trial");
    } else if (s->ok) {
        log_line("slot ok");
    } else {
        log_line("slot rejected");
        if (s->reject) {
            log_str("stub: reject ");
            log_str(s->reject);
            log_str("\r\n");
        }
    }
}

// Plausible Cortex-M vector table at the firmware address: stack in RAM, entry in
// the install window with the Thumb bit. How the plain bootloader boots today.
static bool vectors_plausible(void) {
    uint8_t v[8];
    // No record says what the area holds, so no cipher: firmware flashed there in the clear.
    if (port_crypt_select(NULL) != 0 || port_flash_read(STUB_FIRMWARE_ADDR, v, sizeof(v)) != 0) {
        return false;
    }
    uint32_t sp = (uint32_t) v[0] | ((uint32_t) v[1] << 8) | ((uint32_t) v[2] << 16) | ((uint32_t) v[3] << 24);
    uint32_t pc = (uint32_t) v[4] | ((uint32_t) v[5] << 8) | ((uint32_t) v[6] << 16) | ((uint32_t) v[7] << 24);
    return (sp & 3) == 0 && sp >= STUB_RAM_START && sp <= STUB_RAM_END &&
           (pc & 1) == 1 && pc >= STUB_FIRMWARE_ADDR && pc < STUB_INSTALL_END;
}

int main(void) {
    port_init();
    #ifdef STUB_DEBUG_UART
    // A bench build: the UART open from the first instruction, trailer or no trailer. The
    // shipped stub only trusts a UART a VERIFIED slot names, so a stub that rejects both
    // slots is silent -- this is how the bench hears why.
    log_open(STUB_DEBUG_UART, 115200);
    #endif
    log_str("\r\n--- stub boot ---\r\n");          // the RAM ring sees this; the UART is not open yet

    for (uint32_t i = 0; i < 2; i++) {
        if (!read_slot(&slots[i], i)) {
            slots[i].reject = "read";
        }
        slot_read_floors(&slots[i]);
    }
    uint64_t rollback_floor = 0, epoch_floor = 0;
    for (uint32_t i = 0; i < 2; i++) {
        if (slots[i].rollback_floor > rollback_floor) {
            rollback_floor = slots[i].rollback_floor;
        }
        if (slots[i].epoch_floor > epoch_floor) {
            epoch_floor = slots[i].epoch_floor;
        }
    }
    for (uint32_t i = 0; i < 2; i++) {
        if (slots[i].reject == NULL) {
            verify_slot(&slots[i], rollback_floor, epoch_floor);
        }
    }

    // Candidates, newest first.
    slot_t *order[2] = { &slots[0], &slots[1] };
    if (newer(order[1], order[0]) > 0) {
        order[0] = &slots[1];
        order[1] = &slots[0];
    }

    // The debug UART, from the first slot that verifies and asks for one.
    for (uint32_t i = 0; i < 2; i++) {
        if (order[i]->ok && order[i]->hdr->uart_en) {
            log_open(order[i]->hdr->uart_id, order[i]->hdr->baud);
            break;
        }
    }
    log_kv("version", STUB_VERSION);
    for (uint32_t i = 0; i < 2; i++) {
        log_slot(&slots[i]);
    }

    // A port that keeps the firmware area encrypted needs the chip's key before it reads
    // or writes a byte there. A chip that has none to give is not one to run on.
    if (port_crypt_init() != 0) {
        log_line("no hardware key");
        while (1) {
            port_led_blink(LED_STOP_MS);
        }
    }

    record_read(&record);
    if (record.valid) {
        log_kv("record slot", record.hdr.slot);
    }

    bool other_failed_trial[2] = { false, false };
    for (uint32_t i = 0; i < 2; i++) {
        const slot_t *o = &slots[1 - i];
        other_failed_trial[i] = (!o->ok && o->reject && strcmp(o->reject, "trial-failed") == 0);
    }
    // A slot that verified and still could not be booted -- its body, its blobs, its install:
    // the slot booted after it is a repair, not a choice.
    bool damaged = false;

    for (uint32_t i = 0; i < 2; i++) {
        slot_t *s = order[i];
        if (!s->ok) {
            continue;
        }
        #ifdef STUB_SLOT_CRYPT
        // The body, under this slot's cipher: how the firmware will read it. A body that is
        // not this slot's ciphertext is in the clear (a factory slot, or an installer that
        // wrote plaintext) or damaged. A clear slot never boots as it is: it is converted
        // into the other slot when no ciphered slot verifies, and erased when one does.
        if (!slot_body_ok(s, false)) {
            slot_t *o = &slots[1 - s->index];
            if (port_crypt_slot_select(NULL, 0, 0) != 0 || !slot_in_clear(s)) {
                log_line("slot body does not verify");
                s->ok = false;
                s->reject = "body";
                damaged = true;
                continue;
            }
            if (o->ok && slot_body_ok(o, false)) {
                log_line("slot in the clear: erased");
                journal_event(EVENT_CLEAR_SLOT_ERASED);
                (void) erase_slot(s);
                continue;
            }
            log_line("slot in the clear: converting");
            if (convert_slot(s, o) != 0) {
                log_line("slot convert failed");
                s->ok = false;
                s->reject = "convert";
                damaged = true;
                continue;
            }
            // The copy, as the next boot would find it.
            if (!read_slot(o, o->index)) {
                o->reject = "read";
            }
            slot_read_floors(o);
            verify_slot(o, rollback_floor, epoch_floor);
            if (!o->ok || !slot_body_ok(o, false)) {
                log_line("converted slot does not verify");
                journal_cause(s->index, CAUSE_CONVERT);
                damaged = true;
                continue;
            }
            (void) erase_slot(s);
            log_line("clear slot erased");
            journal_event(EVENT_CLEAR_SLOT_ERASED);
            s = o;
        }
        {
            // The other slot in the clear -- a conversion a reset cut short of its last step,
            // or a plaintext install -- never boots: erased, so no plaintext stays behind.
            slot_t *o = &slots[1 - s->index];
            if (o->ok && slot_in_clear(o)) {
                log_line("other slot in the clear: erased");
                journal_event(EVENT_CLEAR_SLOT_ERASED);
                (void) erase_slot(o);
            }
        }
        #endif
        uint32_t reason = REASON_NORMAL;
        if (s->trial) {
            // Pay for the trial before it runs: a slot that cannot record an attempt
            // cannot be bounded, so it is not run.
            if (burn_attempt(s) != 0) {
                log_line("attempt mark failed");
                journal_cause(s->index, CAUSE_ATTEMPT_MARK);
                damaged = true;
                continue;
            }
            log_kv("attempt burned, used", s->attempts_used);
            reason = REASON_TRIAL;
        } else if (other_failed_trial[s->index]) {
            reason = REASON_REVERTED;
        } else if (damaged) {
            reason = REASON_REPAIRED;
        }

        bool recorded = record_matches(&record, s);
        log_kv("record matches", recorded);
        if (recorded && record_area_ok(&record)) {
            // Nothing to do: the firmware area holds this slot's components. The record
            // keeps the reason it was INSTALLED for; this boot's reason is only logged.
            log_line("firmware area verified");
            boot(s, reason);
        }
        if (recorded && record_area_in_clear(&record)) {
            log_line("firmware area in the clear: encrypting it");
            journal_event(EVENT_AREA_ENCRYPTED);
        } else if (recorded) {
            log_line("firmware area does not match the record");
            reason = REASON_REINSTALLED;    // the record is right but the bytes are not
        }
        log_kv("installing slot", s->index);
        if (install_slot(s, reason) == 0) {
            // The install read back every block it programmed, in programming mode. What
            // the firmware will run is the area through the memory map (and the cipher,
            // where there is one): hash that against the record before trusting it.
            record_read(&record);
            if (record_matches(&record, s) && record_area_ok(&record)) {
                log_line("installed area verified");
                boot(s, reason);
            }
            log_line("installed area does not verify");
            journal_cause(s->index, CAUSE_INSTALL_VERIFY);
        }
        log_line("install failed");
        journal_cause(s->index, CAUSE_INSTALL);     // unless the install said why itself
        damaged = true;
        // Anything the install touched is unrecorded now; try the next slot.
    }

    // No slot verifies. Boot what is installed if the record vouches for it, or if it
    // at least looks like firmware (a USB-provisioned camera whose slots were wiped).
    (void) port_crypt_slot_select(NULL, 0, 0);     // no slot is mapped under a cipher
    if (record.valid && record_area_ok(&record)) {
        journal_commit(slots, JOURNAL_SLOT_NONE, REASON_NO_SLOT);
        jump(record.hdr.reason);
    }
    if (vectors_plausible()) {
        log_line("no slot: booting the installed firmware");
        journal_commit(slots, JOURNAL_SLOT_NONE, REASON_NO_SLOT);
        port_jump(STUB_FIRMWARE_ADDR);
    }
    log_line("nothing to boot");
    journal_commit(slots, JOURNAL_SLOT_NONE, REASON_NOTHING);
    while (1) {
        port_led_blink(LED_STOP_MS);
    }
}
