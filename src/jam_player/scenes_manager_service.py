import os
import json
import time
from jam_player import constants
import shutil
from jam_player.jam_enums import SceneMediaType
import hashlib
from jam_player.utils import media_utils as mu
from jam_player.clients.jam_api_client import JamApiClient
from jam_player.utils import system_utils as su
from jam_player.utils import logging_utils as lu
from jam_player.utils import scene_update_flag_utils as sufu

logger = lu.get_logger("scenes_manager_service")


def hash_string(input_string):
    encoded_string = input_string.encode('utf-8')
    hash_object = hashlib.sha256()
    hash_object.update(encoded_string)
    return hash_object.hexdigest()


class ScenesManager:

    def __init__(self):
        try:
            self.jam_client = JamApiClient(logger)
        except Exception as e:
            logger.error(f"Error initializing JAM API Client: {e}", exc_info=True)
            self.jam_client = None

    def ensure_directories_exist(self):
        """Ensure all required directories exist."""
        directories = [
            constants.APP_DATA_LIVE_SCENES_DIR,
            constants.APP_DATA_LIVE_MEDIA_DIR,
            constants.APP_DATA_STAGED_SCENES_DIR
        ]

        for directory in directories:
            try:
                os.makedirs(directory, exist_ok=True)
            except Exception as e:
                logger.error(f"Failed to create directory {directory}: {e}", exc_info=True)
                raise

    def load_scenes(self):
        """
        Load scenes from the API, download media files, and create Scene json files.
        """
        try:
            # Clear the staged scenes directory if it exists
            shutil.rmtree(constants.APP_DATA_STAGED_SCENES_DIR)
        except FileNotFoundError:
            pass

        # Ensure all required directories exist
        self.ensure_directories_exist()

        try:
            # Get scenes from API
            scenes = self.jam_client.get_scenes()

            for scene in scenes:
                # Generate hashed filename for media
                media_url_hash = hash_string(scene.media_url)
                live_media_filepath = os.path.join(
                    constants.APP_DATA_LIVE_MEDIA_DIR, media_url_hash
                )

                # Download the media file if it doesn't exist in the live media directory
                # already, or if the Scene's redownload_media flag is enabled
                existing_file_path = su.file_exists_or_prefix_exists(live_media_filepath)
                if scene.redownload_media or not existing_file_path:
                    downloaded_file_path = mu.download_media(
                        scene.media_url, live_media_filepath, scene.media_type
                    )
                    if scene.media_type == SceneMediaType.IMAGE:
                        mu.fix_image(
                            downloaded_file_path,
                            max_allowed_size_mb=2.75,
                            target_compressed_size_mb=2.25
                        )

                # Create scene JSON file data
                scene_config = {
                    "id": scene.id,
                    "time_to_display": scene.time_to_display,
                    "order": scene.order,
                    "media_file": (
                        os.path.basename(existing_file_path)
                        if existing_file_path and not scene.redownload_media
                        else os.path.basename(downloaded_file_path)
                    ),
                    "media_type": scene.media_type.value,
                    "time_ranges": scene.time_ranges
                }

                if scene.media_type == SceneMediaType.VIDEO:
                    scene_config["video_loops"] = scene.video_loops

                # Write scene configuration to JSON file
                scene_json_path = os.path.join(
                    constants.APP_DATA_STAGED_SCENES_DIR,
                    f"{scene.id}.json"
                )

                try:
                    with open(scene_json_path, 'w') as f:
                        json.dump(scene_config, f, indent=2)
                except Exception as e:
                    logger.error(f"Failed to write scene config for {scene.id}: {e}", exc_info=True)
                    raise

            # Replace live scenes directory with staged scenes
            shutil.rmtree(constants.APP_DATA_LIVE_SCENES_DIR)
            shutil.copytree(constants.APP_DATA_STAGED_SCENES_DIR, constants.APP_DATA_LIVE_SCENES_DIR)
        except Exception as e:
            logger.error(f"Error in load_scenes: {e}", exc_info=True)
            raise

    def run(self):
        while True:
            try:
                # Initialize JAM client if not already initialized
                if not self.jam_client:
                    self.jam_client = JamApiClient(logger)

                # Initial scenes load
                logger.info("Loading initial scenes ...")
                self.load_scenes()
                logger.info("Done loading initial scenes.")
                break  # Exit initialization loop if successful
            except Exception as e:
                logger.error(f"Error during initialization: {e}", exc_info=True)
                time.sleep(60)  # Wait before retrying initialization

        while True:
            try:
                if self.jam_client.check_for_updates():
                    logger.info("Updates found! Loading scenes ...")
                    try:
                        self.load_scenes()
                        logger.info("Done loading scenes.")
                        sufu.reset_update_flag_to_one()
                    except Exception as e:
                        logger.error(f"Error loading scenes: {e}", exc_info=True)
            except Exception as e:
                logger.error(f"Error checking for updates: {e}", exc_info=True)

            time.sleep(60)


def main():
    sm = ScenesManager()
    sm.run()


if __name__ == "__main__":
    logger.info("Starting - from Scenes Manager Service")
    main()
