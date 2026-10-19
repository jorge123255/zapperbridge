# ATSC 3.0 (experimental)

Two separate things, both for testing.

## 999.1 (the M1's own tuner) on a 3.0 channel

When the M1 itself is tuned to an ATSC 3.0 channel, its HLS window
(`http://<m1>:8076/atsc3/1/`) serves fragmented MP4: HEVC video, AC-4 audio
(one rendition per language) and TTML captions. Only slot 1 exists, and it
always shows what the M1 is watching, so 999.1 stays one fixed channel.

The bridge detects this (`CODECS="hvc1..."`/`ac-4` in `master.m3u8`, or an
`EXT-X-MAP` init in `video.m3u8`) and remuxes video plus the default audio
rendition into one MPEG-TS. Nothing is decoded or re-encoded:

* HEVC: stream_type `0x24`, Annex-B, VPS/SPS/PPS repeated before keyframes.
* AC-4: stream_type `0x06` (private PES) with a registration descriptor
  `AC-4` and the DVB AC-4 descriptor (`0x7F`/`0x15`). Each frame is wrapped
  in an AC-4 sync frame (`0xAC40`); `ZB_AC4_SYNC=0` sends raw frames.
* Captions are not carried.

The ATSC 1.0 path (MPEG-TS segments) is unchanged. The 999.1 row reports
`ATSC3`/`atsc3` once a 3.0 window has been seen. Whether a client can play
AC-4 is up to the client.

The image has no ffmpeg (stdlib-only), so this is a small built-in remuxer
(`fmp4ts.py`). For reference, ffmpeg 7.1 `-c copy` into MPEG-TS does copy
AC-4, but as a bare private-data stream without any AC-4 descriptor, which
its own demuxer then reads back as `bin_data`.

Set `HDHR_HLS_PROTO=3` to read `/atsc3/1/`. Log lines start with `hls3`.

## BRP tunes of 3.0 channels

An M1 chan_id carries the ATSC standard in its top nibble (`1` = 1.0,
`3` = 3.0; e.g. `0x3200000000002001` is 2.1 on 3.0). For 3.0 channels the
bridge waits `ZATSC3_TUNE_WAIT` seconds (default 25, one attempt by
`ZATSC3_TUNE_TRIES`) for the api9 reply instead of 8, and logs:

* `atsc3 tune ... waiting up to 25s for api9`
* `atsc3 tune ... api9 after N s: err_name=... a0=... body=...` (or
  `no reply bytes` / `partial header`)
* `atsc3 push conn #n ... format=... first16=...` for every :31061
  connection during the tune (`data-framed` with `body=ts` or `body=fmp4`,
  or raw `ts` / `fmp4` / `other`)
* `ATSC 3.0 tune not answered by M1 within 25s` when nothing comes back
  (also in `/health` as `last_fail.detail`).

While a 3.0 tune waits it holds the BRP control lock, so other tuners'
heartbeats wait too. Test 3.0 with nothing else playing.
