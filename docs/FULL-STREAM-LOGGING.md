# Full-stream logging (`ZLOG_DIAG`)

Log-only. Output bytes, pacing, locking and the M1 protocol are untouched;
`ZLOG_DIAG=0` restores the previous log lines exactly. Every line carries
`stream=<chan_id> seq=<n>`; `seq` is the per-process stream sequence id printed
once in `stream client seq=N ...` (client ip:port, UA, path without query).
`tune_seq` on the M1-side lines ties a stream to its tune (`stream bind`).

## Lines
| line | when |
|---|---|
| `stream client seq=` / `stream bind seq= tune_seq=` | once per stream |
| `ts psi ... program= pmt_pid= pcr_pid= es=[pid:type:codec:kind]`, `ts audio_codec` | start; `ts psi_change`, `ts pat_change`, `ts audio_codec_change` on any change |
| `ts sec` | every second while live: video/audio packets+bytes, PES starts, last video/audio PTS (ms), A/V skew (audio-video), PCR-PTS offset and drift, PCR discontinuities, CC errors (video/audio/other), duplicates, TEI, scrambling, null, adaptation-field stuffing (approx.), gap counts |
| `ts gap kind=audio/video wall_ms= pts_ms= thr_ms=` | audio or video PES gap above `ZGAP_MS` (100 ms; raised to 2.5x the stream's nominal PES period). `wall_ms` = arrival gap at the writer, `pts_ms` = media-time gap |
| `ts stall` | no audio/video PES for >=1 s (once per stall) |
| `ts audio_pts_backwards`, `ts pcr_discontinuity`, `ts cc_error` | events (rate limited) |
| `ts summary` | stream end: audio/video totals, CC errors, max A/V skew, audio/video gap count and max, PCR drift, codecs |
| `ts in_sec` / `ts in_summary` | M1 side per second: frames/bytes per leg kind (v/a/c), first/last frame `kind:sid`, longest gap between frames, open push connections |
| `ts ctrl_leg`, `ts ctrl_leg_error` | control leg contents / JSON failures |
| `ts push_conn_open/close`, `push conn unbound`, `tune claim` | per-leg push connections and why they closed; legs no session claimed |
| `wgap ...` | unchanged write-gap logging |

`sid` is the 32-bit value at bytes 28-31 of the 72-byte push header (equal on a
tune's video and audio legs, 0 on the control leg); `ts push_sid_change` flags a
second sid on one session.

## Limits
Event lines: 10/s and `ZTS_LINE_BUDGET` (300) per stream; `ts sec` is 1 line/s.
Analyzer cost is measured in thread CPU (`an_us` in `ts sec`, `an_cpu_ms` in the
summary; ~1% of a core at 18 Mbps); above `ZTS_BUDGET` (20%) for 2 s it switches
itself off for that stream (`ts analyzer_off`). `ZLOG_TS=0` disables only the
TS analysis.

## Optional
* `ZCAPTURE_SECONDS=N` (default 0 = off): first N seconds of the *output* TS to
  `ZCAPTURE_DIR` (`/tmp/zapperbridge/captures`) as `<YYYYmmdd-HHMMSS>_<guide>.ts`;
  `ZCAPTURE_FILE_MB` (25) per file, `ZCAPTURE_MAX_MB` (50) total - the oldest
  files with that naming are deleted first; skipped with <1 GB free.
* `ZDIAG_JSONL=/path`: one `stream_client` and one `stream_summary` JSON line
  per stream (5 MB, one rotation). The `ZREQLOG` request log lives on another
  branch (not in this base).
