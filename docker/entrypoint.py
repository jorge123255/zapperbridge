#!/usr/bin/env python3
"""Container entrypoint: validate config, print the plan, then run the facade.

  entrypoint.py check   validate + print the plan, exit 0/2
  entrypoint.py plan    same as check
  entrypoint.py run     check, then start one hdhr_emulator.py per M1 (default)
  entrypoint.py discover [--listen SEC] [--from-pcap F] [--sweep CIDR] [--json]
                        multi-M1 discovery PLAN (m1super.py). Dry run: receive-only,
                        sends nothing. Only an explicit --apply sends (one UDP 31064
                        ANNOUNCE per planned id from its push IP); never api7, never
                        a join.

check/plan/run also run the push-IP "is it free?" probe (push.check_free,
default on): one ICMP echo + a neighbour-table read per candidate push IP,
never to an M1 or a reserved IP. Skip with push.check_free = false,
ZB_CHECK_FREE=0 or --skip-free-check. Otherwise nothing is sent to the LAN.

Config: $ZB_CONFIG (default /config/zapperbridge.toml) + ZB_* env overrides.
It never runs ip/ifconfig, never sends anything to an M1 itself, never uses
api7. Anything invalid exits 2 before any socket is opened.
"""
import os
import signal
import subprocess
import sys
import time

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)
import zbconf  # noqa: E402


def main(argv):
    argv = list(argv)
    skip_free = "--skip-free-check" in argv
    if skip_free:
        argv.remove("--skip-free-check")
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "discover":
        import m1super  # noqa: E402  (stdlib-only; dry run unless --apply)
        return m1super.main(["plan"] + argv[2:])
    if cmd not in ("check", "plan", "run"):
        print("usage: entrypoint.py [check|plan|run|discover]", file=sys.stderr)
        return 64
    path = os.environ.get("ZB_CONFIG", "/config/zapperbridge.toml")
    cfg = {}
    if os.path.exists(path):
        try:
            cfg = zbconf.load(path)
        except Exception as e:  # tomllib.TOMLDecodeError etc.
            print("ERROR cannot read %s: %s" % (path, e), file=sys.stderr)
            return 2
    else:
        print("note: %s not found; using defaults + ZB_* env" % path)
    cfg = zbconf.apply_env(cfg)
    _core, plan, errs, warns = zbconf.expand(cfg)
    print(zbconf.render_plan(plan, errs, warns), flush=True)
    if errs:
        print("config invalid: refusing to start", file=sys.stderr)
        return 2
    fc = plan.get("check_free") or {}
    if fc.get("enabled") and not skip_free:
        in_use, notes = zbconf.check_free(plan)
        for n in notes:
            print("info: " + n, flush=True)
        for x in in_use:
            print("%s push ip answers on the LAN: %s" % (
                "ERROR" if fc.get("fail") else "WARN ", x), flush=True)
        if in_use and fc.get("fail"):
            print("push IP(s) already in use: refusing to start (pick free "
                  "addresses, or set push.check_free = false / "
                  "check_free_fail = false)", file=sys.stderr)
            return 2
    elif not skip_free:
        print("note: push.check_free is off; push IPs were not probed")
    if cmd in ("check", "plan"):
        return 0
    if plan.get("merged"):
        print("hdhr.mode = \"merged\" is plan-only: the facade is not wired "
              "for it yet (docs/MERGED-LINEUP.md). Use \"per-m1\" to run.",
              file=sys.stderr)
        return 2
    procs = []
    for name, env in zbconf.launch_envs(plan):
        e = dict(os.environ)
        e.setdefault("HDHR_LOG", "/data/hdhr_facade.log")
        e.setdefault("ZEPG_CACHE", "/data/epg-cache-%s.xml" % name)
        e.update(env)
        print("starting facade for %s" % name, flush=True)
        procs.append(subprocess.Popen(
            [sys.executable, os.path.join(APP, "hdhr_emulator.py")],
            env=e, cwd=APP))

    def stop(signum, _frame):
        for p in procs:
            if p.poll() is None:
                p.terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    rc = 0
    while procs:
        for p in list(procs):
            r = p.poll()
            if r is not None:
                procs.remove(p)
                rc = rc or r
                stop(signal.SIGTERM, None)  # one down -> stop all, let
                # the container restart policy decide
        time.sleep(1)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
