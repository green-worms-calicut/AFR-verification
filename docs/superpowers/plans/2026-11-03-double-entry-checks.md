# Double-entry checks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Flag a load entered twice in gw_sales. The cases are:
- the same weighing in two disposals (`same_weighing`);
- an image reused from any earlier disposal, of any vehicle (`reused` over all history);
- one photo used for two collection logs (`log_same_photo`);
- one pickup counted twice (`log_reused`, `log_repeated`).

None of these needs new Jev questions or adds Jev cost.

**Architecture:**
- **Pure checks.** All checks are pure functions over data. They live in `verifier/compare_disposals.py` (across disposals) and `verifier/entry_checks.py` (collection logs).
- **Fingerprints** are saved per request in a new `image_print` table before Jev is asked, so a request that ends in an error is still found. The worker thread reaches the database through a small bridge object, as `database.SavedAnswers` does.
- **Collection logs of other disposals** come from the stored request bodies.

**Tech Stack:** Python 3.14, psycopg 3 async pool, Procrastinate, Postgres (dev on 127.0.0.1:5433), unittest with the fake Jev, fake gw_sales and moto.

**Spec:** `docs/superpowers/specs/2026-11-03-double-entry-checks-design.md`. Read it with this plan.

## Global Constraints

- **Scope:**
  - for every trip, not only Verra project trips;
  - every new check key is in `PART_OF` as `"real"`;
  - no new Jev questions, and `question_version` and `answer_key` are unchanged.
- **Migrations:** a new numbered migration, `verifier/migrations/008_image_print.sql`. Never edit an applied one.
- **Flag wording:**
  - a flagged check's `detail` is one plain sentence: what is wrong, in which image or log, and what to look at;
  - give `images`, and set `related` to the other disposals (`compare_disposals.related`);
  - a sure finding is `"fail"`, an unsure one `"review"`, and nothing is rejected.
- **Which requests count:**
  - a disposal is never compared with its own requests (`disposal_id <> %(disposal)s`);
  - only each other disposal's latest request counts (`DISTINCT ON (disposal_id) … ORDER BY received_at DESC`), whether analysed, ended in an error or still being checked.
- **Scope limits:**
  - look-alikes stay as today: same vehicle, trip dates a day either side, through `others`;
  - only exact copies (md5) are looked up over all history;
  - tare is never used to match weighings.
- **Constants:**
  - `SAME_WEIGHING = 2` (minutes);
  - `LOOKS_ALIKE` and `FEATURELESS` are reused from `proof_check.images`;
  - `CONFIDENT` (0.8) from `proof_check.trip_rules` decides sure against unsure.
- **Running things:**
  - run everything from the project folder with `.venv/bin/python`;
  - the tests are `.venv/bin/python -m unittest discover -s tests -v`, with the `jev-verifier-db` container running so `test_verifier.py`'s database tests run, not skip;
  - leave the Postgres on 5432 alone.
- **Never** commit or publish `trips/`, `eval/` or `mockups/`, and never read or print the API key.

## Review Focus

These are the most likely ways the new code breaks for a real user. Each one has a test in the task named after it.

1. **Two duplicates under different vehicles checked at the same time.** The later one must be flagged even though the earlier one has no result yet. Checks are locked per vehicle only. Test in Task 4.
2. **A retry of the same request after Jev was busy** (`JevUnavailable`). Saving the fingerprints again must not fail, and the request must never match its own rows. Tests in Task 3 and Task 4.
3. **An image that couldn't be fetched or decoded** (`image_unavailable`). It gets no fingerprint row, and fingerprinting the others doesn't crash. Test in Task 3.
4. **A collection log missing its pickup date, client or collection point, or with no materials.** `log_repeated` skips it rather than matching on blanks. Test in Task 6.
5. **A disposal with no vehicle number.** The all-history exact `reused` check still runs, because it doesn't need a vehicle. The vehicle-based comparisons still give `not_compared`. Test in Task 3.

---

### Task 1: `same_weighing`: the same weighing in two disposals

**Files:**
- Modify: `proof_check/trip_rules.py` (add `trip_weighings`; use it at the `w = weighings(...)` line in `trip_checks`, ~906; add `"same_weighing": "real"` to `PART_OF`)
- Modify: `proof_check/__init__.py` (re-export `trip_weighings`)
- Modify: `verifier/compare_disposals.py` (`SAME_WEIGHING`, `Other.quantity`, `same_weighing`, call order in `checks`)
- Modify: `verifier/database.py:153` (`others` also returns the quantity)
- Modify: `verifier/tasks.py:148` (`others_of` builds `Other` with the quantity)
- Test: `tests/test_verifier.py` (`CompareTests`, `ServiceTests`)

