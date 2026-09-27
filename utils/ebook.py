"""
Page-image-level tools used by the conversion pipeline:

- quality/size presets
- two-page-spread splitting
- webtoon (tall strip) slicing
- Kindle / e-ink optimization (resize + grayscale)
- thumbnail generation
- EPUB packaging (via ebooklib)
- ComicInfo.xml generation for reading-direction / manga metadata

Kept dependency-free of Telegram and of the archive-extraction code, so
each piece can be tested or reused on its own.
"""

import os
import uuid
from pathlib import Path
from typing import List, Optional

from PIL import Image

QUALITY_PRESETS = {
    # name: (max_dimension_px, jpeg_quality)
    "low": (1000, 60),
    "medium": (1600, 80),
    "high": (2400, 92),
}

KINDLE_SIZES = {
    # common e-ink panel resolutions, portrait (w, h)
    "kindle": (1236, 1648),
    "paperwhite": (1072, 1448),
    "kobo": (1264, 1680),
}


def apply_quality_preset(im: Image.Image, preset: str) -> Image.Image:
    max_dim, _ = QUALITY_PRESETS.get(preset, QUALITY_PRESETS["medium"])
    w, h = im.size
    scale = min(1.0, max_dim / max(w, h))
    if scale < 1.0:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    return im


def preset_jpeg_quality(preset: str) -> int:
    return QUALITY_PRESETS.get(preset, QUALITY_PRESETS["medium"])[1]


def is_two_page_spread(im: Image.Image, ratio: float = 1.15) -> bool:
    """Heuristic: a spread is noticeably wider than it is tall."""
    w, h = im.size
    return w / h >= ratio


def split_spread(im: Image.Image) -> List[Image.Image]:
    """Splits a landscape spread into left/right halves, left-page-first."""
    w, h = im.size
    mid = w // 2
    left = im.crop((0, 0, mid, h))
    right = im.crop((mid, 0, w, h))
    return [left, right]


def optimize_for_kindle(im: Image.Image, device: str = "paperwhite") -> Image.Image:
    target_w, target_h = KINDLE_SIZES.get(device, KINDLE_SIZES["paperwhite"])
    im = im.convert("L")  # e-ink panels are grayscale
    w, h = im.size
    scale = min(target_w / w, target_h / h)
    im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    return im


def slice_webtoon(image_path: str, out_dir: str, slice_height: int = 1600) -> List[str]:
    """Cuts a very tall webtoon-style strip into `slice_height`-px pieces
    (a fixed page height reads far better than one gigantic page in a
    PDF/CBZ viewer). Short images are returned unchanged."""
    os.makedirs(out_dir, exist_ok=True)
    base = Path(image_path).stem
    with Image.open(image_path) as im:
        im.load()
        w, h = im.size
        if h <= slice_height * 1.2:
            out_path = os.path.join(out_dir, f"{base}_000.jpg")
            im.convert("RGB").save(out_path, "JPEG", quality=90)
            return [out_path]

        out_paths = []
        y = 0
        i = 0
        while y < h:
            box = (0, y, w, min(h, y + slice_height))
            piece = im.crop(box).convert("RGB")
            out_path = os.path.join(out_dir, f"{base}_{i:03d}.jpg")
            piece.save(out_path, "JPEG", quality=90)
            out_paths.append(out_path)
            y += slice_height
            i += 1
        return out_paths


def make_thumbnail(image_path: str, out_path: Optional[str] = None, size=(320, 320)) -> str:
    out_path = out_path or (os.path.splitext(image_path)[0] + "_thumb.jpg")
    with Image.open(image_path) as im:
        im = im.convert("RGB")
        im.thumbnail(size, Image.LANCZOS)
        im.save(out_path, "JPEG", quality=85)
    return out_path


def detect_cover(images: List[Path]) -> Optional[Path]:
    """Cover-page detection heuristic: the first image in reading order,
    if it looks like a standalone cover (portrait-ish, not a spread)."""
    if not images:
        return None
    first = images[0]
    try:
        with Image.open(first) as im:
            if not is_two_page_spread(im):
                return first
    except OSError:
        pass
    return None


def write_comicinfo_xml(out_dir: str, *, title: str = "", manga_rtl: bool = False) -> str:
    """Writes a ComicInfo.xml (the de-facto CBZ metadata standard most
    comic readers understand) so reading direction / title survive as
    real metadata rather than just a filename."""
    manga_tag = "YesAndRightToLeft" if manga_rtl else "No"
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        f"  <Title>{_xml_escape(title)}</Title>\n"
        f"  <Manga>{manga_tag}</Manga>\n"
        "</ComicInfo>\n"
    )
    path = os.path.join(out_dir, "ComicInfo.xml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(xml)
    return path


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def images_to_epub(
    image_paths: List[str],
    output_path: str,
    *,
    title: str = "Untitled",
    author: str = "Unknown",
    reading_direction: str = "ltr",
) -> str:
    """Packs a page-image sequence into a fixed-layout-ish EPUB (one
    image per XHTML page) via ebooklib. Good enough for e-readers that
    handle EPUB better than PDF (better reflow / lighter file)."""
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier(str(uuid.uuid4()))
    book.set_title(title)
    book.set_language("en")
    book.add_author(author)
    if reading_direction == "rtl":
        book.set_direction("rtl")

    chapters = []
    for i, img_path in enumerate(image_paths):
        img_name = f"images/page_{i:05d}.jpg"
        with open(img_path, "rb") as f:
            book.add_item(
                epub.EpubItem(
                    uid=f"img_{i}", file_name=img_name, media_type="image/jpeg", content=f.read()
                )
            )

        chapter = epub.EpubHtml(title=f"Page {i + 1}", file_name=f"page_{i:05d}.xhtml", lang="en")
        chapter.content = f'<html><body><img src="{img_name}" style="max-width:100%;"/></body></html>'
        book.add_item(chapter)
        chapters.append(chapter)

    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = (["nav"] + chapters) if reading_direction != "rtl" else list(reversed(chapters))

    epub.write_epub(output_path, book)
    return output_path
