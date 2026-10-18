"""End-to-end tests: run main() on images generated with ImageMagick and check the PDFs and JPEGs.
Skipped if ImageMagick, the sRGB profile, OCRmyPDF or Tesseract's English data is missing."""
import io
import shutil
import subprocess
from pathlib import Path

import pikepdf
import pytest
from PIL import Image, ImageCms

import convert_images as ci

ADOBE_RGB_PROFILE = Path("/usr/share/color/icc/AdobeCompat-v2.icc")  # From icc-profiles-free
BACKGROUND = (216, 192, 144)  # Background color of generated images, as 8-bit RGB


def tesseract_has_eng() -> bool:
    if not shutil.which("tesseract"):
        return False
    result = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True)
    return "eng" in result.stdout.split()


HAVE_IMAGEMAGICK = bool(shutil.which(ci.IMAGEMAGICK_CMD)) and Path(ci.SRGB_PROFILE).is_file()
HAVE_OCR = HAVE_IMAGEMAGICK and bool(shutil.which(ci.OCR_CMD)) and tesseract_has_eng()

pytestmark = pytest.mark.integration
needs_imagemagick = pytest.mark.skipif(not HAVE_IMAGEMAGICK, reason="needs ImageMagick and the sRGB profile")
needs_ocr = pytest.mark.skipif(not HAVE_OCR, reason="needs ImageMagick, OCRmyPDF and Tesseract English data")


# --- Generated images ---

def make_image(path: Path, width: int = 600, height: int = 400, *opts: str, text: str = "Hello World") -> Path:
    """Write a colored image with dark text to path; opts go after the drawing (e.g. -density, -colorspace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        ci.IMAGEMAGICK_CMD,
        "-size", f"{width}x{height}", "xc:#{:02x}{:02x}{:02x}".format(*BACKGROUND),
        "-fill", "#203080", "-pointsize", str(height // 7), "-annotate", f"+20+{height // 2}", text,
        "-type", "TrueColor",  # Otherwise ImageMagick may store the image as gray
        *opts,
        str(path),
    ], check=True, capture_output=True)
    return path


def dpi_opts(dpi: float, units: str = "PixelsPerInch") -> list[str]:
    return ["-units", units, "-density", str(dpi)]


def make_noise_image(path: Path, size: int) -> Path:
    """Random noise: compresses badly, so a few images make a large PDF."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([ci.IMAGEMAGICK_CMD, "-size", f"{size}x{size}", "xc:", "+noise", "Random", *dpi_opts(300),
                    str(path)], check=True, capture_output=True)
    return path


# --- Helpers ---

def run(*argv: str) -> int:
    return ci.main(list(argv))


def log_text(input_folder: Path) -> str:
    return "\n".join(p.read_text() for p in sorted((input_folder / "_logs").glob("*_log.txt")))


def page_info(pdf_path: Path) -> list[dict]:
    """Per page: size in points, /Rotate, the image's pixel size and whether it has a font (text layer)."""
    pages = []
    with pikepdf.open(pdf_path) as pdf:
        for page in pdf.pages:
            (image,) = page.images.values()
            box = [float(v) for v in page.mediabox]
            pages.append({
                "size": (box[2] - box[0], box[3] - box[1]),
                "rotate": int(page.obj.get("/Rotate", 0)),
                "pixels": (int(image.Width), int(image.Height)),
                "text": "/Font" in page.resources,
            })
    return pages


def points(pixels: int, dpi: float) -> float:
    return pixels / dpi * 72


# --- Full run: mixed image types, PDF and JPEG ---

# (filename, width, height, ImageMagick options, expected DPI of the page size).
# Widths differ so each page can be identified in the PDF; names check natural sort order (p10 last).
MIXED_IMAGES = [
    ("p1_rgb.tif", 600, 400, dpi_opts(300), 300),
    ("p2_gray.tif", 620, 400, ["-colorspace", "Gray", *dpi_opts(118.11, "PixelsPerCentimeter")], 118.11 * 2.54),
    ("p3_cmyk.tif", 640, 400, ["-colorspace", "CMYK", *dpi_opts(300)], 300),
    ("p4_nodpi.jp2", 660, 400, [], ci.FALLBACK_DPI),  # OpenJPEG stores no DPI
    ("p5.jpf", 680, 400, [], ci.FALLBACK_DPI),
    ("p6_multi.tif", 700, 400, dpi_opts(300), 300),  # Second frame added below
    ("p10_rgb16.tif", 720, 400, ["-depth", "16", *dpi_opts(400)], 400),
]


