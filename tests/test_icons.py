import tkinter as tk
import unittest
from unittest import mock

from macro_recorder import icons


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


class IconTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def solid(self, image, x, y):
        self.assertFalse(image.transparency_get(x, y), f"({x}, {y}) should be drawn")
        return tuple(image.get(x, y))

    def see_through(self, image, x, y):
        self.assertTrue(image.transparency_get(x, y), f"({x}, {y}) should be transparent")


class ArtTests(unittest.TestCase):
    def test_every_row_is_as_wide_as_the_first(self):
        self.assertEqual({len(row) for row in icons.FOLDER}, {len(icons.FOLDER[0])})

    def test_only_known_colours_are_used(self):
        used = set("".join(icons.FOLDER)) - {"."}
        self.assertEqual(used, set(icons.FOLDER_COLORS))
        self.assertEqual(set(icons.FOLDER_DISABLED_COLORS), set(icons.FOLDER_COLORS))

    def test_the_art_is_as_tall_as_the_text_it_sits_next_to(self):
        self.assertLessEqual(len(icons.FOLDER), icons.ART_LINE_HEIGHT)


class DrawTests(IconTestCase):
    def test_the_image_has_the_size_of_the_art(self):
        image = icons.draw(self.root, icons.FOLDER, icons.FOLDER_COLORS)
        self.assertEqual((image.width(), image.height()), (len(icons.FOLDER[0]), len(icons.FOLDER)))

    def test_pixels_have_the_colour_of_their_letter(self):
        image = icons.draw(self.root, icons.FOLDER, icons.FOLDER_COLORS)
        colors = {letter: hex_to_rgb(color) for letter, color in icons.FOLDER_COLORS.items()}
        for y, row in enumerate(icons.FOLDER):
            for x, letter in enumerate(row):
                if letter == ".":
                    self.see_through(image, x, y)
                else:
                    self.assertEqual(self.solid(image, x, y), colors[letter], f"pixel ({x}, {y})")

    def test_the_corner_next_to_the_tab_is_transparent(self):
        image = icons.draw(self.root, icons.FOLDER, icons.FOLDER_COLORS)
        self.see_through(image, len(icons.FOLDER[0]) - 1, 0)
        self.see_through(image, 0, 0)

    def test_a_scale_enlarges_every_pixel_without_blurring_it(self):
        image = icons.draw(self.root, icons.FOLDER, icons.FOLDER_COLORS, scale=3)
        self.assertEqual((image.width(), image.height()), (3 * len(icons.FOLDER[0]), 3 * len(icons.FOLDER)))
        outline = hex_to_rgb(icons.FOLDER_COLORS["o"])
        for dx in range(3):
            for dy in range(3):
                self.assertEqual(self.solid(image, 3 + dx, 0 + dy), outline)
        self.see_through(image, 0, 0)
        self.see_through(image, 2, 2)

    def test_the_image_belongs_to_the_window_it_was_made_for(self):
        image = icons.draw(self.root, icons.FOLDER, icons.FOLDER_COLORS)
        self.assertEqual(image.tk, self.root.tk)


class FolderIconsTests(IconTestCase):
    def test_the_two_images_have_the_same_size(self):
        normal, disabled = icons.folder_icons(self.root)
        self.assertEqual((normal.width(), normal.height()), (disabled.width(), disabled.height()))

    def test_the_disabled_one_is_grey(self):
        normal, disabled = icons.folder_icons(self.root)
        self.assertEqual(self.solid(normal, 2, 6), hex_to_rgb(icons.FOLDER_COLORS["f"]))
        red, green, blue = self.solid(disabled, 2, 6)
        self.assertEqual(red, green)
        self.assertEqual(green, blue)

    def test_the_shape_is_the_same_in_both(self):
        normal, disabled = icons.folder_icons(self.root)
        for y in range(normal.height()):
            for x in range(normal.width()):
                self.assertEqual(normal.transparency_get(x, y), disabled.transparency_get(x, y))


class ScaleTests(IconTestCase):
    def scale_with_linespace(self, linespace):
        font = mock.Mock()
        font.metrics.return_value = linespace
        with mock.patch.object(icons.tkfont, "Font", return_value=font) as make_font:
            scale = icons.scale_for(self.root)
        make_font.assert_called_once_with(root=self.root, name="TkDefaultFont", exists=True)
        return scale

    def test_normal_text_keeps_the_art_as_it_is(self):
        self.assertEqual(self.scale_with_linespace(16), 1)

    def test_text_twice_as_tall_gets_art_twice_as_big(self):
        self.assertEqual(self.scale_with_linespace(32), 2)

    def test_small_text_never_shrinks_the_art(self):
        self.assertEqual(self.scale_with_linespace(8), 1)

    def test_the_real_font_gives_a_usable_scale(self):
        self.assertGreaterEqual(icons.scale_for(self.root), 1)

    def test_folder_icons_use_that_scale(self):
        with mock.patch.object(icons, "scale_for", return_value=2):
            normal, disabled = icons.folder_icons(self.root)
        for image in (normal, disabled):
            self.assertEqual((image.width(), image.height()), (2 * len(icons.FOLDER[0]), 2 * len(icons.FOLDER)))


if __name__ == "__main__":
    unittest.main()
