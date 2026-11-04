# Real-data replay: design

4 Nov 2026. Planning only, nothing built yet. A testing tool, not a check, so the Verra question doesn't change it:
it runs on every disposal, and the Verra checks switch on only where the verifier would switch them on (the
collection log's client `defaultProject`).

## Why

The webhook works, but gw_sales' dev disposals are a handful of test entries. To see how the verifier does on real
work, we check a whole real day at a time, without the webhook and without sending anything to gw_sales. Then we see
where it flags wrongly, and also where it passes something it should have flagged.

## Input

The user downloads one JSON file per day from gw_sales: a list of disposals, each the same body the webhook sends,
plus a signed URL for every image:

```json
[
  { "subject": {...}, "claims": {...}, "reference": {...},
    "evidence": [ { "category": "weighbill", "s3Key": "disposal/…/a.jpg", "url": "https://…signed…" }, ... ] },
  ...
]
```

- The images are real: the exports come from prod (the user, 4 Nov). They are read only from the signed URLs. Our read-only S3 key reaches only the dev bucket.
- The URLs expire after **100 minutes**.
- `s3Key` stays the image's name, so flags read the same as the webhook's.
- `reference.collectionLogs` must be there, with each log's `collectionDate`, `collectionPoint`, `client` (with
  `defaultProject`) and `materials`, and with the collection photos under their logs (`owner`). Without them the log
  checks (`collection_has_photos`, `collection_photo_date`, collection point, `disposal_after_collections`,
  `log_same_photo`, `log_reused`, `log_repeated`), collected vs weighed, and the Verra switch are skipped.
- The file holds real records and working URLs: it lives in `trips/gw-<date>/` (gitignored), never in chat, git or
  a published page.

**Format seen (4 Nov, a dev export of 1 Nov):** a wrapper `{exportedAt, schemaVersion: 1, filters: {startDate,
endDate, complianceStatus, limit}, signedUrlExpiresAt, count, pageInfo: {nextCursor, hasNextPage}, disposals: [...]}`.
Each disposal is the webhook body (`requestId`, `reason`, `schemaVersion`, `subject` with `dataVersion`, `claims`,
`evidence`, `reference` with `facility`, `collectionLogs`, and also `trip` and `disposedMaterials`, which no check
reads) without `callbackUrl`. Each evidence item has `signedUrl` (not `url`), `fileName`, `mimeType`, `fileSize` and
`uploadedAt`, and the collection photos are under their log's `owner`. So:

