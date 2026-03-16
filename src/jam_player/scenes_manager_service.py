"""
JAM Player 2.0 - Scenes Manager Service

This service manages content for the JAM Player:
1. Receives WebSocket push notifications for immediate content updates
2. Polls the JAM 2.0 API as a fallback for content updates
3. Fetches scene content when updates are available
4. Downloads media files (images and videos)
5. Writes scene data for jam_player_display to consume

The API returns scenes with:
- mediaType: {"value": "CANVAS_IMAGE"|"CANVAS_VIDEO"|..., "label": "..."}
- imageUrl: URL to image file (for image-based scenes)
- videoUrl: URL to video file (for video-based scenes)
- duration: How long to display (seconds)
- daysScheduled: Day/time scheduling info

Content updates can be triggered by:
1. WebSocket REFRESH_CONTENT command (via SIGUSR1 from jam_ws_commands)
2. Polling the /update-poll endpoint (fallback)
3. screen_id.txt changes (BLE linking)
"""

import os
import json
import time
import hashlib
import shutil
import subprocess
import signal
import threading
from typing import List, Optional, Dict, Any
from pathlib import Path

from jam_player import constants

from common.api import api_request
from common.credentials import get_device_uuid, is_device_registered
from common.logging_config import setup_service_logging
from common.paths import SCREEN_ID_FILE

logger = setup_service_logging("jam-content-manager")

# Directories for content storage
LIVE_SCENES_DIR = Path(constants.APP_DATA_LIVE_SCENES_DIR)
LIVE_MEDIA_DIR = Path(constants.APP_DATA_LIVE_MEDIA_DIR)
STAGED_SCENES_DIR = Path(constants.APP_DATA_STAGED_SCENES_DIR)

# Polling interval in seconds (fallback when WebSocket push fails)
POLL_INTERVAL_SECONDS = 120

# Event to signal immediatejam-ha  content refresh (set by SIGUSR1 handler)
refresh_event = threading.Event()


