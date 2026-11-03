import io
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from ugc_cutter import update
from ugc_cutter.app import MainWindow


def release(tag="v1.0.9", **extra):
    return {"tag_name": tag, "assets": [{"name": "ugc-cutter", "browser_download_url": "https://x/linux"},
                                        {"name": update.INSTALLER_NAME, "browser_download_url": "https://x/setup.exe"}],
            **extra}


def test_versions_compare_as_numbers_not_text():
    assert update.is_newer("v1.0.10", "1.0.9")
    assert not update.is_newer("v1.0.9", "1.0.10")
    assert not update.is_newer("v1.0.9", "1.0.9")


def test_running_from_source_never_finds_an_update():
    assert not update.is_newer("v9.9.9", "dev")
    assert update.check_latest("dev") is None


def test_a_newer_release_gives_the_version_and_installer():
    assert update.read_release(release(), "1.0.8") == {"version": "1.0.9", "installer_url": "https://x/setup.exe"}


def test_the_same_or_older_release_or_a_prerelease_is_not_offered():
    assert update.read_release(release(), "1.0.9") is None
    assert update.read_release(release(), "1.0.10") is None
    assert update.read_release(release(prerelease=True), "1.0.8") is None


class Response(io.BytesIO):
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_check_reads_github_and_any_failure_means_no_update(monkeypatch):
    monkeypatch.setattr(update.urllib.request, "urlopen", lambda *a, **k: Response(json.dumps(release()).encode()))
    assert update.check_latest("1.0.8")["version"] == "1.0.9"

    def offline(*a, **k):
        raise OSError("no network")
    monkeypatch.setattr(update.urllib.request, "urlopen", offline)
    assert update.check_latest("1.0.8") is None
    monkeypatch.setattr(update.urllib.request, "urlopen", lambda *a, **k: Response(b"not json"))
    assert update.check_latest("1.0.8") is None


def test_download_leaves_no_file_behind_if_it_fails(monkeypatch, tmp_path):
    def broken(*a, **k):
        raise OSError("dropped")
    monkeypatch.setattr(update.urllib.request, "urlopen", broken)
    with pytest.raises(OSError):
        update.download("https://x/setup.exe", tmp_path / "setup.exe")
    assert not (tmp_path / "setup.exe").exists()
    monkeypatch.setattr(update.urllib.request, "urlopen", lambda *a, **k: Response(b"installer"))
    update.download("https://x/setup.exe", tmp_path / "setup.exe")
    assert (tmp_path / "setup.exe").read_bytes() == b"installer"


def test_button_is_hidden_until_an_update_is_found():
    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    w.show()
    assert not w.update_button.isVisible()
    w._on_update_found({"version": "1.0.9", "installer_url": "https://x/setup.exe"})
    assert w.update_button.isVisible()
    assert "1.0.9" in w.update_button.toolTip()
    w.close()
