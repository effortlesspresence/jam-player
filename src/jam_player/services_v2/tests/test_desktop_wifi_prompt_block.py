"""
A WiFi password prompt must never appear on the customer's TV.

Found on the bench on 2026-11-15: a fielded (7440d2d) player was given a wrong
WiFi password, kept the profile, and after a reboot showed the desktop's
"Authentication required by Wi-Fi network" dialog over the JAM screen. The
dialog comes from the Pi OS desktop's network applet, which registers with
NetworkManager as a "secret agent" and is asked for a new password whenever a
saved profile fails to authenticate. Discarding failed profiles (see
test_failed_wifi_profile_discard) removes one cause, but a profile that used to
work can stop working too -- the customer changes the password at the router.

The fix is a D-Bus policy that lets only root register an agent, installed by
jam-update. These tests pin two things:

  * the policy file denies exactly that one interface and nothing wider (a
    broader deny would cut the player off from NetworkManager altogether);
  * the updater installs it, only when it changed, and never installs a
    damaged copy (a broken file in /etc/dbus-1/system.d is not worth risking).

Whether dbus-daemon then really refuses the applet can only be seen on a
device: BENCH_TESTS.md, cases A6 and A7.
"""
import inspect
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jam_update  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[4]
POLICY = REPO_ROOT / 'etc' / 'dbus-1' / 'system.d' / 'jam-no-desktop-wifi-prompt.conf'

# Read before any test redirects it.
INSTALLED_PATH = jam_update._WIFI_PROMPT_BLOCK_DEST

NM = 'org.freedesktop.NetworkManager'
AGENT_MANAGER = 'org.freedesktop.NetworkManager.AgentManager'


class _Rule(dict):
    """One <allow .../> or <deny .../>: its attributes, plus `kind`."""


class _Policy:
    def __init__(self, attrs, body):
        self.attrs = attrs
        self.rules = []
        for kind, raw in re.findall(r'<(allow|deny)\b([^>]*)/>', body):
            rule = _Rule(re.findall(r'([\w-]+)="([^"]*)"', raw))
            rule.kind = kind
            self.rules.append(rule)

    def get(self, name):
        return self.attrs.get(name)

    def of(self, kind):
        return [dict(rule) for rule in self.rules if rule.kind == kind]


def _policies():
    """
    The file's <policy> blocks, read with a regex: it is ours, it is small,
    and this keeps the suite free of an XML parser (as the updater is).
    """
    text = re.sub(r'<!--.*?-->', '', POLICY.read_text(encoding='utf-8'), flags=re.S)
    return [
        _Policy(dict(re.findall(r'([\w-]+)="([^"]*)"', attrs)), body)
        for attrs, body in re.findall(r'<policy\b([^>]*)>(.*?)</policy>', text, flags=re.S)
    ]


class PolicyFileTests(unittest.TestCase):

    RULE = {'send_destination': NM, 'send_interface': AGENT_MANAGER}

    def test_it_is_a_complete_busconfig(self):
        self.assertTrue(jam_update._is_complete_busconfig(POLICY))
        self.assertEqual(len(_policies()), 3)

    def test_it_is_wellformed_xml(self):
        try:
            import xml.etree.ElementTree as ET
            root = ET.parse(str(POLICY)).getroot()
        except ImportError:
            self.skipTest('no XML parser in this Python')
        self.assertEqual(root.tag, 'busconfig')
        self.assertEqual(len(root.findall('policy')), 3)

    def test_everyone_is_denied_agent_registration_by_default(self):
        default = [p for p in _policies() if p.get('context') == 'default']
        self.assertEqual(len(default), 1)
        self.assertEqual(default[0].of('deny'), [self.RULE])
        self.assertEqual(default[0].of('allow'), [])

    def test_the_desktop_user_is_denied_by_name(self):
        # A user rule is applied after any group rule, so a group that some
        # package allows cannot hand the applet its registration back.
        desktop = [p for p in _policies() if p.get('user') == 'comitup']
        self.assertEqual(len(desktop), 1)
        self.assertEqual(desktop[0].of('deny'), [self.RULE])
        self.assertEqual(desktop[0].of('allow'), [])

    def test_root_keeps_it(self):
        # nmcli, run as root by the JAM services, registers an agent of its own.
        root_policy = [p for p in _policies() if p.get('user') == 'root']
        self.assertEqual(len(root_policy), 1)
        self.assertEqual(root_policy[0].of('deny'), [])
        self.assertEqual(root_policy[0].of('allow'), [self.RULE])

    def test_no_rule_is_wider_than_the_one_interface(self):
        # Every deny names the AgentManager interface. A deny on the
        # destination alone would stop every non-root NetworkManager call.
        for policy in _policies():
            self.assertNotEqual(policy.get('context'), 'mandatory',
                                'a mandatory rule would bind root as well')
            for deny in policy.of('deny'):
                self.assertEqual(deny, self.RULE)


class InstallTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src_root = root / 'etc'
        self.src = self.src_root / 'dbus-1' / 'system.d' / jam_update._WIFI_PROMPT_BLOCK_NAME
        self.src.parent.mkdir(parents=True)
        self.src.write_bytes(POLICY.read_bytes())
        self.dest = root / 'dest' / jam_update._WIFI_PROMPT_BLOCK_NAME
        self.patches = [
            mock.patch.object(jam_update, 'ETC_SRC', self.src_root),
            mock.patch.object(jam_update, '_WIFI_PROMPT_BLOCK_DEST', self.dest),
            mock.patch.object(jam_update.os, 'chown'),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_it_goes_where_dbus_reads_it(self):
        self.assertEqual(INSTALLED_PATH, Path('/etc/dbus-1/system.d/jam-no-desktop-wifi-prompt.conf'))
        # dbus-daemon only reads *.conf from that directory.
        self.assertTrue(POLICY.name.endswith('.conf'))
        self.assertEqual(POLICY.name, jam_update._WIFI_PROMPT_BLOCK_NAME)

    def test_first_install_writes_the_file_byte_for_byte(self):
        self.assertTrue(jam_update.install_desktop_wifi_prompt_block())
        self.assertEqual(self.dest.read_bytes(), POLICY.read_bytes())

    def test_an_unchanged_file_is_not_rewritten(self):
        jam_update.install_desktop_wifi_prompt_block()
        before = self.dest.stat().st_mtime_ns
        self.assertFalse(jam_update.install_desktop_wifi_prompt_block())
        self.assertEqual(self.dest.stat().st_mtime_ns, before)

    def test_a_changed_file_replaces_the_installed_one(self):
        jam_update.install_desktop_wifi_prompt_block()
        self.src.write_text(self.src.read_text().replace('</busconfig>', '<!-- v2 --></busconfig>'))
        self.assertTrue(jam_update.install_desktop_wifi_prompt_block())
        self.assertIn('v2', self.dest.read_text())

    def test_a_damaged_source_is_never_installed(self):
        whole = POLICY.read_bytes()
        for damaged in (b'', whole[:200], whole[:len(whole) // 2], whole.rstrip()[:-3], b'<notbusconfig/>\n'):
            self.src.write_bytes(damaged)
            self.assertFalse(jam_update.install_desktop_wifi_prompt_block(), damaged[:20])
            self.assertFalse(self.dest.exists(), 'nothing may be written from a damaged source')

    def test_a_damaged_source_leaves_a_good_installed_copy_alone(self):
        jam_update.install_desktop_wifi_prompt_block()
        self.src.write_bytes(b'')
        self.assertFalse(jam_update.install_desktop_wifi_prompt_block())
        self.assertEqual(self.dest.read_bytes(), POLICY.read_bytes())

    def test_a_missing_source_is_not_an_error(self):
        self.src.unlink()
        self.assertFalse(jam_update.install_desktop_wifi_prompt_block())
        self.assertFalse(self.dest.exists())

    def test_it_does_not_restart_anything(self):
        # Restarting NetworkManager or D-Bus mid-update would drop the link
        # the update is running over.
        with mock.patch.object(jam_update, 'run_command', side_effect=AssertionError('no commands here')):
            jam_update.install_desktop_wifi_prompt_block()

    def test_every_update_installs_it(self):
        self.assertIn('install_desktop_wifi_prompt_block()', inspect.getsource(jam_update.main))


if __name__ == '__main__':
    unittest.main()
