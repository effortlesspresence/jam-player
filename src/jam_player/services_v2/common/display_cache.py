"""
Pre-rendered display screen cache for jam-player-display.service.

Static setup-state screens (AWAITING_NETWORK, AWAITING_REGISTRATION,
AWAITING_SCREEN_LINK, DOWNLOADING_CONTENT) are expensive to render at 4K
(10-12s of PIL work for the mesh gradient alone). Their contents are
deterministic given (mode, width, height, device_uuid, code_commit) --
device_uuid is per-device-constant, so the meaningful cache key is
(mode, width, height, code_commit).

This module lives in `common/` because both jam_player_display.py (runtime
consumer) and jam_update.py (pre-warm at install time) need it. The
display module owns the render-function registry and passes it in -- no
imports from jam_player_display here, so we don't get a circular import.

Architecture (two layers, each works standalone):
  1. Lazy: get_or_render_cached() generates on cache miss, writes to
     /var/cache/jam-player-display/. Cache write failures fall back to
     /tmp -- correctness over optimization.
  2. Pre-warm: prewarm_display_cache() pre-generates common-resolution
     variants after a jam-update so first-display-after-update is
     instant. Pre-warm failures are logged, never block updates.

Invalidation:
  cleanup_stale_cache() removes files whose commit suffix doesn't match
  the current /etc/jam/version.txt. Tracked by a marker file so we only
  scan when the commit actually changed -- avoids redundant work on every
  service start.
"""
import logging
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from common.installed_version import read_installed_version

logger = logging.getLogger(__name__)


# Linux convention for application caches that can be regenerated from
# source on demand. Survives reboots, doesn't survive a wipe of /var/cache
# (which is exactly what we want -- it's a cache, not state).
DISPLAY_CACHE_DIR = Path('/var/cache/jam-player-display')

# Marker file recording the commit hash we last cleaned the cache for.
# Used to skip cleanup work when nothing has changed since last boot.
DISPLAY_CACHE_VERSION_FILE = DISPLAY_CACHE_DIR / '.cache_version'


# Type alias for a render function: takes (width, height, device_uuid)
# and returns a PIL.Image. Pre-warm code accepts any object that
# implements the .save() method PIL.Image provides; we don't import PIL
# here to keep this module lightweight and PIL-optional.
RenderFn = Callable[[int, int, Optional[str]], object]

# Type alias for the registry. Keys are stringified DisplayMode values
# (e.g. "awaiting_registration") -- using strings rather than the enum
# itself avoids a coupling to the jam_player_display module.
RenderRegistry = Dict[str, RenderFn]


def _read_installed_commit_short() -> Optional[str]:
    """
    Return the 12-char commit hash from the installed-version file, or
    None if it isn't available (very early in first-boot before
    jam-update has ever run). None disables caching for that call --
    the lazy path will render to /tmp every time, which is slow but
    correct.

    Delegates to common.installed_version.read_installed_version() so
    that the file path + read semantics live in one place. We then
    truncate to 12 chars for the cache key suffix (shorter filenames,
    still uniquely identifies a commit in practice).
    """
    commit = read_installed_version()
    return commit[:12] if commit else None


def _cache_path(mode_value: str, width: int, height: int, commit_short: str) -> Path:
    """Compute the deterministic cache filename for a given screen render."""
    return DISPLAY_CACHE_DIR / f"{mode_value}_{width}x{height}_{commit_short}.png"


def get_or_render_cached(
    mode_value: str,
    width: int,
    height: int,
    render_fn: RenderFn,
    device_uuid: Optional[str] = None,
) -> Optional[str]:
    """
    Return a filesystem path to a rendered PNG for the given mode/size.

    On cache hit, returns the cached path immediately (no PIL work).
    On cache miss, renders via PIL, saves atomically to the cache
    directory with fsync, then returns that path.

    On any cache write failure (disk full, permission error, /var/cache
    missing and uncreatable, etc.), falls back to writing to /tmp -- so
    the customer always gets a working display even when caching is
    broken.

    Args:
        mode_value: String identifier of the DisplayMode (e.g.
            "awaiting_registration"). Used in cache filename.
        width, height: Display dimensions in pixels.
        render_fn: Callable that produces a PIL.Image. Only invoked on
            cache miss.
        device_uuid: Optional device UUID for footer rendering. Passed
            through to render_fn unchanged.

    Returns:
        Path string suitable for handing to feh, or None if rendering
        itself failed (e.g. PIL not available, render returned None).
    """
    commit_short = _read_installed_commit_short()
    cache_enabled = commit_short is not None

    # Cache hit path
    if cache_enabled:
        cached = _cache_path(mode_value, width, height, commit_short)
        if cached.exists() and cached.stat().st_size > 0:
            return str(cached)

    # Cache miss: render
    img = render_fn(width, height, device_uuid)
    if img is None:
        return None

    # Try to save to cache. Failure is non-fatal -- fall through to /tmp.
    if cache_enabled:
        try:
            DISPLAY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cached = _cache_path(mode_value, width, height, commit_short)
            tmp_path = cached.with_suffix('.tmp.png')
            with open(tmp_path, 'wb') as f:
                img.save(f, 'PNG', optimize=False, compress_level=1)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, 0o644)
            # Atomic rename -- either fully visible to next call, or not
            # at all. No partial-file race.
            tmp_path.rename(cached)
            logger.info(f"Cached rendered screen at {cached}")
            return str(cached)
        except OSError as e:
            logger.warning(
                f"Cache write failed for {mode_value} ({width}x{height}): {e}. "
                f"Falling back to /tmp (uncached)."
            )

    # Fallback: write to /tmp so the customer still sees content even
    # when /var/cache is broken.
    tmp_path = f'/tmp/jam_display_{mode_value}.png'
    try:
        img.save(tmp_path, 'PNG', optimize=False, compress_level=1)
        os.chmod(tmp_path, 0o644)
        return tmp_path
    except OSError as e:
        logger.error(f"Even /tmp write failed for {mode_value}: {e}")
        return None