**Interfaces:**
- Produces: `proof_check.trip_weighings(images: dict[str, list[dict]], around=None) -> dict | None`. This is `weighings(final_slip(bills), first_slip(bills), around)` for `images["bill"]`, or None without a final slip. `trip_checks` uses it too, so both read the same moment.
- Produces: `compare_disposals.Other(disposal_id: int, analysis: dict, quantity: str | None = None)`. `quantity` is the `verification_request.quantity` text, for example `"2570 kg"`.
- Produces: `database.others(...) -> list[tuple[int, dict, str | None]]` (disposal id, analysis, quantity).
- Produces: `compare_disposals.same_weighing(images: dict, quantity: str | None, others: list[Other], flagged: set[int], around=None) -> dict | None`. It returns None when this disposal has no gross moment.
- Changes: `compare_disposals.checks(vehicle, analysis, others, slack, around, quantity: str | None = None)`. The order is `reused`, `same_weighing`, `overlap`, `same_moment`. Each adds the disposals it flagged to `flagged`, as now.

- [ ] **Step 1: Write the failing tests in `CompareTests`**

```python
def test_a_new_photo_of_the_same_slip_is_the_same_weighing(self):
    uploads, jev = other_trip(loading={**LOADING, **stamp("05:20")}, unloading={**UNLOADING, **stamp("06:40")})
    # other photos, the same bill readings as disposal 1000: gross 03:30 on 15/10
    flag = self.flag(self.checked(uploads, jev, [self.first()]), "same_weighing")
    self.assertEqual((flag["kind"], flag["severity"]), ("problem found", "high"))
    self.assertEqual(flag["related"], [{"type": "disposal_log", "id": 1000}])
    self.assertIn("disposal/2026/10/15/b-weighbill2.jpg shows the gross weighing at 03:30 on 15/10, the same as "
                  "disposal 1000's slip disposal/2026/10/15/weighbill2.jpg", flag["message"])
    self.assertIn("One weighing can't be proof for two disposals.", flag["message"])

def test_the_same_net_weight_is_named_when_both_quantities_match(self):
    first = compare_disposals.Other(1000, self.first().analysis, f"{QUANTITY} kg")
    uploads, jev = other_trip(loading={**LOADING, **stamp("05:20")}, unloading={**UNLOADING, **stamp("06:40")})
    flag = self.flag(self.checked(uploads, jev, [first]), "same_weighing")
    self.assertIn(f"and the same net weight, {QUANTITY} kg", flag["message"])

def test_an_unsure_gross_time_asks_a_person(self):
    unsure = (FIRST, {**FINAL, **at("gross", "03:31", confidence=0.4)})
    uploads, jev = other_trip(loading={**LOADING, **stamp("05:20")}, unloading={**UNLOADING, **stamp("06:40")}, bill=unsure)
    flag = self.flag(self.checked(uploads, jev, [self.first()]), "same_weighing")
    self.assertEqual(flag["kind"], "please check")
    self.assertIn("40% sure", flag["message"])

def test_three_minutes_apart_is_another_weighing(self):
    uploads, jev = other_trip(loading={**LOADING, **stamp("05:20")}, unloading={**UNLOADING, **stamp("06:40")},
                              bill=(FIRST, {**FINAL, **at("gross", "03:33")}))
    checked = self.checked(uploads, jev, [self.first()])
    self.assertEqual(next(c for c in checked.analysis["verdict"]["checks"] if c["key"] == "same_weighing")["status"], "pass")

def test_one_morning_tare_is_not_the_same_weighing(self):
    morning = (FIRST, {**FINAL, **at("gross", "06:30")})  # tare 02:00 as disposal 1000's, another gross
    uploads, jev = other_trip(loading={**LOADING, **stamp("05:20")}, unloading={**UNLOADING, **stamp("06:40")}, bill=morning)
    self.assertIsNone(self.flag(self.checked(uploads, jev, [self.first()]), "same_weighing"))
```

Also make these changes to existing tests:
- Add `"same_weighing"` to the keys in `test_alone_every_comparison_passes`.
- In `test_another_trip_at_the_same_time_is_a_problem_whatever_the_quantity` and the first half of `test_a_pair_is_flagged_once_by_the_most_specific_check`, pass `bill=(FIRST, {**FINAL, **at("gross", "03:40")})`. Those tests are about `overlap`, and `other_trip()`'s default bill now has the same gross weighing as disposal 1000's.
- In `ServiceTests`, assert that `database.others` returns the quantity as its third value. Extend `test_a_disposal_whose_last_edit_is_not_checked_yet_is_left_out`'s neighbour, or add `test_others_carry_the_quantity`.

- [ ] **Step 2: Run the tests and see them fail**

Run: `.venv/bin/python -m unittest tests.test_verifier.CompareTests -v`
Expected: the new tests FAIL with no `same_weighing` flag, or a `TypeError` on `Other(…, quantity)`.

- [ ] **Step 3: Implement**

- **`trip_weighings`** goes in `proof_check/trip_rules.py` next to `weighings`, and `trip_checks` calls it.
- **`same_weighing`** works like this:
  - `here = trip_weighings(images, around)["gross"]`.
  - For each other disposal not in `flagged`, compare with `trip_weighings(o.analysis["images"], around)["gross"]`.
  - A match is the same date with `abs(minutes) <= SAME_WEIGHING`.
  - The result is `"fail"` when both certainties are `>= CONFIDENT`, else `"review"`, with `certainty` set to the lower one.
  - Name the slips with `w["slips"][-1]`.
  - The detail names this slip, the moment, the other disposal and its slip.
  - When `quantity` and `o.quantity` are equal and both final slips' `answers["net_weight"]["choice"] == "same"`, add `", and the same net weight, {kg} kg"` before the closing sentence. `kg` is the number in `quantity`.
  - An unsure match says which slip's time was unsure and how sure Jev was, as `overlap` does: `"read as 03:31 on 15/10, but only 40% sure"`.
  - The pass detail is `"No other disposal of this vehicle with the same gross weighing"`.
  - The label is `"Weighing not in another disposal"`.
