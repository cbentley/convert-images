#!/usr/bin/env python3
"""
Convert folders of images into OCR-processed, size-limited PDFs and/or JPEGs.

Usage:
    python convert_images.py pdf <folder> [options]
    python convert_images.py jpg <folder>

See README.md for full documentation.
"""


import argparse
import datetime
import io
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

import img2pdf
import pikepdf
from PIL import Image


# ============================================================
# CONFIG
# ============================================================

# --- Input selection ---
ALLOWED_EXT = {".tif", ".tiff", ".jpg", ".jpeg", ".jp2", ".jpf"}
# Images whose filenames contain any of these words (case-insensitive) are skipped
EXCLUDED_WORDS = {"ruler"}
# Folder names that are too generic to name the output; use the parent's name instead
GENERIC_FOLDER_NAMES = {"master"}
# Output folders created inside each image folder. Folders with these names are never searched for images.
PDF_DIR_NAME = "pdf"
JPG_DIR_NAME = "jpg"

# --- ImageMagick (encodes each image as a JPEG; for PDFs, img2pdf wraps it into a page without re-encoding) ---
# "magick" on ImageMagick 7 (where "convert" is deprecated), "convert" on ImageMagick 6
IMAGEMAGICK_CMD = "magick" if shutil.which("magick") else "convert"
# Read settings, placed before the input file.
# "-limit thread 1" keeps each process single-threaded, since CONVERT_JOBS already runs one per core.
# Outputs keep the scanned object's real size (original pixels / DPI). Uncomment "-density" to treat every image
# as 300 DPI instead; "-units" makes it also apply to images whose DPI is in pixels/cm or missing.
IMAGEMAGICK_INPUT_OPTS = [
    "-limit", "thread", "1",
    # "-density", "300", "-units", "PixelsPerInch",
]
# DPI assumed for PDF page size when an image doesn't store one and -density is not set
FALLBACK_DPI = 300
# sRGB ICC profile. Images with an embedded profile (e.g. Adobe RGB) are converted to sRGB;
# untagged RGB images are tagged as sRGB without changing pixels; untagged gray/CMYK are left as is.
# Ubuntu: from the icc-profiles-free package. macOS: "/System/Library/ColorSync/Profiles/sRGB Profile.icc"
SRGB_PROFILE = "/usr/share/color/icc/sRGB.icc"
# Number of images converted in parallel. Lower it if large TIFFs exhaust memory.
CONVERT_JOBS = os.cpu_count() or 1

# --- Image size and JPEG quality, per output ---
# *_MAX_SIZE: scale images down so neither side exceeds this many pixels (None = keep full size).
# *_QUALITY: JPEG quality 1-100. ImageMagick halves color resolution (4:2:0) below 90.
PDF_MAX_SIZE = None
PDF_QUALITY = 75
JPG_MAX_SIZE = None
JPG_QUALITY = 90

# --- OCRmyPDF ---
OCR_CMD = "ocrmypdf"
# Languages used when --ocr-lang isn't given
DEFAULT_OCR_LANG = "deu+eng"
OCR_OPTS = [
    "--optimize", "3",  # Select 3 for general use, 1 for higher quality (and larger files). 2 and 3 re-encode page images.
    #"--deskew",  # Re-encodes page images
    #"--rotate-pages",  # Use for text; lossless (only sets page rotation)
    "--output-type", "pdf",  # Plain PDF; the default (PDF/A) re-encodes page images
]

# ============================================================
# LOGGING SETUP
# ============================================================

logger = logging.getLogger("convert_images")

# Inside the input folder; starts with "_" so it's never searched for images
LOGS_DIR_NAME = "_logs"


def setup_logging(input_folder: Path) -> Path:
    """Log to the console and to a new timestamped file in input_folder/_logs; return the log file's path."""
    logs_dir = input_folder / LOGS_DIR_NAME
    logs_dir.mkdir(exist_ok=True)
    log_file = logs_dir / f"{datetime.datetime.now():%Y%m%d_%H%M%S}_log.txt"
    logger.setLevel(logging.INFO)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return log_file


