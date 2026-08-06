import unittest

from ui_draw import draw_calendar_date_state


class _RecordingCanvas:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[float, ...], dict[str, object]]] = []

    def create_polygon(self, points, **kwargs):
        self.calls.append(("polygon", tuple(points), kwargs))
        return len(self.calls)

    def create_line(self, *coords, **kwargs):
        self.calls.append(("line", tuple(coords), kwargs))
        return len(self.calls)


class CalendarDateDrawingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
