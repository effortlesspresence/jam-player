"""
When does a scene play? Stdlib-only schedule logic shared by
jam_player_display.py (the caller) and its tests.

WHAT THE BACKEND SENDS. jam-sphere compiles a row's stored weekly schedule
into CALENDAR-DAY entries before a player ever sees it (packages/common/src/
helpers/rowSchedule.ts, compileRowScheduleForPlayers). Each entry is
{dayOfWeek, startTime "HH:MM" | null, endTime "HH:MM" | null}, and a scene
carries up to two lists of them:

  daysScheduled       At most one entry per weekday. That is all a player
                      built before 2026-11 can read, so when a schedule needs
                      more than one entry a day this list holds the closest
                      single entry instead: an approximation.
  exactDaysScheduled  Present ONLY when one entry per weekday cannot say
                      exactly what plays. Several entries for a weekday; ANY
                      match plays. Two kinds of schedule need it:
                        - a day with several windows of its own
                          ("Mon 8am-12pm + 5pm-10pm");
                        - some windows that run past midnight ("Sat 10pm-2am"
                          then "Sun 10pm-11pm" plays Sunday 00:00-02:00 AND
                          22:00-23:00).

A window that runs past midnight is ALREADY SPLIT at midnight by the backend,
into the evening of the day it starts on and the early morning of the next
day. So nothing here ever has to look at yesterday: every question is "is
there an entry for TODAY's weekday that covers this minute?".

HOW ONE ENTRY IS READ. Identical to jam-player-webos schedule.js
(isSceneScheduledNow / dayEntryMatches) and the browser player; a video wall
can mix player types, so keep the three in step.

  no entries at all            -> the scene always plays
  no entry for today's weekday -> it does not play today
  no start and no end          -> all day
  start <= end                 -> start <= now <= end
  start >  end                 -> now >= start OR now <= end: this calendar
                                  day's early morning and its evening. The
                                  single-entry form of a window repeated every
                                  night, and of "morning and evening".
  start only                   -> from start until midnight
  end only                     -> from midnight until end
  times that cannot be read    -> that entry never matches. Another entry for
                                  the same day still can.

MINUTE GRANULARITY, END MINUTE INCLUSIVE. The backend stores an end minute as
inclusive, and webOS and the browser player have always read it that way: an
end of 21:00 plays through 21:00:59. This player used to compare down to the
second, so it stopped at 21:00:00 -- and a row ending 11:59 followed by one
starting 12:00 left the screen empty for the last minute before noon. With
several windows a day those seams are routine, so the comparison is by whole
minutes here too.

The clock is the device's local time (datetime.now()). jam-heartbeat sets the
system timezone to the Location's, so local time is the store's time.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

# datetime.weekday(): 0 = Monday. The backend's DayOfWeek names.
WEEKDAY_NAMES = ['MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY', 'SUNDAY']


def parse_hhmm_to_minutes(value: Any) -> Optional[int]:
    """
    'HH:MM' -> minutes since midnight, or None when it is empty or not a real
    time of day. Seconds, if present, are ignored: schedules are whole minutes.
    """
    if not value:
        return None
    parts = str(value).split(':')
    if len(parts) < 2:
        return None
    try:
        hours, minutes = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return hours * 60 + minutes


def entry_weekday(entry: Dict[str, Any]) -> Optional[str]:
    """
    The entry's weekday as 'MONDAY'..'SUNDAY'. The API has sent it both as a
    plain string and as {"value": "FRIDAY", "label": "Friday"}.
    """
    day_of_week = entry.get('dayOfWeek')
    if isinstance(day_of_week, dict):
        day_of_week = day_of_week.get('value')
    if not day_of_week:
        return None
    return str(day_of_week).upper()


def entry_covers_minute(entry: Dict[str, Any], minutes_since_midnight: int) -> bool:
    """Does this one calendar-day entry cover this minute of its day?"""
    raw_start = entry.get('startTime')
    raw_end = entry.get('endTime')

    # Decided on the RAW values, before parsing: no times at all means all
    # day, while times that are present but unreadable mean "never".
    if not raw_start and not raw_end:
        return True

    start = parse_hhmm_to_minutes(raw_start)
    end = parse_hhmm_to_minutes(raw_end)
    now = minutes_since_midnight

    if start is not None and end is not None:
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end
    if start is not None:
        return now >= start
    if end is not None:
        return now <= end
    return False


def scene_schedule(scene: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    The entries to evaluate for a scene as scenes_manager wrote it to
    scenes.json: the exact windows when the backend sent them, otherwise the
    one-entry-per-weekday list. A manifest written by older firmware has no
    exact list, and reads exactly as it always did.
    """
    exact = scene.get('exact_days_scheduled')
    if isinstance(exact, list) and exact:
        return exact
    days = scene.get('days_scheduled')
    return days if isinstance(days, list) else []


def is_schedule_live_at(entries: List[Dict[str, Any]], weekday: str,
                        minutes_since_midnight: int) -> bool:
    """
    Is a schedule playing at this weekday and minute? No entries means always.
    EVERY entry for the weekday is tried: with several windows a day, the one
    that matches is often not the first.
    """
    if not entries:
        return True
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry_weekday(entry) != weekday:
            continue
        if entry_covers_minute(entry, minutes_since_midnight):
            return True
    return False


def is_scene_scheduled_at(scene: Dict[str, Any], when: datetime) -> bool:
    """Should this scene be on screen at `when` (device local time)?"""
    return is_schedule_live_at(
        scene_schedule(scene),
        WEEKDAY_NAMES[when.weekday()],
        when.hour * 60 + when.minute,
    )


def schedule_minute(epoch_seconds: float) -> int:
    """
    Which wall-clock minute an instant falls in. Windows start and end on
    whole minutes, so a schedule can only change when this number does: the
    display re-checks once each time it changes, which puts every start and
    end on screen within a moment of the minute turning over, and at the same
    moment on every screen of a wall. (Every timezone in use is a whole number
    of minutes from UTC, so epoch minutes and local minutes turn together.)
    """
    return int(epoch_seconds // 60)
