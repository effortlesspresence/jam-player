#!/usr/bin/env python3
"""
JAM Player Tailscale Service

This service ensures Tailscale is configured and connected for remote access.
It runs on boot and handles two scenarios:

1. Device already announced: Can self-service via Ed25519-authenticated API
2. Device not yet announced: Waits for mobile app to provision Tailscale

Security: No credentials are stored in the image. Tailscale provisioning either
requires an authenticated mobile app session (initial setup) or Ed25519 device
authentication (re-auth after announcement).

Flow:
1. Check if Tailscale is already connected → done
2. Check if device is announced:
   - Yes: Call backend API with Ed25519 auth to get Tailscale credentials
   - No: Log and exit (mobile app will provision during setup)
3. Use credentials to authenticate with Tailscale
"""

import sys
import os
import subprocess
import time
import json
from pathlib import Path
from typing import Optional, Tuple

# Add the services directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.logging_config import setup_service_logging, log_service_start
from common.credentials import (
    get_device_uuid,
    is_device_announced,
    set_device_announced,
    get_api_signing_public_key,
    get_ssh_public_key,
    get_jp_image_id,
)
from common.api import api_request, get_api_base_url

logger = setup_service_logging('jam-tailscale')

# Tailscale API
TAILSCALE_API = "https://api.tailscale.com/api/v2"
TAILSCALE_OAUTH_URL = f"{TAILSCALE_API}/oauth/token"
TAILSCALE_TAILNET = "effortlesspresence.com"

# Connection check settings
MAX_CONNECTION_WAIT = 120  # seconds to wait for existing connection


