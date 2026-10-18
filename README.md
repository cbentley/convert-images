# Convert Images

Converts folders of scanned images into searchable, size-limited PDFs and/or JPEGs. Built for large image collections.

## Setup

Requires ImageMagick and, for PDFs, OCRmyPDF (with Ghostscript, Tesseract language packs and `pngquant`, which `--optimize 2` and `3` need):

```bash
# Ubuntu
sudo apt update && sudo apt install -y imagemagick ocrmypdf pngquant tesseract-ocr-all

# macOS
brew install imagemagick ghostscript ocrmypdf tesseract-lang
```

Then, in the repo directory:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

To work on the script, install `requirements-dev.txt` instead: it includes `requirements.txt` and adds pytest (see Tests).

## Usage

```bash
python convert_images.py pdf [options] <folder>   # one OCR'd PDF per image folder
python convert_images.py jpg <folder>             # one JPEG per image
```

Options for `pdf`:

| Option              | Description                                                    | Default   |
| ------------------- | -------------------------------------------------------------- | --------- |
| `--max-mb=<size>`   | Maximum size of each final PDF in MB; larger output is split   | `100`     |
| `--ocr-lang=<lang>` | OCR languages, passed to OCRmyPDF `-l` (e.g. `eng`, `deu+eng`) | `deu+eng` |
| `--no-ocr`          | Skip text recognition (`--ocr-lang` is ignored); OCRmyPDF still applies `OCR_OPTS` | off |
| `--jpg`             | Also save each image as a JPEG, as `jpg` does                  | off       |

`jpg` has no options. Examples:

```bash
python convert_images.py pdf --ocr-lang=eng --max-mb=150 /path/to/images
python convert_images.py pdf --jpg /path/to/images
python convert_images.py jpg /path/to/images
```

## What it does

The script searches `<folder>` and all its subfolders. Every folder that directly contains images (`.tif`, `.tiff`, `.jpg`, `.jpeg`, `.jp2`, `.jpf`) is converted on its own; images are never combined across folders. Pages are sorted by filename in natural order, like Windows Explorer: numbers by value (`page2` before `page10`), ignoring case.

Skipped:
- folders whose names start with `_`, and folders named `access` (the script's own output folders)
- images whose filenames contain an excluded word (currently `ruler`, case-insensitive)

PDFs and JPEGs both go into an `access` folder inside each image folder, overwriting files with the same name. A folder named `master` (case-insensitive) is the exception: its `access` folder goes next to it, and its PDF takes the parent's name (`b01-f01/master/` → `b01-f01/access/b01-f01.pdf`). If the parent folder holds images too, both would write to the same `access` folder, so both folders fail without writing anything.
- `<foldername>.pdf`, or `<foldername>-part01.pdf`, `-part02.pdf`, … when it exceeds `--max-mb`.
- `<imagename>.jpg`, one per image. If two images would get the same JPEG name (e.g. `a.tif` and `a.jpg`), the folder fails rather than one overwriting the other.

Images are converted to sRGB and keep the scanned object's real size: PDF pages are sized from each original's pixels and DPI, and JPEGs that are scaled down get a correspondingly lower DPI. Image size and JPEG quality are set separately for PDF pages and JPEGs (see Configuration). For files holding several images (multi-page TIFFs, Photoshop layers), only the first is used, with a warning in the log. Images with an orientation tag (e.g. a camera's "rotate 90°") are rotated or flipped so they're upright in the output. Metadata from the originals (EXIF, XMP, Photoshop data, comments) is not carried over, only the DPI; images are tagged with a compact sRGB profile. JPEGs are progressive, so web viewers can show a rough version while they load.

A log of each run is written to `<folder>/_logs/<timestamp>_log.txt`.

### Pipeline

Each image folder is processed in its own `_work` directory:

| Step             | Work directory          | What happens                                                                                                                                                                             |
| ---------------- | ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. Convert       | `_work/01_single_pages`, `_work/jpg` | ImageMagick turns each image into a JPEG. For PDFs, img2pdf wraps it into a one-page PDF without re-encoding.                                                         |
| 2. Merge         | `_work/02_pre_ocr`      | PDF only: all pages are merged into one PDF.                                                                                                                                             |
| 3. OCR           | `_work/03_ocr`          | PDF only: OCRmyPDF adds a searchable text layer. Depending on `OCR_OPTS`, it may also re-compress the page images (see Configuration).                                                  |
| 4. Split         | `_work/04_split`        | PDF only: if the PDF exceeds `--max-mb`, it is split into parts, each holding as many pages as fit.                                                                                      |
| 5. Finish        |                         | The final PDFs and JPEGs are moved into the `access` folder and `_work` is deleted.                                                                                              |

### Errors

If any step fails for a folder, including OCR (there is no non-OCR fallback), that folder gets no new output and the script moves on to the next folder. Split PDFs (`-partNN`) already in its `pdf` folder are removed, including complete ones from earlier runs, so no mix of old and new parts is left. Its `_work` directory is kept for debugging, except when the failure is a problem with the folder's contents that the log explains (such as a JPEG name clash). Failed folders are listed at the end.

Exit status: `0` if all folders succeeded, `1` if any failed, `2` for invalid options or a folder that doesn't exist.

## Tests

```bash
python -m pytest                       # all tests
python -m pytest -m "not integration"  # skip the tests that run ImageMagick and OCRmyPDF
```

Needs `requirements-dev.txt` (see Setup). The integration tests generate their own small images and are skipped if ImageMagick, OCRmyPDF or Tesseract's English data is missing; the Adobe RGB test also needs `icc-profiles-free` (Ubuntu) for its test profile.

## Configuration

Other settings are constants at the top of `convert_images.py`:

| Constant                  | Controls                                                              |
| ------------------------- | --------------------------------------------------------------------- |
| `ALLOWED_EXT`             | Image file extensions to process                                      |
| `EXCLUDED_WORDS`          | Filename words that cause an image to be skipped                      |
| `GENERIC_FOLDER_NAMES`    | Folder names that take their PDF name from the parent folder and get their `access` folder next to them |
| `ACCESS_DIR_NAME`         | Name of the output folder                                             |
| `IMAGEMAGICK_CMD`         | ImageMagick command (default: `magick` if installed, i.e. version 7, else `convert`) |
| `IMAGEMAGICK_INPUT_OPTS`  | ImageMagick options before the input file (e.g. `-density`, commented out so output keeps each scan's real size) |
| `FALLBACK_DPI`            | DPI assumed for PDF page size when an image doesn't store one         |
| `SRGB_PROFILE`            | sRGB ICC profile images are converted to and tagged with (default: the compact `profiles/sRGB-v2-micro.icc` in this repo, 456 bytes, [CC0](https://github.com/saucecontrol/Compact-ICC-Profiles)) |
| `CONVERT_JOBS`            | Images converted in parallel (default: CPU count; lower if memory runs out) |
| `PDF_MAX_SIZE`, `JPG_MAX_SIZE` | Longest side in pixels for PDF page images and JPEGs (`None` keeps full size) |
| `PDF_QUALITY`, `JPG_QUALITY` | JPEG quality for PDF page images and JPEGs (below 90, color resolution is halved) |
| `DEFAULT_OCR_LANG`        | OCR languages used when `--ocr-lang` isn't given                      |
| `OCR_OPTS`                | OCRmyPDF options (language comes from `--ocr-lang`)                   |

These OCRmyPDF options re-encode every page image, adding a second lossy compression: `--optimize 2` or `3` and `--deskew`. `--rotate-pages` is lossless.
