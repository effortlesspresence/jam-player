"""
JAM Player 2.0 - Media Player Application

This player displays scenes sequentially:
- Images are displayed for their configured timeToDisplay duration
- Videos play fully before moving to the next scene
- Scenes cycle continuously in order

Uses MPV for all media playback (both images and videos).
"""

import os
import time
import json
import socket
import subprocess
from pathlib import Path
import signal
import sys
from typing import List, Optional, Dict, Any

from jam_player import constants
from jam_player.utils import scene_update_flag_utils as sufu
from jam_player.utils import logging_utils as lu
from jam_player.utils import system_utils as su

logger = lu.get_logger("jam_player_app")


class MpvController:
    """
    Controller for MPV media player via JSON IPC protocol.
    Handles both video and image display.
    """

    def __init__(self, socket_path: str = "/tmp/mpv-socket"):
        self.socket_path = socket_path
        self.process: Optional[subprocess.Popen] = None
        self.socket: Optional[socket.socket] = None
        self._request_id = 0

    def start(self, rotation_angle: int = 0) -> bool:
        """Start MPV process with IPC socket enabled."""
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

        self.stop()

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
            '--image-display-duration=inf',  # Don't auto-advance images
            '--hr-seek=yes',
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

            # Wait for socket to be created
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

    def stop(self):
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
        self._send_command(['loadfile', filepath, 'replace'])
        return True

    def get_property(self, name: str) -> Optional[Any]:
        """Get an MPV property value."""
        return self._send_command(['get_property', name])

    def set_property(self, name: str, value: Any) -> bool:
        """Set an MPV property value."""
        return self._send_command(['set_property', name, value]) is not None

    def get_duration(self) -> Optional[float]:
        """Get the duration of the current file in seconds."""
        return self.get_property('duration')

    def get_playback_time(self) -> Optional[float]:
        """Get the current playback position in seconds."""
        return self.get_property('playback-time')

    def is_paused(self) -> bool:
        """Check if playback is paused."""
        return self.get_property('pause') == True

    def get_eof_reached(self) -> bool:
        """Check if end of file has been reached."""
        return self.get_property('eof-reached') == True


