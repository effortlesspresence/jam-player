"""
Every non-content screen ends in the device-identity block (Device ID, setup
network, UUID, MACs). In 2026-09 that block grew by two MAC lines and the setup
screen's "Get ready to JAM." tagline landed on top of "Device ID": the screens
laid themselves out top-down with fixed pixel gaps and simply assumed the
block's height. They now lay out against _identity_block_top(), and the two QR
screens shrink their code (never below a scannable floor) if that is what it
takes -- which, on every display size the fleet uses, it is not.

These tests render every screen at the fleet's display sizes with the worst-
case identity block (both MACs), record where every piece of text and every
rectangle actually landed, and assert that nothing overlaps and nothing runs
off the image. They also pin the QR at full size on every normal display so
the fit logic can never quietly shrink it.

Runs on a JAM Player: the render needs the on-device venv (Pillow, qrcode)
and the device's fonts. The pure-geometry tests run anywhere the display
module imports.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve()
for candidate in (_HERE.parents[1], _HERE.parents[2]):
    sys.path.insert(0, str(candidate))

try:
    import jam_player_display as disp
    HAVE_DISPLAY = True
    IMPORT_ERROR = ""
except Exception as exc:                                    # pragma: no cover
    disp = None
    HAVE_DISPLAY = False
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

CAN_RENDER = HAVE_DISPLAY and bool(getattr(disp, 'HAS_PIL', False))
NEEDS_MODULE = f"needs the display module ({IMPORT_ERROR})"
NEEDS_PIL = f"needs the on-device venv with Pillow ({IMPORT_ERROR or 'HAS_PIL is False'})"

UUID = "3f2a9c10-7b4e-4d2a-9e1f-0a1b2c3d4e5f"
BOTH_MACS = {'wifiMac': 'DC:A6:32:12:34:56', 'ethernetMac': 'DC:A6:32:12:34:57'}
WIFI_ONLY = {'wifiMac': 'DC:A6:32:12:34:56', 'ethernetMac': None}
NO_MACS = {'wifiMac': None, 'ethernetMac': None}
# 720p, 1080p, 1440p and 4K -- every mode the Pi drives a TV at.
SIZES = [(1280, 720), (1920, 1080), (2560, 1440), (3840, 2160)]


def _screens():
    return [
        ('unregistered', disp.create_unregistered_screen),
        ('awaiting_registration', disp.create_awaiting_registration_screen),
        ('awaiting_screen_link', disp.create_awaiting_screen_link_screen),
        ('waiting_for_content', disp.create_waiting_for_content_screen),
        ('no_active_scenes', disp.create_no_active_scenes_screen),
        ('no_scheduled_content', disp.create_no_scheduled_content_screen),
        ('outlet_inactive', disp.create_outlet_inactive_screen),
        ('boot_identity', disp.create_boot_identity_screen),
    ]


QR_SCREENS = ('unregistered', 'awaiting_registration')


class _RecordingDraw:
    """Wraps a real ImageDraw: every text() and rectangle() lands in `boxes`
    with the pixel bbox it covers, tagged with the size of the image drawn on
    (the QR placeholder draws on its own small image; those are filtered out)."""

    def __init__(self, real, size, boxes):
        self._real = real
        self._size = size
        self._boxes = boxes

    def text(self, xy, text, *args, **kwargs):
        bbox = self._real.textbbox(xy, text, font=kwargs.get('font'), anchor=kwargs.get('anchor'))
        self._boxes.append((self._size, f"text {text[:40]!r}", tuple(int(v) for v in bbox)))
        return self._real.text(xy, text, *args, **kwargs)

    def rectangle(self, xy, *args, **kwargs):
        flat = []
        for point in xy:
            flat.extend(point if isinstance(point, (tuple, list)) else (point,))
        x0, y0, x1, y1 = flat[:4]
        self._boxes.append((self._size, "rectangle", (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))))
        return self._real.rectangle(xy, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _render(screen_fn, width, height, macs):
    """Render one screen with a recording draw; return (image, boxes on the
    screen image, QR sizes requested)."""
    boxes, qr_sizes = [], []
    real_draw = disp.ImageDraw.Draw
    real_qr = disp.generate_qr_code

    def recording_draw(img, *args, **kwargs):
        return _RecordingDraw(real_draw(img, *args, **kwargs), img.size, boxes)

    def recording_qr(url, size=300):
        qr_sizes.append(size)
        return real_qr(url, size)

    # The mesh gradient is decorative and costs 10-15 s per 4K render; layout
    # depends only on the canvas size, so paint a flat one. 96 renders would
    # otherwise take the better part of ten minutes on a Pi.
    def flat_background(w, h, theme="vibrant"):
        return disp.Image.new('RGB', (w, h), (24, 24, 32))

    with mock.patch.object(disp.ImageDraw, 'Draw', side_effect=recording_draw), \
         mock.patch.object(disp, 'generate_qr_code', side_effect=recording_qr), \
         mock.patch.object(disp, 'create_mesh_gradient_background', side_effect=flat_background), \
         mock.patch.object(disp, '_get_display_macs', return_value=dict(macs)):
        result = screen_fn(width, height, UUID)
    img = result[0] if isinstance(result, tuple) else result
    on_screen = [(label, box) for size, label, box in boxes if size == (width, height)]
    return img, on_screen, qr_sizes


def _overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _n_macs(macs):
    return sum(1 for v in macs.values() if v)


@unittest.skipUnless(HAVE_DISPLAY, NEEDS_MODULE)
class IdentityGeometryTests(unittest.TestCase):
    """Pure arithmetic: the block the screens lay out against is the block
    that gets drawn."""

    def test_lines_stack_upward_and_top_is_above_them_all(self):
        for height in (720, 1080, 2160):
            for n_muted in (1, 2, 3):
                geo = disp._identity_block_geometry(height, n_muted)
                ys = geo['muted_ys']
                self.assertEqual(len(ys), n_muted)
                self.assertEqual(ys, sorted(ys), 'muted lines are listed top to bottom')
                self.assertLess(geo['setup_y'], ys[0])
                self.assertLess(geo['id_y'], geo['setup_y'])
                self.assertLess(geo['top'], geo['id_y'])
                self.assertLess(ys[-1], height)

    def test_more_mac_lines_push_the_top_higher(self):
        tops = [disp._identity_block_geometry(1080, n)['top'] for n in (1, 2, 3)]
        self.assertEqual(tops, sorted(tops, reverse=True))

    def test_block_top_tracks_the_macs_that_will_be_drawn(self):
        with mock.patch.object(disp, '_get_display_macs', return_value=dict(BOTH_MACS)):
            with_both = disp._identity_block_top(1080, UUID)
        with mock.patch.object(disp, '_get_display_macs', return_value=dict(NO_MACS)):
            with_none = disp._identity_block_top(1080, UUID)
        self.assertLess(with_both, with_none)
        self.assertEqual(with_both, disp._identity_block_geometry(1080, 3)['top'])
        self.assertEqual(with_none, disp._identity_block_geometry(1080, 1)['top'])

    def test_no_uuid_means_only_the_bottom_margin_is_reserved(self):
        self.assertEqual(disp._identity_block_top(1080, None),
                         1080 - disp._scaled(disp.IDENTITY_BOTTOM_OFFSET, 1080))

    def test_qr_fit_keeps_nominal_when_there_is_room_and_never_goes_below_the_floor(self):
        nominal = disp._scaled(320, 1080)
        self.assertEqual(disp._fit_qr_size(nominal, nominal + 50, 1080, 't'), nominal)
        self.assertEqual(disp._fit_qr_size(nominal, nominal, 1080, 't'), nominal)
        self.assertEqual(disp._fit_qr_size(nominal, nominal - 40, 1080, 't'), nominal - 40)
        floor = disp._scaled(disp.QR_MIN_SIZE, 1080)
        self.assertEqual(disp._fit_qr_size(nominal, 10, 1080, 't'), floor)


@unittest.skipUnless(CAN_RENDER, NEEDS_PIL)
class ScreenLayoutTests(unittest.TestCase):

    def _check(self, name, fn, width, height, macs):
        img, boxes, qr_sizes = _render(fn, width, height, macs)
        self.assertIsNotNone(img, f'{name} did not render')
        self.assertTrue(boxes, f'{name} drew nothing measurable')
        for label, (x0, y0, x1, y1) in boxes:
            self.assertTrue(
                0 <= x0 and x1 <= width and 0 <= y0 and y1 <= height,
                f"{name} {width}x{height} ({_n_macs(macs)} MACs): {label} runs off the image: {(x0, y0, x1, y1)}",
            )
        for i, (label_a, a) in enumerate(boxes):
            for label_b, b in boxes[i + 1:]:
                self.assertFalse(
                    _overlap(a, b),
                    f"{name} {width}x{height} ({_n_macs(macs)} MACs): {label_a} {a} overlaps {label_b} {b}",
                )
        return qr_sizes

    def test_nothing_overlaps_and_nothing_runs_off_screen(self):
        # Two MACs is the tallest identity block, so it is the case that can
        # collide: every screen at every size. Fewer MAC lines only free up
        # room, so those variants are checked at 1080p alone. 48 renders on a
        # flat background: well under a minute on a Pi.
        for name, fn in _screens():
            for width, height in SIZES:
                with self.subTest(screen=name, size=f"{width}x{height}", macs=2):
                    self._check(name, fn, width, height, BOTH_MACS)
            for macs in (WIFI_ONLY, NO_MACS):
                with self.subTest(screen=name, size="1920x1080", macs=_n_macs(macs)):
                    self._check(name, fn, 1920, 1080, macs)

    def test_qr_screens_keep_a_full_size_code_on_every_display_size(self):
        for name, fn in _screens():
            if name not in QR_SCREENS:
                continue
            for width, height in SIZES:
                with self.subTest(screen=name, size=f"{width}x{height}"):
                    qr_sizes = self._check(name, fn, width, height, BOTH_MACS)
                    self.assertEqual(len(qr_sizes), 1, 'exactly one QR per screen')
                    self.assertEqual(qr_sizes[0], disp._scaled(320, height),
                                     f'{name}: the QR was shrunk to {qr_sizes[0]} at {width}x{height}')

    def test_the_setup_tagline_sits_clear_of_the_device_id(self):
        """The exact collision from the bench: 1080p, both MACs."""
        _, boxes, _ = _render(disp.create_unregistered_screen, 1920, 1080, BOTH_MACS)
        tagline = next(box for label, box in boxes if 'Get ready to JAM' in label)
        device_id = next(box for label, box in boxes if 'Device ID:' in label)
        self.assertLess(
            tagline[3] + disp._scaled(disp.IDENTITY_CLEARANCE, 1080) // 2, device_id[1],
            f"tagline bottom {tagline[3]} is not clear of Device ID top {device_id[1]}",
        )

    def test_every_identity_line_is_drawn_when_both_macs_are_known(self):
        _, boxes, _ = _render(disp.create_unregistered_screen, 1920, 1080, BOTH_MACS)
        labels = " | ".join(label for label, _ in boxes)
        for needle in ('Device ID:', 'Setup network:', 'Device: ', 'Wi-Fi MAC:', 'Ethernet MAC:'):
            self.assertIn(needle, labels)


if __name__ == '__main__':
    unittest.main()