- **`database.others`** selects `q.quantity` too, in the inner and outer SELECTs.
- **`tasks.others_of`** passes `Other(*row)` unchanged.
- **`disposal_check.check`** passes `d.quantity_key` to `compare_disposals.checks(..., quantity=...)`.

- [ ] **Step 4: Run all tests and see them pass**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: OK, with `test_verifier`'s database tests run, not skipped.

- [ ] **Step 5: Update the docs and commit**

- In README.md, in the verifier's "compared with other disposals" text, add one sentence on `same_weighing`.
- In CLAUDE.md, add `same_weighing` to the list in the verifier bullet ("`reused`, `same_moment`, `overlap`, `same_file`").

```bash
git add proof_check/trip_rules.py proof_check/__init__.py verifier/compare_disposals.py verifier/database.py verifier/tasks.py verifier/disposal_check.py tests/test_verifier.py README.md CLAUDE.md
git commit -m "feat: flag the same weighing entered in two disposals (same_weighing)"
```

---

### Task 2: `image_print` table: save and look up fingerprints

**Files:**
- Create: `verifier/migrations/008_image_print.sql`
- Modify: `verifier/database.py` (`ImagePrint`, `Copy`, `save_prints`, `copies`)
- Test: `tests/test_verifier.py` (`MigrationTests`, `ServiceTests`)

**Interfaces:**
- Produces, in `verifier/database.py`:
  - `ImagePrint = tuple[str, str, str, str]`: (kind, name, md5, dhash). `kind` is proof_check's role (`"loading"`, `"unloading"`, `"bill"`) and `name` is the `s3Key`.
  - `@dataclass class Copy: name: str; kind: str; md5: str; disposal_id: int; vehicle: str | None; day: date | None`
  - `async def save_prints(pool, request_id: str, prints: list[ImagePrint]) -> None`. It uses `ON CONFLICT DO NOTHING`, so it's safe to call again on a retry.
  - `async def copies(pool, disposal_id: int, md5s: list[str]) -> list[Copy]`. It returns rows of other disposals' latest requests (analysed, errored or in progress) whose md5 is in `md5s`. Any vehicle, any date, never `disposal_id` itself.

- [ ] **Step 1: Write the failing tests**

In `MigrationTests.test_new_database_then_nothing_twice`, assert `"verifier/008_image_print.sql"` is in `applied`. Then add to `ServiceTests`:

```python
async def test_the_migration_fills_prints_from_saved_results(self):
    # A result saved before 008: its analysis holds the prints. Run 008's backfill SQL again and find them.
    uploads = self.store_trip()
    await self.post(disposal(uploads, disposal_id=1000))
    await self.work()
    await self.sql("TRUNCATE image_print")
    async with self.svc.pool.connection() as conn:
        await conn.execute((migrate.OURS / "008_image_print.sql").read_text().split("-- backfill\n", 1)[1])
    [(n,)] = await self.sql("SELECT count(*) FROM image_print")
    self.assertEqual(n, 6)

async def test_copies_are_found_in_any_vehicles_latest_request_never_its_own(self):
    a = disposal(disposal_id=1000)
    await database.save_request(self.svc.pool, a)
    await database.save_prints(self.svc.pool, a["requestId"], [("loading", "k1.jpg", "m1", "0f" * 8)])
    await database.save_prints(self.svc.pool, a["requestId"], [("loading", "k1.jpg", "m1", "0f" * 8)])  # a retry
    found = await database.copies(self.svc.pool, 1001, ["m1", "m2"])
    self.assertEqual([(c.name, c.disposal_id) for c in found], [("k1.jpg", 1000)])
    self.assertEqual(await database.copies(self.svc.pool, 1000, ["m1"]), [], "never its own")
    edit = disposal(disposal_id=1000, reason="edited", data_version="sha256:bbb")
    await database.save_request(self.svc.pool, edit)
    await database.save_prints(self.svc.pool, edit["requestId"], [("loading", "k2.jpg", "m2", "0f" * 8)])
    self.assertEqual([c.name for c in await database.copies(self.svc.pool, 1001, ["m1", "m2"])], ["k2.jpg"],
                     "an image dropped in an edit is forgotten")
```

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m unittest tests.test_verifier.MigrationTests tests.test_verifier.ServiceTests -v`
Expected: FAIL. The 008 file doesn't exist, and `save_prints` isn't defined.

- [ ] **Step 3: Write the migration and the two functions**

`008_image_print.sql` starts with a two-line comment saying what the table is for, in the style of 004. Then:

```sql
CREATE TABLE image_print (
    request_id uuid NOT NULL REFERENCES verification_request,
    kind       text NOT NULL,
    name       text NOT NULL,
    md5        text NOT NULL,
    dhash      text NOT NULL,
    PRIMARY KEY (request_id, kind, name)
);
CREATE INDEX image_print_md5 ON image_print (md5);
-- backfill
INSERT INTO image_print (request_id, kind, name, md5, dhash)
SELECT r.request_id, k.kind, img->>'name', img->'prints'->>'md5', img->'prints'->>'dhash'
FROM verification_result r, jsonb_each(r.analysis->'images') AS k(kind, list), jsonb_array_elements(k.list) AS img
WHERE r.analysis IS NOT NULL AND img->'prints' IS NOT NULL
ON CONFLICT DO NOTHING;
```

- **`copies`:** select from `image_print p JOIN verification_request q USING (request_id)` where `q.request_id` is the latest request of its disposal. Pick it with `DISTINCT ON (disposal_id) … ORDER BY disposal_id, received_at DESC` over `verification_request`, with `q.disposal_id <> %(disposal)s` and `p.md5 = ANY(%(md5s)s)`. Return `Copy(name, kind, md5, disposal_id, vehicle_number, trip_day)` ordered by disposal id and name.
- **The `TRUNCATE` in `ServiceTests.asyncSetUp`** already cascades from `verification_request`, so it empties `image_print` too.

- [ ] **Step 4: Run all tests and see them pass**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add verifier/migrations/008_image_print.sql verifier/database.py tests/test_verifier.py
git commit -m "feat: keep every request's image fingerprints (image_print) and find copies in any disposal"
```

