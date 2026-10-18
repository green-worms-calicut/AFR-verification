# Jev proof check

Checks photos sent in as proof of a waste-collection trip, using [TypeSafe's Jev model](https://docs.typesafe.ai). For each image it answers:

- **What is it?** A bill (such as a weighbridge weighment slip), a geotagged photo (a photo with a GPS stamp printed on it), or something else.
- **What time and date does it show?** The time in and time out on a bill, or the time in a photo's GPS stamp.
- **Do a bill and a photo belong to the same trip?** The photo's date and time must fall within the bill's time in and time out.

Every answer comes with a confidence score, so unclear readings can go to a person instead of being trusted.

> **Status:** tested offline against a fake Jev API. Not yet run against the real Jev API, which needs an API key.

## How it works

Jev is a "System One" model. A chat model writes text; Jev doesn't. You send it something to look at and a set of questions, and it picks an answer to each from options you define, with a probability for every option. So a time like `03:09` is read as two separate questions: which hour (00–23) and which minute (00–59).

`check_proof.py` sends each image with 12 questions in a single request:

| Question | Options |
|-|-|
| `kind` | `bill`, `geotagged_photo`, `other` |
| `orientation` | `upright`, `top_points_right`, `top_points_left`, `upside_down` |
| `stamp_hour`, `stamp_minute` | Time in the GPS stamp: `00`–`23`, `00`–`59`, or `not_shown` |
| `stamp_day`, `stamp_month` | Date in the GPS stamp: `01`–`31`, `01`–`12`, or `not_shown` |
| `in_hour`, `in_minute` | First (earlier) time on the bill, or `not_shown` |
| `out_hour`, `out_minute` | Second (later) time on the bill, or `not_shown` |
| `bill_day`, `bill_month` | Date on the bill, or `not_shown` |

Every image is asked both the bill questions and the photo questions, because questions in one request are answered in parallel and cost little extra. Each of these questions states what it assumes, for example "If `image` is a bill, what hour is its time in?", and code keeps only the answers for the kind of image Jev found.

Each time and date question can answer `not_shown`, so Jev isn't forced to guess a value that isn't on the image. A time or date with any part not shown is reported as "not shown", and the trip check sends it to a person.

If Jev says the image isn't upright, the script rotates it and asks again. That's two requests for that image.

Before sending, each image has its camera rotation flag applied and is scaled so its longest side is 1568 px, the largest Jev accepts.

## Setup

Needs Python 3.10 or later.

```bash
cd ~/Projects/jev
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### API key

Get a key from the TypeSafe console at <https://console.typesafe.ai/keys>.

Save it in a private file outside the project. Run this in a terminal; it asks for the key without showing it or saving it in your shell history:

```bash
mkdir -p ~/.config/typesafe
read -rs -p "TypeSafe API key: " k && printf 'export TYPESAFE_API_KEY=%q\n' "$k" > ~/.config/typesafe/env && unset k
chmod 600 ~/.config/typesafe/env
echo '[ -f ~/.config/typesafe/env ] && . ~/.config/typesafe/env' >> ~/.bashrc
```

The test page reads the key from that file automatically. For the command-line scripts, open a new terminal or run `. ~/.config/typesafe/env` first.

Never put the key in a file inside this project.

## Usage

### Test page

```bash
.venv/bin/python app.py
```

Open the address it prints. It's `http://127.0.0.1:8765` unless that port is taken, in which case the app uses the next free one; choose a port with `--port`.

On the page:
- Drop in JPEG or PNG images, or click **Try the two samples**.
- Each image gets a card showing its kind, the times and dates read, a confidence bar for each part, and the image as it was actually sent.
- **Trip check** gives one verdict once there's a bill and a geotagged photo:

| Verdict | When |
|-|-|
| Same trip | Same date, photo time between time in and time out, and every reading at least 80% confident |
| Doesn't match | A confident reading shows a different date or a time outside the window |
| Needs a person to check | Any reading below 80% confidence, or a time or date that isn't shown |

The 80% threshold is a placeholder. Choose real thresholds with `evaluate.py` (below).

The server only listens on your own computer (`127.0.0.1`). The API key stays in `app.py` and is never sent to the browser.

### Command line

```bash
.venv/bin/python check_proof.py samples/bill.png samples/photo.png
.venv/bin/python check_proof.py samples/bill.png --model jev-preview   # try Jev 1.14
```

Prints each answer with its confidence, the runner-up options, and the tokens used.

### Measure accuracy and choose thresholds

`evaluate.py` measures how often Jev is right on your own images, and shows what each confidence threshold would do. TypeSafe's guidance is to set thresholds per question, by the risk of acting on a wrong answer, from accuracy measured on your own data.

1. Copy `labels.example.csv` to `labels.csv`. Add one row per image with the correct answers. File paths are relative to the CSV. Leave a cell blank to skip it, or write `not_shown` if that value isn't on the image.

   ```csv
   file,kind,time_in,time_out,bill_date,photo_time,photo_date
   trips/0412-bill.jpg,bill,03:09,08:57,15/10/2026,,
   trips/0412-photo.jpg,geotagged_photo,,,,04:09,15/10/2026
   ```

2. Ask Jev about every image. This costs tokens, and saves the answers to `eval/<model>.jsonl`:

   ```bash
   .venv/bin/python evaluate.py labels.csv
   ```

3. Re-score the saved answers without asking Jev again, for example after fixing a label:

   ```bash
   .venv/bin/python evaluate.py labels.csv --cached
   ```

The report has four parts:
- accuracy for each field;
- accuracy at each confidence level, for the image type, times and dates separately;
- for each candidate threshold, how many answers would be accepted automatically, how many of those would be wrong, and how many would go to a person;
- every mistake, most confident first, with the answer to each part.

Readings are scored against the labeled kind of image, so a wrong bill-or-photo answer doesn't hide how well times were read. Start with 20–50 real bill-and-photo pairs; the report warns while a group has fewer than 30 answers.

### Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The tests use a fake Jev API, so they need no key and cost nothing. They check:
- image resizing;
- the rotate-and-ask-again step;
- how readings are put together, including `not_shown`;
- the question wording rules;
- the evaluation scoring;
- the test page's server.

## Cost

Jev charges **$0.042 per million input tokens**; output is free. Images count as input tokens at the same rate. TypeSafe hasn't published how many tokens an image uses, so the page and the command line show the exact count and cost for every check.

| Model | Alias | Notes |
|-|-|-|
| `jev-1.13.0` | `jev-latest` | Stable. The default here. |
| `jev-1.14.0` | `jev-preview` | Newest, released 14 Oct 2026. |

## Limits

- **Image input is experimental.** TypeSafe calls it "dreamer-ready": fine for prototypes, not yet production quality. Accuracy and the request format can change between releases. Each request needs the `TypeSafe-Danger-Zone-Images: true` header, which the code sends.
- **Jev can't transcribe text.** It only picks from the options it's given. Addresses, serial numbers and weights can't be read out.
- **Jev only sees pixels.** The API strips all file metadata, so GPS data stored in the photo file never reaches it. "Geotagged" here means the stamp is printed on the photo.
- **A GPS stamp isn't proof.** GPS camera apps can be set to any place and time. Check the GPS and capture time in the original photo file on your side as well.
- **Use original photos.** Screenshots and photos forwarded through WhatsApp lose detail and their GPS data. The sample images are low-resolution screenshots, about 350 × 590 px.
- **Bill layout.** Bill times are asked for as "first" and "second" printed time, because the sample slip's values don't line up with its labels. A different slip layout may need different wording.
- **Image size limits.** PNG or JPEG only, 280–1568 px on each side, at most 16 MiB per file. Convert HEIC and WebP first.

## Files

| Path | What it is |
|-|-|
| `check_proof.py` | The checks: questions, image preparation, rotation, and putting readings together. Also a command-line tool. |
| `app.py` | Local server for the test page. Uses only Python's standard library. |
| `ui.html` | The test page. |
| `evaluate.py` | Measures accuracy on labeled images and shows what each confidence threshold would do. |
| `labels.example.csv` | Example labels file for `evaluate.py`, for the sample images. |
| `eval/` | Saved Jev answers from `evaluate.py`, one file per model. Not in git. |
| `tests/test_offline.py` | Offline tests against a fake Jev API. |
| `samples/bill.png`, `samples/photo.png` | A sample weighment slip (photographed sideways) and a truck photo with a GPS stamp, from the same trip. |
| `read_time.py` | An earlier experiment: reads the time on an analog or digital clock. |
| `make_test_clock.py` | Draws test clocks at a chosen time, for `read_time.py`. |
| `samples/analog-*.jpg`, `samples/digital-*.jpg` | Test clocks made by `make_test_clock.py`. |
| `jev-guide.html` | A longer illustrated guide to Jev, pricing, images and this project. Open it in a browser. |

## Expected results for the samples

Read by hand from the images, to compare against Jev's answers:

| Image | Kind | Time | Date |
|-|-|-|-|
| `samples/bill.png` | bill, sideways (needs a 90° counter-clockwise rotation) | in 03:09, out 08:57 | 15/10/2026 |
| `samples/photo.png` | geotagged photo | 04:09 | 15/10/2026 |

Both show truck KL05AF9341. The photo time falls between the bill's times, so the pair should be judged **Same trip**.

## Ideas not built yet

- **Match the truck number.** Send the photo and the bill in one request and ask whether the registration on the truck matches the number on the bill. Better still, compare each against the truck number your own system has for the trip.
- **Teach by example.** If the bill-or-photo answer is unsure on real images, describe each option with a few example images as well as words.
- **Check the file's own GPS data.** Read the GPS position and capture time from original photo files, and compare them with the plant's location and the printed stamp.

## References

- [TypeSafe docs](https://docs.typesafe.ai): [Models](https://docs.typesafe.ai/models), [Images](https://docs.typesafe.ai/images), [Choice questions](https://docs.typesafe.ai/primitives/choice), [Quickstart](https://docs.typesafe.ai/introduction/quickstart)
- [Python SDK](https://docs.typesafe.ai/sdk/python) (`typesafe-sdk` 0.7.5)
