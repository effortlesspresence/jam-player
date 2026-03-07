from datetime import datetime
import certifi
import requests
import json
import os
import typing as tp
from jam_player import constants
from jam_player.jam_enums import SceneMediaType

API_BASE_URL = "https://app.justamenu.com/api/1.1/wf"


class Scene:
    def __init__(
            self,
            id: str,
            time_to_display: float,
            media_url: str,
            order: float,
            media_type: SceneMediaType,
            time_ranges: tp.List[tp.Dict[str, str]] = None,
            redownload_media: bool = False,
            video_loops: int = 1
    ):
        self.id = id
        self.time_to_display = time_to_display
        self.media_url = media_url if media_url.lower().startswith("https") else f"https:{media_url}"
        self.order = order
        self.media_type = media_type
        self.time_ranges = time_ranges if time_ranges is not None else []
        self.redownload_media = redownload_media
        self.video_loops = video_loops


def convert_time_range(tr: tp.Dict) -> tp.Dict[str, str]:
    """
    Convert a time range dict from the API into a standardized format with keys:
    "day_of_week", "start", and "end", where start and end are in HH:MM (24-hour) format.
    """
    day = tr.get("day_of_week", "").upper()
    # Convert start time
    try:
        start_hour = int(tr.get("start_hour", 0))
        start_min = int(tr.get("start_min", 0))
        if tr.get("start_am_pm", "").lower() == "pm" and start_hour != 12:
            start_hour += 12
        elif tr.get("start_am_pm", "").lower() == "am" and start_hour == 12:
            start_hour = 0
        start_time = f"{start_hour:02d}:{start_min:02d}"
    except Exception:
        start_time = "00:00"
    # Convert end time
    try:
        end_hour = int(tr.get("end_hour", 0))
        end_min = int(tr.get("end_min", 0))
        if tr.get("end_am_pm", "").lower() == "pm" and end_hour != 12:
            end_hour += 12
        elif tr.get("end_am_pm", "").lower() == "am" and end_hour == 12:
            end_hour = 0
        end_time = f"{end_hour:02d}:{end_min:02d}"
    except Exception:
        end_time = "00:00"

    return {"day_of_week": day, "start": start_time, "end": end_time}


def dict_to_scene(scene_dict: tp.Dict) -> Scene:
    media_type_str = scene_dict.get("media_type")
    if media_type_str == "BRAND_VIDEO":
        media_type = SceneMediaType.BRAND_VIDEO
    elif media_type_str == "VIDEO":
        media_type = SceneMediaType.VIDEO
    else:
        media_type = SceneMediaType.IMAGE
    raw_time_ranges = scene_dict.get("time_ranges", [])
    converted_time_ranges = [convert_time_range(tr) for tr in raw_time_ranges] if raw_time_ranges else []
    if media_type == SceneMediaType.BRAND_VIDEO:
        media_url = scene_dict.get("brand_video", {}).get("video")
    elif media_type == SceneMediaType.VIDEO:
        media_url = scene_dict.get("video")
    else:
        media_url = scene_dict.get("image")
    return Scene(
        scene_dict.get("_id", scene_dict.get("id")),
        scene_dict.get("time_to_display", 15),
        media_url,
        scene_dict.get("order", 0),
        media_type,
        converted_time_ranges,
        scene_dict.get("redownload_media", False),
        int(scene_dict.get("video_loops", 1) or 1)
    )


def read_btk():
    token_fp = os.path.expanduser("/home/comitup/.j/.p/.t/.k/.btk")
    try:
        with open(token_fp, 'r') as file:
            return file.read().strip()
    except FileNotFoundError:
        raise Exception(f"Bearer token file not found at {token_fp}")
    except IOError:
        raise Exception(f"Error reading bearer token file at {token_fp}")


def get_device_uuid() -> tp.Optional[str]:
    try:
        with open(constants.DEVICE_UUID_FILE_PATH, 'r') as f:
            uuid = f.read().strip()
            return uuid if uuid else None
    except FileNotFoundError:
        return None


class JamApiClient:

    def __init__(self, logger):
        self.logger = logger
        self.logger.info("Initializing JAM API Client...")
        self.device_uuid = get_device_uuid()
        self.logger.info(f"        Device UUID: {self.device_uuid}")
        self.jam_player_info = self.get_jam_player_info(self.device_uuid)
        self.jam_player_uuid = self.jam_player_info.get("id", self.jam_player_info.get("_id"))
        self.logger.info(f"        JAM Player UUID: {self.jam_player_uuid}")
        self.logger.info("Done initializing JAM API Client.")

    def _get_current_day_of_week(self):
        return datetime.now().strftime("%A").upper()

    def _make_api_request(self, endpoint, method="GET", params=None, data=None):
        bearer_token = read_btk()
        headers = {
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json"
        }
        url = f"{API_BASE_URL}/{endpoint}"

        try:
            if method.upper() == "GET":
                response = requests.get(url, headers=headers, params=params, verify=certifi.where())
            elif method.upper() == "POST":
                response = requests.post(url, headers=headers, json=data, verify=certifi.where())
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            response.raise_for_status()
            return response.json()["response"]
        except requests.RequestException as e:
            self.logger.error(f"API request error: {e}", exc_info=True)
            raise

    def check_for_updates(self) -> bool:
        try:
            response = self._make_api_request(
                "check-for-jplayer-updates",
                method="POST",
                data={"jam_player": self.jam_player_uuid}
            )
            return response.get("has_unpulled_playlist_updates", False)
        except requests.RequestException:
            self.logger.error("Error checking for updates")
            return False

    def get_jam_player_info(self, device_uuid) -> tp.Dict[str, tp.Union[str, int, float]]:
        try:
            response = self._make_api_request("get-jam-player", method="POST", data={"device_uuid": device_uuid})
            if not response.get("jam_player"):
                raise Exception("Error getting jam player info - empty response")

            # Get the jam_player info
            jam_player_info = response.get("jam_player")

            # Write the info to a file (JAM 2.0 uses /etc/jam for device data)
            os.makedirs("/etc/jam/device_data", exist_ok=True)
            with open("/etc/jam/device_data/jam_player_info.json", 'w') as file:
                json.dump(jam_player_info, file, indent=4)

            return jam_player_info
        except requests.RequestException as e:
            self.logger.error(f"Error getting jam player info: {e}", exc_info=True)
            raise e

    def get_scenes(self) -> tp.List["Scene"]:
        try:
            current_day = self._get_current_day_of_week()
            params = {
                "jp_id": self.jam_player_uuid,
                "day_of_week": current_day
            }
            response = self._make_api_request(
                "get-scenes-for-jp", method="GET", params=params
            )
            scenes_json_string = response.get("scenes", "[]")
            scene_dicts = json.loads(scenes_json_string)
            return [dict_to_scene(scene_dict) for scene_dict in scene_dicts]
        except Exception as e:
            self.logger.error(
                f"Error getting scenes for JAM Player {self.jam_player_uuid}: {e}",
                exc_info=True,
            )
            raise e