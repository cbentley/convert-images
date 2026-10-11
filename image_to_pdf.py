#!/usr/bin/env python3
"""
Convert folders of images into OCR-processed, size-limited PDFs.

Usage:
    python image_to_pdf.py <folder> [options]

See README.md for full documentation.
"""


import os
import shutil
import argparse
import datetime
import logging
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pikepdf


# ============================================================
# CONFIG
# ============================================================

# --- Input selection ---
ALLOWED_EXT = {".tif", ".tiff", ".jpg", ".jpeg", ".jp2"}
# Images whose filenames contain any of these words (case-insensitive) are skipped
EXCLUDED_WORDS = {"ruler"}
# Folder names that are too generic to name the output; use the parent's name instead
GENERIC_FOLDER_NAMES = {"master"}

# --- ImageMagick (image -> single-page PDF; the only lossy step) ---
IMAGEMAGICK_CMD = "convert" # Set to "magick" for ImageMagick versions > 6
# Placed before the input file (read settings; -density must go here).
# "-limit thread 1" keeps each process single-threaded, since CONVERT_JOBS already runs one per core.
IMAGEMAGICK_INPUT_OPTS = ["-limit", "thread", "1", "-density", "300"]
# Placed after the input file (processing and output encoding)
IMAGEMAGICK_OUTPUT_OPTS = [
    "-resize", "3500x3500>",
    "-quality", "75",
    "-compress", "jpeg",
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

# --- Merging ---
# Size limit (MB) for merged chunks fed to OCR
PRE_OCR_MAX_MB = 50


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
    """Verify ImageMagick and OCRmyPDF are available (OCR mandatory)."""
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


# ============================================================
# IMAGE TO SINGLE-PAGE PDF CONVERSION
# ============================================================

def convert_image_to_pdf(image_path: Path, output_pdf: Path) -> None:
    """Convert a single image into a 1-page PDF using ImageMagick."""
    cmd = [
        IMAGEMAGICK_CMD,
        *IMAGEMAGICK_INPUT_OPTS,
        str(image_path),
        *IMAGEMAGICK_OUTPUT_OPTS,
        str(output_pdf)
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


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


def convert_all_images(folder: Path, single_pages_dir: Path) -> list[Path]:
    """Convert all allowed images in a folder into single-page PDFs."""
    candidates = [p for p in folder.iterdir() if is_allowed_ext(p)]
    images = sorted(p for p in candidates if not is_excluded(p))
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
# MERGING (PRE-OCR CHUNKS)
# ============================================================

def create_empty_pdf(path: Path) -> None:
    """Create an empty PDF at the given path."""
    with pikepdf.Pdf.new() as pdf:
        pdf.save(path)


def open_pdf(path: Path):
    """Open a PDF with allow_overwriting_input=True."""
    return pikepdf.open(path, allow_overwriting_input=True)


def pdf_size_mb(path: Path) -> float:
    """Return the size of the PDF in megabytes."""
    return path.stat().st_size / 1_000_000


def finalize_part(pdf, path: Path, merged_files: list[Path]) -> None:
    """
    Close and record a completed part-PDF.
    """
    size_mb = pdf_size_mb(path)
    logger.info(f"  Finalized {path.name} ({size_mb:.1f} MB)")
    pdf.close()
    merged_files.append(path)


def apply_dynamic_padding(
    merged_files: list[Path],
    output_base: str
) -> list[Path]:
    """
    Retroactively rename merged files using minimal-width padding so lexical
    ordering matches numeric ordering.
    """
    max_part = len(merged_files)
    width = len(str(max_part))  # number of digits needed

    updated_files = []
    for i, old_path in enumerate(merged_files, start=1):
        new_name = f"{output_base}-part{str(i).zfill(width)}.pdf"
        new_path = old_path.with_name(new_name)

        if old_path != new_path:
            old_path.rename(new_path)

        updated_files.append(new_path)

    return updated_files


def merge_pages_into_chunks(
    page_pdfs: list[Path],
    output_base: str,
    pdf_output_dir: Path,
    max_mb: int
) -> list[Path]:
    """
    Merge single-page PDFs into size-limited multi-page PDFs, enforcing max_mb.

    This is used for pre-OCR merging with a hardcoded PRE_OCR_MAX_MB.
    """
    merged_files: list[Path] = []

    part = 1
    current_pdf = pdf_output_dir / f"{output_base}-part{part}.pdf"

    # Create first empty PDF
    create_empty_pdf(current_pdf)
    pdf = open_pdf(current_pdf)

    for page_pdf in page_pdfs:
        with pikepdf.open(page_pdf) as page_doc:
            pdf.pages.extend(page_doc.pages)
            pdf.save(current_pdf)
            size_mb = pdf_size_mb(current_pdf)

        # Size check happens OUTSIDE the inner with.
        # A page alone in its part stays there even if oversized (it can't be split).
        if size_mb > max_mb and len(pdf.pages) > 1:
            # Undo the last page
            del pdf.pages[-1]
            pdf.save(current_pdf)

            finalize_part(pdf, current_pdf, merged_files)

            # Start next part
            part += 1
            current_pdf = pdf_output_dir / f"{output_base}-part{part}.pdf"

            create_empty_pdf(current_pdf)
            pdf = open_pdf(current_pdf)

            # Re-add the page we deferred
            with pikepdf.open(page_pdf) as page_doc:
                pdf.pages.extend(page_doc.pages)
                pdf.save(current_pdf)
            size_mb = pdf_size_mb(current_pdf)

        if size_mb > max_mb:
            logger.warning(
                f"  {page_pdf.name} alone is {size_mb:.1f} MB (> {max_mb} MB); "
                f"keeping it as an oversized part"
            )

    # Final PDF
    finalize_part(pdf, current_pdf, merged_files)

    # If only one PDF, rename part→base and skip dynamic padding
    if len(merged_files) == 1:
        only_pdf = merged_files[0]
        new_name = only_pdf.with_name(f"{output_base}.pdf")
        if only_pdf.name != new_name.name:
            only_pdf.rename(new_name)
            merged_files[0] = new_name
        return merged_files

    # Retroactive dynamic padding
    return apply_dynamic_padding(merged_files, output_base)


# ============================================================
# POST-OCR MERGE & SPLIT (IN _work/merged)
# ============================================================

def merge_all_pdfs_in_dir(pdf_dir: Path, output_pdf: Path) -> Path:
    """
    Merge all PDFs in pdf_dir into a single PDF at output_pdf.
    Existing output_pdf is excluded from the input set if present.
    """
    pdf_files = sorted(
        p for p in pdf_dir.glob("*.pdf") if p.resolve() != output_pdf.resolve()
    )
    if not pdf_files:
        raise RuntimeError(f"No PDFs found to merge in {pdf_dir}")

    with pikepdf.Pdf.new() as merged:
        for pdf in pdf_files:
            with pikepdf.open(pdf) as src:
                merged.pages.extend(src.pages)
        merged.save(output_pdf)

    logger.info(f"  Merged {len(pdf_files)} PDFs into {output_pdf.name}")
    return output_pdf


def split_final_pdf_by_size(input_pdf: Path, max_mb: int) -> list[Path]:
    """
    Split a single large PDF into <= max_mb parts, using a page-based split.

    Output files are named "<stem>-part01.pdf", "<stem>-part02.pdf", etc.
    The original input_pdf is removed if splitting succeeds.
    """
    merged_files: list[Path] = []
    base_name = input_pdf.stem
    pdf_output_dir = input_pdf.parent

    part = 1
    current_pdf = pdf_output_dir / f"{base_name}-part{part:02d}.pdf"

    create_empty_pdf(current_pdf)
    pdf = open_pdf(current_pdf)

    with pikepdf.open(input_pdf) as src:
        for page_num, page in enumerate(src.pages, start=1):
            pdf.pages.append(page)
            pdf.save(current_pdf)
            size_mb = pdf_size_mb(current_pdf)

            # A page alone in its part stays there even if oversized (it can't be split)
            if size_mb > max_mb and len(pdf.pages) > 1:
                # Undo the last page
                del pdf.pages[-1]
                pdf.save(current_pdf)

                finalize_part(pdf, current_pdf, merged_files)

                # Start next part
                part += 1
                current_pdf = pdf_output_dir / f"{base_name}-part{part:02d}.pdf"

                create_empty_pdf(current_pdf)
                pdf = open_pdf(current_pdf)

                # Re-add the page we deferred
                pdf.pages.append(page)
                pdf.save(current_pdf)
                size_mb = pdf_size_mb(current_pdf)

            if size_mb > max_mb:
                logger.warning(
                    f"  Page {page_num} alone is {size_mb:.1f} MB (> {max_mb} MB); "
                    f"keeping it as an oversized part"
                )

    finalize_part(pdf, current_pdf, merged_files)

    # Remove the original big PDF now that parts exist
    input_pdf.unlink()

    return merged_files


# ============================================================
# OCR
# ============================================================

def ocr_pdf(input_pdf: Path, output_pdf: Path, ocr_lang: str) -> None:
    """
    Apply OCR to a PDF, writing the result to output_pdf.
    Raises RuntimeError if OCR fails (OCR is mandatory; there is no fallback).
    """
    cmd = [
        OCR_CMD,
        *OCR_OPTS,
        "-l", ocr_lang,
        str(input_pdf),
        str(output_pdf),
    ]

    logger.info(f"  OCRing {input_pdf.name} -> {output_pdf.name}...")

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

    logger.info(f"  Finished OCR: {output_pdf.name}")


def ocr_all_pdfs(src_dir: Path, dst_dir: Path, ocr_lang: str) -> list[Path]:
    """
    Run OCR on all PDFs in src_dir, writing outputs to dst_dir.
    Returns the list of resulting PDF paths in dst_dir.
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    result_paths: list[Path] = []

    for pdf in sorted(src_dir.glob("*.pdf")):
        output_pdf = dst_dir / pdf.name
        ocr_pdf(pdf, output_pdf, ocr_lang)
        result_paths.append(output_pdf)

    return result_paths


# ============================================================
# HELPERS
# ============================================================

def parse_args(argv=None):
    parser = argparse.ArgumentParser(...)
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
        merged_dir       = work_dir / "04_merged"

        # Ensure work subfolders exist
        single_pages_dir.mkdir(parents=True, exist_ok=True)
        pre_ocr_dir.mkdir(parents=True, exist_ok=True)
        ocr_dir.mkdir(parents=True, exist_ok=True)
        merged_dir.mkdir(parents=True, exist_ok=True)

        folder_success = True

        try:
            # 1. Convert images -> single-page PDFs (in _work/single_pages)
            page_pdfs = convert_all_images(root_path, single_pages_dir)
            if not page_pdfs:
                # No PDFs to process; leave work_dir empty for this folder
                continue

            # 2. Pre-OCR merge: size-limited chunks (<= PRE_OCR_MAX_MB) into _work/pre_ocr
            output_base = f"{base_name}_pre"
            merged_pre_ocr = merge_pages_into_chunks(
                page_pdfs,
                output_base,
                pre_ocr_dir,
                PRE_OCR_MAX_MB,
            )
            logger.info(
                f"  Created {len(merged_pre_ocr)} pre-OCR merged PDFs in: {pre_ocr_dir}"
            )

            # 3. OCR on all pre-OCR merged PDFs into _work/ocr
            logger.info("  Running OCR on pre-merged PDFs...")
            ocr_outputs = ocr_all_pdfs(pre_ocr_dir, ocr_dir, args.ocr_lang)
            if not ocr_outputs:
                raise RuntimeError("OCR stage produced no PDFs.")

            # 4. Post-OCR merge: merge all PDFs in _work/ocr into one big PDF in _work/merged
            logger.info("  Merging all OCRed PDFs into single final PDF (in work dir)...")
            combined_pdf_work = merged_dir / f"{base_name}.pdf"
            merge_all_pdfs_in_dir(ocr_dir, combined_pdf_work)

            # 5. Post-OCR split: in _work/merged, then move to root
            final_size = pdf_size_mb(combined_pdf_work)
            final_outputs: list[Path] = []

            if final_size > args.max_mb:
                logger.info(
                    f"  Combined PDF {combined_pdf_work.name} is {final_size:.1f} MB "
                    f"(>{args.max_mb} MB); splitting..."
                )
                final_outputs = split_final_pdf_by_size(combined_pdf_work, args.max_mb)
            else:
                logger.info(
                    f"  Combined PDF {combined_pdf_work.name} is {final_size:.1f} MB "
                    f"(<= {args.max_mb} MB); no splitting needed."
                )
                final_outputs = [combined_pdf_work]

            # 6. Move final PDFs from _work/merged to root folder, overwriting if needed
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
            # 7. Clean up work directory on success; keep on failure
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