---

### Task 3: `reused` over all history in the check

**Files:**
- Modify: `verifier/disposal_check.py` (`check` gets a `history` parameter and fingerprints the fetched images before `analyze_trip`)
- Modify: `verifier/compare_disposals.py` (`reused` takes `copies`; `checks` runs `reused` without a vehicle too)
- Test: `tests/test_verifier.py` (`CompareTests`)

**Interfaces:**
- Consumes: `database.ImagePrint` and `database.Copy` (Task 2).
- Produces: `disposal_check.check(..., history: Callable[[list[ImagePrint]], list[Copy]] | None = None)`.
  - Before `analyze_trip`, it calls `history` once with a row for every fetched upload: `proof_check.fingerprints(data)`, skipping any that raise `proof_check.UNREADABLE`.
  - It passes the returned copies to `compare_disposals.checks`.
  - With `history=None`, nothing is looked up, so existing callers and tests keep working.
- Changes: `compare_disposals.reused(images, others, copies: list[Copy] = ())`.
  - Exact matches come from `copies` (any vehicle, any date) and from `others` (as now), de-duplicated by (this image, other image, disposal).
  - Look-alikes come from `others` only.
  - A copy's detail reads `"{a} is the same file as {b} in disposal {i} ({vehicle}, {dd/mm})"`. Leave out the vehicle or date when it isn't known.
- Changes: `compare_disposals.checks(vehicle, analysis, others, slack, around, quantity=None, copies=())`.
  - `reused` always runs.
  - Without a vehicle, the result is `[reused(images, [], copies), not_compared]`.

- [ ] **Step 1: Write the failing tests**

```python
def copy(self, name="collection-logs/2026/10/15/collection1.jpg", disposal_id=900, vehicle="KL07BC1234", day=date(2026, 9, 1)):
    uploads, _ = make_trip()
    md5 = proof_check.fingerprints(files(uploads)[name])["md5"]
    return database.Copy(name, "loading", md5, disposal_id, vehicle, day)

def test_a_copy_from_an_older_disposal_of_another_vehicle_is_a_problem(self):
    uploads, jev = make_trip()
    with jev.client() as client:
        checked = disposal_check.check(disposal(uploads), files(uploads).get, client, history=lambda rows: [self.copy()])
    flag = self.flag(checked, "reused")
    self.assertEqual((flag["kind"], flag["severity"]), ("problem found", "high"))
    self.assertIn("is the same file as collection-logs/2026/10/15/collection1.jpg in disposal 900 (KL07BC1234, 01/09)",
                  flag["message"])
    self.assertEqual(flag["related"], [{"type": "disposal_log", "id": 900}])

def test_prints_are_handed_over_before_jev_is_asked(self):
    uploads, jev = make_trip()
    seen = []
    def history(rows):
        seen.append((len(jev.requests), sorted(name for _, name, _, _ in rows)))
        return []
    with jev.client() as client:
        disposal_check.check(disposal(uploads), files(uploads).get, client, history=history)
    self.assertEqual(seen, [(0, sorted(files(uploads)))])

def test_an_unavailable_image_has_no_print(self):
    uploads, jev = make_trip()
    stored = files(uploads)
    del stored["disposal/2026/10/15/unloading2.jpg"]
    rows = []
    with jev.client() as client:
        disposal_check.check(disposal(uploads), stored.get, client, history=lambda r: rows.extend(r) or [])
    self.assertNotIn("disposal/2026/10/15/unloading2.jpg", {name for _, name, _, _ in rows})

def test_without_a_vehicle_number_copies_are_still_found(self):
    uploads, jev = make_trip()
    with jev.client() as client:
        checked = disposal_check.check(disposal(uploads, vehicle={"vehicleNumber": ""}), files(uploads).get, client,
                                       history=lambda rows: [self.copy()])
    keys = {c["key"]: c["status"] for c in checked.analysis["verdict"]["checks"]}
    self.assertEqual((keys["reused"], keys["not_compared"]), ("fail", "review"))
```

