"""Tiny fragmented-MP4 writer for tests: one track per init/segment, enough
boxes (mvhd/tkhd/mdhd/hdlr/stsd/stts/stsc/stsz/stco/mvex) that ffmpeg's mov
demuxer accepts it too. Payloads are synthetic."""
import struct


def box(t, *parts):
    body = b"".join(parts)
    return struct.pack(">I", 8 + len(body)) + t + body


def full(t, ver, flags, *parts):
    return box(t, bytes([ver]) + flags.to_bytes(3, "big"), *parts)


VPS = bytes([0x40, 0x01, 0x0c, 0x01, 0xff, 0xff])
SPS = bytes([0x42, 0x01, 0x01, 0x01, 0x60])
PPS = bytes([0x44, 0x01, 0xc1, 0x72])


def hvcc(params=(VPS, SPS, PPS), nal_len=4):
    out = bytearray(23)
    out[0] = 1
    out[21] = 0xFC | (nal_len - 1)
    out[22] = len(params)
    for p in params:
        typ = (p[0] >> 1) & 0x3F
        out += bytes([0x80 | typ]) + struct.pack(">HH", 1, len(p)) + p
    return box(b"hvcC", bytes(out))


def init(kind="video", codec=b"hvc1", tid=1, timescale=90000, extra=None,
         lang=b"eng"):
    if kind == "video":
        entry = box(codec, b"\x00" * 6 + b"\x00\x01" + b"\x00" * 16 +
                    struct.pack(">HH", 1920, 1080) +
                    struct.pack(">II", 0x00480000, 0x00480000) + b"\x00" * 4 +
                    b"\x00\x01" + b"\x00" * 32 + b"\x00\x18\xff\xff",
                    extra if extra is not None else hvcc())
        hdlr, mh = b"vide", full(b"vmhd", 0, 1, b"\x00" * 8)
    else:
        entry = box(codec, b"\x00" * 6 + b"\x00\x01" + b"\x00" * 8 +
                    struct.pack(">HH", 2, 16) + b"\x00" * 4 +
                    struct.pack(">I", 48000 << 16),
                    extra if extra is not None else box(b"dac4", b"\x20\xa6\x01\x40\x00"))
        hdlr, mh = b"soun", full(b"smhd", 0, 0, b"\x00" * 4)
    stbl = box(b"stbl",
               full(b"stsd", 0, 0, struct.pack(">I", 1), entry),
               full(b"stts", 0, 0, b"\x00" * 4), full(b"stsc", 0, 0, b"\x00" * 4),
               full(b"stsz", 0, 0, b"\x00" * 8), full(b"stco", 0, 0, b"\x00" * 4))
    dinf = box(b"dinf", full(b"dref", 0, 0, struct.pack(">I", 1),
                             full(b"url ", 0, 1)))
    mdia = box(b"mdia",
               full(b"mdhd", 0, 0, b"\x00" * 8, struct.pack(">II", timescale, 0),
                    b"\x15\xc7\x00\x00"),
               full(b"hdlr", 0, 0, b"\x00" * 4, hdlr, b"\x00" * 12, b"h\x00"),
               box(b"minf", mh, dinf, stbl))
    tkhd = full(b"tkhd", 0, 7, b"\x00" * 8, struct.pack(">I", tid), b"\x00" * 4,
                b"\x00" * 4, b"\x00" * 8, b"\x00" * 4,
                b"\x01\x00\x00\x00" if kind == "audio" else b"\x00" * 4,
                struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000),
                struct.pack(">II", 1920 << 16 if kind == "video" else 0,
                            1080 << 16 if kind == "video" else 0))
    mvhd = full(b"mvhd", 0, 0, b"\x00" * 8, struct.pack(">II", 1000, 0),
                struct.pack(">IH", 0x10000, 0x100), b"\x00" * 10,
                struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000),
                b"\x00" * 24, struct.pack(">I", tid + 1))
    mvex = box(b"mvex", full(b"trex", 0, 0, struct.pack(">5I", tid, 1, 0, 0, 0)))
    return (box(b"ftyp", b"iso6", b"\x00\x00\x00\x00", b"iso6cmfc")
            + box(b"moov", mvhd, box(b"trak", tkhd, mdia), mvex))


def fragment(samples, tid=1, seq=1, base_dts=0):
    """samples: [(data, duration, sync, cto)] -> moof+mdat bytes."""
    def trun(off):
        rows = b"".join(struct.pack(">IIIi", d, len(x),
                                    0x02000000 if s else 0x01010000, c)
                        for x, d, s, c in samples)
        return full(b"trun", 1, 0x01 | 0x100 | 0x200 | 0x400 | 0x800,
                    struct.pack(">Ii", len(samples), off), rows)
    tfhd = full(b"tfhd", 0, 0x020000, struct.pack(">I", tid))
    tfdt = full(b"tfdt", 1, 0, struct.pack(">Q", base_dts))
    mfhd = full(b"mfhd", 0, 0, struct.pack(">I", seq))
    moof = box(b"moof", mfhd, box(b"traf", tfhd, tfdt, trun(0)))
    off = len(moof) + 8
    moof = box(b"moof", mfhd, box(b"traf", tfhd, tfdt, trun(off)))
    return moof + box(b"mdat", b"".join(x for x, _d, _s, _c in samples))


def hevc_sample(nal_type=19, size=300, fill=0x55):
    nal = bytes([nal_type << 1, 0x01]) + bytes([fill]) * size
    return struct.pack(">I", len(nal)) + nal
