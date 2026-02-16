#!/usr/bin/env python3
"""
JAM Player WebSocket Commands Service

This service connects to the JAM backend WebSocket API to receive real-time
commands like WiFi reconnect requests.

Purpose:
1. Establish and maintain a WebSocket connection to the backend
2. Subscribe to DEVICE_COMMANDS for this device's UUID
3. Handle incoming commands (e.g., WIFI_RECONNECT)
4. Execute commands with proper fallback behavior

Key behaviors:
- Reconnects automatically if the WebSocket connection drops
- Handles WIFI_RECONNECT commands with fallback to previous network on failure
- Uses exponential backoff for reconnection attempts
- Notifies systemd watchdog to prove liveness
- Designed to be stable - recovers from all errors gracefully

This service runs when the device is registered (enforced by systemd).
"""

import sys
import os
import json
import time
import signal
import threading

# Add the services directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sdnotify

try:
    import websocket
except ImportError:
    print("websocket-client not installed. Run: pip install websocket-client")
    sys.exit(1)

from common.logging_config import setup_service_logging, log_service_start
from common.credentials import get_device_uuid, is_device_announced, update_screen_id_if_changed
from common.network import (
    connect_to_wifi,
    connect_to_saved_wifi,
    check_internet_connectivity,
)
from common.paths import DISPLAY_ORIENTATION_FILE

logger = setup_service_logging('jam-ws-commands')

# WebSocket URL template - will be configured from environment or hardcoded
# Format: wss://{stage}.sockets.justamenu.com
WEBSOCKET_URL_TEMPLATE = "wss://{stage}.sockets.justamenu.com"

# Environment for determining the WebSocket URL
ENVIRONMENT = os.environ.get('JAM_ENVIRONMENT', 'production')

# Reconnection settings
INITIAL_RECONNECT_DELAY = 5  # seconds
MAX_RECONNECT_DELAY = 300  # 5 minutes max
RECONNECT_BACKOFF_MULTIPLIER = 2

# Watchdog interval (ping systemd every 30 seconds)
WATCHDOG_INTERVAL = 30

# Systemd notifier
notifier = sdnotify.SystemdNotifier()

# Track if we should keep running
running = True

# Current reconnect delay (exponential backoff)
reconnect_delay = INITIAL_RECONNECT_DELAY


def get_websocket_url() -> str:
    """Get the WebSocket URL based on environment."""
    if ENVIRONMENT == 'production':
        stage = 'production'
    elif ENVIRONMENT == 'staging':
        stage = 'staging'
    else:
        stage = 'testing'

    return WEBSOCKET_URL_TEMPLATE.format(stage=stage)


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    global running
    logger.info(f"Received signal {signum}, shutting down...")
    running = False


def handle_wifi_reconnect(payload: dict, command_id: str) -> bool:
    """
    Handle a WiFi reconnect command.

    Args:
        payload: The command payload containing ssid, password, etc.
        command_id: The unique command ID for logging

    Returns:
        True if the reconnect succeeded, False otherwise
    """
    ssid = payload.get('ssid')
    password = payload.get('password')
    connection_name = payload.get('connectionName')
    use_saved_credentials = payload.get('useSavedCredentials', False)

    if not ssid:
        logger.error(f"[{command_id}] WiFi reconnect command missing ssid")
        return False

    logger.info(f"[{command_id}] Handling WiFi reconnect to: {ssid} (useSaved={use_saved_credentials})")

    try:
        if use_saved_credentials and connection_name:
            # Use saved credentials
            logger.info(f"[{command_id}] Connecting with saved credentials: {connection_name}")
            success, error = connect_to_saved_wifi(connection_name)
        elif password:
            # Connect with provided password
            logger.info(f"[{command_id}] Connecting with provided password")
            success, error = connect_to_wifi(ssid, password)
        else:
            logger.error(f"[{command_id}] No password and not using saved credentials")
            return False

        if success:
            logger.info(f"[{command_id}] WiFi reconnect successful to: {ssid}")
            # Verify we actually have internet
            has_internet, method = check_internet_connectivity(timeout=10)
            if has_internet:
                logger.info(f"[{command_id}] Internet connectivity verified via {method}")
                return True
            else:
                logger.warning(f"[{command_id}] Connected to WiFi but no internet")
                # The network.py functions already handle fallback, so just report failure
                return False
        else:
            logger.error(f"[{command_id}] WiFi reconnect failed: {error}")
            # Fallback is already handled by connect_to_wifi / connect_to_saved_wifi
            return False

    except Exception as e:
        logger.error(f"[{command_id}] Exception during WiFi reconnect: {e}")
        return False