In `test_without_a_vehicle_number_only_the_disposal_itself_is_checked`, `reused` is now expected. Change the `assertFalse` set to `{"same_moment", "overlap", "same_weighing"}`.

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m unittest tests.test_verifier.CompareTests -v`
Expected: FAIL with `unexpected keyword argument 'history'`.

- [ ] **Step 3: Implement**

- **Fingerprints** are made from the bytes already fetched in `check`, in the loop that builds `uploads[kind]`.
- **`proof_check.fingerprints`** decodes the image. That's a second decode per image, which is acceptable; don't change `analyze_trip`.
- **The copies list** goes from `check` into `compare_disposals.checks(..., copies=found)`.

- [ ] **Step 4: Run all tests and see them pass**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add verifier/disposal_check.py verifier/compare_disposals.py tests/test_verifier.py
git commit -m "feat: find reused images in any earlier disposal, any vehicle"
```

---

### Task 4: Wire the fingerprint history into the worker

**Files:**
- Modify: `verifier/database.py` (`PrintHistory`)
- Modify: `verifier/tasks.py:172` (`verify` passes a `PrintHistory` to `disposal_check.check`)
- Test: `tests/test_verifier.py` (`ServiceTests`)

**Interfaces:**
- Consumes: `save_prints` and `copies` (Task 2), and `check(..., history=)` (Task 3).
- Produces: `database.PrintHistory(pool, loop, request_id: str, disposal_id: int)`. It's callable as `history(rows) -> list[Copy]` from the worker thread. It runs `save_prints`, then `copies`, on `loop` through `asyncio.run_coroutine_threadsafe(...).result()`, as `SavedAnswers.__setitem__` does. The save commits before the lookup starts.

- [ ] **Step 1: Write the failing tests in `ServiceTests`**

```python
async def test_a_copy_of_another_vehicles_older_disposal_is_flagged(self):
    uploads = self.store_trip()
    await self.post(disposal(uploads, disposal_id=1000, vehicle={"id": 13, "vehicleNumber": "KL07BC1234"},
                             arrivalTime="2026-09-01T03:00:00.000Z"))
    await self.work()
    again = disposal(uploads, disposal_id=1001)
    await self.post(again)
    await self.work()
    flag = {f["code"]: f for f in self.gw.stored[again["requestId"]]["flags"]}["reused"]
    self.assertEqual(flag["related"], [{"type": "disposal_log", "id": 1000}])
    self.assertIn("(KL07BC1234, 01/09)", flag["message"])

async def test_a_copy_of_a_disposal_whose_check_ended_in_an_error_is_flagged(self):
    uploads = self.store_trip()
    first = disposal(uploads, disposal_id=1000)
    await database.save_request(self.svc.pool, first)
    await database.save_prints(self.svc.pool, first["requestId"],
                               [("loading", k, proof_check.fingerprints(v)["md5"], "00" * 8)
                                for k, v in files(uploads).items() if "collection" in k])
    await database.save_result(self.svc.pool, first["requestId"], tasks.make_result(first, "error", error={"code": "X", "message": "x"}))
    again = disposal(uploads, disposal_id=1001)
    await self.post(again)
    await self.work()
    self.assertIn("reused", {f["code"] for f in self.gw.stored[again["requestId"]]["flags"]})

async def test_two_vehicles_checked_at_once_the_later_finds_the_earlier(self):
    # Disposal 1000 (another vehicle) is still being checked: request and prints saved, no result yet.
    uploads = self.store_trip()
    first = disposal(uploads, disposal_id=1000, vehicle={"id": 13, "vehicleNumber": "KL07BC1234"})
    await database.save_request(self.svc.pool, first)
    await database.save_prints(self.svc.pool, first["requestId"],
                               [("bill", k, proof_check.fingerprints(v)["md5"], "00" * 8) for k, v in files(uploads).items()])
    again = disposal(uploads, disposal_id=1001)
    await self.post(again)
    await self.work()
    self.assertIn("reused", {f["code"] for f in self.gw.stored[again["requestId"]]["flags"]})

async def test_a_retried_request_never_matches_itself(self):
    uploads = self.store_trip()
    request = disposal(uploads)
    await self.post(request)
    await self.work()
    async with self.svc.pool.connection() as conn:  # as a retry would: the same prints saved again
        rows = await (await conn.execute("SELECT kind, name, md5, dhash FROM image_print WHERE request_id = %s",
                                          (request["requestId"],))).fetchall()
    await database.save_prints(self.svc.pool, request["requestId"], [tuple(r) for r in rows])
    self.assertEqual(self.gw.stored[request["requestId"]]["outcome"], "passed")
    self.assertEqual(await database.copies(self.svc.pool, DISPOSAL, [r[2] for r in rows]), [])
```

