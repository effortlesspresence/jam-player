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
import time
from pathlib import Path
from typing import Optional, List, Callable, TypeVar

T = TypeVar('T')

# Add the services directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.logging_config import setup_service_logging, log_service_start
from common.api import report_error as api_report_error, ErrorSeverity, SystemService
from common.paths import ENVIRONMENT_FILE, safe_write_text

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
CONFIG_SRC = JAM_REPO_DIR / 'jam_player' / 'config'
ETC_SRC = JAM_REPO_DIR / 'etc'  # System config files (dbus, bluetooth)

# Backup paths (for rollback on failed updates)
BACKUP_DIR = OPT_JAM_DIR / 'backup'
SERVICES_BACKUP = BACKUP_DIR / 'services'
SYSTEMD_BACKUP = BACKUP_DIR / 'systemd'
VERSION_BACKUP = BACKUP_DIR / 'version.txt'

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


# Retry configuration for git network operations
GIT_RETRY_MAX_ATTEMPTS = 5
GIT_RETRY_INITIAL_DELAY = 5  # seconds
GIT_RETRY_MAX_DELAY = 60  # seconds


def retry_with_backoff(
    operation: Callable[[], T],
    operation_name: str,
    max_attempts: int = GIT_RETRY_MAX_ATTEMPTS,
    initial_delay: int = GIT_RETRY_INITIAL_DELAY,
    max_delay: int = GIT_RETRY_MAX_DELAY,
) -> T:
    """
    Retry an operation with exponential backoff.

    Args:
        operation: A callable that returns a value. Should return None on failure.
        operation_name: Human-readable name for logging.
        max_attempts: Maximum number of attempts.
        initial_delay: Initial delay between retries in seconds.
        max_delay: Maximum delay between retries in seconds.

    Returns:
        The result of the operation, or None if all attempts failed.
    """
    delay = initial_delay

    for attempt in range(1, max_attempts + 1):
        result = operation()

        if result is not None and result is not False:
            if attempt > 1:
                logger.info(f"{operation_name} succeeded on attempt {attempt}")
            return result

        if attempt < max_attempts:
            logger.warning(
                f"{operation_name} failed (attempt {attempt}/{max_attempts}), "
                f"retrying in {delay}s..."
            )
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
        else:
            logger.error(f"{operation_name} failed after {max_attempts} attempts")

    return None


# =============================================================================
# Backup and Rollback Functions
# =============================================================================

def create_backup() -> bool:
    """
    Create a backup of current installation before updating.

    Backs up:
    - /opt/jam/services/ -> /opt/jam/backup/services/
    - /etc/systemd/system/jam-*.service -> /opt/jam/backup/systemd/
    - /etc/jam/version.txt -> /opt/jam/backup/version.txt

    Returns:
        True if backup was created successfully, False otherwise.
    """
    logger.info("Creating backup of current installation...")

    try:
        # Clean up any existing backup
        if BACKUP_DIR.exists():
            shutil.rmtree(BACKUP_DIR)

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)

        # Backup services directory
        if SERVICES_DEST.exists():
            shutil.copytree(SERVICES_DEST, SERVICES_BACKUP)
            logger.info(f"  Backed up {SERVICES_DEST}")
        else:
            logger.info("  No existing services directory to backup")

        # Backup systemd units
        SYSTEMD_BACKUP.mkdir(parents=True, exist_ok=True)
        systemd_dir = Path('/etc/systemd/system')
        backed_up_units = 0
        for pattern in ['jam-*.service', 'jam-*.timer', 'jam-*.path']:
            for unit_file in systemd_dir.glob(pattern):
                shutil.copy2(unit_file, SYSTEMD_BACKUP / unit_file.name)
                backed_up_units += 1
        logger.info(f"  Backed up {backed_up_units} systemd units")

        # Backup version file
        if VERSION_FILE.exists():
            shutil.copy2(VERSION_FILE, VERSION_BACKUP)
            logger.info(f"  Backed up {VERSION_FILE}")
        else:
            logger.info("  No existing version file to backup")

        logger.info("Backup created successfully")
        return True

    except Exception as e:
        logger.error(f"Failed to create backup: {e}")
        # Clean up partial backup
        if BACKUP_DIR.exists():
            try:
                shutil.rmtree(BACKUP_DIR)
            except:
                pass
        return False


