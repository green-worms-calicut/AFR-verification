# Jev proof check

Checks the proof sent in for a waste-collection trip against the trip's vehicle number, using [TypeSafe's Jev model](https://docs.typesafe.ai). You type the vehicle number and the trip's arrival and end time, add two images, and it answers:

**Vehicle photo**
- Does it show a vehicle?
- Is a GPS stamp printed on it?
- Can the number plate be read?
- Is the plate the vehicle number you typed?

**Weigh bill**
- Is it a weighbridge bill?
- Is the vehicle number printed on it the one you typed?

**The trip**
- Are the photo and the bill from the same date?
- Was the photo taken between the arrival and end time you typed? An end earlier than the arrival means the trip ran past midnight.

The bill's time in and time out aren't asked about: the vehicle can be weighed before it is loaded, so the photo needn't fall between them. Only the bill's date is read.

Every answer comes with a probability or a confidence, so unclear images go to a person instead of being trusted. The trip gets one verdict: **Accepted**, **Rejected** or **Needs a person to check**.

> **Status:** tested offline against a fake Jev API. Not yet run against the real Jev API, which needs an API key.

## How it works

Jev is a "System One" model. A chat model writes text; Jev doesn't. You send it something to look at and a set of questions, and it answers each from options you define, with a probability for every option.

**Jev can't read text out of an image.** It can't tell you "the plate says KL05AF9341". So the vehicle number you type goes into the request next to the image, and Jev judges whether the plate, or the bill, shows *that* number. Spaces, hyphens and line breaks are ignored; every letter and digit must match.

`check_proof.py` sends each image in its own request, with the typed number. The two requests run at the same time.

**Vehicle photo:** 9 questions

| Question | Type | Answers |
|-|-|-|
| `orientation` | choice | `upright`, `top_points_right`, `top_points_left`, `upside_down` |
| `shows_vehicle` | yes/no | Does the photo show a motor vehicle? |
| `gps_stamp` | yes/no | Is a GPS stamp printed on the photo (place, coordinates, date and time)? |
| `plate` | choice | `not_visible`, `partly_readable`, `readable` |
| `plate_number` | choice | Is the plate the typed number: `different`, `same`, `cannot_tell` |
| `stamp_hour`, `stamp_minute` | choice | Time in the GPS stamp: `00`–`23`, `00`–`59`, or `not_shown` |
| `stamp_day`, `stamp_month` | choice | Date in the GPS stamp: `01`–`31`, `01`–`12`, or `not_shown` |

**Weigh bill:** 5 questions

| Question | Type | Answers |
|-|-|-|
| `orientation` | choice | as above |
| `weigh_bill` | yes/no | Is it a weighbridge bill (weighment slip)? |
| `bill_number` | choice | Is the vehicle number on the bill the typed number: `different`, `same`, `not_shown` |
| `bill_day`, `bill_month` | choice | Date on the bill, or `not_shown` |

A time like `03:09` is two questions: which hour and which minute. Code puts the parts together and does all the comparing: same date, and the photo time between the typed arrival and end time. Jev is unreliable at comparing dates and times itself.

Jev can lean towards the first option of a choice, so `same` is never listed first in the number questions.

If Jev finds an image isn't upright with at least 0.5 confidence (`ROTATE`), it is rotated and asked again. That cut-off is lower than the others because a wrong turn costs little: one more request, and unsure answers that go to a person anyway. The sideways sample bill was found turned at 0.56. That's two requests for that image. Before sending, each image has its camera rotation flag applied and is scaled so its longest side is 1568 px, the largest Jev accepts.

### How the verdict is decided

All of this is in code (`check_proof.checks_for`, `trip_checks`, `verdict`), not in the questions:

| Answer | Pass | Needs a person | Fail |
|-|-|-|-|
| yes/no question | yes ≥ 0.8 | between 0.2 and 0.8 | yes ≤ 0.2 |
| `plate` | `readable` | `partly_readable` | `not_visible` |
| `plate_number` | `same` | `cannot_tell` | `different` |
| `bill_number` | `same` | `not_shown` | `different` |
| Same date | yes | a date not shown | no |
| Photo during the trip (typed arrival to end) | yes | photo time not shown, or arrival and end not typed | no |

Any choice answer, or time or date reading, below 80% confidence also goes to a person.

The trip is **Rejected** if any check fails, **Needs a person to check** if any check is unsure, and **Accepted** only when every check on both images passes.

The 0.8 and 0.2 thresholds (`CONFIDENT`, `YES`, `NO` in `check_proof.py`), and the 0.5 rotation cut-off (`ROTATE`), are placeholders. Choose real ones with `evaluate.py` (below).

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

`app.py`, `check_proof.py` and `evaluate.py` read the key from the keyring when they need it, and hand it straight to the TypeSafe client. A `TYPESAFE_API_KEY` environment variable, if set, is used instead, which is handy on a server without a keyring.

To replace the key, run the `store` command again. To remove it: `secret-tool clear service typesafe account api`.

Any program running under your user account can ask the keyring for the key while it's unlocked, as with any secret store. Never paste the key into a chat or a file inside this project.

## Usage

### Test page

```bash
.venv/bin/python app.py
```

Open the address it prints. It's `http://127.0.0.1:8765` unless that port is taken, in which case the app uses the next free one; choose a port with `--port`.

On the page:
1. Type the vehicle number, and the trip's arrival and end time.
2. Add the vehicle photo and the weigh bill (JPEG or PNG): click a slot or drop a file on it. Or click **Try the samples**.
3. Click **Check**.

The verdict lists every check with its answer and how sure Jev was. Below it, each image shows the times and dates read and the image as it was actually sent. You can check one image on its own; the verdict waits for both, unless that image already fails. The arrival and end time are optional, but without them the photo time can't be checked and the trip goes to a person.

When the verdict is **Needs a person to check**, it shows the images full size with what to look at beside them: the unsure checks, what Jev read and what was typed in. You can rotate and zoom in, then click **Accept** or **Reject**. The verdict changes to **Accepted after review** or **Rejected after review**, and **Undo** takes it back. The decision is only kept on the page; it isn't saved anywhere. Any image's **View full size** opens the same viewer.

Changing the number, a time or an image clears the result, so a verdict always matches what's on screen.

The server only listens on your own computer (`127.0.0.1`). The API key stays in the `app.py` process and is never sent to the browser.

### Command line

```bash
.venv/bin/python check_proof.py KL05AF9341 --photo samples/photo.png --bill samples/bill.png --arrival 03:00 --end 09:00
.venv/bin/python check_proof.py KL05AF9314 --photo samples/photo.png    # wrong number: should be rejected
.venv/bin/python check_proof.py KL05AF9341 --photo samples/photo.png --arrival 22:00 --end 05:00   # trip past midnight
.venv/bin/python check_proof.py KL05AF9341 --bill samples/bill.png --model jev-preview   # try Jev 1.14
```

Prints each check with its probability or confidence, the times and dates read, the tokens used, and the verdict.

### Measure accuracy and choose thresholds

`evaluate.py` measures how often Jev is right on your own images, and shows what each confidence threshold would do. TypeSafe's guidance is to set thresholds per question, by the risk of acting on a wrong answer, from accuracy measured on your own data.

1. Copy `labels.example.csv` to `labels.csv`. Add one row per check: the image, its slot (`photo` or `bill`), the vehicle number typed in, and the correct answers. File paths are relative to the CSV. Leave a cell blank to skip it.

   ```csv
   file,slot,vehicle_number,shows_vehicle,gps_stamp,plate,plate_number,weigh_bill,bill_number,bill_date,photo_time,photo_date
   trips/0412-photo.jpg,photo,KL05AF9341,yes,yes,readable,same,,,,04:09,15/10/2026
   trips/0412-photo.jpg,photo,KL05AF9314,,,,different,,,,,
   trips/0412-bill.jpg,bill,KL05AF9341,,,,,yes,same,15/10/2026,,
   ```

   **Add rows with a wrong vehicle number too**, labeled `different`. That's how you find out whether Jev catches a mismatch. Numbers one character off, like `9341` and `9314`, are the hardest case; include plenty of them.

2. Ask Jev about every row. This costs tokens, and saves the answers to `eval/<model>.jsonl`:

   ```bash
   .venv/bin/python evaluate.py labels.csv
   ```

3. Re-score the saved answers without asking Jev again, for example after fixing a label:

   ```bash
   .venv/bin/python evaluate.py labels.csv --cached
   ```

The report has four parts:
- accuracy for each field;
- accuracy at each confidence level, for each group: image (vehicle, GPS stamp, weigh bill), plate, vehicle number, times and dates;
- for each candidate threshold, how many answers would be accepted automatically, how many of those would be wrong, and how many would go to a person;
- every mistake, most confident first.

For a yes/no answer, the confidence in the report is how far it leans (the probability of yes for a yes, of no for a no), so a threshold of 0.8 matches `YES` and `NO` at 0.8 and 0.2. Start with 20–50 real photo-and-bill pairs; the report warns while a group has fewer than 30 answers.

### Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The tests use a fake Jev API, so they need no key and cost nothing. They check:
- reading the API key from the environment or the keyring;
- vehicle number clean-up and image resizing;
- that the typed number is sent with each image;
- the rotate-and-ask-again step;
- each check's pass, fail and needs-a-person outcomes;
- reading the typed arrival and end time, including a trip past midnight;
- the trip verdict: accepted, wrong plate, wrong bill number, different dates, photo outside the trip, bill times not asked, times not typed, unsure answers, one image only;
- the question wording rules;
- the evaluation scoring and label checks;
- the test page's server (the full-size viewer and the Accept and Reject buttons were checked by hand in a browser, not by these tests).

## Cost

Jev charges **$0.042 per million input tokens**; output is free. Images count as input tokens at the same rate. TypeSafe hasn't published how many tokens an image uses, so the page and the command line show the exact count and cost for every check.

| Model | Alias | Notes |
|-|-|-|
| `jev-1.13.0` | `jev-latest` | Stable. The default here. |
| `jev-1.14.0` | `jev-preview` | Newest, released 14 Oct 2026. |

## Limits

- **Image input is experimental.** TypeSafe calls it "dreamer-ready": fine for prototypes, not yet production quality. Accuracy and the request format can change between releases. Each request needs the `TypeSafe-Danger-Zone-Images: true` header, which the code sends.
- **Number matching isn't measured yet.** Whether Jev reliably tells `KL05AF9341` from `KL05AF9314` on real photos is unknown until `evaluate.py` has run on your images. Until then, treat a "same" answer as a strong hint, not proof.
- **Jev can't transcribe text.** It only picks from the options it's given. It can confirm the typed number, but it can't say what a different number is. Addresses, serial numbers and weights can't be read out.
- **Jev only sees pixels.** The API strips all file metadata, so GPS data stored in the photo file never reaches it. "GPS stamp" here means the stamp is printed on the photo.
- **A GPS stamp isn't proof.** GPS camera apps can be set to any place and time. Check the GPS and capture time in the original photo file on your side as well.
- **Use original photos.** Screenshots and photos forwarded through WhatsApp lose detail and their GPS data. The sample images are low-resolution screenshots, about 350 × 590 px.
- **Bill layout.** Only the bill's date and vehicle number are asked about. A slip layout very different from the sample may need different wording.
- **Image size limits.** PNG or JPEG only, 280–1568 px on each side, at most 16 MiB per file. Convert HEIC and WebP first.

## Files

| Path | What it is |
|-|-|
| `check_proof.py` | The checks: questions, image preparation, rotation, and the pass, fail or review decisions. Also a command-line tool. |
| `app.py` | Local server for the test page. Uses only Python's standard library. |
| `ui.html` | The test page. |
| `evaluate.py` | Measures accuracy on labeled images and shows what each confidence threshold would do. |
| `labels.example.csv` | Example labels file for `evaluate.py`, for the sample images, including wrong-number rows. |
| `eval/` | Saved Jev answers from `evaluate.py`, one file per model. Not in git. |
| `tests/test_offline.py` | Offline tests against a fake Jev API. |
| `playground/` | The questions and a state for each slot, as JSON, to paste into the [TypeSafe playground](https://console.typesafe.ai/playground). Generated from `check_proof.QUESTIONS`. |
| `samples/photo.png`, `samples/bill.png` | A truck photo with a GPS stamp, and the weighment slip from the same trip (photographed sideways). |
| `read_time.py` | An earlier experiment: reads the time on an analog or digital clock. |
| `make_test_clock.py` | Draws test clocks at a chosen time, for `read_time.py`. |
| `samples/analog-*.jpg`, `samples/digital-*.jpg` | Test clocks made by `make_test_clock.py`. |
| `jev-guide.html` | A longer illustrated guide to Jev, pricing, images and the first version of this project. Open it in a browser. |

## Expected results for the samples

Read by hand from the images, to compare against Jev's answers:

| Image | Checks | Time | Date |
|-|-|-|-|
| `samples/photo.png` | vehicle, GPS stamp, plate readable: `KL 05 AF 9341` | 04:09 | 15/10/2026 |
| `samples/bill.png` | weighbridge bill, Truck No `KL05AF9341`; sideways (needs a 90° counter-clockwise rotation) | not asked (prints in 03:09, out 08:57) | 15/10/2026 |

With vehicle number `KL05AF9341`, arrival `03:00` and end `09:00`, the pair should be **Accepted**. With `KL05AF9314`, both number checks should fail and the trip should be **Rejected**.

## Ideas not built yet

- **Pick the number from the fleet.** Fill the vehicle number from the trip in the route-optimisation app, or a list of the fleet's registrations, instead of typing it.
- **Cross-check photo and bill directly.** Send both images in one request and ask whether the plate in the photo matches the number on the bill, as a second opinion that doesn't depend on the typed number.
- **Teach by example.** If the vehicle or bill answers are unsure on real images, describe each option with a few example images as well as words.
- **Check the file's own GPS data.** Read the GPS position and capture time from original photo files, and compare them with the plant's location and the printed stamp.

## References

- [TypeSafe docs](https://docs.typesafe.ai): [Models](https://docs.typesafe.ai/models), [Images](https://docs.typesafe.ai/images), [Choice](https://docs.typesafe.ai/primitives/choice), [Noul (yes/no)](https://docs.typesafe.ai/primitives/noul), [Confidence](https://docs.typesafe.ai/confidence), [Jev 1.13 known weaknesses](https://docs.typesafe.ai/model-jaggedness/jev-1.13)
- [Python SDK](https://docs.typesafe.ai/sdk/python) (`typesafe-sdk` 0.7.5)
