"""Per-M1 lineup model: every row comes from that M1's own api8 channel list.

Nothing here knows any market. Guide numbers, names, stream URLs and XMLTV ids
are all derived from the api8 rows of the M1 being served, so a box in another
city produces its own lineup and guide mapping with no config.

Unique guide numbers (the normal case, and every list seen so far) keep
exactly the historical output: URL ``/auto/v<GN>``, XMLTV id ``zb-<GN>``,
GuideName = call letters (``"<call> <GN>"`` when call letters repeat). The
LineupVersion/ETag hash is therefore unchanged for those lineups.

Duplicate guide numbers happen on market fringes (two stations both mapped to,
say, 2.1 from different RF channels). The first row in api8 order keeps the
plain forms; later duplicates get a stable chan_id-based URL
``/auto/zb<chan_id>`` and XMLTV id ``zb-<GN>-t<transport>`` so each station
gets its own stream and guide instead of silently collapsing into one.
"""
import collections
import hashlib


def guide_number(c):
    return "%d.%d" % (c["chan_major"], c["chan_minor"])


def gn_sort_key(gn):
    try:
        return [int(x) for x in gn.split(".")]
    except ValueError:
        return [1 << 30]


def chan_fields(chan_id):
    """Decode a BRP chan_id: atsc (top nibble: 1 = ATSC-1, 3 = ATSC-3),
    transport (bits 48-59, the RF/mux key), major, minor. None if not hex."""
    try:
        v = int(str(chan_id), 16)
    except (TypeError, ValueError):
        return None
    return {"atsc": (v >> 60) & 0xF, "transport": (v >> 48) & 0xFFF,
            "major": (v >> 12) & 0xFFF, "minor": v & 0xFFF}


def visible(chans):
    return [c for c in chans if not c.get("hidden")]


Row = collections.namedtuple("Row", "gn name key xmltv_id chan dup")


def xmltv_id(gn, prefix="", suffix=""):
    """``zb-<GN>`` (historical), ``zb-<prefix>-<GN>`` when serving several M1s
    side by side, plus ``-<suffix>`` for a duplicate guide number."""
    out = "zb-%s%s" % (("%s-" % prefix) if prefix else "", gn)
    return out + ("-%s" % suffix if suffix else "")


def rows(chans, id_prefix=""):
    """One Row per visible channel, api8 order preserved."""
    vis = visible(chans)
    calls = collections.Counter(c["call_letters"].strip() for c in vis)
    seen = collections.Counter()
    out, used_names = [], set()
    for c in vis:
        gn = guide_number(c)
        call = c["call_letters"].strip()
        name = "%s %s" % (call, gn) if calls[call] > 1 else call
        seen[gn] += 1
        dup = seen[gn] > 1
        if not dup:
            key, xid = "v%s" % gn, xmltv_id(gn, id_prefix)
        else:
            f = chan_fields(c.get("chan_id")) or {}
            tsfx = ("t%03x" % f["transport"]) if "transport" in f else "n%d" % seen[gn]
            key = "zb%s" % c.get("chan_id")
            xid = xmltv_id(gn, id_prefix, tsfx)
            if name in used_names:
                name = "%s (%s)" % (name, tsfx)
        used_names.add(name)
        out.append(Row(gn, name, key, xid, c, dup))
    return out


def display_names(chans):
    """GuideName per guide number (first row wins for a duplicate number).
    Kept for callers that index by guide number."""
    out = {}
    for r in rows(chans):
        out.setdefault(r.gn, r.name)
    return out


def lineup_hash(chans):
    """sha1 over sorted ``GN|GuideName`` lines. Identical to the historical
    hash when guide numbers are unique; duplicates add their own lines."""
    rs = rows(chans)
    lines = ["%s|%s" % (r.gn, r.name) for r in
             sorted(rs, key=lambda r: (gn_sort_key(r.gn), r.dup, r.name))]
    return hashlib.sha1("\n".join(lines).encode()).hexdigest()


def lineup_version(chans):
    return int(lineup_hash(chans)[:8], 16)


def find(chans, tail):
    """Resolve the part of a stream path after ``/auto/``.

    ``v<GN>``      -> first row with that guide number
    ``v<major>``   -> first row with that major (historical behaviour)
    ``zb<chan_id>`` -> that exact channel (used by duplicate-number rows)
    """
    tail = (tail or "").split("?", 1)[0].strip("/")
    vis = visible(chans) + [c for c in chans if c.get("hidden")]
    if tail.startswith("zb"):
        cid = tail[2:].lower()
        return next((c for c in vis if str(c.get("chan_id", "")).lower() == cid),
                    None)
    if not tail.startswith("v"):
        return None
    num = tail[1:]
    ch = next((c for c in vis if guide_number(c) == num), None)
    if ch is None and "." not in num:
        ch = next((c for c in vis if str(c["chan_major"]) == num), None)
    return ch