def handle_set_orientation(payload: dict, command_id: str) -> bool:
    """
    Handle a set orientation command.

    Args:
        payload: The command payload containing orientation
        command_id: The unique command ID for logging

    Returns:
        True if the orientation was set successfully, False otherwise
    """
    orientation = payload.get('orientation')

    if not orientation:
        logger.error(f"[{command_id}] SET_ORIENTATION command missing orientation")
        return False

    # Validate orientation value
    valid_orientations = ['LANDSCAPE', 'PORTRAIT_BOTTOM_ON_LEFT', 'PORTRAIT_BOTTOM_ON_RIGHT']
    if orientation not in valid_orientations:
        logger.error(f"[{command_id}] Invalid orientation: {orientation}")
        return False

    logger.info(f"[{command_id}] Setting display orientation to: {orientation}")

    try:
        # Read current orientation to check if it changed
        current_orientation = None
        if DISPLAY_ORIENTATION_FILE.exists():
            current_orientation = DISPLAY_ORIENTATION_FILE.read_text().strip()

        # Write new orientation
        DISPLAY_ORIENTATION_FILE.write_text(orientation)
        logger.info(f"[{command_id}] Display orientation saved to {DISPLAY_ORIENTATION_FILE}")

        # If orientation changed, restart jam-player-display.service to apply it
        if current_orientation != orientation:
            logger.info(f"[{command_id}] Orientation changed from {current_orientation} to {orientation}, restarting display service")
            import subprocess
            try:
                subprocess.run(
                    ['systemctl', 'restart', 'jam-player-display.service'],
                    timeout=10,
                    capture_output=True
                )
                logger.info(f"[{command_id}] Display service restart triggered")
            except Exception as e:
                logger.warning(f"[{command_id}] Failed to restart display service: {e}")
                # Not a failure - the service will pick up the new orientation on next restart

        return True

    except Exception as e:
        logger.error(f"[{command_id}] Failed to set orientation: {e}")
        return False


def handle_set_screen_id(payload: dict, command_id: str) -> bool:
    """
    Handle a set screen ID command.

    Updates the local screen_id.txt and triggers content manager to pull new content.

    Args:
        payload: The command payload containing screenId
        command_id: The unique command ID for logging

    Returns:
        True if the screen ID was set successfully, False otherwise
    """
    screen_id = payload.get('screenId')  # Can be None to unlink

    logger.info(f"[{command_id}] Setting screen ID to: {screen_id}")

    try:
        # Update screen_id.txt if it changed
        changed = update_screen_id_if_changed(screen_id)

        if changed:
            logger.info(f"[{command_id}] Screen ID updated, restarting content manager to pull new content")
            import subprocess
            try:
                subprocess.run(
                    ['systemctl', 'restart', 'jam-content-manager.service'],
                    timeout=10,
                    capture_output=True
                )
                logger.info(f"[{command_id}] Content manager restart triggered")
            except Exception as e:
                logger.warning(f"[{command_id}] Failed to restart content manager: {e}")
                # Not a critical failure - content manager will pick up changes on next cycle
        else:
            logger.info(f"[{command_id}] Screen ID unchanged, no action needed")

        return True

    except Exception as e:
        logger.error(f"[{command_id}] Failed to set screen ID: {e}")
        return False


def handle_device_command(message: dict):
    """
    Handle an incoming device command message.

    Args:
        message: The parsed WebSocket message
    """
    command_type = message.get('commandType')
    command_id = message.get('commandId', 'unknown')
    payload = message.get('payload', {})

    logger.info(f"Received command: {command_type} (id: {command_id})")

    if command_type == 'WIFI_RECONNECT':
        success = handle_wifi_reconnect(payload, command_id)
        if success:
            logger.info(f"[{command_id}] Command completed successfully")
        else:
            logger.warning(f"[{command_id}] Command failed (fallback applied if needed)")
    elif command_type == 'SET_ORIENTATION':
        success = handle_set_orientation(payload, command_id)
        if success:
            logger.info(f"[{command_id}] Orientation set successfully")
        else:
            logger.warning(f"[{command_id}] Failed to set orientation")
    elif command_type == 'SET_SCREEN_ID':
        success = handle_set_screen_id(payload, command_id)
        if success:
            logger.info(f"[{command_id}] Screen ID set successfully")
        else:
            logger.warning(f"[{command_id}] Failed to set screen ID")
    else:
        logger.warning(f"Unknown command type: {command_type}")


