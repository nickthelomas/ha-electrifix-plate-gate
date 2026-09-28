"""Fetch a few Wikimedia Commons photos of cars/driveways as sandbox footage (local fixtures only)."""
import json, time, urllib.parse, urllib.request
from pathlib import Path

OUT = Path(__file__).parent / "media" / "frigate" / "clips"
UA = {"User-Agent": "PlateGateSandbox/1.0 (https://github.com/nickthelomas/ha-electrifix-plate-gate; sandbox test fixtures)"}


def api(q):
    url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "generator": "search", "gsrsearch": q, "gsrnamespace": 6, "gsrlimit": 8,
        "prop": "imageinfo", "iiprop": "url|mime", "iiurlwidth": 1280, "format": "json"})
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30))


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    n = 0
    for q in ("car parked in driveway suburban", "pickup truck driveway", "sedan car front view street"):
        for p in api(q).get("query", {}).get("pages", {}).values():
            ii = (p.get("imageinfo") or [{}])[0]
            if ii.get("mime") != "image/jpeg" or not ii.get("thumburl"):
                continue
            n += 1
            (OUT / f"driveway-17590400{n:02d}.000001-abc{n}-clean.jpg").write_bytes(
                urllib.request.urlopen(urllib.request.Request(ii["thumburl"], headers=UA), timeout=60).read())
            print(p.get("title"))
            time.sleep(0.5)
            if n >= 6:
                break
        if n >= 6:
            break
