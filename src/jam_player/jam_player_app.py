"""
JAM Player Application with Enterprise-Grade Wall-Clock Synchronization

This player uses epoch-based timing with continuous speed adjustment to ensure
multiple devices displaying the same content stay perfectly in sync (<50ms).

Sync Strategy:
1. All devices calculate expected position from wall clock: position = time % duration
2. Continuous monitoring compares actual vs expected position
3. Speed adjustment (not seeking) corrects drift smoothly without visual glitches
4. Seek is only used for large offsets (>500ms) or initial sync

This approach handles:
- Devices starting at different times
- Devices restarting independently
- Content updates at different times
- One device being unplugged and reconnected
"""

import os
import time
import json
import socket
import subprocess
from pathlib import Path
import signal
import sys
from datetime import datetime
from typing import List, Optional, Tuple, Any

from jam_player.utils import scene_update_flag_utils as sufu
from jam_player.utils import logging_utils as lu
from jam_player.utils import system_utils as su
from jam_player.clients.jam_api_client import JamApiClient

logger = lu.get_logger("jam_player_app")


# Sync configuration constants
SYNC_CHECK_INTERVAL_MS = 200      # Check sync every 200ms
SEEK_THRESHOLD_MS = 500           # Only seek if drift > 500ms (emergency)
TARGET_SYNC_TOLERANCE_MS = 10     # Consider "in sync" if within 10ms

# Proportional speed control thresholds and speeds
# Offset ranges and corresponding speed adjustments:
#   0-10ms:   normal speed (1.0x)
#   10-30ms:  gentle correction (1.01x / 0.99x)
#   30-100ms: moderate correction (1.03x / 0.97x)
#   100-500ms: aggressive correction (1.05x / 0.95x)
SPEED_NORMAL = 1.0
SPEED_GENTLE_FAST = 1.01
SPEED_GENTLE_SLOW = 0.99
SPEED_MODERATE_FAST = 1.03
SPEED_MODERATE_SLOW = 0.97
SPEED_AGGRESSIVE_FAST = 1.05
SPEED_AGGRESSIVE_SLOW = 0.95


