"""The RT1062's fuse key: what the stub burns next, and which random words may become a key.

``stub/ports/mimxrt/fuse_key_plan.c`` is the part of the fuse work with no chip in it,
compiled for the host. A fuse is forever, so every state of the two lock bits and every way
a random number generator fails quietly is held here. Skipped without gcc.
"""

import shutil
import subprocess

import pytest

from openmv_ota.build import stub as stub_mod

pytestmark = pytest.mark.skipif(not shutil.which("gcc"), reason="needs gcc to compile the stub's fuse plan")

_PORT = stub_mod.STUB_DIR / "ports" / "mimxrt"

_HARNESS_C = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include "fuse_key.h"

// rows: "N <lock word>"   what to burn next
//       "P <w0> <w1> <w2> <w3>"   whether the words may become a key
int main(int argc, char **argv) {
    FILE *f = fopen(argv[1], "r");
    char op;
    while (fscanf(f, " %c", &op) == 1) {
        if (op == 'N') {
            unsigned long lock;
            if (fscanf(f, "%lx", &lock) != 1) return 1;
            printf("%d\n", fuse_key_next((uint32_t) lock));
        } else {
            unsigned long w[4];
            if (fscanf(f, "%lx %lx %lx %lx", &w[0], &w[1], &w[2], &w[3]) != 4) return 1;
            uint32_t words[4] = { (uint32_t) w[0], (uint32_t) w[1], (uint32_t) w[2], (uint32_t) w[3] };
            printf("%d\n", fuse_key_plausible(words));
        }
    }
    return 0;
}
"""

DONE, WORDS, READ_LOCK = 0, 1, 2
WLOCK, RLOCK = 1 << 21, 1 << 23


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    d = tmp_path_factory.mktemp("fuse_key")
    (d / "harness.c").write_text(_HARNESS_C)
    exe = d / "harness"
    subprocess.run(["gcc", "-O1", "-Wall", "-Werror", "-I", str(_PORT), str(d / "harness.c"),
                    str(_PORT / "fuse_key_plan.c"), "-o", str(exe)], check=True)

    def go(rows):
        (d / "rows").write_text("\n".join(rows) + "\n")
        out = subprocess.run([str(exe), str(d / "rows")], check=True, capture_output=True, text=True)
        return [int(x) for x in out.stdout.split()]
    return go


def test_the_lock_bits_are_the_ones_the_fleet_board_was_burnt_with(run):
    """The LOCK word of the fleet's board before, between and after (bench, 2026-11-16)."""
    assert run(["N 40128003", "N 40328003", "N 40b28003"]) == [WORDS, READ_LOCK, DONE]


def test_the_words_come_first_and_the_read_lock_last(run):
    other = 0xFFFFFFFF & ~(WLOCK | RLOCK)                    # every other lock bit says nothing
    assert run(["N %x" % 0, "N %x" % other]) == [WORDS, WORDS]
    # a read lock with no write lock: the words can still be burnt, so they are not final
    assert run(["N %x" % RLOCK, "N %x" % (other | RLOCK)]) == [WORDS, WORDS]
    assert run(["N %x" % WLOCK, "N %x" % (other | WLOCK)]) == [READ_LOCK, READ_LOCK]
    assert run(["N %x" % (WLOCK | RLOCK), "N ffffffff"]) == [DONE, DONE]


def test_what_a_dead_random_number_generator_gives_is_never_a_key(run):
    rows = [
        "P 0 0 0 0",                                         # nothing
        "P ffffffff ffffffff ffffffff ffffffff",             # everything
        "P 9e3779b9 9e3779b9 9e3779b9 9e3779b9",             # one word, four times
        "P 9e3779b9 243f6a88 9e3779b9 b7e15162",             # a word repeated
        "P 9e3779b9 0 243f6a88 b7e15162",                    # a word that never came
        "P 9e3779b9 243f6a88 ffffffff b7e15162",
        "P 1 2 4 8",                                         # too few ones (4 of 128)
        "P fffffffe fffffffd fffffffb fffffff7",             # too many (124 of 128)
        "P 00010001 00020002 00040004 000000ff",             # 14 ones
    ]
    assert run(rows) == [0] * len(rows)


def test_random_words_are_a_key(run):
    import os
    import struct
    rows, want = [], []
    for _ in range(200):
        w = struct.unpack("<4I", os.urandom(16))
        ones = sum(bin(x).count("1") for x in w)
        rows.append("P %x %x %x %x" % w)
        want.append(int(32 <= ones <= 96 and len(set(w)) == 4 and 0 not in w and 0xFFFFFFFF not in w))
    assert run(rows) == want and sum(want) >= 199
    # the edges of the count: 32 and 96 ones pass, 31 and 97 do not
    assert run(["P 000000ff 0000ff00 00ff0000 ff000000", "P 0000007f 0000ff00 00ff0000 ff000000",
                "P ffffff00 ffff00ff ff00ffff 00ffffff", "P ffffff80 ffff00ff ff00ffff 00ffffff"]) == [1, 0, 1, 0]
