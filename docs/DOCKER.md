# Docker image

Nothing in the existing runtime changes. The image wraps `hdhr_emulator.py`
with a config validator (`zbconf.py` on top of `m1config.py`) and an
entrypoint (`docker/entrypoint.py`).

## Flow
1. On each M1 turn **sharing** on in the Zapper app (otherwise NOT_SHARED).
2. Copy `zapperbridge.example.toml` to `zapperbridge.toml`; every choice is a
   commented key (M1 addresses or auto, push IP pool, network mode, device id
   seed, tuner cap, advertised IP/ports, HDHR options, UI mode, request log).
3. `docker compose run --rm zapperbridge check` validates and prints the plan
   (device ids, push IPs, ports, host alias commands). It opens no sockets.
4. `docker compose up -d`.

## Multi-M1 model
* One virtual HDHomeRun per M1 (`hdhr.mode = "per-m1"`, works today).
* Each M1 gets its own block of up to 3 device ids and push IPs; a tuner is an
  (M1, device id, push IP) triple. Ids are `device_prefix + sha1(seed|m1|slot)`,
  stable across restarts. Unless you set `device_seed` / `ZB_DEVICE_SEED`, the
  seed is random per install: created on first run in `/data/seed`
  (`ZB_DATA_DIR`), written atomically with mode 0600 and never regenerated while
  present. Mount `/data` as a volume and keep the file: deleting it changes
  every device id (the M1 remembers ids). `check` prints where the seed came
  from. If `/data` is not writable the bridge falls back to a fixed legacy seed
  and says so loudly (ids then match every other such install).

## When the M1 is not reachable
* `/healthz` and `/discover.json` always answer at once (no M1 needed).
* `/lineup_status.json`, `/lineup.json`, `/lineup.m3u` and the playlists wait
  at most `HDHR_LINEUP_TIMEOUT` (3 s) for the M1, then answer from the cached
  lineup, else an empty list; the `X-Zapperbridge-Lineup` header says why.
* With `ZB_M1=auto` (the default when no address is set; `ZM1=auto` on the bare facade) the container no longer
  exits: it serves HTTP, logs "still waiting" with a hint every ~30 s, and
  starts normal operation (guide, SSDP, tuning) as soon as the M1's announce is
  heard, no restart needed. In a Docker bridge network that announce never
  arrives: use host/macvlan networking, or set the address (`ZB_M1=<ip>` /
  `[[m1]] address`; `ZM1=<ip>` for the bare facade). Push IPs come from `push.pool` in order, or are
  pinned per M1 with `push_ips`.
* Advertised TunerCount per M1 = leases (+1 fixed HLS window); `hdhr.tuner_cap`
  caps the total across M1s.
* Rejected before anything starts: any address in `push.reserved_ips`, an M1's
  own IP as a push IP (always), duplicate push IPs, duplicate device ids, the
  real Mini's device id, pool exhaustion, bad modes.

## Reserved IPs and the "is it free?" check
* No address is hard-coded in this layer. `push.reserved_ips` is **your** list
  of real devices on the LAN that must never be a push IP (single IPs, ranges
  or CIDR; env `ZB_RESERVED_IPS`). The example file shows a real Zapper Mini at
  a placeholder address: pointing a virtual tuner at it would steal the M1's TS
  pushes. `never_ips` is the older name for the same list. An M1's own address
  is always rejected on top of the list.
* No address is built in: with an empty list every address is ordinary except
  your M1s' own.
* `push.check_free` (default **true**) runs in `check`, `plan` and `run`: for
  each planned push IP it sends one ping and reads the ARP/neighbour table. An
  address that answers fails startup (exit 2); set `check_free_fail = false` to
  only warn. Addresses this host already owns (your aliases) are skipped, tools
  that are missing give a warning and are never fatal, and M1 and reserved
  addresses are never probed. Nothing is sent to an M1 and nothing is spoofed.
  Skip it with `check_free = false`, `ZB_CHECK_FREE=0` or
  `entrypoint.py check --skip-free-check`. Unit tests never touch the network.

