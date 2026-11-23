from urllib.parse import parse_qs, urlparse

from duplikill import report


def test_anonymise_hides_photo_paths_keeps_own_source():
    t = ('File "C:\\Users\\someone\\AppData\\Local\\Temp\\_MEI123\\duplikill\\app.py", line 5\n'
         "OSError: cannot open 'E:\\Pictures\\2019 Family\\IMG_1.JPG' or \\\\nas\\share\\x.png")
    out = report.anonymise(t)
    assert "someone" not in out and "Family" not in out and "nas" not in out
    assert 'File "duplikill/app.py", line 5' in out
    assert "<PATH>.JPG" in out and "<PATH>.png" in out


def test_bug_url_prefilled_and_short():
    url = report.bug_url("0.2.0", "Traceback ...\nValueError: boom\n" + "x" * 20000)
    q = parse_qs(urlparse(url).query)
    assert q["title"] == ["Bug: " + "x" * 80] or q["title"][0].startswith("Bug: ")
    assert "Duplikill 0.2.0" in q["body"][0]
    assert len(url) < 20000


def test_stats_count_and_undo(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "data_dir", lambda: str(tmp_path))
    s = report.load_stats()
    report.count_removed(s, 3, 3000, "move", True)
    report.count_removed(s, 1, 500, "trash", False)
    report.count_undo(s, 1, 500, "trash", False)
    report.save_stats(s)
    s2 = report.load_stats()
    assert (s2["images"], s2["bytes"], s2["groups"]) == (3, 3000, 1)
    assert s2["by_action"] == {"move": 3, "trash": 0}
    assert "3 duplicates" in parse_qs(urlparse(report.stats_url("0.2.0", s2)).query)["title"][0]
