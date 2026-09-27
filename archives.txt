"""
Cross-format helpers layered on top of utils.converter's extraction
logic:

- packaging a page sequence into .cbz (always) or real .cbr (only if a
  `rar` binary is installed -- see config.RAR_BINARY)
- rendering PDF pages back out to images (for PDF -> CBZ/CBR/EPUB)
- pulling video files out of an extracted archive so they can be sent
  as native Telegram videos instead of being silently dropped
"""

import logging
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import List

import config

logger = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}


def images_to_cbz(image_paths: List[str], output_path: str, extra_files: List[str] = None) -> str:
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, img_path in enumerate(image_paths):
            ext = Path(img_path).suffix or ".jpg"
            zf.write(img_path, arcname=f"{i:05d}{ext}")
        for extra in extra_files or []:
            zf.write(extra, arcname=os.path.basename(extra))
    return output_path


def images_to_cbr(image_paths: List[str], output_path: str, work_dir: str) -> str:
    """Best-effort real CBR (RAR) output. Falls back to a .cbz (renamed
    from .cbr won't happen -- caller should switch the extension/tell
    the user) if no `rar` binary is available on this host."""
    if not config.RAR_BINARY:
        raise RuntimeError(
            "No 'rar' binary installed on the server, so a genuine RAR/CBR file "
            "can't be produced here. Falling back to CBZ (ZIP) output, which "
            "every comic reader opens just as well."
        )
    stage_dir = os.path.join(work_dir, "_cbr_stage")
    os.makedirs(stage_dir, exist_ok=True)
    for i, img_path in enumerate(image_paths):
        ext = Path(img_path).suffix or ".jpg"
        shutil.copy(img_path, os.path.join(stage_dir, f"{i:05d}{ext}"))
    try:
        subprocess.run(
            [config.RAR_BINARY, "a", "-ep1", "-m5", output_path, "."],
            cwd=stage_dir,
            check=True,
            capture_output=True,
        )
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
    return output_path


def pdf_to_images(pdf_path: str, out_dir: str, dpi: int = 150) -> List[str]:
    """Renders every page of a PDF to a JPEG, for PDF -> CBZ/CBR/EPUB
    conversion. Requires PyMuPDF (fitz)."""
    import fitz  # PyMuPDF

    os.makedirs(out_dir, exist_ok=True)
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    out_paths = []
    with fitz.open(pdf_path) as doc:
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=matrix)
            out_path = os.path.join(out_dir, f"{i:05d}.jpg")
            pix.save(out_path)
            out_paths.append(out_path)
    return out_paths


def find_videos(extract_dir: str) -> List[str]:
    return [
        str(p)
        for p in Path(extract_dir).rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    ]