## Network
| mode | where | notes |
|---|---|---|
| host | Linux | add each push IP as a host alias (printed, never run) |
| macvlan | Linux, wired | container has its own LAN IP; extra push IPs open item |
| ipvlan L2 | Linux, Wi-Fi ok | same limits as macvlan |
Docker Desktop (macOS/Windows) cannot expose LAN IPs to containers; run the
bridge natively there.

## Privileges
Runs as uid 10001. `:80` works through a file capability
(`cap_net_bind_service`) on the python binary; compose uses `cap_drop: ALL` +
`cap_add: NET_BIND_SERVICE`. `no-new-privileges` breaks that (use
`http_port = 8080`). No `NET_ADMIN`: the image never edits interfaces.

## Discovery planner (`discover`, dry run)
`docker compose run --rm zapperbridge discover --listen 60` (or
`python3 m1super.py plan ...`) finds M1s and prints what *would* be
registered. It changes nothing and, by default, sends nothing.
* **Sources:** passive UDP 31064 listen (`--listen SEC`, receive-only), an
  offline capture (`--from-pcap FILE`), and an optional `--sweep CIDR` that only
  TCP-connect-checks the BRP port (no bytes written; max 1024 hosts; never
  touches `push.reserved_ips` or already-seen boxes). Everything is off unless
  you pass the flag (or set `[discovery] listen_seconds` / `sweep`).
* **Tuner count:** from the announce tuners byte. Rule: **usable = announced
  minus 1** (the M1 keeps one tuner for itself, its fixed HLS window).
  `tuners=4` announced (the only M1 capture we have, share=3) = 3 usable BRP
  leases, shown as `[evidence]`. Any other value follows the same rule but is
  `[unconfirmed]` (capped at 3) and
  the `[[m1]] leases` key always wins. A box whose share byte lacks the tuners
  bit is reported as NOT_SHARED and skipped. What the share byte means beyond
  bit 1 = library / bit 2 = tuners is not confirmed.
* **Real devices:** an announce with tuners=0 (a real Mini/app) is treated as
  protected: its IP is added to the reserved list and its id to `never_devs`.
  No address is a built-in default; `reserved_ips` stays your editable list.
* **Output:** per-M1 device ids + push IPs through `zbconf` (same ids as
  `check`), plus the exact ANNOUNCE datagrams that `--apply` would send
  (`--json` for tooling).
* **`--apply`:** the only code path that sends. It sends one UDP 31064
  ANNOUNCE (share=1, tuners=0) per planned id, bound to that id's push IP as the
  real source (no spoofing). It refuses on any config error, a missing push IP,
  a push IP that is reserved/an M1, a real device's id, or an IP this host does
  not own. It never sends an api6 join (the facade's bootstrap joins each id
  when it starts), never api7. Config files and env cannot enable it. Tests use
  a fake sender and block sockets.
  An `auto` / `auto:<dev>` address is resolved to the M1's discovered IP for
  the unicast copy (if discovery did not see the M1, only the broadcast is
  sent and the output says so). A send error is printed per id
  (`FAILED to announce ...`, exit code 4) instead of a traceback.
  **Same ids as `run`:** the plan uses the same entries, seed and key as the
  facade. `run` records the ids it starts with in `/data/active-ids.json`
  (no seed in it), and `--apply` refuses if its ids differ (that happens when
  the one-off container sees a different `/data`, e.g. another volume). Both
  print a `device id seed ... (fingerprint xxxxxxxx)` line to compare. The
  safest way is to run it inside the bridge container, so it shares its
  `/data` and env:
  `docker compose exec zapperbridge python3 /app/docker/entrypoint.py discover --listen 15 --apply`,
  then restart the container so the facade joins the newly announced ids.
  If the ids still differ, the two containers read different `/data` volumes
  (e.g. the bridge was started with `docker run -v zapperbridge-data:/data`,
  while `docker compose run` uses the project volume
  `<project>_zapperbridge-data`). Use `docker exec` / `docker compose exec`
  into the running bridge, or pin the ids: copy the `dev ...` lines from the
  bridge's startup log into `[[m1]] devs = [...]`; both paths honour an
  explicit `devs` list regardless of the seed. Note the ids are keyed on the
  address form: a literal IP keys on the IP, `auto`/`auto:<dev>` on the entry
  name, so switching between them changes the derived ids.
  **Why the lineup fails:** `/health` → `api8` now shows `err_name` (the M1's
  answer, e.g. `NOT_FOUND`, `DEVICE_ID_NOT_ALLOWED`, `NOT_SHARED`),
  `handshake` (the api6 result) and a `hint`; the log prints it once per
  change. `NOT_SHARED` = turn sharing on in the Zapper app; `NOT_FOUND` /
  `DEVICE_ID_NOT_ALLOWED` = the bridge's device ids are not registered yet.
  The facade itself never announces its ids (registration stays the explicit
  `--apply` step); it only joins (api6) ids the M1 already knows.
  **Status: not validated.** A first real attempt (from a Kubernetes node, new
  device ID) sent the announce but the M1's api6 join still answered
  `NOT_FOUND` every time (7 tries). Cause unconfirmed; don't rely on `--apply`.

