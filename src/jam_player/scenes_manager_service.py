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
from typing import List, Optional, Dict, Any, Tuple
from pathlib import Path

from jam_player import constants

from common.api import api_request
from common.credentials import get_device_uuid, is_device_registered
from common.network import device_is_offline
from common.logging_config import setup_service_logging
from common.paths import SCREEN_ID_FILE

logger = setup_service_logging("jam-content-manager")

# Directories for content storage
LIVE_SCENES_DIR = Path(constants.APP_DATA_LIVE_SCENES_DIR)
LIVE_MEDIA_DIR = Path(constants.APP_DATA_LIVE_MEDIA_DIR)
STAGED_SCENES_DIR = Path(constants.APP_DATA_STAGED_SCENES_DIR)

# Polling interval in seconds (fallback when WebSocket push fails)
POLL_INTERVAL_SECONDS = 300

# load_content() outcomes. A PARTIAL load published what it could but at least
# one asset failed to download; FAILED published nothing new. Both mean the
# device still OWES a refresh and must retry on its own: the backend consumes
# hasUnpulledUpdates the moment we poll it, and the WebSocket push already
# happened, so nothing external will ask again until the customer republishes.
LOAD_COMPLETE = 'complete'
LOAD_PARTIAL = 'partial'
LOAD_FAILED = 'failed'
REFRESH_RETRY_INITIAL_SECONDS = 30
REFRESH_RETRY_MAX_SECONDS = POLL_INTERVAL_SECONDS

# Event to signal immediatejam-ha  content refresh (set by SIGUSR1 handler)
refresh_event = threading.Event()


_poll_failures = {}


def _log_poll_failure(key: str, message: str) -> None:
    """
    First failure of a poll site is a WARNING (kept on the SD card); repeats
    are DEBUG (shipped to the backend only). A registered player polls every
    300 s, so an offline week used to be a steady drip of card writes with no
    new information after the first line.
    """
    count = _poll_failures.get(key, 0) + 1
    _poll_failures[key] = count
    if count == 1:
        logger.warning(message)
    else:
        logger.debug(f"{message} (consecutive failure #{count})")


def _note_poll_success(key: str) -> None:
    count = _poll_failures.pop(key, 0)
    if count:
        logger.info(f"{key} recovered after {count} consecutive failure(s)")


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
            _log_poll_failure("update-poll", "No response from update-poll endpoint")
            return False

        if response.status_code != 200:
            _log_poll_failure("update-poll", f"update-poll returned {response.status_code}: {response.text}")
            return False

        _note_poll_success("update-poll")
        data = response.json()
        has_updates = data.get('hasUnpulledUpdates', False)

        if has_updates:
            logger.info("Updates available")

        return has_updates

    except Exception as e:
        logger.error(f"Error checking for updates: {e}", exc_info=True)
        return False


def fetch_content() -> Optional[Tuple[List[Dict[str, Any]], Optional[int]]]:
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
            _log_poll_failure("content", "No response from content endpoint")
            return None

        if response.status_code != 200:
            _log_poll_failure("content", f"content endpoint returned {response.status_code}: {response.text}")
            return None

        _note_poll_success("content")
        data = response.json()
        scenes = data.get('jamPlayerScenes', [])
        # numScreens: how many screens are in this JP's layout (added additively
        # by the backend). None if an older backend didn't include it -- the
        # display treats "unknown" as "do NOT wall-clock-sync" (safe single-
        # screen default), so this stays fully backward-compatible.
        num_screens = data.get('numScreens')
        logger.info(f"Fetched {len(scenes)} scenes from API (numScreens={num_screens})")
        return scenes, num_screens

    except Exception as e:
        logger.error(f"Error fetching content: {e}", exc_info=True)
        return None


