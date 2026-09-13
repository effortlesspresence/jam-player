"""
Content pipeline integrity: the first tests this pipeline has ever had.

Covers the three audit findings fixed together:
  #12 partial downloads were published forever (stream-to-final-name, reuse
      check "non-empty", no SIGTERM handler) -> now .part + fsync + verify +
      rename, size sidecar, SIGTERM cleanup;
  #13 a failed/partial load was silently lost (backend consumes the update
      flag on read; no retry) -> tri-state result, cleanup skipped on partial,
      main loop owes a refresh;
  #14 the live manifest was deleted BEFORE a fetch on an mtime bump -> a
      manifest with content is now NEVER removed ahead of a fetch (an offline
      player keeps its last content; the atomic swap replaces it when new
      content arrives); only an EMPTY `[]` manifest is cleared, and only online.
Runs on a JAM Player (imports the venv: requests, jam_player.constants);
network, ffprobe and the media directories are mocked / temporary.
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve()
for candidate in (_HERE.parents[1], _HERE.parents[2]):   # /opt/jam/services on a player; src/jam_player in the repo
    sys.path.insert(0, str(candidate))

import scenes_manager_service as sms  # noqa: E402
from common import credentials  # noqa: E402


class FakeResponse:
    def __init__(self, body: bytes, content_length=None, encoding=None, chunk=7):
        self.body, self.chunk = body, chunk
        self.headers = {}
        if content_length is not None:
            self.headers['Content-Length'] = str(content_length)
        if encoding:
            self.headers['Content-Encoding'] = encoding
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def raise_for_status(self): pass
    def iter_content(self, chunk_size):
        for i in range(0, len(self.body), self.chunk):
            yield self.body[i:i + self.chunk]


def _ok_probe(*a, **k):
    return mock.MagicMock(returncode=0, stdout='5.0\n', stderr='')


class _TmpDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.media = root / 'live_media'; self.media.mkdir()
        self.live = root / 'live_scenes'
        self.staged = root / 'staged_scenes'
        self.screen_file = root / 'screen_id.txt'
        self._patches = [
            mock.patch.object(sms, 'LIVE_MEDIA_DIR', self.media),
            mock.patch.object(sms, 'LIVE_SCENES_DIR', self.live),
            mock.patch.object(sms, 'STAGED_SCENES_DIR', self.staged),
            mock.patch.object(sms, 'SCREEN_ID_FILE', self.screen_file),
            mock.patch.object(sms.time, 'sleep'),
        ]
        for p in self._patches: p.start()
    def tearDown(self):
        for p in self._patches: p.stop()
        self.tmp.cleanup()


class DownloadIntegrityTests(_TmpDirs):

    def test_streams_to_part_then_renames_and_records_size(self):
        dest = self.media / 'abc.mp4'
        with mock.patch('requests.get', return_value=FakeResponse(b'x' * 100, content_length=100)), \
             mock.patch.object(sms.subprocess, 'run', side_effect=_ok_probe):
            self.assertTrue(sms.download_media('https://cdn/x.mp4', dest))
        self.assertEqual(dest.read_bytes(), b'x' * 100)
        self.assertFalse(sms._part_path_for(dest).exists(), 'no .part left behind')
        self.assertEqual(sms._read_expected_size(dest), 100)

    def test_truncated_stream_is_rejected_and_existing_file_untouched(self):
        dest = self.media / 'abc.mp4'
        dest.write_bytes(b'GOOD' * 10)                 # a previous good download
        with mock.patch('requests.get', return_value=FakeResponse(b'x' * 60, content_length=100)), \
             mock.patch.object(sms.subprocess, 'run', side_effect=_ok_probe):
            self.assertFalse(sms.download_media('https://cdn/x.mp4', dest))
        self.assertEqual(dest.read_bytes(), b'GOOD' * 10, 'dest_path is never opened for writing')
        self.assertFalse(sms._part_path_for(dest).exists())

    def test_ffprobe_failure_rejects_a_video(self):
        dest = self.media / 'bad.mp4'
        bad = mock.MagicMock(returncode=1, stdout='', stderr='Invalid data')
        with mock.patch('requests.get', return_value=FakeResponse(b'x' * 50, content_length=50)), \
             mock.patch.object(sms.subprocess, 'run', return_value=bad):
            self.assertFalse(sms.download_media('https://cdn/bad.mp4', dest))
        self.assertFalse(dest.exists())

    def test_gzip_encoded_response_does_not_enforce_content_length(self):
        dest = self.media / 'pic.bin'                  # unknown type: size check only
        with mock.patch('requests.get', return_value=FakeResponse(b'x' * 100, content_length=40, encoding='gzip')):
            self.assertTrue(sms.download_media('https://cdn/pic.bin', dest))

    def test_sigterm_handler_unlinks_the_inflight_part(self):
        part = self.media / 'inflight.mp4.part'
        part.write_bytes(b'half')
        sms._inflight_part['path'] = part
        with self.assertRaises(SystemExit):
            sms.handle_terminate(15, None)
        self.assertFalse(part.exists())

    def test_stale_parts_are_swept_at_startup(self):
        (self.media / 'old.mp4.part').write_bytes(b'x')
        (self.media / 'keep.mp4').write_bytes(b'y')
        sms._remove_stale_partials()
        self.assertFalse((self.media / 'old.mp4.part').exists())
        self.assertTrue((self.media / 'keep.mp4').exists())


class ReuseDecisionTests(_TmpDirs):

    def test_matching_sidecar_is_trusted_without_probing(self):
        f = self.media / 'a.mp4'; f.write_bytes(b'x' * 30); sms._write_expected_size(f, 30)
        with mock.patch.object(sms.subprocess, 'run', side_effect=AssertionError('no probe needed')):
            self.assertTrue(sms._existing_media_is_trustworthy(f))

    def test_mismatched_sidecar_means_redownload(self):
        f = self.media / 'a.mp4'; f.write_bytes(b'x' * 30); sms._write_expected_size(f, 100)
        self.assertFalse(sms._existing_media_is_trustworthy(f))

    def test_legacy_file_without_sidecar_is_validated_once_and_adopted(self):
        """A file downloaded by older firmware: probe it; if it passes, write the sidecar."""
        f = self.media / 'legacy.mp4'; f.write_bytes(b'x' * 30)
        with mock.patch.object(sms.subprocess, 'run', side_effect=_ok_probe) as probe:
            self.assertTrue(sms._existing_media_is_trustworthy(f))
        probe.assert_called_once()
        self.assertEqual(sms._read_expected_size(f), 30)

    def test_legacy_truncated_video_is_rejected(self):
        f = self.media / 'trunc.mp4'; f.write_bytes(b'x' * 30)
        bad = mock.MagicMock(returncode=1, stdout='', stderr='moov atom not found')
        with mock.patch.object(sms.subprocess, 'run', return_value=bad):
            self.assertFalse(sms._existing_media_is_trustworthy(f))

    def test_empty_file_is_never_trusted(self):
        f = self.media / 'e.mp4'; f.write_bytes(b'')
        self.assertFalse(sms._existing_media_is_trustworthy(f))


def _scene(i, url):
    return {'id': f's{i}', 'mediaType': {'value': 'CANVAS_IMAGE'}, 'imageUrl': url,
            'videoUrl': None, 'duration': 8, 'daysScheduled': []}


class LoadResultTests(_TmpDirs):

    def setUp(self):
        super().setUp()
        self.screen_file.write_text('screen-A')

    def _fake_download(self, failing_url):
        def dl(url, dest):
            if url == failing_url:
                return False
            dest.write_bytes(b'img'); sms._write_expected_size(dest, 3); return True
        return dl

    def test_fetch_failure_is_failed_and_publishes_nothing(self):
        with mock.patch.object(sms, 'fetch_content', return_value=None):
            self.assertEqual(sms.load_content(), sms.LOAD_FAILED)
        self.assertFalse((self.live / 'scenes.json').exists())

    def test_one_download_failure_is_partial_and_skips_cleanup(self):
        scenes = [_scene(1, 'https://cdn/1.png'), _scene(2, 'https://cdn/2.png')]
        with mock.patch.object(sms, 'fetch_content', return_value=(scenes, 1)), \
             mock.patch.object(sms, 'download_media', side_effect=self._fake_download('https://cdn/2.png')), \
             mock.patch.object(sms, 'cleanup_unused_media') as cleanup:
            self.assertEqual(sms.load_content(), sms.LOAD_PARTIAL)
        cleanup.assert_not_called()
        live = json.loads((self.live / 'scenes.json').read_text())
        self.assertEqual([s['id'] for s in live], ['s1'], 'what could be fetched is published')

    def test_complete_load_cleans_up(self):
        scenes = [_scene(1, 'https://cdn/1.png')]
        with mock.patch.object(sms, 'fetch_content', return_value=(scenes, 1)), \
             mock.patch.object(sms, 'download_media', side_effect=self._fake_download(None)), \
             mock.patch.object(sms, 'cleanup_unused_media') as cleanup:
            self.assertEqual(sms.load_content(), sms.LOAD_COMPLETE)
        cleanup.assert_called_once()

    def test_empty_api_result_is_complete(self):
        with mock.patch.object(sms, 'fetch_content', return_value=([], 1)):
            self.assertEqual(sms.load_content(), sms.LOAD_COMPLETE)
        self.assertEqual(json.loads((self.live / 'scenes.json').read_text()), [])


class OfflinePlayersKeepTheirContentTests(_TmpDirs):
    """Product rule: a reload attempt never takes content off the board. Only an
    EMPTY manifest (the unlinked state) is cleared ahead of a fetch, and only
    while online, so a freshly linked player shows 'Downloading content'."""

    def _live(self, manifest):
        self.live.mkdir()
        (self.live / 'scenes.json').write_text(manifest)

    def test_content_survives_a_relink_while_offline(self):
        self._live('[{"id":"s1"}]')
        self.screen_file.write_text('screen-B')            # different screen than the content
        future = time.time() + 3600
        os.utime(self.screen_file, (future, future))       # and 'newer' than the manifest
        with mock.patch.object(sms, 'device_is_offline', return_value=True):
            sms._clear_empty_live_manifest_before_fetch()
        self.assertTrue((self.live / 'scenes.json').exists(), 'offline players keep their last content')

    def test_content_survives_a_relink_while_online_until_the_swap(self):
        self._live('[{"id":"s1"}]')
        self.screen_file.write_text('screen-B')
        with mock.patch.object(sms, 'device_is_offline', return_value=False):
            sms._clear_empty_live_manifest_before_fetch()
        self.assertTrue((self.live / 'scenes.json').exists(), 'the atomic swap replaces it; nothing is pre-deleted')

    def test_content_survives_an_unlink(self):
        self._live('[{"id":"s1"}]')                          # no screen_id.txt at all
        with mock.patch.object(sms, 'device_is_offline', return_value=False):
            sms._clear_empty_live_manifest_before_fetch()
        self.assertTrue((self.live / 'scenes.json').exists(), 'online, the fetch returns [] and clears it properly')

    def test_empty_manifest_is_cleared_when_online(self):
        self._live('[]')
        with mock.patch.object(sms, 'device_is_offline', return_value=False):
            sms._clear_empty_live_manifest_before_fetch()
        self.assertFalse((self.live / 'scenes.json').exists(), "so the display says 'Downloading content', not 'nothing scheduled'")

    def test_empty_manifest_is_left_alone_when_offline(self):
        self._live('[]')
        with mock.patch.object(sms, 'device_is_offline', return_value=True):
            sms._clear_empty_live_manifest_before_fetch()
        self.assertTrue((self.live / 'scenes.json').exists(), 'no fetch can succeed offline; do not flip the screen')

    def test_corrupt_manifest_is_left_for_recovery(self):
        self._live('{not json')
        with mock.patch.object(sms, 'device_is_offline', return_value=False):
            sms._clear_empty_live_manifest_before_fetch()   # must not raise
        self.assertTrue((self.live / 'scenes.json').exists())


class CleanupKeepsSidecarsTests(_TmpDirs):

    def test_sidecar_follows_its_file_and_stale_parts_die(self):
        (self.media / 'keep.mp4').write_bytes(b'k'); (self.media / 'keep.mp4.size').write_text('1')
        (self.media / 'gone.mp4').write_bytes(b'g'); (self.media / 'gone.mp4.size').write_text('1')
        (self.media / 'half.mp4.part').write_bytes(b'h')
        sms.cleanup_unused_media({'keep.mp4'})
        names = sorted(p.name for p in self.media.iterdir())
        self.assertEqual(names, ['keep.mp4', 'keep.mp4.size'])


class SetScreenIdComparesTests(unittest.TestCase):

    def test_same_value_does_not_rewrite(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'screen_id.txt'; f.write_text('screen-A')
            before = f.stat().st_mtime_ns
            with mock.patch.object(credentials, 'SCREEN_ID_FILE', f), \
                 mock.patch.object(credentials, 'safe_write_text', side_effect=AssertionError('must not write')):
                self.assertTrue(credentials.set_screen_id('screen-A'))
            self.assertEqual(f.stat().st_mtime_ns, before)

    def test_changed_value_is_written(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'screen_id.txt'; f.write_text('screen-A')
            with mock.patch.object(credentials, 'SCREEN_ID_FILE', f), \
                 mock.patch.object(credentials, 'safe_write_text') as write:
                self.assertTrue(credentials.set_screen_id('screen-B'))
            write.assert_called_once_with(f, 'screen-B')


if __name__ == '__main__':
    unittest.main()