# ============================================================
# VALIDATION
# ============================================================

def validate_external_tools(need_ocrmypdf: bool) -> None:
    """Verify ImageMagick, the sRGB profile and, for PDFs, OCRmyPDF are available (it runs even with --no-ocr)."""
    tools = {f"ImageMagick ({IMAGEMAGICK_CMD})": [IMAGEMAGICK_CMD, "--version"]}
    if need_ocrmypdf:
        tools["OCRmyPDF"] = [OCR_CMD, "--version"]

    for name, cmd in tools.items():
        try:
            subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True
            )
            logger.info(f"{name} found.")
        except Exception:
            logger.error(f"{name} is missing or not working. Check installation.")
            raise

    if not Path(SRGB_PROFILE).is_file():
        logger.error(f"sRGB profile not found: {SRGB_PROFILE}. Check SRGB_PROFILE.")
        raise FileNotFoundError(SRGB_PROFILE)
    logger.info(f"sRGB profile found: {SRGB_PROFILE}")


# ============================================================
# IMAGE SELECTION & NAMING
# ============================================================

def is_allowed_ext(path: Path) -> bool:
    """True if path has an allowed image extension."""
    return path.suffix.lower() in ALLOWED_EXT


def is_excluded(path: Path) -> bool:
    """True if the filename contains any word in EXCLUDED_WORDS."""
    name = path.stem.lower()
    return any(word in name for word in EXCLUDED_WORDS)


def is_wanted_image(path: Path) -> bool:
    """True if path has an allowed extension and no excluded word in its name."""
    return is_allowed_ext(path) and not is_excluded(path)


def natural_sort_key(path: Path) -> tuple:
    """Sort key ordering filenames like Windows Explorer: numbers by value (2 before 10), case ignored.
    The full name breaks ties between names that differ only in padding or case (p_1 vs p_01)."""
    # re.split with a capture group alternates text and digit runs, so odd indexes are always digits
    parts = re.split(r"(\d+)", path.name)
    return ([int(t) if i % 2 else t.lower() for i, t in enumerate(parts)], path.name)


def select_images(folder: Path) -> list[Path]:
    """Return the folder's wanted images in natural order, logging excluded ones."""
    candidates = [p for p in folder.iterdir() if is_allowed_ext(p)]
    images = sorted((p for p in candidates if not is_excluded(p)), key=natural_sort_key)
    skipped = sorted(p.name for p in candidates if is_excluded(p))
    if skipped:
        logger.info(f"  Skipped {len(skipped)} excluded image(s): {', '.join(skipped)}")
    return images


class FolderError(Exception):
    """A problem with a folder's contents that fails the folder; logged as a plain message, without a traceback."""


def jpg_name(image: Path) -> str:
    """Output filename of an image's standalone JPEG."""
    return f"{image.stem}.jpg"


def check_jpg_name_clashes(images: list[Path]) -> None:
    """Raise if two images would get the same JPEG name (e.g. a.tif and a.jpg), instead of one overwriting the other.
    Compared case-insensitively, since Windows folders (/mnt/c in WSL) don't distinguish case."""
    by_name: dict[str, list[str]] = {}
    for image in images:
        by_name.setdefault(jpg_name(image).lower(), []).append(image.name)
    clashes = [names for names in by_name.values() if len(names) > 1]
    if clashes:
        raise FolderError(
            "These images would get the same JPEG name: " + "; ".join(" and ".join(names) for names in clashes)
        )


# ============================================================
# IMAGE CONVERSION (JPEG, and single-page PDF)
# ============================================================

# ICC profile color space (header bytes 16-20) expected for each JPEG mode
ICC_COLOR_SPACES = {"L": b"GRAY", "RGB": b"RGB ", "CMYK": b"CMYK"}