# --- download integrity ---------------------------------------------------
# Media used to stream straight into its final filename with no fsync and no
# verification, and the reuse check was "exists and non-empty". The process
# handled only SIGUSR1, so a `systemctl restart` mid-transfer (SET_SCREEN_ID,
# an update, the health monitor, a reboot) left a truncated file under the
# final name that was then reused -- and published -- forever. Now: stream to
# `<name>.part`, fsync, verify (Content-Length, ffprobe / PIL), rename into
# place, and record the expected size in `<name>.size` so reuse can check it.
_inflight_part = {'path': None}  # the .part being written right now (SIGTERM cleanup)
_VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.webm'}
_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}


def _part_path_for(dest_path: Path) -> Path:
    return dest_path.with_name(dest_path.name + '.part')


def _size_path_for(dest_path: Path) -> Path:
    return dest_path.with_name(dest_path.name + '.size')


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug(f"Could not remove {path.name}: {e}")


def _fsync_dir(dir_path: Path) -> None:
    try:
        fd = os.open(str(dir_path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass


def _write_expected_size(dest_path: Path, size: int) -> None:
    """Record the verified byte count beside the file (tmp + rename)."""
    size_path = _size_path_for(dest_path)
    tmp = size_path.with_name(size_path.name + '.tmp')
    try:
        with open(tmp, 'w') as f:
            f.write(str(int(size)))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, size_path)
    except Exception as e:
        logger.debug(f"Could not write size sidecar for {dest_path.name}: {e}")
        _unlink_quiet(tmp)


def _read_expected_size(dest_path: Path) -> Optional[int]:
    try:
        return int(_size_path_for(dest_path).read_text().strip())
    except Exception:
        return None


def _media_content_is_valid(file_path: Path, kind_path: Path) -> Tuple[bool, str]:
    """
    Content-level check. `kind_path` carries the real extension (file_path may be
    a .part). Video: ffprobe must parse a video stream. Image: PIL must verify
    it. Unknown types pass on size alone. Tooling failures (ffprobe timeout,
    PIL missing) are treated as valid -- same stance as before: never refuse to
    play content because a validator is unavailable.
    """
    suffix = kind_path.suffix.lower()
    if suffix in _VIDEO_EXTS:
        try:
            probe = subprocess.run(
                ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                 '-show_entries', 'stream=duration', '-of', 'csv=p=0', str(file_path)],
                capture_output=True, text=True, timeout=20
            )
            if probe.returncode != 0:
                return False, 'ffprobe could not parse the video'
        except subprocess.TimeoutExpired:
            logger.warning(f"ffprobe timeout validating {file_path.name}, assuming valid")
        except Exception as e:
            logger.warning(f"Could not validate video {file_path.name}: {e}")
        return True, ''
    if suffix in _IMAGE_EXTS:
        try:
            from PIL import Image  # lazy: optional here
            with Image.open(file_path) as im:
                im.verify()
        except ImportError:
            return True, ''
        except Exception as e:
            return False, f'PIL could not verify the image ({e})'
    return True, ''


def _verify_download(part_path: Path, bytes_written: int, expected_len: Optional[int],
                     dest_path: Path) -> Tuple[bool, str]:
    if bytes_written == 0:
        return False, 'empty response'
    if expected_len is not None and bytes_written != expected_len:
        return False, f'received {bytes_written} bytes, Content-Length said {expected_len}'
    return _media_content_is_valid(part_path, dest_path)


def _existing_media_is_trustworthy(media_path: Path) -> bool:
    """
    Reuse decision for a file already on disk.
      - empty -> no.
      - size sidecar present -> the byte count must match exactly.
      - no sidecar (downloaded by older firmware) -> validate the CONTENT once;
        if it passes, adopt it by writing the sidecar so later loads are a
        cheap size compare. This is what heals a truncated file left behind
        by fielded 7440d2d on the first load after the update.
    """
    try:
        size = media_path.stat().st_size
    except Exception:
        return False
    if size == 0:
        return False
    expected = _read_expected_size(media_path)
    if expected is not None:
        return size == expected
    ok, why = _media_content_is_valid(media_path, media_path)
    if not ok:
        logger.warning(f"Existing media {media_path.name} failed validation ({why}); will re-download")
        return False
    _write_expected_size(media_path, size)
    return True


