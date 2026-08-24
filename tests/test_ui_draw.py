import unittest

from ui_draw import draw_calendar_date_ring, draw_calendar_date_state, draw_color_swatch
from ui_theme import get_theme


class _RecordingCanvas:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[float, ...], dict[str, object]]] = []

    def create_polygon(self, points, **kwargs):
        self.calls.append(("polygon", tuple(points), kwargs))
        return len(self.calls)

    def create_line(self, *coords, **kwargs):
        self.calls.append(("line", tuple(coords), kwargs))
        return len(self.calls)

    def create_oval(self, *coords, **kwargs):
        self.calls.append(("oval", tuple(coords), kwargs))
        return len(self.calls)

    def create_text(self, *coords, **kwargs):
        self.calls.append(("text", tuple(coords), kwargs))
        return len(self.calls)

    def delete(self, *tags):
        self.calls.append(("delete", tuple(tags), {}))


class CalendarDateDrawingTests(unittest.TestCase):
    def test_selected_color_swatch_uses_checkmark_and_theme_tokens(self) -> None:
        canvas = _RecordingCanvas()
        theme = get_theme("paper")
        draw_color_swatch(
            canvas,
            "#8B70D6",
            selected=True,
            hovered=False,
            theme=theme,
            font_family="Segoe UI",
        )

        text_calls = [kwargs for kind, _coords, kwargs in canvas.calls if kind == "text"]
        self.assertEqual(text_calls[0]["text"], "✓")
        self.assertEqual(text_calls[0]["fill"], theme.text_on_accent)
        outer = next(kwargs for kind, _coords, kwargs in canvas.calls if kind == "oval")
        self.assertEqual(outer["outline"], theme.accent)

    def test_flat_state_uses_one_shared_body_box(self) -> None:
        canvas = _RecordingCanvas()
        draw_calendar_date_state(
            canvas,
            10.4,
            1.2,
            36.4,
            26.2,
            fill="#6673D8",
            border="#5B67C8",
            radius=6,
        )

        polygons = [coords for kind, coords, _kwargs in canvas.calls if kind == "polygon"]
        self.assertEqual(len(polygons), 2)
        self.assertEqual(polygons[0], polygons[1])
        self.assertTrue(all(kwargs.get("tags") == "date_state" for _kind, _coords, kwargs in canvas.calls))

    def test_aero_layers_never_expand_beyond_public_bounds(self) -> None:
        canvas = _RecordingCanvas()
        draw_calendar_date_state(
            canvas,
            10.4,
            1.2,
            44.4,
            30.2,
            fill="#C3CFD1",
            border="#AAC4C7",
            radius=6,
            gradient_start="#EAF0F0",
            gradient_end="#B9C7C9",
            inner_border="#D9E2E3",
            top_highlight="#FFFFFF",
            today_ring="#9FCED1",
        )

        for _kind, coords, kwargs in canvas.calls:
            self.assertTrue(all(10 <= coordinate <= 44 for coordinate in coords[::2]))
            self.assertTrue(all(1 <= coordinate <= 30 for coordinate in coords[1::2]))
            self.assertEqual(kwargs.get("tags"), "date_state")
        self.assertTrue(all(kwargs.get("smooth") is False for kind, _coords, kwargs in canvas.calls if kind == "polygon"))

    def test_ddl_ring_stays_inside_its_box_and_uses_marker_tag(self) -> None:
        canvas = _RecordingCanvas()
        draw_calendar_date_ring(
            canvas,
            8.4,
            0.2,
            42.4,
            26.2,
            color="#B84D58",
            radius=8,
            inner_highlight="#E8C7C8",
        )

        polygons = [call for call in canvas.calls if call[0] == "polygon"]
        self.assertEqual(len(polygons), 2)
        for _kind, coords, kwargs in polygons:
            self.assertTrue(all(8 <= coordinate <= 42 for coordinate in coords[::2]))
            self.assertTrue(all(0 <= coordinate <= 26 for coordinate in coords[1::2]))
            self.assertEqual(kwargs.get("tags"), "date_marker")
            self.assertEqual(kwargs.get("fill"), "")

    def test_date_state_corners_are_integer_and_mirrored(self) -> None:
        canvas = _RecordingCanvas()
        draw_calendar_date_state(
            canvas,
            9,
            1,
            37,
            23,
            fill="#C3CFD1",
            border="#AAC4C7",
            radius=6,
            gradient_start="#EAF0F0",
            gradient_end="#B9C7C9",
            inner_border="#D9E2E3",
            today_ring="#9FCED1",
        )

        for _kind, coords, _kwargs in (call for call in canvas.calls if call[0] == "polygon"):
            self.assertTrue(all(isinstance(coordinate, int) for coordinate in coords))
            points = set(zip(coords[::2], coords[1::2]))
            left = min(point[0] for point in points)
            right = max(point[0] for point in points)
            top = min(point[1] for point in points)
            bottom = max(point[1] for point in points)
            for x, y in points:
                self.assertIn((left + right - x, y), points)
                self.assertIn((x, top + bottom - y), points)


if __name__ == "__main__":
    unittest.main()
