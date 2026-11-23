r"""How often Duplikill was downloaded: GitHub counts every release asset download itself.

Updates from inside the app download Duplikill-Setup.exe too, so they are counted here as well.
No telemetry needed.  Run:  .venv\Scripts\python.exe -I tools\downloads.py
"""
import json
import urllib.request

URL = "https://api.github.com/repos/metamountain/duplikill/releases?per_page=100"

req = urllib.request.Request(URL, headers={"User-Agent": "Duplikill-stats", "Accept": "application/vnd.github+json"})
with urllib.request.urlopen(req, timeout=10) as r:
    releases = json.load(r)
total = 0
for rel in releases:
    print(f"{rel['tag_name']:10} {rel['published_at'][:10]}")
    for a in rel["assets"]:
        print(f"    {a['download_count']:6d}  {a['name']}")
        total += a["download_count"]
print(f"total downloads: {total}")
