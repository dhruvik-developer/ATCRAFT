"""Rasterize the existing favicon's A motif; requires Pillow only when regenerating icons."""

from pathlib import Path

from PIL import Image, ImageDraw

OUTPUT = Path(__file__).resolve().parents[1] / "public" / "icons"
BRAND = "#1a4d8f"


def render(size: int, filename: str, *, maskable: bool = False) -> None:
    scale = 4
    image = Image.new("RGB", (size * scale, size * scale), BRAND)
    draw = ImageDraw.Draw(image)
    # Maskable artwork stays in the central safe circle; the solid background
    # fills the entire square so launchers can apply any icon mask.
    inset = 0.12 if maskable else 0.0

    def point(x: float, y: float) -> tuple[int, int]:
        return (
            round((inset + x / 32 * (1 - 2 * inset)) * size * scale),
            round((inset + y / 32 * (1 - 2 * inset)) * size * scale),
        )

    width = max(1, round(2.4 / 32 * size * scale * (1 - 2 * inset)))
    draw.line([point(9, 22), point(16, 9), point(23, 22)], fill="white", width=width, joint="curve")
    draw.line([point(12.2, 17.4), point(19.8, 17.4)], fill="white", width=width)
    radius = width // 2
    for x, y in [point(9, 22), point(16, 9), point(23, 22)]:
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill="white")
    image.resize((size, size), Image.Resampling.LANCZOS).save(OUTPUT / filename, optimize=True)


if __name__ == "__main__":
    OUTPUT.mkdir(parents=True, exist_ok=True)
    render(192, "icon-192.png")
    render(512, "icon-512.png")
    render(512, "icon-maskable-512.png", maskable=True)
    render(180, "apple-touch-icon.png")
