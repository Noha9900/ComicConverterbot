"""
PDF-level operations that don't need to touch page images: merging
several PDFs into one, splitting a big PDF back into parts, and
writing title/author metadata. Built on pypdf (pure Python, no system
dependency).
"""

import os
from typing import List, Optional

from pypdf import PdfReader, PdfWriter


def page_count(pdf_path: str) -> int:
    return len(PdfReader(pdf_path).pages)


def merge_pdfs(paths: List[str], output_path: str) -> str:
    writer = PdfWriter()
    for p in paths:
        reader = PdfReader(p)
        for page in reader.pages:
            writer.add_page(page)
    with open(output_path, "wb") as f:
        writer.write(f)
    return output_path


def split_pdf(
    pdf_path: str,
    out_dir: str,
    *,
    num_parts: Optional[int] = None,
    pages_per_part: Optional[int] = None,
) -> List[str]:
    """Split into exactly `num_parts` (as evenly as possible) or into
    chunks of `pages_per_part` pages each. Exactly one should be given."""
    reader = PdfReader(pdf_path)
    total = len(reader.pages)
    if num_parts and num_parts > 0:
        pages_per_part = max(1, -(-total // num_parts))  # ceil division
    elif not pages_per_part or pages_per_part <= 0:
        pages_per_part = total

    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    out_paths = []
    idx, part_no = 0, 1
    while idx < total:
        writer = PdfWriter()
        for page in reader.pages[idx : idx + pages_per_part]:
            writer.add_page(page)
        out_path = os.path.join(out_dir, f"{base}_part{part_no}.pdf")
        with open(out_path, "wb") as f:
            writer.write(f)
        out_paths.append(out_path)
        idx += pages_per_part
        part_no += 1
    return out_paths


def set_metadata(pdf_path: str, *, title: Optional[str] = None, author: Optional[str] = None) -> None:
    reader = PdfReader(pdf_path)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    meta = dict(reader.metadata or {})
    if title:
        meta["/Title"] = title
    if author:
        meta["/Author"] = author
    writer.add_metadata(meta)
    tmp = pdf_path + ".tmp"
    with open(tmp, "wb") as f:
        writer.write(f)
    os.replace(tmp, pdf_path)
