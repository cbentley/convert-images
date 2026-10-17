"""Tests of the pure-Python parts; no ImageMagick or OCRmyPDF needed."""
import io
from pathlib import Path

import pytest
from PIL import Image, ImageCms

import convert_images as ci


def touch(folder: Path, *names: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).touch()


# --- Image selection & naming ---

def test_natural_sort_key_orders_numbers_by_value_and_ignores_case():
    names = ["scan_10.tif", "Scan_2.tif", "scan_1.tif", "scan_01.tif", "SCAN_3.tif"]
    ordered = sorted((Path(n) for n in names), key=ci.natural_sort_key)
    # scan_01 and scan_1 tie by value; the full name breaks the tie
    assert [p.name for p in ordered] == ["scan_01.tif", "scan_1.tif", "Scan_2.tif", "SCAN_3.tif", "scan_10.tif"]


@pytest.mark.parametrize("name, wanted", [
    ("a.tif", True),
    ("a.TIFF", True),
    ("a.jpg", True),
    ("a.JPEG", True),
    ("a.jp2", True),
    ("a.jpf", True),
    ("a.png", False),
    ("a.pdf", False),
    ("a-ruler.tif", False),
    ("RULER_a.tif", False),
])
def test_is_wanted_image(name, wanted):
    assert ci.is_wanted_image(Path(name)) is wanted


def test_select_images_returns_wanted_images_in_natural_order(tmp_path):
    touch(tmp_path, "p10.tif", "p2.jpg", "p1.TIF", "ruler.tif", "notes.txt")
    assert [p.name for p in ci.select_images(tmp_path)] == ["p1.TIF", "p2.jpg", "p10.tif"]


def test_find_image_folders_skips_work_log_and_output_folders(tmp_path):
    touch(tmp_path, "top.tif")
    touch(tmp_path / "a" / "master", "1.tif")
    touch(tmp_path / "a" / "master" / "deeper", "2.jpg")
    touch(tmp_path / "b", "only-ruler.tif", "readme.txt")
    for skipped in ("_work", "_logs", "pdf", "JPG", "Pdf"):
        touch(tmp_path / "a" / skipped, "x.jpg")
        touch(tmp_path / "a" / skipped / "sub", "x.jpg")
    found = sorted(ci.find_image_folders(tmp_path))
    assert found == [tmp_path, tmp_path / "a" / "master", tmp_path / "a" / "master" / "deeper"]


@pytest.mark.parametrize("folder, name", [
    ("b01-f01/master", "b01-f01"),
    ("b01-f01/Master", "b01-f01"),
    ("b01-f01/scans", "scans"),
])
def test_output_base_name(folder, name):
    assert ci.output_base_name(Path("/data") / folder) == name


def test_check_jpg_name_clashes_is_case_insensitive():
    with pytest.raises(ci.FolderError, match="a.tif and A.jpg"):
        ci.check_jpg_name_clashes([Path("a.tif"), Path("A.jpg"), Path("b.tif")])


def test_check_jpg_name_clashes_accepts_distinct_names():
    ci.check_jpg_name_clashes([Path("a.tif"), Path("b.jpg")])


# --- Image conversion helpers ---

def test_page_size_points_uses_dpi():
    info = ci.ImageInfo(width=600, height=400, x_dpi=400, y_dpi=200, has_dpi=True, frames=1)
    assert ci.page_size_points(info) == (108, 144)


def test_page_size_points_falls_back_without_dpi():
    info = ci.ImageInfo(width=600, height=300, x_dpi=72, y_dpi=72, has_dpi=False, frames=1)
    assert ci.page_size_points(info) == (600 / ci.FALLBACK_DPI * 72, 300 / ci.FALLBACK_DPI * 72)


SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def jpeg_bytes(mode: str, icc: bytes | None) -> bytes:
    out = io.BytesIO()
    Image.new(mode, (16, 16)).save(out, "JPEG", **({"icc_profile": icc} if icc else {}))
    return out.getvalue()


@pytest.mark.parametrize("mode", ["CMYK", "L"])
def test_strip_mismatched_icc_removes_rgb_profile_from_non_rgb_jpeg(mode):
    jpeg = jpeg_bytes(mode, SRGB_ICC)
    stripped = ci.strip_mismatched_icc(jpeg)
    with Image.open(io.BytesIO(stripped)) as im:
        assert "icc_profile" not in im.info
        assert im.mode == mode
    # Only the profile segment is removed
    with Image.open(io.BytesIO(jpeg)) as before, Image.open(io.BytesIO(stripped)) as after:
        assert before.tobytes() == after.tobytes()


@pytest.mark.parametrize("icc", [SRGB_ICC, None])
def test_strip_mismatched_icc_keeps_matching_or_missing_profile(icc):
    jpeg = jpeg_bytes("RGB", icc)
    assert ci.strip_mismatched_icc(jpeg) is jpeg


# --- Command line ---

def test_parse_args_pdf_defaults(tmp_path):
    args = ci.parse_args(["pdf", str(tmp_path)])
    assert (args.make_pdf, args.make_jpg) == (True, False)
    assert args.max_mb == 100
    assert args.ocr_lang == ci.DEFAULT_OCR_LANG
    assert args.no_ocr is False


def test_parse_args_pdf_options(tmp_path):
    args = ci.parse_args(["pdf", str(tmp_path), "--max-mb", "5", "--ocr-lang", "eng", "--jpg"])
    assert (args.make_pdf, args.make_jpg, args.max_mb, args.ocr_lang) == (True, True, 5, "eng")


def test_parse_args_no_ocr_leaves_language_unset(tmp_path):
    assert ci.parse_args(["pdf", str(tmp_path), "--no-ocr"]).ocr_lang is None
    assert ci.parse_args(["pdf", str(tmp_path), "--no-ocr", "--ocr-lang", "fra"]).ocr_lang == "fra"


def test_parse_args_jpg(tmp_path):
    args = ci.parse_args(["jpg", str(tmp_path)])
    assert (args.make_pdf, args.make_jpg) == (False, True)


@pytest.mark.parametrize("argv", [
    ["pdf", "/does/not/exist"],
    ["pdf", ".", "--max-mb", "big"],
    ["jpg", ".", "--max-mb", "5"],
    ["jpeg", "."],
    ["pdf", ".", "--jpeg"],
    [],
])
def test_parse_args_errors_exit_with_code_2(argv):
    with pytest.raises(SystemExit) as exc:
        ci.parse_args(argv)
    assert exc.value.code == 2


# --- Output files ---

def test_move_outputs_replaces_same_name_and_keeps_others(tmp_path):
    work, dest = tmp_path / "work", tmp_path / "dest"
    touch(dest, "keep.pdf")
    (dest / "a.pdf").write_text("old")
    work.mkdir()
    (work / "a.pdf").write_text("new")
    ci.move_outputs([work / "a.pdf"], dest)
    assert (dest / "a.pdf").read_text() == "new"
    assert (dest / "keep.pdf").exists()
    assert not (work / "a.pdf").exists()


def test_remove_part_pdfs_removes_only_this_folders_parts(tmp_path):
    touch(tmp_path, "b01-part01.pdf", "b01-part02.pdf", "b01.pdf", "b02-part01.pdf")
    ci.remove_part_pdfs(tmp_path, "b01")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b01.pdf", "b02-part01.pdf"]
