"""Portrait/landscape view helpers for the MPV window.

HID coordinates stay in the encoded buffer's space. MPV ``video-rotate`` is
clockwise degrees, matching the CSS convention used by pymobiledevice3's
serve-web viewer after converting negative CSS angles (``-90`` -> ``270``).
"""
TOOLBAR_RATIO = 0.08
MIN_TOOLBAR_PX = 56
MAX_TOOLBAR_RATIO = 0.22
PORTRAIT_GEOMETRY = '400x870'

# SpringBoard getInterfaceOrientation integers, with MPV clockwise degrees.
# A real-phone landscape session reported orientation 3 with video-rotate 90
# and the picture was inverted; 3 therefore needs 270, not 90.
ROTATE_BY_ORIENTATION = {
    1: 0,
    2: 180,
    3: 270,
    4: 90,
    'portrait': 0,
    'portraitUpsideDown': 180,
    'landscapeLeft': 270,
    'landscapeRight': 90,
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

    A 90° or 270° turn changes aspect. When the buffer is already that
    aspect, iOS re-encoded the frame, and extra rotation would turn it
    twice. 0° and 180° keep the same aspect, so a portrait buffer still
    needs the 180° turn for upside-down portrait.
    """
    wanted = rotate_for_orientation(orientation)
    if not (buffer_w > 0 and buffer_h > 0) or (wanted % 180) != 90:
        return wanted
    return 0 if buffer_w > buffer_h else wanted


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


def scroll_hid_delta(amount, rotate):
    """Buffer-space ``(dx, dy)`` for a displayed-vertical finger move.

    Positive ``amount`` moves the finger down on the picture the user sees
    (wheel up). Units match ``amount``, not normalised coordinates.
    """
    x0, y0 = hid_from_displayed(0.5, 0.5, rotate)
    x1, y1 = hid_from_displayed(0.5, 0.75, rotate)
    scale = float(amount) / 0.25
    return (x1 - x0) * scale, (y1 - y0) * scale


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