def _remove_stale_partials() -> None:
    """A .part is by definition incomplete; sweep any left by a kill or power loss."""
    try:
        for part in LIVE_MEDIA_DIR.glob('*.part'):
            _unlink_quiet(part)
            logger.info(f"Removed stale partial download: {part.name}")
    except Exception as e:
        logger.debug(f"Could not sweep partial downloads: {e}")


def handle_terminate(signum, frame):
    """
    SIGTERM (systemctl stop/restart): drop the in-flight .part and exit cleanly.
    Before this handler existed the process died mid-write and the truncated
    file -- under its FINAL name -- was reused on the next start.
    """
    part = _inflight_part.get('path')
    if part is not None:
        _unlink_quiet(part)
    logger.info("Received SIGTERM - exiting cleanly")
    raise SystemExit(0)


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

    part_path = _part_path_for(dest_path)
    max_retries = 5
    for attempt in range(max_retries):
        try:
            _inflight_part['path'] = part_path
            bytes_written = 0
            expected_len = None
            # Use streaming download with per-chunk timeout
            # timeout=(connect, read) - read timeout applies between chunks
            with requests.get(url, stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) as response:
                response.raise_for_status()
                # Only trust Content-Length when the body is not transfer-encoded
                # (a gzip'd response reports the compressed length).
                content_length = response.headers.get('Content-Length')
                if content_length and str(content_length).isdigit() and \
                        response.headers.get('Content-Encoding', 'identity') in ('identity', ''):
                    expected_len = int(content_length)
                # Stream into the .part file. dest_path is NEVER opened for
                # writing: an existing good file survives a failed re-download,
                # and a kill mid-transfer can only ever leave a .part behind.
                with open(part_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:  # filter out keep-alive chunks
                            f.write(chunk)
                            bytes_written += len(chunk)
                    f.flush()
                    os.fsync(f.fileno())
            _inflight_part['path'] = None

            ok, why = _verify_download(part_path, bytes_written, expected_len, dest_path)
            if not ok:
                logger.error(f"Downloaded media failed verification ({why}): {dest_path.name}")
                _unlink_quiet(part_path)
                if attempt < max_retries - 1:
                    time.sleep(5)
                    continue
                return False

            # Complete-or-absent: the verified bytes become the real file in one
            # atomic step, then the directory entry is made durable.
            os.replace(part_path, dest_path)
            _fsync_dir(dest_path.parent)
            _write_expected_size(dest_path, bytes_written)
            logger.info(f"Downloaded media to {dest_path} ({bytes_written / 1024 / 1024:.1f}MB)")
            return True

        except (ReqConnectionError, ReadTimeout) as e:
            _inflight_part['path'] = None
            _unlink_quiet(part_path)  # never touch dest_path
            if attempt < max_retries - 1:
                logger.warning(f"Download attempt {attempt + 1} failed, retrying: {e}")
                time.sleep(5)
            else:
                logger.error(f"Failed to download after {max_retries} attempts: {e}")
                return False

        except Exception as e:
            _inflight_part['path'] = None
            _unlink_quiet(part_path)
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

            if filename.endswith('.part') or filename.endswith('.size.tmp'):
                pass  # an incomplete download or its scratch file: always remove
            elif filename.endswith('.size'):
                if filename[:-len('.size')] in referenced_files:
                    continue  # the size sidecar of a file we keep
            elif filename in referenced_files or filename in always_keep:
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


def _clear_empty_live_manifest_before_fetch() -> None:
    """
    Before a fetch, remove the live manifest ONLY if it is an empty list.

    The rule this encodes: a player never stops showing content because of a
    reload attempt. If live_scenes/scenes.json has scenes, it stays until the
    atomic swap replaces it with the new set -- whether the device was just
    linked to a different screen, unlinked, or is offline and cannot fetch at
    all. An offline player MUST keep playing its last downloaded content; a
    relink while offline can only come from the app over BLE, is rare, and the
    person doing it is standing in front of the board. A blank board for as
    long as the WiFi is down is far worse than last night's menu.

    Why remove an EMPTY manifest at all: `[]` is what an unlinked player holds.
    When it gets linked, leaving `[]` in place makes the display say "nothing
    scheduled" (NO_ACTIVE_SCENES) for the whole fetch + download window;
    removing it makes it say "Downloading content", which is true. There is
    no content to lose. Skipped while offline: no fetch can succeed, so the
    only effect would be a pointless screen change.

    History: this used to delete ANY manifest whose mtime was older than
    screen_id.txt -- a re-link of the SAME screen on an offline player (the
    BLE path rewrote the file even for an unchanged value) threw away a good
    cached manifest and dropped the display to the WiFi setup screen with
    every media file still on disk. Best-effort; never blocks a fetch.
    """
    live_scenes_json = LIVE_SCENES_DIR / "scenes.json"
    try:
        if not live_scenes_json.exists():
            return
        if live_scenes_json.stat().st_size > 4:   # cheap pre-check: `[]` (+ whitespace)
            with open(live_scenes_json) as f:
                if json.load(f):
                    return                          # has scenes: never touch it
        elif json.loads(live_scenes_json.read_text() or '[]'):
            return
        if device_is_offline():
            return                                  # nothing to gain while offline
        live_scenes_json.unlink()
        logger.info("Cleared empty live_scenes/scenes.json ahead of fetch (display shows 'Downloading content')")
    except Exception as e:
        # Unreadable/corrupt manifest: leave it to recover_from_corrupt_live_scenes;
        # a failure here must never block the fetch.
        logger.debug(f"Skipping empty-manifest check: {e}")

def load_content() -> str:
    """
    Fetch content from API and download all media files.

    Downloads both images and videos based on the scene's mediaType.
    Writes scene configs for jam_player_display to consume.

    Returns:
        LOAD_COMPLETE  -- every scene the API returned is live (or the API
                          returned none and live content was cleared);
        LOAD_PARTIAL   -- live content was updated but at least one asset could
                          not be downloaded (its scene is missing; retry owed);
        LOAD_FAILED    -- nothing new was published (fetch failed, or every
                          download failed and the previous content was kept).
    """
    # Never delete content ahead of a fetch. Only an EMPTY manifest (`[]`, the
    # unlinked state) is removed, so a freshly linked player shows "Downloading
    # content" rather than "nothing scheduled" while the first set arrives.
    _clear_empty_live_manifest_before_fetch()

    # Fetch content from API
    fetch_result = fetch_content()
    if fetch_result is None:
        logger.error("Failed to fetch content from API")
        return LOAD_FAILED
    scenes, num_screens = fetch_result

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
    download_failures = 0
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

        # Download unless a file is already present AND provably complete
        # (size matches its recorded sidecar, or -- for files from older
        # firmware -- its content validates once). An untrustworthy existing
        # file is left in place until the verified replacement is renamed
        # over it; download_media never opens dest_path for writing.
        need_download = not media_path.exists() or not _existing_media_is_trustworthy(media_path)

        if need_download:
            if not download_media(media_url, media_path):
                logger.error(f"Failed to download media for scene {scene_id}, skipping (retry owed)")
                download_failures += 1
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
            return LOAD_FAILED
        else:
            # API explicitly returned no scenes - this might be intentional
            # (user removed all content from the screen)
            logger.info("No scenes returned from API - clearing live content")
            LIVE_SCENES_DIR.mkdir(parents=True, exist_ok=True)
            live_scenes_path = LIVE_SCENES_DIR / "scenes.json"
            with open(live_scenes_path, 'w') as f:
                json.dump([], f)
            logger.info("Live content cleared - player should show waiting screen")
            return LOAD_COMPLETE

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

    # Persist the layout screen count next to scenes.json so it swaps into
    # LIVE atomically with the content it belongs to. The display reads this
    # to gate wall-clock video sync on being in a multi-screen wall. Only
    # written when the backend actually provided it; absence -> display
    # defaults to no-sync (single-screen-safe).
    if num_screens is not None:
        try:
            with open(STAGED_SCENES_DIR / "num_screens.txt", 'w') as f:
                f.write(str(int(num_screens)))
        except (ValueError, TypeError, OSError) as e:
            logger.warning(f"Could not write num_screens.txt (non-fatal): {e}")

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
    if download_failures:
        # Published what we could, but the set is incomplete. Do NOT clean up:
        # the missing scene's previous asset may still be needed. The main loop
        # retries with backoff until a load completes.
        logger.warning(
            f"{download_failures} scene(s) could not be downloaded; published the rest, "
            "skipping media cleanup, retry owed"
        )
        return LOAD_PARTIAL
    if referenced_files:
        cleanup_unused_media(referenced_files)
    else:
        logger.warning("No referenced media files - skipping cleanup to preserve existing content")

    logger.info("Content loaded successfully")
    return LOAD_COMPLETE


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
    # systemctl stop/restart must not leave a half-written download behind.
    signal.signal(signal.SIGTERM, handle_terminate)
    _remove_stale_partials()

    # Wait for device to be registered before trying to fetch content
    # An unregistered device won't have content assigned anyway
    logger.info("Waiting for device to be registered...")
    while not is_device_registered():
        time.sleep(10)
    logger.info("Device is registered, proceeding with content management")

    # Initial content load with exponential backoff. A PARTIAL load is enough
    # to leave this loop (there is content on the board); the refresh it still
    # owes is retried by the main loop below.
    needs_refresh = False
    retry_delay = 10  # Start with 10 seconds
    max_retry_delay = 300  # Cap at 5 minutes
    while True:
        try:
            logger.info("Loading initial content...")
            result = load_content()
            if result == LOAD_COMPLETE:
                logger.info("Initial content loaded successfully")
                break
            if result == LOAD_PARTIAL:
                logger.warning("Initial content loaded PARTIALLY; will keep retrying the missing assets")
                needs_refresh = True
                break
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

    # Main polling loop. `needs_refresh` is the device's OWN memory that a
    # load did not complete: the backend resets hasUnpulledUpdates the moment
    # we poll it and the WebSocket push is already spent, so without this a
    # failed or partial load stayed wrong until the customer republished.
    refresh_backoff = REFRESH_RETRY_INITIAL_SECONDS
    while True:
        try:
            should_load_content = False

            # Check if WebSocket triggered a refresh (via SIGUSR1)
            if refresh_event.is_set():
                logger.info("WebSocket refresh event received")
                refresh_event.clear()
                should_load_content = True

            # A refresh we still owe from a failed/partial load
            if not should_load_content and needs_refresh:
                logger.info(f"Retrying the content load that did not complete (backoff {refresh_backoff}s)")
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
                    result = load_content()
                    if result == LOAD_COMPLETE:
                        if needs_refresh:
                            logger.info("Owed refresh completed")
                        needs_refresh = False
                        refresh_backoff = REFRESH_RETRY_INITIAL_SECONDS
                        logger.info("Content reloaded successfully")
                    else:
                        needs_refresh = True
                        logger.error(f"Content reload did not complete ({result}); will retry in {refresh_backoff}s")
                except Exception as e:
                    needs_refresh = True
                    logger.error(f"Error reloading content: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Error in update check loop: {e}", exc_info=True)

        # Wait for the next poll -- sooner while a refresh is owed -- but wake
        # immediately on a refresh signal.
        if needs_refresh:
            refresh_event.wait(timeout=refresh_backoff)
            refresh_backoff = min(refresh_backoff * 2, REFRESH_RETRY_MAX_SECONDS)
        else:
            refresh_event.wait(timeout=POLL_INTERVAL_SECONDS)


def main():
    run()


if __name__ == "__main__":
    logger.info("Starting Scenes Manager Service")
    main()