## SSDP (UPnP) announcements
By default each facade announces itself on the LAN (UDP 1900 multicast) so
HDHomeRun apps auto-discover it. That makes it show up as an extra HDHomeRun,
so a bridge you start from a cluster node, a test container or a second host
advertises a **second HDHomeRun on your LAN**. Turn it off with
`[network] ssdp = false` or `ZB_SSDP=0`; apps then need the bridge's IP added
by hand. Default is on (unchanged).

## Not done yet (reserved / open)
* Live registration loop / re-announce cadence for many M1s in one container
  (today: one facade process per configured M1, `auto` resolves via the
  existing passive announce listener; `discover --apply` is a one-shot).
* An active DISCOVER probe (type 3): whether the M1 answers is unproven, so it
  is not implemented.
* `hdhr.mode = "merged"` runtime wiring. The merge/routing module
  (`lineupmerge.py`) and the config plan exist and are tested; the facade does
  not use them yet. See `docs/MERGED-LINEUP.md` for what is still needed.
* Extra push IPs under macvlan/ipvlan (needs an opt-in NET_ADMIN helper).
* Reserved keys accepted but ignored: UDP 65001 responder, checksum DeviceID,
  `ui.mode = "admin"`.
* Published as `sunnyside1/zapperbridge` on Docker Hub (`docker pull sunnyside1/zapperbridge:v0.1.0`, amd64 + arm64). Registration of new tuners on a fresh M1 from Docker is not yet validated. Earlier note: the image had not been run on real hardware; the config layer, the Dockerfile
  (static review) and the healthcheck are unit-tested offline. Build it locally.

## Web UI, token and healthcheck
* `[ui] mode = "readonly"` turns on the read-only status pages (default `off`).
  The entrypoint passes `ZUI=readonly` and your `reserved_ips` (as
  `ZUI_RESERVED_IPS`) to each facade; the UI shows nothing else about reserved
  devices. Details: `docs/WEBUI.md`.
* **Bind/exposure:** the UI shares the HDHR JSON server, so it listens on every
  interface the container has (host networking: all host interfaces). Only
  private/loopback/link-local source addresses are served. Behind a reverse
  proxy or a Docker bridge network the source is always private, so that check
  alone is NOT protection there: set `ZUI_TOKEN`. Never port-forward it.
* **`ZUI_TOKEN`** (env only, >= 8 chars, 20+ random chars recommended): every
  `/ui` and `/api/ui/*` request then needs `Authorization: Bearer <token>` or
  the `zui_token` cookie. Open `http://<host>/ui?token=<token>` once in a
  browser to get the cookie (HttpOnly, SameSite=Strict). The token is
  plain-HTTP on your LAN, so it keeps casual/accidental access out; it is not
  a substitute for network isolation.
* **HEALTHCHECK:** `docker/healthcheck.py` GETs `/discover.json` on 127.0.0.1
  (port = `ZB_HEALTH_PORT`, else the first M1's `http_port`, else 80). It reads
  only; nothing is sent to the M1.
