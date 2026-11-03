"""Start the app: ugc-cutter [folder], or ugc-cutter --self-test folder to check it works with no screen."""
import os
import sys


def apply_dark_theme(app):
    """The same look on Linux and Windows."""
    from PySide6.QtGui import QColor, QPalette
    app.setStyle("Fusion")
    palette = QPalette()
    for role, colour in ((QPalette.Window, "#1f1f23"), (QPalette.WindowText, "#e6e6ea"), (QPalette.Base, "#18181b"),
                         (QPalette.AlternateBase, "#27272a"), (QPalette.Text, "#e6e6ea"), (QPalette.Button, "#2f2f35"),
                         (QPalette.ButtonText, "#e6e6ea"), (QPalette.ToolTipBase, "#27272a"),
                         (QPalette.ToolTipText, "#e6e6ea"), (QPalette.Highlight, "#2ea043"),
                         (QPalette.HighlightedText, "#ffffff")):
        palette.setColor(role, QColor(colour))
    app.setPalette(palette)


def self_test(folder):
    """Open a folder, decode real frames and write a cut file, then report. Used by the automatic builds."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from pathlib import Path
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from .app import MainWindow

    app = QApplication([])
    window = MainWindow()
    window.show()
    results = {"frames": 0, "size": None, "rotation": None, "errors": []}

    def on_frame(frame):
        if frame.isValid():
            results["frames"] += 1
            results["size"] = (frame.width(), frame.height())
            results["rotation"] = frame.rotation().value
    window.video.videoSink().videoFrameChanged.connect(on_frame)
    window.player.problem.connect(results["errors"].append)
    if not window.open_folder(folder):
        print("FAIL: no clips opened")
        return 1
    print("clips:", [(c.name, round(c.duration, 2)) for c in window.cuts.clips])
    window.toggle_play()

    def finish():
        crossed = window.player.clip_index
        window.cuts.add_cut(window.cuts.total / 3)
        window.cuts.toggle(0.0)
        out = Path(folder) / "self-test-out"
        out.mkdir(exist_ok=True)
        saved = window.cuts.save(out)
        text = saved.read_text(encoding="utf-8")
        saved.unlink()
        out.rmdir()
        window.cuts.dirty = False
        print(f"frames: {results['frames']}  size: {results['size']}  rotation: {results['rotation']}  "
              f"reached clip: {crossed}  errors: {results['errors']}")
        print(text)
        results["ok"] = results["frames"] > 0 and not results["errors"] and "cut\t" in text
        window.close()
        app.quit()

    QTimer.singleShot(int(float(os.environ.get("UGC_SELF_TEST_SECONDS", "4")) * 1000), finish)
    QTimer.singleShot(60000, app.quit)  # never hang a build
    app.exec()
    verdict = "SELF TEST OK" if results.get("ok") else "SELF TEST FAILED"
    print(verdict)
    if os.environ.get("UGC_SELF_TEST_LOG"):  # a windowed Windows app has nowhere to print to
        Path(os.environ["UGC_SELF_TEST_LOG"]).write_text(f"{verdict}\n{results}\n", encoding="utf-8")
    return 0 if results.get("ok") else 1


def main():
    args = sys.argv[1:]
    if args[:1] == ["--self-test"]:
        return self_test(args[1])
    from PySide6.QtWidgets import QApplication
    from .app import MainWindow

    app = QApplication(sys.argv[:1])
    app.setApplicationName("ugc-cutter")
    apply_dark_theme(app)
    window = MainWindow()
    window.show()
    if args:
        window.open_folder(args[0])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
