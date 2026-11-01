from __future__ import annotations

from pathlib import Path
import uuid

from PIL import Image
from .adapter import TaskCancelled
from .file_conflict import media_output, reuse_existing, replace_output
from .task_control import task_cancelled


POSTPROCESS_MODES = {"none", "pdf", "long", "both"}


def build_image_artifacts(
    image_paths: list[Path],
    output_dir: Path,
    base_name: str,
    mode: str,
    *,
    group_size: int = 30,
    file_conflict: str = "reuse",
    cancel_event=None,
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
        _check_cancel(cancel_event)
        paths = image_paths[start:start + size]
        suffix = f"_{group_index:02d}" if len(image_paths) > size else ""
        requested = output_dir / (f"{base_name}{suffix}_long.jpg" if normalized_mode == "long" else f"{base_name}{suffix}.pdf")
        with media_output(requested, file_conflict, cancel_event, related_paths=paths,
                          companions=lambda path: (path.with_name(f"{path.stem}_long.jpg"),) if normalized_mode == "both" else ()) as selected:
            outputs = []
            if normalized_mode in {"pdf", "both"}:
                outputs.append((selected, "document", _save_pdf))
            if normalized_mode in {"long", "both"}:
                destination = selected.with_name(f"{selected.stem}_long.jpg") if normalized_mode == "both" else selected
                outputs.append((destination, "image", _save_long_image))
            if any(path.resolve() in {source.resolve() for source in paths} for path, _kind, _writer in outputs):
                raise ValueError("合成目标与源图片重名，未覆盖源文件")
            reused = [reuse_existing(path, file_conflict) for path, _kind, _writer in outputs]
            staged = []
            try:
                for (destination, _kind, writer), reuse in zip(outputs, reused):
                    _check_cancel(cancel_event)
                    if reuse:
                        continue
                    temporary = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}.convert{destination.suffix}")
                    staged.append((temporary, destination))
                    writer(paths, temporary, cancel_event)
                    if not temporary.is_file() or not temporary.stat().st_size:
                        raise ValueError("合成后未生成有效文件")
                # Generate every requested format before replacing either old artifact.
                _check_cancel(cancel_event)
                for temporary, destination in staged:
                    replace_output(temporary, destination, cancel_event)
                artifacts.extend((path, kind) for path, kind, _writer in outputs)
            finally:
                for temporary, _destination in staged:
                    temporary.unlink(missing_ok=True)
    return artifacts


def _check_cancel(cancel_event):
    if cancel_event is not None and task_cancelled(cancel_event):
        raise TaskCancelled("图片合成已取消")


def _save_pdf(paths: list[Path], destination: Path, cancel_event=None) -> None:
    images: list[Image.Image] = []
    try:
        for path in paths:
            _check_cancel(cancel_event)
            with Image.open(path) as source:
                images.append(source.convert("RGB"))
        if not images:
            return
        _check_cancel(cancel_event)
        images[0].save(destination, "PDF", save_all=True, append_images=images[1:], resolution=100.0)
    finally:
        for image in images:
            image.close()


def _save_long_image(paths: list[Path], destination: Path, cancel_event=None) -> None:
    images: list[Image.Image] = []
    resized: list[Image.Image] = []
    canvas = None
    try:
        for path in paths:
            _check_cancel(cancel_event)
            with Image.open(path) as source:
                images.append(source.convert("RGB"))
        if not images:
            return
        target_width = min(max(image.width for image in images), 2000)
        for image in images:
            _check_cancel(cancel_event)
            if image.width == target_width:
                resized.append(image)
            else:
                height = max(1, round(image.height * target_width / image.width))
                resized.append(image.resize((target_width, height), Image.Resampling.LANCZOS))
        canvas = Image.new("RGB", (target_width, sum(image.height for image in resized)), "white")
        top = 0
        for image in resized:
            _check_cancel(cancel_event)
            canvas.paste(image, (0, top))
            top += image.height
        _check_cancel(cancel_event)
        canvas.save(destination, "JPEG", quality=92, optimize=True)
    finally:
        if canvas is not None:
            canvas.close()
        for image in resized:
            if all(image is not original for original in images):
                image.close()
        for image in images:
            image.close()
