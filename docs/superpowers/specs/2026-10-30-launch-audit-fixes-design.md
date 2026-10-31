# Launch audit fixes: design

30 Oct 2026, branch `whole-trip-checks`. The source is the **Jev Verifier Launch Audit**
(https://claude.ai/artifact/KfJFPiQGmtNdmTzYtiX4eQ): 45 gaps found in the review of `proof_check/`,
`verifier/`, `tools/test_page/` and `deploy/`, plus one added in planning (H1). The user decided each gap
in conversation. This document records those decisions. Calls made by Claude, not the user, are marked
*(Claude's call)*.

## Intent

The verifier is about to take real gw_sales disposals on the dev server. The audit found gaps that give
wrong verdicts, crash on ordinary uploads, pay Jev repeatedly for the same answers, or leave a disposal
unchecked without anyone being told. The aim is to close those gaps without making the checks noisier on
genuine trips: every rule change was tested against the hand-read 19 and 23 Oct trips (saved answers, no
API cost), and changes that would add flags without catching anything real were dropped.

## Goals

- No verdict rests on a reading Jev wasn't sure of, where that changes the result (V2, T1, R3, R4).
- An ordinary upload (half-uploaded JPEG, very large photo, real HEIC, odd box from Jev, a photo uploaded
  as a bill) never crashes a disposal (I1–I4, I6, H1).
- A disposal is never left without a result, and never paid for twice for the same answers (V3–V5, I5).
- Comparisons with other disposals use their current version (V1, L2).
- Hardening of the deployment and the laptop tools (group 2, L-items).

## Non-goals and anti-goals

- **No email or other alert channel.** ALERTs stay as log lines (`sudo docker compose logs -f web worker`).
  The user considered email and dropped it.
- **No longer outage wait.** When Jev is down, retries stay at today's 6 tries over ~25 minutes. The
  6-hour wait with a probe was considered and dropped ("keep it simple").
- **No changes to `question_version` or the tools' cache keys.** Saved answers in `eval/answers-*.json`
  must stay usable (L5 and L9 are left as they are for this reason).
- **No new Jev questions.** The rule changes (R3, R4, R7) are code over existing answers. `QUESTIONS` is
  unchanged, so `examples/playground/*-questions.json` needs no regenerating.
- Anti-goal: a fix that turns genuine trips into "please check" without catching real problems. R2's fix
  would have turned 11 of 35 correct plant passes into "please check" on 19 Oct and caught nothing, so it
  was dropped.
- Nothing is rejected automatically. That stays: checks flag, and a person decides each part.

## Constraints (from CLAUDE.md, restated where they bite)

- Every check is in a part (`PART_OF`). A flagged check's `detail` is one plain sentence saying what is
  wrong or unclear, in which image, and what to look at. Checks flagged for the same reason share a `cause`.
- Thresholds stay in code (`CONFIDENT`, `YES`, `NO`, `NOUL_THRESHOLDS`). They are still placeholders.
- Table changes are new numbered files in `verifier/migrations/` (next: `004`), never edits.
- The API key is never printed, logged or written anywhere. It goes straight to `TypeSafeClient`.
- Use the `typesafe-ai` skill and read the live docs at https://docs.typesafe.ai before changing how Jev is
  called or how its errors are handled (V5, I5, I6). The SDK is `typesafe-sdk` 0.7.5. Its error classes
  are in `typesafe_sdk/_core/errors.py`, and its default `RetryPolicy` is `max_retries=2`.
- No real Jev requests without asking the user and giving an estimated cost first. Offline tests (fake Jev,
  moto, dev Postgres on 5433) are free.
- Run `.venv/bin/python -m unittest discover -s tests -v` after every change. Each fix comes with a test
  that fails before it.
- Update README.md and CLAUDE.md where they describe a behaviour that changes.
- Don't commit. The user commits.

## Decisions by gap

### Group 1: fix before launch

**V1: compare with each disposal's latest version.** `database.others` takes each disposal's latest
request first (by `received_at`, over all its requests), then filters on vehicle, trip day ±1 and an
analysis being present. A disposal whose latest request has no analysis yet (still being checked, or it
ended in `error`) is left out, since its earlier photos may have changed (Claude's call, confirmed by the user). One vehicle's
checks run one at a time, so one still being checked compares itself with this one when it runs. Update
the CLAUDE.md line about "latest kept analysis".

**V2: overlap only from sure photo stamps.** `busy_time` gets the option to use only stamps at or above
`CONFIDENT`. `compare_disposals.overlap` fails (problem found) only when the windows overlap by more than
the slack using sure stamps on both sides. If they overlap only when unsure stamps are counted, it asks a
person ("please check"), with the certainty and the images whose stamps were unsure. Same for
`overlap_checks` in the tools.

**T1: a screen-photo threshold of its own for bills.** `NOUL_THRESHOLDS` is keyed by `(slot, question)`.
`("photo", "screen_photo")` keeps its measured `(0.8, 0.6)`. The user chose `("bill", "screen_photo")` =
`(0.8, 0.4)`: on the 180 saved bills of 19 and 23 Oct, all taken directly, the highest score was 0.33, so
this adds no flags on those days, while the audit's case (a bill at 0.60) now asks a person instead of
passing. Write the measurement in a comment beside it, as for photos. `noul_status` takes the slot.

**V3: a check that crashed outside its try block is picked up again.** `lost()` counts a `failed` verify
job as lost when its request still has no result, so the sweep queues it again and logs an ALERT. It must
not loop endlessly on an error that repeats: cap the requeues per request (builder picks the cap and where
to keep the count). After the cap it tries once to save and send an `outcome: error` result, so gw_sales
still hears about the disposal; if even that fails (the database is the problem), it logs an ALERT and
leaves it for the daily report's count *(Claude's call)*. `day_report` counts requests with no result
(`requestsUnchecked`), and the daily ALERT fires when it isn't 0.

**V4: a send that crashed is picked up again.** Likewise, a `failed` send job whose result is neither sent
nor given up is lost, and is queued again with an ALERT. gw_sales answers a repeated requestId with 200, so
resending is safe. Same cap idea as V3; after the cap the result is marked given up (`gave_up`, which the
daily report already counts) with an ALERT *(Claude's call)*.

**V5: retry only what can change, and never pay twice.**
- `VERIFY_RETRY` retries only transient errors: Jev 5xx, 429 or 529, Jev timeouts and connection errors,
  S3/botocore network errors, and database connection errors. Anything else (our own bugs: `KeyError`,
  `ValueError`, …) fails at once and is sent as `outcome: error` with an ALERT.
- Retry timing stays as today: waits of 1, 2, 4, 8, 10 min, 6 tries in ~25 min.
- Jev answers are saved in Postgres as soon as each image is answered (new migration
  `004_jev_answers.sql`) *(Claude's call)*. A retry of the same request uses them, so only images not yet
  answered are asked again. It plugs into `analyze_trip(cache=...)` through a mapping whose writes reach
  Postgres straight away, so a crash halfway keeps what was answered. The verifier runs `analyze_trip` in a
  thread, so the mapping has to bridge to the async pool, or use its own small sync connection (builder's
  choice). Keyed by requestId and `answer_key`. Kept only for retries of one request: other requests and
  edits are asked afresh (models are pinned in `verifier/standards.py`).

**I5 (decided with V5): what a Jev failure on one image does.**

| Jev's answer for an image | What happens |
|---|---|
| 5xx, 429, 529, timeout, no connection | retried with the whole job as above (only unanswered images are asked again) |
| 400 or 422: Jev refuses *this image* | that image gets "please check" at once (`image_unavailable`, "Jev couldn't read …"), no retries |
| 401 or 403: our key refused | ALERT at once, `outcome: error`, no retries |
| still failing after the last try, other images answered | **partial result** (user chose B): all other checks run, the failed image gets `image_unavailable` "please check" |
| still failing after the last try, *no* image answered (outage) | `outcome: error` + ALERT, as today |
| 400 or 422 for *every* image of the disposal | `outcome: error` + ALERT: almost surely our own fault (a question or image rule), not the photos *(Claude's call)* |
| anything else (404 such as a withdrawn pinned model, 413, `TypeSafeAPIResponseValidationError`, our own bugs) | fails at once: `outcome: error` + ALERT, no retries (V5) |

So `analyze_trip` must be able to report per-image failures instead of raising (an image result marked as
not answered, with why). The verifier decides from those whether to raise for a retry (not the last try),
send a partial result (last try, something answered), or send `error` (nothing answered).
The CLI, folder runner and test page show a not-answered image as "please check" *(Claude's call)*.
The trip can never pass this way: the checks that need the missing image ask a person.

**I1: half-uploaded JPEG.** `s3_photos.problem` fully decodes the image (`load()`, not only `verify()`), so
a truncated file is reported as "isn't an image that can be read" (`image_unavailable`). `load_bytes` in
the tools raises a clear error, caught where an unreadable file is already handled.

**I2: the same file as a photo and as a bill.** Duplicates are matched by `(md5, slot)`, so a file uploaded
as a bill is always asked the bill questions even if it was also a photo. Duplicates are tracked by job
index, not name (**L6**).

**I3 and L8: very large photos.** Photos over ~50 MP are decoded at reduced size (JPEG `draft()`, or a
resize to a long side of about 4000 px) before anything else *(Claude's call: ~50 MP and ~4000 px)*.
`DecompressionBombError` is caught with `OSError` everywhere an unreadable image is handled (verifier
`problem()`, `load_bytes`, `ImageIndex.of_folders`). Make sure the GPS-line cut (`cut_line`) still gets
enough resolution: 4000 px is the size of today's phone photos, which read well.

**I4: flat or upside-down boxes from Jev.** `fit` never rounds a side to 0 (`max(1, round(...))`),
`cut_line` and `cut_slip` order the edges with min/max, and a box too small to read falls back to the whole
image.

**I6: an answer missing from Jev's response.** It is treated as unsure: the check that needs it asks a
person, instead of a `KeyError`.

**H1: real HEIC photos (added in planning).** Add `pillow-heif` to `requirements.txt` (so it is in the
server image too) and register it in `proof_check/images.py`. A real HEIC (for example an iPhone photo) is
then opened, turned upright by EXIF and sent to Jev as JPEG like any other image. Test with a small real
HEIC file made in the test.

### Group 2: security and deployment

- **S1:** refuse a request body over 1 MB in `api.receive` before the signature check, using Content-Length
  and a bounded read *(Claude's call: 1 MB; real requests are a few KB)*. The proxy example (S3) also sets it.
- **S2:** `Settings.from_env` refuses a `GW_SALES_URL` that isn't `https://`, except `http://localhost` and
  `http://127.0.0.1` for tests.
- **S3:** add `deploy/Caddyfile.example`, forwarding only `/v1/verifications` to `127.0.0.1:8080` with the
  1 MB limit. Mention it in the README deploy steps.
- **S4:** an `s3Key` outside `disposal/` and `collection-logs/` is not fetched and is flagged as unavailable
  ("isn't one of gw_sales' photo folders"). Add `deploy/s3-read-policy.example.json` (GetObject on those two
  prefixes only). The user checks the real policy matches.
- **S5:** the test page's `POST /api/check` requires `Content-Type: application/json` and a `Host` of
  `localhost` or `127.0.0.1` with its port.
- **S6:** a non-ASCII signature header is a bad signature (401), not a 500.
- **S7:** the webhook and callback secrets must be at least 32 characters. Deploy note for the user: if the
  dev server's current secrets are shorter, the service will refuse to start, so agree new ones with
  gw_sales first (D3's `…_NEXT` secret makes the switch without downtime).
- **D1:** a nightly `pg_dump` to a folder on the server, keeping 14 days, with restore steps in the README
  *(Claude's call)*. Copying the dumps off the server is left to the user for later. The builder picks
  the mechanism (a compose service or a host cron line).
- **D2:** the app connects as its own non-superuser role that owns the verifier's tables. The README
  generates passwords with `openssl rand -hex 32`. The dev database can be recreated (it holds no real
  data yet).
- **D3:** accept an optional second webhook secret (`…_NEXT`) while switching.
- **D4:** the worker's sweep (every minute) writes a heartbeat to Postgres. The web process checks it on its
  own timer (every minute) and logs an ALERT when the worker has been silent for over 5 minutes, at most
  once every 30 minutes while it stays silent *(Claude's call)*. The daily report can't do this, since it
  runs on the worker; `/ready` stays the database check, since the proxy (S3) doesn't expose it. No extra
  watchdog container.

### Group 3: rules

- **R1:** dropped. Loading photos keep no earliest time.
- **R2:** left as is. *Evidence (19 Oct, saved answers):* 32 unloading photos had an unsure two-decimal
  reading but a sure four-decimal pick. All 77 sure picks matched the hand-read positions. Step 1 hesitates
  mostly between 44 and 40 on the shrunken photo, and the pick resolves it on the full-resolution cut-out.
- **R3: added.** When `gross_vs_tare` answers `tare_heavier` at 0.5 or more but below `CONFIDENT`, add the
  `gross_vs_tare` check as "please check" ("Only 65% sure: the tare weight is larger than the gross weight.
  Check the weights on the slip."). Unsure `gross_heavier` and unsure `not_shown` stay silent, as now.
  *19 Oct:* no such slip (0 extra flags); the one sure `tare_heavier` (171493) was right.
- **R4: added.** `time_in_tare` and `unloading_day` no longer pass below `CONFIDENT`: they ask a person,
  sharing the `cause` of the check that already asks about the same reading (the weighing times'
  `weigh_times`, or the unloading photo times'), so the page shows one flag. *19 Oct:* no such pass.
- **R5:** kept as is. In 101 hand-read trips no Time In/Out slip was gross first and no trip crossed
  midnight. The one `time_in_tare` problem (171595, weighings 9 seconds apart, photos away from the plant)
  was a real problem.
- **R6:** left as is. *19 Oct:* 175 photo stamp hours, 53 of them afternoon, were all read right and sure.
- **R7: added.** Move `same_file` (one file as both a loading and an unloading photo is a problem, real
  part) from `verifier/compare_disposals.py` into `proof_check` (`trip_checks`), so the CLI, folder runner
  and test page catch it too. The verifier then gets it from `trip_checks`; remove its own call so it isn't
  shown twice. The wording says "loading (collection) photo" *(Claude's call)*. `PART_OF` already has
  `same_file`.
- **Q1:** no change. The user says a collection log can't be attached to two disposals, and a disposal
  can't be deleted or re-entered.

### Group 4: lower priority

- **L1:** a gw_sales time without a time zone is read as IST (record 78050's were local times) and logs a
  warning *(Claude's call)*.
- **L2 (user confirmed):** keep comparing disposals (and taking the vehicle lock) by the cleaned-up number, and
  make `normalize_vehicle_number` clean up more: a trailing suffix after a full number (`KL05AF9341/T`,
  `KL05AF9341 (hired)` → `KL05AF9341`) and a one-digit district (`KL 5 AF 9341` → `KL05AF9341`, so it matches
  `KL05AF9341`). Tested 30 Oct: today the first two give None and the third gives `KL5AF9341`. A number that
  still can't be cleaned up gets a "please check" (real part) saying the disposal wasn't compared with other
  disposals. The clean-up is also what the tools and the verifier send Jev as the typed number and put in
  `answer_key`, so keep the change to these new forms and check the cached 19 and 23 Oct runs still find
  every saved answer. The vehicle id isn't used.
- **L3:** photo and bill stamps carry no year. Take it from the trip's date (the verifier: the disposal's
  IST trip date; the tools: the folder date or today), with a stamp in December on a January trip in the
  year before, and the other way round. That fixes `too_late`, `busy_time` across New Year, 28 Feb → 1 Mar,
  and an old stored tare being labelled gross first.
- **L4:** `parse_limits` keeps a typed `0` (`"" if v is None else str(v).strip()`).
- **L5, L9:** left as is, so saved answers stay usable.
- **L6:** with I2.
- **L7:** 16-bit (`I;16`, `I`) images are scaled to 8 bits before converting to RGB.
- **L10:** `database.waiting` only looks at the last 7 days, with an index on `received_at` (in migration
  `004`). Older ones are covered by V3's report count.
- **L11:** covered by V5 and I5.
- **L12:** `parse_quantity` refuses a decimal comma (`2570,5`), `parse_plant` refuses a latitude/longitude
  outside India's range (catching swapped values), and `overlap_checks` uses the run's `clock_slack`.
- **L13:** any 2xx from gw_sales counts as delivered.
- **L14:** the test page answers a bad request (NUL in the trip id, truncated image, a non-string field,
  negative Content-Length) with a 400 and a message instead of hanging.

## Left to the builder

- The order of the work. A sensible order is the crash fixes (I1–I4, I6, H1) first, then V5 with I5 since
  the others lean on them, then V1–V4, T1, then the rules, group 2 and group 4.
- How the V5 answer store bridges the worker thread and Postgres, and the shape of table `004`.
- The requeue cap and its bookkeeping for V3 and V4.
- Exact wording of new flag details, within the CLAUDE.md rules.
- Where the D1 backup job runs.

## Done when

- Every decision above is built with a test that failed before it, and the full suite passes.
- The cached 19 Oct run (`.venv/bin/python -m tools.check_trip_folders trips/inward-2026-10-19 --cached
  --plant "11.4404, 75.9420"`) gives the same verdicts as before, except where a decision above says it
  changes (expected: none on that day). It is free.
- README.md and CLAUDE.md describe the new behaviour (retries, partial results, HEIC, same_file in
  `trip_checks`, vehicle id comparisons, backups, the example deploy files).
