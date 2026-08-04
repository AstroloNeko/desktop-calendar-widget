from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)

size = 256
image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((18, 22, 238, 236), radius=48, fill="#F9F8F4")
draw.rounded_rectangle((18, 22, 238, 96), radius=48, fill="#6273D9")
draw.rectangle((18, 70, 238, 102), fill="#6273D9")
draw.rounded_rectangle((45, 6, 75, 54), radius=12, fill="#39479D")
draw.rounded_rectangle((181, 6, 211, 54), radius=12, fill="#39479D")

font_paths = (
    Path("C:/Windows/Fonts/segoeuib.ttf"),
    Path("C:/Windows/Fonts/arialbd.ttf"),
)
font_path = next((path for path in font_paths if path.exists()), None)
font = ImageFont.truetype(str(font_path), 94) if font_path else ImageFont.load_default()
text = "14"
box = draw.textbbox((0, 0), text, font=font)
x = (size - (box[2] - box[0])) / 2
y = 108 - box[1]
draw.text((x, y), text, font=font, fill="#25262B")

image.save(ASSETS / "calendar.png")
image.save(ASSETS / "calendar.ico", sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def save_day_indicator(filename: str, *, fill: str | None = None, outline: str | None = None) -> None:
    output_size = 28
    scale = 4
    canvas = Image.new("RGBA", (output_size * scale, output_size * scale), (0, 0, 0, 0))
    indicator = ImageDraw.Draw(canvas)
    inset = 1.5 * scale
    indicator.ellipse(
        (inset, inset, output_size * scale - inset, output_size * scale - inset),
        fill=fill,
        outline=outline,
        width=2 * scale if outline else 1,
    )
    canvas.resize((output_size, output_size), Image.Resampling.LANCZOS).save(ASSETS / filename)


save_day_indicator("day_selected.png", fill="#6273D9")
save_day_indicator("day_today.png", outline="#6273D9")
