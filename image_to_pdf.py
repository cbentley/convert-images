#!/usr/bin/env python3
"""
Convert folders of images into OCR-processed, size-limited PDFs.

Usage:
    python image_to_pdf.py <folder> [options]

See README.md for full documentation.
"""


import os
import re
import shutil
import argparse
import datetime
import io
import logging
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import img2pdf
import pikepdf
from PIL import Image


# ============================================================
# CONFIG
# ============================================================

# --- Input selection ---
ALLOWED_EXT = {".tif", ".tiff", ".jpg", ".jpeg", ".jp2"}
# Images whose filenames contain any of these words (case-insensitive) are skipped
EXCLUDED_WORDS = {"ruler"}
# Folder names that are too generic to name the output; use the parent's name instead
GENERIC_FOLDER_NAMES = {"master"}

# --- ImageMagick (image -> JPEG; the only lossy step), then img2pdf wraps the JPEG into a one-page PDF ---
# "magick" on ImageMagick 7 (where "convert" is deprecated), "convert" on ImageMagick 6
IMAGEMAGICK_CMD = "magick" if shutil.which("magick") else "convert"
# Placed before the input file (read settings; -density must go here).
# "-limit thread 1" keeps each process single-threaded, since CONVERT_JOBS already runs one per core.
# PDF page size is the original image's pixels / DPI, measured before -resize, so pages keep the scanned
# object's real size. Uncomment the "-density" line to treat every image as 300 DPI instead (page size =
# original pixels / 300); "-units" makes it apply to images whose DPI is in pixels/cm or missing.
IMAGEMAGICK_INPUT_OPTS = [
    "-limit", "thread", "1",
    # "-density", "300", "-units", "PixelsPerInch",
]
# DPI assumed for images that don't store one, when -density is not set
FALLBACK_DPI = 300
# sRGB ICC profile. Images with an embedded profile (e.g. Adobe RGB) are converted to sRGB;
# untagged RGB images are tagged as sRGB without changing pixels; untagged gray/CMYK are left as is.
# Ubuntu: from the icc-profiles-free package. macOS: "/System/Library/ColorSync/Profiles/sRGB Profile.icc"
SRGB_PROFILE = "/usr/share/color/icc/sRGB.icc"
# Placed after the input file (processing and output encoding)
IMAGEMAGICK_OUTPUT_OPTS = [
    "-profile", SRGB_PROFILE, # Before -resize, so pixels are converted before resizing and encoding
    #"-resize", "3500x3500>",
    "-quality", "75",
]
# Number of images converted in parallel. Lower it if large TIFFs exhaust memory.
CONVERT_JOBS = os.cpu_count() or 1

# --- OCRmyPDF (language is set by --ocr-lang) ---
OCR_CMD = "ocrmypdf"
# --optimize 2/3, PDF/A output and --deskew re-encode page images; --rotate-pages is lossless.
OCR_OPTS = [
    "--optimize", "3", # Select 3 for general use. Select 1 for higher quality (and larger file sizes).
    #"--deskew", # Also re-encodes the JPEG
    #"--rotate-pages", # Use for text
    "--output-type", "pdf",
]

# ============================================================
# LOGGING SETUP
# ============================================================

logger = logging.getLogger("image_to_pdf_pipeline")


def setup_logging(log_file: Path) -> None:
    """Configure module-level logger for file + console output."""
    logger.setLevel(logging.INFO)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)


# ============================================================
# VALIDATION
# ============================================================

def validate_external_tools() -> None:
    """Verify ImageMagick, OCRmyPDF and the sRGB profile are available (OCRmyPDF runs even with --no-ocr)."""
    tools = {
        f"ImageMagick ({IMAGEMAGICK_CMD})": [IMAGEMAGICK_CMD, "--version"],
        "OCRmyPDF": [OCR_CMD, "--version"],
    }

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
# IMAGE TO SINGLE-PAGE PDF CONVERSION
# ============================================================

