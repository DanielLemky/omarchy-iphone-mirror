"""Portrait/landscape view helpers for the MPV window.

HID coordinates stay in the encoded buffer's space. MPV ``video-rotate`` is
clockwise degrees, matching the CSS convention used by pymobiledevice3's
serve-web viewer after converting negative CSS angles (``-90`` -> ``270``).
"""
TOOLBAR_RATIO = 0.08
MIN_TOOLBAR_PX = 56
MAX_TOOLBAR_RATIO = 0.22
PORTRAIT_GEOMETRY = '400x870'

# SpringBoard getInterfaceOrientation returns UIInterfaceOrientation:
# 1 portrait, 2 upside down, 3 landscape-right (home on left), 4 landscape-left
# (home on right). CoreDevice names describe the physical tilt.
ROTATE_BY_ORIENTATION = {
    1: 0,
    2: 180,
    3: 90,
    4: 270,
    'portrait': 0,
    'portraitUpsideDown': 180,
    'landscapeRight': 90,
    'landscapeLeft': 270,
}


def rotate_for_orientation(orientation):
    if orientation is None:
        return 0
    if orientation in ROTATE_BY_ORIENTATION:
        return ROTATE_BY_ORIENTATION[orientation]
    value = getattr(orientation, 'value', orientation)
    if value in ROTATE_BY_ORIENTATION:
        return ROTATE_BY_ORIENTATION[value]
    try:
        return ROTATE_BY_ORIENTATION.get(int(value), 0)
    except (TypeError, ValueError):
        return ROTATE_BY_ORIENTATION.get(str(value), 0)


def visual_rotate(orientation, buffer_w=0, buffer_h=0):
    """Clockwise degrees to apply on top of the current encoded frame.

    When iOS re-encodes the buffer in the new aspect, extra rotation would
    double-rotate, so it is dropped.
    """
    wanted = rotate_for_orientation(orientation)
    if not (buffer_w > 0 and buffer_h > 0):
        return wanted
    buffer_sideways = buffer_w > buffer_h
    wanted_sideways = (wanted % 180) == 90
    return 0 if buffer_sideways == wanted_sideways else wanted


def displayed_landscape(buffer_w, buffer_h, rotate):
    rotated = (int(rotate) % 180) == 90
    if not (buffer_w > 0 and buffer_h > 0):
        return rotated
    return (buffer_w > buffer_h) ^ rotated


def swapped_geometry(width, height, landscape):
    """Return (w, h) if the window aspect should flip, otherwise None."""
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    if landscape and height > width:
        return height, width
    if not landscape and width > height:
        return height, width
    return None


def toolbar_ratio_for(height):
    try:
        height = float(height)
    except (TypeError, ValueError):
        return TOOLBAR_RATIO
    if not (height > 0):
        return TOOLBAR_RATIO
    return max(TOOLBAR_RATIO, min(MAX_TOOLBAR_RATIO, MIN_TOOLBAR_PX / height))


def hid_from_displayed(nx, ny, rotate):
    """Map displayed-video normalised coords to encoded-buffer space."""
    nx = min(1.0, max(0.0, float(nx)))
    ny = min(1.0, max(0.0, float(ny)))
    r = int(rotate) % 360
    if r < 0:
        r += 360
    if r == 90:
        return ny, 1.0 - nx
    if r == 180:
        return 1.0 - nx, 1.0 - ny
    if r == 270:
        return 1.0 - ny, nx
    return nx, ny