`test_an_edit_is_not_compared_with_its_own_earlier_request` must still pass unchanged.

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m unittest tests.test_verifier.ServiceTests -v`
Expected: the new tests FAIL with no `reused` flag, because the worker doesn't pass `history` yet.

- [ ] **Step 3: Implement `PrintHistory` and pass it in `tasks.verify`**

`PrintHistory` is built next to `SavedAnswers`, with `request["subject"]["id"]` as the disposal id, and passed as the `history=` keyword.

- [ ] **Step 4: Run all tests and see them pass**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: OK.

- [ ] **Step 5: Update the docs and commit**

- In README.md's verifier section:
  - `reused` now covers exact copies in any earlier disposal of any vehicle, using `image_print`;
  - look-alikes still cover the same vehicle, a day either side;
  - a disposal still being checked or ended in an error counts.
- In CLAUDE.md's verifier bullet, the comparisons sentence gets the same change. The `database.others` part stays for the other comparisons.

```bash
git add verifier/database.py verifier/tasks.py tests/test_verifier.py README.md CLAUDE.md
git commit -m "feat: save fingerprints before Jev and compare with every disposal's latest request"
```

---

### Task 5: `log_same_photo`: one photo in two collection logs

**Files:**
- Modify: `verifier/entry_checks.py` (`owners` returns every log a key is listed under; `log_same_photo`; `checks`)
- Modify: `proof_check/trip_rules.py` (`"log_same_photo": "real"` in `PART_OF`)
- Test: `tests/test_verifier.py` (`EntryCheckTests`)

**Interfaces:**
- Changes: `entry_checks.owners(request) -> dict[str, list[int | None]]`. It gives each collection photo's `s3Key` and every log id it is listed under, distinct and in order, with None for the disposal's own photo.
  - The callers `has_photos`, `photo_date`, `photos_to_place` and `collection_points` treat a photo as belonging to each log in its list.
  - In `photo_date`, a photo whose list holds only None keeps today's behaviour of "the disposal's logs".
- Produces: `entry_checks.log_same_photo(request: dict, images: dict[str, list[dict]]) -> list[dict]`. It returns `[]` without logs, and otherwise one pass check or one check per finding. The findings are:
  - **one key under two or more logs** → `"fail"`;
  - **two keys with the same md5 under different logs** → `"fail"`;
  - **a look-alike under different logs** → `"review"`. Use `proof_check.ImageIndex` with each log id as the "trip": `add(str(log_id), name, prints)`, then `matches(prints, str(own_log))`;
  - **the same md5 under two keys of the same log** → `"review"`.
- The label is `"Each collection log has its own photo"`. Set no `target` when two logs are named, and `target` to the log when one log is.

- [ ] **Step 1: Write the failing tests**

```python
def test_one_photo_listed_under_two_logs_is_a_problem(self):
    uploads, self.jev = make_trip()  # both collection photos under 45001 (the `disposal` helper)
    request = disposal(uploads, logs=[log(45001), log(45002, client={"name": "XYZ School"})])
    shared = next(e for e in request["evidence"] if e["category"] == "collection")  # collection1.jpg
    request["evidence"].append({**shared, "owner": {"type": "collection_log", "id": 45002}})
    flag = next(f for f in self.checked(request, uploads).flags if f["code"] == "log_same_photo")
    self.assertEqual((flag["kind"], flag["severity"]), ("problem found", "high"))
    self.assertIn("is the photo of both collection log 45001 (ABC Hotel, Main Kitchen) and 45002 (XYZ School, Main Kitchen)",
                  flag["message"])
    self.assertIn("Each pickup needs its own photo.", flag["message"])
```

Add four more in the same style:
- `test_the_same_file_under_two_keys_in_two_logs_is_a_problem`, using `make_trip(same=(("loading", 0, "loading", 1),))` with the second collection photo's evidence owner set to 45002: `"fail"`.
- `test_a_look_alike_in_two_logs_asks_a_person`, building the second photo with `coloured(COLOURS[0], 60)`: `"review"`, `"please check"`.
- `test_the_same_file_twice_in_one_log_asks_a_person`, the same `make_trip(same=…)` with both left under 45001 (the helper's default): `"review"`.
- `test_each_log_with_its_own_photo_passes`, with the status `"pass"`.

The existing `EntryCheckTests` must still pass. They are the regression check for the `owners` change.

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m unittest tests.test_verifier.EntryCheckTests -v`
Expected: FAIL with no `log_same_photo` flag.

- [ ] **Step 3: Implement the `owners` change, `log_same_photo`, and add it to `entry_checks.checks` after `has_photos`**

The detail for each finding:
- **Two logs:** `"{name} is the photo of both {log_name(a)} and {b's id and names}. Each pickup needs its own photo."`. Follow the test's wording: the second log reads `45002 (XYZ School, Main Kitchen)`.
- **A look-alike:** ends `"Compare them."`.
- **The same log:** `"{a} and {b} are the same file, both under {log_name}. Check the log has a photo of its own pickup."`.

- [ ] **Step 4: Run all tests and see them pass**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: OK.

