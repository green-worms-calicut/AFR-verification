# Jev proof check

Checks the proof sent in for a waste-collection trip, using [TypeSafe's Jev model](https://docs.typesafe.ai). You type the trip's vehicle number and quantity in kilograms (and, optionally, the plant's location and whether it's a Verra project trip), add its **loading photos**, **unloading photos** and **weigh bills**, and it checks them against each other and against what you typed.

A trip runs one of two ways, worked out from which weighing came first on the final slip:
- **Tare first: tare → loading → gross → unloading.** The empty vehicle is weighed (tare), loaded and photographed, weighed loaded (gross), and photographed again where it unloads.
- **Gross first: loading → gross → unloading → tare.** The vehicle is loaded and photographed, weighed loaded at the plant's weighbridge (gross), unloaded and photographed, then weighed empty (tare).

**Photos** (each loading and unloading photo)
- Does it show a vehicle, with a GPS stamp printed on it?
- Is it taken directly, not a photo of a phone screen?
- Does at least one loading photo and one unloading photo clearly show the vehicle number you typed? A photo clearly showing another number is a problem. Rear plates are often hidden, so one clear photo of each kind is enough.

**Weigh bills**
- Is there a weighbridge slip? Handwritten notes and tallies are ignored.
- Is every bill a photo of the printed slip, not of a computer or phone screen showing it?
- Is there a final slip, with both weights and the net? A first slip (only the tare) is used only for the vehicle number.
- Does every slip show the vehicle number you typed?
- Is the final slip's net weight the quantity you typed?
- Is the gross heavier than the tare? If not, the labels look swapped.

**Times**, from the final slip and the photos' GPS stamps
- Tare first: were the weighings at least 60 minutes apart, every loading photo taken between them, and every unloading photo after the gross weighing?
- Gross first: were the weighings at least 10 minutes apart, every loading photo taken before the gross weighing, and every unloading photo between the gross and tare weighings?
- Were both weighings on the same day? A tare from another day, more than 12 hours from the gross, is a stored one, not weighed on this trip; nearer, the trip ran across midnight.
- Was every unloading photo taken on the same day as the gross weighing?

A photo's clock may be off the weighbridge's by up to 5 minutes (the clock slack). The 60, 10 and 5 are defaults: you can change them for each run.

**Place**, when you give the plant's location
- Was the trip unloaded at the plant? At least one unloading photo's GPS stamp must place it within 500 m of the plant, and a photo stamped farther away is a problem. Positions are read to four decimals, so the distance is known to about 10 m. The plant's latitude and longitude are an input (`--plant "11.4404, 75.9420"`, a field on the page, or `"plant"` in `trip.json`), never written into the code, and the 500 m is a default you can change.

**Verra**, for a Verra project trip
- Is a material entered on the final slip (Material, Product, Item, or "Loaded with …")? A blank field, only Nil or a dash, or only EMPTY or LOAD, is a problem; a slip with no material field goes to a person. Which material it is isn't checked.
- Does the final slip carry the weighbridge's own ink stamp? No stamp is a problem, and so is a stamp clearly naming a different weighbridge from the slip's heading. Our company's own stamp, often on the slip too, doesn't count.

Only Verra project trips are checked for these, and Jev is only asked about them then: tick **Verra project** on the page, give `--verra`, or put `"verra": true` in `trip.json`.

**Across trips**
- Was any image already used in another trip? The same file, or a look-alike copy.
- Is the vehicle on two trips at the same time? This is checked when a whole day is run with `tools/check_trip_folders.py`. A trip's time is taken from its photos' GPS stamps, from the first collection photo to the last unloading photo. The weighings aren't used: most vehicles are weighed empty once in the morning, and that tare is printed on every slip of the day. Two trips must overlap by more than the clock slack.

These checks fall into three parts, each with its own verdict:
- **Trip is real:** did the trip happen as claimed? The plates in the photos and the number on every slip match the vehicle number you typed; the weighing order, the gap between weighings, the tare weighed on this trip and Time In with the tare; every photo's time; the photos and bills taken directly; unloading at the plant; and no image or trip a duplicate.
- **Documentation:** is the proof complete? Loading photos, unloading photos and bills all added; a vehicle and a GPS stamp in every photo; a final slip; its net weight the quantity you typed; and gross heavier than tare.
- **Verra**, only for a Verra project trip: the material and the weighbridge's stamp.

A missing kind of image is a gap in the documentation, but it also leaves nothing to show the trip is real, so the real part asks a person too (such as "Unloading plate matches: no unloading photos to compare").

Every answer comes with a probability or a confidence, so unclear images go to a person instead of being trusted. **Nothing is rejected automatically.** Each part gets a verdict: **Accepted**, **Flagged: problem found** (a check found something wrong) or **Flagged: please check** (something was unclear). A person accepts or rejects each flagged part. The trip as a whole has the worst of its parts' verdicts.

> **Status:** tested offline against a fake Jev API, and run on one real day (48 trips from one plant, 19 Oct 2026) against the real Jev API: see [Real trips](#real-trips). The confidence thresholds are still placeholders.

## How it works

Jev is a "System One" model. A chat model writes text; Jev doesn't. You send it something to look at and a set of questions, and it answers each from options you define, with a probability for every option.

**Jev can't read text out of an image.** It can't tell you "the plate says KL05AF9341". So the vehicle number you type goes into the request next to the image, and Jev judges whether the plate, or the bill, shows *that* number. Spaces, hyphens and line breaks are ignored; every letter and digit must match. The quantity works the same way: it goes into a bill's request as `quantity_kg`, and Jev judges whether the net weight is that many kilograms. Leading zeros are ignored, so a bill printing `00480 Kg` matches `480`.

`proof_check` sends each image in its own request, all at the same time. Loading and unloading photos get the photo questions; weigh bills get the bill questions. An image added twice to a trip is asked about once.

**Each photo:** 10 questions

| Question | Type | Answers |
|-|-|-|
| `orientation` | choice | `upright`, `top_points_right`, `top_points_left`, `upside_down` |
| `shows_vehicle` | yes/no | Does the photo show a motor vehicle? |
| `gps_stamp` | yes/no | Is a GPS stamp printed on the photo (place, coordinates, date and time)? |
| `screen_photo` | yes/no | Is it a picture of a screen rather than a photo taken directly? |
| `plate` | choice | `not_visible`, `partly_readable`, `readable` |
| `plate_number` | choice | Is the plate the typed number: `different`, `same`, `cannot_tell` |
| `stamp_hour`, `stamp_minute` | choice | Time in the GPS stamp: `00`–`23`, `00`–`59`, or `not_shown` |
| `stamp_day`, `stamp_month` | choice | Date in the GPS stamp: `01`–`31`, `01`–`12`, or `not_shown` |

**Unloading photos, when the plant's location is given:** 4 more questions, then 2 more requests

| Question | Type | Answers |
|-|-|-|
| `lat_degrees`, `long_degrees` | choice | Whole degrees of the stamp's latitude and longitude: `0`–`90`, `0`–`180`, or `not_shown` |
| `lat_decimals`, `long_decimals` | choice | The first two digits after the decimal point: `00`–`99`, or `not_shown` |
| `line` | box | Where the stamp's Lat/Long line is, asked of `jev-preview` (Box questions need Jev 1.14) |
| `lat_pick`, `long_pick` | choice | On that line cut out at full resolution: which of the 100 values starting with the two-decimal reading it is, such as `11.4400`–`11.4499`, or `not_shown` |

Jev can't read a number out, so a position is read in steps. The degrees and first two decimals are asked as Choices on the photo, like a time. Asked for the third and fourth digits the same way, Jev often gave the second one again, so instead code finds the stamp's Lat/Long line with a Box question, cuts it out of the full-resolution photo, and asks Jev which of 100 candidates (`11.4400` to `11.4499`) the line shows. Picking a whole value works where single digits don't (see [Real trips](#real-trips)).

Four decimals place the photo in a square about 11 m across (`11.4404, 75.9419`); code measures how far the plant is from the nearest point of that square, and more than `plant_radius` (500 m) is a problem. A trip passes only on a position picked surely to four decimals. When the pick is unsure, the two-decimal reading (a square about 1.1 km across) can still show a photo is away, read surely or by adding up Jev's probabilities over the likeliest readings (`chance_away`: 11.16 or 11.11 are both far from 11.44), but it can't show one is within 500 m, so that photo goes to a person. The plant's location is never sent to Jev, so the same answers serve any plant, and changing the plant or the distance needs no new request. Only the printed stamp is read: phone apps strip the GPS data from the file (4 of the day's 260 images kept it).

**Each weigh bill:** 32 questions, or 35 for a Verra project trip

| Question | Type | Answers |
|-|-|-|
| `orientation` | choice | as above |
| `weigh_bill` | yes/no | Is it a weighbridge slip? |
| `screen_photo` | yes/no | Is it a picture of a computer or phone screen showing the slip, rather than a photo of a printed paper slip? |
| `slip_kind` | choice | `one_weight` (a first slip), `both_weights` (a final slip), `not_shown` |
| `bill_number` | choice | Is the vehicle number on the bill the typed number: `different`, `same`, `not_shown` |
| `net_weight` | choice | Is the net weight the typed quantity in kg: `different`, `same`, `not_shown`. Only asked when a quantity is typed. |
| `gross_vs_tare` | choice | Which labelled weight is larger: `gross_heavier`, `tare_heavier`, `not_shown` |
| `gross_hour` … `gross_month` | choice | Date and time printed with the gross weight, or `not_shown` |
| `tare_hour` … `tare_month` | choice | Date and time printed with the tare weight, or `not_shown` |
| `in_hour` … `in_month` | choice | Time In and Date In (the first weighment), or `not_shown` |
| `out_hour` … `out_month` | choice | Time Out and Date Out (the second weighment), or `not_shown` |
| `printed_hour` … `printed_month` | choice | The one date and time printed at the top of the slip, not beside a weight, or `not_shown` |
| `first_weight` | choice | Is the first weight (at Time In, or First Wt) the smaller or the larger: `tare` (smaller), `gross` (larger), `not_shown` |
| `material` | choice | Is a material entered: `blank`, `empty_or_load` (only EMPTY or LOAD), `no_field` (no such label or Loaded with line), `filled`. Only for a Verra project trip. |
| `weighbridge_seal` | yes/no | Is an ink stamp of the weighbridge on it, apart from our company's (`our_company`)? Only for a Verra project trip. |
| `weighbridge_name` | choice | Does that stamp's name match the slip's heading: `different`, `same`, `not_shown`. Only for a Verra project trip. |

A time like `03:09` is two questions: which hour and which minute. Code puts the parts together and does all the comparing. Jev is unreliable at comparing dates and times itself.

Bills come in two layouts. Some print a date and time with each weight (the G W weighbridge); others print only a Time In and Time Out. Both sets of questions are asked at once, and `proof_check.weighings` uses whichever the final slip has. With Time In and Time Out, `first_weight` says which weight was taken first, by comparing the first weight with the second. That also reads slips that print First Wt and Second Wt with a date and time on each, such as Kuttiyadi's, which labels both times "Gross Wt. Date & Time". Some slips (Lakshmi's) print no time beside the weights, only one at the top of each slip: then the first slip (one weight) gives the first weighing's time and the final slip the second's, and `first_weight` says which was the tare. The surest layout that can be read is used. Only the day and month are read, so a trip that runs past midnight or across New Year still works.

