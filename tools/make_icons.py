"""アイコンを作る（格子マットの上の端材と、そこに敷き詰めた部材）。作ったものは hazaism/static/icons に置いて commit する。

  icon-180.png   iPhone のホーム画面用（角は iPhone が丸めるので四角のまま）
  icon-192.png, icon-512.png   Android のホーム画面用（角を丸める）
  favicon.svg    ブラウザのタブ用

usage: uv run python tools/make_icons.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "hazaism" / "static" / "icons"

# 0〜100 の座標で描く
BG = "#1d2330"
GRID = "#2c3546"
GRID_MAJOR = "#3a4659"
SCRAP = "#cfd6e0"
PART = "#3b82f6"
SCRAP_PTS = [(14, 30), (60, 16), (86, 24), (88, 62), (80, 76), (66, 86), (30, 84), (12, 66)]
RECTS = [(22, 36, 46, 52), (56, 64, 74, 76)]
L_SHAPE = [(52, 30), (76, 30), (76, 40), (62, 40), (62, 58), (52, 58)]
CIRCLE = (36, 68, 9.0, 3.5)          # 中心 x, y, 半径, 穴の半径
RADIUS = 22                          # 角の丸み


def draw_png(size: int, rounded: bool) -> Image.Image:
    ss = 8                                       # 大きく描いてから縮めて、ふちをなめらかにする
    s = size * ss / 100
    img = Image.new("RGBA", (size * ss, size * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    full = (0, 0, size * ss - 1, size * ss - 1)
    if rounded:
        d.rounded_rectangle(full, radius=RADIUS * s, fill=BG)
    else:
        d.rectangle(full, fill=BG)
    for i in range(10, 100, 10):
        col, w = (GRID_MAJOR, 1.2) if i == 50 else (GRID, 0.7)
        d.line([(i * s, 0), (i * s, size * ss)], fill=col, width=max(1, round(w * s)))
        d.line([(0, i * s), (size * ss, i * s)], fill=col, width=max(1, round(w * s)))
    if rounded:                                  # 丸めた角の外に格子がはみ出さないように切り抜く
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(full, radius=RADIUS * s, fill=255)
        img.putalpha(mask)
        d = ImageDraw.Draw(img)
    d.polygon([(x * s, y * s) for x, y in SCRAP_PTS], fill=SCRAP)
    for x0, y0, x1, y1 in RECTS:
        d.rectangle((x0 * s, y0 * s, x1 * s, y1 * s), fill=PART)
    d.polygon([(x * s, y * s) for x, y in L_SHAPE], fill=PART)
    cx, cy, r, rh = CIRCLE
    d.ellipse(((cx - r) * s, (cy - r) * s, (cx + r) * s, (cy + r) * s), fill=PART)
    d.ellipse(((cx - rh) * s, (cy - rh) * s, (cx + rh) * s, (cy + rh) * s), fill=SCRAP)
    return img.resize((size, size), Image.LANCZOS)


def svg() -> str:
    pts = lambda ps: " ".join(f"{x},{y}" for x, y in ps)
    grid = "".join(
        f'<path d="M{i} 0V100M0 {i}H100" stroke="{GRID_MAJOR if i == 50 else GRID}" stroke-width="{1.2 if i == 50 else 0.7}"/>'
        for i in range(10, 100, 10))
    rects = "".join(f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}"/>' for x0, y0, x1, y1 in RECTS)
    cx, cy, r, rh = CIRCLE
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        f'<clipPath id="c"><rect width="100" height="100" rx="{RADIUS}"/></clipPath>'
        f'<g clip-path="url(#c)"><rect width="100" height="100" fill="{BG}"/>{grid}</g>'
        f'<polygon points="{pts(SCRAP_PTS)}" fill="{SCRAP}"/>'
        f'<g fill="{PART}">{rects}<polygon points="{pts(L_SHAPE)}"/><circle cx="{cx}" cy="{cy}" r="{r}"/></g>'
        f'<circle cx="{cx}" cy="{cy}" r="{rh}" fill="{SCRAP}"/>'
        "</svg>\n"
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    draw_png(180, rounded=False).convert("RGB").save(OUT / "icon-180.png", optimize=True)
    for n in (192, 512):
        draw_png(n, rounded=True).save(OUT / f"icon-{n}.png", optimize=True)
    (OUT / "favicon.svg").write_text(svg(), encoding="utf-8")
    print("wrote", ", ".join(sorted(p.name for p in OUT.iterdir())))


if __name__ == "__main__":
    main()
