"""Build the multi-resolution Windows icon from the project PNG artwork."""

from __future__ import annotations

from pathlib import Path

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT_ROOT / "蔑视.png"
DESTINATION = PROJECT_ROOT / "packaging" / "欲求达.ico"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def main() -> int:
    with Image.open(SOURCE) as opened:
        if opened.width != opened.height:
            raise SystemExit(f"Icon source must be square, got {opened.width}x{opened.height}")
        image = opened.convert("RGBA")
        image.load()
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    temporary = DESTINATION.with_suffix(".ico.tmp")
    try:
        image.save(temporary, format="ICO", sizes=[(size, size) for size in ICON_SIZES])
        temporary.replace(DESTINATION)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Windows icon created: {DESTINATION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
