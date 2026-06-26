"""
meeting_status.py
-----------------
System tray app that detects when you are in a meeting by watching
microphone and camera usage in Windows, and whether the workstation is
locked, and shows the status on:

  - A colored dot in the system tray (always)
  - A Gicisky / PICKSMART e-paper tag via Bluetooth, showing your
    name on top and your status (AVAILABLE / IN MEETING / AWAY) below.

Everything in one file - no separate driver needed. The tag is only
updated WHEN the status changes (Free <-> In meeting <-> Away), because
e-paper is slow to write (~15-20 s) and every write drains the battery.

Lock/unlock (Win+L) is detected via Windows session-change
notifications: locking shows AWAY immediately, unlocking returns to
AVAILABLE / IN MEETING immediately. Lock state comes only from these
events, so the status can't get stuck on AWAY.

Settings (name, tag address, icons) are configured via the Settings
dialog in the tray menu and stored in 'settings.json' next to the
program. Windows only.

https://www.aliexpress.com/item/1005002766306867.html
Color 2.9'' Eink Screen Price Tag Price Display Shelf Label Low Power Consumption ESL Digital Price Tag for Supermarket

Dependencies:  pip install pystray Pillow bleak
"""

# ============================================================
# DEFAULTS -- used if settings.json doesn't exist yet.
# ============================================================

DEFAULT_TAG_NAME_PREFIX = "NEM"     # tags advertise with this prefix
DEFAULT_TAG_ADDRESS     = ""        # MAC address, set via Settings
DEFAULT_TAG_ENABLED     = False
DEFAULT_DISPLAY_NAME    = "Anders"  # name shown at the top of the tag
DEFAULT_INTERVAL        = 5.0       # polling interval (s)
DEFAULT_ICON_FREE       = "none"    # icon next to "AVAILABLE"
DEFAULT_ICON_BUSY       = "none"    # icon next to "IN MEETING"
DEFAULT_ICON_AWAY       = "none"    # icon next to "AWAY"

STATUS_TEXT = {"FREE": "AVAILABLE", "BUSY": "IN MEETING", "AWAY": "AWAY"}

# Available status icons (generic single-color symbols, drawn in code).
# "none" = no icon.
ICONS_FREE = ["none", "check", "thumb", "coffee", "circle", "smiley"]
ICONS_BUSY = ["none", "speech", "camera", "headset", "phone", "video"]
ICONS_AWAY = ["none", "lock", "door", "clock", "moon", "walk", "coffee"]
ICON_LABELS = {
    "none":    "(none)",
    # free
    "check":   "Check mark",
    "thumb":   "Thumbs up",
    "coffee":  "Coffee cup",
    "circle":  "Filled dot",
    "smiley":  "Smiley",
    # busy
    "speech":  "Speech bubble (chat)",
    "camera":  "Video camera",
    "headset": "Headset",
    "phone":   "Phone",
    "video":   "Video / play",
    # away
    "lock":    "Padlock",
    "door":    "Door",
    "clock":   "Clock",
    "moon":    "Moon",
    "walk":    "Walking person",
}

# ============================================================
# PANEL SETTINGS (confirmed for 2.9" BWR tag, NEMRxxxxxxxx)
# ============================================================
PANEL_WIDTH      = 296
PANEL_HEIGHT     = 128
PANEL_ROTATION   = 90       # text readable with barcode on the right
PANEL_THRESHOLD  = 128      # luminance threshold white/black
RED_THRESHOLD    = 128
PANEL_MIRROR_X   = False
PANEL_MIRROR_Y   = False
PANEL_INVERT_BW  = False    # set True if black/white are swapped
PANEL_INVERT_RED = False    # set True if red ends up wrong
PANEL_DATA_CHUNK = 240
PANEL_BORDER_WIDTH = 8      # frame thickness in pixels (0 = no frame)

# GATT: fef0 service, fef1 (cmd) + fef2 (data) - resolved dynamically.
# ============================================================

import argparse
import asyncio
import json
import logging
import os
import struct
import sys
import threading
import time
import queue
from pathlib import Path
from typing import Optional, List, Tuple

import pystray
from PIL import Image, ImageDraw, ImageFont

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
    HAS_TK = True
except ImportError:
    HAS_TK = False

try:
    from bleak import BleakClient, BleakScanner
    HAS_BLE = True
except ImportError:
    HAS_BLE = False


if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).resolve().parent

LOG_PATH = APP_DIR / "meeting_status.log"
HELP_FILE = APP_DIR / "SETUP_HELP.txt"
SETTINGS_FILE = APP_DIR / "settings.json"

COLORS = {
    "BUSY": (196, 49, 75),    # red
    "FREE": (146, 195, 83),   # green
    "AWAY": (90, 120, 200),   # blue
    "OFF":  (138, 136, 134),  # gray
}

_PANEL_RGB = {
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "red":   (255, 0, 0),
}


# ============================================================
#  E-PAPER PANEL (inlined driver)
# ============================================================

