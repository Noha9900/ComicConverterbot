"""
Core conversion logic: CBZ/ZIP and CBR/RAR (including multi-volume RAR
sets) -> single PDF.

Kept dependency-free of Telegram entirely, so it can be unit-tested or
reused (e.g. from a CLI) without spinning up the bot.
"""

import logging
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import img2pdf
import rarfile
from PIL import Image, UnidentifiedImageError

from utils import archives, ebook, pdfutil

logger = logging.getLogger(__name__)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}

ZIP_EXTS = {".cbz", ".zip"}
RAR_EXTS = {".cbr", ".rar"}

# Matches the "volume suffix" of a split RAR archive, e.g.:
#   Comic.part1.rar / Comic.part02.rar   -> group key "Comic"
#   Comic.r00 / Comic.r01 / Comic.r99    -> group key "Comic"
_PART_RAR_RE = re.compile(r"^(?P<base>.+)\.part(?P<num>\d+)\.rar$", re.IGNORECASE)
_OLD_RAR_RE = re.compile(r"^(?P<base>.+)\.r(?P<num>\d{2,3})$", re.IGNORECASE)
_FIRST_VOLUME_RE = re.compile(r"^(?P<base>.+)\.(cbr|rar)$", re.IGNORECASE)


class ConversionError(Exception):
    """Raised for any expected, user-facing conversion failure."""


# --------------------------------------------------------------------------
# Multi-volume archive detection
# --------------------------------------------------------------------------

def volume_group_key(filename: str) -> Optional[str]:
    """
    Returns a stable "group key" for any file that is part of a
    multi-volume RAR set (so all its siblings can be collected together
    before extraction), or None if the file is a complete, standalone
    archive on its own.
    """
    m = _PART_RAR_RE.match(filename)
    if m:
        return m.group("base").lower()
    m = _OLD_RAR_RE.match(filename)
    if m:
        return m.group("base").lower()
    return None


def is_first_volume(filename: str) -> bool:
    """True for '<base>.part1.rar', '<base>.rar', or '<base>.cbr' (the
    volume unrar should be pointed at to auto-discover the rest)."""
    m = _PART_RAR_RE.match(filename)
    if m:
        return int(m.group("num")) == 1
    if _OLD_RAR_RE.match(filename):
        return False  # the .rXX scheme's first volume is the plain .rar/.cbr file
    return bool(_FIRST_VOLUME_RE.match(filename))