def rollback_from_backup() -> bool:
    """
    Restore the previous installation from backup.

    This is called when an update fails partway through. It restores:
    - Services directory
    - Systemd unit files
    - Version file

    Returns:
        True if rollback succeeded, False otherwise.
    """
    logger.warning("Rolling back to previous installation...")

    if not BACKUP_DIR.exists():
        logger.error("No backup directory found - cannot rollback")
        return False

    success = True

    try:
        # Restore services directory
        if SERVICES_BACKUP.exists():
            if SERVICES_DEST.exists():
                shutil.rmtree(SERVICES_DEST)
            shutil.copytree(SERVICES_BACKUP, SERVICES_DEST)
            logger.info(f"  Restored {SERVICES_DEST}")
        else:
            logger.warning("  No services backup to restore")

    except Exception as e:
        logger.error(f"  Failed to restore services: {e}")
        success = False

    try:
        # Restore systemd units
        if SYSTEMD_BACKUP.exists():
            systemd_dir = Path('/etc/systemd/system')
            restored_units = 0
            for unit_file in SYSTEMD_BACKUP.glob('*'):
                shutil.copy2(unit_file, systemd_dir / unit_file.name)
                restored_units += 1
            logger.info(f"  Restored {restored_units} systemd units")
            # Reload systemd to pick up restored units
            run_command(['systemctl', 'daemon-reload'])
        else:
            logger.warning("  No systemd backup to restore")

    except Exception as e:
        logger.error(f"  Failed to restore systemd units: {e}")
        success = False

    try:
        # Restore version file
        if VERSION_BACKUP.exists():
            shutil.copy2(VERSION_BACKUP, VERSION_FILE)
            logger.info(f"  Restored {VERSION_FILE}")
        else:
            logger.warning("  No version backup to restore")

    except Exception as e:
        logger.error(f"  Failed to restore version file: {e}")
        success = False

    if success:
        logger.info("Rollback completed successfully")
    else:
        logger.error("Rollback completed with errors - system may be in inconsistent state")

    return success


def cleanup_backup():
    """Remove the backup directory after a successful update."""
    if BACKUP_DIR.exists():
        try:
            shutil.rmtree(BACKUP_DIR)
            logger.info("Cleaned up backup directory")
        except Exception as e:
            logger.warning(f"Failed to clean up backup: {e}")


