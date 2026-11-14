"""
When does a scene play? The stdlib schedule logic behind the display's
day/time filter (common.schedule), on a laptop or a player.

The first block mirrors jam-player-webos test/schedule.test.js case for case:
a video wall can mix Pi and webOS players, so the two must read every entry
the same way. The schedule examples further down are what jam-sphere's
compileRowScheduleForPlayers (packages/common/src/helpers/rowSchedule.ts)
sends for the schedules named in each test, worked out by hand from its rules.
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import schedule as sch  # noqa: E402

# 2024-01-01 was a Monday, so day N of that week is a known weekday.
_DAY_OF_JANUARY_2024 = {
    'MONDAY': 1, 'TUESDAY': 2, 'WEDNESDAY': 3, 'THURSDAY': 4,
    'FRIDAY': 5, 'SATURDAY': 6, 'SUNDAY': 7,
}


def at(weekday, hhmm, second=0):
    """A local datetime on that weekday at HH:MM[:second]."""
    hours, minutes = (int(part) for part in hhmm.split(':'))
    return datetime(2024, 1, _DAY_OF_JANUARY_2024[weekday], hours, minutes, second)


def day(weekday, start, end):
    """One entry the way the API sends it."""
    return {'dayOfWeek': {'value': weekday, 'label': weekday.title()}, 'startTime': start, 'endTime': end}


def scene(days, exact=None):
    """A scene the way scenes_manager writes it to scenes.json."""
    record = {'id': 's1', 'days_scheduled': days}
    if exact is not None:
        record['exact_days_scheduled'] = exact
    return record


def plays(days, when, exact=None):
    return sch.is_scene_scheduled_at(scene(days, exact), when)


MON10 = at('MONDAY', '10:00')


class OneEntryPerDayTests(unittest.TestCase):
    """The one-entry-per-weekday list every player has always read."""

    def test_no_schedule_always_plays(self):
        self.assertTrue(plays([], MON10))
        self.assertTrue(sch.is_scene_scheduled_at({'id': 's1'}, MON10))
        self.assertTrue(sch.is_scene_scheduled_at({'id': 's1', 'days_scheduled': None}, MON10))

    def test_a_weekday_that_is_not_listed_does_not_play(self):
        self.assertFalse(plays([day('TUESDAY', None, None)], MON10))

    def test_a_listed_day_with_no_times_plays_all_day(self):
        self.assertTrue(plays([day('MONDAY', None, None)], MON10))
        self.assertTrue(plays([day('MONDAY', None, None)], at('MONDAY', '00:00')))
        self.assertTrue(plays([day('MONDAY', None, None)], at('MONDAY', '23:59', 59)))

    def test_a_window_inside_its_start_and_end(self):
        self.assertTrue(plays([day('MONDAY', '09:00', '17:00')], MON10))
        self.assertTrue(plays([day('MONDAY', '10:00', '17:00')], MON10), 'the start minute plays')
        self.assertFalse(plays([day('MONDAY', '11:00', '17:00')], MON10))

    def test_start_later_than_end_is_this_days_early_morning_and_evening(self):
        nightly = [day('MONDAY', '22:00', '02:00')]
        self.assertTrue(plays(nightly, at('MONDAY', '23:30')))
        self.assertTrue(plays(nightly, at('MONDAY', '01:00')))
        self.assertFalse(plays(nightly, MON10))

    def test_start_only_runs_to_midnight_and_end_only_runs_from_it(self):
        self.assertTrue(plays([day('MONDAY', '09:00', None)], MON10))
        self.assertFalse(plays([day('MONDAY', '11:00', None)], MON10))
        self.assertTrue(plays([day('MONDAY', '09:00', None)], at('MONDAY', '23:59', 59)))
        self.assertTrue(plays([day('MONDAY', None, '11:00')], MON10))
        self.assertFalse(plays([day('MONDAY', None, '09:00')], MON10))

    def test_the_weekday_may_be_a_plain_string_in_any_case(self):
        self.assertTrue(plays([{'dayOfWeek': 'MONDAY', 'startTime': None, 'endTime': None}], MON10))
        self.assertTrue(plays([{'dayOfWeek': 'monday', 'startTime': None, 'endTime': None}], MON10))

    def test_times_that_cannot_be_read_never_play(self):
        self.assertFalse(plays([day('MONDAY', '25:99', 'abc')], MON10))

    def test_parsing(self):
        self.assertEqual(sch.parse_hhmm_to_minutes('14:30'), 870)
        self.assertEqual(sch.parse_hhmm_to_minutes('00:00'), 0)
        self.assertEqual(sch.parse_hhmm_to_minutes('23:59'), 1439)
        self.assertEqual(sch.parse_hhmm_to_minutes('14:30:45'), 870, 'seconds are ignored')
        for bad in (None, '', '25:00', '12:60', '-1:00', 'abc', '12', 'ab:cd'):
            self.assertIsNone(sch.parse_hhmm_to_minutes(bad), bad)


class WholeMinuteTests(unittest.TestCase):
    """An end minute is inclusive: the backend's rule, and webOS's reading."""

    def test_an_end_minute_plays_through_its_last_second(self):
        lunch = [day('MONDAY', '11:00', '14:30')]
        self.assertTrue(plays(lunch, at('MONDAY', '14:30', 0)))
        self.assertTrue(plays(lunch, at('MONDAY', '14:30', 59)),
                        'older firmware compared to the second and stopped at 14:30:00')
        self.assertFalse(plays(lunch, at('MONDAY', '14:31', 0)))

    def test_a_start_minute_plays_from_its_first_second(self):
        lunch = [day('MONDAY', '11:00', '14:30')]
        self.assertFalse(plays(lunch, at('MONDAY', '10:59', 59)))
        self.assertTrue(plays(lunch, at('MONDAY', '11:00', 0)))

    def test_back_to_back_rows_leave_no_gap(self):
        # Dayparts save the minute before the next one starts, so that two of
        # them never share a minute: breakfast to 11:59, lunch from 12:00.
        breakfast = [day('MONDAY', '06:00', '11:59')]
        lunch = [day('MONDAY', '12:00', '16:59')]
        for second in (0, 30, 59):
            moment = at('MONDAY', '11:59', second)
            self.assertTrue(plays(breakfast, moment), f'breakfast still up at 11:59:{second:02d}')
            self.assertFalse(plays(lunch, moment))
        noon = at('MONDAY', '12:00', 0)
        self.assertFalse(plays(breakfast, noon))
        self.assertTrue(plays(lunch, noon))

    def test_the_schedule_is_rechecked_when_the_minute_turns(self):
        base = 1_700_000_040.0   # a whole minute
        self.assertEqual(base % 60, 0)
        self.assertEqual(sch.schedule_minute(base), sch.schedule_minute(base + 59.999))
        self.assertEqual(sch.schedule_minute(base) + 1, sch.schedule_minute(base + 60))
        self.assertEqual(sch.schedule_minute(base) - 1, sch.schedule_minute(base - 0.001))


class SeveralEntriesADayTests(unittest.TestCase):
    """
    Sat 22:00 -> 02:00 followed by Sun 22:00-23:00. Sunday holds the end of
    Saturday night plus its own evening, which one entry per weekday cannot
    say. daysScheduled is the closest single entry for older firmware;
    exactDaysScheduled is the truth. (Same data as the webOS suite.)
    """
    EXACT = [day('SATURDAY', '22:00', None), day('SUNDAY', '00:00', '02:00'), day('SUNDAY', '22:00', '23:00')]
    APPROX = [day('SATURDAY', '22:00', None), day('SUNDAY', '22:00', '02:00')]

    def live(self, weekday, hhmm):
        return sch.is_schedule_live_at(self.EXACT, weekday, sch.parse_hhmm_to_minutes(hhmm))

    def test_the_first_window_of_the_day_matches(self):
        self.assertTrue(self.live('SUNDAY', '01:00'))

    def test_a_later_window_of_the_day_matches_too(self):
        self.assertTrue(self.live('SUNDAY', '22:30'), 'not the first entry only')

    def test_between_the_windows_does_not_play(self):
        self.assertFalse(self.live('SUNDAY', '12:00'))

    def test_end_minutes_are_inclusive(self):
        self.assertTrue(self.live('SUNDAY', '02:00'))
        self.assertFalse(self.live('SUNDAY', '02:01'))
        self.assertTrue(self.live('SUNDAY', '23:00'))
        self.assertFalse(self.live('SUNDAY', '23:01'))

    def test_another_weekday_does_not_play(self):
        self.assertFalse(self.live('MONDAY', '01:00'))

    def test_an_entry_that_cannot_be_read_does_not_hide_a_good_one(self):
        entries = [day('MONDAY', 'abc', 'def'), day('MONDAY', '09:00', '11:00')]
        self.assertTrue(sch.is_schedule_live_at(entries, 'MONDAY', 600))

    def test_the_exact_windows_are_preferred_over_the_approximation(self):
        # 23:30 on Sunday: the single-entry approximation runs the evening on
        # to midnight (59 minutes too long); the exact windows stop at 23:00.
        late_sunday = at('SUNDAY', '23:30')
        self.assertTrue(plays(self.APPROX, late_sunday), 'what older firmware plays')
        self.assertFalse(plays(self.APPROX, late_sunday, exact=self.EXACT))
        self.assertTrue(plays(self.APPROX, at('SUNDAY', '22:30'), exact=self.EXACT))
        self.assertTrue(plays(self.APPROX, at('SATURDAY', '23:00'), exact=self.EXACT))
        self.assertFalse(plays(self.APPROX, at('SATURDAY', '21:00'), exact=self.EXACT))

    def test_a_missing_or_empty_exact_list_falls_back_to_days_scheduled(self):
        late_sunday = at('SUNDAY', '23:30')
        self.assertTrue(plays(self.APPROX, late_sunday, exact=[]))
        self.assertTrue(plays(self.APPROX, late_sunday, exact=None))
        self.assertTrue(sch.is_scene_scheduled_at(
            {'days_scheduled': self.APPROX, 'exact_days_scheduled': 'not a list'}, late_sunday))

    def test_entries_that_are_not_objects_are_skipped(self):
        entries = ['MONDAY', None, 7, day('MONDAY', '09:00', '11:00')]
        self.assertTrue(sch.is_schedule_live_at(entries, 'MONDAY', 600))
        self.assertFalse(sch.is_schedule_live_at(['MONDAY', None], 'MONDAY', 600))


class SchedulesPeopleSetTests(unittest.TestCase):
    """The schedules J20Q-583 names, as the backend compiles them."""

    def test_several_windows_in_one_day(self):
        # "Monday 8am-12pm + 5pm-10pm". Older firmware gets the longer window.
        approx = [day('MONDAY', '17:00', '22:00')]
        exact = [day('MONDAY', '08:00', '12:00'), day('MONDAY', '17:00', '22:00')]
        expected = {
            '07:59': False, '08:00': True, '09:00': True, '12:00': True, '12:01': False,
            '14:00': False, '16:59': False, '17:00': True, '22:00': True, '22:01': False,
        }
        for hhmm, want in expected.items():
            self.assertEqual(plays(approx, at('MONDAY', hhmm), exact=exact), want, f'Monday {hhmm}')
        self.assertFalse(plays(approx, at('MONDAY', '09:00')), 'the approximation alone misses the morning')
        self.assertFalse(plays(approx, at('TUESDAY', '09:00'), exact=exact))

    def test_a_window_past_midnight_mixed_with_a_morning_window(self):
        # "Monday 8am-12pm + 5pm-2am". The night is split at midnight: Monday
        # from 17:00 on, and Tuesday until 02:00.
        approx = [day('MONDAY', '17:00', None), day('TUESDAY', '00:00', '02:00')]
        exact = [day('MONDAY', '08:00', '12:00'), day('MONDAY', '17:00', None), day('TUESDAY', '00:00', '02:00')]
        expected = [
            ('MONDAY', '07:59', False), ('MONDAY', '09:00', True), ('MONDAY', '13:00', False),
            ('MONDAY', '17:00', True), ('MONDAY', '23:59', True),
            ('TUESDAY', '00:00', True), ('TUESDAY', '00:30', True), ('TUESDAY', '02:00', True),
            ('TUESDAY', '02:01', False), ('TUESDAY', '09:00', False), ('TUESDAY', '17:30', False),
            ('SUNDAY', '23:30', False), ('MONDAY', '01:00', False),
        ]
        for weekday, hhmm, want in expected:
            self.assertEqual(plays(approx, at(weekday, hhmm), exact=exact), want, f'{weekday} {hhmm}')
        self.assertTrue(plays(approx, at('MONDAY', '23:59', 59), exact=exact), 'up to the last second before midnight')

    def test_one_window_past_midnight_on_one_day(self):
        # "Friday 5pm-2am". One entry per weekday says it exactly, so the
        # backend sends no exact list: Friday from 17:00, Saturday to 02:00.
        days = [day('FRIDAY', '17:00', None), day('SATURDAY', '00:00', '02:00')]
        expected = [
            ('FRIDAY', '16:59', False), ('FRIDAY', '17:00', True), ('FRIDAY', '23:59', True),
            ('SATURDAY', '01:30', True), ('SATURDAY', '02:00', True), ('SATURDAY', '02:01', False),
            ('SATURDAY', '18:00', False), ('FRIDAY', '01:00', False),
        ]
        for weekday, hhmm, want in expected:
            self.assertEqual(plays(days, at(weekday, hhmm)), want, f'{weekday} {hhmm}')

    def test_the_same_window_past_midnight_every_night(self):
        # "5pm-2am" all week compiles to one wrapped entry per weekday.
        days = [day(weekday, '17:00', '02:00') for weekday in sch.WEEKDAY_NAMES]
        for weekday in sch.WEEKDAY_NAMES:
            self.assertTrue(plays(days, at(weekday, '01:00')), weekday)
            self.assertTrue(plays(days, at(weekday, '18:00')), weekday)
            self.assertFalse(plays(days, at(weekday, '03:00')), weekday)
            self.assertFalse(plays(days, at(weekday, '12:00')), weekday)


class WeekdayMappingTests(unittest.TestCase):

    def test_every_weekday_maps_to_its_own_name(self):
        for weekday in sch.WEEKDAY_NAMES:
            only_that_day = [day(weekday, None, None)]
            for other in sch.WEEKDAY_NAMES:
                self.assertEqual(plays(only_that_day, at(other, '12:00')), other == weekday, f'{weekday} vs {other}')


if __name__ == '__main__':
    unittest.main()
