# Image Folder to OCR PDF Converter

Converts folders of scanned images into searchable, size-limited PDFs. Built for large image collections.

## Setup

Requires ImageMagick and OCRmyPDF (with Ghostscript and Tesseract language packs):

```bash
# Ubuntu
sudo apt update && sudo apt install -y imagemagick ocrmypdf tesseract-ocr-all

# macOS
brew install imagemagick ghostscript ocrmypdf tesseract-lang
```

Then, in the repo directory:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
python image_to_pdf.py <folder> [options]
```

| Option              | Description                                                    | Default   |
| ------------------- | -------------------------------------------------------------- | --------- |
| `--max-mb=<size>`   | Maximum size of each final PDF in MB; larger output is split   | `100`     |
| `--ocr-lang=<lang>` | OCR languages, passed to OCRmyPDF `-l` (e.g. `eng`, `deu+eng`) | `deu+eng` |

Example: `python image_to_pdf.py /path/to/images --ocr-lang=eng --max-mb=150`

## What it does

The script searches `<folder>` and all its subfolders. Every folder that directly contains images (`.tif`, `.tiff`, `.jpg`, `.jpeg`, `.jp2`) produces its own PDF; images are never combined across folders. Pages are sorted by filename.

Skipped:
- folders whose names start with `_`
- images whose filenames contain an excluded word (currently `ruler`, case-insensitive)

Output is written into each image folder, overwriting files with the same name:
- `<foldername>.pdf`, or `<foldername>-part01.pdf`, `-part02.pdf`, … when it exceeds `--max-mb`
- A folder named `master` (case-insensitive) takes its parent's name: `b01-f01/master/` → `b01-f01.pdf`

A log of each run is written to `<folder>/_logs/<timestamp>_log.txt`.

### Pipeline

Each image folder is processed in its own `_work` directory:

| Step             | Work directory          | What happens                                                                                                                                                                             |
| ---------------- | ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. Convert       | `_work/01_single_pages` | ImageMagick turns each image into a one-page PDF. By default, images over 3500 px on the long side are scaled down to 3500 px, and pages are stored as JPEG quality 75.                  |
| 2. Merge         | `_work/02_pre_ocr`      | All pages are merged into one PDF.                                                                                                                                                       |
| 3. OCR           | `_work/03_ocr`          | OCRmyPDF adds a searchable text layer. Depending on `OCR_OPTS`, it may also re-compress the page images (see Configuration).                                                             |
| 4. Split         | `_work/04_split`        | If the PDF exceeds `--max-mb`, it is split into parts, each holding as many pages as fit.                                                                                                |
| 5. Finish        |                         | The final PDFs are moved into the image folder and `_work` is deleted.                                                                                                                   |

### Errors

If any step fails for a folder, including OCR (there is no non-OCR fallback), that folder gets no PDF, its `_work` directory is kept for debugging, and the script moves on to the next folder. Failed folders are listed at the end, and the exit status is `1` (`0` if all succeeded).

## Configuration

Other settings are constants at the top of `image_to_pdf.py`:

| Constant                  | Controls                                                              |
| ------------------------- | --------------------------------------------------------------------- |
| `ALLOWED_EXT`             | Image file extensions to process                                      |
| `EXCLUDED_WORDS`          | Filename words that cause an image to be skipped                      |
| `GENERIC_FOLDER_NAMES`    | Folder names that take their output name from the parent folder       |
| `IMAGEMAGICK_CMD`         | ImageMagick command: `convert` for version 6, `magick` for version 7  |
| `IMAGEMAGICK_INPUT_OPTS`  | ImageMagick options before the input file (e.g. `-density`)           |
| `IMAGEMAGICK_OUTPUT_OPTS` | ImageMagick options after the input file (resize limit, JPEG quality) |
| `CONVERT_JOBS`            | Images converted in parallel (default: CPU count; lower if memory runs out) |
| `OCR_OPTS`                | OCRmyPDF options (language comes from `--ocr-lang`)                   |

These OCRmyPDF options re-encode every page image, adding a second lossy compression: `--optimize 2` or `3`, `--deskew`, and PDF/A output (the default if `--output-type pdf` is removed). `--rotate-pages` is lossless.
