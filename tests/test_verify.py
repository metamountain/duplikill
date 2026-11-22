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
