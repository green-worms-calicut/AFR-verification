# Jev proof check

Checks the proof sent in for a waste-collection trip, using [TypeSafe's Jev model](https://docs.typesafe.ai). You type the trip's vehicle number and quantity in kilograms, add its **loading photos**, **unloading photos** and **weigh bills**, and it checks them against each other and against what you typed.

A trip runs **tare → loading → gross → unloading**: the empty vehicle is weighed (tare), loaded and photographed, weighed loaded (gross), and photographed again where it unloads. A trip weighed gross first is flagged.

**Photos** (each loading and unloading photo)
- Does it show a vehicle, with a GPS stamp printed on it?
- Is it taken directly, not a photo of a phone screen?
- Does at least one loading photo and one unloading photo clearly show the vehicle number you typed? A photo clearly showing another number is a problem. Rear plates are often hidden, so one clear photo of each kind is enough.

**Weigh bills**
- Is there a weighbridge slip? Handwritten notes and tallies are ignored.
- Is there a final slip, with both weights and the net? A first slip (only the tare) is used only for the vehicle number.
- Does every slip show the vehicle number you typed?
- Is the final slip's net weight the quantity you typed?
- Is the gross heavier than the tare? If not, the labels look swapped.

**Times**, from the final slip and the photos' GPS stamps
- Was the tare weighed first, at least 60 minutes before the gross?
- Was every loading photo taken between the tare and gross weighings?
- Was every unloading photo taken after the gross weighing, on the same day?

A photo's clock may be off the weighbridge's by up to 5 minutes (the clock slack). The 60 and 5 are defaults: you can change them for each run.

**Across trips**
- Was any image already used in another trip? The same file, or a look-alike copy.
- Is the vehicle on two trips at the same time? This is checked when a whole day is run with `run_trips.py`.

Every answer comes with a probability or a confidence, so unclear images go to a person instead of being trusted. **Nothing is rejected automatically.** The trip gets one verdict: **Accepted**, **Flagged: problem found** (a check found something wrong) or **Flagged: please check** (something was unclear). A person accepts or rejects every flagged trip.

> **Status:** tested offline against a fake Jev API, and run on one real day (48 trips from one plant, 19 Oct 2026) against the real Jev API: see [Real trips](#real-trips). The confidence thresholds are still placeholders.

## How it works

Jev is a "System One" model. A chat model writes text; Jev doesn't. You send it something to look at and a set of questions, and it answers each from options you define, with a probability for every option.

**Jev can't read text out of an image.** It can't tell you "the plate says KL05AF9341". So the vehicle number you type goes into the request next to the image, and Jev judges whether the plate, or the bill, shows *that* number. Spaces, hyphens and line breaks are ignored; every letter and digit must match. The quantity works the same way: it goes into a bill's request as `quantity_kg`, and Jev judges whether the net weight is that many kilograms. Leading zeros are ignored, so a bill printing `00480 Kg` matches `480`.

`check_proof.py` sends each image in its own request, all at the same time. Loading and unloading photos get the photo questions; weigh bills get the bill questions. An image added twice to a trip is asked about once.

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

**Each weigh bill:** 23 questions

| Question | Type | Answers |
|-|-|-|
| `orientation` | choice | as above |
| `weigh_bill` | yes/no | Is it a weighbridge slip? |
| `slip_kind` | choice | `one_weight` (a first slip), `both_weights` (a final slip), `not_shown` |
| `bill_number` | choice | Is the vehicle number on the bill the typed number: `different`, `same`, `not_shown` |
| `net_weight` | choice | Is the net weight the typed quantity in kg: `different`, `same`, `not_shown`. Only asked when a quantity is typed. |
| `gross_vs_tare` | choice | Which labelled weight is larger: `gross_heavier`, `tare_heavier`, `not_shown` |
| `gross_hour` … `gross_month` | choice | Date and time printed with the gross weight, or `not_shown` |
| `tare_hour` … `tare_month` | choice | Date and time printed with the tare weight, or `not_shown` |
| `in_hour` … `in_month` | choice | Time In and Date In (the first weighment), or `not_shown` |
| `out_hour` … `out_month` | choice | Time Out and Date Out (the second weighment), or `not_shown` |
| `first_weight` | choice | Which weight was taken at Time In: `tare`, `gross`, `not_shown` |

A time like `03:09` is two questions: which hour and which minute. Code puts the parts together and does all the comparing. Jev is unreliable at comparing dates and times itself.

Bills come in two layouts. Some print a date and time with each weight (the G W weighbridge); others print only a Time In and Time Out. Both sets of questions are asked at once, and `check_proof.weighings` uses whichever the final slip has. With Time In and Time Out, `first_weight` says which weight was taken first. If both layouts can be read, the surer one is used. Only the day and month are read, so a trip that runs past midnight or across New Year still works. Some weighbridges (Lakshmi, Bhavani and others) print neither layout clearly, and their trips go to a person for now.

Jev can lean towards the first option of a choice, so `same` is never listed first in the number and net weight questions.

If Jev finds an image isn't upright with at least 0.5 confidence (`ROTATE`), it is rotated and asked again. That cut-off is lower than the others because a wrong turn costs little: one more request, and unsure answers that go to a person anyway. Before sending, each image has its camera rotation flag applied and is scaled so its longest side is 1568 px, the largest Jev accepts.

### How the verdict is decided

All of this is in code (`check_proof.trip_checks`, `decide`, `verdict`), not in the questions:

| Check | Pass | Needs a person | Problem |
|-|-|-|-|
| Vehicle, GPS stamp, taken directly | every photo of the kind: yes ≥ 0.8 (screen: no) | any photo unsure | any photo clearly fails |
| Plate matches | at least one photo of the kind clearly `same` | none clear | any photo clearly `different` |
| Weighbridge slip, final slip | a final slip, clearly | no slip, or no clear final slip | – |
| Bills match | every slip `same` | any slip unclear | any slip `different` |
| Net weight (final slip) | `same` | `not_shown`, or no quantity typed | `different` |
| Gross heavier than tare (final slip) | `gross_heavier` | `not_shown` | `tare_heavier` |
| Weighing order | tare first | a time unreadable, or both the same | gross first |
| Tare to gross | at least 60 min apart | a time unsure | less |
| Loading photos | every one between tare and gross, give or take 5 min | a stamp time unreadable or unsure | any outside |
| Unloading photos | every one after gross, give or take 5 min | a stamp time unreadable or unsure | any before |
| Same day as gross | every unloading photo dated the gross weighing's day | a stamp date unreadable or unsure | any on another day |
| Images not used in other trips | none seen in another trip | a look-alike copy | the same file |
| One trip at a time (`run_trips.py`) | – | – | the vehicle is on another trip at the same time |

Any choice answer, or time or date reading, below 80% confidence also goes to a person.

The trip is **Flagged: problem found** if any check finds a problem, **Flagged: please check** if any check is unsure, and **Accepted** only when every check passes. Problems are listed first, and a timeline shows tare, loading, gross and unloading in time order, with any out of place in red.

The timing limits are `check_proof.Limits`: `min_gap_tare_first` (60) and `clock_slack` (5), in minutes. Each run can change them, on the page, the command line, or a trip's `trip.json`.

The 0.8 and 0.2 thresholds (`CONFIDENT`, `YES`, `NO` in `check_proof.py`), and the 0.5 rotation cut-off (`ROTATE`), are placeholders. Choose real ones with `run_trips.py` and `evaluate.py` (below).

## Setup

Needs Python 3.10 or later. The offline tests need Python 3.14, to read the SDK's compressed requests.

```bash
cd ~/Projects/jev
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
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

`app.py`, `check_proof.py`, `run_trips.py` and `evaluate.py` read the key from the keyring when they need it, and hand it straight to the TypeSafe client. A `TYPESAFE_API_KEY` environment variable, if set, is used instead, which is handy on a server without a keyring.

To replace the key, run the `store` command again. To remove it: `secret-tool clear service typesafe account api`.

Any program running under your user account can ask the keyring for the key while it's unlocked, as with any secret store. Never paste the key into a chat or a file inside this project.

## Usage

### Trip folders

A trip is a folder of images named by what they are, as in the inward log export:

```
trips/inward-2026-10-19/InwardLog-2026-10-19-2026-10-19/171773/
  collection1.jpg  collection2.jpg     loading photos (also loading*.jpg)
  unloading1.jpg   unloading2.jpg      unloading photos
  weighbill1.jpg   weighbill2.jpg      weigh bills (also bill*.jpg; .heic files that are really JPEGs work too)
  trip.json                            optional: {"vehicle_number": "KL57AB8815", "quantity": 1630, "clock_slack": 10}
```

`trip.json` holds what was typed for the trip. Without it, the hand-read vehicle number and net weight from a `truth.jsonl` in a parent folder stand in. `trips/` holds real records (phone numbers, driver names), so it isn't in git.

### Test page

```bash
.venv/bin/python app.py
```

Open the address it prints. It's `http://127.0.0.1:8765` unless that port is taken, in which case the app uses the next free one; choose a port with `--port`.

On the page:
1. Pick a trip from **Load a trip from trips/**, or type the vehicle number and quantity yourself.
2. Change the timing limits if you need to: they start at 60 and 5 minutes.
3. Add images to **Loading photos**, **Unloading photos** and **Weigh bills**: click **+ Add** or drop files on a box. **×** removes one.
4. Click **Check**.

The verdict lists problems first, then every check grouped by kind of image, with how sure Jev was, the timeline, and the limits it was checked with. Below it, each image shows what Jev read from it (stamp times, slip times), its tags (first or final slip, rotated, same image as another trip) and its own answers.

When the trip is flagged, **View all images** opens a full-size viewer that steps through every image (‹ › or the arrow keys), with the trip's flags and that image's answers beside it. Rotate and zoom in, then click **Accept** or **Reject**. The decision is only kept on the page; it isn't saved anywhere.

Images are compared with every image under `trips/`, so one already used in another trip is flagged. A trip loaded from `trips/` isn't compared with itself.

Changing the number, the quantity, a limit or an image clears the result, so a verdict always matches what's on screen. The server only listens on your own computer (`127.0.0.1`). The API key stays in the `app.py` process and is never sent to the browser.

### Command line

```bash
.venv/bin/python check_proof.py KL57AB8815 --trip trips/inward-2026-10-19/InwardLog-2026-10-19-2026-10-19/171773 --quantity 1630
.venv/bin/python check_proof.py KL57AB8815 --loading c1.jpg c2.jpg --unloading u1.jpg u2.jpg --bill slip1.jpg slip2.jpg \
    --quantity 1630 --min-gap-tare-first 45 --clock-slack 10 --others trips/   # this run's own limits, and flag reused images
.venv/bin/python check_proof.py KL57AB8815 --trip … --model jev-preview   # try Jev 1.14
```

Prints each image's answers and readings, then the verdict, timeline, limits and flags.

### Check a whole day: run_trips.py

```bash
.venv/bin/python run_trips.py trips/inward-2026-10-19              # asks Jev about every trip, saves the answers in eval/
.venv/bin/python run_trips.py trips/inward-2026-10-19 --cached     # re-checks with saved answers: free
.venv/bin/python run_trips.py trips/inward-2026-10-19 --only 171773 171496 --json eval/two.json
```

It prints a line per trip (verdict and first flags), the totals, and, where `truth.jsonl` has hand-read values, how often Jev read each kind of value right, with every wrong reading. Saved answers are reused whenever the image, questions, typed values and model are the same, so after changing a rule or a threshold, `--cached` re-checks the whole day without asking Jev again.

### Measure accuracy per image: evaluate.py

`evaluate.py` measures how often Jev is right on single labeled images, and shows what each confidence threshold would do. TypeSafe's guidance is to set thresholds per question, by the risk of acting on a wrong answer, from accuracy measured on your own data.

1. Copy `labels.example.csv` to `labels.csv`. Add one row per check: the image, its slot (`photo` or `bill`), the vehicle number typed in, for a bill the quantity typed in (kg), and the correct answers. `net_weight` can only be labeled on a row with a quantity. File paths are relative to the CSV. Leave a cell blank to skip it.

   **Add rows with a wrong vehicle number or quantity too**, labeled `different`. That's how you find out whether Jev catches a mismatch. Numbers one character off, like `9341` and `9314`, or `2570` and `2750`, are the hardest case; include plenty of them.

2. Ask Jev about every row (`.venv/bin/python evaluate.py labels.csv`), then re-score the saved answers for free with `--cached`.

The report shows accuracy per field, accuracy at each confidence level per group, what each candidate threshold would accept and how many of those would be wrong, and every mistake, most confident first.

### Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The tests use a fake Jev API that tells the test images apart by colour, so they need no key and cost nothing. They cover the API key, image sizing, what is sent with each image, rotation, each image's answers, every trip check (both bill layouts, first and final slips, ignored notes, one clear plate per kind, every photo's timing with the clock slack, gross first, the gap, past midnight, limits per run, missing images and quantity), reused images and look-alikes, a vehicle on two trips at once, saved answers, the question wording rules, `evaluate.py`, `run_trips.py`'s comparison, and the test page's server. The page itself was checked by hand in a browser.

## Real trips

`trips/inward-2026-10-19/` holds one plant's inward log for 19 Oct 2026: 48 trips, 42 with weigh bills. `truth.jsonl` there has values read by hand from every image of those 42 (plates, stamp times, slip weights and times), to measure Jev against. They were read by Claude subagents, not yet checked by a person.

What the day showed, from the hand-read values:
- 38 trips were weighed tare first, 3 gross first, and 1 unclear (two weighings 9 seconds apart).
- In every genuine trip the loading photos fall between tare and gross, and the unloading photos come after gross.
- Images were reused across trips: 171531 and 171566 are the same files; 171496 uses 171562's loading photos and 171531's unloading photos and bill; 171654 and 171655 share a bill and unloading photos.
- About half the trips use the G W weighbridge (a time printed with each weight). The rest use Lakshmi, Bhavani and other slips, which print times or labels differently.

What Jev did on that day (`run_trips.py`, `jev-latest`, about $0.10 for the 42 trips; saved answers re-check it for free):
- **Verdicts:** 11 accepted, 12 flagged as a problem, 19 to check. Every problem matches the hand-read values: the six trips with reused images (171496 also has loading photos taken before the tare weighing), the photos of a phone screen (171656), a loading photo without a GPS stamp (171538), 171493 (swapped gross and tare labels, 48 minutes between weighings), 171595 (gross and tare a second apart), and 171503 and 171535 (the same vehicle on both at once, tared a minute apart). No genuine trip was flagged as a problem.
- **Readings against the hand-read values:** photo stamp times 175 of 175 right, vehicle number on bills 73 of 73, plates 164 of 176 (each miss was Jev saying "same" where a person couldn't read the plate), slip kind 67 of 76, bill tare times 31 of 35 and gross times 30 of 35.
- **The 19 to check** are mostly slips whose layout isn't read yet (Lakshmi, Bhavani and others), and bills read below 0.8 confidence.
- **Sideways slips:** most slips are photographed sideways. Jev is sure a slip is sideways but close to a coin flip on which way, so the likelier way is tried and the next look decides between upright and upside down (`turn_for`). Before this, 33 of 43 sideways slips were read sideways, and bill times were right only about 3 in 4.
- **Wrong typed values:** ten real trips checked with the vehicle number's last two characters swapped were all flagged (9 as a problem, 1 to check), and with the quantity's digits swapped all 10 were flagged by the net weight. Jev twice said "same" to a wrong number on a single image, but the other images caught it, so a trip is safer than any single image.
- **AM/PM:** asked to convert "03:06:08 PM" itself, Jev often answered 03; the bill hour is now read as printed with AM or PM asked separately. Photo stamps are read right with the conversion, so they're unchanged.
- **Dates:** every unloading photo must be dated the same day as the gross weighing (by the calendar date, so a trip weighed just before midnight and unloaded just after is flagged too). On 19 Oct every unloading photo was.

## Cost

Jev charges **$0.042 per million input tokens**; output is free. Images count as input tokens at the same rate: a photo or slip is about 10,000 tokens, so a trip of 6 images is about $0.003, and the whole real day about $0.15. The page, the command line and `run_trips.py` show the exact count and cost.

| Model | Alias | Notes |
|-|-|-|
| `jev-1.13.0` | `jev-latest` | Stable. The default here. |
| `jev-1.14.0` | `jev-preview` | Newest, released 14 Oct 2026. |

## Limits

- **Image input is experimental.** TypeSafe calls it "dreamer-ready": fine for prototypes, not yet production quality. Accuracy and the request format can change between releases. Each request needs the `TypeSafe-Danger-Zone-Images: true` header, which the code sends.
- **Jev can't transcribe text.** It only picks from the options it's given. It can confirm the typed number, but it can't say what a different number is. Addresses, serial numbers and weights can't be read out.
- **Jev only sees pixels.** The API strips all file metadata, so GPS data stored in the photo file never reaches it. "GPS stamp" here means the stamp is printed on the photo. The real day's photos had no GPS data in their files anyway.
- **A GPS stamp isn't proof.** GPS camera apps can be set to any place and time. Where the photo was taken isn't checked yet.
- **Reused images** are found only among the trips under `trips/` (or the folder `run_trips.py` is given). A copy that was cropped or edited may not be found.
- **Net weight in kilograms.** The quantity is compared with the final slip's net weight as a number of kilograms.
- **Other weighbridge layouts.** Slips with only FIRST/SECOND weights and one print time (Lakshmi), or Date In/Out without gross and tare labels (Bhavani), aren't read yet; their trips go to a person.
- **Image size limits.** PNG or JPEG only (real HEIC isn't), 280–1568 px on each side after scaling, at most 16 MiB per file.

## Files

| Path | What it is |
|-|-|
| `check_proof.py` | The checks: questions, image preparation, rotation, the trip checks and verdict, reused images. Also a command-line tool. |
| `run_trips.py` | Checks every trip folder under a folder, flags a vehicle on two trips at once, and compares Jev with hand-read values. |
| `app.py` | Local server for the test page. Uses only Python's standard library. |
| `ui.html` | The test page. |
| `evaluate.py` | Measures accuracy on labeled single images and shows what each confidence threshold would do. |
| `labels.example.csv` | Example labels file for `evaluate.py`, for the sample images, including wrong-number rows. |
| `trips/` | Real trip folders and their hand-read `truth.jsonl`. Not in git. |
| `eval/` | Saved Jev answers and run reports. Not in git. |
| `mockups/` | The design mockup of the trip page, with real photos. Not in git. |
| `tests/test_offline.py` | Offline tests against a fake Jev API. |
| `playground/` | The questions and a state for each slot, as JSON, to paste into the [TypeSafe playground](https://console.typesafe.ai/playground). Generated from `check_proof.QUESTIONS`. |
| `samples/photo.png`, `samples/bill.png` | A low-resolution truck photo with a GPS stamp, and a weighment slip (photographed sideways), used by `evaluate.py`'s example labels. |
| `read_time.py`, `make_test_clock.py`, `samples/analog-*.jpg`, `samples/digital-*.jpg` | An earlier experiment: reading the time on a clock. |
| `jev-guide.html` | A longer illustrated guide to Jev, pricing, images and the first version of this project. Open it in a browser. |

## Ideas not built yet

- **Where photos were taken.** Check the GPS stamp's place against the collection point and the plant, and the travel time from loading to the gross weighing.
- **More slip layouts.** Read Lakshmi's two print times (first and final slip) and Bhavani's Date In/Out by which weight is heavier.
- **Records as input.** Take the vehicle number and quantity from the inward log export instead of `trip.json`.
- **Teach by example.** If answers are unsure on real images, describe each option with a few example images as well as words.

## References

- [TypeSafe docs](https://docs.typesafe.ai): [Models](https://docs.typesafe.ai/models), [Images](https://docs.typesafe.ai/images), [Choice](https://docs.typesafe.ai/primitives/choice), [Noul (yes/no)](https://docs.typesafe.ai/primitives/noul), [Confidence](https://docs.typesafe.ai/confidence), [Jev 1.13 known weaknesses](https://docs.typesafe.ai/model-jaggedness/jev-1.13)
- [Python SDK](https://docs.typesafe.ai/sdk/python) (`typesafe-sdk` 0.7.5)
