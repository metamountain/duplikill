import json
import os
import shutil

import pytest

from duplikill import czkawka
from tests.test_core import _img


def _fake_json(tmp_path, groups):
    p = tmp_path / "out.json"
    p.write_text(json.dumps(groups), encoding="utf-8")
    return str(p)


def test_parse_fixes_case_and_drops_missing(tmp_path):
    d = tmp_path / "Fotos" / "Urlaub"
    d.mkdir(parents=True)
    a, b = d / "IMG_1.JPG", d / "IMG_2.JPG"
    a.write_bytes(b"x" * 10)
    b.write_bytes(b"y" * 20)
    entry = lambda p, s, diff: {"path": str(p).lower(), "size": s, "width": 4, "height": 3,
                                "modified_date": 1600000000, "hashes": [], "difference": diff}
    out = _fake_json(tmp_path, [
        [entry(a, 10, 0), entry(b, 20, 3)],
        [entry(a, 10, 0), entry(d / "gone.jpg", 5, 1)],   # only one file left -> dropped
    ])
    groups = czkawka.parse(out)
    assert len(groups) == 1
    paths = sorted(p.path for p in groups[0].photos)
    assert paths == sorted([str(a), str(b)])               # real spelling, not lower case
    diffs = {os.path.basename(p.path): p.diff for p in groups[0].photos}
    assert diffs == {"IMG_1.JPG": 0, "IMG_2.JPG": 3}
    assert groups[0].photos[0].width == 4


def test_command_line():
    cmd = czkawka.command("D:\\x", "out.json", czkawka.Options(max_diff=12, excluded=["D:\\x\\skip"]))
    assert cmd[1:4] == ["image", "-d", "D:\\x"]
    assert cmd[cmd.index("-s") + 1] == "12"
    assert cmd[cmd.index("-e") + 1] == "D:\\x\\skip"
    assert {"-C", "-N", "-W"} <= set(cmd)


def test_count_images(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "big.jpg").write_bytes(b"x" * 20000)
    (tmp_path / "small.png").write_bytes(b"x" * 100)       # below czkawka's 16 KB minimum
    (tmp_path / "doc.txt").write_bytes(b"x" * 20000)
    (tmp_path / "skip").mkdir()
    (tmp_path / "skip" / "c.jpg").write_bytes(b"x" * 20000)
    assert czkawka.count_images(str(tmp_path), [str(tmp_path / "skip")]) == 1


@pytest.mark.skipif(not os.path.exists(czkawka.exe_path()), reason="czkawka_cli missing")
def test_real_search(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _img(root / "x1.jpg", 1, size=(1600, 1200))
    shutil.copy(root / "x1.jpg", root / "x1 (1).jpg")
    _img(root / "y.jpg", 99, size=(1600, 1200))
    groups = czkawka.search(str(root), czkawka.Options(use_cache=False), workdir=str(tmp_path))
    assert len(groups) == 1
    assert sorted(os.path.basename(p.path) for p in groups[0].photos) == ["x1 (1).jpg", "x1.jpg"]