def _load_font(size):
    for path in (
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_status_icon(draw, name, x, y, s, col):
    """Draw a generic single-color status icon at (x, y), size s, in color col."""
    if name == "speech":
        draw.rounded_rectangle([x, y, x+s, y+int(s*0.70)], radius=int(s*0.16), fill=col)
        draw.polygon([(x+int(s*0.22), y+int(s*0.68)), (x+int(s*0.42), y+int(s*0.68)),
                      (x+int(s*0.24), y+int(s*0.96))], fill=col)
    elif name == "camera":
        bw = int(s*0.66)
        draw.rounded_rectangle([x, y+int(s*0.22), x+bw, y+int(s*0.78)],
                               radius=int(s*0.09), fill=col)
        draw.polygon([(x+bw, y+int(s*0.40)), (x+s, y+int(s*0.24)),
                      (x+s, y+int(s*0.76)), (x+bw, y+int(s*0.60))], fill=col)
    elif name == "headset":
        lw = max(3, int(s*0.13))
        draw.arc([x+int(s*0.08), y+int(s*0.05), x+s-int(s*0.08), y+s-int(s*0.05)],
                 start=180, end=360, fill=col, width=lw)
        ew = int(s*0.20); eh = int(s*0.30); ey = y+int(s*0.40)
        draw.rounded_rectangle([x+int(s*0.02), ey, x+int(s*0.02)+ew, ey+eh],
                               radius=int(s*0.07), fill=col)
        draw.rounded_rectangle([x+s-int(s*0.02)-ew, ey, x+s-int(s*0.02), ey+eh],
                               radius=int(s*0.07), fill=col)
        draw.arc([x+int(s*0.28), y+int(s*0.50), x+int(s*0.78), y+s+int(s*0.10)],
                 start=20, end=110, fill=col, width=max(2, int(s*0.08)))
        draw.ellipse([x+int(s*0.24), y+int(s*0.78), x+int(s*0.36), y+int(s*0.90)], fill=col)
    elif name == "phone":
        # Smartphone: solid rounded body with a slim screen line and earpiece,
        # tuned to stay readable at small sizes on the tag.
        draw.rounded_rectangle([x+int(s*0.28), y+int(s*0.04),
                                x+int(s*0.72), y+int(s*0.96)],
                               radius=int(s*0.12), fill=col)
        # earpiece slot (top) and home dot (bottom) punched out in white
        draw.rounded_rectangle([x+int(s*0.42), y+int(s*0.12),
                                x+int(s*0.58), y+int(s*0.155)],
                               radius=int(s*0.02), fill=_PANEL_RGB["white"])
        r = max(2, int(s*0.045))
        cx = x+int(s*0.50); cy = y+int(s*0.87)
        draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=_PANEL_RGB["white"])
    elif name == "video":
        draw.rounded_rectangle([x, y+int(s*0.12), x+s, y+int(s*0.88)],
                               radius=int(s*0.14), fill=col)
        cx = x+int(s*0.50); cy = y+int(s*0.50); t = int(s*0.18)
        draw.polygon([(cx-t+int(s*0.04), cy-t), (cx-t+int(s*0.04), cy+t),
                      (cx+t, cy)], fill=_PANEL_RGB["white"])
    elif name == "check":
        lw = max(4, int(s*0.16))
        draw.line([(x+int(s*0.12), y+int(s*0.52)), (x+int(s*0.40), y+int(s*0.80)),
                   (x+int(s*0.88), y+int(s*0.20))], fill=col, width=lw, joint="curve")
    elif name == "thumb":
        draw.rounded_rectangle([x+int(s*0.06), y+int(s*0.50), x+int(s*0.28), y+int(s*0.95)],
                               radius=int(s*0.05), fill=col)
        draw.rounded_rectangle([x+int(s*0.28), y+int(s*0.42), x+int(s*0.90), y+int(s*0.92)],
                               radius=int(s*0.14), fill=col)
        draw.rounded_rectangle([x+int(s*0.40), y+int(s*0.06), x+int(s*0.62), y+int(s*0.50)],
                               radius=int(s*0.11), fill=col)
    elif name == "coffee":
        draw.rounded_rectangle([x+int(s*0.12), y+int(s*0.34), x+int(s*0.66), y+int(s*0.88)],
                               radius=int(s*0.07), fill=col)
        lw = max(3, int(s*0.10))
        draw.arc([x+int(s*0.58), y+int(s*0.40), x+int(s*0.94), y+int(s*0.74)],
                 start=300, end=70, fill=col, width=lw)
        for dx in (0.26, 0.45):
            draw.arc([x+int(s*dx), y+int(s*0.04), x+int(s*(dx+0.13)), y+int(s*0.30)],
                     start=110, end=290, fill=col, width=max(2, int(s*0.06)))
    elif name == "circle":
        draw.ellipse([x+int(s*0.10), y+int(s*0.10), x+int(s*0.90), y+int(s*0.90)], fill=col)
    elif name == "smiley":
        lw = max(3, int(s*0.09))
        draw.ellipse([x+int(s*0.06), y+int(s*0.06), x+int(s*0.94), y+int(s*0.94)],
                     outline=col, width=lw)
        er = int(s*0.06)
        draw.ellipse([x+int(s*0.32)-er, y+int(s*0.36)-er, x+int(s*0.32)+er, y+int(s*0.36)+er], fill=col)
        draw.ellipse([x+int(s*0.68)-er, y+int(s*0.36)-er, x+int(s*0.68)+er, y+int(s*0.36)+er], fill=col)
        draw.arc([x+int(s*0.28), y+int(s*0.40), x+int(s*0.72), y+int(s*0.78)],
                 start=20, end=160, fill=col, width=lw)
    elif name == "lock":
        # Padlock: body + shackle, with a keyhole punched out in white.
        body_top = y + int(s*0.42)
        draw.rounded_rectangle([x+int(s*0.18), body_top, x+int(s*0.82), y+int(s*0.94)],
                               radius=int(s*0.10), fill=col)
        lw = max(3, int(s*0.11))
        # shackle (arch)
        draw.arc([x+int(s*0.28), y+int(s*0.06), x+int(s*0.72), body_top + int(s*0.10)],
                 start=180, end=360, fill=col, width=lw)
        # keyhole
        kr = max(2, int(s*0.07))
        kcx = x+int(s*0.50); kcy = y+int(s*0.62)
        draw.ellipse([kcx-kr, kcy-kr, kcx+kr, kcy+kr], fill=_PANEL_RGB["white"])
        draw.rectangle([kcx-max(1, int(s*0.025)), kcy,
                        kcx+max(1, int(s*0.025)), kcy+int(s*0.18)],
                       fill=_PANEL_RGB["white"])
    elif name == "door":
        # Door panel with a small knob.
        draw.rounded_rectangle([x+int(s*0.20), y+int(s*0.06),
                                x+int(s*0.80), y+int(s*0.94)],
                               radius=int(s*0.05), fill=col)
        # inner cut to read as a door (white inset)
        draw.rounded_rectangle([x+int(s*0.28), y+int(s*0.14),
                                x+int(s*0.72), y+int(s*0.86)],
                               radius=int(s*0.04), outline=_PANEL_RGB["white"],
                               width=max(2, int(s*0.05)))
        kr = max(2, int(s*0.045))
        kcx = x+int(s*0.64); kcy = y+int(s*0.52)
        draw.ellipse([kcx-kr, kcy-kr, kcx+kr, kcy+kr], fill=_PANEL_RGB["white"])
    elif name == "clock":
        lw = max(3, int(s*0.08))
        draw.ellipse([x+int(s*0.08), y+int(s*0.08), x+int(s*0.92), y+int(s*0.92)],
                     outline=col, width=lw)
        cx = x+int(s*0.50); cy = y+int(s*0.50)
        # hands (12 and 4 o'clock-ish)
        draw.line([(cx, cy), (cx, y+int(s*0.22))], fill=col, width=max(2, int(s*0.07)))
        draw.line([(cx, cy), (x+int(s*0.72), y+int(s*0.62))],
                  fill=col, width=max(2, int(s*0.07)))
    elif name == "moon":
        # Crescent: big disc minus an offset disc (offset filled white).
        draw.ellipse([x+int(s*0.14), y+int(s*0.08), x+int(s*0.90), y+int(s*0.92)], fill=col)
        draw.ellipse([x+int(s*0.34), y+int(s*0.02), x+int(s*1.02), y+int(s*0.84)],
                     fill=_PANEL_RGB["white"])
    elif name == "walk":
        # Simple walking stick-figure.
        lw = max(3, int(s*0.10))
        hr = int(s*0.10)
        hcx = x+int(s*0.50); hcy = y+int(s*0.16)
        draw.ellipse([hcx-hr, hcy-hr, hcx+hr, hcy+hr], fill=col)
        # torso
        draw.line([(hcx, hcy+hr), (x+int(s*0.46), y+int(s*0.58))], fill=col, width=lw)
        # legs
        draw.line([(x+int(s*0.46), y+int(s*0.58)), (x+int(s*0.28), y+int(s*0.92))],
                  fill=col, width=lw)
        draw.line([(x+int(s*0.46), y+int(s*0.58)), (x+int(s*0.66), y+int(s*0.90))],
                  fill=col, width=lw)
        # arms
        draw.line([(hcx, y+int(s*0.34)), (x+int(s*0.30), y+int(s*0.46))], fill=col, width=lw)
        draw.line([(hcx, y+int(s*0.34)), (x+int(s*0.68), y+int(s*0.44))], fill=col, width=lw)