- The tool reads `signedUrlExpiresAt` first. If it has passed, it asks for a fresh export instead of trying every URL.
- `request_id` is the export's `requestId`. A made-up id is used only when one is missing.
- `signedUrl` is removed from every evidence item before the body is saved.
- An export is at most `limit` disposals (100 here). If `hasNextPage` is true, the tool says the day isn't complete,
  and `download` takes the next page's file into the same day folder. Each disposal goes in the folder of its own IST
  trip date (the verifier's `disposal().day`), so an export over several days is split by day.
- `fileName` labels images in the report. The file on disk is named after the whole s3Key (`/` as `__`), so two keys never share a file.

## The tool: `tools/replay_day.py`

Run from the project folder: `.venv/bin/python -m tools.replay_day <command> …`. Each command is a plain word, takes a
file or a day, and says whether it costs anything:

```
download <export.json>    save the export and its images (free; within 100 min of exporting)
check <day>               check the day's disposals with Jev (paid: shows the cost, asks yes or no)
recheck <day>             check the day again from saved answers, after a rule change (free)
report <day>              open the report in the browser to read and mark (free; Ctrl+C to stop)
report <day> --save       write a read-only copy to eval/replay-<day>.html instead
days                      list the saved days: disposals, images missing, checked or not, reviewed
```

A day is written as `2026-11-01`. Running the tool alone, or with `--help`, prints this list with examples. Output
is plain sentences, one line per disposal while it works, a short summary at the end, and a last line `Next: …` with
the command to run next. Errors say what went wrong and what to do (for example, "The image links expired at 15:38
IST. Export the day again from gw_sales and run: download <new file>"), without a Python traceback.

### `download` (free)

Reads the JSON and saves every image right away into `trips/gw-<date>/<disposal id>/`, under a name made from its
s3Key, along with the JSON itself. Only `https` URLs on Amazon S3 hosts (`*.amazonaws.com`) are fetched, at most 30 MB each, with the same image check as
`s3_photos.problem`. An image that fails (an expired URL, 403/404, not an image) is listed. Running `download` again
with a freshly downloaded JSON fetches only what is still missing.

### `check` and `recheck`

Checks the saved disposals in `createdAt` order with **the verifier's own code**, `disposal_check.check`. The only
difference is `fetch`, which reads the saved file for an s3Key instead of S3. An image that couldn't be downloaded
comes back as None, so it is flagged `image_unavailable` exactly as a missing S3 photo is.

- **Database:** `verifier_replay`, a separate database in the dev Postgres container `jev-verifier-db` (127.0.0.1:5433,
  Docker volume `jev-verifier-pg`), with the verifier's migrations. It is kept apart from the dev `verifier` database,
  whose gw_sales test disposals (64187–64194) would show up as false duplicates. The comparisons (`reused`,
  `same_weighing`, `overlap`, `same_moment`, `log_reused`, `log_repeated`) use `database.others`, `other_logs` and
  `PrintHistory` there. Because disposals go in time order, each is compared only with earlier ones, as happens live,
  and replaying days in a row finds duplicates across days.
- **What is saved per disposal:** the same rows the webhook makes. `verification_request.body` is the disposal's
  body with every `url` removed: the s3Key names each image, and a signed URL is a temporary pass that is useless
  after 100 minutes. The images themselves aren't stored in the database. The file for an s3Key is found on disk
  (`trips/gw-<date>/<disposal id>/`, its name made from the s3Key), so no new column is needed. `request_id` and
  `reason` come from the export; `callback_url`, which the export doesn't carry, is `replay:` (never posted). `image_print` (fingerprints) and `jev_answer` are saved by s3Key, as
  live.
- **Nothing is sent.** Each result is saved with `database.save_result`, exactly the JSON gw_sales would receive. The
  sender job is never queued, and no worker runs against this database.
- **Saved Jev answers** (`jev_answer`) make a re-check after a rule change free: `recheck` checks the day again
  from the saved answers, without new requests (its client refuses every request). A disposal needing an answer not
  saved (the questions changed) keeps its earlier result and is listed; `check <day> --all` then asks Jev only for
  what is missing. `check` alone skips disposals already checked and retries those that ended in an error.
- Jev versions and limits come from `verifier/standards.py`, as for the webhook.
- **Cost estimate:** about $0.005–0.01 per disposal (past folder runs about $0.003–0.004 per trip, plus collection
  photo positions and the weighbridge question), so $0.3–0.6 for a 60-disposal day.

### `report` (free): the report and the marks

The report has to save marks, so it is a page served on `127.0.0.1` by the tool (like `tools/test_page`), not a static
file. The Jev key isn't needed and never reaches the page. `report --save` writes a read-only copy (thumbnails inside) to
`eval/replay-<date>.html` for keeping.

## The report

1. **Day summary:** disposals passed, flagged and in error; flags by severity and by check; Jev cost; what wasn't
   checked and why (no facility location, no collection logs, images not downloaded).
2. **Accuracy by check** (from the marks): for each check, flags marked right, flags marked wrong, passes marked
   "should have flagged", and how many are unmarked. Counted over every replayed day, and over this day alone.
3. **Duplicates across disposals:** every pair a comparison linked, with both images side by side.
4. **Flagged disposals:** one card each. It shows the parts' verdicts (real, docs, verra), each flag as gw_sales would
   show it (severity, kind, message, the images it names), then every check with its status, what Jev read and how
   sure it was, and the timeline (weighings and photo times). Images open large on click.
5. **Risky passes** (below)**,** then the other passed disposals, collapsed.
6. **Errors and not checked:** disposals with `outcome: error`, and images that couldn't be downloaded or read, each
   with the reason.

## Marks

- On each **flag**: **Right** / **Wrong**, and an optional note.
- On each **passed check** (in any disposal, passed or flagged): **Should have flagged**, and a note saying what was
  missed.
- A mark can be changed or cleared.
- Marks are saved in `verifier_replay`, in a table only the replay tool uses (`replay_mark`: disposal id, check key,
  the image or log it is about, the mark, note, when). It is created by the tool, not by `verifier/migrations/`, so it
  never reaches the server's database. Marks are keyed by disposal and check, not by request, so they survive a
  re-check. After a rule change the report shows what changed: "marked wrong, now passes" (fixed) and "marked right,
  now passes" (a new miss).
- **What a mark leads to:** a wrong flag or a miss is looked into before anything changes. If Jev was sure and wrong,
  change the question or its threshold (live TypeSafe docs first). If Jev was unsure but just cleared the threshold,
  raise the threshold, after seeing in the report how many extra flags that would cause. If no check looks at the
  problem, add a check (ask "Verra or not?" first). If the image itself is unclear, a person has to look, and nothing
  changes. After a change, `recheck` re-checks the day for free and shows the effect. A missed disposal also
  goes into a hand-read truth file, so a later change can't quietly let it pass again.

## Risky-pass list

You can't look at every passed disposal every day, so passed disposals are put in an order to review:

1. **Barely passed.** A passed check whose certainty is within a margin of the threshold it had to clear (`CONFIDENT`,
   `YES` or `NO` in `proof_check/trip_rules.py`; margin 0.10 by default, `--margin` to change it). Each one shows which
   check, the value and the threshold, for example "Net weight: same, 0.84, needs 0.80". Least sure first.
2. **Random sample.** 5 other passed disposals a day by default (`--sample`), chosen with the day as the seed, so the
   list stays the same each time the page is opened and re-checks are compared on the same disposals. This catches
   mistakes Jev was confident about, which the barely-passed list never shows.
3. **Hand-read sample.** For the disposals in the sample, the user can read the plate, net weight and slip times
   themselves into `trips/gw-<date>/truth.jsonl`. The comparison already in `tools/check_trip_folders.py` (`compare`)
   is reused to show where Jev's readings differ, on passed checks too.

A flagged disposal counts as reviewed once each of its flags is marked; a passed one once it has a "should have
flagged" mark or its **Reviewed: nothing missed** button is pressed. The summary says "reviewed 18 of 60".

## Not in this tool

- No callbacks to gw_sales, no webhook, no worker.
- No change to the live verifier's checks or thresholds. Marks only provide the evidence for later decisions.
- No automatic upload of marks anywhere: they stay on the laptop.

## Tests (offline, free)

A fake day JSON with fake URLs served locally: the download (missing and expired URLs, a non-image), checks in
`createdAt` order with FakeJev and the comparisons finding a copied image across two disposals, nothing sent, marks
saved and kept across a re-check, the barely-passed margin, and the random sample staying the same for the same day.
