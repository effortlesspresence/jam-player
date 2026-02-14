"""
JAM Player 2.0 - Scenes Manager Service

This service manages content for the JAM Player:
1. Polls the JAM 2.0 API for content updates
2. Fetches scene content when updates are available
3. Downloads media files (all videos - images are converted to video by backend)
4. Writes scene data for jam_player_display to consume

The backend now handles:
- Converting all image scenes to videos with exact timeToDisplay duration
- Normalizing all videos to the same encoding (libx265, CRF 22, 30fps)
- Adding silent audio tracks for sync

This service just downloads the pre-processed videos and manages scheduling.

TEMPORARY: This polling mechanism will be replaced with WebSocket push
notifications in a future release.
"""

import os
import json
import time
import hashlib
import shutil
import subprocess
import tempfile
from typing import List, Optional, Dict, Any
from pathlib import Path
from datetime import datetime

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
        List of scene dicts with id, videoUrl, videoDuration, daysScheduled, or None on error.
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


def stitch_scenes_to_loop(scenes: List[Dict[str, Any]], media_dir: Path, output_path: Path) -> bool:
    """
    Stitch all scenes into a single loop.mp4 video for gapless playback.

    Uses stream copy (-c copy) for fast concatenation. All input videos
    must be pre-normalized to the same format (done during download).

    Args:
        scenes: List of processed scene dicts with video_clip field
        media_dir: Directory containing media files
        output_path: Where to write the stitched loop.mp4

    Returns:
        True if successful, False otherwise.
    """
    if not scenes:
        logger.warning("No scenes to stitch")
        return False

    logger.info(f"Stitching {len(scenes)} scenes into loop video (stream copy)...")

    try:
        # Create concat file listing all video clips
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            concat_file = f.name

            for scene in scenes:
                video_clip = scene.get('video_clip')
                if not video_clip:
                    logger.warning(f"Scene {scene.get('id')} has no video_clip, skipping")
                    continue

                clip_path = media_dir / video_clip
                if not clip_path.exists():
                    logger.warning(f"Video clip not found: {clip_path}")
                    continue

                f.write(f"file '{clip_path}'\n")

        # Concatenate with stream copy (very fast, no re-encoding)
        stitch_cmd = [
            'ffmpeg', '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', concat_file,
            '-c', 'copy',  # Stream copy - no re-encoding!
            '-movflags', '+faststart',
            str(output_path)
        ]

        logger.info("Running ffmpeg concat with stream copy...")
        start_time = time.time()

        result = subprocess.run(stitch_cmd, capture_output=True, timeout=60)

        elapsed = time.time() - start_time

        if result.returncode != 0:
            logger.error(f"Failed to stitch videos: {result.stderr.decode()[:500]}")
            # Clean up concat file
            try:
                os.unlink(concat_file)
            except:
                pass
            return False

        # Clean up concat file
        os.unlink(concat_file)

        # Get final loop duration
        loop_duration = get_video_duration(output_path)
        file_size_mb = output_path.stat().st_size / (1024 * 1024)

        logger.info(f"Stitched loop.mp4: {loop_duration:.1f}s, {file_size_mb:.1f}MB (took {elapsed:.1f}s)")

        return True

    except subprocess.TimeoutExpired:
        logger.error("Stitch operation timed out")
        return False
    except Exception as e:
        logger.error(f"Error stitching scenes: {e}", exc_info=True)
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

    # Files to always keep (generated files, not downloaded)
    always_keep = {'loop.mp4'}

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

    The backend now handles:
    - Converting all image scenes to videos with exact duration
    - Normalizing all videos to the same encoding
    - Adding silent audio tracks for sync

    This service just downloads pre-processed videos and stores scheduling info.

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
        video_url = scene.get('videoUrl')
        video_duration = scene.get('videoDuration')
        days_scheduled = scene.get('daysScheduled', [])

        if not video_url:
            logger.warning(f"Scene {scene_id} has no videoUrl, skipping")
            continue

        if video_duration is None:
            logger.warning(f"Scene {scene_id} has no videoDuration, skipping")
            continue

        # Generate filename from URL hash
        url_hash = hash_string(video_url)
        extension = get_file_extension_from_url(video_url)
        media_filename = f"{url_hash}{extension}"
        media_path = LIVE_MEDIA_DIR / media_filename

        # Download if not already present
        if not media_path.exists():
            if not download_media(video_url, media_path):
                logger.error(f"Failed to download video for scene {scene_id}, skipping")
                continue

        logger.info(f"Scene {scene_id}: duration={video_duration}s, scheduled_days={len(days_scheduled)}")

        # Build processed scene data (order preserves API order)
        # All content is now video - backend handles image-to-video conversion
        processed_scene = {
            'id': scene_id,
            'order': order_index,
            'media_file': media_filename,
            'media_type': 'VIDEO',
            'duration': video_duration,
            'video_clip': media_filename,  # Same file - already normalized by backend
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

    # Stitch all scenes into a single loop.mp4 for gapless playback
    # Note: All videos are now pre-normalized by backend, so stream copy should work
    loop_path = STAGED_SCENES_DIR / "loop.mp4"
    if processed_scenes:
        if not stitch_scenes_to_loop(processed_scenes, LIVE_MEDIA_DIR, loop_path):
            logger.warning("Failed to create stitched loop - will use scene-by-scene playback")
        else:
            # Calculate total duration and add to scenes.json metadata
            total_duration = sum(s.get('duration', 0) for s in processed_scenes)
            # Write a loop metadata file
            loop_meta = {
                'loop_file': 'loop.mp4',
                'total_duration': total_duration,
                'scene_count': len(processed_scenes),
                'scenes': processed_scenes,
            }
            with open(STAGED_SCENES_DIR / "loop_meta.json", 'w') as f:
                json.dump(loop_meta, f, indent=2)
            logger.info(f"Loop metadata written: {total_duration:.1f}s total")

    # Atomically swap staged to live
    if LIVE_SCENES_DIR.exists():
        shutil.rmtree(LIVE_SCENES_DIR)
    shutil.copytree(STAGED_SCENES_DIR, LIVE_SCENES_DIR)

    # Also copy loop.mp4 to live media dir for easy access
    if loop_path.exists():
        shutil.copy2(loop_path, LIVE_MEDIA_DIR / "loop.mp4")

    # Clean up unused media files to free disk space
    referenced_files = {s.get('media_file') for s in processed_scenes if s.get('media_file')}
    cleanup_unused_media(referenced_files)

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