def panel_render_status(name, status_text, status_color, icon="none",
                        border_color=None):
    """Render the tag image: name on top, status below, optional icon beside it,
    and an optional colored frame around the whole display."""
    img = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), _PANEL_RGB["white"])
    draw = ImageDraw.Draw(img)

    # --- Frame (drawn first so text sits on top) ---
    inset = 0
    if border_color and PANEL_BORDER_WIDTH > 0:
        bw = PANEL_BORDER_WIDTH
        col = _PANEL_RGB[border_color]
        for i in range(bw):
            draw.rectangle([i, i, PANEL_WIDTH - 1 - i, PANEL_HEIGHT - 1 - i],
                           outline=col)
        inset = bw + 4  # keep text/icon clear of the frame

    avail_w = PANEL_WIDTH - 2 * inset

    # --- Name (top) ---
    name_font = _load_font(52)
    while name_font.size > 12:
        box = draw.textbbox((0, 0), name, font=name_font)
        if box[2] - box[0] <= avail_w - 8:
            break
        name_font = _load_font(name_font.size - 2)
    nbox = draw.textbbox((0, 0), name, font=name_font)
    nw, nh = nbox[2] - nbox[0], nbox[3] - nbox[1]
    name_y = max(inset, int(PANEL_HEIGHT * 0.10))
    draw.text(((PANEL_WIDTH - nw) // 2 - nbox[0], name_y - nbox[1]),
              name, fill=_PANEL_RGB["black"], font=name_font)

    # --- Status (bottom), with optional icon to the left ---
    status_font = _load_font(50)
    has_icon = icon and icon != "none"
    icon_size = 0
    gap = 0
    while status_font.size > 12:
        sbox = draw.textbbox((0, 0), status_text, font=status_font)
        sw = sbox[2] - sbox[0]
        icon_size = int(status_font.size * 0.95) if has_icon else 0
        gap = int(status_font.size * 0.25) if has_icon else 0
        if sw + icon_size + gap <= avail_w - 8:
            break
        status_font = _load_font(status_font.size - 2)

    sbox = draw.textbbox((0, 0), status_text, font=status_font)
    sw, sh = sbox[2] - sbox[0], sbox[3] - sbox[1]
    block_w = sw + icon_size + gap
    start_x = (PANEL_WIDTH - block_w) // 2
    status_y = int(PANEL_HEIGHT * 0.52)

    col = _PANEL_RGB[status_color]
    if has_icon:
        icon_y = status_y + (sh - icon_size) // 2
        _draw_status_icon(draw, icon, start_x, icon_y, icon_size, col)
        text_x = start_x + icon_size + gap
    else:
        text_x = start_x
    draw.text((text_x - sbox[0], status_y - sbox[1]),
              status_text, fill=col, font=status_font)

    return _panel_quantize(img)


def panel_render_text(text, font_size=48, color="black", bg="white",
                      accent_lines=None):
    """Render text to a PANEL_WIDTH x PANEL_HEIGHT image in panel colors."""
    img = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), _PANEL_RGB[bg])
    draw = ImageDraw.Draw(img)
    lines = text.split("\n")
    font = _load_font(font_size)
    heights = []
    while font_size > 10:
        widths, heights = [], []
        for ln in lines:
            box = draw.textbbox((0, 0), ln, font=font)
            widths.append(box[2] - box[0])
            heights.append(box[3] - box[1])
        total_h = sum(heights) + (len(lines) - 1) * 6
        if max(widths) <= PANEL_WIDTH - 8 and total_h <= PANEL_HEIGHT - 8:
            break
        font_size -= 2
        font = _load_font(font_size)

    line_h = (max(heights) if heights else font_size) + 6
    total_h = line_h * len(lines)
    y = (PANEL_HEIGHT - total_h) // 2
    accent_lines = accent_lines or {}
    for i, ln in enumerate(lines):
        box = draw.textbbox((0, 0), ln, font=font)
        w = box[2] - box[0]
        x = (PANEL_WIDTH - w) // 2
        col = _PANEL_RGB[accent_lines.get(i, color)]
        draw.text((x - box[0], y - box[1]), ln, fill=col, font=font)
        y += line_h
    return _panel_quantize(img)


def _panel_quantize(img):
    pal = list(_PANEL_RGB.values())
    px = img.load()
    for yy in range(img.height):
        for xx in range(img.width):
            r, g, b = px[xx, yy]
            best = min(pal, key=lambda c: (c[0]-r)**2 + (c[1]-g)**2 + (c[2]-b)**2)
            px[xx, yy] = best
    return img


def panel_image_to_payload(img):
    """BWR dual-plane: plane 1 = black/white (1=white), plane 2 = red (1=red),
    MSB first, row by row. The image is rotated per PANEL_ROTATION first."""
    base = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), "white")
    base.paste(img.convert("RGB"), (0, 0))
    if PANEL_ROTATION:
        base = base.rotate(PANEL_ROTATION, expand=True)

    width, height = base.size
    px = base.load()
    bw_plane, red_plane = [], []
    cur_bw = cur_red = 0
    bit = 7
    y_range = range(height - 1, -1, -1) if PANEL_MIRROR_Y else range(height)
    x_range = range(width - 1, -1, -1) if PANEL_MIRROR_X else range(width)
    for y in y_range:
        for x in x_range:
            r, g, b = px[x, y]
            luminance = ((r * 38) + (g * 75) + (b * 15)) >> 7
            is_white = luminance > PANEL_THRESHOLD
            is_red = (r > RED_THRESHOLD) and (g < RED_THRESHOLD)
            if PANEL_INVERT_BW:
                is_white = not is_white
            if is_white:
                cur_bw |= (1 << bit)
            if is_red != PANEL_INVERT_RED:
                cur_red |= (1 << bit)
            bit -= 1
            if bit < 0:
                bw_plane.append(cur_bw)
                red_plane.append(cur_red)
                cur_bw = cur_red = 0
                bit = 7
    if bit != 7:
        bw_plane.append(cur_bw)
        red_plane.append(cur_red)
    return bytes(bytearray(bw_plane + red_plane))


