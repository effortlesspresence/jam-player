#!/usr/bin/env python3
"""
JAM Player Update Service

This service runs once on boot to check for and install updates.
It compares the currently installed commit hash against the latest
commit in the jam repo, and if they differ, installs the update.

Flow:
1. Check current version (git commit hash in /etc/jam/version.txt)
2. Fetch latest version from remote git repo
3. If versions differ:
   a. Pull latest code
   b. Install all services to /opt/jam/services/
   c. Install jam_player package to /opt/jam/venv/
   d. Update version file
   e. Clean up legacy cruft
   f. On failure: report error to backend
4. Exit (runs once per boot)

Security: This service runs as root to install system-wide updates.
"""

import sys
import os
import subprocess
import shutil
from pathlib import Path
from typing import Optional, List

# Add the services directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.logging_config import setup_service_logging, log_service_start
from common.credentials import is_device_registered
from common.api import api_request
from common.paths import ENVIRONMENT_FILE

# Try to import PIL for update screen display
try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

logger = setup_service_logging('jam-update')

# =============================================================================
# Path Configuration
# =============================================================================

# Git repo location (new dedicated jam-player repo)
JAM_REPO_DIR = Path('/home/comitup/jam-player')

# Version tracking
VERSION_FILE = Path('/etc/jam/version.txt')

# JAM 2.0 installation paths
OPT_JAM_DIR = Path('/opt/jam')
SERVICES_DEST = OPT_JAM_DIR / 'services'
VENV_DIR = OPT_JAM_DIR / 'venv'

# Source paths in the repo
SERVICES_V2_SRC = JAM_REPO_DIR / 'src' / 'jam_player' / 'services_v2'
JAM_PLAYER_SRC = JAM_REPO_DIR / 'src' / 'jam_player'
SYSTEMD_SRC = JAM_REPO_DIR / 'systemd'
CRON_SRC = JAM_REPO_DIR / 'cron'
LOGROTATE_SRC = JAM_REPO_DIR / 'logrotate_config'

# Legacy paths (for cleanup)
LEGACY_JAM_DIR = Path('/home/comitup/.jam')
LEGACY_APP_VENV = LEGACY_JAM_DIR / 'jam_player_virtual_env'
LEGACY_SCRIPTS_VENV = LEGACY_JAM_DIR / 'scripts' / 'jam_scripts_venv'
LEGACY_JAM_REPO = Path('/home/comitup/jam')  # Old combined repo

# Git settings
GIT_REMOTE = 'origin'
GIT_BRANCH_DEFAULT = 'main'
GIT_TIMEOUT = 300
GIT_REPO_URL = 'https://github.com/effortlesspresence/jam-player.git'


def get_git_branch() -> str:
    """
    Get the git branch to use for updates.

    If /etc/jam/config/environment exists and contains a value other than
    "false" or "prod", use that value as the branch name. Otherwise use 'main'.

    The environment file is set during device provisioning or migration from 1.0.
    """
    if ENVIRONMENT_FILE.exists():
        try:
            content = ENVIRONMENT_FILE.read_text().strip()
            if content and content.lower() not in ('false', 'prod'):
                return content
        except Exception:
            pass
    return GIT_BRANCH_DEFAULT


# =============================================================================
# Helper Functions
# =============================================================================

