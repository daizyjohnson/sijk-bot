"""Render a self-contained suspicion score panel for a Discord embed."""
from io import BytesIO
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT, MARGIN = 920, 206, 32
GAP, BAR_TOP, BAR_HEIGHT = 10, 90, 31
SEGMENT_WIDTH = (WIDTH - 2 * MARGIN - GAP * 9) / 10


def font(size, bold=False):
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    if not bold:
        paths = paths[1:] + paths[:1]
    for path in paths:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    return ImageFont.load_default()


def meter_png(rating: int) -> bytes:
    if type(rating) is not int or not 1 <= rating <= 10:
        raise ValueError("Rating must be between 1 and 10")
    image = Image.new("RGB", (WIDTH, HEIGHT), (28, 24, 31))
    draw = ImageDraw.Draw(image)
    draw.text((MARGIN, 27), "ПОДОЗРИТЕЛЬНОСТЬ",
              font=font(29, True), fill=(237, 231, 236))
    score = f"{rating}/10"
    score_font = font(42, True)
    score_width = draw.textbbox((0, 0), score, font=score_font)[2]
    draw.text((WIDTH - MARGIN - score_width, 16), score,
              font=score_font, fill=(236, 58, 89))
    for i in range(10):
        left = round(MARGIN + i * (SEGMENT_WIDTH + GAP))
        right = round(left + SEGMENT_WIDTH)
        draw.rounded_rectangle(
            (left, BAR_TOP, right, BAR_TOP + BAR_HEIGHT),
            radius=10, fill=(224, 49, 82) if i < rating else (69, 63, 77))
    small = font(20)
    draw.text((MARGIN, 146), "1 — НИЗКАЯ", font=small, fill=(173, 164, 175))
    label = "10 — МАКСИМАЛЬНАЯ"
    width = draw.textbbox((0, 0), label, font=small)[2]
    draw.text((WIDTH - MARGIN - width, 146), label,
              font=small, fill=(173, 164, 175))
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
