# Read-only web UI (optional, off by default)

`ZUI=readonly` turns on a small status UI served by the existing :80 HTTP
server (`webui.py` + static `ui/`, stdlib only, no build step). Unset (the
default) or any other value: `/ui` and `/api/ui/*` do not exist and the facade
behaves exactly as before. No restart is needed to *keep* it off.

| Env | Meaning |
|---|---|
| `ZUI=readonly` | enable (only value that does) |
| `ZUI_TOKEN` | optional access token (>= 8 chars; shorter = every request refused). When set, `/ui` and `/api/ui/*` need `Authorization: Bearer <token>` or the `zui_token` cookie; `GET /ui?token=<token>` sets the cookie (HttpOnly, SameSite=Strict) and redirects to `/ui`. Unset = no token (LAN-source check only) |
| `ZUI_RESERVED_IPS` | comma list of reserved real-device IPs for the Devices/Aliases pages. Only what you set is shown; there is no built-in default (the Docker entrypoint passes `push.reserved_ips`) |
| `ZUI_LOG_LINES` | in-memory log ring size, default 500 |
| `ZREQLOG=/path` (or `ZUI_REQLOG`) | opt-in JSON-lines request log, see below (default OFF; works without `ZUI`) |
| `ZREQLOG_MAX_BYTES` / `ZREQLOG_FILES` | rotation size (default 5 MB) / total files (default 3) |
| `ZUI_DISCO=1` | also run a receive-only UDP 31064 listener for the Devices page (**stays OFF by default**) |

## Pages (`http://<bridge>/ui`)
Dashboard (health, lineup count, tuner slots), Streams (live: client IP, UA,
start, pacer stats; recent closed with close reason), Lineup, Devices
(configured M1 + BRP ids + push IPs; passive announces if `ZUI_DISCO=1`; real
Minis marked protected), Aliases (expected push IPs vs this host, copyable
`ifconfig`/`ip` commands that the bridge never runs), Logs (ring of recent
`stream open/close`, `wgap summary/rebuffer/write_gap`, `brp stream ... closed`
lines from `zdiag`; needs `ZLOG_DIAG` on).

## Live updates (Streams and Logs pages)
Plain `fetch` polling, no websockets. While the Streams or Logs page is open
the browser asks `GET /api/ui/events` every ~1.5 s. It returns three cheap
counters and reads no tuner/BRP/lineup state:
`{"seq": stream open/close counter, "log_seq": log-ring cursor, "live": n, "hls_live": n}`.
* Streams refetches `/api/ui/streams` only when `seq`/`log_seq` moved or a
  stream is live (its byte counters move). Idle = one tiny request per tick.
* Logs asks `GET /api/ui/logs?limit=300&since=<cursor>` and prepends only new
  lines (each line has a `seq`; the reply has the next `cursor`).
* The header shows `live` / `offline`. The tab pauses polling while hidden.
  Other pages refresh every ~5 s as before.

## Request log file (opt-in, default OFF)
`ZREQLOG=/var/log/zapperbridge-req.jsonl` (or `ZUI_REQLOG`). Unset = no file,
no state. One JSON object per line, written when a stream opens and closes:

    {"ts":"...","event":"open","chan_id":"0x..","client_ip":"192.0.2.50","ua":"...","path":"/auto/v2.1"}
    {"ts":"...","event":"close",...,"bytes":41745400,"dur_sec":77.3,"reason":"client_closed","wgap":{...}}

`wgap` = the write-gap summary (p50/p95/max write gap, counts over 100 and
500 ms, max block, max input gap); it is `null` unless `ZLOG_DIAG` is on.
* Never logged: query strings / fragments (tokens live there), cookies, any
  header other than User-Agent (trimmed, control characters replaced), env,
  config. Only the request path is kept.
* Rotation by size: 5 MB x 3 files (`path`, `path.1`, `path.2`); override with
  `ZREQLOG_MAX_BYTES` / `ZREQLOG_FILES`. File mode 0600.
* Every error (missing dir, disk full, permissions) is swallowed; a failing
  log can never affect a stream. Turning it on needs a facade restart (env).

## Guarantees
* GET/HEAD only. No write endpoint, no shell, no restart, never api7.
* Cached state only: never joins the M1, never calls BRP, never refreshes the
  lineup, never creates the tuner. Before the first tune, slot detail is
  absent and the dashboard says so.
* LAN only: non-private source addresses get 403. This check sees the TCP
  peer, so behind a reverse proxy or Docker bridge network it always passes:
  set `ZUI_TOKEN` there and never expose the port to the internet. The UI is
  served by the :80 HDHR server, so it listens wherever that does (all
  interfaces).
* No secrets stored or shown; all values are rendered with `textContent`;
  CSP `default-src 'none'`, `script-src 'self'`.
* Cost when on: a deque append per stream open/close line and a small dict per
  live stream. When off: one env lookup per request.