def run_command(cmd: list, cwd: Path = None, timeout: int = 120) -> tuple[bool, str, str]:
    """Run a shell command and return (success, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", "Command timed out"
    except Exception as e:
        return False, "", str(e)


def configure_git_safe_directory():
    """
    Configure git to trust the JAM repo directory.

    Since jam-update.service runs as root but the repo is owned by comitup,
    git's "dubious ownership" check would block operations. This adds the
    directory to git's safe.directory list.
    """
    run_command([
        'git', 'config', '--global', '--add', 'safe.directory', str(JAM_REPO_DIR)
    ])


def clone_repo(branch: str) -> bool:
    """
    Clone the jam-player repo if it doesn't exist.

    This handles the migration from the old combined 'jam' repo to the
    new dedicated 'jam-player' repo.
    """
    logger.info(f"Cloning jam-player repo to {JAM_REPO_DIR}...")

    success, _, stderr = run_command(
        ['git', 'clone', '--branch', branch, '--single-branch', GIT_REPO_URL, str(JAM_REPO_DIR)],
        timeout=GIT_TIMEOUT
    )

    if not success:
        logger.error(f"Failed to clone repo: {stderr}")
        return False

    # Set ownership to comitup user (UID 1000)
    run_command(['chown', '-R', 'comitup:comitup', str(JAM_REPO_DIR)])

    logger.info("Repo cloned successfully")
    return True


def get_current_version() -> Optional[str]:
    """
    Get the currently installed version (git commit hash).

    Returns None if version.txt doesn't exist or is empty, which indicates
    the device hasn't been migrated to JAM 2.0 yet and needs a full install.

    Note: We intentionally don't fall back to git rev-parse HEAD because
    that would give us a commit hash even when /opt/jam/services/ hasn't
    been populated yet.
    """
    try:
        if VERSION_FILE.exists():
            version = VERSION_FILE.read_text().strip()
            if version:
                return version
        return None
    except Exception as e:
        logger.error(f"Error getting current version: {e}")
        return None


def get_latest_version(branch: str) -> Optional[str]:
    """Fetch and get the latest version from remote."""
    logger.info(f"Fetching latest from {GIT_REMOTE}/{branch}...")

    success, _, stderr = run_command(
        ['git', 'fetch', GIT_REMOTE, branch],
        cwd=JAM_REPO_DIR,
        timeout=GIT_TIMEOUT
    )
    if not success:
        logger.error(f"Git fetch failed: {stderr}")
        return None

    success, stdout, stderr = run_command(
        ['git', 'rev-parse', f'{GIT_REMOTE}/{branch}'],
        cwd=JAM_REPO_DIR
    )
    if success:
        return stdout.strip()

    logger.error(f"Failed to get remote HEAD: {stderr}")
    return None


# =============================================================================
# Update Display Functions
# =============================================================================

# Display configuration
BACKGROUND_COLOR = (20, 20, 30)  # Dark blue-grey
TEXT_COLOR = (255, 255, 255)
ACCENT_COLOR = (0, 180, 255)  # JAM blue

# Global to track if we're showing the update screen
_update_display_process = None


def get_fb_size() -> tuple:
    """Get framebuffer dimensions."""
    try:
        with open('/sys/class/graphics/fb0/virtual_size', 'r') as f:
            w, h = f.read().strip().split(',')
            return int(w), int(h)
    except:
        return 1920, 1080


def get_font(size: int):
    """Get a font, falling back to default if needed."""
    if not HAS_PIL:
        return None

    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    for path in font_paths:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def create_updating_screen(width: int, height: int) -> Optional[Image.Image]:
    """Create the 'Updating...' screen image."""
    if not HAS_PIL:
        logger.warning("PIL not available for creating update screen")
        return None

    img = Image.new('RGB', (width, height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(img)

    title_font = get_font(72)
    subtitle_font = get_font(36)

    center_x = width // 2
    center_y = height // 2

    # Title
    title = "Updating JAM Player..."
    bbox = draw.textbbox((0, 0), title, font=title_font)
    x = center_x - bbox[2] // 2
    y = center_y - bbox[3] - 30
    draw.text((x, y), title, font=title_font, fill=ACCENT_COLOR)

    # Subtitle
    subtitle = "Please wait. This may take a few minutes."
    bbox = draw.textbbox((0, 0), subtitle, font=subtitle_font)
    x = center_x - bbox[2] // 2
    y = center_y + 30
    draw.text((x, y), subtitle, font=subtitle_font, fill=TEXT_COLOR)

    # Warning
    warning = "Do not disconnect power."
    warning_font = get_font(28)
    bbox = draw.textbbox((0, 0), warning, font=warning_font)
    x = center_x - bbox[2] // 2
    y = center_y + 100
    draw.text((x, y), warning, font=warning_font, fill=(255, 100, 100))  # Red warning

    return img


def show_updating_screen():
    """Display the updating screen using feh."""
    global _update_display_process

    if not HAS_PIL:
        logger.warning("PIL not available, skipping update screen display")
        return

    logger.info("Displaying update screen...")

    try:
        # Get screen size and create image
        width, height = get_fb_size()
        img = create_updating_screen(width, height)
        if img is None:
            return

        # Save to temp file
        img_path = '/tmp/jam_updating.png'
        img.save(img_path, 'PNG')
        os.chmod(img_path, 0o644)

        # Kill any existing feh processes first
        subprocess.run(['pkill', '-f', 'feh'], capture_output=True, timeout=5)

        # Wait for X display to be available (might not be ready yet on boot)
        for _ in range(30):
            result = subprocess.run(
                ['sudo', '-u', 'comitup', 'env', 'DISPLAY=:0', 'xdpyinfo'],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0:
                break
            import time
            time.sleep(1)
        else:
            logger.warning("X display not available, skipping update screen")
            return

        # Display with feh
        _update_display_process = subprocess.Popen(
            ['sudo', '-u', 'comitup', 'env', 'DISPLAY=:0', 'feh', '-F', '--hide-pointer', img_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        logger.info("Update screen displayed")

    except Exception as e:
        logger.warning(f"Failed to display update screen: {e}")


def hide_updating_screen():
    """Kill the updating screen display."""
    global _update_display_process

    if _update_display_process:
        try:
            _update_display_process.terminate()
            _update_display_process.wait(timeout=5)
        except:
            pass
        _update_display_process = None

    # Also kill any feh showing our image
    try:
        subprocess.run(['pkill', '-f', 'feh.*jam_updating'], capture_output=True, timeout=5)
    except:
        pass


# =============================================================================
# Installation Functions
# =============================================================================

def pull_latest(branch: str) -> bool:
    """Pull the latest code from remote."""
    logger.info(f"Pulling latest code from {branch}...")

    success, _, stderr = run_command(
        ['git', 'reset', '--hard', f'{GIT_REMOTE}/{branch}'],
        cwd=JAM_REPO_DIR,
        timeout=60
    )
    if not success:
        logger.error(f"Git reset failed: {stderr}")
        return False

    return True


def ensure_venv_exists() -> bool:
    """Ensure the /opt/jam/venv exists with system-site-packages."""
    if VENV_DIR.exists():
        # Check if it has system-site-packages
        pyvenv_cfg = VENV_DIR / 'pyvenv.cfg'
        if pyvenv_cfg.exists():
            content = pyvenv_cfg.read_text()
            if 'include-system-site-packages = true' in content:
                return True
        # Recreate if missing system-site-packages
        logger.info("Recreating venv with system-site-packages...")
        shutil.rmtree(VENV_DIR)

    logger.info(f"Creating virtual environment at {VENV_DIR}...")
    success, _, stderr = run_command(
        ['/usr/bin/python3', '-m', 'venv', '--system-site-packages', str(VENV_DIR)],
        timeout=120
    )
    if not success:
        logger.error(f"Failed to create venv: {stderr}")
        return False

    # Upgrade pip
    run_command([str(VENV_DIR / 'bin' / 'pip'), 'install', '--upgrade', 'pip'], timeout=120)
    return True


def install_services() -> bool:
    """Install all services to /opt/jam/services/."""
    logger.info("Installing services to /opt/jam/services/...")

    try:
        # Ensure destination directories exist
        SERVICES_DEST.mkdir(parents=True, exist_ok=True)
        (SERVICES_DEST / 'common').mkdir(exist_ok=True)

        # Copy JAM 2.0 service files
        logger.info("  Copying v2 services...")
        for py_file in SERVICES_V2_SRC.glob('*.py'):
            shutil.copy2(py_file, SERVICES_DEST / py_file.name)

        # Copy common module
        common_src = SERVICES_V2_SRC / 'common'
        if common_src.exists():
            for py_file in common_src.glob('*.py'):
                shutil.copy2(py_file, SERVICES_DEST / 'common' / py_file.name)

        # Copy legacy scripts that are still needed (jam_player_app.py, scenes_manager_service.py)
        logger.info("  Copying legacy scripts...")
        legacy_scripts = ['jam_player_app.py', 'scenes_manager_service.py']
        for script in legacy_scripts:
            src = JAM_PLAYER_SRC / script
            if src.exists():
                shutil.copy2(src, SERVICES_DEST / script)
                logger.info(f"    Copied {script}")

        # Copy the entire jam_player package (needed for imports)
        logger.info("  Copying jam_player package...")
        pkg_dest = SERVICES_DEST / 'jam_player'
        if pkg_dest.exists():
            shutil.rmtree(pkg_dest)
        shutil.copytree(JAM_PLAYER_SRC, pkg_dest, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))

        return True
    except Exception as e:
        logger.error(f"Failed to install services: {e}")
        return False


def install_dependencies() -> bool:
    """Install Python dependencies to /opt/jam/venv."""
    logger.info("Installing Python dependencies...")

    # Install requirements.txt if exists
    req_file = SERVICES_V2_SRC / 'requirements.txt'
    if req_file.exists():
        shutil.copy2(req_file, SERVICES_DEST / 'requirements.txt')
        success, _, stderr = run_command(
            [str(VENV_DIR / 'bin' / 'pip'), 'install', '-r', str(SERVICES_DEST / 'requirements.txt')],
            timeout=300
        )
        if not success:
            logger.warning(f"pip install requirements.txt had issues: {stderr}")

    # Install the jam_player package
    logger.info("  Installing jam_player package...")
    success, _, stderr = run_command(
        [str(VENV_DIR / 'bin' / 'pip'), 'install', str(JAM_REPO_DIR)],
        timeout=300
    )
    if not success:
        logger.warning(f"pip install jam_player had issues: {stderr}")

    return True


def install_systemd_units() -> bool:
    """Install systemd service and timer files."""
    logger.info("Installing systemd units...")

    try:
        # Copy service files
        for service_file in SYSTEMD_SRC.glob('*.service'):
            dest = Path('/etc/systemd/system') / service_file.name
            shutil.copy2(service_file, dest)
            logger.info(f"  Installed {service_file.name}")

        # Copy timer files
        for timer_file in SYSTEMD_SRC.glob('*.timer'):
            dest = Path('/etc/systemd/system') / timer_file.name
            shutil.copy2(timer_file, dest)
            logger.info(f"  Installed {timer_file.name}")

        # Reload systemd
        logger.info("  Reloading systemd daemon...")
        run_command(['systemctl', 'daemon-reload'])

        return True
    except Exception as e:
        logger.error(f"Failed to install systemd units: {e}")
        return False


def update_version_file(version: str) -> bool:
    """Update the version file."""
    try:
        VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        VERSION_FILE.write_text(version)
        logger.info(f"Version updated to: {version[:12]}...")
        return True
    except Exception as e:
        logger.error(f"Failed to update version file: {e}")
        return False


# =============================================================================
# Cleanup Functions
# =============================================================================

def cleanup_legacy_cruft():
    """Remove legacy files/directories that are no longer needed."""
    logger.info("Cleaning up legacy cruft...")

    # Items to remove (files and directories)
    items_to_remove: List[Path] = [
        # Old venvs - we use /opt/jam/venv now
        LEGACY_APP_VENV,
        LEGACY_SCRIPTS_VENV,

        # Old script copies - now in /opt/jam/services/
        LEGACY_JAM_DIR / 'jam_player_app.py',
        LEGACY_JAM_DIR / 'scenes_manager_service.py',

        # Old autostart files - systemd manages services now
        Path('/home/comitup/.config/autostart'),

        # Old scripts that are no longer used
        LEGACY_JAM_DIR / 'scripts' / 'app_watchdog.sh',
        LEGACY_JAM_DIR / 'scripts' / 'check_set_timezone.py',

        # Old combined jam repo - replaced by dedicated jam-player repo
        LEGACY_JAM_REPO,
    ]

    for item in items_to_remove:
        try:
            if item.exists():
                if item.is_dir():
                    shutil.rmtree(item)
                    logger.info(f"  Removed directory: {item}")
                else:
                    item.unlink()
                    logger.info(f"  Removed file: {item}")
        except Exception as e:
            logger.warning(f"  Failed to remove {item}: {e}")


def install_crontab():
    """Install the JAM crontab with essential scheduled tasks."""
    logger.info("Installing JAM crontab...")

    crontab_src = CRON_SRC / 'jam_crontab.txt'
    if not crontab_src.exists():
        logger.warning(f"Crontab file not found: {crontab_src}")
        return

    # Install crontab for root user (needed for reboot command)
    success, _, stderr = run_command(
        ['crontab', str(crontab_src)],
        timeout=30
    )
    if success:
        logger.info("  Crontab installed for root user")
    else:
        logger.warning(f"  Failed to install crontab: {stderr}")


def install_logrotate_config():
    """Install the logrotate configuration to /etc/jam/."""
    logger.info("Installing logrotate config...")

    logrotate_src = LOGROTATE_SRC / 'logrotate.conf'
    logrotate_dest = Path('/etc/jam/logrotate.conf')

    if not logrotate_src.exists():
        logger.warning(f"Logrotate config not found: {logrotate_src}")
        return

    try:
        logrotate_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(logrotate_src, logrotate_dest)

        # Logrotate requires root ownership
        os.chown(logrotate_dest, 0, 0)  # root:root
        os.chmod(logrotate_dest, 0o644)

        logger.info(f"  Installed {logrotate_dest}")
    except Exception as e:
        logger.warning(f"  Failed to install logrotate config: {e}")


def restart_services():
    """
    Restart JAM services to pick up new code.

    Uses --no-block to avoid waiting for each service to fully start.
    Type=notify services can take time to initialize, and we don't want
    the update process to hang waiting for them.

    After triggering all restarts, we verify services are starting correctly.
    """
    logger.info("Restarting JAM services...")

    # Services to restart - ordered by dependency (independent ones first)
    services_to_restart = [
        'jam-content-manager.service',    # Type=simple, starts fast
        'jam-ble-state-manager.service',  # Type=notify, but sends READY=1 early
        'jam-player-display.service',     # Type=notify, sends READY=1 early
        'jam-health-monitor.service',     # Type=notify, sends READY=1 early
        'jam-heartbeat.service',          # Type=notify, sends READY=1 early (has ConditionPath)
        'jam-tailscale.service',          # Type=oneshot, runs once
    ]

    # Trigger all restarts with --no-block to avoid waiting
    # This is more reliable than waiting for Type=notify services
    logger.info("  Triggering service restarts (non-blocking)...")
    for service in services_to_restart:
        success, _, stderr = run_command(
            ['systemctl', 'restart', '--no-block', service],
            timeout=10
        )
        if success:
            logger.info(f"    Triggered restart: {service}")
        else:
            # Log warning but continue - the service might just not be enabled
            logger.warning(f"    Failed to trigger restart for {service}: {stderr[:100] if stderr else 'unknown'}")

    # Give services a moment to start
    import time
    logger.info("  Waiting for services to initialize...")
    time.sleep(5)

    # Verify critical services are running or starting
    # We check for 'active' or 'activating' status
    logger.info("  Verifying service status...")
    critical_services = [
        'jam-player-display.service',
        'jam-ble-state-manager.service',
        'jam-content-manager.service',
    ]

    all_ok = True
    for service in critical_services:
        success, stdout, _ = run_command(
            ['systemctl', 'is-active', service],
            timeout=5
        )
        status = stdout.strip() if stdout else 'unknown'
        if status in ('active', 'activating'):
            logger.info(f"    {service}: {status}")
        else:
            logger.warning(f"    {service}: {status} (may need attention)")
            all_ok = False

    if all_ok:
        logger.info("  All critical services running")
    else:
        logger.warning("  Some services may not have started correctly - health monitor will handle recovery")


# =============================================================================
# Error Reporting
# =============================================================================

def report_error(error_message: str):
    """Report an update failure to the backend."""
    try:
        if not is_device_registered():
            logger.warning("Device not registered, skipping error report")
            return

        if len(error_message) > 2048:
            error_message = error_message[:2045] + "..."

        response = api_request(
            method='POST',
            path='/jam-players/errors',
            body={
                'affectedService': 'JAM_UPDATE',
                'errorMessage': f"Update failed: {error_message}",
                'severity': 'HIGH'
            },
            signed=True
        )

        if response and response.status_code == 200:
            logger.info("Error reported to backend")
        else:
            logger.warning("Failed to report error to backend")
    except Exception as e:
        logger.warning(f"Failed to report error: {e}")


# =============================================================================
# Main
# =============================================================================

def main():
    log_service_start(logger, 'JAM Update Service')

    # Check if repo exists
    if not JAM_REPO_DIR.exists():
        logger.error(f"JAM repo not found at {JAM_REPO_DIR}")
        sys.exit(1)

    # Configure git to trust the repo (runs as root, repo owned by comitup)
    configure_git_safe_directory()

    # Determine which branch to use (supports non-prod environments)
    branch = get_git_branch()
    if branch != GIT_BRANCH_DEFAULT:
        logger.info(f"Environment override: using branch '{branch}'")

    # Get current version
    current_version = get_current_version()
    force_install = False

    if current_version:
        logger.info(f"Current version: {current_version[:12]}...")
    else:
        # No version.txt means device hasn't been migrated to JAM 2.0 yet
        # Force a full installation regardless of git state
        logger.info("No version.txt found - this appears to be a fresh JAM 2.0 migration")
        force_install = True

    # Get latest version
    latest_version = get_latest_version(branch)
    if not latest_version:
        logger.error("Could not fetch latest version - skipping update")
        sys.exit(0)

    logger.info(f"Latest version: {latest_version[:12]}...")

    # Check if update is needed (skip check if forcing install)
    if not force_install and current_version == latest_version:
        logger.info("Already up to date")
        sys.exit(0)

    if force_install:
        logger.info("Performing initial JAM 2.0 installation...")
    else:
        logger.info(f"Update available: {current_version[:12]}... -> {latest_version[:12]}...")

    # Show updating screen to user
    show_updating_screen()

    # Pull latest code
    if not pull_latest(branch):
        hide_updating_screen()
        report_error("Failed to pull latest code from git")
        sys.exit(1)

    # Ensure venv exists
    if not ensure_venv_exists():
        hide_updating_screen()
        report_error("Failed to create/verify virtual environment")
        sys.exit(1)

    # Install services
    if not install_services():
        hide_updating_screen()
        report_error("Failed to install services")
        sys.exit(1)

    # Install dependencies
    if not install_dependencies():
        hide_updating_screen()
        report_error("Failed to install dependencies")
        sys.exit(1)

    # Install systemd units
    if not install_systemd_units():
        hide_updating_screen()
        report_error("Failed to install systemd units")
        sys.exit(1)

    # Update version file
    update_version_file(latest_version)

    # Clean up legacy cruft
    cleanup_legacy_cruft()

    # Install crontab with essential scheduled tasks (3am reboot, logrotate)
    install_crontab()

    # Install logrotate config
    install_logrotate_config()

    # Restart services to pick up changes
    restart_services()

    # Hide updating screen (jam-player-display.service will take over)
    hide_updating_screen()

    logger.info("Update completed successfully!")
    sys.exit(0)


if __name__ == '__main__':
    main()
