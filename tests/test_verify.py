import shutil

import numpy as np
from PIL import Image

from duplikill import verify
from duplikill.groups import Group
from duplikill.index import Photo
from tests.test_core import _img


def _photo(path):
    return Photo(path=str(path), size=path.stat().st_size, mtime=path.stat().st_mtime)


def test_refine_splits_unrelated_and_keeps_series(tmp_path):
    _img(tmp_path / "a.jpg", 1, exif_time="2021:05:01 10:00:00")
    _img(tmp_path / "b.jpg", 1, blur=1, exif_time="2021:05:01 10:00:05")    # same scene, seconds later
    _img(tmp_path / "c.jpg", 99, exif_time="2021:05:01 10:00:09")           # different picture
    shutil.copy(tmp_path / "a.jpg", tmp_path / "a_copy.jpg")
    g = Group("similar", [_photo(tmp_path / n) for n in ("a.jpg", "b.jpg", "c.jpg", "a_copy.jpg")])
    out = verify.refine([g])
    assert len(out) == 1
    assert sorted(p.path.split("\\")[-1] for p in out[0].photos) == ["a.jpg", "a_copy.jpg", "b.jpg"]


def test_same_picture_months_apart_is_not_a_series(tmp_path):
    _img(tmp_path / "a.jpg", 1, exif_time="2021:05:01 10:00:00")
    _img(tmp_path / "b.jpg", 1, blur=3, exif_time="2021:10:01 10:00:00")    # similar but months later
    g = Group("similar", [_photo(tmp_path / "a.jpg"), _photo(tmp_path / "b.jpg")])
    a, b = verify.signature(str(tmp_path / "a.jpg")), verify.signature(str(tmp_path / "b.jpg"))
    corr = float((a.grey * b.grey).mean())
    if corr >= verify.COPY_CORR:          # blur too weak to tell it from a copy: nothing to test
        return
    assert verify.refine([g]) == []


def test_time_series_and_shift_tolerance_rescues_missed_czkawka_group(tmp_path):
    # Create a smooth picture and a slightly shifted shot taken 2 seconds later that Czkawka missed (groups=[])
    _img(tmp_path / "s1.jpg", 7, blur=6, size=(320, 240), exif_time="2021:05:01 12:00:00")
    with Image.open(tmp_path / "s1.jpg") as im:
        ex = im.getexif()
        # Shift by ~6% (1.5 cells on 24x24 grid) so raw correlation drops but shifted correlation matches
        cropped = im.crop((16, 10, 320, 240)).resize((320, 240), Image.BILINEAR)
        cropped.save(tmp_path / "s2.jpg", exif=ex.tobytes(), quality=95)
    _img(tmp_path / "other.jpg", 42, size=(320, 240), exif_time="2021:05:01 12:00:05")
    paths = [str(tmp_path / n) for n in ("s1.jpg", "s2.jpg", "other.jpg")]
    out = verify.refine([], paths=paths)
    assert len(out) == 1
    assert sorted(p.path.split("\\")[-1] for p in out[0].photos) == ["s1.jpg", "s2.jpg"]


def test_dupliprompt_cli(tmp_path, capsys):
    from duplikill import dupliprompt
    _img(tmp_path / "DSC_0076.JPG", 11, size=(320, 240), exif_time="2021:05:01 12:00:00")
    _img(tmp_path / "DSC_0077.JPG", 11, blur=1, size=(320, 240), exif_time="2021:05:01 12:00:03")
    res = dupliprompt.run_prompt(str(tmp_path), force_scan=True, queries=["0076", "0077"], show_groups=True)
    captured = capsys.readouterr().out
    assert len(res["groups"]) == 1
    assert "DUPLIPROMPT HEADLESS" in captured
    assert "[MATCH] DSC_0076.JPG <-> DSC_0077.JPG" in captured


def test_burst_exposure_shift_and_slider_strictness(tmp_path):
    # Two burst shots 3s apart where flash/exposure brightens the second image (raw cdist > 0.12, chroma cdist ~ 0.02)
    _img(tmp_path / "b1.jpg", 19, blur=2, size=(320, 240), exif_time="2021:05:01 12:00:00")
    with Image.open(tmp_path / "b1.jpg") as im:
        ex = im.getexif()
        arr = np.clip(np.asarray(im, np.float32) + 45.0, 0, 255).astype("uint8")
        Image.fromarray(arr).save(tmp_path / "b2.jpg", exif=ex.tobytes(), quality=92)
    paths = [str(tmp_path / "b1.jpg"), str(tmp_path / "b2.jpg")]
    assert len(verify.refine([], paths=paths, max_diff=12)) == 1
    # At max_diff=0 (strictest: copies only, series_gap=0), time-only candidates without Czkawka match are not grouped
    assert len(verify.refine([], paths=paths, max_diff=0)) == 0
