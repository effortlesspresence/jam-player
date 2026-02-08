"""
MPV IPC Client for controlling MPV playback via JSON IPC protocol.

This module provides a client to communicate with MPV through a Unix socket,
allowing precise control over media playback for synchronized display across
multiple devices.
"""

import json
import socket
import subprocess
import time
import os
from pathlib import Path
from typing import Optional, Any


class MpvIpcClient:
    """
    Client for controlling MPV via JSON IPC protocol.

    Manages an MPV process with IPC enabled and provides methods to
    control playback, load files, and query state.
    """

    def __init__(self, socket_path: str = "/tmp/mpv-socket", logger=None):
        self.socket_path = socket_path
        self.logger = logger
        self.process: Optional[subprocess.Popen] = None
        self.socket: Optional[socket.socket] = None
        self._request_id = 0

    def _log(self, level: str, msg: str):
        if self.logger:
            getattr(self.logger, level)(msg)
        else:
            print(f"[{level.upper()}] {msg}")

    def start_mpv(self, rotation_angle: int = 0) -> bool:
        """
        Start MPV process with IPC socket enabled.

        Args:
            rotation_angle: Video rotation in degrees (0, 90, 180, 270)

        Returns:
            True if MPV started successfully, False otherwise
        """
        # Clean up any existing socket
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

        # Stop any existing process
        self.stop_mpv()

        args = [
            'mpv',
            '--idle=yes',  # Start in idle mode, waiting for commands
            '--fullscreen',
            '--no-osc',
            '--no-osd-bar',
            '--no-input-default-bindings',
            '--input-conf=/dev/null',
            '--force-window=yes',
            '--no-terminal',
            '--keep-open=yes',  # Keep window open after playback
            '--image-display-duration=inf',  # Don't auto-advance images
            f'--video-rotate={rotation_angle}',
            f'--input-ipc-server={self.socket_path}',
        ]

        try:
            self._log('info', f"Starting MPV with IPC socket at {self.socket_path}")
            self.process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            # Wait for socket to be created
            for _ in range(50):  # Wait up to 5 seconds
                if os.path.exists(self.socket_path):
                    time.sleep(0.1)  # Give MPV a moment to fully initialize
                    return True
                time.sleep(0.1)

            self._log('error', "MPV socket not created within timeout")
            return False

        except Exception as e:
            self._log('error', f"Failed to start MPV: {e}")
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
            self.socket.settimeout(5.0)
            return True
        except Exception as e:
            self._log('error', f"Failed to connect to MPV socket: {e}")
            self.socket = None
            return False

    def _send_command(self, command: list, wait_response: bool = True) -> Optional[Any]:
        """
        Send a command to MPV via IPC.

        Args:
            command: List of command arguments (e.g., ['loadfile', '/path/to/file'])
            wait_response: Whether to wait for and return the response

        Returns:
            Response data if wait_response is True, None otherwise
        """
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

                # Parse the response (may contain multiple JSON objects)
                for line in response_data.decode('utf-8').strip().split('\n'):
                    if line:
                        try:
                            resp = json.loads(line)
                            if resp.get('request_id') == self._request_id:
                                if resp.get('error') == 'success':
                                    return resp.get('data')
                                else:
                                    self._log('error', f"MPV command error: {resp.get('error')}")
                                    return None
                        except json.JSONDecodeError:
                            continue

            return None

        except Exception as e:
            self._log('error', f"Failed to send command to MPV: {e}")
            # Reset connection on error
            try:
                self.socket.close()
            except:
                pass
            self.socket = None
            return None

    def load_file(self, filepath: str, mode: str = "replace") -> bool:
        """
        Load a media file into MPV.

        Args:
            filepath: Path to the media file
            mode: 'replace' to replace current, 'append' to add to playlist

        Returns:
            True if successful
        """
        result = self._send_command(['loadfile', filepath, mode])
        return result is not None or True  # loadfile doesn't return data on success

    def pause(self) -> bool:
        """Pause playback."""
        return self._send_command(['set_property', 'pause', True]) is not None

    def play(self) -> bool:
        """Resume playback."""
        return self._send_command(['set_property', 'pause', False]) is not None

    def seek(self, position_seconds: float, mode: str = "absolute") -> bool:
        """
        Seek to a position.

        Args:
            position_seconds: Position in seconds
            mode: 'absolute', 'relative', or 'absolute-percent'
        """
        return self._send_command(['seek', str(position_seconds), mode]) is not None

    def get_property(self, name: str) -> Optional[Any]:
        """Get an MPV property value."""
        return self._send_command(['get_property', name])

    def set_property(self, name: str, value: Any) -> bool:
        """Set an MPV property value."""
        return self._send_command(['set_property', name, value]) is not None

    def get_playback_time(self) -> Optional[float]:
        """Get current playback position in seconds."""
        return self.get_property('playback-time')

    def get_duration(self) -> Optional[float]:
        """Get duration of current file in seconds."""
        return self.get_property('duration')

    def is_playing(self) -> bool:
        """Check if MPV is currently playing (not paused)."""
        paused = self.get_property('pause')
        return paused is False

    def is_idle(self) -> bool:
        """Check if MPV is in idle state (no file loaded)."""
        idle = self.get_property('idle-active')
        return idle is True

    def quit(self):
        """Send quit command to MPV."""
        self._send_command(['quit'], wait_response=False)
        self.stop_mpv()
