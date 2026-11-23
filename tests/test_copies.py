import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PIL import Image

from duplikill import actions, copies
from duplikill.groups import Group
from duplikill.index import Photo

XMP = (b"http://ns.adobe.com/xap/1.0/\x00"
       b"<x:xmpmeta><rdf:li PersonDisplayName='Someone'/></x:xmpmeta>")


def with_xmp(src: str, dst: str) -> None:
    """Same JPEG with an XMP segment inserted after SOI (picture data untouched)."""
    data = open(src, "rb").read()
    seg = b"\xff\xe1" + (len(XMP) + 2).to_bytes(2, "big") + XMP
    open(dst, "wb").write(data[:2] + seg + data[2:])


def photo(p: str) -> Photo:
    st = os.stat(p)
    with Image.open(p) as im:
        w, h = im.size
    return Photo(path=p, size=st.st_size, mtime=st.st_mtime, width=w, height=h)


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """root\\ holds 6 tagged pictures + 4 others; root\\album\\ holds untagged copies of the 6 + 1 own."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    copies._cache.clear()
    root, album = tmp_path / "root", tmp_path / "root" / "album"
    album.mkdir(parents=True)
    rng = np.random.default_rng(1)
    for i in range(6):
        plain = str(album / f"img{i}.jpg")
        Image.fromarray(rng.integers(0, 255, (40, 60, 3), dtype=np.uint8)).save(plain, quality=90)
        with_xmp(plain, str(root / f"img{i}.jpg"))
    for i in range(4):
        Image.fromarray(rng.integers(0, 255, (40, 60, 3), dtype=np.uint8)).save(str(root / f"other{i}.jpg"))
    Image.fromarray(rng.integers(0, 255, (40, 60, 3), dtype=np.uint8)).save(str(album / "own.jpg"))
    (root / "img0.jpg.xmp").write_text("sidecar")
    paths = [str(p) for p in root.rglob("*.jpg")]
    groups = [Group("similar", [photo(str(album / f"img{i}.jpg")), photo(str(root / f"img{i}.jpg"))])
              for i in range(6)]
    return root, album, paths, groups


def test_scan_ignores_metadata(archive):
    root, album, *_ = archive
    a, b = copies.scan(str(album / "img1.jpg")), copies.scan(str(root / "img1.jpg"))
    assert a[0] == b[0]                     # same picture data
    assert b[1] > a[1] == 0                 # only the root copy has tags
    assert copies.scan(str(root / "other0.jpg"))[0] != a[0]


def test_find_suggests_album_and_swaps_tagged(archive):
    root, album, paths, groups = archive
    [m] = copies.find(groups, paths)
    assert os.path.normcase(m.keep) == os.path.normcase(str(album))          # 6 of 7 vs 6 of 10
    assert (m.count, m.n_keep, m.n_other, m.swaps) == (6, 7, 10, 6)
    remove, renames = m.plan()
    assert sorted(map(os.path.basename, remove)) == [f"img{i}.jpg" for i in range(6)]
    assert all(os.path.dirname(r) == str(album) for r in remove)             # untagged album copies go ...
    assert all(os.path.dirname(s) == str(root) and os.path.dirname(d) == str(album) for s, d in renames)
    f = m.flipped()                                                          # ... the other way: keep root
    assert os.path.normcase(f.keep) == os.path.normcase(str(root)) and f.swaps == 0
    assert all(os.path.dirname(r) == str(album) for r in f.plan()[0])
    assert copies.find(groups, paths, min_copies=7) == []
    assert copies.find(groups, paths, skip=[str(album)]) == []


def test_merge_and_undo_in_one_step(archive, tmp_path):
    root, album, paths, groups = archive
    [m] = copies.find(groups, paths)
    before = {p: open(p, "rb").read() for p in paths}
    mover = actions.Mover(str(tmp_path / "bin"))
    mover.move([str(root / "other3.jpg")], "g")              # an older, unrelated step
    remove, renames = m.plan()
    mover.merge(remove, renames, "merge:1")
    assert sorted(os.listdir(root)) == ["album", "other0.jpg", "other1.jpg", "other2.jpg"]
    assert open(album / "img2.jpg", "rb").read() == before[str(root / "img2.jpg")]   # tagged one moved in
    assert (album / "img0.jpg.xmp").read_text() == "sidecar"                         # sidecar along
    assert len(mover.bin_items()) == 7                                               # 6 untagged + other3
    mover.undo_last("merge:1")
    for p, data in before.items():
        if not p.endswith("other3.jpg"):
            assert open(p, "rb").read() == data
    assert (root / "img0.jpg.xmp").exists() and not (album / "img0.jpg.xmp").exists()
    assert [e["src"] for e in mover.bin_items()] == [str(root / "other3.jpg")]      # older step untouched


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_same_folder_copies_and_merge(tmp_path, monkeypatch, qapp):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    copies._cache.clear()
    folder = tmp_path / "folder"
    folder.mkdir()
    rng = np.random.default_rng(2)
    for i in range(6):
        base = str(folder / f"img{i}.jpg")
        copy = str(folder / f"img{i} (1).jpg")
        if i < 3:
            Image.fromarray(rng.integers(0, 255, (40, 60, 3), dtype=np.uint8)).save(copy, quality=90)
            with_xmp(copy, base)                # 3 pairs: XMP on the original, plain copy
        else:
            Image.fromarray(rng.integers(0, 255, (40, 60, 3), dtype=np.uint8)).save(base, quality=90)
            with_xmp(base, copy)                # 3 pairs: XMP + sidecar on the copy
            (folder / f"img{i} (1).jpg.xmp").write_text("sidecar")
    paths = [str(p) for p in folder.rglob("*.jpg")]
    groups = [Group("similar", [photo(str(folder / f"img{i}.jpg")), photo(str(folder / f"img{i} (1).jpg"))])
              for i in range(6)]
    [m] = copies.find(groups, paths)
    assert m.same_folder is True
    assert (m.count, m.swaps) == (6, 3)
    from duplikill.app import MergeScreen
    screen = MergeScreen()
    screen.ask(m, 0, 1)
    assert any("EXACT COPIES IN ONE FOLDER:" in line for line in screen.lines())
    before = {p: open(p, "rb").read() for p in paths}
    sidecars = {str(folder / f"img{i} (1).jpg.xmp"): "sidecar" for i in range(3, 6)}
    mover = actions.Mover(str(tmp_path / "bin"))
    remove, renames = m.plan()
    mover.merge(remove, renames, "merge:same")
    assert sorted(os.path.basename(p) for p in folder.iterdir() if p.suffix == ".jpg") == \
           [f"img{i}.jpg" for i in range(6)]
    for i in range(3):
        assert open(folder / f"img{i}.jpg", "rb").read() == before[str(folder / f"img{i}.jpg")]   # tagged original kept
    for i in range(3, 6):
        assert open(folder / f"img{i}.jpg", "rb").read() == before[str(folder / f"img{i} (1).jpg")]   # tagged copy renamed
        assert (folder / f"img{i}.jpg.xmp").read_text() == "sidecar"
    assert len(mover.bin_items()) == 6
    mover.undo_last("merge:same")
    for p, data in before.items():
        assert open(p, "rb").read() == data
    for p, text in sidecars.items():
        assert open(p).read() == text
    assert len(mover.bin_items()) == 0
