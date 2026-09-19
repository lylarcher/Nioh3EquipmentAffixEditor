# -*- coding: utf-8 -*-
"""GUI branding: the window icon and the logo in the header.

The GUI is branded from the same generated assets as the executable, so these
tests pin down three things that are easy to break silently:

* the window icon is set from ``assets/app.ico`` (not from a leftover default);
* the header really displays the logo image -- and the widget holds a reference,
  because a ``PhotoImage`` that nothing references is garbage collected and the
  logo vanishes a moment after the window appears;
* a user who deletes ``assets/`` still gets a working window (cosmetic assets must
  never be a hard dependency).
"""

from __future__ import annotations

import tempfile
import tkinter
import unittest
from pathlib import Path
from unittest import mock

from nioh3_accessory_editor import paths, ui

try:  # pragma: no cover - environment dependent
    _probe = tkinter.Tk()
    _probe.destroy()
    TK_AVAILABLE = True
    TK_ERROR = ""
except Exception as error:  # pragma: no cover - environment dependent
    TK_AVAILABLE = False
    TK_ERROR = f"{type(error).__name__}: {error}"

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_app() -> ui.AccessoryEditorApp:
    with mock.patch.object(ui.AccessoryEditorApp, "refresh_saves",
                           lambda self: None):
        return ui.AccessoryEditorApp()


@unittest.skipUnless(TK_AVAILABLE, f"Tk unavailable ({TK_ERROR})")
class WindowIconTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        self.addCleanup(self.app.destroy)

    def test_assets_exist_where_the_gui_expects_them(self) -> None:
        self.assertTrue(paths.icon_path().is_file(), paths.icon_path())
        self.assertTrue(paths.logo_path(64).is_file(), paths.logo_path(64))

    def test_window_icon_comes_from_the_generated_ico(self) -> None:
        with mock.patch.object(tkinter.Tk, "iconbitmap") as iconbitmap, \
                mock.patch.object(ui.AccessoryEditorApp, "refresh_saves",
                                  lambda self: None):
            app = ui.AccessoryEditorApp()
        self.addCleanup(app.destroy)
        iconbitmap.assert_called_once_with(default=str(paths.icon_path()))

    def test_falls_back_to_the_logo_when_the_ico_is_missing(self) -> None:
        """iconbitmap cannot read every Tk build, so the PNG path is a fallback."""
        with mock.patch.object(paths, "icon_path",
                               return_value=Path("missing") / "app.ico"), \
                mock.patch.object(tkinter.Tk, "iconbitmap") as iconbitmap, \
                mock.patch.object(ui.AccessoryEditorApp, "refresh_saves",
                                  lambda self: None):
            app = ui.AccessoryEditorApp()
        self.addCleanup(app.destroy)
        iconbitmap.assert_not_called()
        self.assertIn("window", app._images)


@unittest.skipUnless(TK_AVAILABLE, f"Tk unavailable ({TK_ERROR})")
class HeaderLogoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        self.addCleanup(self.app.destroy)

    def labelled_widgets(self) -> list[tkinter.Widget]:
        return [child for child in self.app.winfo_children()]

    def find_labels(self, widget: tkinter.Widget) -> list[tkinter.Widget]:
        found = []
        for child in widget.winfo_children():
            if child.winfo_class() == "TLabel":
                found.append(child)
            found.extend(self.find_labels(child))
        return found

    def test_header_shows_the_logo_image(self) -> None:
        images = [label for label in self.find_labels(self.app)
                  if str(label.cget("image"))]
        self.assertTrue(images, "标题区没有显示 logo")
        image = images[0].cget("image")
        logo = ui._load_image(paths.logo_path(64))
        self.assertIsNotNone(logo)
        # 64 px asset used at native resolution: no blurry resampling.
        self.assertEqual((self.app.tk.call("image", "width", image),
                          self.app.tk.call("image", "height", image)),
                         (logo.width(), logo.height()))

    def test_logo_widget_is_kept_referenced(self) -> None:
        """Without a Python reference Tk drops the image (classic silent bug)."""
        self.assertIn("header", self.app._images)
        self.assertGreaterEqual(self.app._images["header"].width(), 32)

    def test_title_and_subtitle_are_shown_next_to_the_logo(self) -> None:
        texts = [str(label.cget("text")) for label in self.find_labels(self.app)]
        self.assertIn(ui.TITLE, texts)
        self.assertIn(ui.SUBTITLE, texts)

    def test_missing_logo_degrades_to_text_instead_of_failing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nioh3-logo-") as temp, \
                mock.patch.object(paths, "logo_path",
                                  return_value=Path(temp) / "nope.png"), \
                mock.patch.object(paths, "icon_path",
                                  return_value=Path(temp) / "nope.ico"), \
                mock.patch.object(ui.AccessoryEditorApp, "refresh_saves",
                                  lambda self: None):
            app = ui.AccessoryEditorApp()
        self.addCleanup(app.destroy)
        texts = [str(label.cget("text")) for label in self.find_labels(app)]
        self.assertIn("勾玉", texts)
        self.assertIn(ui.TITLE, texts)

    def test_disclaimer_is_still_shown(self) -> None:
        texts = [str(label.cget("text")) for label in self.find_labels(self.app)]
        self.assertTrue(any("仅供测试学习用" in text for text in texts))


@unittest.skipUnless(TK_AVAILABLE, f"Tk unavailable ({TK_ERROR})")
class LoadImageTests(unittest.TestCase):
    def setUp(self) -> None:
        # A PhotoImage needs an interpreter; the application passes its own.
        self.root = tkinter.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(ui._load_image(Path("does-not-exist.png"), self.root))

    def test_corrupt_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nioh3-logo-") as temp:
            broken = Path(temp) / "broken.png"
            broken.write_bytes(b"not a png")
            self.assertIsNone(ui._load_image(broken, self.root))

    def test_real_asset_loads_at_its_native_size(self) -> None:
        image = ui._load_image(PROJECT_ROOT / "assets" / "logo-32.png", self.root)
        self.assertIsNotNone(image)
        self.assertEqual((image.width(), image.height()), (32, 32))

    def test_each_logo_size_is_loadable(self) -> None:
        for size in paths.LOGO_SIZES:
            with self.subTest(size=size):
                image = ui._load_image(paths.logo_path(size), self.root)
                self.assertIsNotNone(image)
                self.assertEqual((image.width(), image.height()), (size, size))


if __name__ == "__main__":
    unittest.main()
