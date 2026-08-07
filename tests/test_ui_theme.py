import unittest

from ui_theme import DEFAULT_THEME_NAME, PAPER, THEMES, get_theme, normalize_theme_name


class ThemeRegistryTests(unittest.TestCase):
    def test_three_themes_are_registered_in_menu_order(self):
        self.assertEqual(tuple(THEMES), ("modern", "win7_aero", "paper"))
        self.assertEqual(PAPER.display_name, "Paper")
        self.assertEqual(PAPER.style, "paper")

    def test_paper_has_an_independent_visual_palette(self):
        modern = get_theme("modern")
        aero = get_theme("win7_aero")
        self.assertNotEqual(PAPER.panel_background, modern.panel_background)
        self.assertNotEqual(PAPER.panel_background, aero.panel_background)
        self.assertNotEqual(PAPER.accent, modern.accent)
        self.assertNotEqual(PAPER.date_selected_background, aero.date_selected_background)

    def test_legacy_aero_alias_and_invalid_fallback(self):
        self.assertEqual(normalize_theme_name("aero"), "win7_aero")
        self.assertEqual(normalize_theme_name("paper"), "paper")
        self.assertEqual(normalize_theme_name("unknown"), DEFAULT_THEME_NAME)
        self.assertEqual(normalize_theme_name(None), DEFAULT_THEME_NAME)


if __name__ == "__main__":
    unittest.main()
