"""Checks GitHub for a newer release and fetches the installer. No Qt or video code here."""
import json
import urllib.request

REPO = "AndrewCromar/ugc-cutter"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
INSTALLER_NAME = "ugc-cutter-setup.exe"


def parse_version(text):
    """"v1.0.12" -> (1, 0, 12). Anything else (such as "dev") -> None."""
    try:
        parts = tuple(int(p) for p in text.strip().lstrip("v").split("."))
    except (AttributeError, ValueError):
        return None
    return parts or None


def is_newer(latest, current):
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b and a > b)


def read_release(data, current):
    """{"version", "installer_url"} if this release is newer than current, otherwise None."""
    tag = data.get("tag_name", "")
    if data.get("draft") or data.get("prerelease") or not is_newer(tag, current):
        return None
    url = next((a.get("browser_download_url") for a in data.get("assets", []) if a.get("name") == INSTALLER_NAME), None)
    return {"version": tag.lstrip("v"), "installer_url": url}


def check_latest(current, timeout=8):
    """The newer release, or None. Any failure (offline, a private repo) just means no button."""
    if parse_version(current) is None:
        return None
    request = urllib.request.Request(LATEST_URL, headers={"Accept": "application/vnd.github+json",
                                                          "User-Agent": "ugc-cutter"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return read_release(json.load(response), current)
    except (OSError, ValueError):
        return None


def download(url, path, progress=None, timeout=30):
    """Save url to path, reporting percent done. A partial download never reaches path."""
    part = str(path) + ".part"
    request = urllib.request.Request(url, headers={"User-Agent": "ugc-cutter"})
    with urllib.request.urlopen(request, timeout=timeout) as response, open(part, "wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while chunk := response.read(1 << 18):
            out.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(int(done * 100 / total))
    import os
    os.replace(part, path)
    return path
