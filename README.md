# zapperbridge

Turns a **ZapperBox M1** (ATSC 1.0/3.0 tuner on your LAN) into a standard
**HDHomeRun** device, so any app that speaks the HDHR API — Channels DVR,
VLC, Tvheadend, Jellyfin — can watch its channels over plain HTTP.

The M1 only pushes live video to a *paired* client. This service joins the M1's
BRP protocol as its own device, asks it to tune a channel, receives the pushed
MPEG-TS on TCP `31061`, re-muxes it with a clean PAT/PMT, and serves it on the
HDHomeRun TS port.

```
ZapperBox M1  ──BRP api6/api9──▶  this service  ──HDHR :80 JSON──▶  Channels / Plex / apps
   192.0.2.10  ──TS push :31061──▶  (re-mux)  ──HDHR :5004 TS──▶  VLC / recorder
```

> **Unofficial / not affiliated.** This is an independent, community
> interoperability project. It is **not affiliated with, endorsed by, or
> supported by ZapperBox (Zapper) or SiliconDust.** *HDHomeRun* is a trademark
> of SiliconDust; the name is used here only to describe API compatibility.
> It is meant for hardware you own, on your own network.
>
> **How protected content is handled.** The ZapperBox M1 does all tuning and
> any decryption itself, and outputs an unencrypted transport stream.
> zapperbridge only relays what the M1 delivers. It contains no DRM or
> decryption code, does not request, extract or handle keys or licences, and
> does not alter or remove any protection. Channels the M1 itself cannot
> decrypt are simply unavailable (they are flagged in the lineup and will not
> play). Use it only for content you are entitled to watch, and keep the
> resulting streams to yourself. This is not legal advice.
>
> The M1's BRP protocol is reverse-engineered and can change with any firmware
> update. Provided as-is, no warranty. **License:** MIT (see
> [License](#license)).

## Quick start with Docker (Linux host, recommended)

The container needs a **Linux host on the same subnet as the M1** (a NAS,
mini-PC, Raspberry Pi, VM with a bridged NIC). Steps:

1. **Turn sharing on.** In the Zapper app, enable *sharing* on each M1. Without
   it the M1 answers `NOT_SHARED` and nothing tunes.
2. **Make a config.** `cp zapperbridge.example.toml zapperbridge.toml`. With exactly
   one M1 on the LAN you can leave everything commented out (the M1 is found
   from its UDP 31064 announce). Otherwise add
   `[[m1]] address = "<M1 IP>"`. Reserve spare IPs for push addresses
   (`[push] pool = ["192.0.2.240-249"]`, outside your DHCP range) and list
   real Zapper Minis/devices in `reserved_ips` so they are never used.
3. **Check it** (opens no sockets; prints device IDs, push IPs and the exact
   `ip addr add` lines for the aliases it needs):
   ```sh
   docker run --rm --network host -v "$PWD/zapperbridge.toml:/config/zapperbridge.toml:ro" \
     <image> check
   ```
4. **Add the push-IP aliases** on the host NIC (copy the lines `check` printed;
   the container never edits interfaces), e.g.
   `sudo ip addr add 192.0.2.240/32 dev eth0`.
5. **Run it** (host networking is the one complete option today):
   ```sh
   docker run -d --name zapperbridge --restart unless-stopped --network host \
     -v "$PWD/zapperbridge.toml:/config/zapperbridge.toml:ro" -v zapperbridge-data:/data \
     --cap-drop ALL --cap-add NET_BIND_SERVICE <image>
   ```
   or with Compose: `docker compose up -d` (see `docker-compose.yml`).
   Do **not** add `no-new-privileges` (it disables the capability that lets the
   unprivileged user bind `:80`; set `http_port = 8080` if you must).
6. **Point your app at it.** In Channels DVR / Jellyfin / Plex / Tvheadend add an
   HDHomeRun at `http://<host>`; `curl http://<host>/lineup.json` should list
   your channels. A multi-M1 setup gets one virtual tuner per M1.

`<image>` is `sunnyside1/zapperbridge:v0.1.0` (Docker Hub, amd64 and arm64;
`latest` also works) or an image you built yourself (`docker build -t zapperbridge .`). Find M1s first with
`docker run --rm --network host <image> discover --listen 60` (a dry run:
receive-only, sends nothing).

**Where it works**

| Host | Status |
|---|---|
| Linux, `network_mode: host` | supported (needs the push-IP aliases above) |
| Linux, macvlan / ipvlan | **partial**: the container gets its own HDHR IP, but the extra per-tuner push IPs need a helper that is not shipped yet (see `docs/DOCKER.md`). macvlan is wired Ethernet only; Wi-Fi needs ipvlan L2 |
| Docker Desktop on **macOS / Windows** | **not possible**: the Docker VM hides the LAN, so the M1 cannot reach the container on TCP 31061 and you cannot give it LAN IPs. Run the Python bridge natively (below) |

Optional read-only status pages: set `[ui] mode = "readonly"` **and** the env var
`ZUI_TOKEN` (e.g. `-e ZUI_TOKEN=$(openssl rand -hex 16)`); then open
`/ui?token=<token>` once. Without a token the UI only checks for a private client
IP, which is not enough behind Docker networking or a reverse proxy
(see [`docs/WEBUI.md`](docs/WEBUI.md)). LAN only; never port-forward.

**Container health** is liveness only: the HEALTHCHECK calls `/healthz`
(`{"ok": true}`, no token, no M1 access). "M1 down / unreachable" is shown in the
UI and `/health`, not by the container's health state.

## Requirements (native run)

- ZapperBox M1 (or Mini) on the LAN, already activated and paired to at least one client.
- A host on the same subnet that can bind TCP `31061` (the M1 pushes there).
- Python 3.10+ — standard library only, no pip packages.
- Ports `80` (HDHR JSON) and `5004` (HDHR TS) free. `:80` needs root on macOS/Linux.

## Quick start (native Python)

```sh
git clone https://github.com/jorge123255/zapperbridge.git
cd zapperbridge

# point at your box + the IP this host is known by
export ZM1=192.0.2.10

sudo python3 hdhr_emulator.py            # :80 JSON, :5004 TS

curl -s localhost/lineup.json | head     # lineup should list your channels
```

Then in VLC: **Media → Open Network Stream** →
`http://<this-host>:5004/auto/v2.1`

Or load the whole playlist: `http://<this-host>:5004/playlist.m3u` (or
`http://<this-host>/lineup.m3u` with guide ids). Both are built live from your
own M1's channel list. 
Don't know the M1's IP? (Native: `ZM1=auto`; Docker: `ZB_M1=auto`, or set no address.) It finds it from the M1's own broadcast on
UDP 31064 (passive listen). It keeps listening in the background until an M1
is heard: the bridge serves HTTP meanwhile (`/healthz` OK, empty lineup) and
logs a hint every ~30 s. With several M1s on
the LAN, use `ZM1=auto:<m1 device id>` (Docker: `ZB_M1=auto:<m1 device id>`). `python3 m1disco.py 90` lists what it
hears. For several M1s see [`docs/DOCKER.md`](docs/DOCKER.md)
and `python3 m1config.py <config.json>`.

## HDHomeRun endpoints served

| Port | Path | Notes |
|---|---|---|
| 80 | `/discover.json` | device id, `TunerCount` |
| 80 | `/lineup.json` | GuideNumber / GuideName / `URL` = `http://<host>:5004/auto/v<GuideNumber>` |
| 80 | `/lineup_status.json` | scan-complete |
| 80, 5004 | `/playlist.m3u`, `/playlist.m3u8`, `/channels.m3u` | live playlist |
| 80 | `/tuners.json` | per-tuner status |
| 5004 | `/auto/v<GuideNumber>` | tunes the M1 and streams MPEG-TS |
| 5004 | `/hls/atsc{1,3}/1/master.m3u8` | redirect to the M1's own HLS server (`:8076`) |

## Configuration

All knobs are environment variables. `ZM1` defaults to `auto` (discovery); the example addresses below use the
documentation range `192.0.2.0/24`.

> **Docker vs native.** Docker users set the M1 with `ZB_M1=<ip|auto|auto:<id>>` or
> `[[m1]] address = "<ip>"` in the config; the container then sets `ZM1` for each
> facade itself, so a `ZM1=` you pass to the container is ignored. `ZM1` is the knob
> for the bare native facade (`python3 hdhr_emulator.py`). Other `ZM1_*` tuning knobs
> such as `ZM1_DISCOVER_LOG_SEC` are plain environment variables that the container
> passes through to the facade, so they work in Docker too.

### `hdhr_emulator.py`

| Var | Default | Meaning |
|---|---|---|
| `HDHR_JSON_PORT` | `80` | HDHR JSON port |
| `HDHR_TS_PORT` | `5004` | HDHR MPEG-TS port |
| `HDHR_BRIP` | `1` | use the BRP push tuner (set `0` to fall back to M1 HLS redirects) |
| `HDHR_HLS_TUNER` | `1` | add the M1's own tuner as a 4th row (`0` to drop it) |
| `HDHR_HLS_GUIDE` | `999.1` | guide number for the M1's own tuner |
| `HDHR_HLS_NAME` | `M1-TUNER4` | call letters for the M1's own tuner |
| `HDHR_HLS_PROTO` / `HDHR_HLS_SLOT` | `1` / `1` | which M1 HLS window to read |
| `HDHR_TS_DIRECT` | unset | unset = 302 redirect to `:5004`; `1` = serve TS directly from the JSON port too |
| `HDHR_BRP_DEV` | `02bb00000001` | device id used for the BRP join |
| `ZM1` | `auto` | M1 IP, or `auto` / `auto:<m1 dev id or ip>` (BRzb discovery) |
| `ZM1_DISCOVER_LOG_SEC` | `30` | seconds between "still waiting for an M1" log lines when `ZM1=auto` (discovery never gives up) |
| `HDHR_LINEUP_TIMEOUT` | `3.0` | seconds the lineup endpoints wait for the M1 before answering from the cached lineup (or `[]`) |
| `ZB_DATA_DIR` | `/data` | where the per-install device-id `seed` file lives |
| `HDHR_DEVICE_ID` | `02BB0001` | HDHR DeviceID (one per M1 when serving several) |
| `HDHR_FRIENDLY` | `ZapperBox M1 (emulated)` | FriendlyName |
| `ZEPG_ID_PREFIX` | empty | XMLTV id namespace (`zb-<prefix>-<GN>`) for side-by-side M1s |
| `ZEPG_PLACEHOLDER_H` | `0` | hours per filler programme for channels the M1 has no guide for (0 = off) |
| `HDHR_HB` | `1` | send api11 heartbeats |
| `HDHR_HB_SEC` | `2` | heartbeat interval |
| `ZGAP` | `1.0` | minimum gap between BRP requests |
| `M1_HLS_PORT` | `8076` | M1's own HLS port (fallback path) |
| `HDHR_LOG` | `<repo>/hdhr_facade.log` | log file |
| `HDHR_DAEMON` | unset | `1` = fork to background |

`ZM1` also sets the M1 address inside `hdhr_emulator.py`. Hard-coded at the top
of `hdhr_emulator.py`: `EMU_ID`, `DEVICE_ID`
(HDHR device id, 8 hex), `TUNER_COUNT` (advertised tuners — see the single-tuner
caveat below). Lineup is cached to `<repo>/lineup-cache.json`.

### `brptune.py` (BRP client + TS muxer)

| Var | Default | Meaning |
|---|---|---|
| `ZM1` | *(none; set it)* | M1 address |
| `ZBRP` | `31065` | BRP request port |
| `ZPUSH` | `31061` | TCP port the M1 pushes TS to (must be bindable here) |
| `ZDEV` | `02bb00000001` | BRP device id |
| `ZSTALE` | `6` | seconds of silence before a stream is declared dead |
| `ZCHUNK` | `256` | bytes per HTTP write to the client |
| `ZPSI` | `0.1` | re-emit PAT/PMT at least every N seconds |
| `ZPSI_PKTS` | `64` | …or every N packets, whichever comes first |
| `ZHB_SEC` | `2` | BRP heartbeat interval |

#### Stream pacing / A-V interleave (raw-TS `/auto/vN` path only)

The M1 pushes ~2 s bursts per leg (video, audio, ...). Without help the client
sees ~12-24 Mbps for ~0.4 s then ~1.6 s of nothing, with the audio of a burst
sitting after its video in the byte stream. Two stages fix that; the 999.1 HLS
remux is not touched.

| Var | Default | Meaning |
|---|---|---|
| `ZINTERLEAVE` | `1` | merge the per-PID queues by DTS/PTS (audio sent `ZIL_AUDIO_LEAD_MS` early) instead of round-robin by packet; never reorders packets of one PID. `0` = previous behaviour, byte for byte |
| `ZIL_WAIT_MS` | `2500` | most a PID with no data yet may hold the others back (the video tail of a burst waits for the next burst's audio) |
| `ZIL_AUDIO_LEAD_MS` | `130` | audio is scheduled this much before its PTS (an audio PES covers ~190 ms) |
| `ZPACE` | `1` | PCR-paced release behind a start jitter buffer. `0` = previous straight write-through |
| `ZPACE_PREBUF_MS` | `800` | start jitter buffer (adds about this much to time-to-first-video) |
| `ZPACE_MAX_START_MS` | `1500` | never hold the start longer than this |
| `ZPACE_MAX_BUF_MS` | `6000` | cap on buffered media; beyond it (slow client) skip ahead to a random-access point |
| `ZPACE_MAX_BYTES` | `12582912` | same cap in bytes |
| `ZPACE_AHEAD_MS` | `30` | writes coalesce the next ~30 ms of due packets (~25 KB at 7 Mbps, <= 64 KB) |
| `ZPACE_SNDBUF` | `1048576` | `SO_SNDBUF` on the client socket (`TCP_NODELAY` is also set) |

## How it works

1. **Join** — the bridge registers with the M1 as its own network client.
2. **Tune** — it asks the M1 to tune the channel chosen from the M1's own lineup.
3. **Push** — the M1 sends the stream back to the bridge's push address.
4. **Re-mux** — the bridge rebuilds clean stream tables, paces and interleaves
   audio/video, and serves it as ordinary MPEG-TS.
5. **Serve** — HDHR JSON on `:80`, TS on `:5004`.

The protocol is reverse-engineered and intentionally not documented in detail
here; the source is the reference.

## Channel notes

Most ATSC 1.0 channels are MPEG-2 (`704x480` up to `1920x1080`). Some H.264
channels use **GDR** pictures (`nal_type 9`) with broadcast SEI instead of IDR.
They decode fine, but the first in-band SPS/PPS can arrive ~1.4 s into the
stream, so expect a second or two of garbage before the picture resolves.

Check a channel on your own box with `ffprobe` against
`http://<this-host>:5004/auto/v<GN>`.

## Troubleshooting

- **`503 tuner busy`** — the M1 leases **three** BRP tuners (instances 3, 4, 5)
  from one global pool, shared by every device id. A 4th concurrent BRP tune is
  refused until a lease is released (the slot frees ~3–4 s late). The M1's own
  tuner is the 4th row (`999.1`) and is always available.
- **`404` on `/auto/v2.1`** — that path is on the **TS port (5004)**, not `:80`.
- **Tunes but nothing decodes** — never run a second BRP client while the
  facade is running; the M1 then answers `PACKET_ERROR`. One BRP request per
  TCP connection.
- **Stream stops after a few seconds** — check `ZSTALE`/heartbeat; the M1 closes
  a push if heartbeats stop.
- **`Address already in use` on 31061** — another listener (an old facade, a
  `tcpdump`-style probe) holds it. Only one process can own the push port.
- **Python HTTP client hangs reading the TS** — use `read1()`, not
  `read(65536)`; the latter blocks until the buffer is full.

## FAQ / troubleshooting (Docker and general)

- **`NOT_SHARED` / nothing tunes** — turn *sharing* on for the M1 in the Zapper app.
- **"M1 not found" / `auto` times out** — the M1 must be on the same subnet
  (UDP 31064 broadcast does not cross routers/VLANs). Set the address explicitly
  (`address = "<M1 IP>"` / `ZB_M1=<M1 IP>`); auto-discovery keeps waiting and
  logs why every ~30 s. In Docker it needs host or macvlan networking (a bridge
  network never sees the M1's broadcast). Run
  `<image> discover --listen 60` to see what is heard.
- **Port 80 busy** — another web server owns `:80`. Stop it, or set
  `[network] http_port = 8080` (some HDHR clients only look on 80).
- **Push-IP alias missing / `check` says address not local** — add the
  `ip addr add .../32 dev <nic>` (Linux) or `ifconfig <nic> alias ...` (macOS)
  lines `check` printed; they vanish at reboot unless you persist them.
- **"push ip answers on the LAN: refusing to start"** — something already owns
  that address. Pick free IPs outside DHCP, or set `push.check_free_fail = false`
  only if you are sure.
- **`check` says "could not check (no ping/ip/arp tool available)"** — the image
  deliberately ships without ping/iproute2, so the "is this push IP free?" probe
  (`push.check_free`) cannot run inside it. This is informational only. Verify
  the addresses are free yourself (`arping -D`), or run `check` natively.
- **Firewall** — allow inbound TCP 31061 (M1 push) and 80/5004 (clients), and UDP
  31064 (M1 announce) and 1900 (SSDP discovery).
- **Same subnet required** — the M1 pushes video back to a LAN IP; routed or
  NAT'd setups (and Docker bridge networks) do not work.
- **Wi-Fi + macvlan fails** — most Wi-Fi NICs drop macvlan frames; use ipvlan L2
  or a wired host.
- **One M1 = one set of device IDs** — each M1 gets its own IDs/push IPs derived
  from a seed. By default each install generates its own random seed on first
  run and keeps it in `/data/seed` (mount `/data`, or the ids change when the
  container is recreated). **Deleting that file changes every device id**, and
  the M1 remembers ids. Set `device_seed` / `ZB_DEVICE_SEED` to pin your own.
  Never
  reuse a real Mini's device ID.
- **How many tuners?** An M1 grants three concurrent BRP leases plus its own
  fixed HLS window. Multi-M1: usable = announced - 1 (only a 4-tuner announce is
  confirmed; others are `[unconfirmed]`).
- **Some channels won't play** — zapperbridge can only relay what the M1
  outputs in the clear. Channels the M1 itself cannot decrypt (for example some
  protected ATSC 3.0 services) are unavailable; the bridge has no decryption
  code and does not try to work around this.
- **Blank UI / `401`** — the UI is off unless `ui.mode = "readonly"`; with
  `ZUI_TOKEN` set, open `/ui?token=<token>` once.
- **Reporting a bug** — run `<image> check` and include its output plus a
  redacted log (remove IPs/IDs you care about) in a GitHub issue.

## Kubernetes / k3s

zapperbridge runs on Kubernetes, with limits. A pod normally has no LAN address of its own, and every virtual tuner needs its own IP for the M1 to push video to. So:

- **`hostNetwork: true` gives you the node's IP only.** That means one push IP, so one tuner (`leases = 1`), per node. Pin the pod to that node with a `nodeSelector` on `kubernetes.io/hostname`.
- **More tuners need more IPs.** Plain Kubernetes can't give pods their own LAN IPs. You need multus with macvlan or ipvlan, MetalLB or kube-vip, or extra IP aliases on the node. None of these are set up or tested here.
- **The push IP must be reachable by the M1.** The M1 sends video to the IP you register, on TCP `:31061`. With `hostNetwork` that is the node IP, and it must be on the M1's LAN.
- **Registering with an M1 uses its tuner slots**, and they stay taken while the pod is registered. Don't run a second bridge against the same M1 at the same time.
- **Use arm64 or amd64 images to match your nodes.** The published image is multi-arch (amd64 and arm64). If you build it yourself on a different architecture, use `docker build --platform linux/arm64 .`. Without a registry, copy it to the node with `docker save`, then `k3s ctr images import`, and set `imagePullPolicy: Never`.
- **Port 80 is probably taken on k3s.** The bundled Traefik/ServiceLB claims `:80` on every node, so a `hostNetwork` pod can bind it but never gets the traffic. Set `[network] http_port = 8080` (the example does) and point the probes at it. HDHomeRun clients that read the base URL from `discover.json` cope. Clients that assume `:80` may not.
- Keep the web UI token in a Secret (`ZUI_TOKEN`), never in the ConfigMap. The config file goes in a ConfigMap mounted at `/config`.
- Don't set `allowPrivilegeEscalation: false`. It disables the file capability that lets the non-root user bind low ports.

A ready-to-edit Deployment, ConfigMap and Secret example is in [`docs/examples/k8s-hostnetwork.yaml`](docs/examples/k8s-hostnetwork.yaml). It uses `192.0.2.x` placeholders. Replace them with your M1's address and your node's IP.

**What has been tested:** only a first stage on a k3s cluster of arm64 nodes: the image builds for arm64, starts as non-root on a `hostNetwork` pod, passes its `/healthz` probes, and serves the web UI with and without a token. It was pointed at an unreachable placeholder M1.

**Registration from a cluster node is UNTESTED, and the first real-hardware attempt failed.** A second stage ran `discover --apply` from a node against a real M1. The announce was sent, but the M1's `api6` join for the new device ID answered `NOT_FOUND` every time (7 tries), so the M1 never accepted the device. Earlier hand-run experiments from a Mac did register fresh IDs, so the method itself can work. The cause is not confirmed yet. **`discover --apply` has not been validated against real hardware**, so don't rely on it, and treat Kubernetes as "the image runs", not "tuning works".

Other things to know:

- **With an unreachable M1, `discover.json` doesn't answer** (it waits for the lineup), so a bridge whose M1 is down looks offline to apps. A shorter timeout is a planned follow-up.
- **Announcing from a cluster node advertises a second HDHomeRun on your LAN.** SSDP (UPnP) announcements come from the node's IP, so apps can briefly see an extra device at the node's address. For test runs, turn them off with `[network] ssdp = false` (or `ZB_SSDP=0`); then add the bridge to apps by IP.

## Ops

```sh
sudo ops/install-facade-launchd.sh     # macOS LaunchDaemon, survives reboot
```

It resolves paths from the repo directory, so it works from any clone. It
uses `venv/bin/python` if that exists, otherwise `python3` (override with
`PYTHON=/path/to/python`). The launchd installer renders
`ops/com.zapperbridge.facade.plist`, replacing `@INSTALL_DIR@` and `@PYTHON@`,
and writes it to `/Library/LaunchDaemons/`. Logs land in the repo directory
(`HDHR_LOG` to override).

## Layout

- `hdhr_emulator.py` — HDHR facade (entry point)
- `brptune.py` — BRP protocol client + MPEG-TS re-muxer
- `brp_probe.py` — BRP framing helpers (`pack`, `unpack_header`, `brp_tcp`)
- `tsparse.py` — MPEG-TS/PSI parser used by the diagnostics
- `tsinterleave.py` — merge per-PID TS packets by media time (A/V alignment)
- `tspacer.py` — PCR-paced release + jitter buffer for the raw-TS stream
- `ops/` — launchd install (macOS)
- `docs/` — Docker, web UI, logging, lineup and codec notes
- Codec info (`VideoCodec`/`AudioCodec`) is learned per channel from streams you tune and cached locally; nothing is shipped. See [`docs/CODECS.md`](docs/CODECS.md).

## Limitations

- Four tuners total: three shared BRP leases (concurrent) plus the M1's
  own tuner, which is fixed-channel and cannot be retuned over BRP.
- The BRP pool is global and device-agnostic: you cannot ask for a specific
  instance, and instances 1 and 2 are never grantable.
- Guide data comes from the M1's api8/api12 lineup; the facade serves the HDHR
  lineup, not a full EPG.
- Requires the host to be reachable by the M1 on TCP `31061`.
- The M1 pushes back to the source address of the BRP connection, so on a
  multi-homed host make sure that address is one the M1 can route to.

*Interoperability research for hardware you own. Keep the resulting streams to
yourself.*

## Tests

Standard library only (pytest optional):

```sh
python3 -m unittest discover -s tests     # whole suite (there is no CI; run tests locally)
python3 tests/test_multi_m1.py             # or any single file
```

Fixtures in `tests/fixtures/` are sample captures (api8 lists, short TS
snippets and control frames) with identifiers replaced; announce captures
are synthesised at test time. The tests never contact an M1 or bind the
service ports.

## Docker

Details: [`docs/DOCKER.md`](docs/DOCKER.md), `docker-compose.yml`,
`zapperbridge.example.toml`. Validate a config with
`python3 docker/entrypoint.py check` (set `ZB_CONFIG`). Image: `docker pull sunnyside1/zapperbridge:v0.1.0`
(Docker Hub; images are published manually, see [`docs/RELEASING.md`](docs/RELEASING.md));
or build it yourself with `docker build -t zapperbridge .`.

## License

[MIT](LICENSE) © zapperbridge contributors. No warranty. This project is
unaffiliated with ZapperBox or SiliconDust; see the disclaimer at the top.
