# Smart Doorbell Dashboard (laptop)

Streamlit dashboard that visualizes YOLO detections captured by the UniHiker
doorbell, handles live doorbell-press notifications, and remotely toggles the
UniHiker's built-in LED. All communication with the device runs over
HiveMQ Cloud MQTT, so the dashboard and the UniHiker only need outbound
Internet — they do **not** need to be on the same LAN.

## What it does

- Queries InfluxDB (`light_data_bucket`, measurement `doorbell_detection`) for
  the last 24 hours and renders each event on a vis.js timeline. Clicking an
  event shows the annotated image from Cloudinary.
- Polls `events.json` every 1.5 s for new doorbell presses published by the
  UniHiker on the `doorbell/press` MQTT topic. New presses trigger a
  `beepy` alert and a red banner + toast in the UI.
- Sidebar LED **ON** / **OFF** buttons publish to the `doorbell/led` MQTT
  topic, which the UniHiker subscribes to.

## Prerequisites

- Python 3.9+
- On Linux, `beepy` needs ALSA dev libs for its `simpleaudio` backend:

  ```bash
  sudo apt-get install libasound2-dev
  ```

## Install

```bash
cd doorbell/dashboard
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Configure

All credentials (HiveMQ, Cloudinary, InfluxDB) live in a single file:

- `dashboard_config.py` — edit `BROKER_HOST` / `BROKER_USERNAME` /
  `BROKER_PASSWORD` to match your HiveMQ Cloud cluster. The defaults already
  carry the InfluxDB token and Cloudinary keys used by the UniHiker helpers
  in this repo.

## Run

Open two terminals in `doorbell/dashboard/`:

```bash
# Terminal 1 — MQTT listener writes doorbell presses to events.json.
python mqtt_listener.py

# Terminal 2 — Streamlit dashboard.
streamlit run app.py
```

Then start the UniHiker side:

```bash
# On the UniHiker
python doorbell/unihiker/doorbell_unihiker.py
```

## Files

- `app.py` — Streamlit dashboard (timeline + image viewer + LED controls).
- `mqtt_listener.py` — subscribes to `doorbell/press` and appends events.
- `events_store.py` — thread/process-safe reader/writer for `events.json`.
- `dashboard_config.py` — single source of credentials + runtime settings.
- `events.json` — created at runtime; do not edit manually.

## Troubleshooting

- **No timeline events**: confirm the UniHiker has uploaded at least once.
  The `doorbell_detection` measurement must have a `url` field for the item
  to be rendered.
- **No doorbell toast**: make sure `mqtt_listener.py` is running in its own
  terminal and is showing `[mqtt] connected` output.
- **No sound**: verify `beepy`/`simpleaudio` is installed and your laptop has
  audio output; `beepy` logs errors to stdout in the Streamlit terminal.