@pytest.fixture(scope="module")
def mixed_run(tmp_path_factory):
    """Run pdf --jpg once on a folder of mixed images (plus an EXIF-rotated JPEG and an excluded ruler)."""
    root = tmp_path_factory.mktemp("mixed")
    scratch = tmp_path_factory.mktemp("scratch")  # Intermediate images, outside the input folder
    folder = root / "b01" / "master"
    for name, width, height, opts, _ in MIXED_IMAGES:
        if name.endswith(".jpf"):
            # ImageMagick has no JPX writer; a JP2 file with a .jpf name is what Photoshop's JP2-compatible JPX is
            shutil.copy(make_image(scratch / "tmp.jp2", width, height, *opts), folder / name)
        else:
            make_image(folder / name, width, height, *opts)
    multi = folder / "p6_multi.tif"
    subprocess.run([ci.IMAGEMAGICK_CMD, str(multi), "-size", "50x50", "xc:gray", str(multi)], check=True)

    # EXIF orientation 6 (rotate 90° clockwise): the page must be upright, i.e. 400x740
    with Image.open(make_image(scratch / "tmp.tif", 740, 400, "-depth", "8")) as im:
        exif = Image.Exif()
        exif[0x0112] = 6
        im.convert("RGB").save(folder / "p7_exif6.jpg", exif=exif, dpi=(300, 300), quality=95)
    make_image(folder / "ruler.tif", 300, 100)
    make_image(root / "other" / "scan.tif", 600, 400, *dpi_opts(300))

    exit_code = run("pdf", str(root), "--jpg", "--ocr-lang", "eng")
    return root, folder, exit_code


@needs_ocr
def test_mixed_run_succeeds_and_cleans_up(mixed_run):
    root, folder, exit_code = mixed_run
    assert exit_code == 0
    assert not (folder / "_work").exists()
    assert not (root / "other" / "_work").exists()
    assert len(list((root / "_logs").glob("*_log.txt"))) == 1


@needs_ocr
def test_mixed_run_pdf_names(mixed_run):
    root, folder, _ = mixed_run
    # "master" takes the parent's name, and its access folder goes next to it
    assert not (folder / "access").exists()
    assert [p.name for p in (root / "b01" / "access").glob("*.pdf")] == ["b01.pdf"]
    assert [p.name for p in (root / "other" / "access").glob("*.pdf")] == ["other.pdf"]


@needs_ocr
def test_mixed_run_pdf_pages(mixed_run):
    _, folder, _ = mixed_run
    pages = page_info(folder.parent / "access" / "b01.pdf")
    # Natural order puts p7_exif6.jpg (740x400 stored, 400x740 upright, 300 DPI) between p6 and p10;
    # ruler.tif is excluded
    expected = [(w, h, dpi) for _, w, h, _, dpi in MIXED_IMAGES]
    expected.insert(6, (400, 740, 300))
    assert [p["pixels"] for p in pages] == [(w, h) for w, h, _ in expected]
    for page, (w, h, dpi) in zip(pages, expected):
        assert page["size"] == pytest.approx((points(w, dpi), points(h, dpi)), abs=0.01)
        assert page["rotate"] == 0
        assert page["text"], "every page should have an OCR text layer"


