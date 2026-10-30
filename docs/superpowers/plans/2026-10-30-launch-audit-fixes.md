# Launch Audit Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the gaps of the Jev Verifier Launch Audit as decided in the approved design, so the verifier can take real gw_sales disposals without wrong verdicts, crashes, repeated Jev charges or unnoticed lost disposals.

**Architecture:** Fixes land where each gap lives: image handling in `proof_check/images.py`, Jev calls and per-image failures in `proof_check/ask_jev.py`, rules in `proof_check/trip_rules.py`, the service in `verifier/` (retries, saved answers, sweep, comparisons), and deploy examples in `deploy/`. New tables come as new numbered migrations `004`–`007`. Each task is test-first against the offline fakes (FakeJev, moto, dev Postgres on 5433).

**Tech Stack:** Python 3.14, Pillow + pillow-heif, typesafe-sdk 0.7.5, FastAPI, Procrastinate 3.10, psycopg 3, boto3/moto, unittest.

**Spec:** `docs/superpowers/specs/2026-10-30-launch-audit-fixes-design.md` (read it first; it holds the reasons and the 19/23 Oct numbers).

## Global Constraints

- Run everything from `/home/greenworms/Projects/jev` with `.venv/bin/python`. Full suite: `.venv/bin/python -m unittest discover -s tests -v`. Verifier tests need the dev Postgres on 127.0.0.1:5433 (`jev-verifier-db`); leave the Postgres on 5432 alone.
- **No commits.** The user commits. Each task ends with the full suite passing instead.
- **No real Jev requests** without asking the user with a cost estimate. `--cached` runs are free.
- Never look up, print or write the TypeSafe API key.
- Use the `typesafe-ai` skill and read the live docs (https://docs.typesafe.ai, append `.md`) before Tasks 4 and 5 (they change how Jev answers and errors are handled).
- Don't change `QUESTIONS`, `question_version` or `answer_key` (saved answers in `eval/answers-*.json` must stay usable). Task 15 changes `normalize_vehicle_number` only for the new forms, and checks the cached runs.
- Every new check key needs its part in `PART_OF` (`proof_check/trip_rules.py`), and its `detail` is one plain sentence: what is wrong or unclear, in which image, what to look at. Checks flagged for the same reason share a `cause`.
- Table changes are new files in `verifier/migrations/` (`004_…` to `007_…` below); never edit an applied one.
- `CONFIDENT = 0.8`, `YES, NO = 0.8, 0.2` stay as they are.
- Follow the surrounding code's style: short docstrings that say why, plain names, no new layers.
- Update README.md and CLAUDE.md in the task whose behaviour they describe.

## Review Focus

- A disposal with one image missing from S3 **and** one image Jev never answered: both get `image_unavailable`, the trip can't pass, and they aren't merged into one wrong flag (Task 7 test).
- A retry after the worker restarted mid-trip: answers saved before the crash are reused, and two writes of the same key don't fail (Task 6 test: `ON CONFLICT DO NOTHING`).
- A real iPhone HEIC with EXIF orientation 6 comes out upright, like a JPEG (Task 1 test).
- The sweep must not queue a job that is only waiting for its next retry (`todo` with a future `scheduled_at`): only `failed` jobs and missing ones are lost (Task 8 test).
- A typed vehicle number already clean (`KL05AF9341`, `TN38BX1234`, `KL57X7639`) is unchanged by the new clean-up (Task 15 test), so cached answers keep matching.

---

### Task 1: Images that can't crash a trip (I1, I3, L8, L7, H1)

**Files:**
- Modify: `proof_check/images.py` (`load_bytes`, `ImageIndex.of_folders`, constants), `verifier/s3_photos.py` (`problem`), `requirements.txt`, `proof_check/__init__.py` (re-export new names)
- Test: `tests/test_proof_check.py` (new `ImageLoadTests`), `tests/test_verifier.py` (`DisposalTests` or a new `PhotoProblemTests`)

**Interfaces:**
- Produces: `UNREADABLE = (OSError, PILImage.DecompressionBombError, SyntaxError, ValueError)` in `proof_check/images.py`: every place that treats a file as unreadable catches this tuple. `MAX_PIXELS = 50_000_000`, `REDUCED_SIDE = 4000`, and `PILImage.MAX_IMAGE_PIXELS = 250_000_000` set at import, so a 200 MP phone photo opens and is read reduced, while anything larger is unreadable. `load_bytes(data: bytes) -> PILImage.Image` unchanged signature, now: registers pillow-heif once at import (`pillow_heif.register_heif_opener()`), decodes images over `MAX_PIXELS` reduced (JPEG `draft("RGB", …)`, then `thumbnail` to `REDUCED_SIDE` on the long side), scales 16-bit modes (`I;16`, `I;16B`, `I`) to 8 bits before `convert("RGB")`, and raises on a truncated file.

- [ ] **Step 1: Write the failing tests**
  - `test_truncated_jpeg_is_unreadable`: `problem(coloured(COLOURS[0])[:-2000])` returns `"isn't an image that can be read"`; `load_bytes` of the same bytes raises one of `UNREADABLE`.
  - `test_huge_photo_is_read_reduced`: a 9000×6000 JPEG (built with `PILImage.new` + save, quality 50) → `max(load_bytes(data).size) == 4000`; `problem(data)` is None. Also patch `PILImage.MAX_IMAGE_PIXELS` low (e.g. 1_000_000) in a second assertion so a `DecompressionBombError` is raised and caught: `problem(...)` gives the unreadable message and `ImageIndex.of_folders` on a temp folder holding it skips that file instead of raising.
  - `test_16_bit_scan_keeps_its_greys`: an `I;16` image with value 32768 saved as PNG → `load_bytes(...).getpixel((0, 0))` is about `(128, 128, 128)` (±2), not `(255, 255, 255)`.
  - `test_real_heic_is_read_upright`: build a HEIC in the test with `pillow_heif.from_pillow(img).save(buffer, quality=50)` from a 200×100 image with EXIF orientation 6 → `load_bytes(...).size == (100, 200)`; `problem(...)` is None.
- [ ] **Step 2: Run them, expect failures**: `.venv/bin/python -m unittest tests.test_proof_check.ImageLoadTests -v` → FAIL/ERROR (no `UNREADABLE`, heif can't open, size 9000).
- [ ] **Step 3: Add `pillow-heif` to `requirements.txt`** (a version at least two weeks old; pin it the way the file pins Pillow) and install: `.venv/bin/pip install -r requirements.txt`.
- [ ] **Step 4: Implement** `UNREADABLE`, `MAX_PIXELS`, `REDUCED_SIDE` and the new `load_bytes` in `proof_check/images.py`; `ImageIndex.of_folders` catches `UNREADABLE`; `s3_photos.problem` opens with `load_bytes` (full decode) inside `try/except UNREADABLE` instead of `img.verify()`. Keep `IMAGE_TYPES` and its comment updated ("real HEIC is read with pillow-heif").
- [ ] **Step 5: Run the new tests, then the full suite** → all pass.
- [ ] **Step 6: Docs**: README "Image size limits" line (real HEIC now works; over ~50 MP read at 4000 px), CLAUDE.md Python line (pillow-heif).

### Task 2: Boxes from Jev that are flat, inverted or tiny (I4)

**Files:**
- Modify: `proof_check/images.py` (`fit`, `cut_line`, `cut_slip`)
- Test: `tests/test_proof_check.py` (`FitTests`, new `CutTests`)

**Interfaces:**
- Produces: `MIN_CUT = 16` (px): a cut smaller than this on either side returns the whole image. `fit` never returns a side of 0.

- [ ] **Step 1: Write the failing tests**
  - `test_fit_never_makes_a_zero_side`: `fit(PILImage.new("RGB", (4000, 1)))` has both sides ≥ 1 (today: ValueError).
  - `test_inverted_box_is_put_right`: `cut_slip(img_400x300, {"top": 0.8, "bottom": 0.2, "left": 0.9, "right": 0.1})` has the same size as the box with edges swapped.
  - `test_tiny_box_falls_back_to_the_whole_image`: `cut_slip(img, {"top": 0.5, "bottom": 0.5, "left": 0.5, "right": 0.5}).size == img.size`; same for `cut_line` with a `SimpleNamespace` box.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement**: `fit` uses `max(1, round(...))`; both cuts order edges with `min`/`max` before adding the margin, and return `img` when the cut is under `MIN_CUT` on a side.
- [ ] **Step 4: Run the new tests, then the full suite** → pass.

### Task 3: One file in two roles is asked in each slot (I2, L6)

**Files:**
- Modify: `proof_check/ask_jev.py` (`analyze_trip`: `first_of`, `unique`, `done`, `same_as`)
- Test: `tests/test_proof_check.py` (`TripTests`)

**Interfaces:**
- Produces: inside `analyze_trip`, duplicates keyed by `(md5, slot)`; the first copy tracked by job index. Image results keep `same_as` (name of the first copy in the same slot, or None).

- [ ] **Step 1: Write the failing tests**
  - `test_a_photo_uploaded_as_a_bill_is_asked_bill_questions`: `make_trip(same=(("loading", 0, "bill", 1),))`, give that colour both photo picks and bill picks (FakeJev answers by slot when the request has `weigh_bill`) → `analyze_trip` returns without `KeyError`; the bill result has `answers["weigh_bill"]`; the loading result has `answers["shows_vehicle"]`.
  - `test_same_name_and_bytes_twice_is_asked_once`: two `Upload("collection1.jpg", same_bytes)` in `loading` → `len(jev.requests)` counts one photo request for them, and the second has `same_as == "collection1.jpg"`.
- [ ] **Step 2: Run, expect failures** (KeyError 'weigh_bill'; two requests).
- [ ] **Step 3: Implement** the `(md5, slot)` key and index tracking in `analyze_trip`.
- [ ] **Step 4: Run the new tests, then the full suite** → pass.

### Task 4: An answer missing from Jev's response asks a person (I6)

Read the typesafe-ai skill and https://docs.typesafe.ai/sdk/python/api/exceptions.md first.

**Files:**
- Modify: `proof_check/ask_jev.py` (`analyze` / `result_from`)
- Test: `tests/test_proof_check.py` (`AnalyzeTests`, `TripTests`)

**Interfaces:**
- Produces: `fill_missing(slot: str, answers: dict[str, dict], asked: Iterable[str]) -> dict[str, dict]` in `proof_check/ask_jev.py`: every question asked but absent from Jev's answers gets an answer that every check reads as unsure (a yes/no at `yes = 0.5`; a Choice at confidence `0.0` on an option that check already knows, so no lookup fails). Called in `analyze` on the raw answers before they are kept or cached.

- [ ] **Step 1: Write the failing tests**
  - `test_missing_plate_answer_asks_a_person`: a FakeJev photo response without `plate_number` (add a `drop: set[str]` option to `fake_answers`/`FakeJev`) → `analyze_trip` returns; the loading plate check is `review`.
  - `test_missing_yes_no_answer_asks_a_person`: response without `screen_photo` → `loading_direct` is `review`.
- [ ] **Step 2: Run, expect `KeyError`.**
- [ ] **Step 3: Implement `fill_missing`** and call it.
- [ ] **Step 4: Run the new tests, then the full suite** → pass.

### Task 5: Jev failing on one image doesn't lose the others (I5, core in proof_check)

Read https://docs.typesafe.ai/api/errors.md and `typesafe_sdk/_core/errors.py` first.

**Files:**
- Modify: `proof_check/ask_jev.py` (`analyze_trip`, its `run`), `proof_check/trip_rules.py` (`PART_OF`, new check), `proof_check/__init__.py`, `proof_check/__main__.py`, `tools/check_trip_folders.py`, `tools/test_page/server.py` (show the new field)
- Test: `tests/test_proof_check.py` (new `JevFailureTests`)

**Interfaces:**
- Produces in `proof_check/ask_jev.py`:
  - `failure_kind(error: Exception) -> str | None`: `"retry"` for `TypeSafeInternalServerError`, `TypeSafeRateLimitError`, any `TypeSafeAPIError` with status 529, `TypeSafeAPIConnectionError` (includes timeouts); `"image"` for `TypeSafeBadRequestError`, `TypeSafeUnprocessableEntityError`; `"fatal"` for any other `TypeSafeError` (401, 403, 404, 413, `TypeSafeAPIResponseValidationError`); `None` for a non-TypeSafe exception (a bug: let it raise).
  - `class JevRefused(Exception)`: raised by `analyze_trip` for a `"fatal"` failure, and when Jev refused every image (`"image"` for all). Message names the error class and status, never the key.
  - `analyze_trip(...)` returns `trip["unanswered"]: list[dict]` = `[{"role": str, "name": str, "kind": "retry" | "image", "why": str}]`. Unanswered images are left out of `trip["images"]` before the verdict, and `trip["verdict"]` includes one `image_unanswered` check per role that has any.
- Produces in `proof_check/trip_rules.py`: `unanswered_check(role: str, names: list[str]) -> dict`, key `"image_unanswered"`, status `review`, part `docs` (add to `PART_OF`), detail like "Jev couldn't read weighbill2.jpg: read it yourself." with `images=names`.

- [ ] **Step 1: Write the failing tests** (FakeJev gets `fail: dict[colour, int]`, answering that HTTP status for that image's requests, and a SDK client built with `RetryPolicy(max_retries=0)` in these tests so they stay fast)
  - `test_one_image_failing_keeps_the_others`: bill 2 answers 500 → trip returned; `trip["unanswered"] == [{"role": "bill", "name": "weighbill2.jpg", "kind": "retry", ...}]`; loading checks still pass; the trip's verdict isn't `pass`; an `image_unanswered` check is `review`.
  - `test_an_image_jev_refuses_is_marked_image`: 422 → `kind == "image"`.
  - `test_key_refused_raises`: 401 on every image → `JevRefused`.
  - `test_every_image_refused_raises`: 400 on every image → `JevRefused`.
  - `test_a_bug_still_raises`: patch `proof_check.ask_jev.analyze` to raise `KeyError` for one image → `KeyError` propagates.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** `failure_kind`, `JevRefused`, the catch in `run`, `unanswered`, and `unanswered_check`.
- [ ] **Step 4: Show it in the tools**: `__main__` prints unanswered images; `check_trip_folders` counts them in its summary line; the test page lists them (it renders `trip` already, so add the names to the response).
- [ ] **Step 5: Run the new tests, then the full suite** → pass.

### Task 6: Jev answers saved for retries (V5 store)

**Files:**
- Create: `verifier/migrations/004_jev_answers.sql`
- Modify: `verifier/database.py`
- Test: `tests/test_verifier.py` (`MigrationTests`, new `SavedAnswerTests` in the Postgres-backed tests)

**Interfaces:**
- Produces: table `jev_answer (request_id uuid REFERENCES verification_request, answer_key text, raw jsonb NOT NULL, made_at timestamptz DEFAULT now(), PRIMARY KEY (request_id, answer_key))`.
- Produces in `verifier/database.py`:
  - `async def saved_answers(pool, request_id: str) -> dict[str, dict]`.
  - `class SavedAnswers(collections.abc.MutableMapping)`: `__init__(self, pool, loop: asyncio.AbstractEventLoop, request_id: str, saved: dict[str, dict])`; reads from `saved`; `__setitem__` stores in `saved` and writes the row straight away with `asyncio.run_coroutine_threadsafe(..., loop).result()`, `INSERT … ON CONFLICT DO NOTHING`. Used from the worker thread that runs `analyze_trip(cache=...)`. `__delitem__` raises `TypeError` (not used).

- [ ] **Step 1: Write the failing tests**
  - `test_answers_are_saved_as_they_come`: in a thread (`asyncio.to_thread`), `SavedAnswers(...)["k1"] = {"a": 1}` → `await saved_answers(pool, rid) == {"k1": {"a": 1}}`.
  - `test_the_same_answer_saved_twice_is_fine`: two sets of the same key don't raise.
  - Migration test: `004` applied and recorded.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Write the migration and the two functions.**
- [ ] **Step 4: Run the new tests, then the full suite** → pass.

### Task 7: Retry only what can change; partial results (V5 retry, I5 in the verifier)

**Files:**
- Modify: `verifier/tasks.py` (`VERIFY_RETRY`, `verify`), `verifier/disposal_check.py` (`check`)
- Test: `tests/test_verifier.py` (`ServiceTests`, `CheckTests`, `BackoffTests`)

**Interfaces:**
- Consumes: `analyze_trip(..., cache=...)`, `trip["unanswered"]`, `JevRefused` (Task 5); `SavedAnswers`, `saved_answers` (Task 6).
- Produces:
  - `class JevUnavailable(Exception)` in `verifier/disposal_check.py`: raised by `check` when an image is unanswered with kind `"retry"` and `last_try` is False.
  - `check(request, fetch, client, verra_project=None, others=(), cache=None, last_try=False) -> Checked`. On the last try: if no image was answered at all, raise `JevUnavailable` anyway (so `verify` sends `error`); otherwise turn each unanswered image into the existing `unavailable_check` (`image_unavailable`, "Jev couldn't read it", review) instead of `image_unanswered`, so a missing S3 image and a Jev-unanswered one read the same in gw_sales.
  - `VERIFY_RETRY = Backoff(60, 600, 30 * 60, RETRYABLE)` with `RETRYABLE = (JevUnavailable, botocore.exceptions.ConnectionError, botocore.exceptions.HTTPClientError, psycopg.OperationalError)`. Waits unchanged (6 tries, ~25 min).
  - `verify` builds `SavedAnswers` from `saved_answers(...)` and `asyncio.get_running_loop()`, passes `last_try=VERIFY_RETRY.gives_up(JevUnavailable(), context.job)`. Anything not retryable (including `JevRefused` and our own bugs) goes straight to the existing `error` result with its ALERT.

- [ ] **Step 1: Write the failing tests**
  - `test_a_bug_is_not_retried`: patch `disposal_check.check` to raise `KeyError` → one attempt only, outcome `error`, an `ALERT` log line.
  - `test_jev_down_for_one_image_retries_then_sends_the_rest`: FakeJev answers 500 for the final bill on every try → the job is retried (check `procrastinate_jobs.attempts`), photo images are asked once in total across tries (count `jev.requests` for their colours), and after the last try gw_sales stores `outcome: flagged` with an `image_unavailable` flag naming the bill.
  - `test_jev_down_for_everything_sends_error`: every image 500 → after the last try `outcome: error` + ALERT.
  - `test_jev_refusing_every_image_sends_error_at_once`: every image 422 → one attempt, `outcome: error`, ALERT.
  - Review Focus: `test_missing_in_s3_and_unanswered_both_flagged`: one loading photo not in S3, the final bill unanswered on the last try → two `image_unavailable` evidence entries, outcome `flagged`, never `passed`.
  - `BackoffTests`: `VERIFY_RETRY.gives_up(KeyError(), job)` is True at attempt 0; `gives_up(JevUnavailable(), job)` False until the 6th.
  To run the retry tests quickly, patch `Backoff.wait` to return 0 in them.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** in `disposal_check.check` and `tasks.verify`.
- [ ] **Step 4: Run the new tests, then the full suite** → pass.
- [ ] **Step 5: Docs**: README verifier section (what is retried, partial results, saved answers); CLAUDE.md verifier line ("a checking error … sent as error" wording).

### Task 8: Lost checks and sends are picked up again (V3, V4)

**Files:**
- Create: `verifier/migrations/005_requeues.sql` (`ALTER TABLE verification_request ADD COLUMN requeues integer NOT NULL DEFAULT 0; ALTER TABLE verification_result ADD COLUMN requeues integer NOT NULL DEFAULT 0;`)
- Modify: `verifier/tasks.py` (`lost`, `sweep`), `verifier/database.py` (`waiting`, `note_requeue`, `day_report`)
- Test: `tests/test_verifier.py` (`ServiceTests`)

**Interfaces:**
- Produces: `MAX_REQUEUES = 3` in `verifier/tasks.py`. `lost(job_id) -> bool` now also True for status `failed`. `async def note_requeue(pool, task: str, request_id: str) -> int` returns the new count. `waiting(...)` rows gain the requeue count: `list[tuple[str, int | None, int]]`. `day_report` gains `"requestsUnchecked"` (requests with no result, any day).
- Sweep: for a lost job with requeues < `MAX_REQUEUES`: `note_requeue`, log `ALERT … queued again`, queue it. At the cap: verify → `save_result` an `outcome: error` result (`code: "VerifierFailed"`) and queue `send_result`; send → `note_attempt(..., gave_up="kept failing")`; both log an ALERT. If that save itself raises, log the ALERT and carry on (the daily report counts it).
- `daily_report` logs its ALERT when `requestsUnchecked` is not 0 too.

- [ ] **Step 1: Write the failing tests**
  - `test_a_verify_that_crashed_outside_its_try_is_queued_again`: patch `database.save_result` to raise once → after `sweep` (with `LOST_AFTER` patched to 0) the request gets a result and gw_sales stores it.
  - `test_a_verify_that_keeps_crashing_ends_as_error`: `save_result` raises `MAX_REQUEUES` times, then works → `outcome: error` stored, ALERT lines counted.
  - `test_a_send_that_crashed_is_sent_again`: patch `note_attempt` to raise once after a 200 → the result is resent and marked sent (gw_sales got it twice; fine).
  - Review Focus: `test_a_job_waiting_for_its_retry_is_not_lost`: a verify job in `todo` with a future `scheduled_at` → `lost()` is False and the sweep doesn't queue a second one.
  - `test_day_report_counts_requests_without_a_result`.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the new tests, then the full suite** → pass.

### Task 9: Compare with each disposal's latest version (V1)

**Files:**
- Modify: `verifier/database.py` (`others`), `CLAUDE.md` (the "latest kept analysis" line)
- Test: `tests/test_verifier.py` (`ServiceTests`)

**Interfaces:**
- `others(pool, disposal_id, vehicle, day) -> list[tuple[int, dict]]` unchanged signature: picks each disposal's latest request (`DISTINCT ON (disposal_id) … ORDER BY disposal_id, received_at DESC`, over all its requests, LEFT JOIN results), then keeps those whose latest has `vehicle_number = vehicle`, `trip_day` within ±1 day and an analysis.

- [ ] **Step 1: Write the failing tests**
  - `test_a_disposal_is_compared_as_last_edited`: 1000 checked as `kl 05 af 9341`, then edited to `vehicle={"id": 13, "vehicleNumber": "kl 07 bc 1234"}` and checked; 1001 with the same photos as `kl 05 af 9341` → no `reused` flag.
  - `test_a_disposal_whose_last_edit_is_not_checked_yet_is_left_out`: 1000 checked, then an edit only saved (`database.save_request`) → `others(pool, 1001, NUMBER, date(2026, 10, 15)) == []`.
- [ ] **Step 2: Run, expect both to fail.**
- [ ] **Step 3: Rewrite the query.**
- [ ] **Step 4: Run the new tests, then the full suite** → pass. Update CLAUDE.md: "use each disposal's latest request, found by its vehicle and IST trip date ±1 day, and skip one whose latest request has no analysis yet (`database.others`)".

### Task 10: Overlap only fails on sure photo times (V2)

**Files:**
- Modify: `proof_check/trip_rules.py` (`busy_time`, `overlap_checks`), `verifier/compare_disposals.py` (`overlap`)
- Test: `tests/test_proof_check.py`, `tests/test_verifier.py` (`CompareTests`)

**Interfaces:**
- `busy_time(images, sure_only: bool = False) -> tuple[datetime, datetime] | None`: with `sure_only`, only stamps whose moment `certainty >= CONFIDENT`.
- `overlap(...)` and `overlap_checks(...)`: `fail` when the sure windows of both overlap by more than the slack; else `review` when the all-stamps windows do, with `certainty` the lowest stamp certainty involved and a detail naming the unsure photos ("… was read as 15:10, but only 30% sure …"); else pass.

- [ ] **Step 1: Write the failing tests**
  - `test_overlap_on_an_unsure_stamp_asks_a_person`: this trip 09:00–10:00 sure plus one unloading stamp 15:00 at 0.30; another disposal 13:00–14:00 sure → `overlap` is `review`, not `fail`.
  - `test_overlap_on_sure_stamps_is_still_a_problem`: both sure and overlapping → `fail` (existing behaviour kept).
  - Same pair for `overlap_checks` in the tools.
- [ ] **Step 2: Run, expect the first to fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the new tests, then the full suite** → pass.

### Task 11: A screen-photo threshold for bills (T1)

**Files:**
- Modify: `proof_check/trip_rules.py` (`NOUL_THRESHOLDS`, `noul_status`, its callers `checks_for`, `each_photo`)
- Test: `tests/test_proof_check.py` (`AnalyzeTests.test_yes_no_thresholds`, `TripTests`)

**Interfaces:**
- `NOUL_THRESHOLDS = {("photo", "screen_photo"): (0.8, 0.6), ("bill", "screen_photo"): (0.8, 0.4)}` with the comment: "On the 180 saved bills of 19 and 23 Oct 2026, all taken directly, screen_photo scored at most 0.33."
- `noul_status(yes: float, pass_when_yes: bool = True, qid: str | None = None, slot: str | None = None) -> str`, looking up `(slot, qid)`, else `(YES, NO)`.

- [ ] **Step 1: Write the failing tests**: a bill with `screen_photo` yes 0.60 → `bill_direct` `review`; 0.33 → pass; a photo at 0.60 → pass (unchanged).
- [ ] **Step 2: Run, expect the 0.60 bill to fail the assertion (passes today).**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the new tests, then the full suite** → pass.
- [ ] **Step 5: Cached check (free):** `.venv/bin/python -m tools.check_trip_folders trips/inward-2026-10-19 --cached --plant "11.4404, 75.9420"` → summary line still `20 accepted, 14 with a problem, 8 to check, 6 skipped`.

### Task 12: An unsure lean towards "tare heavier" asks a person (R3)

**Files:**
- Modify: `proof_check/trip_rules.py` (`bill_checks`, the `gross_vs_tare` block)
- Test: `tests/test_proof_check.py` (`TripTests`)

**Interfaces:** `LEANS = 0.5` beside `CONFIDENT`. When `gross_vs_tare` is `tare_heavier` with `LEANS <= confidence < CONFIDENT`, the `gross_vs_tare` check is added as `review` ("Only 65% sure: the tare weight is larger than the gross weight. Check the weights on the slip.", `images=[final name]`). Other unsure answers still add nothing.

- [ ] **Step 1: Write the failing tests**: tare_heavier at 0.65 → `review` check present; at 0.45 → no check; gross_heavier at 0.65 → no check; not_shown at 0.65 → no check; tare_heavier at 0.9 → `fail` (unchanged).
- [ ] **Step 2: Run, expect the first to fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the new tests, then the full suite** → pass.

### Task 13: No pass on an unsure Time In or unloading date (R4)

**Files:**
- Modify: `proof_check/trip_rules.py` (`time_in_tare` block in `trip_checks`, `same_day`)
- Test: `tests/test_proof_check.py` (`TripTests`)

**Interfaces:** `time_in_tare` with `first_weight` = `tare` below `CONFIDENT` → `review` ("Only 30% sure: the lighter weight is at Time In. Check which weight is at Time In.") with `cause="weigh_times"`. `same_day` with every photo on the gross date but a photo date below `CONFIDENT` → `review` naming those photos, with the same `cause` the `unloading_timing` check gives those photos (give both the cause `"unloading_stamps"` if they have none today).

- [ ] **Step 1: Write the failing tests**: `first_weight` tare at 0.30 → `time_in_tare` `review` and the trip's flags show one flag for it and `weigh_order` (same cause); an unloading photo dated 15/10 at 0.30 with gross on 15/10 → `unloading_day` `review`, grouped with `unloading_timing`.
- [ ] **Step 2: Run, expect failures (both pass today).**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the new tests, then the full suite** → pass, and the cached 19 Oct summary line is unchanged (Task 11 Step 5 command).

### Task 14: Same photo for loading and unloading, in every tool (R7)

**Files:**
- Modify: `proof_check/trip_rules.py` (new `same_file`, called from `trip_checks`), `verifier/compare_disposals.py` (remove `same_file` and its call; keep the docstring right), `proof_check/__init__.py`
- Test: `tests/test_proof_check.py` (`TripTests`), `tests/test_verifier.py` (move the `same_file` tests)

**Interfaces:** `same_file(images: dict[str, list[dict]]) -> dict` in `trip_rules`, key `same_file` (already `real` in `PART_OF`), `fail` when an unloading result's `prints["md5"]` equals a loading one's, detail "The same file is both a loading (collection) photo and an unloading photo: unloading1.jpg. One photo can't show both the loading and the unloading.", `images` both names; `pass` otherwise. Only added when the trip has both kinds.

- [ ] **Step 1: Write the failing test**: `run_trip(same=(("loading", 0, "unloading", 0),))` with picks that pass every timing check → `same_file` is `fail` and the real part is `problem`.
- [ ] **Step 2: Run, expect failure (no such check in proof_check).**
- [ ] **Step 3: Move the check**; the verifier's flags come from `trip["verdict"]["checks"]` now, so `compare_disposals.checks` no longer adds it.
- [ ] **Step 4: Run the new tests, then the full suite** → pass; the verifier's `same_file` flag test still passes with one flag, not two.

### Task 15: More vehicle numbers cleaned up; say when not compared (L2)

**Files:**
- Modify: `proof_check/inputs.py` (`normalize_vehicle_number`), `verifier/compare_disposals.py` (`checks`), `proof_check/trip_rules.py` (`PART_OF`)
- Test: `tests/test_proof_check.py` (`TypedValueTests.test_normalize_vehicle_number`), `tests/test_verifier.py` (`CompareTests`)

**Interfaces:**
- `normalize_vehicle_number(text) -> str | None`: as today, plus: a full number followed by `/…` or `(…)` keeps the full number (`KL05AF9341/T`, `KL05AF9341 (hired)` → `KL05AF9341`); a two-letter state, one-digit district, series and number gets a 0 (`KL 5 AF 9341` → `KL05AF9341`).
- `checks(vehicle=None, ...)` adds `not_compared` (part `real`, `review`): "The vehicle number entered couldn't be read as a number, so this disposal wasn't compared with other disposals. Check it against the photos."

- [ ] **Step 1: Write the failing tests**: the three new forms; Review Focus: `KL05AF9341`, `TN38BX1234`, `KL57X7639`, `kl 05 af-9341` unchanged; `abc` still None; `checks(None, …)` includes `not_compared` `review`.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the new tests, then the full suite** → pass.
- [ ] **Step 5: Cached check (free):** run the Task 11 Step 5 command and the same for `trips/inward-2026-10-23` → the 19 Oct summary line unchanged, and 23 Oct skips no more trips than before (it skipped 59 on 30 Oct for lack of saved answers under today's questions).

### Task 16: Times without a zone, and the year of a stamp (L1, L3)

**Files:**
- Modify: `verifier/standards.py` (`ist_time`), `proof_check/trip_rules.py` (`moment`, `photo_moments`, `weighings`, `busy_time`, `trip_checks`), `proof_check/verdict.py` (`verdict`), `proof_check/ask_jev.py` (`analyze_trip`), `verifier/disposal_check.py`, `verifier/entry_checks.py` (`too_late`), `tools/check_trip_folders.py`, `proof_check/__main__.py`, `tools/test_page/server.py`
- Test: `tests/test_proof_check.py`, `tests/test_verifier.py`

**Interfaces:**
- `ist_time(utc)`: a time with no zone is read as IST and logs a warning naming the value.
- `moment(time, date, around: date | None = None) -> dict | None`: with `around`, the year is the one of `around.year - 1`, `around.year`, `around.year + 1` that puts the date nearest `around`; without it, `YEAR` as today. `photo_moments(results, around=None)`, `weighings(bill, first_slip=None, around=None)`, `busy_time(images, sure_only=False, around=None)`, `trip_checks(..., around=None)`, `verdict(..., around=None)`, `analyze_trip(..., around=None)` pass it down.
- Callers: the verifier passes the disposal's IST trip date (`d.day`); `check_trip_folders` passes the date in the folder name when it has one (`inward-2026-10-19`), else today; the CLI and test page pass today.

- [ ] **Step 1: Write the failing tests**
  - `test_a_zone_less_time_is_read_as_ist`: `ist_time("2026-10-15T09:00:00")` is 09:00 IST, with a warning logged.
  - `test_new_year_trip`: around 2027-01-01, loading stamped 31/12 23:50, unloading 01/01 00:20 → `busy_time` spans 30 min.
  - `test_28_feb_to_1_mar_in_a_common_year`: around 2027-03-01, weighings 28/02 23:00 and 01/03 01:00 → 120 min apart.
  - `test_too_late_across_new_year`: entry end 31/12 23:00 IST, photo 31/12 23:20 → not flagged by date wrap.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement**, keeping `around=None` behaviour identical (all existing tests pass unchanged).
- [ ] **Step 4: Run the new tests, then the full suite** → pass; cached 19 Oct summary line unchanged.

### Task 17: Input parsing (L4, L12)

**Files:**
- Modify: `proof_check/inputs.py` (`parse_limits`, `parse_quantity`, `parse_plant`), `tools/check_trip_folders.py` (pass the run's limits to `overlap_checks`)
- Test: `tests/test_proof_check.py` (`TypedValueTests`)

**Interfaces:** `parse_limits({"clock_slack": 0}).clock_slack == 0`; `parse_quantity("2570,5")` raises `ValueError` (a comma before exactly one or two digits at the end is a decimal comma); `"2,570"` still gives `"2570"`; `parse_plant("75.9420, 11.4404")` raises (`INDIA = ((6.0, 37.5), (68.0, 97.5))` for latitude and longitude); `overlap_checks(trips, slack=limits.clock_slack)` in the runner.

- [ ] **Step 1: Write the failing tests** for each line above.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the new tests, then the full suite** → pass.

### Task 18: Sweep window and gw_sales 2xx (L10, L13)

**Files:**
- Create: `verifier/migrations/006_received_index.sql` (`CREATE INDEX verification_request_received ON verification_request (received_at);`)
- Modify: `verifier/database.py` (`waiting`), `verifier/tasks.py` (`send_result`)
- Test: `tests/test_verifier.py`

**Interfaces:** `SWEEP_DAYS = 7`; `waiting` only looks at requests and results from the last `SWEEP_DAYS` (and before `before`). `send_result` treats any `200 <= status < 300` as delivered.

- [ ] **Step 1: Write the failing tests**: a request 8 days old with no result isn't returned by `waiting` (and is still counted by `day_report["requestsUnchecked"]`); gw_sales answering 204 → marked sent, no ALERT.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the new tests, then the full suite** → pass.

### Task 19: Hardening the receiver and settings (S1, S2, S6, S7, D3)

**Files:**
- Modify: `verifier/api.py` (`receive`), `verifier/settings.py` (`ENV`/`OPTIONAL`, `Settings`, `from_env`), `verifier/signatures.py` (`verify`), `deploy/dev.env.example`
- Test: `tests/test_verifier.py` (`SigningTests`, a new `SettingsTests`, `ServiceTests`; update `SETTINGS` secrets to 32+ characters)

**Interfaces:**
- `MAX_REQUEST = 1024 * 1024` in `api.py`: a Content-Length over it, or a body that grows past it while read with `request.stream()`, gets 413 before the signature check.
- `Settings.webhook_secret_next: str | None = field(default=None, repr=False)` from `VERIFIER_WEBHOOK_SECRET_NEXT`; `receive` accepts a request signed with either.
- `from_env` raises `SystemExit` naming the setting when `GW_SALES_URL` isn't `https://` (allowed: `http://localhost…`, `http://127.0.0.1…`), or a webhook/callback secret (or the next one) is under 32 characters.
- `signatures.verify` returns False on a non-ASCII signature (catch `TypeError`).

- [ ] **Step 1: Write the failing tests**: 2 MB body → 413 and no row saved; `X-GW-Signature: sha256=é` → 401; `from_env` with `http://dev-sales.example` → SystemExit; with a 10-character secret → SystemExit; a request signed with the next secret → 202.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement**; add the new setting to `deploy/dev.env.example` (name only).
- [ ] **Step 4: Run the new tests, then the full suite** → pass.
- [ ] **Step 5: Docs**: README settings table (new setting, 32-character rule, https rule) and the deploy note from the spec (S7: agree longer secrets with gw_sales first if the dev ones are shorter).

### Task 20: Only gw_sales' photo folders are read (S4)

**Files:**
- Modify: `verifier/disposal_check.py` (`check`), `verifier/s3_photos.py`
- Create: `deploy/s3-read-policy.example.json`
- Test: `tests/test_verifier.py` (`CheckTests`)

**Interfaces:** `PHOTO_PREFIXES = ("disposal/", "collection-logs/")` in `s3_photos.py`; a key outside them isn't fetched and is unavailable with why `"isn't in one of gw_sales' photo folders"`. The policy example allows `s3:GetObject` on `arn:aws:s3:::<bucket>/disposal/*` and `…/collection-logs/*` only.

- [ ] **Step 1: Write the failing test**: evidence with `s3Key` `"secrets/x.jpg"` → fetch never called for it; an `image_unavailable` flag with that why.
- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3: Implement** and write the policy example.
- [ ] **Step 4: Run the new test, then the full suite** → pass.

### Task 21: Test page hardening (S5, L14)

**Files:**
- Modify: `tools/test_page/server.py` (`Handler.do_POST` and the trip loader)
- Test: `tests/test_proof_check.py` (the test page tests that already post to `/api/check`)

**Interfaces:** `POST /api/check` answers 415 unless `Content-Type` is `application/json`, 403 unless `Host` is `127.0.0.1:<port>` or `localhost:<port>`, 400 for a negative Content-Length, a NUL in the trip id, a non-string field, or an image that can't be read (`UNREADABLE`), each with a JSON `{"error": "..."}`.

- [ ] **Step 1: Write the failing tests**: one request per case above → that status and an `error` message; the server keeps answering afterwards.
- [ ] **Step 2: Run, expect failures/hangs (use a timeout in the test client).**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the new tests, then the full suite** → pass.

### Task 22: Worker heartbeat (D4)

**Files:**
- Create: `verifier/migrations/007_worker_heartbeat.sql` (`CREATE TABLE worker_heartbeat (id int PRIMARY KEY DEFAULT 1 CHECK (id = 1), beat_at timestamptz NOT NULL);`)
- Modify: `verifier/database.py` (`beat`, `last_beat`), `verifier/tasks.py` (`sweep` calls `beat`), `verifier/api.py` (a background task started with the app)
- Test: `tests/test_verifier.py`

**Interfaces:** `async def beat(pool) -> None` (upsert now); `async def last_beat(pool) -> datetime | None`. `SILENT_AFTER = timedelta(minutes=5)`, `ALERT_EVERY = timedelta(minutes=30)` in `api.py`; `async def watch_worker(pool, now=...)` runs every minute in the web process and logs `ALERT worker silent since …` when `last_beat` is older than `SILENT_AFTER` (or missing after the web has run 5 minutes), at most once per `ALERT_EVERY`. `/ready` is unchanged.

- [ ] **Step 1: Write the failing tests**: after `sweep`, `last_beat` is recent; `watch_worker` with a 6-minute-old beat logs one ALERT, a second call within 30 min logs none, one 31 min later logs again; a fresh beat logs none.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the new tests, then the full suite** → pass.

### Task 23: Deploy examples, backups and the app's database role (S3, D1, D2)

**Files:**
- Create: `deploy/Caddyfile.example`, `deploy/backup.sh`
- Modify: `deploy/docker-compose.yml`, `deploy/dev.env.example`, `README.md` (deploy section)
- Test: `docker compose -f deploy/docker-compose.yml --env-file deploy/dev.env.example config` validates (fill the example's blanks in a throwaway copy in the scratchpad).

**Interfaces:**
- `Caddyfile.example`: the verifier's host, `request_body { max_size 1MB }`, `reverse_proxy /v1/verifications 127.0.0.1:8080`, everything else 404.
- D2: the `db` service's init script creates role `verifier_app` (LOGIN, password from `VERIFIER_DB_PASSWORD`) owning database `verifier`; `POSTGRES_USER` stays the superuser for the container only; the app's `VERIFIER_DATABASE_URL` uses `verifier_app`. README: generate both passwords with `openssl rand -hex 32`; the dev database is recreated (`down -v`, it holds no real data yet), and say that this deletes it.
- D1: `deploy/backup.sh` runs `pg_dump -Fc` through `docker compose exec -T db` into `/var/backups/jev-verifier/verifier-YYYY-MM-DD.dump`, deletes dumps older than 14 days; README gives the root cron line (`10 2 * * *`) and the restore command (`pg_restore --clean --if-exists`), and notes that copying the dumps off the server is still to do.

- [ ] **Step 1: Write the files.**
- [ ] **Step 2: Validate** the compose file as above → no errors; `bash -n deploy/backup.sh` → no errors.
- [ ] **Step 3: Smoke-test** only if the user agrees (it needs Docker): bring the stack up in a throwaway compose project, check the app connects as `verifier_app` and migrations run, run `backup.sh` against it, then `down -v`.

### Task 24: Final check

- [ ] **Step 1:** Full suite: `.venv/bin/python -m unittest discover -s tests -v` → all pass (note the count).
- [ ] **Step 2:** Cached runs (free): `.venv/bin/python -m tools.check_trip_folders trips/inward-2026-10-19 --cached --plant "11.4404, 75.9420"` → `20 accepted, 14 with a problem, 8 to check, 6 skipped` unless a task above said why it moved; same command for `trips/inward-2026-10-23` → no more skipped than the 59 of 30 Oct.
- [ ] **Step 3:** README and CLAUDE.md read through against the spec's "Done when": retries, partial results, saved answers, HEIC, `same_file` in `trip_checks`, vehicle clean-up and `not_compared`, the bill threshold, backups, deploy examples, the heartbeat.
- [ ] **Step 4:** Report to the user: what changed per gap, test count, cached-run lines, and anything skipped. Don't commit.
