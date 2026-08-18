# ROMFS image anatomy

A byte-by-byte decode of a real ROMFS image, for anyone verifying an image by hand or
writing a reader. The tool that makes
these is on [the tutorial's ROMFS page](../tutorial/01-romfs.md); everything about the
format itself lives here.

## The format in three ideas

Three ideas, and the whole format falls out of them.

**1 — Variable-length integers.** Every number (a record's kind, a payload's size) is
an ordinary binary number stored in as few bytes as it needs. Split the number into
7-bit chunks, most-significant chunk first, one chunk per byte; each byte's eighth
bit is a flag — **1 = another byte follows, 0 = this is the last one**. A value up to
127 is one byte, and each further 7 bits of magnitude costs one more:

```
300 = 0b0000010_0101100   →   0x82 0x2C
          │        └── low 7 bits + flag 0 (last byte)      0x2C = 0_0101100
          └── upper 7 bits + flag 1 (more follows)          0x82 = 1_0000010
```

**2 — Records.** The image is made of one shape only:

```
┌──────┬─────────────────┬──────┬────────────────────┐
│ kind │ padding (0x80…) │ size │ payload (size B)   │
└──────┴─────────────────┴──────┴────────────────────┘
```

| kind | meaning |
|---|---|
| 0 | unused / name carrier |
| 1 | padding |
| 2 | data, stored verbatim |
| 3 | data, stored as a pointer |
| 4 | directory |
| 5 | file |

**3 — The padding trick.** The `padding` run between `kind` and `size` is the
alignment mechanism, and it is free. `0x80` is a byte whose flag says "more follows"
and whose seven value bits are all **zero** — so the decoder just reads each one as
leading zeros of `size`, no special case at all, while the writer inserts exactly
enough of them that the payload starts on the boundary it needs. Padding is parsing.

**Putting it together.** The whole image is a single header record whose `kind` is
the three magic bytes `D2 CD 31` — which are themselves `'R'|0x80, 'M'|0x80, '1'`, so
the magic is simultaneously a readable signature *and* a valid varint. Its payload,
aligned to 16, is the root directory's entries, concatenated:

```
image
└── header record        kind = D2 CD 31 ("RM1"), payload aligned to 16
    ├── file record      kind 5, aligned to 8
    │   ├── name         length-prefixed, e.g. 07 "main.py"
    │   └── data record  kind 2, payload aligned per the extension rule
    ├── directory record kind 4
    │   ├── name         e.g. 06 "models"
    │   └── …child file/directory records…
    └── …
```

A directory's payload is its name followed by its children — the same record shapes,
nested. A file's data record gets the **extension alignment rule** applied to its
payload, so the file's actual bytes (not its record header) land on the boundary.

## Reading a real image

The image: a three-file app (`main.py`, `settings.py`, `models/detector.tflite`) packed
for the N6 with `openmv-ota romfs pack` — 3167 bytes. Its first 48:

```
00000000  d2 cd 31 80 80 80 80 80 80 80 80 80 80 80 98 4f
00000010  05 80 80 80 80 80 80 2f 07 6d 61 69 6e 2e 70 79
00000020  02 80 80 23 69 6d 70 6f 72 74 20 73 65 6e 73 6f  …
```

```
00000000  d2 cd 31 80 80 80 80 80 80 80 80 80 80 80 98 4f
00000010  05 80 80 80 80 80 80 2f 07 6d 61 69 6e 2e 70 79
00000020  02 80 80 23 69 6d 70 6f 72 74 20 73 65 6e 73 6f  …
```

Decoded, field by field:

| offset | bytes | meaning |
|---|---|---|
| 0 | `d2 cd 31` | the magic — the header record's `kind`, readable as "RM1" |
| 3 | `80` × 11 | padding: eleven "more follows" bytes carrying zeros |
| 14 | `98 4f` | `size` = 3151 — exactly the rest of the file |
| 16 | `05` | `kind` 5: a **file** record (and the root payload starts here, on the 16-byte boundary the padding above bought) |
| 17 | `80` × 6 | padding |
| 23 | `2f` | `size` = 47 |
| 24 | `07` + `main.py` | the file's name, length-prefixed |
| 32 | `02` | `kind` 2: **verbatim data** |
| 33 | `80 80` | padding |
| 35 | `23` | `size` = 35 |
| 36 | `import senso…` | the file's bytes — at offset 36, just as `ls -l` reported |

Every `0x80` above exists only to push the next payload onto its boundary, and the
decoder never treats them specially — it reads each one as leading zeros of the
`size` that follows.

The record shapes nest from here: the rest of the file is the remaining root entries —
`settings.py`, and a directory record wrapping `models/detector.tflite`, whose data
payload the writer padded out to offset 128 to satisfy the N6's 32-byte `tflite`
alignment rule.
