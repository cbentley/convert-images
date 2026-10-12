# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A single-file CLI (`image_to_pdf.py`) that turns folders of scanned images (`.tif/.tiff/.jpg/.jpeg/.jp2`) into OCR'd, size-capped PDFs. `README.md` documents user-facing behavior and options.

## Setup & running

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # only Python dep: pikepdf
python image_to_pdf.py /path/to/images [--max-mb=100] [--ocr-lang=deu+eng]
```

External tools must be on PATH: ImageMagick, OCRmyPDF (with Ghostscript and Tesseract language packs). `jbig2` is unused, since every page is JPEG and OCRmyPDF only applies JBIG2 to 1-bit images. `validate_external_tools()` checks them at startup.

There is no test suite, linter config, or build step. To verify changes, run the script against a small sample folder of images; logs go to `<input>/_logs/<timestamp>_log.txt`.

## Architecture

`main()` does an `os.walk` over the input folder and runs the pipeline **independently for every subfolder that directly contains images** (each folder produces its own `<foldername>.pdf`; images are not aggregated across folders). Directories whose names start with `_` are pruned from the walk, which is how the script's own `_work` and `_logs` dirs are excluded.

Per-folder stages, each writing to a numbered subdir of `<folder>/_work/`:

1. `01_single_pages` — `convert_image_to_pdf` shells out to ImageMagick with `IMAGEMAGICK_INPUT_OPTS` before the input file and `IMAGEMAGICK_OUTPUT_OPTS` after it (density, resize cap, JPEG quality; ImageMagick uses 4:2:0 chroma below quality 90). This is the pipeline's only lossy encode unless `OCR_OPTS` includes an option that re-encodes images (see below). Images are converted in parallel (`CONVERT_JOBS` threads, each running a `convert` limited to one thread via `-limit thread 1`); a failure cancels queued conversions.
2. `02_pre_ocr` — `merge_pdfs` merges all pages into one PDF (single save). OCRmyPDF parallelizes across pages itself, so one run per folder keeps all cores busy.
3. `03_ocr` — `ocr_pdf` runs OCRmyPDF with `OCR_OPTS` plus `-l <ocr-lang>`. `--output-type pdf` gives a plain PDF rather than PDF/A. The `--optimize` level is a user-tunable size/quality trade-off: don't assume or document a specific value; read `OCR_OPTS` for the current one. If OCR fails it raises, failing the whole folder (no non-OCR fallback).
4. `04_split` — if the OCR'd PDF is larger than `--max-mb`, `split_final_pdf_by_size` splits it into `-partNN` files; otherwise the OCR output is the final PDF.
5. Final PDFs are moved into the source folder (overwriting). `_work` is deleted on success and preserved on failure; on failure, root-level `<name>-part*.pdf` files are removed. Failed folders are listed at the end of the run and `main()` returns exit code 1.

Implementation notes:
- Splitting fills each part greedily (as many pages as fit under the cap; an oversized single page is kept as its own part, with a warning). To avoid a full save per page, `page_object_sizes` estimates each page's bytes (shared objects like the OCR font counted once per part), then real saves correct the estimate: drop pages while over the cap, add pages while still under. The result matches saving after every page; with accurate estimates that's ~2 saves per part. Keep it exact: the cap is user-facing.
- Ordering relies on lexical sort of filenames, so names use zero padding (`_page000000` for single pages, `-part01` in the final split). Preserve this when changing naming.
- Output names come from `output_base_name()`: the image folder's name, or its parent's name if the folder is in `GENERIC_FOLDER_NAMES` (currently `master`). Output still lands inside the image folder.
- Image selection goes through `is_wanted_image()` (allowed extension and no `EXCLUDED_WORDS` substring in the filename, case-insensitive). Both the folder-skip check in `main()` and `convert_all_images` use it; keep them in sync.
- Image quality and size are set by step 1 (`-resize` cap and `-quality` in `IMAGEMAGICK_OUTPUT_OPTS`) and, if enabled, by OCRmyPDF re-encoding. Several OCRmyPDF options silently re-encode every page image: `--optimize 2/3` (JPEG q75/q40), the default PDF/A output (Ghostscript, ~q55 4:2:0 regardless of input quality), and `--deskew` (Pillow default q75). They add a second lossy encode; that can be intentional (e.g. `--optimize 3` for smaller files). `--rotate-pages` is lossless (sets page rotation only).
- `IMAGEMAGICK_CMD` is `"convert"` for ImageMagick 6 compatibility; ImageMagick 7 users should set it to `"magick"`.
