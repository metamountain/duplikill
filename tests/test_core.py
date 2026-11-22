import os, shutil, time
import numpy as np
from PIL import Image, ImageFilter
import pytest

from duplikill import index, groups, actions


def _img(path, seed, blur=0, size=(800, 600), exif_time=None):
    rng = np.random.default_rng(seed)
    a = (rng.random((60, 80, 3)) * 255).astype("uint8")
    im = Image.fromarray(a).resize(size, Image.BICUBIC)
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    ex = Image.Exif()
    if exif_time:
        ex[0x8769] = {36867: exif_time}
    im.save(path, "JPEG", quality=90, exif=ex.tobytes())


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    root = tmp_path / "photos"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    _img(root / "a" / "x1.jpg", 1, exif_time="2020:05:01 10:00:00")
    shutil.copy(root / "a" / "x1.jpg", root / "b" / "x1 (1).jpg")          # exact duplicate
    _img(root / "a" / "x2.jpg", 1, blur=2, exif_time="2020:05:01 10:00:01")  # similar + burst, blurrier
    _img(root / "a" / "y.jpg", 99, exif_time="2020:05:01 12:00:00")          # unrelated
    Image.open(root / "a" / "x1.jpg").resize((400, 300)).save(root / "a" / "x1_small.jpg")  # similar, smaller
    (root / "a" / "x2.xmp").write_text("<x/>")
    return root


def test_scan_and_groups(lib):
    cache = index.Cache()
    photos = index.scan([str(lib)], cache, index.CpuEngine(2), lambda *a: None)
    assert len(photos) == 5
    p = {os.path.basename(x.path): x for x in photos}
    assert p["x1.jpg"].width == 800 and p["x1.jpg"].taken
    assert p["x1.jpg"].sharp > p["x2.jpg"].sharp

    ex = groups.find("exact", photos, None, cache, lambda *a: None)
    assert len(ex) == 1 and len(ex[0].photos) == 2
    assert os.path.basename(ex[0].photos[ex[0].best].path) == "x1.jpg"   # copy "(1)" is not the suggestion

    sim = groups.find("similar", photos, "Normal", cache, lambda *a: None, use_cuda=False)
    names = sorted(os.path.basename(x.path) for x in sim[0].photos)
    assert len(sim) == 1 and "y.jpg" not in names and "x1_small.jpg" in names and "x2.jpg" in names

    bu = groups.find("burst", photos, "2 s", cache, lambda *a: None)
    assert len(bu) == 1
    assert os.path.basename(bu[0].photos[bu[0].best].path) in ("x1.jpg", "x1 (1).jpg")

    # second scan uses the cache (no re-analysis)
    class Boom:
        def analyze(self, *a, **k):
            raise AssertionError("should be cached")
    assert len(index.scan([str(lib)], cache, Boom(), lambda *a: None)) == 5


def test_move_and_undo(lib, tmp_path):
    target = tmp_path / "out"
    m = actions.Mover(str(target))
    src = str(lib / "a" / "x2.jpg")
    moved = m.move([src], "g1")
    assert not os.path.exists(src) and not os.path.exists(str(lib / "a" / "x2.xmp"))
    assert len(moved) == 2 and all(os.path.exists(d) for _, d in moved)
    assert "photos" in moved[0][1] and moved[0][1].startswith(str(target))
    assert m.stats()[0] == 2
    entry = m.undo_last()
    assert entry["group"] == "g1" and os.path.exists(src) and os.path.exists(str(lib / "a" / "x2.xmp"))
    assert m.undo_last() is None


def test_done_groups_hidden(lib):
    cache = index.Cache()
    photos = index.scan([str(lib)], cache, index.CpuEngine(2), lambda *a: None)
    g = groups.find("exact", photos, None, cache, lambda *a: None)[0]
    cache.mark_done(g.key)
    assert groups.find("exact", photos, None, cache, lambda *a: None) == []


def test_cuda_matches_cpu(lib):
    torch = pytest.importorskip("torch")
    if not torch.cuda.is_available():
        pytest.skip("no CUDA")
    paths = [str(p) for p in sorted(lib.rglob("*.jpg"))]
    cpu = {r[0]: r for r in index.CpuEngine(2).analyze(paths, lambda *a: None)}
    gpu = {r[0]: r for r in index.CudaEngine(batch=3, workers=2).analyze(paths, lambda *a: None)}
    assert cpu.keys() == gpu.keys()
    for p in paths:
        c, g = cpu[p], gpu[p]
        assert c[1:4] == g[1:4]
        assert bin(c[4] ^ g[4]).count("1") <= 4, p          # hashes nearly identical
        assert abs(c[5] - g[5]) / max(c[5], 1) < 0.15, p   # sharpness close


def test_auto_target_is_duplikill_folder_on_same_drive():
    from duplikill.actions import target_for
    assert target_for(r"D:\Pictures\2019\a.jpg", "") == r"D:\Duplikill\Pictures\2019\a.jpg"
    assert target_for(r"D:\Pictures\2019\a.jpg", r"E:\out") == r"E:\out\D\Pictures\2019\a.jpg"


def test_target_modes():
    from duplikill.actions import target_for
    p = r"D:\Pics\2019\Trip\a.jpg"
    assert target_for(p, "", "central") == r"D:\Duplikill\Pics\2019\Trip\a.jpg"
    assert target_for(p, "", "folder", r"D:\Pics\2019") == r"D:\Pics\2019\Duplikill\Trip\a.jpg"
    assert target_for(p, r"E:\out", "custom") == r"E:\out\D\Pics\2019\Trip\a.jpg"