def run_command(cmd: list, timeout: int = 30) -> Tuple[bool, str, str]:
    """Run a shell command and return (success, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", "Command timed out"
    except Exception as e:
        return False, "", str(e)


def is_tailscale_installed() -> bool:
    """Check if Tailscale is installed."""
    success, _, _ = run_command(['which', 'tailscale'])
    return success


def is_tailscale_running() -> bool:
    """Check if tailscaled service is running."""
    success, _, _ = run_command(['systemctl', 'is-active', '--quiet', 'tailscaled'])
    return success


def is_tailscale_connected() -> bool:
    """
    Check if Tailscale is authenticated and has an IP address.

    Returns True if:
    - tailscaled is running
    - Backend state is 'Running'
    - Device has a 100.x.x.x IP
    """
    if not is_tailscale_running():
        return False

    # Check backend state
    success, stdout, _ = run_command(['tailscale', 'status', '--json'])
    if not success:
        return False

    try:
        status = json.loads(stdout)
        backend_state = status.get('BackendState', '')
        if backend_state != 'Running':
            return False
    except (json.JSONDecodeError, KeyError):
        return False

    # Check for Tailscale IP
    success, stdout, _ = run_command(['tailscale', 'ip', '-4'])
    if not success:
        return False

    ip = stdout.strip()
    return ip.startswith('100.')


def get_tailscale_ip() -> Optional[str]:
    """Get the current Tailscale IPv4 address."""
    success, stdout, _ = run_command(['tailscale', 'ip', '-4'])
    if success:
        ip = stdout.strip()
        if ip.startswith('100.'):
            return ip
    return None


def wait_for_existing_connection() -> bool:
    """
    Wait for an existing Tailscale connection to establish.

    On boot, tailscaled may take time to reconnect. Wait up to
    MAX_CONNECTION_WAIT seconds before assuming we need to re-auth.
    """
    logger.info(f"Waiting up to {MAX_CONNECTION_WAIT}s for existing Tailscale connection...")

    for i in range(0, MAX_CONNECTION_WAIT, 5):
        if is_tailscale_connected():
            ip = get_tailscale_ip()
            logger.info(f"Tailscale connected with IP: {ip}")
            return True

        if i % 30 == 0 and i > 0:
            logger.info(f"  Still waiting... ({i}/{MAX_CONNECTION_WAIT}s)")
        time.sleep(5)

    logger.info("Tailscale did not connect within timeout")
    return False


def try_announce() -> bool:
    """
    Attempt to announce the device to the backend.

    This is a fallback in case jam-announce.service hasn't run yet.
    Returns True if successfully announced (or already announced).
    """
    if is_device_announced():
        logger.info("Device already announced")
        return True

    logger.info("Attempting to announce device to backend...")

    # Gather required credentials
    device_uuid = get_device_uuid()
    if not device_uuid:
        logger.warning("No device UUID found - cannot announce")
        return False

    api_signing_public_key = get_api_signing_public_key()
    if not api_signing_public_key:
        logger.warning("No API signing public key found - cannot announce")
        return False

    ssh_public_key = get_ssh_public_key()
    if not ssh_public_key:
        logger.warning("No SSH public key found - cannot announce")
        return False

    jp_image_id = get_jp_image_id()
    if not jp_image_id:
        logger.warning("No JP image ID found - cannot announce")
        return False

    payload = {
        'deviceUuid': device_uuid,
        'apiSigningPublicKey': api_signing_public_key,
        'sshPublicKey': ssh_public_key,
        'jpImageId': jp_image_id,
    }

    logger.info(f"Announcing to {get_api_base_url()}/jam-players/announce")

    response = api_request(
        method='POST',
        path='/jam-players/announce',
        body=payload,
        signed=False  # announce endpoint has no authorizer
    )

    if response is None:
        logger.warning("No response from API - announce failed")
        return False

    if response.status_code == 200:
        logger.info("Announcement successful!")
        set_device_announced()
        return True
    elif response.status_code == 409:
        # Already exists - treat as success
        logger.info("Device already announced/registered")
        set_device_announced()
        return True
    else:
        logger.warning(f"Announce API returned {response.status_code}: {response.text}")
        return False


def fetch_tailscale_credentials() -> Optional[Tuple[str, str]]:
    """
    Fetch Tailscale OAuth credentials from the JAM backend.

    Requires device to be announced (Ed25519 public key stored in backend).

    Returns:
        Tuple of (client_id, client_secret) or None on failure.
    """
    from common.api import get_api_base_url, sign_request
    from common.credentials import get_api_signing_private_key
    import requests

    # Debug: Check credentials before making request
    device_uuid = get_device_uuid()
    private_key = get_api_signing_private_key()
    logger.info(f"Device UUID: {device_uuid}")
    logger.info(f"Private key exists: {private_key is not None}")
    if private_key:
        logger.info(f"Private key length: {len(private_key)}")

    base_url = get_api_base_url()
    endpoint = '/jam-players/tailscale-conn-info'
    url = f"{base_url}{endpoint}"
    logger.info(f"Fetching Tailscale credentials from {url}")

    # Debug: Try signing manually to see what happens
    sign_headers = sign_request('GET', endpoint, '')
    if not sign_headers:
        logger.error("Failed to sign request - sign_request returned None")
        return None
    logger.info(f"Signed request with headers: X-Device-ID={sign_headers.get('X-Device-ID')}, X-Timestamp={sign_headers.get('X-Timestamp')}")

    # Make request manually with full error details
    headers = {'Content-Type': 'application/json'}
    headers.update(sign_headers)

    try:
        logger.info(f"Making GET request to {url}")
        response = requests.get(url, headers=headers, timeout=30)
        logger.info(f"Response status: {response.status_code}")
    except requests.exceptions.Timeout:
        logger.error(f"Request timed out after 30s: {url}")
        return None
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection error: {url} - {e}")
        return None
    except Exception as e:
        logger.error(f"Request failed: {url} - {type(e).__name__}: {e}")
        return None

    if not response:
        logger.error(f"No response from backend - request to {url} failed")
        return None

    if response.status_code == 401:
        logger.error(f"Authentication failed (401) - device may not be announced or signature invalid. Response: {response.text}")
        return None

    if response.status_code != 200:
        logger.error(f"Backend returned status {response.status_code}: {response.text}")
        return None

    try:
        data = response.json()
        client_id = data.get('clientId')
        client_secret = data.get('clientSecret')

        if client_id and client_secret:
            logger.info("Received Tailscale credentials successfully")
            return client_id, client_secret
        else:
            logger.error(f"Invalid credentials response - missing clientId or clientSecret. Response: {data}")
            return None
    except Exception as e:
        logger.error(f"Error parsing credentials response: {e}. Raw response: {response.text}")
        return None


def get_oauth_access_token(client_id: str, client_secret: str) -> Optional[str]:
    """
    Get an access token from Tailscale OAuth API.

    Args:
        client_id: Tailscale OAuth client ID
        client_secret: Tailscale OAuth client secret

    Returns:
        Access token string or None on failure.
    """
    import requests

    logger.info("Getting Tailscale OAuth access token...")

    try:
        response = requests.post(
            TAILSCALE_OAUTH_URL,
            data={
                'client_id': client_id,
                'client_secret': client_secret,
                'grant_type': 'client_credentials'
            },
            timeout=30
        )

        if response.status_code == 200:
            data = response.json()
            return data.get('access_token')
        else:
            logger.error(f"Tailscale OAuth failed: {response.status_code} {response.text}")
            return None
    except Exception as e:
        logger.error(f"Error getting OAuth token: {e}")
        return None


def generate_auth_key(access_token: str) -> Optional[str]:
    """
    Generate a Tailscale auth key for this device.

    Args:
        access_token: Tailscale OAuth access token

    Returns:
        Auth key string or None on failure.
    """
    import requests

    logger.info("Generating Tailscale auth key...")

    try:
        response = requests.post(
            f"{TAILSCALE_API}/tailnet/{TAILSCALE_TAILNET}/keys",
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            },
            json={
                'capabilities': {
                    'devices': {
                        'create': {
                            'reusable': False,
                            'ephemeral': False,
                            'preauthorized': True,
                            'tags': ['tag:jam-player']
                        }
                    }
                }
            },
            timeout=30
        )

        if response.status_code == 200:
            data = response.json()
            return data.get('key')
        else:
            logger.error(f"Failed to generate auth key: {response.status_code} {response.text}")
            return None
    except Exception as e:
        logger.error(f"Error generating auth key: {e}")
        return None


def setup_tailscale(auth_key: str) -> bool:
    """
    Configure and start Tailscale with the given auth key.

    Args:
        auth_key: Tailscale auth key

    Returns:
        True if successful, False otherwise.
    """
    device_uuid = get_device_uuid()
    hostname = f"jp-{device_uuid}" if device_uuid else "jp-unknown"

    logger.info(f"Setting up Tailscale with hostname: {hostname}")

    # Stop tailscaled and clear state for fresh start
    run_command(['systemctl', 'stop', 'tailscaled'])

    # Remove old state to get a fresh IP
    state_file = Path('/var/lib/tailscale/tailscaled.state')
    if state_file.exists():
        try:
            state_file.unlink()
            logger.info("Cleared old Tailscale state")
        except Exception as e:
            logger.warning(f"Could not remove state file: {e}")

    # Start tailscaled
    run_command(['systemctl', 'start', 'tailscaled'])
    time.sleep(2)

    # Authenticate with Tailscale
    success, stdout, stderr = run_command(
        ['tailscale', 'up', '--authkey', auth_key, '--hostname', hostname],
        timeout=60
    )

    if not success:
        logger.error(f"tailscale up failed: {stderr}")
        return False

    # Ensure tailscaled starts on boot
    run_command(['systemctl', 'enable', 'tailscaled'])

    # Wait for connection
    time.sleep(5)
    if is_tailscale_connected():
        ip = get_tailscale_ip()
        logger.info(f"Tailscale connected successfully with IP: {ip}")
        return True
    else:
        logger.error("Tailscale did not connect after setup")
        return False


def report_tailscale_ip_to_backend(ip: str) -> bool:
    """
    Report the Tailscale IP address to the JAM backend.

    This is called every time jam-tailscale.service runs and has a Tailscale IP,
    even if the IP hasn't changed. This ensures the backend always has the
    current IP for remote support access.

    Args:
        ip: The Tailscale IP address (e.g., "100.64.1.123")

    Returns:
        True if successfully reported, False otherwise.
    """
    device_uuid = get_device_uuid()
    if not device_uuid:
        logger.warning("No device UUID - cannot report Tailscale IP")
        return False

    logger.info(f"Reporting Tailscale IP {ip} to backend...")

    response = api_request(
        method='PUT',
        path=f'/jam-players/{device_uuid}/set-ip',
        body={'tailscaleIpAddress': ip},
        signed=True,
        timeout=30
    )

    if not response:
        logger.warning("No response from backend when reporting Tailscale IP")
        return False

    if response.status_code == 200:
        logger.info("Tailscale IP reported to backend successfully")
        return True
    else:
        logger.warning(f"Failed to report Tailscale IP: {response.status_code} {response.text}")
        return False


def main():
    log_service_start(logger, 'JAM Tailscale Service')

    # Check if Tailscale is installed
    if not is_tailscale_installed():
        logger.error("Tailscale is not installed - cannot configure remote access")
        sys.exit(1)

    # Wait for existing connection (may reconnect after boot)
    if wait_for_existing_connection():
        logger.info("Tailscale already connected")
        # Report IP to backend (even if already connected - ensures backend has current IP)
        ip = get_tailscale_ip()
        if ip:
            report_tailscale_ip_to_backend(ip)
        sys.exit(0)

    # Tailscale not connected - need to set it up
    logger.info("Tailscale not connected - checking if we can self-provision...")

    # Check if device is announced (required for Ed25519 API auth)
    if not is_device_announced():
        logger.info("Device not yet announced - attempting to announce now...")
        if not try_announce():
            logger.info("Could not announce device - Tailscale will be provisioned via mobile app setup")
            logger.info("Exiting - jam-tailscale will retry on next boot or after app provisioning")
            sys.exit(0)
        logger.info("Device announced successfully!")

    # Device is announced - we can use the authenticated API
    logger.info("Device is announced - fetching Tailscale credentials via API...")

    # Fetch credentials from backend
    credentials = fetch_tailscale_credentials()
    if not credentials:
        logger.error("Failed to fetch Tailscale credentials from backend")
        sys.exit(1)

    client_id, client_secret = credentials

    # Get OAuth access token
    access_token = get_oauth_access_token(client_id, client_secret)
    if not access_token:
        logger.error("Failed to get Tailscale access token")
        sys.exit(1)

    # Generate auth key
    auth_key = generate_auth_key(access_token)
    if not auth_key:
        logger.error("Failed to generate Tailscale auth key")
        sys.exit(1)

    # Set up Tailscale
    if setup_tailscale(auth_key):
        logger.info("Tailscale setup completed successfully")
        # Report IP to backend
        ip = get_tailscale_ip()
        if ip:
            report_tailscale_ip_to_backend(ip)
        sys.exit(0)
    else:
        logger.error("Tailscale setup failed")
        sys.exit(1)


if __name__ == '__main__':
    main()
