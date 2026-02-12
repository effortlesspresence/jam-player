"""
JAM Player 2.0 - Scenes Manager Service

This service manages content for the JAM Player:
1. Polls the JAM 2.0 API for content updates
2. Fetches scene content when updates are available
3. Downloads media files (images and videos)
4. Writes scene data for jam_player_app to consume

TEMPORARY: This polling mechanism will be replaced with WebSocket push
notifications in a future release.
"""

import os
import json
import time
import hashlib
from typing import List, Optional, Dict, Any
from pathlib import Path

from jam_player import constants
from jam_player.utils import logging_utils as lu

from common.api import api_request
from common.credentials import get_device_uuid, is_device_registered

logger = lu.get_logger("scenes_manager_service")

# Directories for content storage
LIVE_SCENES_DIR = Path(constants.APP_DATA_LIVE_SCENES_DIR)
LIVE_MEDIA_DIR = Path(constants.APP_DATA_LIVE_MEDIA_DIR)
STAGED_SCENES_DIR = Path(constants.APP_DATA_STAGED_SCENES_DIR)

# Polling interval in seconds
POLL_INTERVAL_SECONDS = 7


def hash_string(input_string: str) -> str:
    """Generate SHA256 hash of a string."""
    return hashlib.sha256(input_string.encode('utf-8')).hexdigest()


def check_for_updates() -> bool:
    """
    Check for content updates using the JAM 2.0 API.

    Calls GET /jam-players/{deviceUuid}/update-poll with Ed25519 signing.
    If hasUnpulledUpdates is true, the backend resets the flag and we return True.

    Returns:
        True if there are updates available, False otherwise.
    """
    device_uuid = get_device_uuid()
    if not device_uuid:
        logger.error("No device UUID found - cannot check for updates")
        return False

    try:
        response = api_request(
            method='GET',
            path=f'/jam-players/{device_uuid}/update-poll',
            signed=True,
            timeout=30
        )

        if not response:
            logger.warning("No response from update-poll endpoint")
            return False

        if response.status_code != 200:
            logger.warning(f"update-poll returned {response.status_code}: {response.text}")
            return False

        data = response.json()
        has_updates = data.get('hasUnpulledUpdates', False)

        if has_updates:
            logger.info("Updates available")

        return has_updates

    except Exception as e:
        logger.error(f"Error checking for updates: {e}", exc_info=True)
        return False


def fetch_content() -> Optional[List[Dict[str, Any]]]:
    """
    Fetch content from the JAM 2.0 API.

    Calls GET /jam-players/{deviceUuid}/content with Ed25519 signing.

    Returns:
        List of scene dicts with id, videoUrl, imageUrl, timeToDisplay, or None on error.
    """
    device_uuid = get_device_uuid()
    if not device_uuid:
        logger.error("No device UUID found - cannot fetch content")
        return None

    try:
        logger.info(f"Fetching content for device {device_uuid}")
        response = api_request(
            method='GET',
            path=f'/jam-players/{device_uuid}/content',
            signed=True,
            timeout=60
        )

        if not response:
            logger.error("No response from content endpoint")
            return None

        if response.status_code != 200:
            logger.error(f"content endpoint returned {response.status_code}: {response.text}")
            return None

        data = response.json()
        scenes = data.get('jamPlayerScenes', [])
        logger.info(f"Fetched {len(scenes)} scenes from API")
        return scenes

    except Exception as e:
        logger.error(f"Error fetching content: {e}", exc_info=True)
        return None


def download_media(url: str, dest_path: Path) -> bool:
    """
    Download media file from URL to destination path.

    Args:
        url: URL to download from
        dest_path: Path to save the file to

    Returns:
        True if successful, False otherwise.
    """
    import requests
    from requests.exceptions import ReadTimeout, ConnectionError as ReqConnectionError

    if not url:
        logger.warning("Empty URL, skipping download")
        return False

    # Ensure URL has protocol
    if not url.startswith(('http:', 'https:')):
        url = 'https:' + url

    # Log the full URL for debugging
    logger.info(f"Downloading media: {url[:100]}{'...' if len(url) > 100 else ''}")
    logger.info(f"  Destination: {dest_path}")

    # Retry logic for transient network issues
    max_retries = 5
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=120)
            response.raise_for_status()

            # Ensure parent directory exists
            dest_path.parent.mkdir(parents=True, exist_ok=True)

            # Write file
            with open(dest_path, 'wb') as f:
                f.write(response.content)

            logger.info(f"Downloaded media to {dest_path}")
            return True

        except (ReqConnectionError, ReadTimeout) as e:
            if attempt < max_retries - 1:
                logger.warning(f"Download attempt {attempt + 1} failed, retrying: {e}")
                time.sleep(5)
            else:
                logger.error(f"Failed to download after {max_retries} attempts: {e}")
                return False
        except Exception as e:
            logger.error(f"Error downloading media: {e}", exc_info=True)
            return False

    return False


