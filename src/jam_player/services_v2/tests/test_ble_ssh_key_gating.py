"""
The SSH private key in the BLE Device Info characteristic is a REGISTRATION
credential: the app forwards it to the backend when registering. After
registration the backend already holds it, so it must stay off the air.

The field itself must ALWAYS be present (both fielded apps declare it as a
required string); only its VALUE is blanked.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jam_ble_provisioning as ble  # noqa: E402

PRIVATE_KEY = '-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----'
REQUIRED_FIELDS = {
    'deviceUuid', 'bleDeviceName', 'jpImageId', 'softwareVersion',
    'apiSigningPublicKey', 'sshPublicKey', 'sshPrivateKey',
    'isConnected', 'isAnnounced', 'isRegistered',
}


def _emit_device_info(registered):
    """Run _send_device_info_chunked on a bare characteristic; capture the JSON."""
    chrc = object.__new__(ble.DeviceInfoCharacteristic)
    chrc.CHUNK_SIZE = 4096  # one chunk keeps the capture simple
    chrc.PropertiesChanged = mock.MagicMock()
    captured = {}
    real_dumps = json.dumps

    def capture(obj, *a, **k):
        captured['info'] = obj
        return real_dumps(obj, *a, **k)

    with mock.patch.object(ble, 'get_device_uuid', return_value='uuid-1234'), \
         mock.patch.object(ble, 'get_device_name', return_value='JAM-PLAYER-D1234'), \
         mock.patch.object(ble, 'get_jp_image_id', return_value='img'), \
         mock.patch.object(ble, 'get_api_signing_public_key', return_value='signpub'), \
         mock.patch.object(ble, 'get_ssh_public_key', return_value='sshpub'), \
         mock.patch.object(ble, 'get_ssh_private_key', return_value=PRIVATE_KEY) as get_priv, \
         mock.patch.object(ble, 'is_device_announced', return_value=True), \
         mock.patch.object(ble, 'is_device_registered', return_value=registered), \
         mock.patch.object(ble.json, 'dumps', side_effect=capture):
        chrc._send_device_info_chunked()
    return captured['info'], get_priv


class SshKeyGatingTests(unittest.TestCase):
    def test_unregistered_device_exposes_the_key_for_registration(self):
        info, _ = _emit_device_info(registered=False)
        self.assertEqual(info['sshPrivateKey'], PRIVATE_KEY)
        self.assertFalse(info['isRegistered'])

    def test_announced_but_unregistered_still_exposes_the_key(self):
        """Announce != registered; the app still needs the key to register."""
        info, _ = _emit_device_info(registered=False)
        self.assertTrue(info['isAnnounced'])
        self.assertEqual(info['sshPrivateKey'], PRIVATE_KEY)

    def test_registered_device_blanks_the_key(self):
        info, get_priv = _emit_device_info(registered=True)
        self.assertEqual(info['sshPrivateKey'], '')
        self.assertTrue(info['isRegistered'])
        # Not even read from disk once registered.
        get_priv.assert_not_called()

    def test_field_is_always_present_for_fielded_app_decoders(self):
        for registered in (False, True):
            info, _ = _emit_device_info(registered=registered)
            self.assertEqual(set(info.keys()), REQUIRED_FIELDS,
                             f'schema drift with registered={registered}')
            self.assertIsInstance(info['sshPrivateKey'], str)

    def test_public_key_is_unaffected_by_registration(self):
        for registered in (False, True):
            info, _ = _emit_device_info(registered=registered)
            self.assertEqual(info['sshPublicKey'], 'sshpub')


if __name__ == '__main__':
    unittest.main()
