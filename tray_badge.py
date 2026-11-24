"""Native, DPI-scaled compositing of the existing notification-area icon."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes as w
import math


BADGE_COLORS = {
    'green': 0x35B66F,
    'yellow': 0xE7B43D,
    'red': 0xE05252,
    'gray': 0x9CA3AF,
}


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('size', w.DWORD), ('width', w.LONG), ('height', w.LONG), ('planes', w.WORD),
                ('depth', w.WORD), ('compression', w.DWORD), ('image_size', w.DWORD),
                ('x_size', w.LONG), ('y_size', w.LONG), ('used', w.DWORD), ('important', w.DWORD)]


class ICONINFO(ctypes.Structure):
    _fields_ = [('icon', w.BOOL), ('hot_x', w.DWORD), ('hot_y', w.DWORD),
                ('mask', w.HBITMAP), ('color', w.HBITMAP)]


def overlay_dot(pixels, size, rgb):
    """Blend a white-ringed circle into premultiplied BGRA pixels."""
    radius = max(3, size * .18)
    inset = max(1.25, size / 24)
    center = size - radius - .4
    start = max(0, int(center - radius - .5))
    for y in range(start, size):
        for x in range(start, size):
            edge = radius + .5 - math.hypot(x + .5 - center, y + .5 - center)
            outer = min(1, max(0, edge))
            if not outer:
                continue
            inner = min(1, max(0, edge - inset))
            index = y * size + x
            base = pixels[index]
            value = 0
            for shift in (0, 8, 16, 24):
                channel = 255 if shift == 24 else (rgb >> shift) & 255
                component = round(channel * inner + 255 * (outer - inner) +
                                  ((base >> shift) & 255) * (1 - outer))
                value |= component << shift
            pixels[index] = value


class TrayBadgeRenderer:
    def __init__(self, user):
        self.user = user
        self.gdi = ctypes.WinDLL('gdi32', use_last_error=True)
        for dll, name, args, result in (
            (user, 'DrawIconEx', [w.HDC, ctypes.c_int, ctypes.c_int, w.HICON, ctypes.c_int, ctypes.c_int,
                                  w.UINT, w.HBRUSH, w.UINT], w.BOOL),
            (user, 'CreateIconIndirect', [ctypes.POINTER(ICONINFO)], w.HICON),
            (self.gdi, 'CreateDIBSection', [w.HDC, ctypes.POINTER(BITMAPINFOHEADER), w.UINT,
                                           ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD], w.HBITMAP),
            (self.gdi, 'CreateCompatibleDC', [w.HDC], w.HDC),
            (self.gdi, 'SelectObject', [w.HDC, w.HANDLE], w.HANDLE),
            (self.gdi, 'CreateBitmap', [ctypes.c_int, ctypes.c_int, w.UINT, w.UINT, w.LPVOID], w.HBITMAP),
            (self.gdi, 'DeleteDC', [w.HDC], w.BOOL),
            (self.gdi, 'DeleteObject', [w.HANDLE], w.BOOL),
            (self.gdi, 'GdiFlush', [], w.BOOL),
        ):
            function = getattr(dll, name)
            function.argtypes, function.restype = args, result

    @contextmanager
    def bitmap(self, size):
        header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), size, -size, 1, 32)
        bits = ctypes.c_void_p()
        bitmap = self.gdi.CreateDIBSection(None, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        if not bitmap:
            raise OSError('CreateDIBSection failed')
        try:
            ctypes.memset(bits, 0, size * size * 4)
            yield bitmap, (ctypes.c_uint32 * (size * size)).from_address(bits.value)
        finally:
            self.gdi.DeleteObject(bitmap)

    def draw(self, bitmap, icon, size, flags=3):
        dc = self.gdi.CreateCompatibleDC(None)
        if not dc:
            raise OSError('CreateCompatibleDC failed')
        original = None
        try:
            original = self.gdi.SelectObject(dc, bitmap)
            if not original or original == ctypes.c_void_p(-1).value:
                raise OSError('SelectObject failed')
            if not self.user.DrawIconEx(dc, 0, 0, icon, size, size, 0, None, flags):
                raise OSError('DrawIconEx failed')
            self.gdi.GdiFlush()
        finally:
            if original and original != ctypes.c_void_p(-1).value:
                self.gdi.SelectObject(dc, original)
            self.gdi.DeleteDC(dc)

    def create(self, icon, size, color):
        with self.bitmap(size) as (bitmap, pixels):
            self.draw(bitmap, icon, size)
            if not any(pixel >> 24 for pixel in pixels):
                # Older system fallback icons may have an AND mask instead of alpha.
                with self.bitmap(size) as (mask_bitmap, mask_pixels):
                    self.draw(mask_bitmap, icon, size, flags=1)
                    for index, mask in enumerate(mask_pixels):
                        pixels[index] = 0 if mask & 0xFFFFFF else pixels[index] | 0xFF000000
            overlay_dot(pixels, size, BADGE_COLORS[color])
            mask_bits = ctypes.create_string_buffer(((size + 15) // 16) * 2 * size)
            mask = self.gdi.CreateBitmap(size, size, 1, 1, mask_bits)
            if not mask:
                raise OSError('CreateBitmap failed')
            try:
                combined = self.user.CreateIconIndirect(ctypes.byref(ICONINFO(True, 0, 0, mask, bitmap)))
                if not combined:
                    raise OSError('CreateIconIndirect failed')
                return combined
            finally:
                self.gdi.DeleteObject(mask)
