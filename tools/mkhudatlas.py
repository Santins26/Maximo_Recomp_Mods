# Builds the modern HUD's atlas (shaders/native/hud_atlas.h; run: python tools/mkhudatlas.py shaders/native/hud_atlas.h): digits and icons drawn here (Liberation Serif Bold, SIL OFL
# 1.1, for the digits; the icons are plain vector shapes), 512x512, two channels (shade, alpha), run-length coded.
# The renderer tints each piece with its vertex colour, so one grey icon becomes a gold coin or a blue soul.
import sys, math
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W = H = 512
S = 4  # supersampling
lum = np.zeros((H, W), np.float32)
alp = np.zeros((H, W), np.float32)
layout = {}

def put(name, x, y, L, A):
    h, w = A.shape
    lum[y:y + h, x:x + w] = L
    alp[y:y + h, x:x + w] = A
    layout[name] = (x, y, w, h)

def down(img):
    a = np.asarray(img, np.float32) / 255.0
    h, w = a.shape[0] // S, a.shape[1] // S
    return a[:h * S, :w * S].reshape(h, S, w, S).mean(axis=(1, 3))

def canvas(w, h):
    im = Image.new('L', (w * S, h * S), 0)
    return im, ImageDraw.Draw(im)

# ---- digits and signs: shade 1 inside, dark outline, a little lighter at the top
font = ImageFont.truetype('/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf', 50 * S)
glyphs = '0123456789+x/'
for i, ch in enumerate(glyphs):
    cw, chh = 40, 64
    im, d = canvas(cw, chh)
    bb = d.textbbox((0, 0), ch, font=font)
    gx = (cw * S - (bb[2] - bb[0])) // 2 - bb[0]
    gy = (chh * S - (50 * S)) // 2 - int(0.14 * 50 * S) + 4 * S
    if ch == 'x':
        gy += 4 * S
    d.text((gx, gy), ch, font=font, fill=255)
    m = down(im)
    o = down(im.filter(ImageFilter.MaxFilter(4 * S + 1)))
    o = np.maximum(o, m)
    yy = np.linspace(1.0, 0.0, chh)[:, None]
    L = m * (0.78 + 0.22 * yy) + (o - m) * 0.05
    A = np.clip(o * 0.92 + m * 0.08, 0, 1)
    gxp, gyp = (i % 12) * 40, (i // 12) * 64
    put('g' + ch, gxp, gyp, L, A)
# glyph advance widths (in atlas pixels): narrower for 1 and signs
adv = {}
for ch in glyphs:
    bb = font.getbbox(ch)
    adv[ch] = (bb[2] - bb[0]) / S + 6

def shaded(mask_im, light=(-0.6, -0.8), relief=6.0, base=0.55, spec=0.45):
    """Shade from a mask's blurred height: lit from the upper left (an embossed look)."""
    m = down(mask_im)
    hgt = np.asarray(mask_im.filter(ImageFilter.GaussianBlur(relief * S)), np.float32) / 255.0
    hgt = hgt[:m.shape[0] * S, :m.shape[1] * S].reshape(m.shape[0], S, m.shape[1], S).mean(axis=(1, 3))
    gy, gx = np.gradient(hgt)
    nx, ny = -gx * 8, -gy * 8
    nz = np.ones_like(nx)
    l = np.sqrt(nx * nx + ny * ny + nz * nz)
    lx, ly = light
    lz = 0.6
    ll = math.sqrt(lx * lx + ly * ly + lz * lz)
    d = (nx * lx + ny * ly + nz * lz) / (l * ll)
    s = np.clip(base + spec * d, 0, 1.2)
    return m, s

def icon(name, x, y, draw_fn, size=96, outline=True, **kw):
    im, d = canvas(size, size)
    draw_fn(d, size * S)
    m, s = shaded(im, **kw)
    if outline:
        o = down(im.filter(ImageFilter.MaxFilter(3 * S + 1)))
        o = np.maximum(o, m)
        L = m * s + (o - m) * 0.04
        A = np.clip(o, 0, 1)
    else:
        L, A = m * s, m
    put(name, x, y, np.clip(L, 0, 1), A)
    return im, d

def coin(d, n):
    c = n / 2
    r = n * 0.40
    d.ellipse([c - r, c - r, c + r, c + r], fill=255)
def coin_detail(L, A, x, y, n=96):
    # an inner ring and a star stamped in, a little darker
    im, d = canvas(n, n)
    c = n * S / 2
    r1, r2 = n * S * 0.30, n * S * 0.26
    d.ellipse([c - r1, c - r1, c + r1, c + r1], fill=255)
    d.ellipse([c - r2, c - r2, c + r2, c + r2], fill=0)
    pts = []
    for k in range(10):
        a = -math.pi / 2 + k * math.pi / 5
        rr = n * S * (0.17 if k % 2 == 0 else 0.07)
        pts.append((c + rr * math.cos(a), c + rr * math.sin(a)))
    d.polygon(pts, fill=255)
    m = down(im)
    lum[y:y + n, x:x + n] *= 1.0 - 0.35 * m
icon('coin', 0, 128, coin, relief=5.0, base=0.62, spec=0.5)
coin_detail(lum, alp, 0, 128)

def key(d, n):
    k = n / 96
    d.ellipse([10 * k, 30 * k, 46 * k, 66 * k], fill=255)
    d.ellipse([20 * k, 40 * k, 36 * k, 56 * k], fill=0)
    d.rectangle([42 * k, 44 * k, 88 * k, 52 * k], fill=255)
    d.rectangle([70 * k, 52 * k, 76 * k, 66 * k], fill=255)
    d.rectangle([80 * k, 52 * k, 87 * k, 62 * k], fill=255)
icon('key', 96, 128, key, relief=3.0, base=0.6, spec=0.5)

def soul(d, n):
    k = n / 96
    # a flame: a round bottom and a pointed top curling to one side
    d.ellipse([26 * k, 40 * k, 70 * k, 84 * k], fill=255)
    pts = []
    for t in np.linspace(0, 1, 40):
        yv = 62 - t * 56
        w = 22 * (1 - t) ** 0.8
        xc = 48 + 10 * math.sin(t * 3.2) * t
        pts.append((xc * k + w * k, yv * k))
    for t in np.linspace(1, 0, 40):
        yv = 62 - t * 56
        w = 22 * (1 - t) ** 0.8
        xc = 48 + 10 * math.sin(t * 3.2) * t
        pts.append((xc * k - w * k, yv * k))
    d.polygon(pts, fill=255)
im, d = canvas(96, 96)
soul(d, 96 * S)
m = down(im)
core = down(im.filter(ImageFilter.MinFilter(12 * S + 1)).filter(ImageFilter.GaussianBlur(6 * S)))
glow = down(im.filter(ImageFilter.GaussianBlur(7 * S)))
put('soul', 192, 128, np.clip(0.45 * m + 0.7 * core + 0.25 * glow, 0, 1), np.clip(np.maximum(m * 0.85, glow * 0.7), 0, 1))

def helmet(d, n):
    k = n / 96
    d.chord([20 * k, 10 * k, 76 * k, 70 * k], 180, 360, fill=255)
    d.rectangle([20 * k, 39 * k, 76 * k, 70 * k], fill=255)
    d.polygon([(20 * k, 70 * k), (34 * k, 86 * k), (62 * k, 86 * k), (76 * k, 70 * k)], fill=255)
    d.rectangle([26 * k, 44 * k, 70 * k, 50 * k], fill=0)   # the visor slit
    d.rectangle([46 * k, 50 * k, 50 * k, 80 * k], fill=0)   # the nose guard's gap
    d.polygon([(44 * k, 4 * k), (52 * k, 4 * k), (50 * k, 16 * k), (46 * k, 16 * k)], fill=255)  # the crest
icon('helmet', 288, 128, helmet, relief=4.0, base=0.58, spec=0.55)

def shieldpip(d, n):
    k = n / 96
    pts = [(18 * k, 14 * k), (78 * k, 14 * k), (78 * k, 44 * k)]
    for t in np.linspace(0, 1, 20):
        pts.append(((78 - 30 * t) * k, (44 + 42 * math.sin(t * math.pi / 2)) * k))
    for t in np.linspace(1, 0, 20):
        pts.append(((18 + 30 * t) * k, (44 + 42 * math.sin(t * math.pi / 2)) * k))
    pts.append((18 * k, 44 * k))
    d.polygon(pts, fill=255)
icon('shield', 384, 128, shieldpip, relief=5.0, base=0.6, spec=0.5)

def sword(d, n):
    k = n / 96
    d.polygon([(48 * k, 4 * k), (54 * k, 14 * k), (54 * k, 62 * k), (42 * k, 62 * k), (42 * k, 14 * k)], fill=255)
    d.rectangle([28 * k, 62 * k, 68 * k, 68 * k], fill=255)
    d.rectangle([45 * k, 68 * k, 51 * k, 84 * k], fill=255)
    d.ellipse([42 * k, 82 * k, 54 * k, 94 * k], fill=255)
icon('sword', 96, 224, sword, relief=3.0, base=0.6, spec=0.55)

def heart(d, n):
    k = n / 96
    d.ellipse([14 * k, 18 * k, 50 * k, 54 * k], fill=255)
    d.ellipse([46 * k, 18 * k, 82 * k, 54 * k], fill=255)
    d.polygon([(16 * k, 44 * k), (80 * k, 44 * k), (48 * k, 84 * k)], fill=255)
icon('heart', 192, 224, heart, relief=5.0, base=0.6, spec=0.5)

# a soft round glow
yy, xx = np.mgrid[0:96, 0:96]
r = np.sqrt((xx - 47.5) ** 2 + (yy - 47.5) ** 2) / 47.5
put('glow', 0, 224, np.ones((96, 96), np.float32), np.clip(np.exp(-r * r * 3.0) - 0.05, 0, 1) / 0.95)
# a panel: dark glass, soft edges (stretched behind the counters)
yy, xx = np.mgrid[0:64, 0:128]
ex = np.minimum(xx + 0.5, 128 - xx - 0.5) / 20.0
ey = np.minimum(yy + 0.5, 64 - yy - 0.5) / 20.0
e = np.clip(np.minimum(ex, ey), 0, 1)
put('panel', 288, 224, np.full((64, 128), 0.12, np.float32), (e * e * (3 - 2 * e)))
# a bar end cap: a round-ended bar's shape (its middle stretched)
yy, xx = np.mgrid[0:32, 0:64]
def capsule(x, y, w, h):
    rr = h / 2
    cx = np.clip(x, rr, w - rr)
    return np.clip(rr - np.sqrt((x - cx) ** 2 + (y - rr) ** 2) + 0.5, 0, 1)
cap = capsule(xx + 0.5, yy + 0.5, 64, 32)
put('bar', 416, 224, np.ones((32, 64), np.float32), cap)
# solid white
put('white', 496, 496, np.ones((16, 16), np.float32), np.ones((16, 16), np.float32))

# ---- the header
L8 = np.clip(lum * 255 + 0.5, 0, 255).astype(np.uint8).ravel()
A8 = np.clip(alp * 128 + 0.5, 0, 128).astype(np.uint8).ravel()  # the GS's alpha scale
out = []
i, n = 0, len(L8)
while i < n:
    j = i
    while j < n and j - i < 255 and L8[j] == L8[i] and A8[j] == A8[i]:
        j += 1
    out += [j - i, int(L8[i]), int(A8[i])]
    i = j
with open(sys.argv[1], 'w') as f:
    f.write('// Generated by tools/mkhudatlas.py: the modern HUD\'s atlas (512x512, shade + alpha, run-length coded:\n'
            '// count, shade, alpha). Digits from Liberation Serif Bold (SIL Open Font License 1.1).\n#pragma once\n')
    f.write('static const unsigned kHudAtlasW = 512, kHudAtlasH = 512;\n')
    f.write('static const unsigned char kHudAtlasRle[] = {\n')
    for k in range(0, len(out), 32):
        f.write(','.join(str(v) for v in out[k:k + 32]) + ',\n')
    f.write('};\n')
    f.write('struct HudRect { const char *name; float x, y, w, h; };\n')
    f.write('static const HudRect kHudRects[] = {\n')
    for k, (x, y, w, h) in layout.items():
        f.write('    {"%s", %d, %d, %d, %d},\n' % (k, x, y, w, h))
    f.write('};\n')
    f.write('static const float kHudGlyphAdvance[] = {%s}; // "%s"\n' % (', '.join('%.1f' % adv[c] for c in glyphs), glyphs))
Image.fromarray(np.dstack([np.clip(lum * 255, 0, 255), np.clip(lum * 255, 0, 255), np.clip(lum * 255, 0, 255),
                           np.clip(alp * 255, 0, 255)]).astype(np.uint8), 'RGBA').save('/tmp/hud_atlas.png')
print(len(out) // 3, 'runs')