Jev can lean towards the first option of a choice, so `same` is never listed first in the number, net weight and stamp questions.

Jev can't read a stamp's words out either, so it compares the weighbridge's stamp with the name printed in the slip's own heading. Our company's name (`proof_check.OUR_COMPANY`, set once in the code) goes into the bill's request as `our_company`, so our own stamp isn't taken for the weighbridge's. The stamp and material questions (`proof_check.VERRA`) are only asked for a Verra project trip, so other trips don't pay for them. Whether the stamp is there and whose name it shows are asked apart, as one question for both was often unsure. The stamp passes only when it is surely there and its name surely matches the heading; an unsure name goes to a person (on 22 Oct, 2 of 3 genuine stamps had unsure names).

If Jev finds an image isn't upright with at least 0.5 confidence (`ROTATE`), it is rotated and asked again. That cut-off is lower than the others because a wrong turn costs little: one more request, and unsure answers that go to a person anyway. Before sending, each image has its camera rotation flag applied and is scaled so its longest side is 1568 px, the largest Jev accepts.

**Each weigh bill is cut to its slip first.** A Box question (`proof_check.SLIP`, on Jev 1.14) finds the slip in the photo as uploaded, code cuts it out of the full-resolution photo with a little room around it, and the cut-out is turned upright and read. A slip photographed as a strip across a tall phone photo otherwise ends up in tiny print once scaled to 1568 px. On 21 final slips from 19 Oct (23 Oct test), times read on the cut-out were right 21 times and sure 15, against 20 and 13 on the whole photo; a Maliyekkal slip filling 19% of its photo went from wrong to right and 97% sure. On sideways photos the box takes the whole slip too, even when its confidence is low (0.32 on a Lakshmi sheet), so the cut is used whatever the confidence.

