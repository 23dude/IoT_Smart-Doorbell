"""UniHiker device credentials — copy this file to device_config.py and fill in your values."""

# ─── Cloudinary ───────────────────────────────────────────────────────────────

CLOUDINARY_CLOUD_NAME = "your_cloud_name"
CLOUDINARY_API_KEY    = "your_api_key"
CLOUDINARY_API_SECRET = "your_api_secret"

# ─── InfluxDB ─────────────────────────────────────────────────────────────────

INFLUX_URL         = "https://us-east-1-1.aws.cloud2.influxdata.com"
INFLUX_TOKEN       = "your_influxdb_token"
INFLUX_ORG         = "your_org"
INFLUX_BUCKET      = "image_url_bucket"
INFLUX_MEASUREMENT = "doorbell_detection"
INFLUX_DEVICE_TAG  = "unihiker_m10"

# ─── HiveMQ Cloud (MQTT over TLS) ─────────────────────────────────────────────

BROKER_HOST     = "your_broker.s1.eu.hivemq.cloud"
BROKER_PORT     = 8883
BROKER_USERNAME = "your_username"
BROKER_PASSWORD = "your_password"
MQTT_CLIENT_ID  = "unihiker_doorbell"

TOPIC_PRESS = "doorbell/press"
TOPIC_LED   = "doorbell/led"

# ─── Detection / upload behavior ──────────────────────────────────────────────

CONF_THRESHOLD         = 0.85
COOLDOWN_SEC           = 30.0
FPS_WINDOW_SEC         = 1.0
CAMERA_VIEW_UPDATE_SEC = 0.08
