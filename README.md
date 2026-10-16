# Convert Images

Converts folders of scanned images into searchable, size-limited PDFs and/or JPEGs. Built for large image collections.

## Setup

Requires ImageMagick, an sRGB ICC profile (see `SRGB_PROFILE`) and, for PDFs, OCRmyPDF (with Ghostscript and Tesseract language packs):

```bash
# Ubuntu
sudo apt update && sudo apt install -y imagemagick ocrmypdf tesseract-ocr-all icc-profiles-free

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
python convert_images.py pdf <folder> [options]   # one OCR'd PDF per image folder
python convert_images.py jpg <folder>             # one JPEG per image (`jpeg` also works)
```

Options for `pdf`:

| Option              | Description                                                    | Default   |
| ------------------- | -------------------------------------------------------------- | --------- |
| `--max-mb=<size>`   | Maximum size of each final PDF in MB; larger output is split   | `100`     |
| `--ocr-lang=<lang>` | OCR languages, passed to OCRmyPDF `-l` (e.g. `eng`, `deu+eng`) | `deu+eng` |
| `--no-ocr`          | Skip text recognition; OCRmyPDF still applies `OCR_OPTS`       | off       |
| `--jpg`             | Also save each image as a JPEG, as `jpg` does                  | off       |

`jpg` has no options. Examples:

```bash
python convert_images.py pdf /path/to/images --ocr-lang=eng --max-mb=150
python convert_images.py pdf /path/to/images --jpg
python convert_images.py jpg /path/to/images
```

## What it does

The script searches `<folder>` and all its subfolders. Every folder that directly contains images (`.tif`, `.tiff`, `.jpg`, `.jpeg`, `.jp2`) is converted on its own; images are never combined across folders. Pages are sorted by filename in natural order, like Windows Explorer: numbers by value (`page2` before `page10`), ignoring case.

Skipped:
- folders whose names start with `_`, and folders named `pdf` or `jpg` (the script's own output folders)
- images whose filenames contain an excluded word (currently `ruler`, case-insensitive)

Output goes into folders inside each image folder, overwriting files with the same name:
- `pdf/<foldername>.pdf`, or `pdf/<foldername>-part01.pdf`, `-part02.pdf`, … when it exceeds `--max-mb`. A folder named `master` (case-insensitive) takes its parent's name: `b01-f01/master/` → `b01-f01/master/pdf/b01-f01.pdf`.
- `jpg/<imagename>.jpg`, one per image. If two images would get the same JPEG name (e.g. `a.tif` and `a.jpg`), the folder fails rather than one overwriting the other.

Images are converted to sRGB and keep the scanned object's real size: PDF pages are sized from each original's pixels and DPI, and JPEGs that are scaled down get a correspondingly lower DPI. Image size and JPEG quality are set separately for PDF pages and JPEGs (see Configuration). For files holding several images (multi-page TIFFs, Photoshop layers), only the first is used, with a warning in the log.

A log of each run is written to `<folder>/_logs/<timestamp>_log.txt`.

### Pipeline

Each image folder is processed in its own `_work` directory:

| Step             | Work directory          | What happens                                                                                                                                                                             |
| ---------------- | ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. Convert       | `_work/01_single_pages`, `_work/jpg` | ImageMagick turns each image into a JPEG. For PDFs, img2pdf wraps it into a one-page PDF without re-encoding.                                                         |
| 2. Merge         | `_work/02_pre_ocr`      | PDF only: all pages are merged into one PDF.                                                                                                                                             |
| 3. OCR           | `_work/03_ocr`          | PDF only: OCRmyPDF adds a searchable text layer. Depending on `OCR_OPTS`, it may also re-compress the page images (see Configuration).                                                  |
| 4. Split         | `_work/04_split`        | PDF only: if the PDF exceeds `--max-mb`, it is split into parts, each holding as many pages as fit.                                                                                      |
| 5. Finish        |                         | The final PDFs and JPEGs are moved into the `pdf` and `jpg` folders and `_work` is deleted.                                                                                              |

### Errors

If any step fails for a folder, including OCR (there is no non-OCR fallback), that folder gets no new output, its `_work` directory is kept for debugging, and the script moves on to the next folder. Failed folders are listed at the end, and the exit status is `1` (`0` if all succeeded).

## Configuration

Other settings are constants at the top of `convert_images.py`:

| Constant                  | Controls                                                              |
| ------------------------- | --------------------------------------------------------------------- |
| `ALLOWED_EXT`             | Image file extensions to process                                      |
| `EXCLUDED_WORDS`          | Filename words that cause an image to be skipped                      |
| `GENERIC_FOLDER_NAMES`    | Folder names that take their output name from the parent folder       |
| `PDF_DIR_NAME`, `JPG_DIR_NAME` | Names of the output folders                                      |
| `IMAGEMAGICK_CMD`         | ImageMagick command (default: `magick` if installed, i.e. version 7, else `convert`) |
| `IMAGEMAGICK_INPUT_OPTS`  | ImageMagick options before the input file (e.g. `-density`, commented out so output keeps each scan's real size) |
| `FALLBACK_DPI`            | DPI assumed for PDF page size when an image doesn't store one         |
| `SRGB_PROFILE`            | Path to the sRGB ICC profile images are converted to                  |
| `CONVERT_JOBS`            | Images converted in parallel (default: CPU count; lower if memory runs out) |
| `PDF_MAX_SIZE`, `JPG_MAX_SIZE` | Longest side in pixels for PDF page images and JPEGs (`None` keeps full size) |
| `PDF_QUALITY`, `JPG_QUALITY` | JPEG quality for PDF page images and JPEGs (below 90, color resolution is halved) |
| `OCR_OPTS`                | OCRmyPDF options (language comes from `--ocr-lang`)                   |

These OCRmyPDF options re-encode every page image, adding a second lossy compression: `--optimize 2` or `3`, `--deskew`, and PDF/A output (the default if `--output-type pdf` is removed). `--rotate-pages` is lossless.