- [ ] **Step 5: Update the docs and commit**

Add one README line to the verifier's entry checks list, and add `log_same_photo` to CLAUDE.md's "Entered data is checked in `verifier/entry_checks.py`" sentence.

```bash
git add verifier/entry_checks.py proof_check/trip_rules.py tests/test_verifier.py README.md CLAUDE.md
git commit -m "feat: flag one photo used for two collection logs (log_same_photo)"
```

---

### Task 6: `log_reused` and `log_repeated`: one pickup counted twice

**Files:**
- Modify: `verifier/database.py` (`other_logs`)
- Create: `verifier/migrations/009_request_logs_index.sql`: `CREATE INDEX verification_request_body ON verification_request USING gin (body jsonb_path_ops);`
- Modify: `verifier/entry_checks.py` (`log_reused`, `log_repeated`; `checks` takes `other_logs`)
- Modify: `verifier/disposal_check.py` (`check(..., other_logs=())` passed on to `entry_checks.checks`)
- Modify: `verifier/tasks.py` (`verify` fetches `other_logs` next to `others_of`)
- Modify: `proof_check/trip_rules.py` (`"log_reused": "real"`, `"log_repeated": "real"` in `PART_OF`)
- Test: `tests/test_verifier.py` (`EntryCheckTests`, `ServiceTests`)

**Interfaces:**
- Produces: `async def database.other_logs(pool, disposal_id: int, log_ids: list[int], day: date) -> list[tuple[int, dict]]`. It returns (other disposal id, collection log as sent) for every log in other disposals' latest requests that either has an id in `log_ids` (any date) or belongs to a request with `trip_day` within a day of `day`.
  - "Latest" includes requests not yet analysed and requests that ended in an error.
  - Match the ids with `body @> '{"reference":{"collectionLogs":[{"id":N}]}}'` for each N (ORed). The JSON shape is in `entry_checks.logs`.
- Produces: `entry_checks.log_reused(request: dict, other_logs: list[tuple[int, dict]]) -> list[dict]`. Each log id also on another disposal gives one `"fail"` check, aimed at the log (`target`), with `related` set.
- Produces: `entry_checks.log_repeated(request: dict, other_logs: list[tuple[int, dict]]) -> list[dict]`. It returns one `"review"` check per pair, with `related` for other disposals.
  - A pair is two logs with different ids, the same `client.name` and `collectionPoint.name` (trimmed, ignoring case), the same IST pickup day (`standards.ist_day(collectionDate)`), and the same set of `(materialName.lower(), float(quantity), unit.lower())`.
  - The logs are this request's own logs pairwise, plus this request's logs against `other_logs`.
  - Skip a log missing any of those fields or with no materials, and skip a pair already caught by `log_reused` for that disposal.
- Changes: `entry_checks.checks(request, images, radius=500, around=None, other_logs=())`. It adds `log_same_photo`, `log_reused` and `log_repeated`.

- [ ] **Step 1: Write the failing tests**

In `EntryCheckTests`, the pure tests:

```python
MATERIALS = [{"materialName": "PET", "quantity": 120, "unit": "kg"}]

def test_a_log_on_another_disposal_is_a_problem(self):
    checked = self.checked(logs=[log(45001, materials=MATERIALS)], other_logs=[(1000, log(45001, materials=MATERIALS))])
    flag = next(f for f in checked.flags if f["code"] == "log_reused")
    self.assertEqual((flag["kind"], flag["severity"]), ("problem found", "high"))
    self.assertEqual(flag["target"], {"type": "collection_log", "id": 45001})
    self.assertEqual(flag["related"], [{"type": "disposal_log", "id": 1000}])
    self.assertIn("is also on disposal 1000. One pickup can't be counted in two disposals.", flag["message"])

def test_the_same_pickup_under_another_id_asks_a_person(self):
    checked = self.checked(logs=[log(45001, materials=MATERIALS)], other_logs=[(1000, log(46001, materials=MATERIALS))])
    flag = next(f for f in checked.flags if f["code"] == "log_repeated")
    self.assertEqual(flag["kind"], "please check")
    self.assertIn("46001", flag["message"])

def test_two_pickups_the_same_day_with_other_weights_pass(self):
    other = log(46001, materials=[{"materialName": "PET", "quantity": 95, "unit": "kg"}])
    checked = self.checked(logs=[log(45001, materials=MATERIALS)], other_logs=[(1000, other)])
    self.assertNotIn("log_repeated", {f["code"] for f in checked.flags})

def test_a_log_without_materials_or_pickup_is_not_matched(self):
    bare = log(46001, collectionDate=None)
    checked = self.checked(logs=[log(45001), bare], other_logs=[(1000, log(46002))])
    self.assertNotIn("log_repeated", {f["code"] for f in checked.flags})
```

`self.checked` passes `other_logs` through to `disposal_check.check`. Extend the helper with an `other_logs=()` keyword.

In `ServiceTests`:
- `test_other_logs_finds_a_log_id_on_any_date_and_others_within_a_day`. Save three other disposals' requests directly:
  - one carrying log 45001 with a trip date two months back;
  - one with log 46001 a day before;
  - one with log 47001 three days before.

  Assert that `database.other_logs(pool, DISPOSAL, [45001], date(2026, 10, 15))` returns the first two and not the third, and never `DISPOSAL`'s own.