@needs_ocr
def test_mixed_run_jpegs(mixed_run):
    _, folder, _ = mixed_run
    jpg_dir = folder.parent / "access"
    names = {name.rsplit(".", 1)[0] for name, *_ in MIXED_IMAGES} | {"p7_exif6"}
    assert {p.stem for p in jpg_dir.glob("*.jpg")} == names

    srgb = Path(ci.SRGB_PROFILE).read_bytes()
    expected = {  # name: (mode, has sRGB profile, DPI)
        "p1_rgb": ("RGB", True, 300),
        "p2_gray": ("L", False, 300),
        "p3_cmyk": ("CMYK", False, 300),
        "p4_nodpi": ("RGB", True, None),  # No DPI in, no DPI out
        "p10_rgb16": ("RGB", True, 400),
    }
    for name, (mode, has_srgb, dpi) in expected.items():
        with Image.open(jpg_dir / f"{name}.jpg") as im:
            assert im.mode == mode, name
            assert (im.info.get("icc_profile") == srgb) if has_srgb else ("icc_profile" not in im.info), name
            if dpi is None:
                assert "dpi" not in im.info, name
            else:
                assert im.info["dpi"] == pytest.approx((dpi, dpi), abs=1), name
            assert im.size == next((w, h) for n, w, h, *_ in MIXED_IMAGES if n.startswith(name + ".")), name


@needs_ocr
def test_mixed_run_untagged_rgb_keeps_its_colors(mixed_run):
    _, folder, _ = mixed_run
    with Image.open(folder.parent / "access" / "p1_rgb.jpg") as im:
        assert im.getpixel((5, 5)) == pytest.approx(BACKGROUND, abs=3)


@needs_ocr
def test_mixed_run_logs_multi_image_file(mixed_run):
    root, _, _ = mixed_run
    log = log_text(root)
    assert "p6_multi.tif contains 2 images; using only the first" in log
    assert "Skipped 1 excluded image(s): ruler.tif" in log


# --- Color conversion ---

@needs_imagemagick
@pytest.mark.skipif(not ADOBE_RGB_PROFILE.is_file(), reason="needs icc-profiles-free's AdobeCompat-v2.icc")
def test_adobe_rgb_is_converted_to_srgb(tmp_path):
    # -profile on an untagged image assigns the profile: the pixels keep BACKGROUND, now meaning Adobe RGB
    make_image(tmp_path / "adobe.tif", 600, 400, "-profile", str(ADOBE_RGB_PROFILE))
    assert run("jpg", str(tmp_path)) == 0

    adobe_to_srgb = ImageCms.buildTransform(str(ADOBE_RGB_PROFILE), str(ci.SRGB_PROFILE), "RGB", "RGB")
    expected = ImageCms.applyTransform(Image.new("RGB", (1, 1), BACKGROUND), adobe_to_srgb).getpixel((0, 0))
    with Image.open(tmp_path / "access" / "adobe.jpg") as im:
        assert im.info["icc_profile"] == Path(ci.SRGB_PROFILE).read_bytes()
        assert im.getpixel((5, 5)) == pytest.approx(expected, abs=3)
        assert expected != pytest.approx(BACKGROUND, abs=6), "test colors should change visibly"


# --- Metadata ---

@needs_ocr
def test_metadata_is_stripped(tmp_path, monkeypatch):
    """PDF page images and JPEGs lose comments and EXIF/XMP/Photoshop data, and are tagged with the compact sRGB
    profile; JPEGs keep their DPI and are progressive, PDF page images aren't.
    OCR_OPTS without --optimize keeps OCRmyPDF from re-encoding (which would also drop the metadata)."""
    monkeypatch.setattr(ci, "OCR_OPTS", ["--optimize", "0", "--output-type", "pdf"])
    exif = Image.Exif()
    exif[0x0112] = 6
    (tmp_path / "in").mkdir()
    with Image.open(make_image(tmp_path / "gen" / "a.tif", 600, 400, "-depth", "8")) as im:
        im.convert("RGB").save(tmp_path / "in" / "a.jpg", exif=exif, comment=b"scan note", dpi=(300, 300))
    assert run("pdf", str(tmp_path / "in"), "--jpg", "--no-ocr") == 0

    with Image.open(tmp_path / "in" / "access" / "a.jpg") as im:
        assert 0x0112 not in im.getexif()
        assert "comment" not in im.info
        assert im.info["icc_profile"] == Path(ci.SRGB_PROFILE).read_bytes()
        assert im.info["dpi"] == pytest.approx((300, 300), abs=1)
        assert im.info.get("progressive")
    with pikepdf.open(tmp_path / "in" / "access" / "in.pdf") as pdf:
        (image,) = pdf.pages[0].images.values()
        data = image.read_raw_bytes()
        assert data[:2] == b"\xff\xd8"  # Still the JPEG img2pdf embedded, not re-encoded
        assert b"Exif\0" not in data[:65536]
        assert b"scan note" not in data[:65536]
        with Image.open(io.BytesIO(data)) as page_jpeg:
            assert page_jpeg.info["icc_profile"] == Path(ci.SRGB_PROFILE).read_bytes()
            assert not page_jpeg.info.get("progressive")  # Baseline: progressive only helps on the web
        assert b"ICC_PROFILE" in data[:65536]


