import os

from duplikill import session
from duplikill.groups import Group
from duplikill.index import Photo


def _photos(tmp_path, names):
    out = []
    for n in names:
        p = tmp_path / n
        p.write_bytes(b"x" * 10)
        out.append(Photo(path=str(p), size=10, mtime=float(int(os.stat(p).st_mtime)), width=4, height=3))
    return out


def test_round_trip_prune_and_marks(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "data_dir", lambda: str(tmp_path / "data"))
    a = Group("similar", _photos(tmp_path, ["a1.jpg", "a2.jpg", "a3.jpg"]))
    b = Group("similar", _photos(tmp_path, ["b1.jpg", "b2.jpg"]))
    fp = session.fingerprint([p.path for g in (a, b) for p in g.photos])
    assert fp["count"] == 5 and fp["bytes"] == 50
    session.save("R", 12, 8, fp, [a, b], {a.key: [a.photos[2].path]}, [], 1)
    os.remove(a.photos[0].path)                              # a keeps 2 photos, mark moves from 2 to 1
    os.remove(b.photos[0].path)                              # b drops below 2 -> gone
    s = session.load("R", 12, 8)
    assert [len(g.photos) for g in s["groups"]] == [2]
    g = s["groups"][0]
    assert s["marks"] == {g.key: {1}} and g.photos[1].path.endswith("a3.jpg")
    assert s["position"] == 0 and s["fingerprint"] == fp
    assert session.load("R", 13, 8) is None                  # other strictness: other file


def test_adjust_and_corrupt(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "data_dir", lambda: str(tmp_path))
    ps = _photos(tmp_path, ["x.jpg", "y.jpg"])
    fp = session.fingerprint([p.path for p in ps])
    less = session.adjust(fp, ps[:1], -1)
    assert less == session.fingerprint([ps[1].path])
    assert session.adjust(less, ps[:1], +1) == fp
    os.makedirs(os.path.dirname(session._file("R", 1, 8)), exist_ok=True)
    with open(session._file("R", 1, 8), "w") as fh:
        fh.write("{broken")
    assert session.load("R", 1, 8) is None
