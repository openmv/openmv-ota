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

#define LED_VERIFY_MS   (50)
#define LED_TRIAL_MS    (250)
#define LED_STOP_MS     (25)

static slot_t slots[2] __attribute__((section(".buffers"), aligned(32)));
static record_t record;

static const char *reason_name(uint32_t reason) {
    switch (reason) {
        case REASON_TRIAL: return "trial";
        case REASON_REVERTED: return "reverted";
        case REASON_REINSTALLED: return "reinstalled";
        default: return "normal";
    }
}

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

static void jump(uint32_t reason) {
    log_str("stub: boot ");
    log_str(reason_name(reason));
    log_str("\r\n");
    port_jump(STUB_INSTALL_START);
}

// Plausible Cortex-M vector table at the firmware address: stack in RAM, entry in
// the install window with the Thumb bit. How the plain bootloader boots today.
static bool vectors_plausible(void) {
    uint8_t v[8];
    if (port_flash_read(STUB_INSTALL_START, v, sizeof(v)) != 0) {
        return false;
    }
    uint32_t sp = (uint32_t) v[0] | ((uint32_t) v[1] << 8) | ((uint32_t) v[2] << 16) | ((uint32_t) v[3] << 24);
    uint32_t pc = (uint32_t) v[4] | ((uint32_t) v[5] << 8) | ((uint32_t) v[6] << 16) | ((uint32_t) v[7] << 24);
    return (sp & 3) == 0 && sp >= STUB_RAM_START && sp <= STUB_RAM_END &&
           (pc & 1) == 1 && pc >= STUB_INSTALL_START && pc < STUB_INSTALL_END;
}

int main(void) {
    port_init();

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
        const slot_t *s = &slots[i];
        log_str("stub: slot ");
        log_str(s->index ? "B" : "A");
        log_str(s->ok ? (s->trial ? " trial" : " ok") : " rejected ");
        if (!s->ok && s->reject) {
            log_str(s->reject);
        }
        log_str("\r\n");
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

    for (uint32_t i = 0; i < 2; i++) {
        slot_t *s = order[i];
        if (!s->ok) {
            continue;
        }
        uint32_t reason = REASON_NORMAL;
        if (s->trial) {
            // Pay for the trial before it runs: a slot that cannot record an attempt
            // cannot be bounded, so it is not run.
            if (burn_attempt(s) != 0) {
                log_line("attempt mark failed");
                continue;
            }
            reason = REASON_TRIAL;
        } else if (other_failed_trial[s->index]) {
            reason = REASON_REVERTED;
        }

        bool recorded = record_matches(&record, s);
        if (recorded && record_area_ok(&record)) {
            // Nothing to do: the firmware area holds this slot's components. Keep the
            // reason the install was made for unless this boot changed it.
            jump(s->trial ? REASON_TRIAL : (reason == REASON_REVERTED ? reason : record.hdr.reason));
        }
        if (recorded) {
            reason = REASON_REINSTALLED;    // the record is right but the bytes are not
        }
        if (install_slot(s, reason) == 0) {
            jump(reason);
        }
        log_line("install failed");
        // Anything the install touched is unrecorded now; try the next slot.
    }

    // No slot verifies. Boot what is installed if the record vouches for it, or if it
    // at least looks like firmware (a USB-provisioned camera whose slots were wiped).
    if (record.valid && record_area_ok(&record)) {
        jump(record.hdr.reason);
    }
    if (vectors_plausible()) {
        log_line("no slot: booting the installed firmware");
        port_jump(STUB_INSTALL_START);
    }
    log_line("nothing to boot");
    while (1) {
        port_led_blink(LED_STOP_MS);
    }
}