# ICC profile color space (header bytes 16-20) expected for each JPEG mode
ICC_COLOR_SPACES = {"L": b"GRAY", "RGB": b"RGB ", "CMYK": b"CMYK"}


def strip_mismatched_icc(jpeg: bytes) -> bytes:
    """Remove the ICC profile from a JPEG if it doesn't match the image's color space.
    ImageMagick's -profile tags untagged gray/CMYK images with the sRGB profile without converting them;
    img2pdf would embed that RGB profile for a CMYK image, giving wrong colors. Pixel data is unchanged."""
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


def original_page_size(print_output: str) -> tuple[float, float]:
    """Page size in points from ImageMagick's "-print '%w %h %x %y %U'" line for the original image.
    ImageMagick reports 72 DPI with units "Undefined" when the file stores no DPI; FALLBACK_DPI is used then."""
    width, height, x_dpi, y_dpi, units = print_output.split("\n")[0].split()
    x_dpi, y_dpi = float(x_dpi), float(y_dpi)
    if units == "PixelsPerCentimeter":
        x_dpi, y_dpi = x_dpi * 2.54, y_dpi * 2.54
    elif units != "PixelsPerInch" or x_dpi <= 0 or y_dpi <= 0:
        x_dpi = y_dpi = FALLBACK_DPI
    return int(width) / x_dpi * 72, int(height) / y_dpi * 72


def convert_image_to_pdf(image_path: Path, output_pdf: Path) -> None:
    """Convert a single image into a 1-page PDF: ImageMagick encodes a JPEG, img2pdf wraps it without re-encoding.
    (ImageMagick's own PDF writer is blocked by the default security policy on Ubuntu 22.04, and adds a thumbnail
    and, for images with an alpha channel, an opaque soft mask to every page.)"""
    jpeg_path = output_pdf.with_suffix(".jpg")
    cmd = [
        IMAGEMAGICK_CMD,
        *IMAGEMAGICK_INPUT_OPTS,
        str(image_path),
        "-print", "%w %h %x %y %U\n",  # Original size and DPI, before -resize changes the pixel count
        *IMAGEMAGICK_OUTPUT_OPTS,
        str(jpeg_path)
    ]
    result = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    page_size = original_page_size(result.stdout)
    jpeg = strip_mismatched_icc(jpeg_path.read_bytes())
    # Rotation.none ignores EXIF orientation, matching ImageMagick's PDF output
    output_pdf.write_bytes(img2pdf.convert(
        jpeg,
        rotation=img2pdf.Rotation.none,
        layout_fun=lambda *_: (*page_size, *page_size),  # Image fills a page of the original's size
    ))
    jpeg_path.unlink()


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


def convert_all_images(folder: Path, single_pages_dir: Path) -> list[Path]:
    """Convert all allowed images in a folder into single-page PDFs."""
    candidates = [p for p in folder.iterdir() if is_allowed_ext(p)]
    images = sorted((p for p in candidates if not is_excluded(p)), key=natural_sort_key)
    skipped = sorted(p.name for p in candidates if is_excluded(p))
    if skipped:
        logger.info(f"  Skipped {len(skipped)} excluded image(s): {', '.join(skipped)}")

    if not images:
        logger.warning("  No valid images found.")
        return []

    logger.info(f"  Found {len(images)} images")

    pdf_pages = [
        single_pages_dir / f"{folder.name}_page{idx:06d}.pdf"
        for idx in range(len(images))
    ]

    with ThreadPoolExecutor(max_workers=CONVERT_JOBS) as executor:
        futures = [
            executor.submit(convert_image_to_pdf, img, pdf_path)
            for img, pdf_path in zip(images, pdf_pages)
        ]
        try:
            for future in futures:
                future.result()
        except BaseException:
            # Don't start queued conversions after a failure or Ctrl-C
            executor.shutdown(cancel_futures=True)
            raise

    logger.info(f"  Converted {len(pdf_pages)} images to PDFs")
    return pdf_pages