def check_and_reexec_if_updated() -> bool:
    """
    Check if jam_update.py itself has changed and re-exec if needed.

    This solves the chicken-and-egg problem where changes to jam_update.py
    (like new install functions) don't take effect until the NEXT update,
    because the currently running process is the old version.

    Flow:
    1. Compare repo version vs currently running version
    2. If different and not already re-execed:
       a. Copy new version to /opt/jam/services/
       b. Re-exec with the new version

    Uses JAM_UPDATE_REEXEC environment variable to prevent infinite loops.

    Returns:
        True if we should continue (no re-exec needed or re-exec failed safely)
        Does not return if re-exec succeeds (process is replaced)
    """
    REEXEC_ENV_VAR = 'JAM_UPDATE_REEXEC'

    # Check if we've already re-execed (prevent infinite loop)
    if os.environ.get(REEXEC_ENV_VAR) == '1':
        logger.info("Running after re-exec - continuing with new version")
        return True

    # Paths
    repo_script = SERVICES_V2_SRC / 'jam_update.py'
    installed_script = SERVICES_DEST / 'jam_update.py'

    # If repo script doesn't exist, something is wrong - continue anyway
    if not repo_script.exists():
        logger.warning(f"Repo script not found at {repo_script} - skipping re-exec check")
        return True

    # If installed script doesn't exist, this is first install - no need to re-exec
    if not installed_script.exists():
        logger.info("No installed jam_update.py yet - skipping re-exec check")
        return True

    try:
        repo_content = repo_script.read_text()
        installed_content = installed_script.read_text()

        if repo_content == installed_content:
            logger.info("jam_update.py unchanged - no re-exec needed")
            return True

        logger.info("jam_update.py has changed - preparing to re-exec with new version...")

        # Copy new version to installed location
        shutil.copy2(repo_script, installed_script)
        logger.info(f"Copied new jam_update.py to {installed_script}")

        # Set environment variable to prevent infinite loop
        os.environ[REEXEC_ENV_VAR] = '1'

        # Re-exec with the new script
        # Use the same Python interpreter and the installed script path
        python_exe = sys.executable
        script_path = str(installed_script)

        logger.info(f"Re-executing: {python_exe} {script_path}")

        # os.execv replaces the current process - this does not return on success
        os.execv(python_exe, [python_exe, script_path])

        # If we get here, execv failed
        logger.error("os.execv returned unexpectedly - this should not happen")
        return True

    except Exception as e:
        # If anything goes wrong, log it and continue with old version
        # Better to complete the update with old logic than to fail entirely
        logger.error(f"Re-exec check failed: {e} - continuing with current version")
        return True


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
    new dedicated 'jam-player' repo. Retries with exponential backoff
    on failure (GitHub can be intermittently unavailable).
    """
    logger.info(f"Cloning jam-player repo to {JAM_REPO_DIR}...")

    def attempt_clone():
        success, _, stderr = run_command(
            ['git', 'clone', '--branch', branch, '--single-branch', GIT_REPO_URL, str(JAM_REPO_DIR)],
            timeout=GIT_TIMEOUT
        )
        if not success:
            logger.warning(f"Clone attempt failed: {stderr}")
            # Clean up partial clone if it exists
            if JAM_REPO_DIR.exists():
                shutil.rmtree(JAM_REPO_DIR)
            return None
        return True

    result = retry_with_backoff(attempt_clone, "git clone")
    if not result:
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
    """
    Fetch and get the latest version from remote.

    Retries with exponential backoff on failure (GitHub can be
    intermittently unavailable).
    """
    logger.info(f"Fetching latest from {GIT_REMOTE}/{branch}...")

    def attempt_fetch():
        success, _, stderr = run_command(
            ['git', 'fetch', GIT_REMOTE, branch],
            cwd=JAM_REPO_DIR,
            timeout=GIT_TIMEOUT
        )
        if not success:
            logger.warning(f"Fetch attempt failed: {stderr}")
            return None
        return True

    result = retry_with_backoff(attempt_fetch, "git fetch")
    if not result:
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
TEXT_COLOR = (255, 255, 255)
JAM_ORANGE = (255, 107, 53)  # #FF6B35

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


def get_font(size: int, bold: bool = True):
    """Get a font, falling back to default if needed."""
    if not HAS_PIL:
        return None

    if bold:
        font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    else:
        font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        ]
    for path in font_paths:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def create_mesh_gradient_background(width: int, height: int) -> Image.Image:
    """
    Create a vibrant mesh gradient background for the update screen.
    Uses teal/cyan/blue colors to indicate "updating" state.
    """
    import math

    img = Image.new('RGB', (width, height))

    # Color anchor points: (x_ratio, y_ratio, (r, g, b))
    # Teal/cyan theme for "updating" feel
    color_points = [
        # Deep blue top
        (0.5, 0.0, (40, 60, 160)),
        # Teal left
        (0.0, 0.4, (30, 150, 160)),
        # Cyan center glow
        (0.5, 0.5, (50, 200, 220)),
        # Purple right
        (1.0, 0.3, (120, 60, 180)),
        # Blue bottom-right
        (1.0, 1.0, (40, 100, 200)),
        # Teal bottom-left
        (0.0, 1.0, (30, 140, 150)),
    ]

    # Process in chunks for speed
    step = 2

    draw = ImageDraw.Draw(img)

    for y in range(0, height, step):
        for x in range(0, width, step):
            # Normalize coordinates
            nx = x / width
            ny = y / height

            # Calculate weighted color based on distance to each anchor point
            total_weight = 0.0
            r_sum, g_sum, b_sum = 0.0, 0.0, 0.0

            for px, py, color in color_points:
                dx = nx - px
                dy = ny - py
                dist = math.sqrt(dx * dx + dy * dy)
                weight = 1.0 / (dist * dist * 4 + 0.01)

                r_sum += color[0] * weight
                g_sum += color[1] * weight
                b_sum += color[2] * weight
                total_weight += weight

            r = int(min(255, max(0, r_sum / total_weight)))
            g = int(min(255, max(0, g_sum / total_weight)))
            b = int(min(255, max(0, b_sum / total_weight)))

            draw.rectangle([x, y, x + step, y + step], fill=(r, g, b))

    return img


def create_updating_screen(width: int, height: int) -> Optional[Image.Image]:
    """Create the 'Updating...' screen image with vibrant gradient."""
    if not HAS_PIL:
        logger.warning("PIL not available for creating update screen")
        return None

    # Create vibrant gradient background
    img = create_mesh_gradient_background(width, height)
    draw = ImageDraw.Draw(img)

    title_font = get_font(72)
    subtitle_font = get_font(36, bold=False)
    warning_font = get_font(28, bold=False)

    center_x = width // 2
    center_y = height // 2

    # Title
    title = "Updating JAM Player..."
    draw.text(
        (center_x, center_y - 50),
        title,
        font=title_font,
        fill=TEXT_COLOR,
        anchor="mm"
    )

    # Subtitle
    subtitle = "Please wait. This may take a few minutes."
    draw.text(
        (center_x, center_y + 30),
        subtitle,
        font=subtitle_font,
        fill=TEXT_COLOR,
        anchor="mm"
    )

    # Warning - use orange instead of red for better visibility on gradient
    warning = "Do not disconnect power."
    draw.text(
        (center_x, center_y + 100),
        warning,
        font=warning_font,
        fill=JAM_ORANGE,
        anchor="mm"
    )

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
        installed_services = []

        # Copy service files
        for service_file in SYSTEMD_SRC.glob('*.service'):
            dest = Path('/etc/systemd/system') / service_file.name
            shutil.copy2(service_file, dest)
            logger.info(f"  Installed {service_file.name}")
            installed_services.append(service_file.name)

        # Copy timer files
        for timer_file in SYSTEMD_SRC.glob('*.timer'):
            dest = Path('/etc/systemd/system') / timer_file.name
            shutil.copy2(timer_file, dest)
            logger.info(f"  Installed {timer_file.name}")

        # Copy path files (for file-watching triggers)
        for path_file in SYSTEMD_SRC.glob('*.path'):
            dest = Path('/etc/systemd/system') / path_file.name
            shutil.copy2(path_file, dest)
            logger.info(f"  Installed {path_file.name}")

        # Reload systemd
        logger.info("  Reloading systemd daemon...")
        run_command(['systemctl', 'daemon-reload'])

        # Enable all installed services (idempotent, safe to run on already-enabled services)
        logger.info("  Enabling services...")
        for service in installed_services:
            run_command(['systemctl', 'enable', service], timeout=10)

        return True
    except Exception as e:
        logger.error(f"Failed to install systemd units: {e}")
        return False


def update_version_file(version: str) -> bool:
    """Update the version file."""
    try:
        VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        safe_write_text(VERSION_FILE, version)
        logger.info(f"Version updated to: {version[:12]}...")
        return True
    except Exception as e:
        logger.error(f"Failed to update version file: {e}")
        return False


# =============================================================================
# Cleanup Functions
# =============================================================================

def ensure_jam_user_exists() -> bool:
    """
    Ensure the 'jam' user exists for SSH key-based authentication.

    The jam user:
    - Is used for remote support SSH access
    - Can ONLY authenticate via SSH keys (no password)
    - Has its authorized_keys populated by jam-first-boot.service

    The comitup user remains as a backup with password auth.

    This is called during updates to ensure old devices (that already
    completed first-boot before the jam user was introduced) get the
    user created.

    Returns True if user exists or was created successfully.
    """
    import pwd

    try:
        pwd.getpwnam('jam')
        logger.debug("jam user already exists")
        return True
    except KeyError:
        pass  # User doesn't exist, create it

    logger.info("Creating jam user for SSH key authentication...")

    try:
        # Create user with home directory, no password (SSH key only)
        success, _, stderr = run_command([
            'useradd',
            '-m',              # Create home directory
            '-s', '/bin/bash', # Shell
            '-c', 'JAM Player SSH Access',  # Comment
            'jam'
        ])

        if not success:
            logger.error(f"Failed to create jam user: {stderr}")
            return False

        # Lock the password (prevents password auth, SSH keys only)
        success, _, stderr = run_command(['passwd', '-l', 'jam'])

        if not success:
            logger.warning(f"Failed to lock jam password: {stderr}")
            # Not fatal - user still created

        logger.info("Created jam user successfully (SSH key auth only)")
        return True

    except Exception as e:
        logger.error(f"Failed to create jam user: {e}")
        return False


def disable_comitup():
    """
    Disable comitup - JAM 2.0 uses its own BLE provisioning instead.

    Comitup creates a WiFi hotspot for provisioning, but JAM 2.0 uses BLE
    for WiFi provisioning. The comitup hotspot interferes with WiFi connections
    because it uses the same wlan0 interface.
    """
    logger.info("Disabling comitup (JAM 2.0 uses BLE provisioning)...")

    # Stop and disable comitup service
    run_command(['systemctl', 'stop', 'comitup'], timeout=30)
    run_command(['systemctl', 'disable', 'comitup'], timeout=30)
    logger.info("  Stopped and disabled comitup service")

    # Remove the JAM-SETUP hotspot connection profiles
    result = run_command(
        ['nmcli', '-t', '-f', 'NAME', 'connection', 'show'],
        timeout=10
    )
    if result[0]:  # success
        stdout = result[1]
        for line in stdout.strip().split('\n'):
            conn_name = line.strip()
            if conn_name.startswith('JAM-SETUP'):
                run_command(['nmcli', 'connection', 'delete', conn_name], timeout=10)
                logger.info(f"  Removed hotspot connection: {conn_name}")

    # Clear comitup state
    comitup_state = Path('/var/lib/comitup')
    if comitup_state.exists():
        try:
            shutil.rmtree(comitup_state)
            logger.info("  Cleared comitup state directory")
        except Exception as e:
            logger.warning(f"  Failed to clear comitup state: {e}")

    logger.info("  Comitup disabled successfully")


def cleanup_legacy_cruft():
    """Remove legacy files/directories that are no longer needed."""
    logger.info("Cleaning up legacy cruft...")

    # Disable comitup first - it interferes with JAM 2.0 BLE provisioning
    disable_comitup()

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


def install_chrony_peering_config():
    """
    Install chrony peering configuration for offline clock sync.

    This enables JAM Players in the same ScreenLayout to sync their clocks
    with each other when internet connectivity is lost. The config:
    - Allows peer queries from local network (10.x, 172.16.x, 192.168.x)
    - Enables local stratum 10 for serving time when NTP is unavailable

    The jam-chrony-peering.service discovers other JAM Players and adds
    them as chrony peers dynamically.
    """
    logger.info("Installing chrony peering config...")

    chrony_src = CONFIG_SRC / 'chrony-jam-peering.conf'
    chrony_dest = Path('/etc/chrony/conf.d/jam-peering.conf')

    if not chrony_src.exists():
        logger.warning(f"Chrony peering config not found: {chrony_src}")
        return

    try:
        # Ensure conf.d directory exists (it should on most systems)
        chrony_dest.parent.mkdir(parents=True, exist_ok=True)

        # Copy config file
        shutil.copy2(chrony_src, chrony_dest)
        os.chown(chrony_dest, 0, 0)  # root:root
        os.chmod(chrony_dest, 0o644)

        logger.info(f"  Installed {chrony_dest}")

        # Restart chrony to pick up the new config
        success, _, stderr = run_command(['systemctl', 'restart', 'chrony'], timeout=30)
        if success:
            logger.info("  Restarted chrony to apply config")
        else:
            logger.warning(f"  Failed to restart chrony: {stderr}")

    except Exception as e:
        logger.warning(f"  Failed to install chrony peering config: {e}")


def install_boot_config():
    """
    Install/update HDMI and display settings in the Raspberry Pi boot config.

    This ensures proper 4K output and HDMI detection. Settings include:
    - hdmi_enable_4kp60=1: Enable 4K at 60Hz
    - hdmi_force_hotplug=1: Don't wait for display detection
    - disable_overscan=1: Remove black borders
    - hdmi_group=0: Auto-detect resolution from EDID

    The config is appended to /boot/firmware/config.txt if the marker
    isn't already present. Changes require a reboot to take effect.
    """
    logger.info("Checking boot config for HDMI settings...")

    boot_config_src = CONFIG_SRC / 'boot-config.txt'

    # Try both possible locations (newer and older Pi OS)
    boot_config_paths = [
        Path('/boot/firmware/config.txt'),
        Path('/boot/config.txt'),
    ]

    boot_config_dest = None
    for path in boot_config_paths:
        if path.exists():
            boot_config_dest = path
            break

    if not boot_config_dest:
        logger.warning("Could not find boot config.txt - skipping HDMI config")
        return

    if not boot_config_src.exists():
        logger.warning(f"Boot config source not found: {boot_config_src}")
        return

    try:
        # Check if our settings are already present
        marker = "# JAM Player HDMI/Display Configuration"
        current_config = boot_config_dest.read_text()

        if marker in current_config:
            logger.info("  Boot config already has JAM HDMI settings")
            return

        # Read our settings
        jam_settings = boot_config_src.read_text()

        # Append to boot config
        with open(boot_config_dest, 'a') as f:
            f.write("\n")
            f.write(jam_settings)

        logger.info(f"  Appended HDMI settings to {boot_config_dest}")
        logger.info("  NOTE: Reboot required for boot config changes to take effect")

    except Exception as e:
        logger.warning(f"  Failed to install boot config: {e}")


def install_wifi_stability_configs():
    """
    Install NetworkManager configuration for WiFi stability.

    These configs improve WiFi reliability on Raspberry Pi:
    - wifi-powersave-off.conf: Disables WiFi power management to prevent
      the Broadcom chip from becoming unresponsive
    - wifi-stability.conf: Unlimited auth retries, disable MAC randomization

    Also immediately disables power save for the current session using iw.
    """
    logger.info("Installing WiFi stability configs...")

    nm_conf_src = ETC_SRC / 'NetworkManager' / 'conf.d'
    nm_conf_dest = Path('/etc/NetworkManager/conf.d')

    if not nm_conf_src.exists():
        logger.warning(f"NetworkManager config source not found: {nm_conf_src}")
        return

    try:
        nm_conf_dest.mkdir(parents=True, exist_ok=True)

        # Install all config files from the source directory
        for conf_file in nm_conf_src.glob('*.conf'):
            dest_file = nm_conf_dest / conf_file.name
            shutil.copy2(conf_file, dest_file)
            os.chown(dest_file, 0, 0)  # root:root
            os.chmod(dest_file, 0o644)
            logger.info(f"  Installed {dest_file}")

        # Immediately disable power save for current session
        # (config file takes effect on next NetworkManager restart or reboot)
        success, _, stderr = run_command(['iw', 'wlan0', 'set', 'power_save', 'off'], timeout=10)
        if success:
            logger.info("  Disabled WiFi power save for current session")
        else:
            logger.warning(f"  Failed to disable power save immediately: {stderr}")

    except Exception as e:
        logger.warning(f"  Failed to install WiFi stability configs: {e}")


def install_ble_configs():
    """
    Install D-Bus and BlueZ configuration files for BLE provisioning.

    These configs are required for the NoInputNoOutput pairing agent to work
    properly, preventing Bluetooth pairing popups on both the Pi and mobile devices.

    Installs:
    - /etc/dbus-1/system.d/jam-ble-provisioning.conf: D-Bus permissions for agent
    - /etc/bluetooth/main.conf: BlueZ configuration for JustWorks pairing
    """
    logger.info("Installing BLE configuration files...")

    # Install D-Bus config for BLE agent
    dbus_config_src = ETC_SRC / 'dbus-1' / 'system.d' / 'jam-ble-provisioning.conf'
    dbus_config_dest = Path('/etc/dbus-1/system.d/jam-ble-provisioning.conf')

    if dbus_config_src.exists():
        try:
            dbus_config_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dbus_config_src, dbus_config_dest)
            os.chown(dbus_config_dest, 0, 0)  # root:root
            os.chmod(dbus_config_dest, 0o644)
            logger.info(f"  Installed {dbus_config_dest}")
        except Exception as e:
            logger.warning(f"  Failed to install D-Bus config: {e}")
    else:
        logger.debug(f"  D-Bus config not found: {dbus_config_src}")

    # Install BlueZ main.conf
    bluetooth_config_src = ETC_SRC / 'bluetooth' / 'main.conf'
    bluetooth_config_dest = Path('/etc/bluetooth/main.conf')

    if bluetooth_config_src.exists():
        try:
            bluetooth_config_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bluetooth_config_src, bluetooth_config_dest)
            os.chown(bluetooth_config_dest, 0, 0)  # root:root
            os.chmod(bluetooth_config_dest, 0o644)
            logger.info(f"  Installed {bluetooth_config_dest}")

            # Restart bluetooth service to pick up new config
            success, _, stderr = run_command(['systemctl', 'restart', 'bluetooth'], timeout=30)
            if success:
                logger.info("  Restarted bluetooth service to apply config")
            else:
                logger.warning(f"  Failed to restart bluetooth: {stderr}")
        except Exception as e:
            logger.warning(f"  Failed to install BlueZ config: {e}")
    else:
        logger.debug(f"  BlueZ config not found: {bluetooth_config_src}")


def install_lightdm_cursor_config():
    """
    Configure lightdm to hide the X11 cursor.

    This prevents the cursor from showing on the desktop background during
    boot and between display transitions. The -nocursor flag tells X11 to
    not render any cursor at all.

    Modifies /etc/lightdm/lightdm.conf to add xserver-command=X -nocursor
    under the [Seat:*] section.
    """
    logger.info("Checking lightdm config for cursor hiding...")

    lightdm_conf = Path('/etc/lightdm/lightdm.conf')

    if not lightdm_conf.exists():
        logger.warning(f"lightdm.conf not found at {lightdm_conf} - skipping cursor config")
        return

    try:
        content = lightdm_conf.read_text()

        # Check if -nocursor is already configured
        if '-nocursor' in content:
            logger.info("  lightdm already configured with -nocursor")
            return

        # Check if there's an existing xserver-command line we need to modify
        lines = content.split('\n')
        new_lines = []
        modified = False
        in_seat_section = False

        for line in lines:
            # Track if we're in the [Seat:*] section
            if line.strip().startswith('['):
                in_seat_section = line.strip() == '[Seat:*]'

            # If we find an existing xserver-command, add -nocursor to it
            if in_seat_section and line.strip().startswith('xserver-command='):
                if '-nocursor' not in line:
                    # Append -nocursor to existing command
                    line = line.rstrip() + ' -nocursor'
                    modified = True
                    logger.info("  Added -nocursor to existing xserver-command")

            new_lines.append(line)

        # If no xserver-command was found, add it under [Seat:*]
        if not modified:
            final_lines = []
            added = False
            for line in new_lines:
                final_lines.append(line)
                if line.strip() == '[Seat:*]' and not added:
                    final_lines.append('xserver-command=X -nocursor')
                    added = True
                    modified = True
                    logger.info("  Added xserver-command=X -nocursor under [Seat:*]")

            # If [Seat:*] section doesn't exist, append it
            if not added:
                final_lines.append('')
                final_lines.append('[Seat:*]')
                final_lines.append('xserver-command=X -nocursor')
                modified = True
                logger.info("  Added [Seat:*] section with xserver-command=X -nocursor")

            new_lines = final_lines

        if modified:
            # Write the modified config
            lightdm_conf.write_text('\n'.join(new_lines))
            logger.info(f"  Updated {lightdm_conf}")
            logger.info("  NOTE: Reboot required for lightdm changes to take effect")
        else:
            logger.info("  No changes needed to lightdm config")

    except Exception as e:
        logger.warning(f"  Failed to install lightdm cursor config: {e}")


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
        'jam-ble-provisioning.service',   # Type=notify, BLE provisioning (must restart after BLE config)
        'jam-ble-state-manager.service',  # Type=notify, but sends READY=1 early
        'jam-player-display.service',     # Type=notify, sends READY=1 early
        'jam-health-monitor.service',     # Type=notify, sends READY=1 early
        'jam-heartbeat.service',          # Type=notify, sends READY=1 early (has ConditionPath)
        'jam-ws-commands.service',        # Type=notify, WebSocket commands (has ConditionPath)
        'jam-chrony-peering.service',     # Type=simple, chrony peer discovery
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
    api_report_error(
        SystemService.JAM_UPDATE,
        f"Update failed: {error_message}",
        ErrorSeverity.HIGH,
    )


# =============================================================================
# Main
# =============================================================================

def main():
    log_service_start(logger, 'JAM Update Service')

    # Always disable comitup on every run - it interferes with JAM 2.0 BLE provisioning
    # This is idempotent and safe to run repeatedly, ensuring comitup stays disabled
    # even if a previous attempt failed or the device was imaged with comitup enabled
    disable_comitup()

    # Ensure jam user exists for SSH key authentication
    # This creates the user on old devices that already completed first-boot
    # before the jam user was introduced
    ensure_jam_user_exists()

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

    # Helper function to handle update failure with rollback
    def fail_update(error_msg: str, should_rollback: bool = True):
        """Handle update failure: rollback if needed, report error, and exit."""
        logger.error(f"Update failed: {error_msg}")
        if should_rollback:
            rollback_from_backup()
        hide_updating_screen()
        report_error(error_msg)
        sys.exit(1)

    # Pull latest code (git repo, not the installed files)
    if not pull_latest(branch):
        fail_update("Failed to pull latest code from git", should_rollback=False)

    # Check if jam_update.py itself changed - if so, re-exec with new version
    # This ensures new install functions (like install_wifi_stability_configs)
    # take effect immediately, not on the next update cycle
    check_and_reexec_if_updated()

    # Ensure venv exists (before backup since we're not backing up venv)
    if not ensure_venv_exists():
        fail_update("Failed to create/verify virtual environment", should_rollback=False)

    # Create backup of current installation before making changes
    # Skip backup for fresh installs (nothing to backup)
    has_backup = False
    if not force_install:
        if not create_backup():
            fail_update("Failed to create backup - aborting update for safety", should_rollback=False)
        has_backup = True

    # From here on, failures should trigger rollback (if we have a backup)

    # Install services
    if not install_services():
        fail_update("Failed to install services", should_rollback=has_backup)

    # Install dependencies
    if not install_dependencies():
        fail_update("Failed to install dependencies", should_rollback=has_backup)

    # Install systemd units
    if not install_systemd_units():
        fail_update("Failed to install systemd units", should_rollback=has_backup)

    # Update version file
    update_version_file(latest_version)

    # Clean up legacy cruft (non-critical, don't fail update for this)
    try:
        cleanup_legacy_cruft()
    except Exception as e:
        logger.warning(f"Legacy cleanup had issues (non-fatal): {e}")

    # Install crontab with essential scheduled tasks (3am reboot, logrotate)
    install_crontab()

    # Install logrotate config
    install_logrotate_config()

    # Install chrony peering config for offline clock sync
    install_chrony_peering_config()

    # Install boot config for proper 4K/HDMI output
    install_boot_config()

    # Install WiFi stability configs (power save off, unlimited retries)
    install_wifi_stability_configs()

    # Install BLE configuration (D-Bus and BlueZ) for pairing-free provisioning
    install_ble_configs()

    # Configure lightdm to hide cursor on desktop
    install_lightdm_cursor_config()

    # Restart services to pick up changes
    restart_services()

    # Update successful - clean up backup
    if has_backup:
        cleanup_backup()

    # Hide updating screen (jam-player-display.service will take over)
    hide_updating_screen()

    logger.info("Update completed successfully!")
    sys.exit(0)


if __name__ == '__main__':
    main()
