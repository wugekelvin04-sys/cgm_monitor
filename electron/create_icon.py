"""
Generate electron/icon.png — a 32×32 CGM sensor icon.
Concept: round sensor patch worn on skin + wireless signal arcs above it.
Uses only Python stdlib: struct + zlib. RGBA PNG with anti-aliasing.
"""
import struct, zlib, pathlib, math

# ── PNG encoder ───────────────────────────────────────────────────────────────
def _chunk(tag, data):
    c = tag + data
    return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xFFFFFFFF)

def make_png(w, h, pixels):
    ihdr = struct.pack('>II', w, h) + bytes([8, 6, 0, 0, 0])
    raw  = b''.join(
        b'\x00' + bytes([c for px in pixels[y*w:(y+1)*w] for c in px])
        for y in range(h)
    )
    return (b'\x89PNG\r\n\x1a\n' +
            _chunk(b'IHDR', ihdr) +
            _chunk(b'IDAT', zlib.compress(raw, 9)) +
            _chunk(b'IEND', b''))

# ── Anti-aliasing ─────────────────────────────────────────────────────────────
OFFSETS = [i/4 + 0.125 for i in range(4)]

def cov(x, y, fn):
    return sum(1 for dy in OFFSETS for dx in OFFSETS if fn(x+dx, y+dy)) / 16

# ── Shape helpers ─────────────────────────────────────────────────────────────
def in_ellipse(px, py, cx, cy, rx, ry):
    return ((px-cx)/rx)**2 + ((py-cy)/ry)**2 <= 1.0

def in_circle(px, py, cx, cy, r):
    return (px-cx)**2 + (py-cy)**2 <= r*r

def on_arc(px, py, cx, cy, r, thickness, a_start, a_end):
    """Point on a circular arc between two angles (radians, 0=right, CCW)."""
    dx, dy = px - cx, py - cy
    dist = math.sqrt(dx*dx + dy*dy)
    if not (r - thickness/2 <= dist <= r + thickness/2):
        return False
    angle = math.atan2(-dy, dx)   # flip Y for screen coords
    # Normalize angle to [a_start, a_end]
    while angle < a_start:
        angle += 2*math.pi
    return angle <= a_end

# ── Design parameters ─────────────────────────────────────────────────────────
SIZE = 32

# Sensor patch (large oval, adhesive pad) — bottom-center
PATCH_CX, PATCH_CY = 16.0, 21.0
PATCH_RX, PATCH_RY = 13.0, 10.5

# Sensor body (smaller circle on the patch)
SENS_CX, SENS_CY, SENS_R = 16.0, 21.0, 7.5

# Inner highlight on sensor (glossy)
HI_CX, HI_CY, HI_R = 13.5, 18.5, 3.0

# Signal arcs above sensor — three concentric arcs (like WiFi symbol rotated up)
SIG_CX, SIG_CY = 16.0, 13.5    # arc center point
SIG_A0, SIG_A1 = math.radians(30), math.radians(150)   # arc from 30° to 150°
ARC_CONFIGS = [
    (3.5, 1.3),    # innermost arc: radius, thickness
    (6.0, 1.2),
    (8.5, 1.1),
]

# ── Colors ────────────────────────────────────────────────────────────────────
C_PATCH  = (220, 230, 240)   # light blue-gray patch
C_BORDER = (150, 170, 190)   # patch border (slightly darker)
C_SENSOR = ( 21,  101, 192)  # #1565c0 blue sensor body
C_SENSOR2= ( 13,   71, 161)  # darker ring on sensor edge
C_HI     = (100, 181, 246)   # #64b5f6 highlight
C_ARC    = (  0, 188, 212)   # #00bcd4 teal signal arcs

TRANSP = (0, 0, 0, 0)

def blend(base, col, a):
    if base[3] == 0:
        return (*col, round(a * 255))
    ba = base[3] / 255
    out_a = a + ba * (1 - a)
    if out_a == 0:
        return TRANSP
    r = int((col[0]*a + base[0]*ba*(1-a)) / out_a)
    g = int((col[1]*a + base[1]*ba*(1-a)) / out_a)
    b = int((col[2]*a + base[2]*ba*(1-a)) / out_a)
    return (r, g, b, round(out_a * 255))

# ── Render ────────────────────────────────────────────────────────────────────
pixels = []
for y in range(SIZE):
    for x in range(SIZE):
        px = TRANSP

        # 1. Patch border (slightly larger ellipse)
        c = cov(x, y, lambda a, b: in_ellipse(a, b, PATCH_CX, PATCH_CY, PATCH_RX+0.8, PATCH_RY+0.8))
        if c: px = blend(px, C_BORDER, c)

        # 2. Patch fill
        c = cov(x, y, lambda a, b: in_ellipse(a, b, PATCH_CX, PATCH_CY, PATCH_RX, PATCH_RY))
        if c: px = blend(px, C_PATCH, c)

        # 3. Sensor outer ring (dark border)
        c = cov(x, y, lambda a, b: in_circle(a, b, SENS_CX, SENS_CY, SENS_R))
        if c: px = blend(px, C_SENSOR2, c)

        # 4. Sensor body
        c = cov(x, y, lambda a, b: in_circle(a, b, SENS_CX, SENS_CY, SENS_R - 1.0))
        if c: px = blend(px, C_SENSOR, c)

        # 5. Highlight on sensor
        c = cov(x, y, lambda a, b: in_circle(a, b, HI_CX, HI_CY, HI_R))
        if c: px = blend(px, C_HI, c * 0.6)

        # 6. Signal arcs
        for arc_r, arc_t in ARC_CONFIGS:
            c = cov(x, y, lambda a, b, r=arc_r, t=arc_t: on_arc(a, b, SIG_CX, SIG_CY, r, t, SIG_A0, SIG_A1))
            if c: px = blend(px, C_ARC, c)

        pixels.append(px)

out = pathlib.Path(__file__).parent / 'icon.png'
out.write_bytes(make_png(SIZE, SIZE, pixels))
print(f"Icon created: {out}  ({SIZE}x{SIZE} RGBA)")
