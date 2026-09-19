from __future__ import annotations

from pathlib import Path

from PIL import Image


POSTPROCESS_MODES = {"none", "pdf", "long", "both"}


def build_image_artifacts(
    image_paths: list[Path],
    output_dir: Path,
    base_name: str,
    mode: str,
    *,
    group_size: int = 30,
) -> list[tuple[Path, str]]:
    """Build optional PDF/long-image artifacts in bounded groups for any platform."""
    normalized_mode = str(mode or "none").strip().lower()
    if normalized_mode not in POSTPROCESS_MODES:
        raise ValueError(f"不支持的图片后处理模式：{mode}")
    if normalized_mode == "none" or not image_paths:
        return []
    size = max(1, min(int(group_size), 30))
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[tuple[Path, str]] = []
    for group_index, start in enumerate(range(0, len(image_paths), size), 1):
        paths = image_paths[start:start + size]
        suffix = f"_{group_index:02d}" if len(image_paths) > size else ""
        if normalized_mode in {"pdf", "both"}:
            destination = output_dir / f"{base_name}{suffix}.pdf"
            _save_pdf(paths, destination)
            artifacts.append((destination, "document"))
        if normalized_mode in {"long", "both"}:
            destination = output_dir / f"{base_name}{suffix}_long.jpg"
            _save_long_image(paths, destination)
            artifacts.append((destination, "image"))
    return artifacts


def _save_pdf(paths: list[Path], destination: Path) -> None:
    images: list[Image.Image] = []
    try:
        for path in paths:
            with Image.open(path) as source:
                images.append(source.convert("RGB"))
        if not images:
            return
        images[0].save(destination, "PDF", save_all=True, append_images=images[1:], resolution=100.0)
    finally:
        for image in images:
            image.close()


def _save_long_image(paths: list[Path], destination: Path) -> None:
    images: list[Image.Image] = []
    try:
        for path in paths:
            with Image.open(path) as source:
                images.append(source.convert("RGB"))
        if not images:
            return
        target_width = min(max(image.width for image in images), 2000)
        resized: list[Image.Image] = []
        for image in images:
            if image.width == target_width:
                resized.append(image)
            else:
                height = max(1, round(image.height * target_width / image.width))
                resized.append(image.resize((target_width, height), Image.Resampling.LANCZOS))
        canvas = Image.new("RGB", (target_width, sum(image.height for image in resized)), "white")
        top = 0
        for image in resized:
            canvas.paste(image, (0, top))
            top += image.height
        canvas.save(destination, "JPEG", quality=92, optimize=True)
        canvas.close()
        for image in resized:
            if image not in images:
                image.close()
    finally:
        for image in images:
            image.close()
