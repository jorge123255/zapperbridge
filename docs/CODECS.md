# Learned VideoCodec / AudioCodec

`/lineup.json` rows may carry `VideoCodec` and `AudioCodec` (`MPEG2`, `H264`,
`HEVC`; `AC3`, `EAC3`, `AAC`, `MPEG`, `AC4`). Nothing market-specific ships with
zapperbridge: the values are **learned from your own channels**.

## How it works

- When a client tunes a channel, the bridge reads the PMT of the stream it is
  already serving and records the codecs (stream types `0x02` MPEG-2, `0x1b`
  H.264, `0x24` HEVC; `0x81` AC-3, `0x87` E-AC-3, `0x0f`/`0x11` AAC,
  `0x03`/`0x04` MPEG audio, and `0x06` private data with AC-3 / E-AC-3 / AC-4
  descriptors).
- Learning is passive: no extra tunes, no probing, no tuner lock. It stops at
  the first PMT (or after ~20,000 packets) and any error is swallowed, so it
  cannot interrupt a stream.
- Entries are keyed by **M1 device id + guide number + GuideName**, so another
  market's `2.1`, or the same number on a second M1, is never mislabelled.
- A channel nobody has tuned yet simply has **no codec fields**. Nothing is
  guessed. ATSC 3.0 and the `hls:` row never get codec fields.
- The `/lineup.json` ETag includes a short hash of the cache, so clients refetch
  after new codecs are learned.

## Cache file

`codec_cache.json` next to the log file (`/data` in Docker; override with
`ZCODEC_CACHE=/path/file.json`). Atomic write, merged with other facade
processes' entries, at most 2000 entries (oldest dropped), files over 1 MiB or
with a bad version/format are ignored and rewritten. It is safe to delete.

`ZCODEC_LEARN=0` stops learning (an existing cache is still used).

The web UI Lineup page shows the learned codecs per channel and the
learned / not-yet-learned counts (read-only).

To warm the cache, tune each channel once (for example, open the channel in
your player). A background scan is intentionally not provided.