- `test_a_log_on_two_disposals_is_flagged_end_to_end`. Post two disposals with the same log id and different vehicles. The second's stored flags include `log_reused`.

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m unittest tests.test_verifier.EntryCheckTests tests.test_verifier.ServiceTests -v`
Expected: FAIL. There is no `other_logs` keyword yet, and `database.other_logs` isn't defined.

- [ ] **Step 3: Implement the migration, `other_logs`, the two checks and the wiring**

- **In `tasks.verify`,** fetch with `database.other_logs(svc.pool, request["subject"]["id"], [e["id"] for e in entry_checks.logs(request)], d.day)`. `others_of` already notes the trip day first.
- **The `log_reused` detail:** `"{Log_name} (picked up {dd/mm}) is also on disposal {i}. One pickup can't be counted in two disposals."`.
- **The `log_repeated` detail:** `"{log_name(a)} and collection log {b} (on disposal {i}, or "this disposal") are the same pickup: same client, collection point, day {dd/mm} and weights. Check it wasn't entered twice."`.

- [ ] **Step 4: Run all tests and see them pass**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: OK.

- [ ] **Step 5: Update the docs and commit**

- README.md's verifier entry checks get two lines.
- CLAUDE.md's entry-checks sentence gets the two keys, and a note that one pickup is never split over two disposals (the user, 3 Nov 2026).

```bash
git add verifier/migrations/009_request_logs_index.sql verifier/database.py verifier/entry_checks.py verifier/disposal_check.py verifier/tasks.py proof_check/trip_rules.py tests/test_verifier.py README.md CLAUDE.md
git commit -m "feat: flag one pickup counted in two disposals (log_reused, log_repeated)"
```

---

### Task 7: Measure look-alikes over all saved images

**Files:**
- Create: `tools/measure_look_alikes.py`
- Test: `tests/test_proof_check.py` (one test of the pairing function)

**Interfaces:**
- Produces: `tools.measure_look_alikes.pairs(items: list[tuple[str, str, str, int]], window) -> list[tuple]`. Each item is `(group, name, md5, dhash)`, where the group is a trip folder or a disposal. It returns look-alike pairs:
  - in different groups;
  - not exact copies;
  - with at most `LOOKS_ALIKE` differing bits;
  - neither hash `FEATURELESS`;
  - outside today's window, so `window(group_a, group_b)` is False.
- Run as `.venv/bin/python -m tools.measure_look_alikes [--db URL] [--trips trips/]`. It writes `eval/look-alikes-YYYY-MM-DD.html`, listing each pair with both images:
  - for `trips/`, `<img>` tags with relative paths to the local files;
  - for the database, the two `s3Key`s and disposal ids.

  It also prints the count of pairs inside and outside the window. No Jev and no S3. `eval/` is gitignored, so the report is never committed.
- The window:
  - **Database:** same `vehicle_number` and `trip_day` within a day, from `image_print` joined to `verification_request`.
  - **Trip folders:** the same parent date folder (`trips/inward-2026-10-23/…`) and the same vehicle in `trip.json`.

- [ ] **Step 1: Write the failing test**

```python
def test_look_alike_pairs_outside_the_window(self):
    from tools import measure_look_alikes as m
    items = [("a", "1.jpg", "x", 0b1011 << 20), ("b", "2.jpg", "y", 0b1010 << 20), ("c", "3.jpg", "x", 0b1011 << 20),
             ("d", "4.jpg", "z", 0)]
    found = m.pairs(items, window=lambda g, h: {g, h} == {"a", "c"})
    self.assertEqual([(p[0], p[1]) for p in found], [("1.jpg", "2.jpg")])
```

Neither "3.jpg" (an exact copy, inside the window) nor "4.jpg" (featureless) appears. Adjust the hash literals so they really have more than `FEATURELESS` bits set, and so "1" and "2" differ by at most 3 bits.

- [ ] **Step 2: Run it and see it fail**

Run: `.venv/bin/python -m unittest tests.test_proof_check -k look_alike -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `pairs` and the command-line tool**

`trips/` images are fingerprinted with `proof_check.ImageIndex.of_folders`. Read `ImageIndex.items` for (trip, name, md5, dhash).

- [ ] **Step 4: Run all tests, then run the tool on the dev data**

Run: `.venv/bin/python -m unittest discover -s tests -v`, expected OK.
Then run: `.venv/bin/python -m tools.measure_look_alikes --trips trips/ --db postgresql://verifier@127.0.0.1:5433/verifier`
Expected: a count line, and the report in `eval/`.

- [ ] **Step 5: Commit the tool (not the report) and report to the user**

```bash
git add tools/measure_look_alikes.py tests/test_proof_check.py
git commit -m "feat: tool to measure look-alike matches outside today's window"
```

Tell the user:
- the count of pairs outside the window;
- the report's path;
- that they mark which pairs are real copies, before any decision to widen look-alikes, which is not part of this plan.

Add a README line under the tools.