class MpvIpcClient:
    """
    Client for controlling MPV via JSON IPC protocol.
    Embedded here to avoid import issues during deployment.
    """

    def __init__(self, socket_path: str = "/tmp/mpv-socket"):
        self.socket_path = socket_path
        self.process: Optional[subprocess.Popen] = None
        self.socket: Optional[socket.socket] = None
        self._request_id = 0

    def start_mpv(self, rotation_angle: int = 0) -> bool:
        """Start MPV process with IPC socket enabled."""
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

        self.stop_mpv()

        args = [
            'mpv',
            '--idle=yes',
            '--fullscreen',
            '--no-osc',
            '--no-osd-bar',
            '--no-input-default-bindings',
            '--input-conf=/dev/null',
            '--force-window=yes',
            '--no-terminal',
            '--keep-open=yes',
            '--loop-file=inf',
            '--hr-seek=yes',
            '--hr-seek-framedrop=no',  # Don't drop frames during seek
            '--video-sync=audio',       # Sync video to audio clock
            '--cache=yes',
            '--demuxer-max-bytes=150M',
            '--demuxer-readahead-secs=20',
            f'--video-rotate={rotation_angle}',
            f'--input-ipc-server={self.socket_path}',
        ]

        try:
            logger.info(f"Starting MPV with IPC socket at {self.socket_path}")
            self.process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            for _ in range(50):
                if os.path.exists(self.socket_path):
                    time.sleep(0.1)
                    return True
                time.sleep(0.1)

            logger.error("MPV socket not created within timeout")
            return False

        except Exception as e:
            logger.error(f"Failed to start MPV: {e}")
            return False

    def stop_mpv(self):
        """Stop the MPV process and clean up."""
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None

        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            except:
                pass
            self.process = None

        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except:
                pass

    def _connect(self) -> bool:
        """Establish connection to MPV socket."""
        if self.socket:
            return True

        try:
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.socket.connect(self.socket_path)
            self.socket.settimeout(2.0)
            return True
        except Exception as e:
            logger.error(f"Failed to connect to MPV socket: {e}")
            self.socket = None
            return False

    def _send_command(self, command: list, wait_response: bool = True) -> Optional[Any]:
        """Send a command to MPV via IPC."""
        # Always reconnect to avoid stale socket state
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None

        if not self._connect():
            return None

        self._request_id += 1
        request = {
            'command': command,
            'request_id': self._request_id
        }

        try:
            msg = json.dumps(request) + '\n'
            self.socket.sendall(msg.encode('utf-8'))

            if wait_response:
                response_data = b''
                while True:
                    chunk = self.socket.recv(4096)
                    if not chunk:
                        break
                    response_data += chunk
                    if b'\n' in response_data:
                        break

                decoded = response_data.decode('utf-8').strip()
                for line in decoded.split('\n'):
                    if line:
                        try:
                            resp = json.loads(line)
                            if resp.get('request_id') == self._request_id:
                                if resp.get('error') == 'success':
                                    return resp.get('data')
                                else:
                                    # Don't log for common "property unavailable" errors
                                    err = resp.get('error', '')
                                    if 'unavailable' not in err.lower():
                                        logger.warning(f"MPV command {command[0]} error: {err}")
                                    return None
                        except json.JSONDecodeError:
                            continue

            return None

        except Exception as e:
            logger.error(f"Failed to send command to MPV: {e}")
            try:
                self.socket.close()
            except:
                pass
            self.socket = None
            return None

    def load_file(self, filepath: str) -> bool:
        """Load a media file into MPV."""
        result = self._send_command(['loadfile', filepath, 'replace'])
        return True

    def seek(self, position_seconds: float) -> bool:
        """Seek to a position in seconds (absolute)."""
        return self._send_command(['seek', str(position_seconds), 'absolute']) is not None

    def set_property(self, name: str, value: Any) -> bool:
        """Set an MPV property value."""
        return self._send_command(['set_property', name, value]) is not None

    def get_property(self, name: str) -> Optional[Any]:
        """Get an MPV property value."""
        return self._send_command(['get_property', name])

    def get_duration(self) -> Optional[float]:
        """Get the duration of the current file in seconds."""
        return self.get_property('duration')

    def get_playback_time(self) -> Optional[float]:
        """Get the current playback position in seconds."""
        return self.get_property('playback-time')

    def set_speed(self, speed: float) -> bool:
        """Set playback speed (1.0 = normal)."""
        return self.set_property('speed', speed)

    def get_speed(self) -> Optional[float]:
        """Get current playback speed."""
        return self.get_property('speed')


