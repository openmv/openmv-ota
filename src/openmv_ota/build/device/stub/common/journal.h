/*
 * OpenMV boot stub -- the boot journal (openmv_ota.ota.boot_journal).
 *
 * One 16-byte entry per boot that was not an ordinary one, appended to the second half of
 * the install record's block; boot.py reads them and the check-in carries them. A boot that
 * found what the last entry describes and did nothing writes nothing.
 *
 * THE NUMBERS ARE FOR EVER: a cause or an event bit is given once, never moved, never
 * reused; a new one takes the next free number. tests/build/test_boot_journal_c.py holds
 * this file and the Python list together.
 */
#ifndef STUB_JOURNAL_H
#define STUB_JOURNAL_H

#include "stub.h"

#define JOURNAL_OFFSET              (2048)          // in the record's block
#define JOURNAL_ENTRY               (16)
#define JOURNAL_TAG                 (0x01)
#define JOURNAL_SLOT_NONE           (0xFF)
#define JOURNAL_CARRY               (8)             // entries kept across an install's erase
// An entry takes a whole write unit (the H7's internal flash: a 32-byte word, written once).
#define JOURNAL_STRIDE              ((STUB_WRITE_UNIT > JOURNAL_ENTRY) ? STUB_WRITE_UNIT : JOURNAL_ENTRY)
#define JOURNAL_ENTRIES             ((RECORD_BLOCK - JOURNAL_OFFSET) / JOURNAL_STRIDE)

// Why a slot was not booted.
#define CAUSE_OK                    (0)
#define CAUSE_READ                  (1)
#define CAUSE_MAGIC                 (2)
#define CAUSE_CRC                   (3)
#define CAUSE_FRAMING               (4)
#define CAUSE_VERSION               (5)
#define CAUSE_SIZE                  (6)
#define CAUSE_BOARD                 (7)
#define CAUSE_COMPONENTS            (8)
#define CAUSE_COMP_BOUNDS           (9)
#define CAUSE_COMP_WINDOW           (10)
#define CAUSE_COMP_OVERLAP          (11)
#define CAUSE_CERT                  (12)
#define CAUSE_CERT_ALG              (13)
#define CAUSE_CERT_SIG              (14)
#define CAUSE_ROOT                  (15)
#define CAUSE_SIG                   (16)
#define CAUSE_EPOCH                 (17)
#define CAUSE_MIN_STUB              (18)
#define CAUSE_ROLLBACK              (19)
#define CAUSE_STATUS                (20)
#define CAUSE_TRIAL_FAILED          (21)
#define CAUSE_BODY                  (22)
#define CAUSE_CONVERT               (23)
#define CAUSE_ERASED                (24)
#define CAUSE_ATTEMPT_MARK          (25)
#define CAUSE_BLOB                  (26)
#define CAUSE_INSTALL               (27)
#define CAUSE_INSTALL_VERIFY        (28)
#define CAUSE_COUNT                 (29)

// Things that happened and are not one slot's fate: bit numbers.
#define EVENT_AREA_ENCRYPTED        (0)
#define EVENT_SLOT_CONVERTED        (1)
#define EVENT_CLEAR_SLOT_ERASED     (2)
#define EVENT_AREA_SCRUBBED         (3)
#define EVENT_FLASH_RESET_RETRY     (4)
#define EVENT_STRAY_FAULT           (5)     // not set yet: the fault shows at the jump, after the entry
#define EVENT_SCRUB_FAILED          (6)
#define EVENT_MAP_RESTORE_FAILED    (7)
#define EVENT_JOURNAL_RESTARTED     (8)
#define EVENT_JOURNAL_FULL          (9)
#define EVENT_UNSIGNED_REFUSED      (10)    // secure boot: firmware no verified slot vouches for was not booted

void journal_load(const uint8_t *block);            // the record's block, as first read
void journal_erased(void);                          // an install erased the block
void journal_event(uint32_t bit);
void journal_cause(uint32_t slot, uint8_t cause);   // the first cause given for a slot stays
void journal_commit(const slot_t *slots, uint8_t booted, uint8_t reason);

#endif // STUB_JOURNAL_H
