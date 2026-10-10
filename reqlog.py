"""Opt-in on-disk request log for stream open/close (stdlib only, default OFF).

Enable by pointing ``ZREQLOG`` (or ``ZUI_REQLOG``) at a file path. Unset or
empty = completely off: no file is created and no state is kept.

One JSON object per line:
  open : {ts, event:"open", chan_id, client_ip, ua, path}
  close: {ts, event:"close", chan_id, client_ip, ua, path, bytes, dur_sec,
          reason, wgap}
``wgap`` is the write-gap summary dict (needs ZLOG_DIAG) or null.

Safety rules:
  * the request path is logged WITHOUT its query string / fragment; no other
    request header, cookie or URL is ever recorded (only User-Agent, trimmed
    and control-characters replaced);
  * every error (bad path, disk full, permissions, rotation race) is
    swallowed: logging can never affect a stream;
  * size-based rotation: ``ZREQLOG_MAX_BYTES`` (default 5 MB) and
    ``ZREQLOG_FILES`` total files (default 3: path, path.1, path.2); file mode
    0600; the oldest file is dropped.
"""
import json
import os
import threading
import time

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_FILES = 3
_lk = threading.Lock()


def path():
    """Configured log path or None (off)."""
    p = (os.environ.get("ZREQLOG") or os.environ.get("ZUI_REQLOG") or "").strip()
    return p or None


def _int_env(name, default, lo):
    try:
        return max(lo, int(os.environ.get(name, "") or default))
    except Exception:
        return default


def _clean(v, limit=200):
    try:
        s = "".join("?" if (ord(c) < 32 or ord(c) == 127) else c
                    for c in str(v if v is not None else "-")).strip() or "-"
        return s if len(s) <= limit else s[:limit] + "..."
    except Exception:
        return "?"


def safe_path(raw):
    """Request path only: query string and fragment dropped."""
    try:
        return _clean(str(raw).split("?", 1)[0].split("#", 1)[0], 160)
    except Exception:
        return "?"


def _rotate(p, max_bytes, nfiles):
    try:
        if os.path.getsize(p) < max_bytes:
            return
    except OSError:
        return
    keep = max(1, nfiles) - 1          # number of backups
    if keep == 0:
        open(p, "w").close()
        return
    last = "%s.%d" % (p, keep)
    if os.path.exists(last):
        os.remove(last)
    for i in range(keep - 1, 0, -1):
        src = "%s.%d" % (p, i)
        if os.path.exists(src):
            os.replace(src, "%s.%d" % (p, i + 1))
    os.replace(p, p + ".1")


def write(rec):
    """Append one record (a dict). No-op when off; never raises."""
    try:
        p = path()
        if not p:
            return False
        line = json.dumps(rec, separators=(",", ":"), default=str) + "\n"
        with _lk:
            _rotate(p, _int_env("ZREQLOG_MAX_BYTES", DEFAULT_MAX_BYTES, 1024),
                    _int_env("ZREQLOG_FILES", DEFAULT_FILES, 1))
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8", "replace"))
            finally:
                os.close(fd)
        return True
    except Exception:
        return False


def _base(event, chan_id, client_ip, ua, raw_path):
    return {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event,
            "chan_id": _clean(chan_id, 64), "client_ip": _clean(client_ip, 64),
            "ua": _clean(ua), "path": safe_path(raw_path)}


def log_open(chan_id, client_ip, ua, raw_path):
    try:
        if not path():
            return False
        return write(_base("open", chan_id, client_ip, ua, raw_path))
    except Exception:
        return False


def log_close(chan_id, client_ip, ua, raw_path, nbytes=None, dur_sec=None,
              reason=None, wgap=None):
    try:
        if not path():
            return False
        r = _base("close", chan_id, client_ip, ua, raw_path)
        r.update({"bytes": nbytes,
                  "dur_sec": None if dur_sec is None else round(float(dur_sec), 2),
                  "reason": _clean(reason or "unknown", 80),
                  "wgap": wgap if isinstance(wgap, dict) else None})
        return write(r)
    except Exception:
        return False
