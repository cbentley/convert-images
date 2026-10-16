# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A single-file CLI (`image_to_pdf.py`) that turns folders of scanned images (`.tif/.tiff/.jpg/.jpeg/.jp2`) into OCR'd, size-capped PDFs. `README.md` documents user-facing behavior and options.

## Setup & running

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # pikepdf, img2pdf, Pillow
python image_to_pdf.py /path/to/images [--max-mb=100] [--ocr-lang=deu+eng]
```

External tools must be on PATH: ImageMagick, OCRmyPDF (with Ghostscript and Tesseract language packs), and `pngquant` (needed by OCRmyPDF for `--optimize 2/3`). `jbig2` is unused, since every page is JPEG and OCRmyPDF only applies JBIG2 to 1-bit images. `validate_external_tools()` checks ImageMagick, OCRmyPDF and the sRGB profile at startup (not `pngquant`).

There is no test suite, linter config, or build step. To verify changes, run the script against a small sample folder of images; logs go to `<input>/_logs/<timestamp>_log.txt`.

## Architecture

`main()` does an `os.walk` over the input folder and runs the pipeline **independently for every subfolder that directly contains images** (each folder produces its own `<foldername>.pdf`; images are not aggregated across folders). Directories whose names start with `_` are pruned from the walk, which is how the script's own `_work` and `_logs` dirs are excluded.

Per-folder stages, each writing to a numbered subdir of `<folder>/_work/`:

1. `01_single_pages` — `convert_image_to_pdf` shells out to ImageMagick with `IMAGEMAGICK_INPUT_OPTS` before the input file and `IMAGEMAGICK_OUTPUT_OPTS` after it (density, sRGB conversion via `-profile SRGB_PROFILE`, resize cap, JPEG quality, `-units PixelsPerInch` so the JPEG carries the density; ImageMagick uses 4:2:0 chroma below quality 90) to write a JPEG, which img2pdf wraps into a one-page PDF without re-encoding (`Rotation.none`: EXIF orientation is ignored). ImageMagick doesn't write the PDF itself: Ubuntu 22.04's default ImageMagick policy blocks the PDF coder, and its PDF writer adds a thumbnail and an opaque soft mask (for images with alpha) to every page. `strip_mismatched_icc` drops the sRGB profile that `-profile` attaches to untagged gray/CMYK images, which img2pdf would otherwise embed for CMYK. `-profile` converts images with an embedded profile (e.g. Adobe RGB masters) to sRGB, tags untagged RGB as sRGB without changing pixels, and leaves untagged gray/CMYK alone; the profile survives OCRmyPDF to the final PDF. This is the pipeline's only lossy encode unless `OCR_OPTS` includes an option that re-encodes images (see below). Images are converted in parallel (`CONVERT_JOBS` threads, each running a `convert` limited to one thread via `-limit thread 1`); a failure cancels queued conversions.
2. `02_pre_ocr` — `merge_pdfs` merges all pages into one PDF (single save). OCRmyPDF parallelizes across pages itself, so one run per folder keeps all cores busy.
3. `03_ocr` — `ocr_pdf` runs OCRmyPDF with `OCR_OPTS` plus `-l <ocr-lang>`. `--output-type pdf` gives a plain PDF rather than PDF/A. The `--optimize` level is a user-tunable size/quality trade-off: don't assume or document a specific value; read `OCR_OPTS` for the current one. `--no-ocr` adds `--tesseract-timeout 0`: no text layer, but OCRmyPDF still runs (and still rasterizes every page, so it saves only the Tesseract time). If OCRmyPDF fails it raises, failing the whole folder (no fallback).
4. `04_split` — if the OCR'd PDF is larger than `--max-mb`, `split_final_pdf_by_size` splits it into `-partNN` files; otherwise the OCR output is the final PDF.
5. Final PDFs are moved into the source folder (overwriting). `_work` is deleted on success and preserved on failure; on failure, root-level `<name>-part*.pdf` files are removed. Failed folders are listed at the end of the run and `main()` returns exit code 1.

Implementation notes:
- Splitting fills each part greedily (as many pages as fit under the cap; an oversized single page is kept as its own part, with a warning). To avoid a full save per page, `page_object_sizes` estimates each page's bytes (shared objects like the OCR font counted once per part), then real saves correct the estimate: drop pages while over the cap, add pages while still under. The result matches saving after every page; with accurate estimates that's ~2 saves per part. Keep it exact: the cap is user-facing.
- Input images are ordered by `natural_sort_key` (numbers by value, case-insensitive, full name as tie-breaker). Internally, ordering relies on lexical sort of filenames, so names use zero padding (`_page000000` for single pages, `-part01` in the final split). Preserve this when changing naming.
- Output names come from `output_base_name()`: the image folder's name, or its parent's name if the folder is in `GENERIC_FOLDER_NAMES` (currently `master`). Output still lands inside the image folder.
- Image selection goes through `is_wanted_image()` (allowed extension and no `EXCLUDED_WORDS` substring in the filename, case-insensitive). Both the folder-skip check in `main()` and `convert_all_images` use it; keep them in sync.
- Image quality and size are set by step 1 (`-resize` cap and `-quality` in `IMAGEMAGICK_OUTPUT_OPTS`) and, if enabled, by OCRmyPDF re-encoding. Several OCRmyPDF options silently re-encode every page image: `--optimize 2/3` (JPEG q75/q40), the default PDF/A output (Ghostscript, ~q55 4:2:0 regardless of input quality), and `--deskew` (Pillow default q75). They add a second lossy encode; that can be intentional (e.g. `--optimize 3` for smaller files). `--rotate-pages` is lossless (sets page rotation only).
- `IMAGEMAGICK_CMD` is `"convert"` for ImageMagick 6 compatibility; ImageMagick 7 users should set it to `"magick"`.

## In progress: sharing the script with a non-programmer coworker on Windows/WSL

Goal: a coworker who isn't a programmer runs the script on their Windows PC through WSL (Ubuntu) without ever using a terminal. It must work reliably, so every step is scripted and tested, and anything that can't be tested from the Mac is listed for the handover test.

### Decisions (and why)

- **Run inside WSL Ubuntu**, not native Windows (ImageMagick/OCRmyPDF/Tesseract are painful to install there) and not Docker (too much to support for a non-programmer). Fallback if WSL proves unreliable: run the script on a machine we control and have the coworker drop scans into a shared watched folder.
- **Ubuntu version: an LTS, installed by explicit name** (e.g. `wsl --install -d Ubuntu-26.04`; check exact names with `wsl --list --online`). Plain `wsl --install` gives whatever "Ubuntu" currently points to, and a non-LTS or future release may bring a Python that `pikepdf>=10.0.2,<10.1` has no wheels for. The full pipeline has been tested on 22.04, 24.04 and 26.04 (see Testing).
- **Install from a git clone** of the public repo (`https://github.com/cbentley/image-to-pdf`, HTTPS so no credentials) into `~/image-to-pdf` inside WSL, tracking `main`, with the venv at `env/`. Updates are `git pull` plus `pip install -r requirements.txt`. Nothing generated for this machine (launchers, paths, distro name) may be written into tracked files, or `git pull` will conflict.
- **Desktop icons are Windows shortcuts (`.lnk`) that start `wsl.exe` directly; all logic lives in bash scripts in the repo.** Not `.bat` files, because:
  - cmd.exe splits a dragged path containing `&` (Explorer only quotes dragged paths that contain spaces);
  - capturing `wsl.exe` output in a `.bat` (`for /f`) garbles non-ASCII text: `wsl.exe` writes its own messages as UTF-16 unless `WSL_UTF8=1` is set ([microsoft/WSL#4607](https://github.com/microsoft/WSL/issues/4607));
  - `.bat` files need CRLF line endings and are fragile with umlauts.

  With a shortcut, Explorer passes a dropped folder straight to `wsl.exe` as an argument, and `wsl.exe --exec` hands it to the bash script without any shell parsing.
- **Options via prompts with defaults** (Enter keeps the default) inside the bash script, not a config file (typos fail silently).
- **Logs:** the script prints progress to the console and writes `<chosen folder>/_logs/<timestamp>_log.txt`. The bash script turns the exit code into a plain message (0 = all folders OK; 1 = some folder failed; any other nonzero code, e.g. 2 for a bad option, is also a failure); when something fails, the coworker sends that log file.
- **ImageMagick writes JPEGs, img2pdf makes the PDFs** (commit `66521dd`). This avoids Ubuntu 22.04's ImageMagick policy, which blocks the PDF coder. Don't go back to ImageMagick's PDF output.

### Done (branch `perf/faster-pipeline`, pushed; not yet merged to `main`)

- `23b31d3` argparse program name showed as `Ellipsis` in usage/errors.
- `27d8604` natural page order (`scan_2` before `scan_10`, case ignored; close to, but not exactly, Windows Explorer's order).
- `66521dd` ImageMagick → JPEG → img2pdf. Verified on Ubuntu 24.04 and 22.04 against the old output: identical decoded pixels, page sizes, rotation, colors (incl. CMYK) and OCR text; final PDFs ~24% smaller (no thumbnail / opaque SMask per page). Also runs on Ubuntu 26.04 with the same page sizes.

### Remaining steps

1. **Merge `perf/faster-pipeline` into `main`** (PR), since the coworker's clone tracks `main`.
2. **`windows/run.sh`** (bash, LF line endings, executable; the shortcut runs it):
   - Folder: use `$1` if given (dropped on the icon); otherwise open a Windows folder picker by calling `powershell.exe` from WSL (`System.Windows.Forms.FolderBrowserDialog`; set `[Console]::OutputEncoding` to UTF-8 so umlauts survive, strip the trailing `\r`, and give the dialog a topmost owner window so it doesn't open behind the console). Cancel → exit cleanly.
   - Convert with `wslpath -a`, then check the folder exists (`[ -d ]`). If not, explain in plain words: UNC paths (`\\server\share`) don't convert, and mapped drive letters (`S:\`) convert to `/mnt/s`, which doesn't exist in WSL; ask them to copy the scans to a local folder like `C:\Scans`.
   - Prompts (`read -r -p`): max MB `[100]`, OCR languages `[deu+eng]`, recognize text `(Y/n)` → `--max-mb`, `--ocr-lang`, `--no-ocr`. Validate the MB value is a whole number and re-ask otherwise.
   - Run `"$HOME/image-to-pdf/env/bin/python" "$HOME/image-to-pdf/image_to_pdf.py" "$folder" ...` with every variable quoted.
   - Exit code 0 → "All PDFs created."; anything else → "Some folders failed. Please send Chris the newest file in the _logs folder inside <folder>." Then `read -r -p "Press Enter to close this window"`, so the window never closes before they've read the result.
3. **`windows/update.sh`:** `git -C "$HOME/image-to-pdf" pull --ff-only`, then `env/bin/pip install -r requirements.txt`; print a clear success or failure message and wait for Enter.
4. **`setup.sh`** at the repo root, run once inside their WSL; must be safe to re-run:
   - `sudo apt update`, then `sudo apt install -y imagemagick ocrmypdf pngquant tesseract-ocr-deu tesseract-ocr-eng icc-profiles-free python3-venv git` (only the needed language packs; `tesseract-ocr-all` is huge). `sudo` asks for the coworker's WSL (Linux) password, so run setup together with them.
   - create `env/` and `pip install -r requirements.txt`
   - self-test: generate a few small images at runtime with `convert` (don't commit binaries), run the script on them, and check that a PDF was produced with a text layer (e.g. pikepdf: every page has a `/Font` resource). End with a clear "Setup OK" or a plain error.
   - create the two desktop shortcuts by calling `powershell.exe` (`WScript.Shell` → `CreateShortcut`). Find the desktop with `[Environment]::GetFolderPath('Desktop')` (it may be redirected to OneDrive), and pass Windows paths to PowerShell via `wslpath -w`.
     - "Scans to PDF": target `C:\Windows\System32\wsl.exe`, arguments `-d <distro> --cd ~ --exec /home/<user>/image-to-pdf/windows/run.sh`
     - "Update Scans to PDF": the same with `windows/update.sh`
     - Take `<distro>` from `$WSL_DISTRO_NAME` and the path from `$HOME`. `--exec` doesn't expand `~` in the command itself, so it must be an absolute path.
   - also add `pngquant` to the README's Ubuntu install line
5. **Handover on their machine** (none of the Windows side can be tested from the Mac):
   - First confirm `wsl -l -v` shows VERSION 2 and an LTS Ubuntu.
   - Run `setup.sh`, then check that both desktop icons appear.
   - Test with real data:
     - double-click the icon and use the picker
     - drag a folder onto the icon
     - their largest TIFF
     - a `.jp2` if they have any
     - a folder name with an umlaut, a space, `&` and parentheses
     - a folder big enough to split
     - `--no-ocr`
     - one deliberate failure (e.g. a corrupt `.tif`), to confirm they see the failure message
     - a mapped drive or OneDrive path, to confirm the friendly message
   - Check that their filenames sort as expected.

Instructions for the coworker: work in a plain local folder like `C:\Scans` (not OneDrive, whose cloud-only files may not be readable from WSL, and not a mapped network drive); keep the PC awake and the window open during long runs (closing it stops the run); close output PDFs before re-running (an open PDF can't be overwritten); don't run two conversions on the same folder at once; `_logs` is normal, and `_work` left after a failure can be deleted once it has been looked at.

### Gotchas found

- `--optimize 2/3` needs `pngquant`. Without it every folder fails at OCR (OCRmyPDF exit code 3). `apt install --no-install-recommends ocrmypdf` doesn't install it.
- ImageMagick limits (the same on 24.04 `/etc/ImageMagick-6/policy.xml` and 26.04 `/etc/ImageMagick-7/policy.xml`): memory 1GiB, map 2GiB, area 256MP, disk 2GiB, width/height 32KP. A very large TIFF can fail with "cache resources exhausted"; raise the limits if that happens. `CONVERT_JOBS` runs one `convert` per core, and WSL 2 gets 50% of host RAM by default.
- Ubuntu 26.04 ships ImageMagick 7. Its `convert` command still works with the script's default `IMAGEMAGICK_CMD = "convert"` (tested). Upstream deprecates `convert`; if a future release drops it, switch to `magick`.
- Disk space: `_work` in each image folder holds the single-page PDFs, the merged PDF and the OCR output until the folder succeeds, and OCRmyPDF's temp files go to `/tmp` inside WSL's virtual disk, which grows on C: and doesn't shrink by itself. Large batches need free space on C:.
- WSL file access under `/mnt/c` is slower than Linux-native files; acceptable since OCR dominates.
- Corporate networks: `apt`, `git clone` and `pip` need internet access; a proxy or TLS inspection can break them. Check this first during setup.
- WSL interop must be enabled (the default) for `powershell.exe` and `wslpath` to work from inside WSL.

### Testing

- Linux parts can be tested on the Mac with Podman (`podman machine start`): build Ubuntu 22.04/24.04/26.04 images with the packages above plus `qpdf`. Generate synthetic scans with ImageMagick (`plasma:` backgrounds with annotated text) covering: untagged RGB over 3500 px, 16-bit Adobe RGB, 8-bit gray, untagged CMYK, a JPEG with EXIF orientation 6, JPEG 2000, 1-bit 600 dpi, a TIFF orientation tag, and a 400 dpi tag.
- Tested versions: 22.04 (ImageMagick 6.9.11, OCRmyPDF 13.4, Python 3.10), 24.04 (ImageMagick 6.9.12, OCRmyPDF 15.2, Python 3.12), 26.04 (ImageMagick 7.1.2, OCRmyPDF 16.13, Tesseract 5.5, Python 3.14; pikepdf 10.0.3 wheel available). All on arm64 containers; the coworker's PC is x86_64. PyPI has pikepdf 10.0.3 x86_64 Linux wheels for Python 3.10, 3.12 and 3.14, and the Ubuntu packages are the same on both architectures, but `setup.sh`'s self-test is the real check.
- Wrap any Ghostscript `txtwrite` text-extraction check in a timeout: Ghostscript 9.55 (Ubuntu 22.04) hangs on OCR'd PDFs. The pipeline itself doesn't use `txtwrite`.
- `run.sh` and `update.sh` can be partly tested in the containers (prompts, path checks, exit-code messages) by passing a Linux path as `$1`; the picker, shortcuts, `wslpath` and drag-and-drop need the real Windows machine.
- To run the script natively on the Mac, use a test copy with `SRGB_PROFILE = "/System/Library/ColorSync/Profiles/sRGB Profile.icc"` and `IMAGEMAGICK_CMD = "magick"` (Homebrew has ImageMagick 7); the committed defaults are for Ubuntu.
- The comparison and test scripts from the first session were in a temporary folder and weren't kept; regenerate them from the description above, or commit them under `tests/` next time.

### Still to verify on the real machine

These are from documentation or reasoning, not tested:
- `wsl.exe --cd ~ --exec <absolute path> <dropped folder>` passes the dropped path through unchanged, including umlauts, spaces and `&`.
- The PowerShell folder picker appears in front and returns non-ASCII paths intact.
- The shortcut's console window shows the prompts and stays open until Enter.

### Open questions

- Their Windows version, WSL version (1 or 2), Ubuntu version and distro name.
- Where their scans live (local, OneDrive, network share).
- Their largest image size and typical batch size, and free space on C:.
- Their filename scheme (zero-padded or not).
- Whether their network allows GitHub and PyPI.