# ============================================================
# MERGE (PRE-OCR) & SPLIT (POST-OCR)
# ============================================================

def pdf_size_mb(path: Path) -> float:
    """Return the size of the PDF in megabytes."""
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
    return pdf_size_mb(path)


def split_final_pdf_by_size(input_pdf: Path, output_dir: Path, max_mb: int) -> list[Path]:
    """
    Split a PDF into parts of at most max_mb, each holding as many pages as fit.

    Pages per part are first estimated from page_object_sizes, then corrected
    with real saves: pages are removed while the part is over max_mb and added
    while it still fits. This gives the same parts as adding and saving one
    page at a time, with only a few saves per part. A page alone in its part
    stays there even if oversized (it can't be split).

    Output files are named "<stem>-part01.pdf", "<stem>-part02.pdf", etc.
    """
    output_files: list[Path] = []
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

            part_pdf = output_dir / f"{input_pdf.stem}-part{len(output_files) + 1:02d}.pdf"
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
            output_files.append(part_pdf)
            start = end

    return output_files


# ============================================================
# OCR
# ============================================================

def ocr_pdf(input_pdf: Path, output_pdf: Path, ocr_lang: str, no_ocr: bool = False) -> None:
    """
    Run OCRmyPDF on a PDF, writing the result to output_pdf.

    With no_ocr, text recognition is skipped ("--tesseract-timeout 0") but the
    rest of OCR_OPTS (e.g. --optimize) still applies.
    Raises RuntimeError if OCRmyPDF fails (there is no fallback).
    """
    cmd = [
        OCR_CMD,
        *OCR_OPTS,
        *(["--tesseract-timeout", "0"] if no_ocr else []),
        "-l", ocr_lang,
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


# ============================================================
# HELPERS
# ============================================================

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Convert folders of scanned images to OCR'd PDFs.")
    parser.add_argument("folder")
    parser.add_argument(
        "--max-mb",
        type=int,
        default=100,
        help="Maximum size in MB for each final PDF before splitting. Default: 100"
    )
    parser.add_argument(
        "--ocr-lang",
        default="deu+eng",
        help='Languages to use for OCR. Examples: "eng", "deu", "eng+deu".'
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="Skip text recognition. OCRmyPDF still runs, so OCR_OPTS such as --optimize still apply."
    )
    return parser.parse_args(argv)


def output_base_name(folder: Path) -> str:
    """Return the base name for a folder's final PDF(s)."""
    if folder.name.lower() in GENERIC_FOLDER_NAMES:
        return folder.parent.name
    return folder.name


def cleanup_partial_outputs(folder: Path, base_name: str) -> None:
    """Remove partial merged PDFs after a failure (root-level, if any)."""
    for pdf in folder.glob(f"{base_name}-part*.pdf"):
        try:
            pdf.unlink()
            logger.info(f"  Removed partial output: {pdf.name}")
        except Exception:
            logger.exception(f"  Failed to remove partial output: {pdf.name}")


# ============================================================
# MAIN
# ============================================================

def main(argv=None) -> int:
    """
    Run the full image→PDF→OCR pipeline with temp work directories.
    Returns the process exit code: 0 if all folders succeeded, 1 otherwise.
    """
    args = parse_args(argv)

    input_folder = Path(args.folder).resolve()

    # Log
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    logs_dir = input_folder / "_logs"
    logs_dir.mkdir(exist_ok=True)
    log_file = logs_dir / f"{timestamp}_log.txt"
    setup_logging(log_file)
    logger.info(f"Starting pipeline for {input_folder}")

    validate_external_tools()

    failed_folders: list[Path] = []

    # Folder processing
    for root, dirs, files in os.walk(input_folder):
        # Skip folders starting with "_"
        dirs[:] = [d for d in dirs if not d.startswith("_")]

        # Skip folders with no files
        if not files:
            continue

        # Skip folders with no images
        if not any(is_wanted_image(Path(f)) for f in files):
            continue

        root_path = Path(root)
        base_name = output_base_name(root_path)

        logger.info(f"\nProcessing folder: {root_path}")

        # Per-folder work folder structure
        work_dir         = root_path / "_work"
        single_pages_dir = work_dir / "01_single_pages"
        pre_ocr_dir      = work_dir / "02_pre_ocr"
        ocr_dir          = work_dir / "03_ocr"
        split_dir        = work_dir / "04_split"

        # Ensure work subfolders exist
        single_pages_dir.mkdir(parents=True, exist_ok=True)
        pre_ocr_dir.mkdir(parents=True, exist_ok=True)
        ocr_dir.mkdir(parents=True, exist_ok=True)
        split_dir.mkdir(parents=True, exist_ok=True)

        folder_success = True

        try:
            # 1. Convert images -> single-page PDFs (in _work/single_pages)
            page_pdfs = convert_all_images(root_path, single_pages_dir)
            if not page_pdfs:
                # No PDFs to process; leave work_dir empty for this folder
                continue

            # 2. Pre-OCR merge: all pages into one PDF in _work/02_pre_ocr
            pre_ocr_pdf = merge_pdfs(page_pdfs, pre_ocr_dir / f"{base_name}.pdf")

            # 3. OCR the merged PDF into _work/03_ocr (with --no-ocr, OCR_OPTS only)
            ocr_output = ocr_dir / f"{base_name}.pdf"
            ocr_pdf(pre_ocr_pdf, ocr_output, args.ocr_lang, args.no_ocr)

            # 4. Split into parts in _work/04_split if over --max-mb
            final_size = pdf_size_mb(ocr_output)

            if final_size > args.max_mb:
                logger.info(
                    f"  PDF {ocr_output.name} is {final_size:.1f} MB "
                    f"(>{args.max_mb} MB); splitting..."
                )
                final_outputs = split_final_pdf_by_size(ocr_output, split_dir, args.max_mb)
            else:
                logger.info(
                    f"  PDF {ocr_output.name} is {final_size:.1f} MB "
                    f"(<= {args.max_mb} MB); no splitting needed."
                )
                final_outputs = [ocr_output]

            # 5. Move final PDFs from _work to root folder, overwriting if needed
            for pdf in final_outputs:
                dest = root_path / pdf.name
                if dest.exists():
                    try:
                        dest.unlink()
                    except Exception:
                        logger.exception(
                            f"  Failed to remove existing file before overwrite: {dest}"
                        )
                pdf.rename(dest)
                logger.info(f"  Final output written: {dest}")

        except KeyboardInterrupt:
            folder_success = False
            logger.error("Interrupted by user (Ctrl-C); leaving _work for debugging.")
            raise   # re-raise so the whole script stops immediately
        except Exception:
            folder_success = False
            logger.exception(f"Folder failed: {root_path}; leaving _work for debugging.")
            failed_folders.append(root_path)
            # Clean up any partial root-level outputs matching the folder base name
            cleanup_partial_outputs(root_path, base_name)
            continue
        finally:
            # 6. Clean up work directory on success; keep on failure
            if folder_success:
                try:
                    shutil.rmtree(work_dir)
                    logger.info(f"  Cleaned up work directory: {work_dir}")
                except Exception:
                    logger.exception(
                        f"  Failed to remove work directory: {work_dir}"
                    )
            else:
                logger.info(
                    f"  Preserving work directory for debugging: {work_dir}"
                )

    if failed_folders:
        logger.error(f"\nPipeline finished with {len(failed_folders)} failed folder(s):")
        for folder in failed_folders:
            logger.error(f"  {folder}")
    else:
        logger.info("Pipeline complete.")
    logger.info(f"Log written to: {log_file}")

    return 1 if failed_folders else 0


if __name__ == "__main__":
    sys.exit(main())
