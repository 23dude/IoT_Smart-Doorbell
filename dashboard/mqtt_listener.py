#!/usr/bin/env python3
"""Laptop-side MQTT bridge.

Subscribes to the doorbell-press topic on HiveMQ and appends each press to
`events.json` so the Streamlit dashboard can pick it up on its next refresh.

Run as a standalone process alongside Streamlit:

    python mqtt_listener.py
"""

from __future__ import annotations

import json
import signal
import ssl
import sys
import time

import paho.mqtt.client as mqtt

import dashboard_config as cfg
import events_store


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"[mqtt] connected to {cfg.BROKER_HOST}; subscribing to {cfg.TOPIC_PRESS}")
        client.subscribe(cfg.TOPIC_PRESS, qos=1)
    else:
        print(f"[mqtt] connect failed rc={rc}")


def on_disconnect(client, userdata, rc):
    print(f"[mqtt] disconnected rc={rc}")


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception:
        payload = {"raw": msg.payload.decode("utf-8", errors="replace")}
    if not isinstance(payload, dict):
        payload = {"value": payload}

    event = events_store.append_event(payload)
    print(f"[press] id={event.get('id')} ts={event.get('ts')} received={event['received_at']}")


def build_client() -> mqtt.Client:
    client = mqtt.Client(client_id=cfg.MQTT_LISTENER_CLIENT_ID)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    client.username_pw_set(cfg.BROKER_USERNAME, cfg.BROKER_PASSWORD)
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS)
    ssl_ctx.load_default_certs()
    client.tls_set_context(ssl_ctx)
    return client


def main() -> int:
    client = build_client()

    def _shutdown(signum, frame):
        print("\n[mqtt] shutting down")
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while True:
        try:
            client.connect(cfg.BROKER_HOST, cfg.BROKER_PORT, keepalive=60)
            client.loop_forever(retry_first_connection=True)
        except KeyboardInterrupt:
            _shutdown(None, None)
        except Exception as exc:
            print(f"[mqtt] loop error: {exc}; retrying in 5s")
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
