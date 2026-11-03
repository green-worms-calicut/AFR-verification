# Double-entry checks: design

3 Nov 2026. For every trip, not only Verra project trips (the user's answer). All checks run in the verifier
(`verifier/`), are in the **real** part, and need no new Jev questions, so they add no Jev cost.

## Why

A load is sometimes entered twice in gw_sales. The user has seen the second entry reuse the first one's weigh bill,
collection photos or unloading photos, and its quantity. Sometimes the very same file is uploaded again, and
sometimes a new photo of the same slip is taken. Today the verifier catches only part of this:

- `reused` (`verifier/compare_disposals.py`) finds the same file, or a look-alike, but only in disposals of the
  same vehicle with trip dates from the day before to the day after. A copy from an older disposal, or from another
  vehicle's disposal, isn't named as a copy. The plate check only says "the plate doesn't match", and it misses a
  copied photo with no plate in it, because one clear matching plate per kind of photo is enough.
- A new photo of the same slip has different fingerprints, so `reused` doesn't see it at all.
- Within one disposal, nothing checks whether two collection logs share a photo. When the same `s3Key` is listed
  under two logs, the verifier keeps it once and ties it to the last log only (`entry_checks.owners`,
  `disposal_check.py`, where evidence is de-duplicated per kind), so nobody sees it.
- Nothing compares collection logs across disposals, so the same pickup can be counted twice.

A disposal whose check ended in an error has no saved analysis, so it is never compared with anything.

## Goals

1. **`same_weighing`**: a new photo of the same slip is found.
2. **`reused` over all history**: a reused image is found in any earlier disposal, of any vehicle, on any date.
3. **A disposal that ended in an error is still compared** by its images. This was the builder's call, recommended
   in the conversation and not objected to.
4. **`log_same_photo`**: one photo in two collection logs of a disposal is found. The user: each collection has its
   own photo, and two places each have a photo of their own area.
5. **`log_reused`** and **`log_repeated`**: the same pickup counted twice is found, both as the same collection log
   id in two disposals and as a second log with a different id that says the same thing.

## Non-goals

- Old gw_sales disposals from before the verifier aren't fingerprinted (the user: not needed). Only disposals the
  verifier has checked are compared.
- When disposal A is edited, the disposals already flagged against it aren't rechecked. Their flags stay until a
  person reviews them or they are edited themselves (the user's choice). Nothing is sent to gw_sales by itself.
- The first entry isn't flagged when the second is found. The second's flag names the first (`related`).
- Deleted disposals aren't handled: the verifier doesn't hear of deletions.
- `same_weighing`, `overlap` and `same_moment` don't widen: they stay at the same vehicle, from the day before to
  the day after. A slip entered under another vehicle is already flagged by `bill_number`.

## Anti-goals

- No new Jev questions and no extra Jev cost. Everything comes from fingerprints, saved readings and what gw_sales
  sent.
- Nothing is rejected automatically. As everywhere: a sure finding is "problem found", an unsure one "please check".
- A disposal is never compared with its own earlier requests: those are its edits. Only each other disposal's
  latest request counts, so an image or slip dropped in an edit is forgotten, and one kept is still found.
- No false "same weighing" from the morning tare, which is often printed on every slip of the day.

## The checks

Every flagged check follows CLAUDE.md: its `detail` is one plain sentence saying what is wrong, in which image or
log, and what to look at; it gives `images`; across disposals it sets `related` to the other disposals; checks
flagged for the same reason share a `cause`. Each new key goes in `PART_OF` as `"real"`. Severity follows from the
part as now (`disposal_check.SEVERITY`): a problem is `high`, a please-check `recheck`.

### 1. `same_weighing`: the same weighing in two disposals

- Compared with: the same disposals as `overlap` and `same_moment` (`database.others`: the same vehicle, trip dates
  from the day before to the day after, each one's latest analysed request, never this disposal).
- The gross weighing moment of this disposal's final slip, against the gross weighing moment of each other
  disposal's final slip, worked out from saved readings as the weighing order is (`proof_check.weighings`, or the
  saved `timeline`; the builder picks whichever holds the certainty and gives the same moment).
- Same date and within 2 minutes (builder's call: allows for Jev misreading a minute; two real trips of one vehicle
  can't be weighed loaded that close together) → **problem found** when both moments are sure (`CONFIDENT`), else
  **please check**, saying which time was unsure and how sure Jev was.
- Example: "weighbill2.jpg shows the gross weighing at 07:21 on 30/10, the same as disposal 64188's slip
  weighbill1.jpg. One weighing can't be proof for two disposals."
- When both disposals typed the same quantity and both slips' `net_weight` answer is `same`, the detail adds "and
  the same net weight, 1090 kg". That is evidence only, never a check of its own. Jev can't transcribe a weight, so
  this is the only way to compare net weights.
- Tare is never used. A gross time that can't be read gives no flag here: the weighing checks already ask about it.

### 2. `reused` over all history

- New table `image_print` (a new numbered migration, `008_…`): one row per image per request, holding the request
  id, disposal id, vehicle number, kind, image name (`s3Key`), md5 and the 64-bit look-alike hash.
- Rows are written as each request's images are fingerprinted, before Jev is asked, so a request whose check later
  ends in an error still has them (goal 3). The builder chooses how, much as `jev_answer` is written as each image is
  answered.
- The migration fills the table from every saved `verification_result.analysis` (`images.*[].prints`), at no cost.
- `reused` looks up this disposal's images in `image_print` against each other disposal's **latest** request only
  (analysed, ended in an error, or still being checked), of any vehicle and any date. Fingerprints and logs don't
  change while a request is checked, so one still being checked counts too, unlike for the analysis-based checks.
  Builder's call, to close a gap the reviewer found: checks are locked per vehicle (`tasks.queue`), so two
  duplicates under different vehicles can run at once and, skipping each other, neither would be flagged. Each check
  writes its own `image_print` rows (and its request is already stored with its logs) **before** it looks up the
  others, both committed, so whichever of two concurrent checks looks second finds the first.
  - same md5 → **problem found**, naming the other disposal, its vehicle and date;
  - look-alike (`LOOKS_ALIKE` = 3 bits or fewer differ, not `FEATURELESS`) → **please check**: "Compare them."
    **At first only as now** (same vehicle, trip dates a day either side). The user's choice: before look-alikes are
    looked for over all history, measure how many false matches they give. A step of this work runs the look-alike
    test over every pair of saved fingerprints (the dev database's `image_print`, and the trip folders under
    `trips/`, which are hand-read), in code with no Jev cost, and reports to the user the matches outside today's
    window, each with both images, so the user can say which are real copies. The user then decides whether
    look-alikes go to all history, a wider window, or a tighter threshold; that change is not part of this build.
- Indexes: md5 for exact matches. Look-alikes keep today's narrow window for now (below), so no full-history scan is
  needed yet.
- Everything else about `reused` stays: its place first in `compare_disposals.checks`, each other disposal flagged
  once by the first check that finds it, and the wording.

### 3. `log_same_photo`: one photo in two collection logs (within one disposal)

- The verifier records every collection log an `s3Key` is listed under, instead of only the last (`owners`). The
  image is still downloaded and asked about once.
- Same `s3Key`, or same md5, under two or more logs → **problem found**: "collection1.jpg is the photo of both
  collection log 45001 (ABC Hotel, Main Kitchen) and 45002 (XYZ School, Main Kitchen). Each pickup needs its own
  photo."
- Look-alike under two logs → **please check**.
- The same file twice within one log → **please check** (builder's guess: padding, not fraud).
- In `entry_checks`, next to `collection_has_photos`.

### 4. `log_reused` and `log_repeated`: the same pickup counted twice

Collection logs come in the request (`reference.collectionLogs`: `id`, `client.name`, `collectionPoint.name` and
`location`, `collectionDate`, `materials[]` with `quantity` and `unit`), and every request is kept
(`verification_request.body`).

- Both log checks use each other disposal's latest request, also one still being checked or ended in an error
  (logs are in the stored request body), for the reason given under `reused`.
- **`log_reused`**: a log id that is also on another disposal's latest request, any date → **problem found**: "Collection
  log 45001 (ABC Hotel, Main Kitchen, picked up 30/10) is also on disposal 64188. One pickup can't be counted in two
  disposals." The user: one pickup is never split over two disposals, so this is always a problem, and so is one of
  its photos in two disposals (`reused`).
- **`log_repeated`**: a log with a different id but the same client, the same collection point, the same pickup day
  (IST) and the same materials with the same quantities, in this disposal or in the latest request of any other
  disposal, of any vehicle, whose trip date is from the day before to the day after this one's → **please check**.
  The day either side is where to look; the pickup day itself must be the same (builder's call, reading the user's
  two answers together). Two real pickups on one day are
  possible, but identical weights are unlikely (the user's choice of rule and of where to look).
- A log already flagged by `log_reused` isn't also flagged by `log_repeated` for the same other disposal.
- Names are compared after trimming and ignoring case. Materials are compared as a set of (material, quantity,
  unit). A log with no materials isn't compared by `log_repeated`. These are the builder's calls.
- The builder decides whether to read the logs from stored request bodies or to keep a small table of them (like
  `image_print`) for the lookup; the IST pickup day and the lookup by log id should be quick.

## Order and once-only flagging

`compare_disposals.checks` runs `reused`, then `same_weighing`, then `overlap`, then `same_moment`. Each other disposal
is flagged by the first that finds it, as now, so a re-uploaded slip file still shows as `reused` and a re-photographed
one as `same_weighing`. The log checks are separate: a log is about a pickup, not a trip, so they may name a disposal
already flagged above.

## Edits

- An edit is the same disposal id. It is never compared with its own earlier requests.
- Each other disposal counts by its latest request. An image, slip or log dropped in an edit is forgotten.
- Editing the first entry rechecks it, so it is then flagged against the second entry too.
- A disposal flagged against an edited one keeps its flag (non-goal above).

## Done when

- The new checks are in `PART_OF` (real), and the offline tests pass:
  `.venv/bin/python -m unittest discover -s tests -v`, with the dev Postgres on 5433 running for `test_verifier.py`.
- Tests cover at least these cases:
  - a re-photographed slip (problem);
  - an unsure gross time (please check);
  - two different weighings, and two slips sharing only the morning tare (both pass);
  - an exact reused image from an older disposal and from another vehicle;
  - a look-alike from an older disposal not flagged (until the user widens it);
  - two disposals of different vehicles sharing a photo, checked at the same time: the later one is flagged;
  - a reused image from a disposal whose check ended in an error;
  - an edit not flagged against itself;
  - a dropped image no longer found;
  - one key under two logs, one file under two keys in two logs, a look-alike under two logs, and the same file twice
    in one log;
  - the same log id in two disposals;
  - a repeated log with a different id;
  - two pickups the same day with different weights (pass).
- The migration is new and numbered; no applied migration is edited.
- The look-alike measurement has been reported to the user.
- README.md (the verifier's checks and the comparisons with other disposals) and CLAUDE.md (the comparisons line:
  `reused` over all history, the new keys) describe the new behaviour.

## Left to the builder

- How `image_print` rows are written early enough to survive an error, and the exact schema and indexes.
- Whether `same_weighing` recomputes `weighings` or reads the saved `timeline`.
- Whether logs are read from request bodies or a small table.
- Exact wording of each `detail`, within the CLAUDE.md rule.