The minute of a bill time is asked as "the two digits right after the hour", not the seconds: slips often print seconds (07:02:14), and Jev's second guess for the minute was often the seconds (09:46:54 was once read as 09:54).

### How the verdict is decided

All of this is in code (`proof_check.trip_checks`, `decide_parts`, `verdict`), not in the questions. Which part each check is in is `proof_check.PART_OF`:

| Part | Check | Pass | Needs a person | Problem |
|-|-|-|-|-|
| Real | Taken directly | every photo of the kind: screen yes ≤ 0.6 | any photo unsure | any photo clearly of a screen |
| Real | Bill taken directly | every bill: screen yes ≤ 0.6 | any bill unsure | any bill clearly of a screen |
| Real | Loading / unloading plate matches | at least one photo of the kind clearly `same` | none clear, or no photos | any photo clearly `different` |
| Real | Bills match | every slip `same` | any slip unclear, or no bills | any slip `different` |
| Real | Weighing order | tare first or gross first, clearly | a time unreadable or unsure, both the same, or no bills | – |
| Real | Time In is the empty weighing (In/Out slips) | the lighter weight at Time In | unsure which is heavier | the heavier weight at Time In: In and Out entered swapped, or weighed loaded first |
| Real | Time between weighings (tare first) | at least 60 min apart | a time unsure | less |
| Real | Time between weighings (gross first) | at least 10 min apart | a time unsure | less |
| Real | Tare weighed on this trip | both weighings on one day, or on two days at most 12 h apart (across midnight) | a time unsure | on different days more than 12 h apart: a stored tare |
| Real | Loading photo times | every one between tare and gross (tare first) or before gross (gross first), give or take 5 min | a stamp time unreadable or unsure | any outside |
| Real | Unloading photo times | every one after gross (tare first) or between gross and tare (gross first), give or take 5 min | a stamp time unreadable or unsure | any outside |
| Real | Unloading on the weighing day | every unloading photo dated the gross weighing's day | a stamp date unreadable or unsure | any on another day |
| Real | At the plant (with `--plant`) | at least one unloading photo's stamp, picked surely to four decimals, places it within 500 m of the plant | no photo read surely to four decimals | any photo's stamp places it farther away: four decimals picked surely, or two read surely or at least 80% likely |
| Real | Images not used in other trips | none seen in another trip | a look-alike copy | the same file |
| Real | One trip at a time (`tools/check_trip_folders.py`) | – | – | the vehicle's photos for another trip overlap this trip's (more than the clock slack) |
| Docs | Loading photos, unloading photos, weigh bills | added | none added | – |
| Docs | Vehicle, GPS stamp | every photo of the kind: yes ≥ 0.8 | any photo unsure | any photo clearly fails |
| Docs | Weighbridge slip, final slip | a final slip, clearly | no slip, or no clear final slip | – |
| Docs | Net weight (final slip) | `same` | `not_shown`, or no quantity typed | `different` |
| Docs | Gross heavier than tare (final slip) | `gross_heavier` | – | `tare_heavier` (an unsure or `not_shown` answer gives no check) |
| Verra | Material entered (final slip) | `filled` | `no_field`, or no final slip | `blank`, or only EMPTY or LOAD |
| Verra | Weighbridge stamp (final slip) | yes ≥ 0.8, and its name surely `same` | the stamp or its name unsure, or no final slip | yes ≤ 0.2, or its name surely `different` |

Any choice answer, or time or date reading, below 80% confidence also goes to a person.

Each part is **Flagged: problem found** if any of its checks finds a problem, **Flagged: please check** if any is unsure, and **Accepted** only when every one passes. The trip as a whole is decided the same way from all its checks, so it has its worst part's verdict. A timeline shows the weighings and photos in time order, with any out of place in red.

Each flagged check gives its reason as a sentence a person can act on: what is wrong or unclear, in which image, and what to look at, such as "collection1.jpg was taken at 06:35, before the tare (empty) weighing at 06:53. Loading photos must come after it (5 min clock difference allowed)." An unsure answer says how sure Jev was ("Only 62% sure: …") and which value was unclear: the photo's stamp time, or the weighing times on the slip. Each check also names the images to look at (`images`), and checks flagged for the same reason share a `cause`: an unsure reading of the weighing times (`weigh_times`) also leaves the gap and every photo time unsure, and a missing kind of image (`loading_missing`, `bill_missing`) or final slip (`no_final`) leaves its other checks with nothing to read.

The limits are `proof_check.Limits`: `min_gap_tare_first` (60), `min_gap_gross_first` (10), `clock_slack` (5) and `max_overnight_gap` (720), in minutes, and `plant_radius` (500), in metres. Each run can change them on the command line or in a trip's `trip.json`, and all but `max_overnight_gap` on the page.

The 0.8 and 0.2 thresholds (`CONFIDENT`, `YES`, `NO` in `proof_check/trip_rules.py`), and the 0.5 rotation cut-off (`ROTATE`), are placeholders. Choose real ones with `tools/check_trip_folders.py` and `tools/measure_accuracy.py` (below).

## Setup

Needs Python 3.10 or later. The offline tests need Python 3.14, to read the SDK's compressed requests.

```bash
cd ~/Projects/jev
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The verifier service for gw_sales (`verifier/`, see Usage) also needs `requirements-verifier.txt`: FastAPI, Procrastinate on Postgres and boto3 for S3. Its server image installs only that. `requirements-dev.txt` adds moto, a fake S3, for the offline tests. Install it on a laptop:

```bash
.venv/bin/pip install -r requirements-dev.txt
```

### API key

Get a key from the TypeSafe console at <https://console.typesafe.ai/keys>.

The key is kept in your login keyring (GNOME Keyring), encrypted on disk and unlocked when you log in. It's never in a file, in this project, in your shell history or in your shell's environment. Store it once, in your own terminal:

```bash
secret-tool store --label="TypeSafe API key" service typesafe account api
```

It asks for a "Password": paste the key there. Nothing is shown as you type. To check it was saved, without printing it:

```bash
secret-tool lookup service typesafe account api >/dev/null && echo saved
```

The test page, `proof_check`, and the trip-folder and accuracy tools read the key from the keyring when they need it, and hand it straight to the TypeSafe client. A `TYPESAFE_API_KEY` environment variable, if set, is used instead, which is handy on a server without a keyring.

To replace the key, run the `store` command again. To remove it: `secret-tool clear service typesafe account api`.

Any program running under your user account can ask the keyring for the key while it's unlocked, as with any secret store. Never paste the key into a chat or a file inside this project.

## Usage

### Trip folders

A trip is a folder of images named by what they are, as in the inward log export:

```
trips/inward-2026-10-19/InwardLog-2026-10-19-2026-10-19/171773/
  collection1.jpg  collection2.jpg     loading photos (also loading*.jpg)
  unloading1.jpg   unloading2.jpg      unloading photos
  weighbill1.jpg   weighbill2.jpg      weigh bills (also bill*.jpg; .heic works too)
  trip.json                            optional: {"vehicle_number": "KL57AB8815", "quantity": 1630, "clock_slack": 10,
                                                  "plant": "11.4404, 75.9420", "plant_radius": 500, "verra": true}