async def _panel_resolve_chars(client):
    uuids = []
    for svc in client.services:
        if svc.uuid.lower().startswith("0000f"):
            for ch in svc.characteristics:
                uuids.append(ch.uuid)
    if len(uuids) < 2:
        raise RuntimeError(f"Found only {len(uuids)} f-characteristics.")
    uuids = sorted(uuids, key=lambda u: int(u[4:8], 16))
    return uuids[0], uuids[1]


def _panel_size_packet(size):
    pkt = bytearray(8)
    pkt[0] = 0x02
    struct.pack_into("<I", pkt, 1, size)
    return bytes(pkt)


def _panel_data_packet(part, payload):
    start = part * PANEL_DATA_CHUNK
    chunk = payload[start:start + min(PANEL_DATA_CHUNK, len(payload) - start)]
    pkt = bytearray(4 + len(chunk))
    struct.pack_into("<I", pkt, 0, part)
    pkt[4:] = chunk
    return bytes(pkt)


async def panel_send(address, payload):
    """Send payload to the tag via the BWR handshake. Returns True/False."""
    if not HAS_BLE:
        log.warning("bleak missing - cannot send to tag")
        return False

    resp = {"data": None}
    got = asyncio.Event()

    def on_notify(_ch, data):
        if resp["data"] is None:
            resp["data"] = bytes(data)
            got.set()

    try:
        async with BleakClient(address) as client:
            cmd_uuid, data_uuid = await _panel_resolve_chars(client)

            async def cmd(uuid, packet, timeout=10.0):
                resp["data"] = None
                got.clear()
                await client.write_gatt_char(uuid, packet, response=False)
                await asyncio.wait_for(got.wait(), timeout)
                return resp["data"]

            await client.start_notify(cmd_uuid, on_notify)
            await asyncio.sleep(1.0)

            d = await cmd(cmd_uuid, bytes([0x01]))
            if len(d) < 3 or d[0] != 0x01 or d[1] != 0xF4 or d[2] != 0x00:
                log.warning(f"Tag START unexpected reply: {d.hex()}")
                return False

            d = await cmd(cmd_uuid, _panel_size_packet(len(payload)))
            if len(d) < 1 or d[0] != 0x02:
                log.warning(f"Tag SIZE unexpected reply: {d.hex()}")
                return False

            d = await cmd(cmd_uuid, bytes([0x03]))
            if len(d) < 6 or d[0] != 0x05 or d[1] != 0x00:
                log.warning(f"Tag START IMAGE unexpected reply: {d.hex()}")
                return False
            part = int.from_bytes(d[2:6], "little")

            last_part, repeat = -1, 0
            while True:
                if part * PANEL_DATA_CHUNK >= len(payload):
                    break
                d = await cmd(data_uuid, _panel_data_packet(part, payload),
                              timeout=10.0)
                if len(d) < 6 or d[0] != 0x05 or d[1] != 0x00:
                    break
                new_part = int.from_bytes(d[2:6], "little")
                if new_part == last_part:
                    repeat += 1
                    if repeat >= 3:
                        log.warning(f"Tag stalled at part {new_part}")
                        return False
                else:
                    repeat, last_part = 1, new_part
                part = new_part
            return True
    except Exception as e:
        log.warning(f"Tag write error: {e}")
        return False


# ============================================================
#  SETUP GUIDE
# ============================================================

SETUP_GUIDE = """\
================================================================
  Meeting Status (e-paper) - Setup Guide
================================================================

This program detects when you are in a meeting via microphone and
camera usage in Windows, and when you lock your computer. It shows
the status on:

  - A colored dot in the system tray (always)
  - A Gicisky / PICKSMART e-paper tag via Bluetooth, showing your
    name on top and your status below:
        AVAILABLE  (black)
        IN MEETING (red)
        AWAY       (black) - shown when the computer is locked

Everything in a single file. Windows only.


================================================================
  How detection works
================================================================

Windows tracks per-app mic/camera access in the registry. When an
app has the device open, "LastUsedTimeStop" is 0.

Lock/unlock is detected via Windows session-change notifications
(a hidden window registered with WTSRegisterSessionNotification):

  - Press Win+L (or lock any other way)  -->  AWAY, immediately.
  - Unlock the computer                  -->  back to AVAILABLE or
                                              IN MEETING, immediately.

The lock state comes ONLY from these events - there is no desktop
probing - so a managed/domain machine can't get stuck on AWAY.

Status priority while UNLOCKED:

  Mic active OR camera active  -->  IN MEETING (red)
  Otherwise                    -->  AVAILABLE

Works for Teams, Zoom, Meet, Webex, Slack, Discord, etc.
NOTE: mute keeps the microphone open, so you show as IN MEETING
even while muted.


================================================================
  Why only on status change?
================================================================

E-paper is slow: ~15-20 s per write, drains the battery. So the tag
is only written WHEN the status switches between Available, In
meeting and Away - not on every poll.


================================================================
  Pairing the tag
================================================================

1. Wake the tag (remove/reinsert a battery).
2. Right-click the tray icon -> "Settings...".
3. Check "Enable e-paper tag", click "Scan for tags".
4. Select your tag, click "Use selected".
5. Type your name in "Name on the tag".
6. Optionally pick icons for Available / In meeting / Away.
7. Click "Save".


================================================================
  Build exe
================================================================

  pip install pystray Pillow bleak pyinstaller
  python -m PyInstaller --onefile --noconsole ^
      --name MeetingStatus ^
      --hidden-import=PIL._tkinter_finder ^
      --collect-all pystray --collect-all PIL ^
      --collect-all bleak ^
      MeetingStatus.py

Result: dist\\MeetingStatus.exe
Autostart: place a shortcut in shell:startup.


================================================================
  Troubleshooting
================================================================

* Tag doesn't update: see meeting_status.log. Check that the tag is
  awake, within range, and that the correct address is saved.
* Scan finds no tags: tags only advertise briefly after waking -
  remove/reinsert a battery and scan immediately.
* Text looks wrong: adjust the PANEL_ constants at the top of the
  file (PANEL_ROTATION, PANEL_INVERT_BW, PANEL_INVERT_RED).
* Lock not detected: the session-notification listener may have
  failed to register (see meeting_status.log). Without it the AWAY
  status won't trigger. Restart the program; if it still fails, check
  that wtsapi32 is reachable on the machine.
* Stuck on AWAY after unlocking: this version only changes lock state
  from Win+L lock/unlock events, so it should clear the instant you
  unlock. If it doesn't, check the log for an "unlock" entry.

================================================================
"""


def setup_logging() -> None:
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    handlers = [logging.FileHandler(LOG_PATH, encoding="utf-8")]
    if sys.stdout is not None and getattr(sys.stdout, "isatty", lambda: False)():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