class MediaPlayer:
    def __init__(self, media_directory, scenes_directory):
        self.media_directory = Path(media_directory)
        self.scenes_directory = Path(scenes_directory)
        self.default_image_path = Path('/home/comitup/.jam/static_images/ready_for_content.jpeg')
        self.running = True

        # Loop video state
        self.loop_video_path: Optional[Path] = None
        self.loop_duration_ms: int = 0
        self.is_playing: bool = False

        # Sync state
        self.current_speed: float = SPEED_NORMAL
        self.last_sync_log_time: float = 0
        self.sync_stats = {'adjustments': 0, 'seeks': 0, 'in_sync_count': 0}

        # Initialize MPV IPC client
        self.mpv = MpvIpcClient()

        # Get orientation from JAM client
        try:
            self.jam_client = JamApiClient(logger)
        except Exception as e:
            logger.error(f"Caught exception initializing JamApiClient: {e}")

            class OfflineApiClient:
                jam_player_info = {}
                try:
                    json_path = "/home/comitup/.jam/device_data/jam_player_info.json"
                    if os.path.exists(json_path):
                        with open(json_path, 'r') as f:
                            jam_player_info = json.load(f)
                except Exception as e:
                    logger.error(f"Error reading jam_player_info.json: {e}")

            self.jam_client = OfflineApiClient()
            logger.info(
                f"Using offline API client ... jam_player_info: {self.jam_client.jam_player_info}"
            )

        orientation = self.jam_client.jam_player_info.get("orientation", "LANDSCAPE")
        logger.info(f"    Detected JAM Player Orientation: {orientation}")
        self.screen_config = su.get_screen_config(orientation)

        # Set up signal handler for graceful shutdown
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)

    def signal_handler(self, signum, frame):
        self.running = False
        self.cleanup()
        sys.exit(0)

    def cleanup(self):
        """Clean up resources and ensure process termination"""
        logger.info("Cleaning up MPV...")
        self.mpv.stop_mpv()

    def get_current_wall_clock_ms(self) -> int:
        """Get current wall clock time in milliseconds since epoch."""
        return int(time.time() * 1000)

    def find_loop_video(self) -> Optional[Path]:
        """
        Find the video to play as the loop.

        Priority:
        1. loop.mp4 in media directory (backend-generated stitched video)
        2. Single video scene (for testing)
        """
        # Check for backend-generated loop file
        loop_file = self.media_directory / "loop.mp4"
        if loop_file.exists():
            logger.info(f"Found backend-generated loop: {loop_file}")
            return loop_file

        # Fall back to finding a single video scene (for testing)
        video_files = []
        for scene_file in self.scenes_directory.glob('*.json'):
            try:
                with open(scene_file, 'r') as f:
                    scene = json.load(f)
                    if scene.get('media_type') in ('VIDEO', 'BRAND_VIDEO'):
                        media_path = self.media_directory / scene['media_file']
                        if media_path.exists():
                            video_files.append(media_path)
            except Exception as e:
                logger.error(f"Error reading scene file {scene_file}: {e}")

        if len(video_files) == 1:
            logger.info(f"Found single video scene for loop testing: {video_files[0]}")
            return video_files[0]
        elif len(video_files) > 1:
            logger.info(f"Found {len(video_files)} video scenes - using first one for testing")
            return video_files[0]

        logger.warning("No loop video found")
        return None

    def calculate_expected_position_ms(self, duration_ms: int) -> int:
        """
        Calculate where in the loop we should be right now based on wall clock.

        This is the core sync algorithm - all devices with the same duration
        will calculate the same position at the same wall clock time.
        """
        current_time_ms = self.get_current_wall_clock_ms()
        position_ms = current_time_ms % duration_ms
        return position_ms

    def get_sync_offset_ms(self, duration_ms: int) -> Optional[int]:
        """
        Calculate the offset between actual and expected position.

        Returns:
            Positive value = actual is AHEAD of expected (need to slow down)
            Negative value = actual is BEHIND expected (need to speed up)
            None if unable to determine
        """
        expected_ms = self.calculate_expected_position_ms(duration_ms)
        actual_sec = self.mpv.get_playback_time()

        if actual_sec is None:
            return None

        actual_ms = int(actual_sec * 1000)

        # Calculate raw offset
        offset_ms = actual_ms - expected_ms

        # Handle wrap-around near loop boundary
        # If offset is more than half the duration, we wrapped
        if offset_ms > duration_ms / 2:
            offset_ms = offset_ms - duration_ms
        elif offset_ms < -duration_ms / 2:
            offset_ms = offset_ms + duration_ms

        return offset_ms

    def adjust_sync(self, duration_ms: int) -> None:
        """
        Core sync adjustment logic using proportional speed control.

        This is called frequently (every ~200ms) and makes speed
        adjustments proportional to the offset magnitude.
        """
        offset_ms = self.get_sync_offset_ms(duration_ms)

        if offset_ms is None:
            return

        abs_offset = abs(offset_ms)
        current_time = time.time()

        # Determine action based on offset magnitude (proportional control)
        if abs_offset > SEEK_THRESHOLD_MS:
            # Very large offset - emergency seek required
            expected_ms = self.calculate_expected_position_ms(duration_ms)
            expected_sec = expected_ms / 1000.0
            logger.warning(f"EMERGENCY SEEK: offset={offset_ms}ms, seeking to {expected_sec:.2f}s")
            self.mpv.seek(expected_sec)
            self.mpv.set_speed(SPEED_NORMAL)
            self.current_speed = SPEED_NORMAL
            self.sync_stats['seeks'] += 1

        elif abs_offset > 100:
            # Large offset (100-500ms) - aggressive correction
            new_speed = SPEED_AGGRESSIVE_FAST if offset_ms < 0 else SPEED_AGGRESSIVE_SLOW
            if new_speed != self.current_speed:
                self.mpv.set_speed(new_speed)
                self.current_speed = new_speed
                self.sync_stats['adjustments'] += 1

        elif abs_offset > 30:
            # Moderate offset (30-100ms) - moderate correction
            new_speed = SPEED_MODERATE_FAST if offset_ms < 0 else SPEED_MODERATE_SLOW
            if new_speed != self.current_speed:
                self.mpv.set_speed(new_speed)
                self.current_speed = new_speed
                self.sync_stats['adjustments'] += 1

        elif abs_offset > TARGET_SYNC_TOLERANCE_MS:
            # Small offset (10-30ms) - gentle correction
            new_speed = SPEED_GENTLE_FAST if offset_ms < 0 else SPEED_GENTLE_SLOW
            if new_speed != self.current_speed:
                self.mpv.set_speed(new_speed)
                self.current_speed = new_speed
                self.sync_stats['adjustments'] += 1

        else:
            # In sync (0-10ms) - normal speed
            if self.current_speed != SPEED_NORMAL:
                self.mpv.set_speed(SPEED_NORMAL)
                self.current_speed = SPEED_NORMAL
            self.sync_stats['in_sync_count'] += 1

        # Log sync status periodically (every 5 seconds)
        if current_time - self.last_sync_log_time >= 5.0:
            self.last_sync_log_time = current_time
            speed_str = f"{self.current_speed:.2f}x" if self.current_speed != 1.0 else "1.0x"
            status = "IN_SYNC" if abs_offset <= TARGET_SYNC_TOLERANCE_MS else "ADJUSTING"

            # Get detailed position info for diagnostics
            wall_clock_ms = self.get_current_wall_clock_ms()
            expected_ms = self.calculate_expected_position_ms(duration_ms)
            actual_sec = self.mpv.get_playback_time()
            actual_ms = int(actual_sec * 1000) if actual_sec else 0

            logger.info(
                f"SYNC [{status}]: offset={offset_ms:+d}ms speed={speed_str} "
                f"| wall={wall_clock_ms} expected={expected_ms}ms actual={actual_ms}ms "
                f"| stats={{seeks:{self.sync_stats['seeks']}, adj:{self.sync_stats['adjustments']}, sync:{self.sync_stats['in_sync_count']}}}"
            )

    def initial_sync(self, duration_ms: int) -> bool:
        """
        Perform initial synchronization when starting playback.

        Waits for a "clean" time boundary to minimize initial offset variance.
        """
        # Calculate target position
        expected_ms = self.calculate_expected_position_ms(duration_ms)
        expected_sec = expected_ms / 1000.0

        wall_clock_ms = self.get_current_wall_clock_ms()
        logger.info(
            f"INITIAL SYNC: wall_clock={wall_clock_ms} "
            f"duration={duration_ms}ms target_position={expected_ms}ms ({expected_sec:.3f}s)"
        )

        # Seek to calculated position
        self.mpv.seek(expected_sec)

        # Ensure playing at normal speed
        self.mpv.set_property('pause', False)
        self.mpv.set_speed(SPEED_NORMAL)
        self.current_speed = SPEED_NORMAL

        # Wait a moment for seek to complete, then verify
        time.sleep(0.3)

        # Check initial offset
        offset_ms = self.get_sync_offset_ms(duration_ms)
        if offset_ms is not None:
            logger.info(f"INITIAL SYNC COMPLETE: initial_offset={offset_ms:+d}ms")
        else:
            logger.warning("INITIAL SYNC: Could not verify offset")

        return True

    def run(self):
        """Main loop - plays video on infinite loop with continuous sync adjustment."""
        logger.info("=" * 60)
        logger.info("STARTING JAM PLAYER - ENTERPRISE SYNC MODE v2 (Proportional)")
        logger.info(f"Sync config: check={SYNC_CHECK_INTERVAL_MS}ms, "
                   f"tolerance={TARGET_SYNC_TOLERANCE_MS}ms, "
                   f"seek_threshold={SEEK_THRESHOLD_MS}ms")
        logger.info(f"Speed tiers: 0-{TARGET_SYNC_TOLERANCE_MS}ms=1.0x, "
                   f"{TARGET_SYNC_TOLERANCE_MS}-30ms=±1%, 30-100ms=±3%, 100-500ms=±5%")
        logger.info("=" * 60)

        # Start MPV with IPC
        rotation = self.screen_config.get_pygame_rotation()
        if not self.mpv.start_mpv(rotation_angle=rotation):
            logger.error("Failed to start MPV, exiting")
            return

        logger.info("MPV started successfully with IPC control")

        last_sync_check = time.time()

        try:
            while self.running:
                # Check for scene updates from backend
                if sufu.should_reload_scenes():
                    logger.info("Content update detected - reloading")
                    sufu.reset_update_flag_to_zero()
                    self.is_playing = False
                    self.loop_video_path = None
                    self.sync_stats = {'adjustments': 0, 'seeks': 0, 'in_sync_count': 0}

                # Find loop video if we don't have one
                if self.loop_video_path is None:
                    self.loop_video_path = self.find_loop_video()

                    if self.loop_video_path is None:
                        logger.info("No loop video available, waiting...")
                        time.sleep(5)
                        continue

                # Start playback if not playing
                if not self.is_playing:
                    logger.info(f"Loading loop video: {self.loop_video_path}")
                    self.mpv.load_file(str(self.loop_video_path))

                    # Poll for duration to become available
                    duration_sec = None
                    for attempt in range(30):  # Try for up to 15 seconds
                        time.sleep(0.5)
                        duration_sec = self.mpv.get_duration()
                        if duration_sec is not None and duration_sec > 0:
                            break
                        if attempt % 4 == 0:  # Log every 2 seconds
                            logger.info(f"Waiting for video duration... attempt {attempt + 1}/30")

                    if duration_sec is None or duration_sec <= 0:
                        logger.error("Could not get video duration after 15 seconds, restarting MPV...")
                        self.mpv.stop_mpv()
                        time.sleep(1)
                        if not self.mpv.start_mpv(rotation_angle=rotation):
                            logger.error("Failed to restart MPV")
                            time.sleep(5)
                        self.loop_video_path = None
                        continue

                    self.loop_duration_ms = int(duration_sec * 1000)
                    logger.info(f"Loop duration: {self.loop_duration_ms}ms ({duration_sec:.2f}s)")

                    # Perform initial synchronization
                    self.initial_sync(self.loop_duration_ms)
                    self.is_playing = True
                    last_sync_check = time.time()

                # Continuous sync adjustment
                current_time = time.time()
                if (current_time - last_sync_check) * 1000 >= SYNC_CHECK_INTERVAL_MS:
                    last_sync_check = current_time
                    self.adjust_sync(self.loop_duration_ms)

                # Small sleep to prevent CPU spinning
                time.sleep(0.05)  # 50ms

        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
            self.cleanup()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='JAM Player - Enterprise Sync Mode')
    parser.add_argument('media_directory', help='Directory containing media files')
    parser.add_argument('scenes_directory', help='Directory containing scene configuration JSON files')

    args = parser.parse_args()

    player = MediaPlayer(args.media_directory, args.scenes_directory)
    player.run()

    try:
        subprocess.run(['pkill', 'feh'], check=False)
    except Exception as e:
        logger.error(f"Error killing feh processes: {e}")
