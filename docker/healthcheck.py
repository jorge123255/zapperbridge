#!/usr/bin/env python3
"""Container HEALTHCHECK: GET /healthz on the first facade (stdlib only).

Exit 0 = healthy, 1 = unhealthy. The port is, in order: $ZB_HEALTH_PORT, the
first M1's http_port from the config plan, then 80. Liveness only: /healthz never touches
the M1, so an unreachable M1 does NOT make the container unhealthy (the UI shows that).
"""
import json
import os
import sys
import urllib.request

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)


def port():
    p = os.environ.get("ZB_HEALTH_PORT")
    if p:
        return int(p)
    try:
        import zbconf
        path = os.environ.get("ZB_CONFIG", "/config/zapperbridge.toml")
        cfg = zbconf.load(path) if os.path.exists(path) else {}
        _core, plan, _errs, _warns = zbconf.expand(zbconf.apply_env(cfg))
        return int(plan["m1s"][0]["http_port"])
    except Exception:
        return 80


def main():
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port(), timeout=4) as r:
            d = json.load(r)
        return 0 if r.status == 200 and d.get("ok") is True else 1
    except Exception as e:
        print("unhealthy: %r" % (e,), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