```

`trip.json` holds what was typed for the trip. Without it, the hand-read vehicle number and net weight from a `truth.jsonl` in a parent folder stand in. `trips/` holds real records (phone numbers, driver names), so it isn't in git.

### Test page

```bash
.venv/bin/python -m tools.test_page.server
```

Open the address it prints. It's `http://127.0.0.1:8765` unless that port is taken, in which case the app uses the next free one; choose a port with `--port`.

On the page:
1. Pick a trip from **Load a trip from trips/**, or type the vehicle number and quantity yourself. Tick **Verra project** for a Verra project trip, to check the material and the weighbridge's stamp.
2. Change the timing limits if you need to: they start at 60 and 5 minutes. Type the plant's location (copied from Google Maps, such as `11.4404, 75.9420`) to check where unloading happened; the page remembers it.
3. Add images to **Loading photos**, **Unloading photos** and **Weigh bills**: click **+ Add** or drop files on a box. **×** removes one.
4. Click **Check**.

The verdict shows the trip as a whole, then each part (**Trip is real**, **Documentation**, and **Verra** for a Verra project trip) with its own verdict. A flagged part lists **Why it's flagged**: each reason in a sentence, problems first, with reasons that share a cause shown as one (an unsure weighing time, with the gap and photo times it leaves unsure beneath it), and a **Look at** button for each image it names, which opens it full size. The checks that passed fold under **N checks passed**, grouped by kind of image, with how sure Jev was. Below the parts are the timeline and the limits it was checked with. Below it, each image shows what Jev read from it (stamp times, slip times), its tags (first or final slip, rotated, same image as another trip) and its own answers.

Each flagged part has its own **Accept** and **Reject**; **Undo** takes a decision back. The trip shows **Rejected after review** once any part is rejected, and **Accepted after review** once every part is accepted. **View all images** opens a full-size viewer that steps through every image (‹ › or the arrow keys), with the reasons that name that image and its own answers beside it, and Accept and Reject for each part still to decide. The decisions are only kept on the page; they aren't saved anywhere.

Images are compared with every image under `trips/`, so one already used in another trip is flagged. A trip loaded from `trips/` isn't compared with itself.

Changing the number, the quantity, a limit or an image clears the result, so a verdict always matches what's on screen. The server only listens on your own computer (`127.0.0.1`). The API key stays in the server's process and is never sent to the browser.

### Command line

```bash
.venv/bin/python -m proof_check KL57AB8815 --trip trips/inward-2026-10-19/InwardLog-2026-10-19-2026-10-19/171773 --quantity 1630
.venv/bin/python -m proof_check KL57AB8815 --loading c1.jpg c2.jpg --unloading u1.jpg u2.jpg --bill slip1.jpg slip2.jpg \
    --quantity 1630 --min-gap-tare-first 45 --min-gap-gross-first 15 --clock-slack 10 --others trips/   # this run's own limits, and flag reused images
.venv/bin/python -m proof_check KL57AB8815 --trip … --plant "11.4404, 75.9420" --plant-radius 300   # unloaded at the plant?
.venv/bin/python -m proof_check KL57AB8815 --trip … --verra   # a Verra project trip: material and weighbridge stamp checked
.venv/bin/python -m proof_check KL57AB8815 --trip … --model jev-preview   # try Jev 1.14
```

Prints each image's answers and readings, then the verdict, timeline and limits, and each part's verdict and flags.

### Check a whole day: tools/check_trip_folders.py

```bash
.venv/bin/python -m tools.check_trip_folders trips/inward-2026-10-19              # asks Jev about every trip, saves the answers in eval/
.venv/bin/python -m tools.check_trip_folders trips/inward-2026-10-19 --cached     # re-checks with saved answers: free
.venv/bin/python -m tools.check_trip_folders trips/inward-2026-10-19 --only 171773 171496 --json eval/two.json
.venv/bin/python -m tools.check_trip_folders trips/inward-2026-10-19 --plant "11.4404, 75.9420"   # every trip without its own plant in trip.json
```

It prints a line per trip (its verdict, each part's, and its first flags), how many trips each part accepted, flagged or sent to a person, the totals, and, where `truth.jsonl` has hand-read values, how often Jev read each kind of value right, with every wrong reading. Saved answers are reused whenever the image, questions, typed values and model are the same, so after changing a rule or a threshold, `--cached` re-checks the whole day without asking Jev again.

### Measure accuracy per image: tools/measure_accuracy.py

`tools/measure_accuracy.py` measures how often Jev is right on single labeled images, and shows what each confidence threshold would do. TypeSafe's guidance is to set thresholds per question, by the risk of acting on a wrong answer, from accuracy measured on your own data.

1. Copy `examples/labels.example.csv` to `labels.csv`. Add one row per check: the image, its slot (`photo` or `bill`), the vehicle number typed in, for a bill the quantity typed in (kg), and the correct answers. `net_weight` can only be labeled on a row with a quantity. File paths are relative to the CSV. Leave a cell blank to skip it.

   **Add rows with a wrong vehicle number or quantity too**, labeled `different`. That's how you find out whether Jev catches a mismatch. Numbers one character off, like `9341` and `9314`, or `2570` and `2750`, are the hardest case; include plenty of them.

2. Ask Jev about every row (`.venv/bin/python -m tools.measure_accuracy labels.csv`), then re-score the saved answers for free with `--cached`.

The report shows accuracy per field, accuracy at each confidence level per group, what each candidate threshold would accept and how many of those would be wrong, and every mistake, most confident first.

### Verifier service (gw_sales)

`verifier/` runs the check as the service gw_sales calls for every new or edited disposal, as the Disposal Verification Contract sets out. It answers each signed request with 202, queues it, and later posts a signed result to the request's `callbackUrl`. Requests and results are kept in Postgres. A result is saved before it is sent, and sending is retried for up to 24 hours while gw_sales is down. A job runs every minute to pick up stalled or lost jobs, and a check or send that crashed outside its own error handling (a database blip while saving) or whose worker died on it: it is queued again, up to 3 times, after which the check is sent as `outcome: error` or the send is given up.

Each disposal is checked as one trip with `proof_check`. Its weigh bills, collection photos (as loading photos) and unloading photos are read from S3 by `s3Key`, and checked against the vehicle number and net quantity entered, with the facility's location as the plant. A disposal is a Verra project trip when one of its collection logs' clients has the Verra project as its default project. A photo that isn't in S3, or isn't an image, or whose `s3Key` isn't under `disposal/` or `collection-logs/` (it isn't read at all), asks a person (`image_unavailable`, docs part), and the rest are still checked. The read-only S3 key should allow only those two folders: `deploy/s3-read-policy.example.json` is the policy to compare the real one with. A check is tried again only when trying again can change it: Jev busy or down for an image (5xx, 429, a timeout), or S3 or the database not reached, 6 tries over about 25 minutes. Jev's answers are saved in Postgres as each image is answered (`jev_answer`), so a retry asks only the images not answered yet. If one image still isn't answered on the last try, it is flagged like a missing photo (`image_unavailable`) and the other checks are sent. If Jev answered for none, or refused every image, or refused our key or model, or the check hit our own bug, the disposal is sent at once as `outcome: error` with an alert. Checks flagged for the same reason become one flag, whose `code` is the check's key. Severity follows from the check's part: a problem with the trip being real is `high`, an unsure Verra check is `info`, everything else `recheck`. The result also carries `extractions` (each part's status, the weighing timeline, each image's readings) and the `standard` it was checked against. proof_check's whole result and what Jev cost are kept in our database.

Each disposal is also compared with what was kept for other disposals (`verifier/compare_disposals.py`). It only looks at its vehicle's other disposals with a trip within a day of its own date, each as last checked, and never at its own earlier requests (those are edits).
- An image also in such a disposal is a problem if it's an exact copy, and asks a person if it's a look-alike (`reused`). This applies whatever the quantity entered: photos borrowed from another trip come with a different quantity. On 23 Oct, 173186's collection photos were 172929's unloading photos (1870 against 1990 kg).
- A collection or unloading photo stamped within 5 minutes of one in another disposal asks a person (`same_moment`). Each other disposal is flagged once, by the first check that finds it, in the order reused, overlap, same moment, so `same_moment` is left for photos close in time whose trips don't overlap.
- A trip whose photos (first collection photo to last unloading photo) overlap another disposal's by more than the clock slack is a problem, whatever its quantity (`overlap`). The weighings aren't used: one morning tare is often printed on every slip of the day, which made normal second and third trips look like overlaps (about 40 wrong flags on 19 and 23 Oct).
- Within the disposal itself, one file used as both a collection and an unloading photo is a problem (`same_file`). This one is part of every trip's checks, so the command line, the folder runner and the test page catch it too.

What was entered in gw_sales is also checked against the photos (`verifier/entry_checks.py`). gw_sales sends every time in UTC, and a photo's GPS stamp shows the phone's time (IST), so each time is turned into IST first. Stamps are read without a year, so dates are compared by day and month. Each of these asks a person when it fails:
- Every collection log has a photo of its own. This flag is aimed at the log (`target`) (`collection_has_photos`, docs).
- Every collection photo is stamped on its log's pickup day (`collectionDate`). A collection photo of the disposal itself, linked to no log, must be stamped on the pickup day of one of the disposal's logs (`collection_photo_date`, real).
- No unloading photo is stamped more than 1 h 30 min after the end time entered. This is skipped without an end time (`unloading_too_late`, real).
- Every pickup is before the arrival entered. For a manual trip entry, the disposal's or the log's, only the dates are compared (`disposal_after_collections`, docs).
- For a log whose collection point's location gw_sales knows, at least one of its photos must be stamped within 500 m of it (`collection_point`, real; `collection_radius` in the standard). A clearly farther photo is a problem. Its photos' positions are read to four decimals, the same way as unloading photos at the plant: the plant check and this one share `proof_check.near_check`. This costs about $0.0005 more per collection photo, and only for logs with a known point.
- The final slip is from the weighbridge entered (`claims.weighbridge.name`). Bills are asked whether their heading names it, allowing for the name written differently (`weighbridge_matches`, docs). Another weighbridge asks a person, even when Jev is sure, since it may only be the wrong weighbridge entered; so does unsure. The question (`weighbridge_entered`) is only asked when a weighbridge is entered, so saved answers without one stay valid. `trip.json` can give one as `"weighbridge"`.

What the logs weighed by hand scale (kg, with bags counted apart) is recorded against the net weight entered, in `extractions.collectedVsWeighed`. It isn't flagged, so a tolerance can be chosen from real numbers.

These flags name the other disposals in `related`. Checks of one vehicle's disposals run one at a time, so two copies arriving together still see each other. An earlier disposal isn't flagged again when a later copy arrives: how to show it there is still to agree with gw_sales.

The limits a trip is checked by are those of its own day (`verifier/standards.py`, `STANDARDS`). To change a limit, add an entry from the day it applies rather than editing one. The Jev versions are pinned there too (`MODEL`, `BOX_MODEL`, `jev-1.14.0`), so answers don't change when an alias moves. Only upgrade them after testing.

A dev Postgres for it runs in Docker, on port 5433:

```bash
docker run -d --name jev-verifier-db --restart unless-stopped -p 127.0.0.1:5433:5432 \
  -v jev-verifier-pg:/var/lib/postgresql -e POSTGRES_USER=verifier -e POSTGRES_DB=verifier \
  -e POSTGRES_HOST_AUTH_METHOD=trust postgres:18
```

Settings come from the environment: `VERIFIER_DATABASE_URL` (`postgresql://verifier@127.0.0.1:5433/verifier` for the dev one), `VERIFIER_WEBHOOK_SECRET` (gw_sales signs its requests with it), `VERIFIER_CALLBACK_SECRET` (we sign our results with it), `VERIFIER_API_KEY` (sent as `X-API-Key` with our results), `GW_SALES_URL` (results are only posted under it), `VERIFIER_S3_BUCKET` (the photos; AWS credentials and region come the usual AWS ways), the TypeSafe key (`TYPESAFE_API_KEY`, or on a laptop the keyring, as above), optionally `VERIFIER_VERRA_PROJECT` (the project code that makes a disposal a Verra project trip; unset, no Verra checks), and while changing the webhook secret `VERIFIER_WEBHOOK_SECRET_NEXT` (the new one, accepted as well as the old until gw_sales has switched; then make it the only one). The secrets must be at least 32 characters (`openssl rand -hex 32`) and `GW_SALES_URL` must be `https://` (`http://localhost` or `http://127.0.0.1` only for tests), or the service won't start. Before deploying this, check the dev server's secrets: if one is shorter, agree a new one with gw_sales first. A request over 1 MB is refused (413) before its signature is checked.

```bash
.venv/bin/python -m verifier migrate      # apply the queue's and our migrations not applied yet
.venv/bin/python -m verifier worker       # run the jobs
.venv/bin/python -m verifier web --port 8080   # POST /v1/verifications, GET /health
```

Table changes go in a new numbered file in `verifier/migrations/` (`002_what_it_does.sql`), never by editing one already applied. `migrate` applies each new file once, in order, and records it in `verifier_migration`; one that fails is rolled back whole and not recorded. On a new database it installs Procrastinate's tables too. After upgrading Procrastinate, stop the workers and run `migrate`: its new migrations are applied the same way.

Log lines start with the requestId; one that needs a person starts with `ALERT`: a result gw_sales refused (401, 403, 413, 422), gave up on after 24 hours, a disposal that couldn't be checked, or the database unreachable.

### Running the verifier on a server

The verifier runs on one Linux server with Docker, from `deploy/docker-compose.yml`. That starts Postgres, runs the migrations, then starts the web receiver and the worker, and restarts any that stop. The image (`Dockerfile`) holds only `proof_check/`, `verifier/` and their requirements. `.dockerignore` keeps `trips/`, `eval/`, `mockups/` and any `.env` out.

1. Copy `deploy/dev.env.example` to `deploy/dev.env` on the server, fill it in, and make it root-only (`chmod 600`). It holds the secrets, so it is never committed (`deploy/*.env` is ignored) and never pasted anywhere. Make the passwords and secrets with `openssl rand -hex 32`.
2. `cd deploy && sudo docker compose --env-file dev.env up -d --build`. On a new database volume Postgres runs `deploy/db-init/01-app-role.sh`, which makes the app's own login, `verifier_app`, owner of the `verifier` database; the web receiver and the worker connect as it, not as the superuser. A server set up before this has its database made the old way: since the dev database holds no real data yet, recreate it with `sudo docker compose --env-file dev.env down -v` (this deletes the database volume and everything in it) and step 2 again.
3. Put an HTTPS proxy at the verifier's URL that forwards only `/v1/verifications` to `127.0.0.1:8080`, with a 1 MB request limit: `deploy/Caddyfile.example` does that with Caddy. Give gw_sales that URL.
4. Back up the database each night: add `deploy/backup.sh` to root's crontab (the line is at its top). It keeps 14 days of copies in `/var/backups/jev-verifier`, and its top also says how to restore one. The copies stay on the server, so copy them somewhere else too.

The photos are read from S3 with a read-only access key for `disposal/*` and `collection-logs/*`, because the server isn't on AWS.

Monitoring:
- **Logs:** one JSON object per line, with `requestId` when the line is about a request, and `"alert": true` on a line a person must act on (`sudo docker compose logs -f web worker`). In a terminal the lines are plain text.
- **Health:** `GET /health` says the process is up, which the container's health check uses. `GET /ready` says the database can be reached.
- **Worker heartbeat:** the worker's sweep notes the time every minute, and the web process logs an alert when it has heard nothing for 5 minutes (again every 30 minutes while it stays silent), since Docker doesn't restart a worker that hangs.
- **Daily summary:** every day at 00:05 IST the worker logs the day before: requests by reason, outcomes, flags by code, Jev's cost, results still waiting or given up, and requests that have no result. It logs an alert when any of those three isn't 0. `python -m verifier report [--day 2026-10-29]` prints the same summary.

### Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The tests use a fake Jev API that tells the test images apart by colour, so they need no key and cost nothing. They cover the API key, image sizing, what is sent with each image, rotation, each image's answers, every trip check (both bill layouts, first and final slips, ignored notes, the material entered on a Verra project trip, one clear plate per kind, every photo's timing with the clock slack, both weighing orders and their gaps, past midnight, a tare from another day, limits per run, missing images and quantity, unloading at or away from the plant given, unreadable or unsure positions, the weighbridge's stamp on a Verra project trip's final slip), which part each check is in and how each part is decided, reused images and look-alikes, a vehicle on two trips at once, saved answers, the question wording rules, `tools/measure_accuracy.py`, `tools/check_trip_folders.py`'s comparison, and the test page's server. The page itself was checked by hand in a browser.

`tests/test_verifier.py` covers the verifier service against a fake gw_sales: signatures, gw_sales' end-to-end cases (a signed result for a new disposal, an edit checked again, a repeated request checked once, wrong secrets refused), each answer gw_sales can give, retries for 24 hours, a checking error sent as `outcome: error`, and lost jobs picked up. It also covers disposals checked with `proof_check`: photos from a fake S3 (moto) answered by the offline tests' fake Jev, what is taken from a request, the standard by trip date, pinned models, unavailable photos, comparisons with other disposals, and how checks become flags. It also covers migrations. It needs the dev Postgres on port 5433 and makes a `verifier_test` database there. Without that Postgres, those tests are skipped.

## Real trips

`trips/inward-2026-10-19/` holds one plant's inward log for 19 Oct 2026: 48 trips, 42 with weigh bills. `truth.jsonl` there has values read by hand from every image of those 42 (plates, stamp times and positions, slip weights and times), to measure Jev against. They were read by Claude subagents (the unloading positions by Claude, checked against tesseract text recognition), not yet checked by a person.

`trips/inward-2026-10-23/` holds the same plant's log for 23 Oct 2026: 59 trips, all with weigh bills, hand-read the same way into its own `truth.jsonl`. Its bills also record the weighbridge's name, whether its own stamp is on the slip (`weighbridge_seal`) and the name on that stamp (`seal_name`), for the Verra checks. Lakshmi slips print one time per slip, so it is only `printed_at`, as on 19 Oct. The day has cases worth testing against:
- Reused images: 172897, 172899, 172900 and 172901 share their unloading photos and bill (one 310 kg run claimed four times, each with its own loading photos); 173253, 173254 and 173255 do the same with a 390 kg slip; 172929's unloading photos are 173186's loading photos, stamped before 173186's tare.
- Tares from the day before (172907, 172940, 173097), a morning tare used again for a second trip (172913 and 173177, 173255 and 173262), one gross-first trip (172904) and a two-day trip from Wayanad (173258, whose slip prints one time with no date).
- A final slip with material LOAD (173153), a "bill" that is a photo of the weighbridge's screen (172913), a loading photo with no stamp at all (173275), and stamps that name a place in Vietnam over Kerala coordinates (172930, 172945, 173284).

What the 19 Oct day showed, from the hand-read values:
- 38 trips were weighed tare first, 3 gross first, and 1 unclear (two weighings 9 seconds apart).
- In every genuine tare-first trip the loading photos fall between tare and gross, and the unloading photos come after gross. In the gross-first trips 171522 and 171562, the loading photos come before gross and the unloading photos between gross and tare, which are 25 and 16 minutes apart. The third, 171493, has its gross and tare labels swapped on the final slip.
- Images were reused across trips: 171531 and 171566 are the same files; 171496 uses 171562's loading photos and 171531's unloading photos and bill; 171654 and 171655 share a bill and unloading photos.
- About half the trips use the G W weighbridge (a time printed with each weight). The rest use Lakshmi, Bhavani and other slips, which print times or labels differently.

What Jev did on that day (`tools/check_trip_folders.py`, `jev-latest`, about $0.10 for the 42 trips; saved answers re-check it for free):
- **Verdicts:** 11 accepted, 12 flagged as a problem, 19 to check. Every problem matches the hand-read values: the six trips with reused images (171496 also has loading photos taken before the tare weighing), the photos of a phone screen (171656), a loading photo without a GPS stamp (171538), 171493 (swapped gross and tare labels, 48 minutes between weighings), 171595 (gross and tare a second apart), and 171503 and 171535 (the same vehicle on both at once, tared a minute apart). No genuine trip was flagged as a problem.
- **Readings against the hand-read values:** photo stamp times 175 of 175 right, vehicle number on bills 73 of 73, plates 164 of 176 (each miss was Jev saying "same" where a person couldn't read the plate), slip kind 67 of 76, bill tare times 31 of 35 and gross times 30 of 35.
- **The 19 to check** are mostly slips whose layout isn't read yet (Lakshmi, Bhavani and others), and bills read below 0.8 confidence.
- **Sideways slips:** most slips are photographed sideways. Jev is sure a slip is sideways but close to a coin flip on which way, so the likelier way is tried and the next look decides between upright and upside down (`turn_for`). Before this, 33 of 43 sideways slips were read sideways, and bill times were right only about 3 in 4.
- **Wrong typed values:** ten real trips checked with the vehicle number's last two characters swapped were all flagged (9 as a problem, 1 to check), and with the quantity's digits swapped all 10 were flagged by the net weight. Jev twice said "same" to a wrong number on a single image, but the other images caught it, so a trip is safer than any single image.
- **AM/PM:** asked to convert "03:06:08 PM" itself, Jev often answered 03; the bill hour is now read as printed with AM or PM asked separately. Photo stamps are read right with the conversion, so they're unchanged. Jev still sometimes splits the printed hour and the 24-hour one (02:45 PM read 02 at 0.54 and 14 at 0.46, 23 Oct 2026), so code adds together hour options that make the same time with the AM or PM read (`same_hour`).
- **Where unloading happened** (`--plant "11.4404, 75.9420"`, 22 Oct 2026, about $0.08 for the 88 unloading photos, three requests each): the hand-read stamps put 6 trips' unloading photos away from the plant, 2.9 to 41 km off (171493, 171513, 171518, 171595, 171673, 171699); every other unloading photo was within 51 m. The check flagged all 6 as a problem, five with the distance to the metre (such as "stamped at 11.4201, 75.9250, 2.9 km from the plant"), including 171513, which had been accepted. It passed 35 of the 36 trips at the plant and sent 1 to a person (neither photo picked surely); none was flagged as a problem. Jev picked 82 of 88 positions to four decimals and was sure of 77, all right.
- **How the four decimals were found:** asked for the third and fourth digits as single Choices, Jev got the longitude's third digit right 47 times in 88 (often giving the second digit again), 67 when told the digits before it, and no better on the Lat/Long line cut out with a Box question. Picking the whole value from 100 candidates on the cut-out line was right 173 times in 176. When the two-decimal reading was torn (11.44 or 11.40, mostly), candidates for both starts are offered; before that, two photos at the plant were flagged from a misread 11.40.
- **Dates:** every unloading photo must be dated the same day as the gross weighing (by the calendar date, so a trip weighed just before midnight and unloaded just after is flagged too). On 19 Oct every unloading photo was.
- **Weighbridge stamp** (22 Oct 2026, 24 requests on 3 other bills, not from this day): asked as one question (stamp there with the heading's name), the weighbridge's stamp was found surely on only 1 of the 3 slips in the full check (0.88; 0.79 and 0.45 on the others). Split into whether a stamp is there and whose name it shows (9 requests, $0.005), it was surely there on all 6 slips that had it (0.82 to 0.96), with its name sure on only 3 of them. Of the 3 copies with it painted over, one was a sure no (0.15) and two went to a person (0.53 and 0.58, where a signature or ink remained). Not yet tested: a stamp of a different weighbridge, a real unstamped slip, or this day's bills.
- **Gross first** (22 Oct 2026, re-checked from saved answers with `--plant`, gross to tare at least 10 min): 171522 and 171562 are no longer flagged for being weighed gross first; their weighings were 25 and 16 minutes apart (hand-read). Both still go to a person, because Jev read their final slips' times below 0.8 confidence (171522's tare as 08:50; hand-read 09:02). 171562 is still a problem for its reused images. 171595 (gross 11:41, tare 11:42 as read) is a problem: 1 minute apart, unloading at 12:28 after the tare, and 41 km from the plant. The verdict counts didn't change: 10 accepted, 14 problems, 18 to check.

## Cost

Jev charges **$0.042 per million input tokens**; output is free. Images count as input tokens at the same rate: a photo or slip is about 10,000 tokens, so a trip of 6 images is about $0.003, and the whole real day about $0.15. Each bill takes one more request to find its slip (about 4,300 tokens). With `--plant`, each unloading photo takes two more requests (finding and reading its Lat/Long line), about $0.001 more per photo. The page, the command line and `tools/check_trip_folders.py` show the exact count and cost.

| Model | Alias | Notes |
|-|-|-|
| `jev-1.13.0` | `jev-latest` | Stable. The default here. |
| `jev-1.14.0` | `jev-preview` | Newest, released 14 Oct 2026. |

## Limits

- **Box questions need `jev-preview`.** Finding a bill's slip and the stamp's Lat/Long line always uses Jev 1.14, whichever model the rest of the check uses.
- **Image input is experimental.** TypeSafe calls it "dreamer-ready": fine for prototypes, not yet production quality. Accuracy and the request format can change between releases. Each request needs the `TypeSafe-Danger-Zone-Images: true` header, which the code sends.
- **Jev can't transcribe text.** It only picks from the options it's given. It can confirm the typed number, but it can't say what a different number is. Addresses, serial numbers, weights and the words on a stamp can't be read out.
- **A stamp isn't proof either.** Rubber stamps can be copied. The check only shows a stamp with the right name is there.
- **Jev only sees pixels.** The API strips all file metadata, so GPS data stored in the photo file never reaches it. "GPS stamp" here means the stamp is printed on the photo. The real day's photos had no GPS data in their files anyway.
- **A GPS stamp isn't proof.** GPS camera apps can be set to any place and time. With the plant's location given, an unloading photo's printed position must be near it, but a faked stamp can show any position.
- **Reused images** are found only among the trips under `trips/` (or the folder `tools/check_trip_folders.py` is given). A copy that was cropped or edited may not be found.
- **Net weight in kilograms.** The quantity is compared with the final slip's net weight as a number of kilograms.
- **Other weighbridge layouts.** A Lakshmi-style trip (one print time per slip) needs both its first and final slip uploaded; with only the final slip, its times can't be read and it goes to a person. A layout not seen yet may not be read either.
- **Image size limits.** Jev takes PNG or JPEG, 280–1568 px on each side after scaling, at most 16 MiB per file. Every image is opened here and sent as JPEG, so real HEIC photos work (read with pillow-heif). A photo over 50 MP is read at 4000 px on its long side. One over 250 MP is refused from the size in its header, without being decoded, and like a cut-off file or a file that isn't an image it is flagged as unreadable.

## Files

```
proof_check/         the checks (a Python package; run it with `python -m proof_check`)
verifier/            the gw_sales verifier service (`python -m verifier`)
  migrations/        its database changes, numbered, applied once each
tools/               tools for working on the checks
  test_page/         the local test page: server and page
examples/            sample images, an example labels file, playground JSON
experiments/         earlier experiments (reading a clock), kept for reference
docs/                a longer illustrated guide
tests/               offline tests, no API key needed
trips/ eval/ mockups/   real trips, saved answers and reports, design mockups: not in git
```

| Path | What it is |
|-|-|
| `proof_check/questions.py` | The questions Jev is asked about each image, and the Box questions that find a slip or a stamp's position line. |
| `proof_check/images.py` | Reading, sizing and cutting images, the images of a trip folder, and fingerprints to find an image used again. |
| `proof_check/trip_rules.py` | Every check and which part it is in, the limits, and the (placeholder) confidence thresholds. |
| `proof_check/inputs.py` | What people type in, checked and made uniform: vehicle number, quantity, plant, limits, switches. |
| `proof_check/verdict.py` | Each part's verdict and the trip's, and the trip's timeline. |
| `proof_check/ask_jev.py` | The API key, asking Jev about each image (turning, cutting the slip, reading the position), saved answers, and checking a whole trip (`analyze_trip`). |
| `proof_check/__main__.py` | The command line for one trip. |
| `verifier/api.py` | Receives gw_sales' requests (`POST /v1/verifications`) and answers `GET /health`. |
| `verifier/tasks.py` | The queue's jobs: check a request, post its result, recover stalled or lost jobs. |
| `verifier/disposal_check.py` | Checks a disposal with `proof_check` and turns its checks into gw_sales flags. |
| `verifier/compare_disposals.py` | Compares a disposal with the same vehicle's other disposals: reused images, overlapping trips (by photo times), the same moment. |
| `verifier/entry_checks.py` | Checks what was entered in gw_sales against the photos: a photo for every collection log, pickup days, the end time, the arrival after the pickups, and collected vs weighed (recorded only). |
| `verifier/database.py` | Requests, the outbox of results, and what disposals are compared by. |
| `verifier/migrate.py`, `verifier/migrations/` | Applies the database changes (ours and the queue's), each once, in order. |
| `verifier/s3_photos.py` | Reads the photos from S3. |
| `verifier/standards.py` | The limits by trip date and the pinned Jev versions. |
| `verifier/signatures.py` | Signs and checks requests and results (HMAC-SHA256). |
| `verifier/settings.py` | The service's settings, from the environment. |
| `verifier/logs.py` | Log lines: JSON on a server, with the request and alerts in their own fields. |
| `Dockerfile`, `.dockerignore`, `deploy/` | The verifier's image, and how it runs on a server (`docker-compose.yml`, `dev.env.example`). |
| `tools/check_trip_folders.py` | Checks every trip folder under a folder, flags a vehicle on two trips at once, and compares Jev with hand-read values. |
| `tools/measure_accuracy.py` | Measures accuracy on labeled single images and shows what each confidence threshold would do. |
| `tools/test_page/server.py`, `page.html` | The local test page. The server uses only Python's standard library. |
| `examples/labels.example.csv` | Example labels for `tools/measure_accuracy.py`, including wrong-number rows. |
| `examples/images/` | A low-resolution truck photo with a GPS stamp and a weighment slip (photographed sideways), for the example labels and tests. |
| `examples/playground/` | The questions and a state for each slot, as JSON, to paste into the [TypeSafe playground](https://console.typesafe.ai/playground). Generated from `proof_check.QUESTIONS`. |
| `experiments/clock_reading/` | An earlier experiment: reading the time on a clock (`read_time.py`, `make_test_clock.py`, sample clocks). |
| `docs/jev-guide.html` | A longer illustrated guide to Jev, pricing, images and the first version of this project. Open it in a browser. |
| `tests/test_proof_check.py` | Offline tests of the checks, the tools and the test page, against a fake Jev API. |
| `tests/test_verifier.py` | Offline tests of the verifier service against a fake gw_sales, a fake S3 and the fake Jev. |
| `trips/` | Real trip folders and their hand-read `truth.jsonl`. Not in git. |
| `eval/` | Saved Jev answers and run reports. Not in git. |
| `mockups/` | The design mockup of the trip page, with real photos. Not in git. |

## Ideas not built yet

- **Where loading photos were taken.** Check loading photos against each collection point, the same way unloading photos are checked against the plant, and the travel time from loading to the gross weighing.
- **More slip layouts.** Read Lakshmi's two print times (first and final slip) and Bhavani's Date In/Out by which weight is heavier.
- **Records as input.** Take the vehicle number and quantity from the inward log export instead of `trip.json`.
- **Teach by example.** If answers are unsure on real images, describe each option with a few example images as well as words.

## References

- [TypeSafe docs](https://docs.typesafe.ai): [Models](https://docs.typesafe.ai/models), [Images](https://docs.typesafe.ai/images), [Choice](https://docs.typesafe.ai/primitives/choice), [Noul (yes/no)](https://docs.typesafe.ai/primitives/noul), [Confidence](https://docs.typesafe.ai/confidence), [Jev 1.13 known weaknesses](https://docs.typesafe.ai/model-jaggedness/jev-1.13)
- [Python SDK](https://docs.typesafe.ai/sdk/python) (`typesafe-sdk` 0.7.5)
