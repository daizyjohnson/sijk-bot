"""Draw a clean ten-segment meter without text (Discord renders the labels)."""
from io import BytesIO
from PIL import Image, ImageDraw

WIDTH = 920
HEIGHT = 46
PAD = 2
GAP = 10
SEGMENTS = 10
SEGMENT_WIDTH = (WIDTH - PAD * 2 - GAP * (SEGMENTS - 1)) // SEGMENTS


def rating_color(rating: int) -> tuple[int, int, int]:
    if rating <= 3:
        return (51, 185, 119)
    if rating <= 6:
        return (235, 189, 56)
    if rating <= 8:
        return (237, 137, 54)
    return (219, 51, 76)


def meter_png(rating: int) -> bytes:
    if type(rating) is not int or not 1 <= rating <= 10:
        raise ValueError("Rating must be between 1 and 10")
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for i in range(SEGMENTS):
        x = PAD + i * (SEGMENT_WIDTH + GAP)
        draw.rounded_rectangle(
            (x, PAD, x + SEGMENT_WIDTH - 1, HEIGHT - PAD - 1),
            radius=12,
            fill=(*rating_color(rating), 255) if i < rating else (64, 62, 75, 255),
        )
    stream = BytesIO()
    image.save(stream, format="PNG", optimize=True)
    return stream.getvalue()
