import io
import json
import os

from duplikill import updater


class Fake(io.BytesIO):
    def __init__(self, data: bytes, headers=None):
        super().__init__(data)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _release(tag, assets=("Duplikill-Setup.exe",)):
    return json.dumps({"tag_name": tag, "assets": [{"name": n, "browser_download_url": "https://x/" + n, "size": 5}
                                                   for n in assets]}).encode()


def test_check(monkeypatch):
    for tag, assets, want in [("v0.2.0", ("Duplikill-Setup.exe",), "0.2.0"), ("v0.1.0", ("Duplikill-Setup.exe",), None),
                              ("v0.0.9", ("Duplikill-Setup.exe",), None), ("v0.3.0", ("other.zip",), None)]:
        monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: Fake(_release(tag, assets)))
        got = updater.check("0.1.0")
        assert (got and got["version"]) == want or (got is None and want is None)

    def boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(updater.urllib.request, "urlopen", boom)
    assert updater.check("0.1.0") is None
    assert updater.parse_version("v1.10.2") == (1, 10, 2) and updater.parse_version("junk") == ()


def test_download(tmp_path, monkeypatch):
    data = os.urandom(600000)
    monkeypatch.setattr(updater.urllib.request, "urlopen",
                        lambda *a, **k: Fake(data, {"Content-Length": str(len(data))}))
    calls = []
    path = updater.download("https://x/y", str(tmp_path / "u"), lambda d, t: calls.append((d, t)))
    assert open(path, "rb").read() == data
    assert calls[-1] == (len(data), len(data)) and len(calls) == 3
    assert not os.path.exists(path + ".part")
