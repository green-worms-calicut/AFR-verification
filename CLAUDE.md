# Jev proof check

Checks waste-trip proof images (weighbridge bills and GPS-stamped truck photos) with TypeSafe's Jev model. See README.md for what it does and how to run it.

## Working on this project

- Use the `typesafe-ai` skill (plugin `typesafe@typesafe-ai`) for any change that touches Jev questions, state, models or the API. Follow it: read the relevant live docs at https://docs.typesafe.ai (append `.md` to a page path) before changing an integration, rather than relying on memory.
- Python lives in `.venv/` (`typesafe-sdk` 0.7.5, Pillow). Run code with `.venv/bin/python`.
- Run the offline tests after any change: `.venv/bin/python -m unittest discover -s tests -v`. They use a fake Jev API and need no key.
- The API key lives in `~/.config/typesafe/env`, outside the project. Never write a key into this folder, and keep it server-side (`app.py`), never in `ui.html`.
- Questions are defined once in `check_proof.py` (`QUESTIONS`, `REPORT`); `app.py` and the tests reuse them. Keep rules and calculations (rotation, the trip time-window check) in code, not in questions.
- Image input is experimental in Jev and needs the `TypeSafe-Danger-Zone-Images: true` header (`check_proof.HEADERS`).
- Confidence thresholds (currently 0.8 in `ui.html`) are placeholders. Choose them from `evaluate.py` reports on labeled real images, per question group and by risk, as the TypeSafe confidence docs advise.