def is_archive_file(filename: str) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in ZIP_EXTS or ext in RAR_EXTS or volume_group_key(filename) is not None


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def _natural_key(path: Path):
    """Sort '2.jpg' before '10.jpg' the way a human expects."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path.name)]


def _extract_zip(archive_path: str, extract_to: str) -> None:
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(extract_to)
    except zipfile.BadZipFile as e:
        raise ConversionError("This .cbz/.zip file is corrupted or not a valid ZIP archive.") from e


def _extract_rar(archive_path: str, extract_to: str) -> None:
    """
    Extracts a RAR/CBR archive. If sibling volumes (.r00, .part2.rar, ...)
    sit next to `archive_path` on disk, rarfile/unrar discovers and pulls
    them in automatically -- that's the entire multi-volume story.
    """
    try:
        with rarfile.RarFile(archive_path, "r") as rf:
            rf.extractall(extract_to)
    except rarfile.NeedFirstVolume as e:
        raise ConversionError(
            "This looks like a non-first volume of a split archive. Send /convert "
            "only after all parts have been uploaded, or re-send the first volume."
        ) from e
    except rarfile.RarCannotExec as e:
        raise ConversionError(
            "The 'unrar' binary is missing on the server. It must be installed "
            "in the Docker image (see the provided Dockerfile)."
        ) from e
    except rarfile.BadRarFile as e:
        raise ConversionError(
            "This .cbr/.rar file is corrupted, or a required volume is missing."
        ) from e


def extract_archive(archive_path: str, extract_to: str) -> None:
    os.makedirs(extract_to, exist_ok=True)
    ext = Path(archive_path).suffix.lower()
    if ext in ZIP_EXTS:
        _extract_zip(archive_path, extract_to)
    elif ext in RAR_EXTS:
        _extract_rar(archive_path, extract_to)
    else:
        raise ConversionError(f"Unsupported archive extension: {ext}")


def collect_images(folder: str) -> List[Path]:
    images = [p for p in Path(folder).rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    images.sort(key=_natural_key)
    if not images:
        raise ConversionError("No readable images were found inside the archive.")
    return images


def _sanitize_images(images: List[Path], work_dir: str) -> List[str]:
    """
    Normalizes every page to a JPEG img2pdf can always consume.
    Protects against CMYK scans, palette/GIF frames, broken EXIF, etc.
    Unreadable pages are skipped rather than failing the whole book.
    """
    clean_dir = os.path.join(work_dir, "_clean")
    os.makedirs(clean_dir, exist_ok=True)
    clean_paths: List[str] = []

    for i, img_path in enumerate(images):
        try:
            with Image.open(img_path) as im:
                im.load()
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                out_path = os.path.join(clean_dir, f"{i:05d}.jpg")
                im.save(out_path, "JPEG", quality=92)
                clean_paths.append(out_path)
        except (UnidentifiedImageError, OSError) as e:
            logger.warning("Skipping unreadable page %s: %s", img_path, e)

    if not clean_paths:
        raise ConversionError("All images inside the archive failed to decode.")
    return clean_paths


def convert_to_pdf(archive_path: str, work_dir: str, output_pdf_path: str) -> str:
    """
    Extracts a CBZ/CBR/ZIP/RAR (including multi-volume RAR, as long as
    every part sits alongside `archive_path` on disk) and compiles its
    pages into a single PDF at `output_pdf_path`. Intermediate files are
    always cleaned up, even on failure. Returns the output path.

    Kept as-is (simple, fixed defaults) for backward compatibility / the
    original flow. New code should use `convert_with_options` below,
    which wraps the same extraction logic with the full settings pipeline
    (output format, quality, spreads, webtoon slicing, Kindle mode, ...).
    """
    extract_dir = os.path.join(work_dir, "extracted")
    try:
        extract_archive(archive_path, extract_dir)
        images = collect_images(extract_dir)
        clean_images = _sanitize_images(images, work_dir)

        with open(output_pdf_path, "wb") as f:
            f.write(img2pdf.convert(clean_images))

        return output_pdf_path
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
        shutil.rmtree(os.path.join(work_dir, "_clean"), ignore_errors=True)


# --------------------------------------------------------------------------
# Options-driven pipeline: output format, quality, page tools
# --------------------------------------------------------------------------

VALID_OUTPUT_FORMATS = {"pdf", "cbz", "cbr", "epub", "images"}


@dataclass
class ConversionOptions:
    output_format: str = "pdf"          # pdf | cbz | cbr | epub | images
    quality: str = "medium"             # low | medium | high
    split_spreads: bool = False
    webtoon_slice: bool = False
    kindle_optimize: bool = False
    kindle_device: str = "paperwhite"
    reading_direction: str = "ltr"      # ltr | rtl
    add_thumbnail: bool = False
    skip_cover: bool = False
    extract_videos: bool = True
    title: str = ""
    author: str = ""


@dataclass
class ConversionResult:
    output_path: str
    output_format: str
    thumbnail_path: Optional[str] = None
    extra_videos: List[str] = field(default_factory=list)
    warning: Optional[str] = None


def _process_pages(image_paths: List[Path], work_dir: str, opts: ConversionOptions) -> List[str]:
    """Runs the page-level pipeline (spread split -> webtoon slice ->
    kindle/quality) and returns the final ordered list of processed
    (always-JPEG) image paths."""
    stage_dir = os.path.join(work_dir, "_pages")
    os.makedirs(stage_dir, exist_ok=True)
    ordered: List[str] = []
    counter = 0

    for img_path in image_paths:
        try:
            with Image.open(img_path) as im:
                im.load()
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")

                pieces = [im]
                if opts.split_spreads and ebook.is_two_page_spread(im):
                    pieces = ebook.split_spread(im)

                for piece in pieces:
                    if opts.kindle_optimize:
                        piece = ebook.optimize_for_kindle(piece, opts.kindle_device)
                    else:
                        piece = ebook.apply_quality_preset(piece, opts.quality)

                    out_path = os.path.join(stage_dir, f"{counter:05d}.jpg")
                    quality = ebook.preset_jpeg_quality(opts.quality)
                    piece.convert("RGB" if piece.mode != "L" else "L").save(
                        out_path, "JPEG", quality=quality
                    )
                    ordered.append(out_path)
                    counter += 1
        except (UnidentifiedImageError, OSError) as e:
            logger.warning("Skipping unreadable page %s: %s", img_path, e)

    if opts.webtoon_slice:
        sliced_dir = os.path.join(work_dir, "_webtoon")
        sliced: List[str] = []
        for p in ordered:
            sliced.extend(ebook.slice_webtoon(p, sliced_dir))
        ordered = sliced

    if not ordered:
        raise ConversionError("All images inside the archive failed to decode.")

    if opts.reading_direction == "rtl" and opts.output_format in ("pdf",):
        # PDF has no native page-direction flag; for a right-to-left book
        # the pages themselves must already be in RTL reading order coming
        # out of the archive, so we leave ordering untouched here and only
        # record the intent in metadata for formats that support it
        # (CBZ's ComicInfo.xml, EPUB's page-progression-direction).
        pass

    if opts.skip_cover and ordered:
        cover = ebook.detect_cover([Path(p) for p in image_paths])
        if cover is not None:
            # The processed/cover correspondence is positional (cover was
            # always page 0 pre-processing unless spreads/webtoon slicing
            # changed the count) -- safe to drop index 0 when nothing
            # upstream fanned page 0 out into multiple pieces.
            if len(ordered) == len(image_paths):
                ordered = ordered[1:]

    return ordered


def convert_with_options(
    source_path: str,
    work_dir: str,
    output_dir: str,
    output_stem: str,
    opts: ConversionOptions,
    *,
    is_pdf_source: bool = False,
    source_images: Optional[List[Path]] = None,
) -> ConversionResult:
    """
    Full pipeline: extract (or render, if the source is already a PDF)
    -> page-level processing -> package into the requested output
    format. Used for archives, loose-image batches, and PDF-as-source
    (re-processing / reformatting an existing PDF).
    """
    os.makedirs(output_dir, exist_ok=True)
    extract_dir = os.path.join(work_dir, "extracted")
    extra_videos: List[str] = []
    warning: Optional[str] = None

    try:
        if source_images is not None:
            images = source_images
        elif is_pdf_source:
            os.makedirs(extract_dir, exist_ok=True)
            rendered = archives.pdf_to_images(source_path, extract_dir)
            images = [Path(p) for p in rendered]
        else:
            extract_archive(source_path, extract_dir)
            images = collect_images(extract_dir)
            if opts.extract_videos:
                extra_videos = archives.find_videos(extract_dir)

        processed = _process_pages(images, work_dir, opts)

        thumb_path = None
        if opts.add_thumbnail and processed:
            thumb_path = ebook.make_thumbnail(processed[0], os.path.join(work_dir, "thumb.jpg"))

        fmt = opts.output_format if opts.output_format in VALID_OUTPUT_FORMATS else "pdf"

        if fmt == "images":
            # Caller sends `processed` directly; nothing to package.
            final_dir = os.path.join(output_dir, output_stem + "_images")
            os.makedirs(final_dir, exist_ok=True)
            final_paths = []
            for i, p in enumerate(processed):
                dest = os.path.join(final_dir, f"{i:05d}.jpg")
                shutil.copy(p, dest)
                final_paths.append(dest)
            return ConversionResult(output_path=final_dir, output_format="images", thumbnail_path=thumb_path, extra_videos=extra_videos)

        if fmt == "pdf":
            out_path = os.path.join(output_dir, output_stem + ".pdf")
            with open(out_path, "wb") as f:
                f.write(img2pdf.convert(processed))
            if opts.title or opts.author:
                pdfutil.set_metadata(out_path, title=opts.title or None, author=opts.author or None)

        elif fmt == "cbz":
            out_path = os.path.join(output_dir, output_stem + ".cbz")
            meta_dir = os.path.join(work_dir, "_meta")
            os.makedirs(meta_dir, exist_ok=True)
            info_xml = ebook.write_comicinfo_xml(
                meta_dir, title=opts.title or output_stem, manga_rtl=(opts.reading_direction == "rtl")
            )
            archives.images_to_cbz(processed, out_path, extra_files=[info_xml])

        elif fmt == "cbr":
            out_path = os.path.join(output_dir, output_stem + ".cbr")
            try:
                archives.images_to_cbr(processed, out_path, work_dir)
            except RuntimeError as e:
                warning = str(e)
                out_path = os.path.join(output_dir, output_stem + ".cbz")
                meta_dir = os.path.join(work_dir, "_meta")
                os.makedirs(meta_dir, exist_ok=True)
                info_xml = ebook.write_comicinfo_xml(meta_dir, title=opts.title or output_stem)
                archives.images_to_cbz(processed, out_path, extra_files=[info_xml])

        elif fmt == "epub":
            out_path = os.path.join(output_dir, output_stem + ".epub")
            ebook.images_to_epub(
                processed,
                out_path,
                title=opts.title or output_stem,
                author=opts.author or "Unknown",
                reading_direction=opts.reading_direction,
            )
        else:
            raise ConversionError(f"Unsupported output format: {fmt}")

        return ConversionResult(
            output_path=out_path,
            output_format=fmt,
            thumbnail_path=thumb_path,
            extra_videos=extra_videos,
            warning=warning,
        )
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
        shutil.rmtree(os.path.join(work_dir, "_pages"), ignore_errors=True)
        shutil.rmtree(os.path.join(work_dir, "_webtoon"), ignore_errors=True)


def images_only_source(image_paths: List[str]) -> List[Path]:
    """Adapter so a loose batch of uploaded images can go through the
    same `_process_pages` pipeline as an extracted archive."""
    return [Path(p) for p in image_paths]
