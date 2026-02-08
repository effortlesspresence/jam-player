import os

APP_DATA_DIR = os.path.join(
    os.path.expanduser('~'), ".jam", "app_data"
)
APP_DATA_JSON_DIR = os.path.join(
    APP_DATA_DIR, "json"
)

APP_DATA_LIVE_SCENES_DIR = os.path.join(APP_DATA_DIR, "live_scenes")
APP_DATA_LIVE_MEDIA_DIR = os.path.join(APP_DATA_DIR, "live_media")
APP_DATA_STAGED_SCENES_DIR = os.path.join(APP_DATA_DIR, "staged_scenes")

# Device data is stored in /etc/jam in JAM 2.0 (was ~/.jam in 1.0)
DEVICE_UUID_FILE_PATH = "/etc/jam/device_data/device_uuid.txt"
