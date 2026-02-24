#!/usr/bin/env python3
"""
JAM First Boot Service

This service runs once on the very first boot of a JAM Player device.
It generates and stores:
1. Device UUID v7 (timestamp-sortable for efficient DB queries)
2. Ed25519 SSH key pair (for remote support access)
3. Ed25519 API signing key pair (for authenticating API requests)

Once all tasks complete successfully, a flag file is created to prevent
the service from running again on subsequent boots.

Security: All credentials are stored in /etc/jam/ with root-only permissions.
The auto-logged-in comitup user cannot read private keys even with a keyboard.

This is a critical service - if it fails, the device cannot be provisioned.
"""

import os
import sys
import subprocess
from pathlib import Path

# Add services directory to path for common module imports
sys.path.insert(0, str(Path(__file__).parent))

import uuid6  # For UUID v7 support

from common.logging_config import setup_service_logging, log_service_start
from common.paths import (
    JAM_ETC_DIR,
    DEVICE_DATA_DIR,
    CREDENTIALS_DIR,
    CONFIG_DIR,
    DEVICE_UUID_FILE,
    FIRST_BOOT_COMPLETE_FLAG,
    API_SIGNING_PRIVATE_KEY_FILE,
    API_SIGNING_PUBLIC_KEY_FILE,
    SSH_PRIVATE_KEY_FILE,
    SSH_PUBLIC_KEY_FILE,
)

logger = setup_service_logging('jam-first-boot')

# Path to jam user's SSH directory
JAM_USER_SSH_DIR = Path('/home/jam/.ssh')
JAM_USER_AUTHORIZED_KEYS = JAM_USER_SSH_DIR / 'authorized_keys'


