"""XMLTV EPG from the M1's api12, with on-disk cache and background refresh."""
import html
import json
import os
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EPG_CACHE = os.environ.get("ZEPG_CACHE", os.path.join(HERE, "epg-cache.xml"))
EPG_REFRESH = float(os.environ.get("ZEPG_REFRESH", str(6 * 3600)))
EPG_FRESH = float(os.environ.get("ZEPG_FRESH", str(7 * 3600)))


def channel_xmltv_id(guide_number):
    return "zb-%s" % guide_number


def _xt(t):
    return time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(t))


def _e(s):
    return html.escape(str(s), quote=True)


def placeholder_programmes(now, hours, days=7):
    """Filler slots for a channel the M1 has no guide data for, aligned to
    the hour, covering `days` from the start of the current hour."""
    if hours <= 0:
        return []
    step = int(hours * 3600)
    start = int(now) - int(now) % 3600
    return [(t, t + step) for t in range(start, start + days * 86400, step)]


def build_xmltv(chans, fetch_epg, guide_number, display_name, xmltv_id=None,
                placeholder_hours=0, now=None):
    """Build XMLTV text from a lineup. fetch_epg(chan) -> dict or None.

    xmltv_id(chan) -> id or None (None = historical ``zb-<GN>``).
    placeholder_hours > 0 adds "<name>" filler programmes for channels with no
    guide data so clients still map them; they never count as covered."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<tv generator-info-name="zapperbridge-hdhr-facade">']
    progs = []
    ok = 0
    for c in chans:
        if c.get("hidden"):
            continue
        gn = guide_number(c)
        cid = (xmltv_id(c) if xmltv_id else None) or channel_xmltv_id(gn)
        name = display_name(c)
        d = fetch_epg(c) or {}
        pl = d.get("program_list") or []
        if pl:
            ok += 1
        icon = ('<icon src="%s"/>' % _e(d["icon"])) if d.get("icon") else ""
        out.append(
            ' <channel id="%s"><display-name>%s</display-name>'
            '<display-name>%s</display-name>'
            '<display-name>%s %s</display-name><lcn>%s</lcn>%s</channel>'
            % (cid, _e(gn), _e(name), _e(gn), _e(name), _e(gn), icon))
        for p in pl:
            x = (' <programme start="%s" stop="%s" channel="%s"><title>%s</title>'
                 % (_xt(p["start_time"]), _xt(p["end_time"]), cid, _e(p["title"])))
            if p.get("episode_title"):
                x += "<sub-title>%s</sub-title>" % _e(p["episode_title"])
            if p.get("description"):
                x += "<desc>%s</desc>" % _e(p["description"])
            if p.get("poster"):
                x += '<icon src="%s"/>' % _e(p["poster"])
            if p.get("season_num") or p.get("episode_num"):
                sn = (max(int(p.get("season_num") or 1) - 1, 0)
                      if p.get("season_num") else "")
                en = (max(int(p.get("episode_num") or 1) - 1, 0)
                      if p.get("episode_num") else "")
                x += '<episode-num system="xmltv_ns">%s.%s.</episode-num>' % (
                    sn, en)
            if p.get("first_run") and not p.get("repeat"):
                x += "<new/>"
            elif p.get("repeat"):
                x += "<previously-shown/>"
            if p.get("rating"):
                x += ('<rating system="VCHIP"><value>%s</value></rating>'
                      % _e(p["rating"]))
            progs.append(x + "</programme>")
        if not pl and placeholder_hours > 0:
            for a, b in placeholder_programmes(
                    time.time() if now is None else now, placeholder_hours):
                progs.append(
                    ' <programme start="%s" stop="%s" channel="%s"><title>%s'
                    '</title><desc>No guide data from the M1 for %s.</desc>'
                    '</programme>' % (_xt(a), _xt(b), cid, _e(name), _e(gn)))
    out.extend(progs)
    out.append("</tv>")
    return "\n".join(out) + "\n", ok, len(progs)


class EpgCache:
    """Serve last-good XMLTV; refresh in the background every EPG_REFRESH."""

    def __init__(self, path=EPG_CACHE, owner=None):
        self.path = path
        #: Which M1 this guide belongs to. A cached guide tagged for another
        #: M1 (another user's box / market) is never loaded. None = no check.
        self.owner = owner
        self.lock = threading.RLock()
        self.xml = None
        self.at = 0
        self.channels_ok = 0
        self.programmes = 0
        self.last_error = None
        self.refreshing = False
        self.builder = None
        self.last_attempt = 0
        self._load_disk()

    def _meta_path(self):
        return self.path + ".meta.json"

    def _disk_owner(self):
        try:
            with open(self._meta_path()) as fh:
                return json.load(fh).get("m1")
        except Exception:
            return None

    def _load_disk(self):
        disk_owner = self._disk_owner()
        if (self.owner is not None and disk_owner is not None
                and disk_owner != self.owner):
            print("epg cache belongs to M1 %s, not %s: not loaded" % (
                disk_owner, self.owner), flush=True)
            return False
        try:
            with open(self.path) as fh:
                xml = fh.read()
            if xml.startswith("<?xml") and "</tv>" in xml:
                self.xml = xml
                self.at = os.path.getmtime(self.path)
                return True
        except Exception:
            pass
        return False

    def fresh(self):
        return bool(self.xml) and (time.time() - self.at) < EPG_FRESH

    def age_sec(self):
        return None if not self.xml else max(0.0, time.time() - self.at)

    def get(self, ensure=True):
        with self.lock:
            have = self.xml
        if have is None and ensure:
            self.refresh(blocking=True)
            with self.lock:
                have = self.xml
        elif ensure and not self.fresh() and not self.refreshing:
            threading.Thread(target=self.refresh, kwargs={"blocking": False},
                             daemon=True).start()
        return have

    def refresh(self, blocking=False, builder=None):
        with self.lock:
            if self.refreshing:
                return False
            self.refreshing = True
        try:
            builder = builder or self.builder
            if builder is None:
                return False
            self.last_attempt = time.time()
            xml, ok, nprog = builder()
            if not xml or "</tv>" not in xml:
                raise RuntimeError("empty epg")
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                fh.write(xml)
            os.replace(tmp, self.path)
            if self.owner is not None:
                try:
                    with open(self._meta_path(), "w") as fh:
                        json.dump({"m1": self.owner}, fh)
                except Exception:
                    pass
            with self.lock:
                self.xml = xml
                self.at = time.time()
                self.channels_ok = ok
                self.programmes = nprog
                self.last_error = None
            print("epg refresh ok channels=%d programmes=%d bytes=%d" % (
                ok, nprog, len(xml)), flush=True)
            return True
        except Exception as e:
            with self.lock:
                self.last_error = str(e)
            print("epg refresh fail: %r (serving last-good=%s)" % (
                e, bool(self.xml)), flush=True)
            return False
        finally:
            with self.lock:
                self.refreshing = False

    def start_background(self, builder, interval=EPG_REFRESH, retry=300,
                         first_delay=20):
        """Refresh every `interval`; after a failure/deferral retry in `retry`."""
        self.builder = builder

        def loop():
            time.sleep(first_delay)
            while True:
                ok = True
                try:
                    if self.xml is None or (time.time() - self.at) >= interval:
                        ok = self.refresh(blocking=True)
                except Exception as e:
                    ok = False
                    print("epg bg err: %r" % (e,), flush=True)
                if ok and self.xml is not None:
                    wait = max(60.0, interval - (time.time() - self.at))
                else:
                    wait = retry
                time.sleep(wait)
        threading.Thread(target=loop, daemon=True).start()
