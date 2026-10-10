# Merged lineup across several M1s (`hdhr.mode = "merged"`)

Status: **module + config plan + tests only. Off by default. Not wired into the
live facade.** Nothing in this change sends anything to an M1, binds a port or
runs by default. `per-m1` stays the default and is unchanged.

## What exists

* `lineupmerge.py` (pure Python, stdlib only, no I/O):
  * `merge(sources, assignments)` -> one row per station with a candidate M1
    list (source order). Identity = guide number + call letters. A guide number
    shared by a *different* station gets the next free minor of the same major
    (reported in `notes`), never dropped.
  * Stable guide numbers: persist the returned `assignments` (`dump_assignments`
    / `load_assignments`) and numbers survive M1s being added, removed or
    reordered; numbers of a missing M1's stations stay reserved.
  * `usable_tuners(announced, leases)` = announced - 1 (the M1 keeps one),
    capped by config `leases` and by 3. `total_tuner_count(sources, cap)` is
    the advertised TunerCount (`hdhr.tuner_cap` caps the total).
  * `Router`: per-M1 usage, `candidates(key)` (up, not benched, free tuner,
    most free first), `tune(key, tune_fn)` with failover (a raise or falsy
    return tries the next M1 and benches the failing one for `bench_sec`),
    `Busy` when nothing is free (soft-busy, never api7), `release(lease)`.
  * Hidden rows and each M1's fixed HLS window (999.x / `hls:`) are not merged.
* `zbconf.py`: `hdhr.mode = "merged"` is accepted. `check`/`plan` print a
  `MERGED HDHR` line (device id, M1 count, total TunerCount, usable per M1).
  Optional `hdhr.device_id` (8 hex). `docker/entrypoint.py run` **refuses**
  merged mode (exit 2) until the facade is wired.

## Still needed to make it live

1. A front process: one HDHR (JSON + SSDP + `/auto/v<GN>` TS port) that loads
   each M1's api8 lineup (the existing per-M1 facades can serve it as a
   backend on loopback or their own IP), calls `merge`, serves
   `lineup_rows`, and persists `assignments` next to the lineup cache.
2. `/auto/v<GN>` -> `Router.tune` with a `tune_fn` that asks the chosen M1's
   facade/BRP session to tune and returns once bytes flow; proxy that stream;
   `release` on close. Decide whether the front proxies or redirects (a proxy
   adds one hop).
3. M1 health: feed `mark_up` from announce presence / BRP errors, and refresh
   `announced` from the announce (only `announced = 4 -> 3 usable` is confirmed).
4. Guide: merge XMLTV per identity (use the first candidate that has data).
5. HDHR `TunerCount` and `/tuners.json` from `Router.tuner_count()` / snapshot;
   clients cache TunerCount, so changing it is a rescan-visible event.
6. Same-channel sharing (two clients on one channel use one M1 tuner) is not
   modelled: each stream takes a tuner.
7. Live test with two real M1s (none available here), including failover.

## Tests

`python3 tests/test_lineupmerge.py` (fake lineups only) and
`python3 tests/test_zbconf.py`.