def ensure_jam_user_exists() -> bool:
    """
    Ensure the 'jam' user exists for SSH key-based authentication.

    The jam user:
    - Is used for remote support SSH access
    - Can ONLY authenticate via SSH keys (no password)
    - Has its authorized_keys populated with the device's public key

    The comitup user remains as a backup with password auth.

    Returns True if user exists or was created successfully.
    """
    import pwd

    try:
        pwd.getpwnam('jam')
        logger.info("jam user already exists")
        return True
    except KeyError:
        pass  # User doesn't exist, create it

    logger.info("Creating jam user for SSH key authentication...")

    try:
        # Create user with home directory, no password (SSH key only)
        result = subprocess.run(
            [
                'useradd',
                '-m',              # Create home directory
                '-s', '/bin/bash', # Shell
                '-c', 'JAM Player SSH Access',  # Comment
                'jam'
            ],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            logger.error(f"Failed to create jam user: {result.stderr}")
            return False

        # Lock the password (prevents password auth, SSH keys only)
        result = subprocess.run(
            ['passwd', '-l', 'jam'],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            logger.warning(f"Failed to lock jam password: {result.stderr}")
            # Not fatal - user still created

        logger.info("Created jam user successfully (SSH key auth only)")
        return True

    except Exception as e:
        logger.error(f"Failed to create jam user: {e}")
        return False


def generate_device_uuid() -> str:
    """
    Generate a UUID v7 for this device installation.

    UUID v7 is timestamp-sortable, making database queries by device_uuid
    more efficient. The UUID is tied to this specific installation/SD card,
    not to the hardware. Generated once and never changes.
    """
    return str(uuid6.uuid7())


def generate_api_signing_keys() -> bool:
    """
    Generate Ed25519 key pair for API request signing.

    Uses PyNaCl (libsodium) for Ed25519 key generation.
    The private key is used to sign API requests.
    The public key is sent to the backend during provisioning.
    """
    try:
        from nacl.signing import SigningKey
        from nacl.encoding import Base64Encoder

        # Generate new signing key pair
        signing_key = SigningKey.generate()
        verify_key = signing_key.verify_key

        # Save private key (raw bytes, base64 encoded)
        private_key_b64 = signing_key.encode(encoder=Base64Encoder).decode('utf-8')
        API_SIGNING_PRIVATE_KEY_FILE.write_text(private_key_b64)
        os.chmod(API_SIGNING_PRIVATE_KEY_FILE, 0o600)  # Root read/write only

        # Save public key (raw bytes, base64 encoded)
        public_key_b64 = verify_key.encode(encoder=Base64Encoder).decode('utf-8')
        API_SIGNING_PUBLIC_KEY_FILE.write_text(public_key_b64)
        os.chmod(API_SIGNING_PUBLIC_KEY_FILE, 0o644)  # World readable (it's public)

        logger.info("Generated API signing key pair successfully")
        return True

    except ImportError:
        logger.error("PyNaCl not installed. Install with: pip install pynacl")
        return False
    except Exception as e:
        logger.error(f"Failed to generate API signing keys: {e}")
        return False


def setup_ssh_authorized_keys() -> bool:
    """
    Set up SSH authorized_keys for the jam user using the device's own public key.

    This enables passwordless SSH access: the device's private key (stored in
    /etc/jam/credentials/) is uploaded to the backend during provisioning, and
    the JAM CLI downloads it to authenticate when connecting.

    Creates /home/jam/.ssh/authorized_keys with the device's own public key.
    """
    try:
        # Read the device's SSH public key
        if not SSH_PUBLIC_KEY_FILE.exists():
            logger.error("SSH public key not found - generate SSH keys first")
            return False

        device_public_key = SSH_PUBLIC_KEY_FILE.read_text().strip()

        # Get jam user's UID and GID
        import pwd
        try:
            jam_user = pwd.getpwnam('jam')
            jam_uid = jam_user.pw_uid
            jam_gid = jam_user.pw_gid
        except KeyError:
            logger.error("jam user does not exist")
            return False

        # Create .ssh directory if it doesn't exist
        JAM_USER_SSH_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(JAM_USER_SSH_DIR, 0o700)
        os.chown(JAM_USER_SSH_DIR, jam_uid, jam_gid)

        # Write authorized_keys file with the device's own public key
        JAM_USER_AUTHORIZED_KEYS.write_text(device_public_key + '\n')
        os.chmod(JAM_USER_AUTHORIZED_KEYS, 0o600)
        os.chown(JAM_USER_AUTHORIZED_KEYS, jam_uid, jam_gid)

        logger.info("Set up SSH authorized_keys with device's public key")
        return True

    except Exception as e:
        logger.error(f"Failed to set up SSH authorized_keys: {e}")
        return False


def generate_ssh_keys() -> bool:
    """
    Generate Ed25519 SSH key pair for sshd host key verification.

    Uses ssh-keygen for compatibility with standard SSH tools.
    The public key is sent to the backend during provisioning so
    the JAM CLI can verify the host identity.
    """
    try:
        # Remove existing keys if present (shouldn't happen, but be safe)
        if SSH_PRIVATE_KEY_FILE.exists():
            SSH_PRIVATE_KEY_FILE.unlink()
        if SSH_PUBLIC_KEY_FILE.exists():
            SSH_PUBLIC_KEY_FILE.unlink()

        # Generate new SSH key pair
        result = subprocess.run(
            [
                'ssh-keygen',
                '-t', 'ed25519',
                '-f', str(SSH_PRIVATE_KEY_FILE),
                '-N', '',  # No passphrase
                '-C', 'jam-player-support'
            ],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            logger.error(f"ssh-keygen failed: {result.stderr}")
            return False

        # ssh-keygen creates .pub file, rename it to our expected name
        generated_pub = Path(str(SSH_PRIVATE_KEY_FILE) + '.pub')
        if generated_pub.exists():
            generated_pub.rename(SSH_PUBLIC_KEY_FILE)

        # Set correct permissions
        os.chmod(SSH_PRIVATE_KEY_FILE, 0o600)  # Private key: root only
        os.chmod(SSH_PUBLIC_KEY_FILE, 0o644)   # Public key: world readable

        logger.info("Generated SSH key pair successfully")
        return True

    except Exception as e:
        logger.error(f"Failed to generate SSH keys: {e}")
        return False


def ensure_directories_exist():
    """Create required directories with secure permissions."""
    # Create /etc/jam with standard permissions
    JAM_ETC_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(JAM_ETC_DIR, 0o755)

    # Create device_data dir (contains non-secret data)
    DEVICE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DEVICE_DATA_DIR, 0o755)

    # Create credentials dir with restricted access (root only)
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CREDENTIALS_DIR, 0o700)

    # Create config dir
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o755)

    logger.info("Ensured directories exist with correct permissions")


