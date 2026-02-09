"""
JAM Player 2.0 - API Client Utilities

Functions for communicating with the JAM 2.0 backend API.
Includes request signing with Ed25519 keys.
"""

import hashlib
import time
import logging
from typing import Optional, Dict, Any
from pathlib import Path

import requests
from nacl.signing import SigningKey
from nacl.encoding import Base64Encoder

from .paths import ENVIRONMENT_FILE
from .credentials import (
    get_device_uuid,
    get_api_signing_private_key,
)

logger = logging.getLogger(__name__)

# Environment-specific API URLs
API_URLS = {
    'prod': 'https://api.justamenu.com',
    'staging': 'https://staging.api.justamenu.com',
    'testing': 'https://testing.api.justamenu.com',
}
DEFAULT_ENVIRONMENT = 'prod'
DEFAULT_REQUEST_TIMEOUT = 10  # seconds


def get_api_base_url() -> str:
    """
    Get the API base URL based on environment.

    Defaults to production. To override, create /etc/jam/config/environment
    with content: 'testing', 'staging', or 'prod'.

    Returns:
        API base URL string.
    """
    env = DEFAULT_ENVIRONMENT

    if ENVIRONMENT_FILE.exists():
        try:
            env = ENVIRONMENT_FILE.read_text().strip().lower()
        except Exception as e:
            logger.warning(f"Error reading environment file: {e}")

    url = API_URLS.get(env, API_URLS[DEFAULT_ENVIRONMENT])

    if env != DEFAULT_ENVIRONMENT:
        logger.info(f"Using {env} environment: {url}")

    return url


def check_api_availability(timeout: int = DEFAULT_REQUEST_TIMEOUT) -> bool:
    """
    Check if the JAM 2.0 API is reachable.

    This is a non-blocking check - we log but don't raise on failure
    because offline playback must work.

    Args:
        timeout: Request timeout in seconds

    Returns:
        True if API health endpoint returns 200.
    """
    base_url = get_api_base_url()
    health_url = f"{base_url}/jam-players/health"

    try:
        logger.debug(f"Checking API availability: {health_url}")
        response = requests.get(health_url, timeout=timeout)

        if response.status_code == 200:
            logger.debug("JAM 2.0 API is available")
            return True
        else:
            # Only log as debug - this is expected when device isn't provisioned yet
            # or during normal connectivity checks
            logger.debug(f"JAM 2.0 API returned status {response.status_code}")
            return False

    except requests.exceptions.Timeout:
        # Don't log - this is expected during connectivity checks when offline
        return False
    except requests.exceptions.ConnectionError:
        # Don't log - this is expected during connectivity checks when offline
        return False
    except Exception as e:
        # Only log unexpected errors
        logger.debug(f"Error checking API availability: {e}")
        return False


def sign_request(method: str, path: str, body: str = "") -> Optional[Dict[str, str]]:
    """
    Generate signature headers for an API request.

    Creates the X-Device-ID, X-Timestamp, X-Body-Hash, and X-Signature headers
    required by the jam-player API authorizer.

    Signature format: "{method}:{path}:{timestamp}:{body_hash}"
    signed with Ed25519 private key.

    Note: X-Body-Hash is sent as a header because API Gateway REQUEST authorizers
    don't have access to the request body. The authorizer uses this header to
    reconstruct the signed payload.

    Args:
        method: HTTP method (GET, POST, etc.)
        path: API path (e.g., /jam-player/provision)
        body: Request body as string (empty for GET)

    Returns:
        Dict of headers to add to request, or None on error.
    """
    device_uuid = get_device_uuid()
    private_key_b64 = get_api_signing_private_key()

    if not device_uuid:
        logger.error("Cannot sign request: device UUID not found")
        return None

    if not private_key_b64:
        logger.error("Cannot sign request: API signing private key not found")
        return None

    try:
        # Load the signing key
        signing_key = SigningKey(private_key_b64.encode(), encoder=Base64Encoder)

        # Generate timestamp
        timestamp = str(int(time.time()))

        # Hash the body (SHA256)
        body_hash = hashlib.sha256(body.encode()).hexdigest()

        # Create the message to sign
        message = f"{method.upper()}:{path}:{timestamp}:{body_hash}"

        # Sign the message
        signed = signing_key.sign(message.encode(), encoder=Base64Encoder)
        signature = signed.signature.decode()

        return {
            'X-Device-ID': device_uuid,
            'X-Timestamp': timestamp,
            'X-Body-Hash': body_hash,
            'X-Signature': signature,
        }

    except Exception as e:
        logger.error(f"Error signing request: {e}")
        return None


def api_request(
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    signed: bool = True
) -> Optional[requests.Response]:
    """
    Make a signed request to the JAM 2.0 API.

    Args:
        method: HTTP method (GET, POST, etc.)
        path: API path (e.g., /jam-player/provision)
        body: Request body dict (will be JSON encoded)
        timeout: Request timeout in seconds
        signed: Whether to sign the request (default True)

    Returns:
        Response object, or None on error.
    """
    import json

    base_url = get_api_base_url()
    url = f"{base_url}{path}"

    headers = {'Content-Type': 'application/json'}
    body_str = json.dumps(body) if body else ""

    if signed:
        logger.info(f"Signing request: {method} {path}")
        sign_headers = sign_request(method, path, body_str)
        if not sign_headers:
            logger.error("Failed to sign request - check device UUID and API signing private key")
            return None
        headers.update(sign_headers)
        logger.info(f"Request signed with device ID: {sign_headers.get('X-Device-ID', 'unknown')}")

    try:
        logger.info(f"Making API request: {method} {url}")

        if method.upper() == 'GET':
            response = requests.get(url, headers=headers, timeout=timeout)
        elif method.upper() == 'POST':
            response = requests.post(url, headers=headers, data=body_str, timeout=timeout)
        elif method.upper() == 'PUT':
            response = requests.put(url, headers=headers, data=body_str, timeout=timeout)
        elif method.upper() == 'DELETE':
            response = requests.delete(url, headers=headers, timeout=timeout)
        else:
            logger.error(f"Unsupported HTTP method: {method}")
            return None

        logger.info(f"API response: {response.status_code}")
        return response

    except requests.exceptions.Timeout:
        logger.error(f"API request timed out after {timeout}s: {method} {url}")
        return None
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Could not connect to API: {method} {url} - {e}")
        return None
    except Exception as e:
        logger.error(f"API request error: {method} {url} - {type(e).__name__}: {e}")
        return None