class ImageInfo(NamedTuple):
    width: int
    height: int
    x_dpi: float
    y_dpi: float
    has_dpi: bool  # False if the file stores no DPI (x_dpi/y_dpi are then ImageMagick's placeholder 72)
    frames: int    # Images in the file (multi-page TIFFs, Photoshop layers); only the first is used


def read_image_info(image_path: Path) -> ImageInfo:
    """Read size and DPI of the first image in a file from its header (-ping doesn't decode pixels).
    IMAGEMAGICK_INPUT_OPTS apply, so an uncommented -density overrides the stored DPI.
    ImageMagick reports 72 DPI with units "Undefined" when the file stores no DPI."""
    cmd = [
        IMAGEMAGICK_CMD,
        *IMAGEMAGICK_INPUT_OPTS,
        "-ping",
        str(image_path),
        "-format", "%w %h %x %y %U\n",  # One line per image in the file
        "info:",
    ]
    result = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = result.stdout.splitlines()
    width, height, x_dpi, y_dpi, units = lines[0].split()
    x_dpi, y_dpi = float(x_dpi), float(y_dpi)
    if units == "PixelsPerCentimeter":
        x_dpi, y_dpi = x_dpi * 2.54, y_dpi * 2.54
    has_dpi = units in ("PixelsPerInch", "PixelsPerCentimeter") and x_dpi > 0 and y_dpi > 0
    return ImageInfo(int(width), int(height), x_dpi, y_dpi, has_dpi, len(lines))


def strip_mismatched_icc(jpeg: bytes) -> bytes:
    """Remove the ICC profile from a JPEG if it doesn't match the image's color space.
    ImageMagick's -profile tags untagged gray/CMYK images with the sRGB profile without converting them;
    viewers and img2pdf would apply that RGB profile to a CMYK image, giving wrong colors. Pixel data is unchanged."""
    with Image.open(io.BytesIO(jpeg)) as im:
        icc = im.info.get("icc_profile")
        if not icc or icc[16:20] == ICC_COLOR_SPACES.get(im.mode):
            return jpeg
    out = bytearray(jpeg[:2])  # SOI marker
    i = 2
    while i < len(jpeg):
        marker = jpeg[i + 1]
        if marker == 0xDA:  # Start of scan: the rest is image data
            out += jpeg[i:]
            break
        segment = jpeg[i:i + 2 + int.from_bytes(jpeg[i + 2:i + 4], "big")]
        if not (marker == 0xE2 and segment[4:16] == b"ICC_PROFILE\0"):
            out += segment
        i += len(segment)
    return bytes(out)


