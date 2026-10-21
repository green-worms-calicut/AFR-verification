# Jev proof check

Checks a waste trip's proof against the vehicle number typed in, with TypeSafe's Jev model: a vehicle photo (vehicle shown, GPS stamp, number plate readable and matching) and a weigh bill (weighbridge bill, vehicle number matching), plus the same-date check and the photo time within the typed arrival and end time (the bill's times aren't asked about: the vehicle can be weighed before loading). See README.md for what it does and how to run it.

## Working on this project

- Use the `typesafe-ai` skill (plugin `typesafe@typesafe-ai`) for any change that touches Jev questions, state, models or the API. Follow it: read the relevant live docs at https://docs.typesafe.ai (append `.md` to a page path) before changing an integration, rather than relying on memory.
- Python lives in `.venv/` (`typesafe-sdk` 0.7.5, Pillow). Run code with `.venv/bin/python`.
- Run the offline tests after any change: `.venv/bin/python -m unittest discover -s tests -v`. They use a fake Jev API and need no key.
- The API key lives in GNOME Keyring (`secret-tool`, attributes `service typesafe account api`), read by `check_proof.api_key()`. Never look it up, print it, or write it to a file, the environment or `ui.html`; pass it straight to `TypeSafeClient(api_key=...)` server-side.
- Questions are defined once in `check_proof.py` (`QUESTIONS` per slot, `REPORT`); `app.py`, `evaluate.py` and the tests reuse them. Regenerate `playground/*-questions.json` from `QUESTIONS` after changing them. Keep rules and calculations (rotation, pass/fail/review decisions, the trip time-window check) in code, not in questions.
- Jev can't transcribe text: the typed vehicle number goes in the state and Jev judges whether the plate or bill shows it. Don't list `same` first in a number question (Jev leans towards the first option).
- Image input is experimental in Jev and needs the `TypeSafe-Danger-Zone-Images: true` header (`check_proof.HEADERS`).
- Confidence thresholds (`CONFIDENT`, `YES`, `NO`, and `ROTATE` for turning an image, in `check_proof.py`) are placeholders. Choose them from `evaluate.py` reports on labeled real images, per question group and by risk, as the TypeSafe confidence docs advise.
