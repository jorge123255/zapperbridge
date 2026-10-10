"""Minimal MPEG-TS PSI reader: PAT/PMT/PCR/PID census over a byte buffer."""
import collections

VIDEO = (0x02, 0x03, 0x06, 0x0F, 0x1B, 0x24)
AUDIO = (0x03, 0x04, 0x06, 0x0F, 0x81, 0x87, 0x83, 0x05)


def sections(buf):
    """Yield (table_id, section_bytes) for pointer-aligned PSI sections."""
    i = 0
    n = len(buf) - 188
    while i <= n:
        if buf[i] != 0x47:
            i += 1
            continue
        p = buf[i:i + 188]
        i += 188
        afc = (p[3] >> 4) & 3
        if not (afc & 1):
            continue
        off = 4
        if afc & 2:
            if p[4] >= 184:
                continue
            off = 5 + p[4]
        if off >= 188:
            continue
        if not (p[1] & 0x40):
            continue
        ptr = p[off]
        body = p[off + 1 + ptr:188]
        if len(body) < 6:
            continue
        tid = body[0]
        tblen = ((body[1] & 0x0f) << 8) | body[2]
        if tblen < 3 or 3 + tblen > len(body):
            continue
        yield tid, body[:3 + tblen]


def analyze(buf):
    pids = collections.Counter()
    pcr = set()
    pat = {}
    pmts = []
    i = 0
    n = len(buf) - 188
    while i <= n:
        if buf[i] != 0x47:
            i += 1
            continue
        p = buf[i:i + 188]
        i += 188
        pid = ((p[1] & 0x1f) << 8) | p[2]
        pids[pid] += 1
        afc = (p[3] >> 4) & 3
        if afc & 2 and p[4] > 0 and p[4] < 184 and (p[5] & 0x10):
            pcr.add(pid)
    for tid, sec in sections(buf):
        if tid == 0x00 and len(sec) >= 12:
            q = 6
            while q + 4 <= len(sec):
                prog = ((sec[q] & 0x0f) << 8) | sec[q + 1]
                ppm = ((sec[q + 2] & 0x1f) << 8) | sec[q + 3]
                if prog:
                    pat[prog] = ppm
                q += 4
        elif tid == 0x02 and len(sec) >= 12:
            prog = ((sec[3] & 0x1f) << 8) | sec[4]
            plen = ((sec[8] & 0x0f) << 8) | sec[9]
            pcr_pid = ((sec[6] & 0x1f) << 8) | sec[7]
            es = {}
            q = 10 + plen
            while q + 5 <= len(sec):
                st = sec[q]
                elen = ((sec[q + 3] & 0x0f) << 8) | sec[q + 4]
                epid = ((sec[q + 1] & 0x1f) << 8) | sec[q + 2]
                es[epid] = st
                q += 5 + elen
            pmts.append((prog, pcr_pid, es))
    return dict(pids=pids, pcr=sorted(pcr), pat=pat, pmts=pmts)


if __name__ == "__main__":
    import sys
    d = open(sys.argv[1], "rb").read()
    a = analyze(d)
    print("bytes", len(d), "packets", len(a["pids"]) and sum(a["pids"].values()))
    print("top pids", a["pids"].most_common(8))
    print("pcr", a["pcr"], "pat", a["pat"])
    for prog, pcr_pid, es in a["pmts"][:3]:
        print("pmt prog=%s pcr=%s es=%s" % (prog, pcr_pid, {k: hex(v) for k, v in es.items()}))