class JamPlayer:
    """
    Main JAM Player application.
    Displays scenes sequentially, cycling continuously.
    """

    def __init__(self, media_directory: str, scenes_directory: str):
        self.media_directory = Path(media_directory)
        self.scenes_directory = Path(scenes_directory)
        self.running = True

        # Scene state
        self.scenes: List[Dict[str, Any]] = []
        self.current_scene_index = 0

        # Initialize MPV controller
        self.mpv = MpvController()

        # Get screen orientation
        self.rotation_angle = self._get_rotation_angle()

        # Set up signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _get_rotation_angle(self) -> int:
        """Get the screen rotation angle from JAM player info."""
        try:
            # Try to read orientation from jam_player_info.json
            json_path = Path("/etc/jam/device_data/jam_player_info.json")
            if json_path.exists():
                with open(json_path, 'r') as f:
                    info = json.load(f)
                    orientation = info.get("orientation", "LANDSCAPE")
                    screen_config = su.get_screen_config(orientation)
                    return screen_config.get_pygame_rotation()
        except Exception as e:
            logger.warning(f"Could not read orientation: {e}")

        return 0  # Default to no rotation

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    def load_scenes(self) -> bool:
        """
        Load scenes from the scenes.json file.

        Returns:
            True if scenes were loaded successfully.
        """
        scenes_file = self.scenes_directory / "scenes.json"

        if not scenes_file.exists():
            logger.warning(f"Scenes file not found: {scenes_file}")
            return False

        try:
            with open(scenes_file, 'r') as f:
                scenes = json.load(f)

            # Sort by order to ensure correct display sequence
            scenes.sort(key=lambda s: s.get('order', 0))

            self.scenes = scenes
            logger.info(f"Loaded {len(self.scenes)} scenes")

            for i, scene in enumerate(self.scenes):
                logger.info(f"  Scene {i}: {scene.get('id')} - {scene.get('media_type')} - {scene.get('media_file')}")

            return len(self.scenes) > 0

        except Exception as e:
            logger.error(f"Error loading scenes: {e}", exc_info=True)
            return False

    def display_scene(self, scene: Dict[str, Any]) -> bool:
        """
        Display a single scene.

        For images: displays for timeToDisplay seconds
        For videos: plays the full video

        Args:
            scene: Scene dict with media_file, media_type, time_to_display

        Returns:
            True if scene was displayed successfully.
        """
        media_file = scene.get('media_file')
        media_type = scene.get('media_type', 'IMAGE')
        time_to_display = scene.get('time_to_display', 15)

        media_path = self.media_directory / media_file

        if not media_path.exists():
            logger.error(f"Media file not found: {media_path}")
            return False

        logger.info(f"Displaying scene: {scene.get('id')} ({media_type})")

        # Load the file
        self.mpv.load_file(str(media_path))

        # Wait a moment for file to load
        time.sleep(0.3)

        if media_type == 'VIDEO':
            # For videos, wait until the video ends
            return self._wait_for_video_end()
        else:
            # For images, wait for the configured duration
            return self._wait_for_duration(time_to_display)

    def _wait_for_video_end(self) -> bool:
        """
        Wait for the current video to finish playing.

        Returns:
            True if video finished, False if interrupted.
        """
        # First, wait for duration to become available
        duration = None
        for _ in range(30):  # Wait up to 15 seconds
            if not self.running:
                return False
            if sufu.should_reload_scenes():
                return False

            duration = self.mpv.get_duration()
            if duration is not None and duration > 0:
                break
            time.sleep(0.5)

        if duration is None:
            logger.warning("Could not get video duration, using 30s fallback")
            duration = 30

        logger.info(f"Video duration: {duration:.1f}s")

        # Wait for video to finish
        start_time = time.time()
        while self.running:
            # Check for content updates
            if sufu.should_reload_scenes():
                logger.info("Content update detected during video playback")
                return False

            # Check if video has ended
            if self.mpv.get_eof_reached():
                logger.info("Video playback complete")
                return True

            # Safety timeout (duration + 5 seconds buffer)
            elapsed = time.time() - start_time
            if elapsed > duration + 5:
                logger.warning("Video playback timeout, moving to next scene")
                return True

            time.sleep(0.1)

        return False

    def _wait_for_duration(self, duration_seconds: int) -> bool:
        """
        Wait for the specified duration.

        Args:
            duration_seconds: How long to display the image

        Returns:
            True if duration completed, False if interrupted.
        """
        logger.info(f"Displaying image for {duration_seconds}s")

        start_time = time.time()
        while self.running:
            # Check for content updates
            if sufu.should_reload_scenes():
                logger.info("Content update detected during image display")
                return False

            # Check if duration has elapsed
            elapsed = time.time() - start_time
            if elapsed >= duration_seconds:
                return True

            time.sleep(0.1)

        return False

    def run(self):
        """Main player loop."""
        logger.info("=" * 60)
        logger.info("JAM Player 2.0 - Starting")
        logger.info("=" * 60)

        # Start MPV
        if not self.mpv.start(rotation_angle=self.rotation_angle):
            logger.error("Failed to start MPV, exiting")
            return

        logger.info("MPV started successfully")

        try:
            while self.running:
                # Check for content updates
                if sufu.should_reload_scenes():
                    logger.info("Content update detected, reloading scenes")
                    sufu.reset_update_flag_to_zero()
                    self.current_scene_index = 0

                # Load scenes if needed
                if not self.scenes:
                    if not self.load_scenes():
                        logger.info("No scenes available, waiting...")
                        time.sleep(5)
                        continue

                # Get current scene
                if self.current_scene_index >= len(self.scenes):
                    self.current_scene_index = 0

                scene = self.scenes[self.current_scene_index]

                # Display the scene
                if self.display_scene(scene):
                    # Move to next scene
                    self.current_scene_index += 1
                    if self.current_scene_index >= len(self.scenes):
                        self.current_scene_index = 0
                        logger.info("Completed scene cycle, starting over")
                else:
                    # Scene was interrupted (likely content update)
                    # Reset scenes to force reload
                    self.scenes = []

        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
            self.cleanup()

    def cleanup(self):
        """Clean up resources."""
        logger.info("Cleaning up...")
        self.mpv.stop()


def main():
    import argparse

    parser = argparse.ArgumentParser(description='JAM Player 2.0')
    parser.add_argument('media_directory', help='Directory containing media files')
    parser.add_argument('scenes_directory', help='Directory containing scene configuration')

    args = parser.parse_args()

    player = JamPlayer(args.media_directory, args.scenes_directory)
    player.run()


if __name__ == "__main__":
    logger.info("Starting JAM Player Application")
    main()
