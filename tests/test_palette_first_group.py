import unittest
from unittest.mock import Mock

from search_palette_ui import PaletteList


class PaletteFirstGroupTests(unittest.TestCase):
    def test_reselect_first_result_reveals_its_heading(self):
        canvas = Mock()
        PaletteList.see(canvas, "0")
        canvas.yview_moveto.assert_called_once_with(0)
        canvas.canvasy.assert_not_called()