def encode_jpeg(image_path: Path, info: ImageInfo, jpeg_path: Path, max_size: int | None, quality: int,
                keep_real_size: bool) -> None:
    """Encode the first image of a file as an sRGB JPEG, scaled down so neither side exceeds max_size (if set).
    With keep_real_size, the JPEG's DPI is scaled along with the pixels, so it keeps the original's real size;
    images without a stored DPI are left without one."""
    scale = 1.0
    if max_size and max(info.width, info.height) > max_size:
        scale = max_size / max(info.width, info.height)
    cmd = [
        IMAGEMAGICK_CMD,
        *IMAGEMAGICK_INPUT_OPTS,
        f"{image_path}[0]",  # First image only (multi-page TIFFs, Photoshop layers)
        "-profile", SRGB_PROFILE,  # Before -resize, so pixels are converted before resizing and encoding
        *(["-resize", f"{max_size}x{max_size}>"] if scale < 1 else []),
        *(["-units", "PixelsPerInch", "-density", f"{info.x_dpi * scale:.4f}x{info.y_dpi * scale:.4f}"]
          if keep_real_size and info.has_dpi else []),
        "-quality", str(quality),
        str(jpeg_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    jpeg = jpeg_path.read_bytes()
    stripped = strip_mismatched_icc(jpeg)
    if stripped is not jpeg:
        jpeg_path.write_bytes(stripped)


def page_size_points(info: ImageInfo) -> tuple[float, float]:
    """PDF page size in points: the original image's real size (pixels / DPI, FALLBACK_DPI if it has none)."""
    x_dpi, y_dpi = (info.x_dpi, info.y_dpi) if info.has_dpi else (FALLBACK_DPI, FALLBACK_DPI)
    return info.width / x_dpi * 72, info.height / y_dpi * 72


def convert_image(image_path: Path, page_pdf: Path | None, jpg_path: Path | None) -> None:
    """Convert one image into a one-page PDF (page_pdf) and/or a standalone JPEG (jpg_path).
    PDF pages: ImageMagick encodes a JPEG and img2pdf wraps it without re-encoding. (ImageMagick's own PDF writer
    is blocked by the default security policy on Ubuntu 22.04, and adds a thumbnail and, for images with an
    alpha channel, an opaque soft mask to every page.)"""
    info = read_image_info(image_path)
    if info.frames > 1:
        logger.warning(f"  {image_path.name} contains {info.frames} images; using only the first")

    if page_pdf:
        page_jpeg = page_pdf.with_suffix(".jpg")
        encode_jpeg(image_path, info, page_jpeg, PDF_MAX_SIZE, PDF_QUALITY, keep_real_size=False)
        page_size = page_size_points(info)
        # Rotation.none: ignore EXIF orientation, so the page shows the pixels as stored
        page_pdf.write_bytes(img2pdf.convert(
            page_jpeg.read_bytes(),
            rotation=img2pdf.Rotation.none,
            layout_fun=lambda *_: (*page_size, *page_size),  # Image fills a page of the original's size
        ))
        page_jpeg.unlink()

    if jpg_path:
        encode_jpeg(image_path, info, jpg_path, JPG_MAX_SIZE, JPG_QUALITY, keep_real_size=True)


def convert_all_images(images: list[Path], single_pages_dir: Path | None, jpg_dir: Path | None) -> list[Path]:
    """Convert images into single-page PDFs in single_pages_dir and/or JPEGs in jpg_dir (either may be None;
    both are created). Returns the page PDFs in page order (empty if single_pages_dir is None)."""
    for d in (single_pages_dir, jpg_dir):
        if d:
            d.mkdir(parents=True, exist_ok=True)
    # Zero-padded so the pages also sort correctly by name
    page_pdfs = [single_pages_dir / f"page{idx:06d}.pdf" for idx in range(len(images))] \
        if single_pages_dir else [None] * len(images)
    jpgs = [jpg_dir / jpg_name(img) for img in images] if jpg_dir else [None] * len(images)

    with ThreadPoolExecutor(max_workers=CONVERT_JOBS) as executor:
        futures = [
            executor.submit(convert_image, img, page_pdf, jpg_path)
            for img, page_pdf, jpg_path in zip(images, page_pdfs, jpgs)
        ]
        try:
            for future in futures:
                future.result()
        except BaseException:
            # Don't start queued conversions after a failure or Ctrl-C
            executor.shutdown(cancel_futures=True)
            raise

    outputs = [name for name, wanted in (("PDF pages", single_pages_dir), ("JPEGs", jpg_dir)) if wanted]
    logger.info(f"  Converted {len(images)} images to {' and '.join(outputs)}")
    return page_pdfs if single_pages_dir else []


# ============================================================
# PDF: MERGE (PRE-OCR), OCR, SPLIT (POST-OCR)
# ============================================================

def size_mb(path: Path) -> float:
    """Return the size of a file in megabytes."""
    return path.stat().st_size / 1_000_000


def merge_pdfs(pdf_files: list[Path], output_pdf: Path) -> Path:
    """Merge pdf_files, in order, into a single PDF at output_pdf."""
    with pikepdf.Pdf.new() as merged:
        for pdf in pdf_files:
            with pikepdf.open(pdf) as src:
                merged.pages.extend(src.pages)
        merged.save(output_pdf)

    logger.info(f"  Merged {len(pdf_files)} PDFs into {output_pdf.name}")
    return output_pdf


def ocr_pdf(input_pdf: Path, output_pdf: Path, ocr_lang: str | None) -> None:
    """
    Run OCRmyPDF on a PDF, writing the result to output_pdf.

    With ocr_lang None, text recognition is skipped ("--tesseract-timeout 0") but the
    rest of OCR_OPTS (e.g. --optimize) still applies. "-l" is then left out, so OCRmyPDF
    only checks for English, which comes with Tesseract.
    Raises RuntimeError if OCRmyPDF fails (there is no fallback).
    """
    no_ocr = ocr_lang is None
    cmd = [
        OCR_CMD,
        *OCR_OPTS,
        *(["--tesseract-timeout", "0"] if no_ocr else ["-l", ocr_lang]),
        str(input_pdf),
        str(output_pdf),
    ]

    action = "Optimizing (no OCR)" if no_ocr else "OCRing"
    logger.info(f"  {action} {input_pdf.name} -> {output_pdf.name}...")

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:
        logger.error(f"OCRmyPDF error output for {input_pdf.name}:\n{result.stderr}")
        raise RuntimeError(
            f"OCR failed for {input_pdf.name} (OCRmyPDF exit code {result.returncode})"
        )

    logger.info(f"  Finished {'optimizing' if no_ocr else 'OCR'}: {output_pdf.name}")


def page_object_sizes(page: pikepdf.Page) -> dict[tuple[int, int], int]:
    """
    Approximate the saved size in bytes of each indirect object a page uses
    (content streams, images, fonts), keyed by object id so objects shared
    between pages can be counted once.
    """
    sizes: dict[tuple[int, int], int] = {}
    stack = [page.obj]
    while stack:
        obj = stack.pop()
        if not isinstance(obj, pikepdf.Object):
            continue  # numbers and booleans come back as plain Python values
        if obj.is_indirect:
            if obj.objgen in sizes:
                continue
            if isinstance(obj, pikepdf.Stream):
                size = len(obj.read_raw_bytes()) + len(obj.stream_dict.unparse())
            else:
                size = len(obj.unparse(resolved=True))
            sizes[obj.objgen] = size
        if isinstance(obj, pikepdf.Stream):
            obj = obj.stream_dict
        if isinstance(obj, pikepdf.Dictionary):
            # /Parent leads back up to the page tree and every other page
            stack.extend(v for k, v in obj.items() if k != "/Parent")
        elif isinstance(obj, pikepdf.Array):
            stack.extend(obj)
    return sizes


def save_page_range(src: pikepdf.Pdf, start: int, end: int, path: Path) -> float:
    """Save pages [start, end) of src as a new PDF at path; return its size in MB."""
    with pikepdf.Pdf.new() as pdf:
        pdf.pages.extend(src.pages[start:end])
        pdf.save(path)
    return size_mb(path)


def split_pdf_by_size(input_pdf: Path, output_dir: Path, max_mb: int) -> list[Path]:
    """
    Split a PDF into parts of at most max_mb, each holding as many pages as fit.

    Pages per part are first estimated from page_object_sizes, then corrected
    with real saves: pages are removed while the part is over max_mb and added
    while it still fits. This gives the same parts as adding and saving one
    page at a time, with only a few saves per part. A page alone in its part
    stays there even if oversized (it can't be split).

    Output files are named "<stem>-part01.pdf", "<stem>-part02.pdf", etc.
    """
    part_pdfs: list[Path] = []
    max_bytes = max_mb * 1_000_000

    with pikepdf.open(input_pdf) as src:
        page_sizes = [page_object_sizes(page) for page in src.pages]
        page_count = len(page_sizes)

        start = 0
        while start < page_count:
            # Estimate: add pages while the part's distinct objects fit (at least one page)
            end = start
            seen: set[tuple[int, int]] = set()
            estimate = 0
            while end < page_count:
                new = {k: v for k, v in page_sizes[end].items() if k not in seen}
                if end > start and estimate + sum(new.values()) > max_bytes:
                    break
                seen.update(new)
                estimate += sum(new.values())
                end += 1

            part_pdf = output_dir / f"{input_pdf.stem}-part{len(part_pdfs) + 1:02d}.pdf"
            trial_pdf = part_pdf.with_suffix(".trial.pdf")
            size_mb = save_page_range(src, start, end, part_pdf)

            # Estimate too high: remove pages until the part fits
            while size_mb > max_mb and end - start > 1:
                end -= 1
                size_mb = save_page_range(src, start, end, part_pdf)

            # Estimate too low: add pages while the part still fits
            while size_mb <= max_mb and end < page_count:
                trial_mb = save_page_range(src, start, end + 1, trial_pdf)
                if trial_mb > max_mb:
                    trial_pdf.unlink()
                    break
                trial_pdf.replace(part_pdf)
                end += 1
                size_mb = trial_mb

            if size_mb > max_mb:
                logger.warning(
                    f"  Page {start + 1} alone is {size_mb:.1f} MB (> {max_mb} MB); "
                    f"keeping it as an oversized part"
                )
            logger.info(f"  Finalized {part_pdf.name} ({size_mb:.1f} MB, {end - start} pages)")
            part_pdfs.append(part_pdf)
            start = end

    return part_pdfs


# ============================================================
# COMMAND LINE
# ============================================================

def parse_args(argv=None) -> argparse.Namespace:
    """Parse the command line. Sets make_pdf and make_jpg for either subcommand; exits if the folder doesn't exist."""
    parser = argparse.ArgumentParser(
        description="Convert folders of scanned images to OCR'd PDFs and/or JPEGs."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    pdf_parser = subparsers.add_parser(
        "pdf",
        help="Make an OCR'd PDF per image folder, in its pdf folder",
        description="Make an OCR'd, size-limited PDF per image folder, written to a pdf folder inside it.",
    )
    pdf_parser.add_argument("folder")
    pdf_parser.add_argument(
        "--max-mb",
        type=int,
        default=100,
        help="Maximum size in MB for each final PDF before splitting. Default: %(default)s"
    )
    pdf_parser.add_argument(
        "--ocr-lang",
        help=f'Languages to use for OCR. Examples: "eng", "deu", "eng+deu". Default: {DEFAULT_OCR_LANG}'
    )
    pdf_parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="Skip text recognition. OCRmyPDF still runs, so OCR_OPTS such as --optimize still apply."
    )
    pdf_parser.add_argument(
        "--jpg",
        dest="make_jpg",
        action="store_true",
        help="Also save each image as a JPEG in a jpg folder inside its image folder."
    )
    pdf_parser.set_defaults(make_pdf=True)

    jpg_parser = subparsers.add_parser(
        "jpg",
        aliases=["jpeg"],
        help="Save each image as a JPEG, in a jpg folder inside its image folder",
        description="Save each image as a JPEG, written to a jpg folder inside its image folder.",
    )
    jpg_parser.add_argument("folder")
    jpg_parser.set_defaults(make_pdf=False, make_jpg=True)

    args = parser.parse_args(argv)
    if not Path(args.folder).is_dir():
        parser.error(f"folder not found: {args.folder}")
    # Left as None with --no-ocr, so main() can tell if a given --ocr-lang is ignored
    if args.make_pdf and not args.no_ocr and args.ocr_lang is None:
        args.ocr_lang = DEFAULT_OCR_LANG
    return args


# ============================================================
# OUTPUT FILES
# ============================================================

def output_base_name(folder: Path) -> str:
    """Return the base name for a folder's final PDF(s)."""
    if folder.name.lower() in GENERIC_FOLDER_NAMES:
        return folder.parent.name
    return folder.name


def is_output_dir(name: str) -> bool:
    """True for the script's own output folders, which are never searched for images."""
    return name.lower() in (PDF_DIR_NAME.lower(), JPG_DIR_NAME.lower())


def move_outputs(files: list[Path], dest_dir: Path) -> None:
    """Move finished files from _work into dest_dir, replacing files with the same name."""
    dest_dir.mkdir(exist_ok=True)
    for file in files:
        file.replace(dest_dir / file.name)


def remove_part_pdfs(pdf_dir: Path, base_name: str) -> None:
    """After a failure, remove the folder's split PDFs (<base_name>-partNN.pdf) from pdf_dir, including those
    from earlier runs, so no mixed set of parts is left."""
    for pdf in pdf_dir.glob(f"{base_name}-part*.pdf"):
        try:
            pdf.unlink()
            logger.info(f"  Removed split PDF: {pdf.name}")
        except Exception:
            logger.exception(f"  Failed to remove split PDF: {pdf.name}")


# ============================================================
# FOLDER PROCESSING
# ============================================================

# Inside each image folder; starts with "_" so it's never searched for images
WORK_DIR_NAME = "_work"


def find_image_folders(input_folder: Path) -> Iterator[Path]:
    """Yield input_folder and every folder below it that directly contains wanted images.
    Folders starting with "_" (work and log folders) and the output folders are skipped, with their subfolders."""
    for root, dirs, files in os.walk(input_folder):
        dirs[:] = [d for d in dirs if not d.startswith("_") and not is_output_dir(d)]
        if any(is_wanted_image(Path(f)) for f in files):
            yield Path(root)


def make_final_pdfs(page_pdfs: list[Path], work_dir: Path, base_name: str, args: argparse.Namespace) -> list[Path]:
    """Merge the page PDFs, OCR the result and split it if it's over --max-mb (steps 2-4).
    Returns the final PDFs, still in work_dir."""
    pre_ocr_dir = work_dir / "02_pre_ocr"
    ocr_dir = work_dir / "03_ocr"
    split_dir = work_dir / "04_split"
    for d in (pre_ocr_dir, ocr_dir, split_dir):
        d.mkdir(exist_ok=True)

    # 2. Merge all pages into one PDF, so a single OCRmyPDF run can use every core
    pre_ocr_pdf = merge_pdfs(page_pdfs, pre_ocr_dir / f"{base_name}.pdf")

    # 3. OCR (with --no-ocr, only the rest of OCR_OPTS applies)
    ocr_output = ocr_dir / f"{base_name}.pdf"
    ocr_pdf(pre_ocr_pdf, ocr_output, None if args.no_ocr else args.ocr_lang)

    # 4. Split if over --max-mb
    ocr_size_mb = size_mb(ocr_output)
    if ocr_size_mb <= args.max_mb:
        logger.info(f"  PDF {ocr_output.name} is {ocr_size_mb:.1f} MB (<= {args.max_mb} MB); no splitting needed.")
        return [ocr_output]
    logger.info(f"  PDF {ocr_output.name} is {ocr_size_mb:.1f} MB (>{args.max_mb} MB); splitting...")
    return split_pdf_by_size(ocr_output, split_dir, args.max_mb)


def convert_folder(folder: Path, args: argparse.Namespace) -> None:
    """Convert one image folder's images into its pdf and/or jpg folder. Intermediate files go to folder/_work;
    outputs are moved out only after every step succeeded. Raises on failure."""
    images = select_images(folder)
    if not images:
        # Only if the images vanished since find_image_folders listed them
        logger.warning("  No valid images found.")
        return
    logger.info(f"  Found {len(images)} images")
    if args.make_jpg:
        check_jpg_name_clashes(images)

    # 1. Convert each image into a single-page PDF and/or a JPEG
    work_dir = folder / WORK_DIR_NAME
    jpg_work_dir = work_dir / "jpg"
    page_pdfs = convert_all_images(
        images,
        work_dir / "01_single_pages" if args.make_pdf else None,
        jpg_work_dir if args.make_jpg else None,
    )

    # 2-4. Merge, OCR and split
    base_name = output_base_name(folder)
    final_pdfs = make_final_pdfs(page_pdfs, work_dir, base_name, args) if args.make_pdf else []

    # 5. Move the outputs into the pdf and jpg folders
    if args.make_pdf:
        pdf_dir = folder / PDF_DIR_NAME
        move_outputs(final_pdfs, pdf_dir)
        for pdf in final_pdfs:
            logger.info(f"  Final output written: {pdf_dir / pdf.name}")
    if args.make_jpg:
        jpg_dir = folder / JPG_DIR_NAME
        move_outputs(sorted(jpg_work_dir.iterdir()), jpg_dir)
        logger.info(f"  {len(images)} JPEGs written to: {jpg_dir}")


def process_folder(folder: Path, args: argparse.Namespace) -> bool:
    """Run convert_folder, log any failure and clean up. Returns True if the folder succeeded.

    _work is deleted, except after unexpected failures and Ctrl-C, where it's kept for debugging. A FolderError
    is an expected problem with the folder's contents, logged without a traceback. Ctrl-C stops the whole run."""
    logger.info(f"\nProcessing folder: {folder}")
    work_dir = folder / WORK_DIR_NAME
    success = False
    keep_work = False
    try:
        convert_folder(folder, args)
        success = True
    except KeyboardInterrupt:
        keep_work = True
        logger.error("Interrupted by user (Ctrl-C); leaving _work for debugging.")
        raise
    except FolderError as e:
        logger.error(f"Folder failed: {folder}\n  {e}")
    except Exception:
        keep_work = True
        logger.exception(f"Folder failed: {folder}; leaving _work for debugging.")
    finally:
        if keep_work:
            logger.info(f"  Preserving work directory for debugging: {work_dir}")
        elif work_dir.exists():
            try:
                shutil.rmtree(work_dir)
                if success:
                    logger.info(f"  Cleaned up work directory: {work_dir}")
            except Exception:
                logger.exception(f"  Failed to remove work directory: {work_dir}")

    if not success and args.make_pdf:
        remove_part_pdfs(folder / PDF_DIR_NAME, output_base_name(folder))
    return success


# ============================================================
# MAIN
# ============================================================

def main(argv=None) -> int:
    """Convert every image folder under the input folder. Returns the exit code: 0 if all folders succeeded, else 1."""
    args = parse_args(argv)
    input_folder = Path(args.folder).resolve()

    log_file = setup_logging(input_folder)
    outputs = " and ".join(name for name, wanted in (("PDFs", args.make_pdf), ("JPEGs", args.make_jpg)) if wanted)
    logger.info(f"Converting images to {outputs} in {input_folder}")
    if args.make_pdf and args.no_ocr and args.ocr_lang:
        logger.warning(f"Ignoring --ocr-lang {args.ocr_lang}: no text is recognized with --no-ocr")
    validate_external_tools(need_ocrmypdf=args.make_pdf)

    failed_folders = [folder for folder in find_image_folders(input_folder) if not process_folder(folder, args)]

    if failed_folders:
        logger.error(f"\nFinished with {len(failed_folders)} failed folder(s):")
        for folder in failed_folders:
            logger.error(f"  {folder}")
    else:
        logger.info("All folders converted.")
    logger.info(f"Log written to: {log_file}")
    return 1 if failed_folders else 0


if __name__ == "__main__":
    sys.exit(main())
