"""Portrait/landscape view helpers for the MPV window.

HID coordinates stay in the encoded buffer's space. MPV ``video-rotate`` is
clockwise degrees, matching the CSS convention used by pymobiledevice3's
serve-web viewer after converting negative CSS angles (``-90`` -> ``270``).
"""
TOOLBAR_RATIO = 0.08
TOOLBAR_HEIGHT_PX = 68
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
    return min(MAX_TOOLBAR_RATIO, TOOLBAR_HEIGHT_PX / height)


def display_size_from(info, display_id=1):
    """Read pixel dimensions for the selected display from CoreDevice metadata."""
    if not isinstance(info, dict) or not isinstance(info.get('displays'), list):
        return None
    for display in info['displays']:
        if not isinstance(display, dict) or display.get('displayId') != display_id:
            continue
        mode = display.get('currentMode')
        mode_size = mode.get('size') if isinstance(mode, dict) else None
        for size in (mode_size, display.get('nativeSize')):
            if (isinstance(size, (list, tuple)) and len(size) == 2
                    and all(type(value) in (int, float) and 0 < value <= 16384
                            and float(value).is_integer() for value in size)):
                return tuple(sorted(int(value) for value in size))
    return None


def phone_frame_crop(width, height, display_size):
    """Remove small encoder padding, only when display metadata establishes its size."""
    if display_size is not None and width > 0 and height > 0:
        active_w, active_h = display_size
        if width > height:
            active_w, active_h = active_h, active_w
        # Do not crop scaled streams or a different display mode. HEVC padding
        # for this stream is less than one 64-pixel coding block on each axis.
        if 0 <= width - active_w < 64 and 0 <= height - active_h < 64:
            crop = f'{active_w}x{active_h}+0+0' if (width, height) != (active_w, active_h) else ''
            return active_w, active_h, crop
    return width, height, ''


def fitted_geometry(width, height, buffer_w, buffer_h, rotate,
                    previous_landscape=None, resize_axis=None):
    """Fit the frame and toolbar, preserving the user's resized axis."""
    if min(width, height, buffer_w, buffer_h) <= 0:
        return None
    frame_w, frame_h = buffer_w, buffer_h
    if rotate % 180 == 90:
        frame_w, frame_h = frame_h, frame_w
    landscape = frame_w > frame_h
    flipped = previous_landscape is not None and landscape != previous_landscape
    anchor_width = resize_axis == 'width' or (landscape and resize_axis != 'height')
    if anchor_width:
        target_w = height if flipped else width
        if previous_landscape is None and resize_axis is None:
            target_w = max(width, height)
        video_h = target_w * frame_h / frame_w
        # Solve h * (1 - toolbar_ratio_for(h)) == video_h.
        target_h = min(video_h / (1 - MAX_TOOLBAR_RATIO), video_h + TOOLBAR_HEIGHT_PX)
    else:
        target_h = width if flipped else height
        if previous_landscape is None and resize_axis is None:
            target_h = max(width, height)
        target_w = target_h * (1 - toolbar_ratio_for(target_h)) * frame_w / frame_h
    return max(1, round(target_w)), max(1, round(target_h))


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