log = logging.getLogger("meeting_status")


def write_help_file() -> Path:
    try:
        HELP_FILE.write_text(SETUP_GUIDE, encoding="utf-8")
    except Exception as e:
        log.warning(f"Could not write help file: {e}")
    return HELP_FILE


def open_help_file() -> None:
    write_help_file()
    try:
        if sys.platform == "win32":
            os.startfile(str(HELP_FILE))
        elif sys.platform == "darwin":
            os.system(f"open '{HELP_FILE}'")
        else:
            os.system(f"xdg-open '{HELP_FILE}' &")
    except Exception as e:
        log.warning(f"Could not open help file: {e}")


def show_setup_help_now() -> None:
    if sys.stdout is not None and getattr(sys.stdout, "isatty", lambda: False)():
        print(SETUP_GUIDE)
    else:
        open_help_file()


# ============================================================
#  SETTINGS
# ============================================================

class Settings:
    FIELDS = [
        "tag_enabled", "tag_address", "tag_name_prefix",
        "display_name", "interval", "icon_free", "icon_busy", "icon_away",
    ]

    def __init__(self):
        self.tag_enabled     = DEFAULT_TAG_ENABLED
        self.tag_address     = DEFAULT_TAG_ADDRESS
        self.tag_name_prefix = DEFAULT_TAG_NAME_PREFIX
        self.display_name    = DEFAULT_DISPLAY_NAME
        self.interval        = DEFAULT_INTERVAL
        self.icon_free       = DEFAULT_ICON_FREE
        self.icon_busy       = DEFAULT_ICON_BUSY
        self.icon_away       = DEFAULT_ICON_AWAY

    def load(self) -> None:
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                for key in self.FIELDS:
                    if key in data:
                        setattr(self, key, data[key])
                log.info(f"Loaded settings from {SETTINGS_FILE.name}")
            except Exception as e:
                log.warning(f"Could not load {SETTINGS_FILE.name}: {e}")

    def save(self) -> bool:
        data = {k: getattr(self, k) for k in self.FIELDS}
        try:
            SETTINGS_FILE.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8")
            try:
                os.chmod(SETTINGS_FILE, 0o600)
            except OSError:
                pass
            log.info(f"Settings saved to {SETTINGS_FILE.name}")
            return True
        except Exception as e:
            log.error(f"Could not save {SETTINGS_FILE.name}: {e}")
            return False


# ============================================================
#  MIC/CAMERA DETECTION
# ============================================================

def _capability_in_use(capability: str) -> Optional[str]:
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None

    bases = [
        rf"SOFTWARE\Microsoft\Windows\CurrentVersion"
        rf"\CapabilityAccessManager\ConsentStore\{capability}",
        rf"SOFTWARE\Microsoft\Windows\CurrentVersion"
        rf"\CapabilityAccessManager\ConsentStore\{capability}\NonPackaged",
    ]
    for base in bases:
        try:
            root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base)
        except FileNotFoundError:
            continue
        try:
            i = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(root, i)
                    i += 1
                except OSError:
                    break
                try:
                    sub = winreg.OpenKey(root, sub_name)
                except OSError:
                    continue
                try:
                    try:
                        stop, _ = winreg.QueryValueEx(sub, "LastUsedTimeStop")
                        start, _ = winreg.QueryValueEx(sub, "LastUsedTimeStart")
                        if stop == 0 and start != 0:
                            return sub_name
                    except FileNotFoundError:
                        pass
                finally:
                    winreg.CloseKey(sub)
        finally:
            winreg.CloseKey(root)
    return None


def is_microphone_in_use() -> Optional[str]:
    return _capability_in_use("microphone")


def is_camera_in_use() -> Optional[str]:
    return _capability_in_use("webcam")


# ============================================================
#  LOCK DETECTION (Win+L session events only)
# ============================================================


