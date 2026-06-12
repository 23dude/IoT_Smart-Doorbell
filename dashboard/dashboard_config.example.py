"""Dashboard credentials — copy this file to dashboard_config.py and fill in your values."""

import os

# ─── Cloudinary (optional — images are served by URL from InfluxDB) ──────────

CLOUDINARY_CLOUD_NAME = "your_cloud_name"
CLOUDINARY_API_KEY    = "your_api_key"
CLOUDINARY_API_SECRET = "your_api_secret"

# ─── InfluxDB ─────────────────────────────────────────────────────────────────

INFLUX_URL         = "https://us-east-1-1.aws.cloud2.influxdata.com"
INFLUX_TOKEN       = "your_influxdb_token"
INFLUX_ORG         = "your_org"
INFLUX_BUCKET      = "image_url_bucket"
INFLUX_MEASUREMENT = "doorbell_detection"

# ─── HiveMQ Cloud (MQTT over TLS) ─────────────────────────────────────────────

BROKER_HOST     = "your_broker.s1.eu.hivemq.cloud"
BROKER_PORT     = 8883
BROKER_USERNAME = "your_username"
BROKER_PASSWORD = "your_password"

TOPIC_PRESS = "doorbell/press"
TOPIC_LED   = "doorbell/led"

MQTT_LISTENER_CLIENT_ID  = "laptop_doorbell_listener"
MQTT_PUBLISHER_CLIENT_ID = "laptop_doorbell_publisher"

# ─── Dashboard runtime ────────────────────────────────────────────────────────

EVENTS_PATH          = os.path.join(os.path.dirname(os.path.abspath(__file__)), "events.json")
MAX_EVENTS           = 200
TIMELINE_LOOKBACK    = "-24h"
REFRESH_INTERVAL_MS  = 1500
