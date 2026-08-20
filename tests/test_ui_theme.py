import unittest

from ui_theme import DEFAULT_THEME_NAME, FRUTIGER, PAPER, THEMES, WIN7_AERO, get_theme, normalize_theme_name


class ThemeRegistryTests(unittest.TestCase):
    def test_four_themes_are_registered_in_menu_order(self):
        self.assertEqual(tuple(THEMES), ("modern", "aero", "paper", "frutiger"))
        self.assertEqual(WIN7_AERO.display_name, "Windows 7 Glass")
        self.assertEqual(PAPER.display_name, "Paper")
        self.assertEqual(PAPER.style, "paper")
        self.assertEqual(FRUTIGER.display_name, "Frutiger Aero")
        self.assertEqual(FRUTIGER.style, "frutiger")

    def test_paper_has_an_independent_visual_palette(self):
        modern = get_theme("modern")
        aero = get_theme("aero")
        self.assertNotEqual(PAPER.panel_background, modern.panel_background)
        self.assertNotEqual(PAPER.panel_background, aero.panel_background)
        self.assertNotEqual(PAPER.accent, modern.accent)
        self.assertNotEqual(PAPER.date_selected_background, aero.date_selected_background)

    def test_frutiger_has_an_independent_visual_palette(self):
        modern = get_theme("modern")
        aero = get_theme("aero")
        self.assertNotEqual(FRUTIGER.panel_background, modern.panel_background)
        self.assertNotEqual(FRUTIGER.header_gradient_mid, aero.header_gradient_mid)
        self.assertNotEqual(FRUTIGER.accent, aero.accent)
        self.assertNotEqual(FRUTIGER.date_selected_background, aero.date_selected_background)
        self.assertNotEqual(FRUTIGER.environment_haze, FRUTIGER.schedule_background)
        self.assertNotEqual(FRUTIGER.environment_accent, FRUTIGER.accent)

    def test_legacy_theme_aliases_and_invalid_fallback(self):
        self.assertEqual(normalize_theme_name("aero"), "aero")
        self.assertEqual(normalize_theme_name("win7_aero"), "aero")
        self.assertEqual(normalize_theme_name("paper"), "paper")
        self.assertEqual(normalize_theme_name("frutiger"), "frutiger")
        self.assertEqual(normalize_theme_name("unknown"), DEFAULT_THEME_NAME)
        self.assertEqual(normalize_theme_name(None), DEFAULT_THEME_NAME)


if __name__ == "__main__":
    unittest.main()