class SessionMonitor:
    """Instant lock/unlock detection via Windows session-change events.

    Creates a hidden message-only window, registers it for session
    notifications, and runs a Win32 message loop on its own thread. On
    WM_WTSSESSION_CHANGE with the lock/unlock subcodes it invokes the
    supplied callbacks. If anything fails (non-Windows, missing APIs)
    it stays silent and the poll loop remains the fallback."""

    WM_WTSSESSION_CHANGE = 0x02B1
    WTS_SESSION_LOCK     = 0x7
    WTS_SESSION_UNLOCK   = 0x8
    NOTIFY_FOR_THIS_SESSION = 0
    WM_CLOSE = 0x0010

    def __init__(self, on_lock, on_unlock):
        self._on_lock = on_lock
        self._on_unlock = on_unlock
        self._thread: Optional[threading.Thread] = None
        self._hwnd = None
        self._user32 = None
        self._wtsapi = None
        self._wndproc_ref = None  # keep WNDPROC alive

    def start(self) -> None:
        if sys.platform != "win32":
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes
        except Exception as e:
            log.warning(f"Session monitor unavailable: {e}")
            return

        try:
            # use_last_error=True so ctypes.get_last_error() is reliable.
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            wtsapi = ctypes.WinDLL("wtsapi32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._user32, self._wtsapi = user32, wtsapi

            # LRESULT / LONG_PTR are pointer-sized; wintypes doesn't define
            # LRESULT on all Python versions, so map it ourselves.
            if ctypes.sizeof(ctypes.c_void_p) == 8:
                LRESULT = ctypes.c_int64
            else:
                LRESULT = ctypes.c_long

            WNDPROC = ctypes.WINFUNCTYPE(
                LRESULT, wintypes.HWND, wintypes.UINT,
                wintypes.WPARAM, wintypes.LPARAM)

            # --- Correct prototypes (critical on 64-bit) ---
            user32.DefWindowProcW.restype = LRESULT
            user32.DefWindowProcW.argtypes = [
                wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

            user32.CreateWindowExW.restype = wintypes.HWND
            user32.CreateWindowExW.argtypes = [
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                wintypes.HINSTANCE, wintypes.LPVOID]

            user32.DestroyWindow.restype = wintypes.BOOL
            user32.DestroyWindow.argtypes = [wintypes.HWND]

            user32.GetMessageW.restype = ctypes.c_int
            user32.GetMessageW.argtypes = [
                ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT]

            user32.PostMessageW.restype = wintypes.BOOL
            user32.PostMessageW.argtypes = [
                wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

            wtsapi.WTSRegisterSessionNotification.restype = wintypes.BOOL
            wtsapi.WTSRegisterSessionNotification.argtypes = [
                wintypes.HWND, wintypes.DWORD]
            wtsapi.WTSUnRegisterSessionNotification.restype = wintypes.BOOL
            wtsapi.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]

            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

            def wndproc(hwnd, msg, wparam, lparam):
                if msg == self.WM_WTSSESSION_CHANGE:
                    code = int(wparam)
                    if code == self.WTS_SESSION_LOCK:
                        log.info("WTS_SESSION_LOCK received")
                        try:
                            self._on_lock()
                        except Exception as e:
                            log.warning(f"on_lock error: {e}")
                    elif code == self.WTS_SESSION_UNLOCK:
                        log.info("WTS_SESSION_UNLOCK received")
                        try:
                            self._on_unlock()
                        except Exception as e:
                            log.warning(f"on_unlock error: {e}")
                    return 0
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            self._wndproc_ref = WNDPROC(wndproc)

            class WNDCLASS(ctypes.Structure):
                _fields_ = [
                    ("style", wintypes.UINT),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                ]

            user32.RegisterClassW.restype = wintypes.ATOM
            user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]

            hinst = kernel32.GetModuleHandleW(None)

            # Unique class name per process so re-registration can't clash.
            class_name = f"MeetingStatusSessionWnd_{os.getpid()}"
            wc = WNDCLASS()
            wc.style = 0
            wc.lpfnWndProc = self._wndproc_ref
            wc.cbClsExtra = 0
            wc.cbWndExtra = 0
            wc.hInstance = hinst
            wc.hIcon = None
            wc.hCursor = None
            wc.hbrBackground = None
            wc.lpszMenuName = None
            wc.lpszClassName = class_name

            atom = user32.RegisterClassW(ctypes.byref(wc))
            if not atom:
                raise ctypes.WinError(ctypes.get_last_error())

            # HWND_MESSAGE = -3 -> message-only window (no taskbar/UI).
            HWND_MESSAGE = wintypes.HWND(-3)
            hwnd = user32.CreateWindowExW(
                0, class_name, "MeetingStatusSession",
                0, 0, 0, 0, 0, HWND_MESSAGE, None, hinst, None)
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._hwnd = hwnd

            if not wtsapi.WTSRegisterSessionNotification(
                    hwnd, self.NOTIFY_FOR_THIS_SESSION):
                raise ctypes.WinError(ctypes.get_last_error())

            log.info("Session monitor active (instant lock/unlock).")

            # Standard Win32 message loop. GetMessageW returns >0 normally,
            # 0 on WM_QUIT, -1 on error.
            msg = wintypes.MSG()
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0:       # WM_QUIT
                    break
                if ret == -1:      # error
                    log.warning("GetMessageW error in session monitor")
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

            log.info("Session monitor stopped.")

        except Exception as e:
            log.warning(f"Session monitor failed ({e}); "
                        "lock/unlock (AWAY) will not be detected.")

    def stop(self) -> None:
        try:
            if self._hwnd and self._user32:
                if self._wtsapi:
                    try:
                        self._wtsapi.WTSUnRegisterSessionNotification(self._hwnd)
                    except Exception:
                        pass
                # Post WM_CLOSE to break the message loop.
                self._user32.PostMessageW(self._hwnd, self.WM_CLOSE, 0, 0)
        except Exception:
            pass


# ============================================================
#  TRAY ICON
# ============================================================

def make_icon(state: str, size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = COLORS.get(state, COLORS["OFF"])
    pad = 4
    draw.ellipse([pad, pad, size - pad, size - pad],
                 fill=color, outline=(0, 0, 0, 90), width=1)
    return img


# ============================================================
#  E-PAPER TRANSPORT (queue-based, serialized)
# ============================================================

class TagTransport:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.busy = False
        self.last_written = None
        self._stop = False
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None

    @property
    def enabled(self) -> bool:
        return (HAS_BLE
                and self.settings.tag_enabled
                and bool(self.settings.tag_address))

    def start(self) -> None:
        if not self.enabled:
            log.info("E-paper tag disabled (not configured or off)")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info(f"Tag thread started, target {self.settings.tag_address}")

    def stop(self) -> None:
        self._stop = True
        self._queue.put(None)

    def restart(self) -> None:
        self.stop()
        time.sleep(0.3)
        self._thread = None
        self.start()

    def publish(self, state: str, force: bool = False) -> None:
        if not self.enabled:
            return
        if not force and state == self.last_written:
            return
        self._queue.put(state)

    def _run(self) -> None:
        while not self._stop:
            try:
                state = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if state is None or self._stop:
                break
            while True:  # keep only the latest desired status
                try:
                    newer = self._queue.get_nowait()
                    if newer is None:
                        state = None
                        break
                    state = newer
                except queue.Empty:
                    break
            if state is None:
                break
            self._write(state)

    def _write(self, state: str) -> None:
        addr = self.settings.tag_address
        name = self.settings.display_name or "Status"
        status_text = STATUS_TEXT.get(state, "")
        if state == "BUSY":
            status_color = "red"
            icon = self.settings.icon_busy
            border = "red"
        elif state == "AWAY":
            status_color = "black"
            icon = self.settings.icon_away
            border = "black"
        else:  # FREE
            status_color = "black"
            icon = self.settings.icon_free
            border = "black"
        self.busy = True
        try:
            log.info(f"Writing tag: {name} / {status_text} (icon={icon}) ({addr})")
            img = panel_render_status(name, status_text, status_color,
                                      icon=icon, border_color=border)
            payload = panel_image_to_payload(img)
            ok = asyncio.run(panel_send(addr, payload))
            if ok:
                self.last_written = state
                log.info("Tag updated.")
            else:
                log.warning("Tag write failed.")
        except Exception as e:
            log.warning(f"Tag write error: {e}")
        finally:
            self.busy = False


# ============================================================
#  BLE SCAN (one-shot, for settings)
# ============================================================

def scan_for_tags(prefix: str, timeout: float = 8.0) -> List[Tuple[str, str]]:
    if not HAS_BLE:
        return []

    async def _scan():
        results = []
        try:
            devices = await BleakScanner.discover(timeout=timeout)
            for d in devices:
                name = d.name or ""
                if name.upper().startswith(prefix.upper()):
                    results.append((name, d.address))
        except Exception as e:
            log.warning(f"BLE scan error: {e}")
        return results

    try:
        return asyncio.run(_scan())
    except Exception as e:
        log.warning(f"BLE scan failed: {e}")
        return []


# ============================================================
#  SETTINGS DIALOG
# ============================================================

def open_settings_dialog(settings: Settings, on_saved=None) -> None:
    if not HAS_TK:
        log.error("Settings dialog requires tkinter")
        return

    root = tk.Tk()
    root.title("Meeting Status (e-paper) - Settings")
    root.resizable(False, False)
    try:
        root.attributes("-topmost", True)
        root.after(200, lambda: root.attributes("-topmost", False))
    except Exception:
        pass

    main = ttk.Frame(root, padding=12)
    main.grid(row=0, column=0)

    ttk.Label(main, text="Name on the tag", font=("", 10, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
    ttk.Label(main, text="Name:").grid(row=1, column=0, sticky="e", padx=(0, 6))
    var_name = tk.StringVar(value=settings.display_name)
    ttk.Entry(main, textvariable=var_name, width=28).grid(
        row=1, column=1, sticky="w", pady=2)

    ttk.Separator(main).grid(row=2, column=0, columnspan=2, sticky="ew", pady=8)
    ttk.Label(main, text="E-paper tag", font=("", 10, "bold")).grid(
        row=3, column=0, columnspan=2, sticky="w", pady=(0, 6))

    var_enabled = tk.BooleanVar(value=settings.tag_enabled)
    ttk.Checkbutton(main, text="Enable e-paper tag",
                    variable=var_enabled).grid(row=4, column=0, columnspan=2,
                                               sticky="w", pady=2)

    if not HAS_BLE:
        ttk.Label(main, text="(bleak not installed - 'pip install bleak')",
                  foreground="#a00").grid(row=5, column=0, columnspan=2,
                                          sticky="w", pady=2)

    ttk.Label(main, text="Saved tag:").grid(row=6, column=0, sticky="e",
                                            padx=(0, 6), pady=2)
    var_address = tk.StringVar(value=settings.tag_address or "(none)")
    ttk.Label(main, textvariable=var_address,
              font=("Consolas", 9)).grid(row=6, column=1, sticky="w", pady=2)

    ttk.Label(main, text="Discovered tags:").grid(row=7, column=0, sticky="ne",
                                                   padx=(0, 6), pady=(8, 2))
    listbox = tk.Listbox(main, width=42, height=5, font=("Consolas", 9))
    listbox.grid(row=7, column=1, sticky="w", pady=(8, 2))

    discovered: List[Tuple[str, str]] = []

    def do_scan():
        if not HAS_BLE:
            messagebox.showerror("Scan", "bleak library not installed.")
            return
        listbox.delete(0, tk.END)
        listbox.insert(tk.END, "Scanning... (~8 s)")
        scan_btn.config(state="disabled")
        root.update_idletasks()

        def worker():
            try:
                results = scan_for_tags(settings.tag_name_prefix, timeout=8.0)
            except Exception as e:
                results = []
                log.warning(f"Scan thread error: {e}")
            root.after(0, lambda: _scan_done(results))

        def _scan_done(results):
            listbox.delete(0, tk.END)
            discovered.clear()
            discovered.extend(results)
            if not results:
                listbox.insert(tk.END, "(no tags found)")
            else:
                for name, addr in results:
                    listbox.insert(tk.END, f"{name}   {addr}")
            scan_btn.config(state="normal")

        threading.Thread(target=worker, daemon=True).start()

    def use_selected():
        idx = listbox.curselection()
        if not idx or not discovered:
            messagebox.showinfo("Select", "Select a tag from the list first.")
            return
        name, addr = discovered[idx[0]]
        var_address.set(addr)
        settings.tag_address = addr
        var_enabled.set(True)

    def forget_tag():
        var_address.set("(none)")
        settings.tag_address = ""

    btnrow = ttk.Frame(main)
    btnrow.grid(row=8, column=1, sticky="w", pady=(0, 8))
    scan_btn = ttk.Button(btnrow, text="Scan for tags", command=do_scan)
    scan_btn.grid(row=0, column=0, padx=(0, 6))
    ttk.Button(btnrow, text="Use selected", command=use_selected).grid(
        row=0, column=1, padx=(0, 6))
    ttk.Button(btnrow, text="Forget", command=forget_tag).grid(row=0, column=2)

    ttk.Separator(main).grid(row=9, column=0, columnspan=2, sticky="ew", pady=8)
    ttk.Label(main, text="Status icons (on the tag)", font=("", 10, "bold")).grid(
        row=10, column=0, columnspan=2, sticky="w", pady=(0, 4))

    # Map display label -> internal key, and reverse, for each dropdown.
    free_labels = [ICON_LABELS[k] for k in ICONS_FREE]
    busy_labels = [ICON_LABELS[k] for k in ICONS_BUSY]
    away_labels = [ICON_LABELS[k] for k in ICONS_AWAY]
    label_to_free = {ICON_LABELS[k]: k for k in ICONS_FREE}
    label_to_busy = {ICON_LABELS[k]: k for k in ICONS_BUSY}
    label_to_away = {ICON_LABELS[k]: k for k in ICONS_AWAY}

    ttk.Label(main, text="When AVAILABLE:").grid(row=11, column=0, sticky="e", padx=(0, 6))
    var_icon_free = tk.StringVar(
        value=ICON_LABELS.get(settings.icon_free, ICON_LABELS["none"]))
    ttk.Combobox(main, textvariable=var_icon_free, values=free_labels,
                 state="readonly", width=24).grid(row=11, column=1, sticky="w", pady=2)

    ttk.Label(main, text="When IN MEETING:").grid(row=12, column=0, sticky="e", padx=(0, 6))
    var_icon_busy = tk.StringVar(
        value=ICON_LABELS.get(settings.icon_busy, ICON_LABELS["none"]))
    ttk.Combobox(main, textvariable=var_icon_busy, values=busy_labels,
                 state="readonly", width=24).grid(row=12, column=1, sticky="w", pady=2)

    ttk.Label(main, text="When AWAY:").grid(row=13, column=0, sticky="e", padx=(0, 6))
    var_icon_away = tk.StringVar(
        value=ICON_LABELS.get(settings.icon_away, ICON_LABELS["none"]))
    ttk.Combobox(main, textvariable=var_icon_away, values=away_labels,
                 state="readonly", width=24).grid(row=13, column=1, sticky="w", pady=2)

    ttk.Separator(main).grid(row=14, column=0, columnspan=2, sticky="ew", pady=8)
    ttk.Label(main, text="Detection", font=("", 10, "bold")).grid(
        row=15, column=0, columnspan=2, sticky="w", pady=(0, 4))
    ttk.Label(main, text="Polling interval (s):").grid(row=16, column=0,
                                                       sticky="e", padx=(0, 6))
    var_interval = tk.StringVar(value=str(settings.interval))
    ttk.Entry(main, textvariable=var_interval, width=10).grid(
        row=16, column=1, sticky="w", pady=2)

    ttk.Separator(main).grid(row=17, column=0, columnspan=2, sticky="ew", pady=8)
    status_var = tk.StringVar(value=f"Settings file: {SETTINGS_FILE.name}")
    ttk.Label(main, textvariable=status_var, foreground="#555").grid(
        row=18, column=0, columnspan=2, sticky="w", pady=(0, 6))

    btns = ttk.Frame(main)
    btns.grid(row=19, column=0, columnspan=2, sticky="ew")
    btns.columnconfigure(0, weight=1)

    saved = {"ok": False}

    def do_save():
        try:
            settings.interval = max(2.0, float(var_interval.get().strip() or "5"))
        except ValueError:
            messagebox.showerror("Save", "Invalid polling interval.")
            return
        settings.tag_enabled = var_enabled.get()
        settings.display_name = var_name.get().strip() or "Status"
        settings.icon_free = label_to_free.get(var_icon_free.get(), "none")
        settings.icon_busy = label_to_busy.get(var_icon_busy.get(), "none")
        settings.icon_away = label_to_away.get(var_icon_away.get(), "none")
        if settings.save():
            saved["ok"] = True
            root.destroy()
        else:
            messagebox.showerror("Save", "Could not write settings.json.")

    def do_cancel():
        root.destroy()

    ttk.Button(btns, text="Cancel", command=do_cancel).grid(
        row=0, column=1, padx=(0, 6), sticky="e")
    ttk.Button(btns, text="Save", command=do_save).grid(row=0, column=2,
                                                        sticky="e")

    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 2
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.mainloop()

    if saved["ok"] and on_saved:
        on_saved()


# ============================================================
#  APPLICATION
# ============================================================

class App:
    def __init__(self, args, settings: Settings):
        self.args = args
        self.settings = settings
        self.current = "OFF"
        self.last_source = "starting..."
        self.stop = False
        self.tag = TagTransport(settings)
        # Set by the session monitor; the poll loop honours it so an
        # instant lock isn't immediately overridden by a slow poll.
        self.locked = False
        self.session = SessionMonitor(self._on_session_lock,
                                      self._on_session_unlock)

        self.icon = pystray.Icon(
            "meeting_status",
            make_icon("OFF"),
            "Meeting Status: starting...",
            menu=pystray.Menu(
                pystray.MenuItem(lambda i: f"Status:  {self._status_label()}", None, enabled=False),
                pystray.MenuItem(lambda i: f"Source:  {self.last_source}", None, enabled=False),
                pystray.MenuItem(lambda i: f"Tag:     {self._tag_label()}", None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Settings...", self._open_settings, default=True),
                pystray.MenuItem("Update tag now", self._force_update),
                pystray.MenuItem("Setup help", self._show_help),
                pystray.MenuItem("Quit", self._quit),
            ),
        )

    def _status_label(self) -> str:
        return {"BUSY": "IN MEETING", "FREE": "AVAILABLE",
                "AWAY": "AWAY", "OFF": "OFF"}.get(self.current, self.current)

    def _tag_label(self) -> str:
        if not self.tag.enabled:
            return "off"
        if self.tag.busy:
            return "writing..."
        return "ready"

    # --- Session-change callbacks (instant lock/unlock) ---
    def _on_session_lock(self) -> None:
        self.locked = True
        log.info("Session locked (event)")
        self._apply("AWAY", "locked")

    def _on_session_unlock(self) -> None:
        self.locked = False
        log.info("Session unlocked (event)")
        # Re-evaluate immediately so we don't wait for the next poll.
        self._evaluate_unlocked()

    def _force_update(self, icon, item) -> None:
        if self.current in ("BUSY", "FREE", "AWAY"):
            log.info("Manual tag update")
            self.tag.publish(self.current, force=True)

    def _show_help(self, icon, item) -> None:
        open_help_file()

    def _open_settings(self, icon, item) -> None:
        def runner():
            open_settings_dialog(self.settings, on_saved=self._on_settings_saved)
        threading.Thread(target=runner, daemon=True).start()

    def _on_settings_saved(self) -> None:
        log.info("Settings updated, restarting tag transport")
        self.tag.restart()
        def repush():
            time.sleep(1.0)
            if self.current in ("BUSY", "FREE", "AWAY"):
                self.tag.publish(self.current, force=True)
        threading.Thread(target=repush, daemon=True).start()

    def _quit(self, icon, item) -> None:
        log.info("Shutting down")
        self.stop = True
        try:
            self.session.stop()
        except Exception:
            pass
        self.tag.stop()
        icon.stop()

    def _apply(self, cmd: str, source: str) -> None:
        self.last_source = source
        self.icon.title = f"Meeting Status: {self._status_label()} ({source})"
        if cmd != self.current:
            self.current = cmd
            self.icon.icon = make_icon(cmd)
            log.info(f"{cmd} ({source})")
            self.tag.publish(cmd)

    def _evaluate_unlocked(self) -> None:
        """Pick BUSY/FREE from mic+camera (used right after unlock)."""
        try:
            mic_app = is_microphone_in_use()
            cam_app = is_camera_in_use()
            if mic_app or cam_app:
                signals = []
                if mic_app:
                    signals.append(f"mic:{mic_app[:30]}")
                if cam_app:
                    signals.append(f"cam:{cam_app[:30]}")
                self._apply("BUSY", ", ".join(signals))
            else:
                self._apply("FREE", "free")
        except Exception as e:
            log.warning(f"Evaluate error: {e}")

    def _poll_loop(self) -> None:
        log.info("Polling started (microphone + camera; lock via Win+L events only)")
        while not self.stop:
            try:
                # Lock state comes ONLY from Win+L session events. While
                # locked we keep the tag on AWAY and don't poll mic/cam;
                # as soon as you unlock, the unlock event flips us back to
                # AVAILABLE / IN MEETING. No desktop-probing fallback, so
                # managed/domain machines can't get stuck on AWAY.
                if not self.locked:
                    self._evaluate_unlocked()
            except Exception as e:
                log.warning(f"Poll error: {e}")
            self._sleep_interruptible(self.settings.interval)

    def _sleep_interruptible(self, seconds: float) -> None:
        end = time.time() + seconds
        while not self.stop and time.time() < end:
            time.sleep(0.2)

    def run(self) -> None:
        self.tag.start()
        self.session.start()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.icon.run()


def main():
    if "--setup-help" in sys.argv or "-?" in sys.argv:
        show_setup_help_now()
        sys.exit(0)

    setup_logging()
    write_help_file()
    log.info(f"Starting (log: {LOG_PATH})")

    if sys.platform != "win32":
        msg = ("This program is Windows-only. It uses the registry to "
               "detect microphone and camera usage.")
        log.error(msg)
        print(f"ERROR: {msg}", file=sys.stderr)
        sys.exit(1)

    ap = argparse.ArgumentParser(
        description="Detects mic/camera usage and lock state on Windows. "
                    "Status shown in the tray and on a BLE-paired e-paper tag.")
    ap.add_argument("--settings", action="store_true",
                    help="Open settings dialog and exit")
    ap.add_argument("--setup-help", action="store_true",
                    help="Show setup guide and exit")
    args = ap.parse_args()

    settings = Settings()
    settings.load()

    if args.settings:
        if not HAS_TK:
            print("ERROR: tkinter not available", file=sys.stderr)
            sys.exit(1)
        open_settings_dialog(settings)
        sys.exit(0)

    if not SETTINGS_FILE.exists():
        log.info("First run, opening settings dialog")
        if HAS_TK:
            open_settings_dialog(settings)

    App(args, settings).run()


if __name__ == "__main__":
    main()