# --- Orientation ---

def make_marked_image(path: Path, *opts: str) -> Path:
    """300x200 blue image with a red 60x60 square in the top-left corner of the stored pixels."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([ci.IMAGEMAGICK_CMD, "-size", "300x200", "xc:blue", "-fill", "red", "-draw", "rectangle 0,0 59,59",
                    "-type", "TrueColor", *opts, str(path)], check=True, capture_output=True)
    return path


def red_corner(im: Image.Image) -> str:
    """Which corner of the image is red."""
    w, h = im.size
    corners = {"top-left": (10, 10), "top-right": (w - 11, 10), "bottom-left": (10, h - 11),
               "bottom-right": (w - 11, h - 11)}
    red = [name for name, xy in corners.items() if im.convert("RGB").getpixel(xy)[0] > 200]
    assert len(red) == 1, red
    return red[0]


# (ImageMagick orientation, upright size, corner the stored top-left ends up in)
ORIENTATIONS = [
    ("TopLeft", (300, 200), "top-left"),          # As stored
    ("TopRight", (300, 200), "top-right"),        # Mirrored horizontally
    ("BottomRight", (300, 200), "bottom-right"),  # Rotated 180°
    ("RightTop", (200, 300), "top-right"),        # Rotated 90° clockwise (EXIF 6)
    ("LeftBottom", (200, 300), "bottom-left"),    # Rotated 90° counter-clockwise (EXIF 8)
]


@needs_ocr
@pytest.mark.parametrize("orientation, size, corner", ORIENTATIONS)
def test_orientation_tag_is_applied(tmp_path, orientation, size, corner):
    # Stored 300x200 at 300x150 DPI: 1 x 1.333 inches, so 1.333 x 1 inches when turned by 90°
    make_marked_image(tmp_path / "in" / "a.tif", "-units", "PixelsPerInch", "-density", "300x150",
                      "-orient", orientation)
    assert run("pdf", str(tmp_path / "in"), "--jpg", "--no-ocr") == 0

    turned = size == (200, 300)
    with Image.open(tmp_path / "in" / "access" / "a.jpg") as im:
        assert im.size == size
        assert red_corner(im) == corner
        assert im.info["dpi"] == pytest.approx((150, 300) if turned else (300, 150), abs=1)
    (page,) = page_info(tmp_path / "in" / "access" / "in.pdf")
    assert page["pixels"] == size
    assert page["rotate"] == 0
    assert page["size"] == pytest.approx((96, 72) if turned else (72, 96), abs=0.01)
    with pikepdf.open(tmp_path / "in" / "access" / "in.pdf") as pdf:
        (image,) = pdf.pages[0].images.values()
        assert red_corner(pikepdf.PdfImage(image).as_pil_image()) == corner


# --- Resizing ---

@needs_ocr
def test_resize_keeps_real_size(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "PDF_MAX_SIZE", 300)
    monkeypatch.setattr(ci, "JPG_MAX_SIZE", 200)
    make_image(tmp_path / "a.tif", 600, 400, *dpi_opts(300))
    assert run("pdf", str(tmp_path), "--jpg", "--no-ocr") == 0

    (page,) = page_info(tmp_path / "access" / f"{tmp_path.name}.pdf")
    assert page["pixels"] == (300, 200)
    assert page["size"] == pytest.approx((144, 96), abs=0.01)  # Original 600x400 px at 300 DPI
    with Image.open(tmp_path / "access" / "a.jpg") as im:
        assert im.size == (200, 133)
        assert im.info["dpi"] == pytest.approx((100, 100), abs=1)  # 300 DPI scaled by 200/600


# --- Splitting ---

@needs_ocr
def test_large_pdf_is_split_under_max_mb(tmp_path):
    for i in range(1, 7):
        make_noise_image(tmp_path / f"noise{i}.tif", 1000)
    assert run("pdf", str(tmp_path), "--max-mb", "1", "--no-ocr") == 0

    parts = sorted((tmp_path / "access").iterdir())
    assert len(parts) > 1
    assert [p.name for p in parts] == [f"{tmp_path.name}-part{i:02d}.pdf" for i in range(1, len(parts) + 1)]
    for part in parts:
        assert part.stat().st_size <= 1_000_000 or len(page_info(part)) == 1
    assert sum(len(page_info(p)) for p in parts) == 6


# --- --no-ocr ---

@needs_ocr
def test_no_ocr_ignores_missing_language(tmp_path):
    make_image(tmp_path / "a.tif")
    assert run("pdf", str(tmp_path), "--no-ocr", "--ocr-lang", "xyz") == 0
    (page,) = page_info(tmp_path / "access" / f"{tmp_path.name}.pdf")
    assert not page["text"]
    assert "Ignoring --ocr-lang xyz" in log_text(tmp_path)


# --- JPEG only ---

@needs_imagemagick
def test_jpg_only_makes_no_pdf(tmp_path):
    make_image(tmp_path / "a.tif")
    make_image(tmp_path / "b.jpg")
    assert run("jpg", str(tmp_path)) == 0
    assert sorted(p.name for p in (tmp_path / "access").iterdir()) == ["a.jpg", "b.jpg"]
    assert not (tmp_path / "_work").exists()


# --- Failures ---

@needs_ocr
def test_corrupt_image_fails_folder_keeps_work_and_continues(tmp_path):
    bad, good = tmp_path / "bad", tmp_path / "good"
    make_image(bad / "a.tif")
    (bad / "b.tif").write_bytes(b"not an image")
    make_image(good / "a.tif")

    assert run("pdf", str(tmp_path), "--no-ocr") == 1
    assert (bad / "_work").exists()  # Unexpected failure: kept for debugging
    assert not (bad / "access").exists()
    assert (good / "access" / "good.pdf").exists()
    assert "Finished with 1 failed folder(s)" in log_text(tmp_path)


@needs_ocr
def test_shared_output_folder_fails_both_folders_and_keeps_outputs(tmp_path):
    parent = tmp_path / "b01"
    make_image(parent / "a.tif")
    make_image(parent / "master" / "b.tif")
    make_image(tmp_path / "other" / "c.tif")
    access = parent / "access"
    access.mkdir()
    (access / "b01-part01.pdf").write_bytes(b"old")  # From an earlier run; must not be removed

    assert run("pdf", str(tmp_path), "--no-ocr") == 1
    assert [p.name for p in access.iterdir()] == ["b01-part01.pdf"]
    assert not (parent / "_work").exists() and not (parent / "master" / "_work").exists()
    assert (tmp_path / "other" / "access" / "other.pdf").exists()
    log = log_text(tmp_path)
    assert f"is also used by the images in {parent / 'master'}" in log
    assert f"is also used by the images in {parent}." in log
    assert "Finished with 2 failed folder(s)" in log


@needs_ocr
def test_name_clash_fails_folder_and_removes_split_pdfs(tmp_path):
    make_image(tmp_path / "a.tif")
    make_image(tmp_path / "a.jpg")
    pdf_dir = tmp_path / "access"
    pdf_dir.mkdir()
    for name in (f"{tmp_path.name}-part01.pdf", f"{tmp_path.name}.pdf", "unrelated.pdf"):
        (pdf_dir / name).write_bytes(b"old")
    (tmp_path / "_work").mkdir()  # Left by an earlier failed run
    (tmp_path / "_work" / "old.pdf").write_bytes(b"old")

    assert run("pdf", str(tmp_path), "--jpg", "--no-ocr") == 1
    assert not (tmp_path / "_work").exists()  # FolderError: an expected problem, so _work is deleted
    assert sorted(p.name for p in pdf_dir.iterdir()) == [f"{tmp_path.name}.pdf", "unrelated.pdf"]
    log = log_text(tmp_path)
    assert "These images would get the same JPEG name: a.jpg and a.tif" in log
    assert "Traceback" not in log