def get_file_extension_from_url(url: str) -> str:
    """Extract file extension from URL, defaulting to appropriate type."""
    if not url:
        return ''

    # Parse the path from URL
    from urllib.parse import urlparse
    parsed = urlparse(url)
    path = parsed.path

    # Get extension
    ext = os.path.splitext(path)[1].lower()

    # Validate it's a known media extension
    known_extensions = {'.mp4', '.mov', '.avi', '.webm', '.jpg', '.jpeg', '.png', '.webp', '.gif'}
    if ext in known_extensions:
        return ext

    # Default based on likely content type
    return '.mp4' if 'video' in url.lower() else '.jpg'


def load_content() -> bool:
    """
    Fetch content from API and download all media files.

    This stages content first, then atomically swaps to live.

    Returns:
        True if successful, False otherwise.
    """
    # Fetch content from API
    scenes = fetch_content()
    if scenes is None:
        logger.error("Failed to fetch content from API")
        return False

    if not scenes:
        logger.warning("No scenes returned from API")
        # Still write empty scenes file so player knows there's nothing to show
        pass

    # Clear staged directory
    if STAGED_SCENES_DIR.exists():
        import shutil
        shutil.rmtree(STAGED_SCENES_DIR)

    # Ensure directories exist
    STAGED_SCENES_DIR.mkdir(parents=True, exist_ok=True)
    LIVE_MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    # Process each scene (API returns them in order by Scene.order)
    processed_scenes = []
    for order_index, scene in enumerate(scenes):
        scene_id = scene.get('id')
        video_url = scene.get('videoUrl')
        image_url = scene.get('imageUrl')
        time_to_display = scene.get('timeToDisplay', 15)

        # Determine which URL to use (video takes priority)
        media_url = video_url or image_url
        is_video = video_url is not None

        if not media_url:
            logger.warning(f"Scene {scene_id} has no media URL, skipping")
            continue

        # Generate filename from URL hash
        url_hash = hash_string(media_url)
        extension = get_file_extension_from_url(media_url)
        media_filename = f"{url_hash}{extension}"
        media_path = LIVE_MEDIA_DIR / media_filename

        # Download if not already present
        if not media_path.exists():
            if not download_media(media_url, media_path):
                logger.error(f"Failed to download media for scene {scene_id}, skipping")
                continue

        # Build processed scene data (order preserves API order)
        processed_scene = {
            'id': scene_id,
            'order': order_index,
            'media_file': media_filename,
            'media_type': 'VIDEO' if is_video else 'IMAGE',
            'time_to_display': time_to_display,
        }
        processed_scenes.append(processed_scene)

        # Write individual scene JSON (for compatibility)
        scene_json_path = STAGED_SCENES_DIR / f"{scene_id}.json"
        with open(scene_json_path, 'w') as f:
            json.dump(processed_scene, f, indent=2)

    # Write master scenes list
    scenes_list_path = STAGED_SCENES_DIR / "scenes.json"
    with open(scenes_list_path, 'w') as f:
        json.dump(processed_scenes, f, indent=2)

    logger.info(f"Staged {len(processed_scenes)} scenes")

    # Atomically swap staged to live
    import shutil
    if LIVE_SCENES_DIR.exists():
        shutil.rmtree(LIVE_SCENES_DIR)
    shutil.copytree(STAGED_SCENES_DIR, LIVE_SCENES_DIR)

    logger.info("Content loaded successfully")
    return True


def run():
    """Main service loop."""
    logger.info("=" * 60)
    logger.info("JAM Player 2.0 - Scenes Manager Service Starting")
    logger.info("=" * 60)

    # Wait for device to be registered before trying to fetch content
    # An unregistered device won't have content assigned anyway
    logger.info("Waiting for device to be registered...")
    while not is_device_registered():
        time.sleep(10)
    logger.info("Device is registered, proceeding with content management")

    # Initial content load with exponential backoff
    retry_delay = 10  # Start with 10 seconds
    max_retry_delay = 300  # Cap at 5 minutes
    while True:
        try:
            logger.info("Loading initial content...")
            if load_content():
                logger.info("Initial content loaded successfully")
                break
            else:
                logger.warning(f"Failed to load initial content, retrying in {retry_delay}s")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry_delay)
        except Exception as e:
            logger.error(f"Error during initial content load: {e}", exc_info=True)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, max_retry_delay)

    # Main polling loop
    while True:
        try:
            if check_for_updates():
                logger.info("Updates detected, reloading content...")
                try:
                    if load_content():
                        logger.info("Content reloaded successfully")
                        # Note: jam_player_display.py monitors scenes.json mtime directly
                    else:
                        logger.error("Failed to reload content")
                except Exception as e:
                    logger.error(f"Error reloading content: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Error in update check loop: {e}", exc_info=True)

        time.sleep(POLL_INTERVAL_SECONDS)


def main():
    run()


if __name__ == "__main__":
    logger.info("Starting Scenes Manager Service")
    main()