def cleanup_stale_cache() -> None:
    """
    Remove cached PNGs from old code commits.

    Called once at jam-player-display service startup. Only fires when
    the current commit hash differs from the last-cleaned-for commit
    hash (recorded in DISPLAY_CACHE_VERSION_FILE) -- so subsequent boots
    skip the scan unless the commit actually changed.

    Files matching the current commit are preserved; everything else is
    deleted. Unknown filename formats are left alone -- we don't delete
    files we don't recognize.
    """
    commit_short = _read_installed_commit_short()
    if commit_short is None:
        # No version info -> can't safely identify stale entries. Skip.
        return

    if not DISPLAY_CACHE_DIR.exists():
        return

    # Has the cache already been cleaned for this commit?
    try:
        last_cleaned = DISPLAY_CACHE_VERSION_FILE.read_text().strip()
    except OSError:
        last_cleaned = ""

    if last_cleaned == commit_short:
        # Already cleaned for this version, nothing to do.
        return

    logger.info(
        f"Cleaning stale display cache (last cleaned for "
        f"'{last_cleaned}', current commit '{commit_short}')..."
    )

    removed = 0
    kept = 0
    for cache_file in DISPLAY_CACHE_DIR.glob('*.png'):
        # Filenames look like: awaiting_registration_3840x2160_a06adedd9255.png
        # The commit_short is the last underscore-delimited segment before .png
        stem = cache_file.stem  # strips .png
        parts = stem.rsplit('_', 1)
        if len(parts) != 2:
            # Unknown format -- leave it alone.
            continue
        file_commit = parts[1]
        if file_commit == commit_short:
            kept += 1
            continue
        try:
            cache_file.unlink()
            removed += 1
        except OSError as e:
            logger.warning(f"Could not remove stale cache file {cache_file}: {e}")

    # Also clean up any .tmp.png orphans from interrupted atomic-rename writes.
    for tmp_file in DISPLAY_CACHE_DIR.glob('*.tmp.png'):
        try:
            tmp_file.unlink()
        except OSError:
            pass

    logger.info(
        f"Display cache cleanup: removed {removed} stale, kept {kept} current"
    )

    # Record that we've cleaned for this commit. Subsequent boots will
    # short-circuit this function unless the commit changes again.
    try:
        DISPLAY_CACHE_VERSION_FILE.write_text(commit_short)
    except OSError as e:
        logger.warning(f"Could not record cache version marker: {e}")


# Resolutions to pre-warm. Covers ~99% of fielded JP displays:
#   - 1920x1080: standard menu boards
#   - 3840x2160: 4K TVs (Fortune 100 customer requirement)
# Uncommon resolutions (1366x768, 2560x1440, etc.) fall through to
# lazy generation -- one slow render the first time the display is
# attached, then cached.
PREWARM_RESOLUTIONS: List[Tuple[int, int]] = [
    (1920, 1080),
    (3840, 2160),
]


def prewarm_display_cache(
    registry: RenderRegistry,
    device_uuid: Optional[str] = None,
) -> dict:
    """
    Pre-render every screen in `registry` at every PREWARM_RESOLUTIONS
    combination so the first post-update display is instant for the
    customer.

    Called by jam-update.service after a successful install. Failures
    are logged and counted; this function never raises.

    Args:
        registry: {mode_value: render_fn} mapping. Caller (typically
            jam_player_display) owns the registry definition.
        device_uuid: Per-device UUID. None is fine -- the render
            functions handle it.

    Returns:
        dict with counts: {'rendered': int, 'skipped': int, 'failed': int}
        'skipped' = cache hit (no work needed). Always returns.
    """
    summary = {'rendered': 0, 'skipped': 0, 'failed': 0}

    commit_short = _read_installed_commit_short()
    if commit_short is None:
        logger.warning("Pre-warm skipped: /etc/jam/version.txt not available")
        return summary

    for mode_value, render_fn in registry.items():
        for width, height in PREWARM_RESOLUTIONS:
            cached = _cache_path(mode_value, width, height, commit_short)
            if cached.exists() and cached.stat().st_size > 0:
                summary['skipped'] += 1
                continue
            try:
                path = get_or_render_cached(
                    mode_value, width, height, render_fn, device_uuid
                )
                if path is not None and Path(path).exists():
                    summary['rendered'] += 1
                else:
                    summary['failed'] += 1
            except Exception as e:
                logger.warning(
                    f"Pre-warm failed for {mode_value} at {width}x{height}: {e}"
                )
                summary['failed'] += 1

    logger.info(
        f"Display cache pre-warm complete: "
        f"rendered={summary['rendered']}, skipped={summary['skipped']}, "
        f"failed={summary['failed']}"
    )
    return summary