def already_completed() -> bool:
    """Check if first boot has already completed successfully."""
    return FIRST_BOOT_COMPLETE_FLAG.exists()


def mark_complete():
    """Mark first boot as complete by creating flag file."""
    FIRST_BOOT_COMPLETE_FLAG.touch()
    logger.info(f"Created completion flag: {FIRST_BOOT_COMPLETE_FLAG}")


def run_first_boot() -> bool:
    """
    Execute all first boot tasks.

    Returns True if all tasks completed successfully.
    """
    log_service_start(logger, 'JAM First Boot Service')

    # Check if already completed
    if already_completed():
        logger.info("First boot already completed, skipping")
        return True

    # Ensure directories exist with correct permissions
    ensure_directories_exist()

    # Track success
    all_success = True

    # 1. Generate and save device UUID
    if not DEVICE_UUID_FILE.exists():
        logger.info("Generating device UUID (v7)...")
        device_uuid = generate_device_uuid()
        DEVICE_UUID_FILE.write_text(device_uuid)
        os.chmod(DEVICE_UUID_FILE, 0o644)  # World readable (not a secret)
        logger.info(f"Device UUID: {device_uuid}")
    else:
        device_uuid = DEVICE_UUID_FILE.read_text().strip()
        logger.info(f"Device UUID already exists: {device_uuid}")

    # 2. Generate API signing key pair
    if not API_SIGNING_PRIVATE_KEY_FILE.exists() or not API_SIGNING_PUBLIC_KEY_FILE.exists():
        logger.info("Generating API signing key pair...")
        if not generate_api_signing_keys():
            logger.error("Failed to generate API signing keys")
            all_success = False
    else:
        logger.info("API signing keys already exist")

    # 3. Generate SSH key pair (for host key verification)
    if not SSH_PRIVATE_KEY_FILE.exists() or not SSH_PUBLIC_KEY_FILE.exists():
        logger.info("Generating SSH host key pair...")
        if not generate_ssh_keys():
            logger.error("Failed to generate SSH keys")
            all_success = False
    else:
        logger.info("SSH host keys already exist")

    # 4. Ensure jam user exists (for SSH key auth)
    if not ensure_jam_user_exists():
        logger.error("Failed to ensure jam user exists")
        all_success = False

    # 5. Set up SSH authorized_keys (using device's own public key)
    if not JAM_USER_AUTHORIZED_KEYS.exists():
        logger.info("Setting up SSH authorized_keys...")
        if not setup_ssh_authorized_keys():
            logger.error("Failed to set up SSH authorized_keys")
            all_success = False
    else:
        logger.info("SSH authorized_keys already configured")

    # Mark complete only if all tasks succeeded
    if all_success:
        mark_complete()
        logger.info("=" * 60)
        logger.info("JAM First Boot Service Completed Successfully")
        logger.info("=" * 60)
    else:
        logger.error("=" * 60)
        logger.error("JAM First Boot Service FAILED - will retry on next boot")
        logger.error("=" * 60)

    return all_success


def main():
    """Entry point for the service."""
    try:
        success = run_first_boot()
        sys.exit(0 if success else 1)
    except Exception as e:
        logger.exception(f"Unhandled exception in first boot service: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
