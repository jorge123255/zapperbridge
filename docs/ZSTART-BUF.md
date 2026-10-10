# Opt-in startup prebuffer (`ZSTART_BUF_SEC`)

Off by default: unset leaves today's behaviour exactly (prebuf 0.8 s, max start
1.5 s, `ZPACE_PREBUF_MS` / `ZPACE_MAX_START_MS` still work).

Why: at 6:49:31 AM CT a tune showed a 994 ms write gap and one rebuffer while the
pacer held only 0.75-2 s after a slow, retried tune. The M1 pushes ~2 s bursts;
a burst that arrives late drains a 0.8 s buffer.

Setting `ZSTART_BUF_SEC=2.0` (seconds, clamped to 0.1 .. 3.0) makes the pacer
wait for 2.0 s of media (and 2.0 s of wall time since the first data) before it
releases the first byte. `max_start` is raised to value + 0.7 s so the old 1.5 s
safety net does not silently cut it short. A bad value is ignored with a note.

Cost, honest:
* tune latency grows by about (value - 0.8) s, i.e. about 1.2 s at 2.0. It can be
  longer if the source's first burst holds less media than the value.
* the same depth is used when the pacer re-buffers after a real source underrun,
  so such a stall is also longer, but repeated short rebuffers become rarer.
* it does not fix a hop that jitters after the bridge (media server -> client).

Unchanged: PCR pacing, the M1 lock/claim logic, the protocol, the max-buffer skip.
Apply by setting the env var on the facade; it needs a facade restart (not done).
Logged once at startup (`startup prebuffer: ...`) and visible per stream as
`start_delay=` in the existing close line.

Tests: `tests/test_startup_buffer.py` (env parsing, facade defaults unchanged,
simulated bursty M1 input: the default rebuffers and 2.0 s does not, byte-for-byte
the same stream, +1.2 s latency measured).