def on_message(ws, message):
    """Handle incoming WebSocket messages."""
    global reconnect_delay

    try:
        data = json.loads(message)
        msg_type = data.get('type')

        logger.debug(f"Received message type: {msg_type}")

        if msg_type == 'CONNECTED':
            logger.info("WebSocket connection confirmed by server")
            # Reset reconnect delay on successful connection
            reconnect_delay = INITIAL_RECONNECT_DELAY

        elif msg_type == 'DEVICE_COMMAND':
            handle_device_command(data)

        elif msg_type == 'ERROR':
            logger.error(f"Server error: {data.get('message', 'Unknown error')}")

        else:
            logger.debug(f"Unhandled message type: {msg_type}")

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse WebSocket message: {e}")
    except Exception as e:
        logger.error(f"Error handling WebSocket message: {e}")


def on_error(ws, error):
    """Handle WebSocket errors."""
    logger.error(f"WebSocket error: {error}")
    # TODO: call report-jp-error with JAM_WEBSOCKET_COMMANDS service arg


def on_close(ws, close_status_code, close_msg):
    """Handle WebSocket close."""
    logger.info(f"WebSocket closed: {close_status_code} - {close_msg}")


def on_open(ws):
    """Handle WebSocket connection open."""
    logger.info("WebSocket connection established")


def run_websocket(device_uuid: str):
    """
    Run the WebSocket connection with reconnection logic.

    Args:
        device_uuid: The device UUID for subscribing to commands
    """
    global running, reconnect_delay

    base_url = get_websocket_url()
    # Build connection URL with subscription parameters
    ws_url = (
        f"{base_url}?"
        f"resourceType=JAM_PLAYER&"
        f"resourceId={device_uuid}&"
        f"subscriptionType=DEVICE_COMMANDS"
    )

    logger.info(f"Connecting to WebSocket: {base_url} (device: {device_uuid[:8]}...)")

    while running:
        try:
            # Create WebSocket connection
            ws = websocket.WebSocketApp(
                ws_url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )

            # Run WebSocket in a thread so we can handle signals
            ws_thread = threading.Thread(
                target=lambda: ws.run_forever(ping_interval=60, ping_timeout=30)
            )
            ws_thread.daemon = True
            ws_thread.start()

            # Wait for thread to finish or signal
            while running and ws_thread.is_alive():
                notifier.notify("WATCHDOG=1")
                ws_thread.join(timeout=WATCHDOG_INTERVAL)

            # If we're still running, connection dropped - reconnect
            if running:
                logger.info(f"WebSocket disconnected, reconnecting in {reconnect_delay}s...")
                time.sleep(reconnect_delay)

                # Exponential backoff
                reconnect_delay = min(
                    reconnect_delay * RECONNECT_BACKOFF_MULTIPLIER,
                    MAX_RECONNECT_DELAY
                )

        except Exception as e:
            logger.error(f"WebSocket exception: {e}")
            if running:
                logger.info(f"Reconnecting in {reconnect_delay}s...")
                time.sleep(reconnect_delay)
                reconnect_delay = min(
                    reconnect_delay * RECONNECT_BACKOFF_MULTIPLIER,
                    MAX_RECONNECT_DELAY
                )


def main():
    """Main entry point for the WebSocket commands service."""
    global running

    log_service_start(logger, "JAM WebSocket Commands Service")

    # Check if device is announced
    if not is_device_announced():
        logger.error("Device is not announced. WebSocket commands require announcement.")
        logger.error("This service should only run after announcement is complete.")
        sys.exit(1)

    # Get device UUID
    device_uuid = get_device_uuid()
    if not device_uuid:
        logger.error("Cannot read device UUID")
        sys.exit(1)

    logger.info(f"Device UUID: {device_uuid[:8]}...")

    # Set up signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Notify systemd we're ready
    notifier.notify("READY=1")
    logger.info("Service ready, connecting to WebSocket...")

    # Run the WebSocket connection
    run_websocket(device_uuid)

    logger.info("WebSocket commands service shutting down")
    notifier.notify("STOPPING=1")


if __name__ == '__main__':
    main()
