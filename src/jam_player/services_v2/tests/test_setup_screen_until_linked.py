"""
A registered player with no screen linked shows the SAME screen as a player
that is not registered yet.

Until 2026-11 it had a screen of its own: "Registered! ... Link this JAM
Player to a screen using the JAM Player Setup app or the web app." Customers
read it as a finished state with a new, unexplained task and stalled on it.
Picking the screen is simply the next step of the setup they are already
doing in the app, so the TV now keeps saying "Almost there. Set up this JAM
Player in the JAM Player Setup app on your phone." until a screen is linked.
The separate display mode and its screen are gone.

Runs anywhere the display module imports (it needs the jam_player package on
the path, as on a player).
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jam_player_display as disp  # noqa: E402

Mode = disp.DisplayMode


def _mode(online, registered, screen_id, content=None, outlet_ok=True):
    """What the display decides for one combination of the player's state."""
    manager = object.__new__(disp.JamPlayerDisplayManager)
    content = content or Mode.DOWNLOADING_CONTENT
    with mock.patch.object(disp, 'is_outlet_operational', return_value=outlet_ok), \
         mock.patch.object(disp, 'is_internet_verified', return_value=online), \
         mock.patch.object(disp, 'is_device_registered', return_value=registered), \
         mock.patch.object(disp, 'get_screen_id', return_value=screen_id), \
         mock.patch.object(disp.JamPlayerDisplayManager, '_get_content_display_mode', return_value=content):
        return manager.determine_display_mode()


class OneSetupScreenUntilAScreenIsLinked(unittest.TestCase):

    def test_registered_without_a_screen_shows_the_setup_screen(self):
        self.assertEqual(_mode(online=True, registered=True, screen_id=None), Mode.AWAITING_REGISTRATION)
        self.assertEqual(_mode(online=True, registered=True, screen_id=''), Mode.AWAITING_REGISTRATION)

    def test_it_is_the_same_mode_as_an_unregistered_player(self):
        unregistered = _mode(online=True, registered=False, screen_id=None)
        registered_unlinked = _mode(online=True, registered=True, screen_id=None)
        self.assertEqual(unregistered, Mode.AWAITING_REGISTRATION)
        self.assertEqual(registered_unlinked, unregistered)

    def test_a_player_that_was_unlinked_later_goes_back_to_it(self):
        # heartbeat / SET_SCREEN_ID delete screen_id.txt on unlink; with no
        # content left to play this is the same state.
        self.assertEqual(_mode(online=True, registered=True, screen_id=None,
                               content=Mode.NO_ACTIVE_SCENES), Mode.AWAITING_REGISTRATION)

    def test_once_a_screen_is_linked_it_moves_on(self):
        self.assertEqual(_mode(online=True, registered=True, screen_id='scr-1',
                               content=Mode.DOWNLOADING_CONTENT), Mode.DOWNLOADING_CONTENT)
        self.assertEqual(_mode(online=True, registered=True, screen_id='scr-1',
                               content=Mode.NO_ACTIVE_SCENES), Mode.NO_ACTIVE_SCENES)


class TheRestOfTheLadderIsUnchanged(unittest.TestCase):

    def test_offline_still_asks_for_wifi_first(self):
        for registered in (False, True):
            self.assertEqual(_mode(online=False, registered=registered, screen_id=None), Mode.AWAITING_NETWORK)

    def test_playable_content_still_wins_over_every_setup_screen(self):
        for online in (False, True):
            for registered in (False, True):
                self.assertEqual(_mode(online=online, registered=registered, screen_id=None,
                                       content=Mode.PLAYING_CONTENT), Mode.PLAYING_CONTENT)

    def test_an_inactive_outlet_still_overrides_everything(self):
        self.assertEqual(_mode(online=True, registered=True, screen_id=None, outlet_ok=False),
                         Mode.OUTLET_INACTIVE)


class TheOldScreenIsGone(unittest.TestCase):

    def test_there_is_no_separate_mode_for_it(self):
        self.assertNotIn('AWAITING_SCREEN_LINK', Mode.__members__)
        self.assertNotIn('awaiting_screen_link', [m.value for m in Mode])

    def test_its_screen_is_not_drawn_or_cached_any_more(self):
        self.assertFalse(hasattr(disp, 'create_awaiting_screen_link_screen'))
        # Comments may still quote the old words (to say why it went); no
        # line of code may draw them.
        code = [line for line in Path(disp.__file__).read_text().split('"""', 2)[2].split('\n')
                if not line.strip().startswith('#')]
        for words in ('Link this JAM Player to a screen', 'Registered!'):
            self.assertEqual([line for line in code if words in line], [], words)

    def test_the_setup_screen_keeps_its_cache_name(self):
        # Pre-rendered images are stored under the mode's value; renaming it
        # would make every player re-render the screen.
        self.assertEqual(Mode.AWAITING_REGISTRATION.value, 'awaiting_registration')


if __name__ == '__main__':
    unittest.main()
