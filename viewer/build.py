"""Build the desktop app: the icon from the logo, then one Windows executable (PyInstaller).

    python build.py               dist/laop-view.exe
    python build.py icon          only assets/laop.ico and assets/laop.png

Needs ``pip install pyinstaller pillow pywebview``. The icon is the "a" of the logo on its rounded
square (logo.pdf, "01 · mezzo gambo"), drawn at 8x and scaled down for every size of the .ico.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
INK = (15, 18, 23, 255)
WHITE = (255, 255, 255, 255)
BLUE = (59, 108, 255, 255)
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def draw(size: int):
    """The symbol at ``size`` pixels, proportions measured on the logo (square of 373 units)."""
    from PIL import Image, ImageDraw

    k = 8
    s = size * k
    u = s / 373
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=round(85 * u), fill=INK)
    cx, cy, outer, inner = 176 * u, 187 * u, 77 * u, 48.5 * u
    d.ellipse((cx - outer, cy - outer, cx + outer, cy + outer), fill=WHITE)
    d.ellipse((cx - inner, cy - inner, cx + inner, cy + inner), fill=INK)
    d.rectangle((cx + inner, cy, cx + outer, cy + outer), fill=BLUE)
    return img.resize((size, size), Image.LANCZOS)


def make_icon() -> None:
    ASSETS.mkdir(exist_ok=True)
    big = draw(256)
    big.save(ASSETS / "laop.png")
    big.save(ASSETS / "laop.ico", sizes=[(n, n) for n in SIZES],
             append_images=[draw(n) for n in SIZES if n != 256])


def main(argv: list[str]) -> int:
    make_icon()
    if argv[:1] == ["icon"]:
        return 0
    sep = ";" if sys.platform == "win32" else ":"
    return subprocess.call([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", "laop-view", "--icon", str(ASSETS / "laop.ico"),
        "--add-data", f"{HERE / 'viewer.html'}{sep}.",
        "--add-data", f"{ASSETS / 'laop.png'}{sep}assets",
        "--distpath", str(HERE / "dist"), "--workpath", str(HERE / "build"), "--specpath", str(HERE / "build"),
        str(HERE / "app.py"),
    ], cwd=HERE)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