def handle_refresh_signal(signum, frame):
    """
    Handle SIGUSR1 signal to trigger immediate content refresh.

    This is sent by jam_ws_commands when it receives a REFRESH_CONTENT
    WebSocket command from the backend.
    """
    logger.info("Received SIGUSR1 - immediate content refresh requested")
    refresh_event.set()


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
        List of scene dicts with id, mediaType, imageUrl, videoUrl, duration, daysScheduled,
        or None on error.
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
    Download media file from URL to destination path using chunked streaming.

    Uses chunked downloads to:
    - Avoid loading entire file into memory
    - Detect connection stalls quickly (per-chunk timeout vs per-file)
    - Handle large files on slow connections gracefully

    Args:
        url: URL to download from
        dest_path: Path to save the file to

    Returns:
        True if successful, False otherwise.
    """
    import requests
    from requests.exceptions import ReadTimeout, ConnectionError as ReqConnectionError

    # Chunked download settings
    CHUNK_SIZE = 64 * 1024  # 64KB chunks
    CONNECT_TIMEOUT = 10    # 10 seconds to establish connection
    READ_TIMEOUT = 30       # 30 seconds between chunks (detects stalls)

    if not url:
        logger.warning("Empty URL, skipping download")
        return False

    # Ensure URL has protocol
    if not url.startswith(('http:', 'https:')):
        url = 'https:' + url

    # Log the full URL for debugging
    logger.info(f"Downloading media: {url[:100]}{'...' if len(url) > 100 else ''}")

    # Ensure parent directory exists before we start downloading
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Retry logic for transient network issues
    max_retries = 5
    for attempt in range(max_retries):
        try:
            # Use streaming download with per-chunk timeout
            # timeout=(connect, read) - read timeout applies between chunks
            with requests.get(url, stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) as response:
                response.raise_for_status()

                # Write file in chunks as data arrives
                with open(dest_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:  # filter out keep-alive chunks
                            f.write(chunk)

            # Verify file was written and has content
            if not dest_path.exists():
                logger.error(f"File not found after write: {dest_path}")
                return False

            file_size = dest_path.stat().st_size
            if file_size == 0:
                logger.error(f"Downloaded file is empty: {dest_path}")
                dest_path.unlink()  # Clean up empty file
                return False

            # For video files, validate with ffprobe
            if dest_path.suffix.lower() in {'.mp4', '.mov', '.avi', '.webm'}:
                try:
                    probe_result = subprocess.run(
                        ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                         '-show_entries', 'stream=duration', '-of', 'csv=p=0',
                         str(dest_path)],
                        capture_output=True, text=True, timeout=20
                    )
                    if probe_result.returncode != 0:
                        logger.error(f"Downloaded video is invalid: {dest_path}")
                        dest_path.unlink()  # Clean up invalid file
                        return False
                except subprocess.TimeoutExpired:
                    logger.warning(f"ffprobe timeout validating {dest_path}, assuming valid")
                except Exception as e:
                    logger.warning(f"Could not validate video {dest_path}: {e}")

            logger.info(f"Downloaded media to {dest_path} ({file_size / 1024 / 1024:.1f}MB)")
            return True

        except (ReqConnectionError, ReadTimeout) as e:
            # Clean up partial file before retry
            if dest_path.exists():
                try:
                    dest_path.unlink()
                except Exception:
                    pass

            if attempt < max_retries - 1:
                logger.warning(f"Download attempt {attempt + 1} failed, retrying: {e}")
                time.sleep(5)
            else:
                logger.error(f"Failed to download after {max_retries} attempts: {e}")
                return False

        except Exception as e:
            # Clean up partial file on unexpected error
            if dest_path.exists():
                try:
                    dest_path.unlink()
                except Exception:
                    pass

            logger.error(f"Error downloading media: {e}", exc_info=True)
            return False

    return False


def get_video_duration(file_path: Path) -> Optional[float]:
    """
    Get the duration of a video file using ffprobe.

    Returns:
        Duration in seconds, or None if unable to determine.
    """
    import subprocess

    try:
        result = subprocess.run(
            [
                'ffprobe', '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                str(file_path)
            ],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception as e:
        logger.warning(f"Could not get video duration for {file_path}: {e}")

    return None


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


def cleanup_unused_media(referenced_files: set) -> int:
    """
    Remove media files that are no longer referenced by any scene.

    Args:
        referenced_files: Set of filenames (not full paths) that are currently in use.

    Returns:
        Number of files deleted.
    """
    if not LIVE_MEDIA_DIR.exists():
        return 0

    # Files to always keep (if any)
    always_keep = set()

    deleted_count = 0
    total_bytes_freed = 0

    try:
        for file_path in LIVE_MEDIA_DIR.iterdir():
            if not file_path.is_file():
                continue

            filename = file_path.name

            # Skip files that are referenced or should always be kept
            if filename in referenced_files or filename in always_keep:
                continue

            # Delete unused file
            try:
                file_size = file_path.stat().st_size
                file_path.unlink()
                deleted_count += 1
                total_bytes_freed += file_size
                logger.info(f"Deleted unused media file: {filename} ({file_size / 1024 / 1024:.1f}MB)")
            except Exception as e:
                logger.warning(f"Failed to delete {filename}: {e}")

        if deleted_count > 0:
            logger.info(f"Cleanup complete: deleted {deleted_count} files, freed {total_bytes_freed / 1024 / 1024:.1f}MB")

    except Exception as e:
        logger.error(f"Error during media cleanup: {e}", exc_info=True)

    return deleted_count


def load_content() -> bool:
    """
    Fetch content from API and download all media files.

    Downloads both images and videos based on the scene's mediaType.
    Writes scene configs for jam_player_display to consume.

    Returns:
        True if successful, False otherwise.
    """
    # Fetch content from API
    scenes = fetch_content()
    if scenes is None:
        logger.error("Failed to fetch content from API")
        return False

    # Track how many scenes the API returned (before download attempts)
    api_scene_count = len(scenes) if scenes else 0

    if not scenes:
        logger.warning("No scenes returned from API")
        # Still write empty scenes file so player knows there's nothing to show

    # Clear staged directory
    if STAGED_SCENES_DIR.exists():
        shutil.rmtree(STAGED_SCENES_DIR)

    # Ensure directories exist
    STAGED_SCENES_DIR.mkdir(parents=True, exist_ok=True)
    LIVE_MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    # Process each scene (API returns them in order by Scene.order)
    processed_scenes = []
    for order_index, scene in enumerate(scenes):
        scene_id = scene.get('id')

        # mediaType is an object: {"value": "CANVAS_IMAGE", "label": "Canvas Image"}
        media_type_obj = scene.get('mediaType', {})
        media_type_value = media_type_obj.get('value', 'CANVAS_IMAGE') if isinstance(media_type_obj, dict) else 'CANVAS_IMAGE'

        image_url = scene.get('imageUrl')
        video_url = scene.get('videoUrl')
        duration = scene.get('duration')
        days_scheduled = scene.get('daysScheduled', [])

        # Determine if this is an image or video scene based on mediaType
        # IMAGE types: CANVAS_IMAGE, and CANVAS_BRAND_AD with image
        # VIDEO types: CANVAS_VIDEO, BRAND_VIDEO_AD, MENU_PULSE_GROUP_BRAND_VIDEO_AD
        is_video = media_type_value in ('CANVAS_VIDEO', 'BRAND_VIDEO_AD', 'MENU_PULSE_GROUP_BRAND_VIDEO_AD')

        # For CANVAS_BRAND_AD and MENU_PULSE_GROUP_CANVAS_BRAND_AD, check which URL is present
        if media_type_value in ('CANVAS_BRAND_AD', 'MENU_PULSE_GROUP_CANVAS_BRAND_AD'):
            is_video = video_url is not None and image_url is None

        # Get the appropriate media URL
        if is_video:
            media_url = video_url
            local_media_type = 'VIDEO'
        else:
            media_url = image_url
            local_media_type = 'IMAGE'

        if not media_url:
            logger.warning(f"Scene {scene_id} has no media URL (type={media_type_value}), skipping")
            continue

        if duration is None:
            logger.warning(f"Scene {scene_id} has no duration, skipping")
            continue

        # Generate filename from URL hash
        url_hash = hash_string(media_url)
        extension = get_file_extension_from_url(media_url)
        media_filename = f"{url_hash}{extension}"
        media_path = LIVE_MEDIA_DIR / media_filename

        # Download if not already present or if existing file is invalid
        need_download = False
        if not media_path.exists():
            need_download = True
        else:
            # Verify existing file is valid (non-empty)
            file_size = media_path.stat().st_size
            if file_size == 0:
                logger.warning(f"Existing file is empty, re-downloading: {media_path}")
                media_path.unlink()
                need_download = True

        if need_download:
            if not download_media(media_url, media_path):
                logger.error(f"Failed to download media for scene {scene_id}, skipping")
                continue

        # Final verification before adding to scenes
        if not media_path.exists():
            logger.error(f"Media file missing after download for scene {scene_id}, skipping")
            continue

        logger.info(f"Scene {scene_id}: type={local_media_type}, duration={duration}s, scheduled_days={len(days_scheduled)}")

        # Build processed scene data (order preserves API order)
        processed_scene = {
            'id': scene_id,
            'order': order_index,
            'media_file': media_filename,
            'media_type': local_media_type,  # 'IMAGE' or 'VIDEO'
            'duration': duration,
            'days_scheduled': days_scheduled,  # For display service to filter by day/time
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

    if not processed_scenes:
        # Distinguish between "API returned no scenes" vs "all downloads failed"
        if api_scene_count > 0:
            # API returned scenes but we couldn't download any of them
            # Keep existing content rather than blanking the display
            logger.error(
                f"API returned {api_scene_count} scenes but all downloads failed - "
                "keeping existing content to avoid blank display"
            )
            return False
        else:
            # API explicitly returned no scenes - this might be intentional
            # (user removed all content from the screen)
            logger.info("No scenes returned from API - clearing live content")
            LIVE_SCENES_DIR.mkdir(parents=True, exist_ok=True)
            live_scenes_path = LIVE_SCENES_DIR / "scenes.json"
            with open(live_scenes_path, 'w') as f:
                json.dump([], f)
            logger.info("Live content cleared - player should show waiting screen")
            return True

    # Calculate total duration and write metadata
    total_duration = sum(s.get('duration', 0) for s in processed_scenes)
    content_meta = {
        'total_duration': total_duration,
        'scene_count': len(processed_scenes),
        'scenes': processed_scenes,
    }
    with open(STAGED_SCENES_DIR / "content_meta.json", 'w') as f:
        json.dump(content_meta, f, indent=2)
    logger.info(f"Content metadata written: {total_duration:.1f}s total, {len(processed_scenes)} scenes")

    # Atomically swap staged to live using a safe 3-step process:
    # 1. Copy staged to a NEW temp directory (if interrupted, live is untouched)
    # 2. Rename live -> live.old, temp -> live (atomic renames)
    # 3. Delete old backup
    # This prevents 0-byte files if the process is killed mid-operation
    live_backup = LIVE_SCENES_DIR.with_suffix('.old')
    live_new = LIVE_SCENES_DIR.with_suffix('.new')

    try:
        # Clean up any leftover temp directories from previous failed swaps
        if live_new.exists():
            shutil.rmtree(live_new)

        # Step 1: Copy staged to new temp location (safe - doesn't touch live)
        shutil.copytree(STAGED_SCENES_DIR, live_new)

        # Step 2a: Move current live to backup (atomic)
        if live_backup.exists():
            shutil.rmtree(live_backup)
        if LIVE_SCENES_DIR.exists():
            LIVE_SCENES_DIR.rename(live_backup)

        # Step 2b: Move new to live (atomic)
        live_new.rename(LIVE_SCENES_DIR)

        # Step 3: Remove backup only after successful swap
        if live_backup.exists():
            shutil.rmtree(live_backup)

    except Exception as e:
        logger.error(f"Error during atomic swap: {e}")
        # Try to recover: if live is gone but backup exists, restore it
        if live_backup.exists() and not LIVE_SCENES_DIR.exists():
            logger.info("Restoring from backup after failed swap")
            live_backup.rename(LIVE_SCENES_DIR)
        # Clean up failed new directory
        if live_new.exists():
            shutil.rmtree(live_new)
        raise

    # Clean up media files that are no longer referenced
    # The atomic swap above ensures display service sees consistent content,
    # so it's safe to clean up immediately
    # IMPORTANT: Only cleanup if we have referenced files - never delete everything
    # This protects against API returning empty scenes (backend bug, user error, etc.)
    referenced_files = {s.get('media_file') for s in processed_scenes if s.get('media_file')}
    if referenced_files:
        cleanup_unused_media(referenced_files)
    else:
        logger.warning("No referenced media files - skipping cleanup to preserve existing content")

    logger.info("Content loaded successfully")
    return True


def recover_from_corrupt_live_scenes():
    """
    Check if live_scenes has corrupt (0-byte) files and recover from staged_scenes.

    This handles the case where the device was powered off during a content swap,
    leaving live_scenes with truncated files.
    """
    live_scenes_json = LIVE_SCENES_DIR / "scenes.json"
    staged_scenes_json = STAGED_SCENES_DIR / "scenes.json"

    # Check if live scenes.json exists but is empty/corrupt
    if live_scenes_json.exists():
        try:
            if live_scenes_json.stat().st_size == 0:
                logger.warning("live_scenes/scenes.json is 0 bytes (corrupt)")

                # Check if staged has valid content
                if staged_scenes_json.exists() and staged_scenes_json.stat().st_size > 0:
                    logger.info("Recovering from staged_scenes...")
                    if LIVE_SCENES_DIR.exists():
                        shutil.rmtree(LIVE_SCENES_DIR)
                    shutil.copytree(STAGED_SCENES_DIR, LIVE_SCENES_DIR)
                    logger.info("Recovery complete - copied staged_scenes to live_scenes")
                else:
                    logger.warning("staged_scenes also missing or empty - cannot recover")
        except Exception as e:
            logger.error(f"Error during recovery check: {e}")

    # Also check for .old backup from failed swap
    live_backup = LIVE_SCENES_DIR.with_suffix('.old')
    if live_backup.exists():
        logger.info("Found leftover .old backup from previous failed swap")
        if not LIVE_SCENES_DIR.exists() or (live_scenes_json.exists() and live_scenes_json.stat().st_size == 0):
            logger.info("Restoring from .old backup...")
            if LIVE_SCENES_DIR.exists():
                shutil.rmtree(LIVE_SCENES_DIR)
            live_backup.rename(LIVE_SCENES_DIR)
            logger.info("Restored from .old backup")
        else:
            # live_scenes is fine, just clean up the backup
            shutil.rmtree(live_backup)
            logger.info("Cleaned up leftover .old backup")


def run():
    """Main service loop."""
    logger.info("=" * 60)
    logger.info("JAM Player 2.0 - Scenes Manager Service Starting")
    logger.info("=" * 60)

    # Check for and recover from corrupt live_scenes (e.g., from power loss during swap)
    recover_from_corrupt_live_scenes()

    # Register signal handler for WebSocket-triggered refresh
    signal.signal(signal.SIGUSR1, handle_refresh_signal)
    logger.info("Registered SIGUSR1 handler for WebSocket content refresh")

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

    # Track screen_id.txt to detect when device is linked to a screen
    # This allows immediate content fetch when screen is linked via BLE
    last_screen_id_mtime = None
    try:
        if SCREEN_ID_FILE.exists():
            last_screen_id_mtime = SCREEN_ID_FILE.stat().st_mtime
    except Exception:
        pass

    # Main polling loop
    while True:
        try:
            should_load_content = False

            # Check if WebSocket triggered a refresh (via SIGUSR1)
            if refresh_event.is_set():
                logger.info("WebSocket refresh event received")
                refresh_event.clear()
                should_load_content = True

            # Check for backend updates (hasUnpulledUpdates flag) - fallback polling
            if not should_load_content and check_for_updates():
                logger.info("Backend updates detected via polling")
                should_load_content = True

            # Check if screen_id.txt changed (e.g., device linked via BLE or heartbeat)
            try:
                if SCREEN_ID_FILE.exists():
                    current_mtime = SCREEN_ID_FILE.stat().st_mtime
                    if last_screen_id_mtime is None or current_mtime != last_screen_id_mtime:
                        logger.info("screen_id.txt changed - device linked to screen")
                        last_screen_id_mtime = current_mtime
                        should_load_content = True
                else:
                    # File was deleted (device unlinked)
                    if last_screen_id_mtime is not None:
                        logger.info("screen_id.txt removed - device unlinked from screen")
                        last_screen_id_mtime = None
            except Exception as e:
                logger.warning(f"Error checking screen_id.txt: {e}")

            # Load content if needed
            if should_load_content:
                logger.info("Reloading content...")
                try:
                    if load_content():
                        logger.info("Content reloaded successfully")
                    else:
                        logger.error("Failed to reload content")
                except Exception as e:
                    logger.error(f"Error reloading content: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Error in update check loop: {e}", exc_info=True)

        # Wait for next poll interval, but wake up immediately if refresh signal received
        refresh_event.wait(timeout=POLL_INTERVAL_SECONDS)


def main():
    run()


if __name__ == "__main__":
    logger.info("Starting Scenes Manager Service")
    main()
