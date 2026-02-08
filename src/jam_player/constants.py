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

DEVICE_UUID_FILE_PATH = os.path.join(
    os.path.expanduser('~'), ".jam", "device_data", "device_uuid.txt"
)

STATIC_IMAGES_DIR = os.path.join(
    os.path.expanduser('~'), ".jam", "static_images"
)
