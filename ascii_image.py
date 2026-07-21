#!/usr/bin/env python3
"""Convert raster images into high-detail rendered ASCII images."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import (
    Image,
    ImageChops,
    ImageColor,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageFont,
    ImageOps,
    UnidentifiedImageError,
)


TEXTURE_FAMILIES = tuple(" .'`^\",:;Il!i~+_-?][}{1)(|\\/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$")

COMPACT_FAMILIES = tuple(c for c in " .:-=+*#%@")

BAYER_4 = (
    (0, 8, 2, 10),
    (12, 4, 14, 6),
    (3, 11, 1, 9),
    (15, 7, 13, 5),
)


def parse_color(value: str) -> tuple[int, int, int]:
    """Parse a named or hexadecimal color for argparse."""
    try:
        return ImageColor.getcolor(value, "RGB")
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"invalid color {value!r}; use a name like cyan or a hex value like #39FF14"
        ) from error


def stable_pick(x: int, y: int, size: int) -> int:
    """Deterministic coordinate hash; avoids a noisy result that changes per run."""
    value = (x * 0x1F123BB5) ^ (y * 0x5F356495) ^ ((x + y) * 0x6C8E9CF5)
    value ^= value >> 16
    return value % size


def prepare_image(
    path: Path,
    width: int,
    height: int | None,
    cell_aspect: float,
    contrast: float,
    gamma: float,
    black_point: float,
    detail: float,
    edge_boost: float,
    invert: bool,
    autocontrast: bool,
) -> tuple[Image.Image, Image.Image]:
    # Force a full decode inside the context so the input handle is always
    # closed before an output file is opened (particularly important on Windows).
    with Image.open(path) as opened:
        source = ImageOps.exif_transpose(opened).convert("RGBA")
    # Transparent pixels become black, matching the default terminal canvas.
    canvas = Image.new("RGBA", source.size, (0, 0, 0, 255))
    source = Image.alpha_composite(canvas, source).convert("RGB")

    if height is None:
        height = max(1, round(width * source.height / source.width * cell_aspect))

    rgb = source.resize((width, height), Image.Resampling.LANCZOS)
    gray = ImageOps.grayscale(rgb)

    if autocontrast:
        # Useful for flat source images, but optional because it can promote a
        # near-black JPEG/PNG background into visible texture.
        gray = ImageOps.autocontrast(gray, cutoff=(0.35, 0.35))
    if detail > 0:
        sharpened = gray.filter(
            ImageFilter.UnsharpMask(
                radius=1.25,
                percent=max(0, round(130 * detail)),
                threshold=2,
            )
        )
        # Do not turn sensor/compression noise below the chosen black point
        # into isolated punctuation on an otherwise clean dark background.
        shadow_mask = gray.point(lambda value: 255 if value > black_point * 255 else 0)
        gray = Image.composite(sharpened, gray, shadow_mask)
    if edge_boost > 0:
        # Luminance alone loses boundaries between different hues of equal
        # brightness. RGB edge detection restores a subtle bright contour.
        edges = ImageOps.grayscale(rgb.filter(ImageFilter.FIND_EDGES))
        for x in range(width):
            edges.putpixel((x, 0), 0)
            edges.putpixel((x, height - 1), 0)
        for y in range(height):
            edges.putpixel((0, y), 0)
            edges.putpixel((width - 1, y), 0)
        # A soft edge floor removes low-level chroma/JPEG noise while retaining
        # meaningful boundaries such as hair, glasses, packaging, and text.
        edge_floor = 12
        edges = edges.point(
            lambda value: round(max(0, value - edge_floor) * 255 / (255 - edge_floor))
        )
        edges = ImageEnhance.Brightness(edges).enhance(edge_boost)
        gray = ImageChops.screen(gray, edges)
    if contrast != 1:
        gray = ImageEnhance.Contrast(gray).enhance(contrast)

    lut = [
        round(255 * (max(0.0, (i / 255 - black_point) / (1 - black_point)) ** gamma))
        for i in range(256)
    ]
    gray = gray.point(lut)
    if invert:
        gray = ImageOps.invert(gray)
    return gray, rgb


def quantized_levels(gray: Image.Image, count: int, dither: str) -> list[list[int]]:
    width, height = gray.size
    values = [[gray.getpixel((x, y)) / 255 for x in range(width)] for y in range(height)]
    result = [[0] * width for _ in range(height)]

    if dither == "floyd":
        work = [row[:] for row in values]
        for y in range(height):
            x_range = range(width) if y % 2 == 0 else range(width - 1, -1, -1)
            direction = 1 if y % 2 == 0 else -1
            for x in x_range:
                old = min(1.0, max(0.0, work[y][x]))
                level = round(old * (count - 1))
                result[y][x] = level
                error = old - level / (count - 1)
                for dx, dy, weight in (
                    (direction, 0, 7 / 16),
                    (-direction, 1, 3 / 16),
                    (0, 1, 5 / 16),
                    (direction, 1, 1 / 16),
                ):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < width and 0 <= ny < height:
                        work[ny][nx] += error * weight
        return result

    for y, row in enumerate(values):
        for x, value in enumerate(row):
            if dither == "ordered":
                # Less than one tonal step: enough to reveal gradients without
                # turning smooth areas into an obvious checkerboard.
                value += ((BAYER_4[y % 4][x % 4] + 0.5) / 16 - 0.5) / (count - 1)
            result[y][x] = round(min(1, max(0, value)) * (count - 1))
    return result


def ansi_rgb(rgb: tuple[int, int, int]) -> str:
    return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def render_ascii(
    gray: Image.Image,
    rgb: Image.Image,
    families: tuple[str, ...],
    dither: str,
    color: bool,
) -> str:
    width, height = gray.size
    levels = quantized_levels(gray, len(families), dither)
    lines: list[str] = []

    for y in range(height):
        chars = [
            families[levels[y][x]][stable_pick(x, y, len(families[levels[y][x]]))]
            for x in range(width)
        ]
        visible_width = len("".join(chars).rstrip())
        if not color:
            lines.append("".join(chars[:visible_width]))
            continue

        pieces: list[str] = []
        previous_color: tuple[int, int, int] | None = None
        for x, char in enumerate(chars[:visible_width]):
            if char != " ":
                pixel_color = rgb.getpixel((x, y))
                # Reducing precision greatly shrinks ANSI output while remaining
                # visually indistinguishable at character-cell resolution.
                pixel_color = tuple((channel // 12) * 12 for channel in pixel_color)
                if pixel_color != previous_color:
                    pieces.append(ansi_rgb(pixel_color))
                    previous_color = pixel_color
            pieces.append(char)
        if pieces:
            pieces.append("\x1b[0m")
        lines.append("".join(pieces))
    return "\n".join(lines)


def load_monospace_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a common monospace font, falling back to Pillow's bundled font."""
    candidates = (
        Path(r"C:\Windows\Fonts\consola.ttf"),
        Path(r"C:\Windows\Fonts\lucon.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
        Path("/System/Library/Fonts/Menlo.ttc"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Older Pillow fallback.
        return ImageFont.load_default()


def render_ascii_image(
    art: str,
    rgb: Image.Image,
    font_size: int,
    color: bool,
    invert: bool,
    ink_color: tuple[int, int, int] | None = None,
    background_color: tuple[int, int, int] | None = None,
) -> Image.Image:
    """Render ASCII characters into an ordinary RGB image."""
    lines = art.split("\n")
    font = load_monospace_font(font_size)
    try:
        ascent, descent = font.getmetrics()
        cell_height = max(font_size, ascent + descent)
    except AttributeError:
        cell_height = font_size
    cell_width = max(1, round(font.getlength("M")))

    background = background_color or ((255, 255, 255) if invert else (0, 0, 0))
    monochrome_ink = ink_color or ((0, 0, 0) if invert else (255, 255, 255))
    canvas = Image.new(
        "RGB",
        (rgb.width * cell_width, rgb.height * cell_height),
        background,
    )
    draw = ImageDraw.Draw(canvas)

    for y, line in enumerate(lines[:rgb.height]):
        for x, char in enumerate(line[:rgb.width]):
            if char == " ":
                continue
            ink = rgb.getpixel((x, y)) if color else monochrome_ink
            draw.text((x * cell_width, y * cell_height), char, font=font, fill=ink)
    return canvas


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert an image to a high-detail rendered ASCII image.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("image", type=Path, help="input PNG, JPEG, WebP, etc.")
    parser.add_argument("-w", "--width", type=int, default=220, help="output character columns")
    parser.add_argument("--height", type=int, help="force output rows")
    parser.add_argument(
        "--cell-aspect", type=float, default=0.48,
        help="terminal character width/height correction",
    )
    parser.add_argument("--contrast", type=float, default=1.12)
    parser.add_argument(
        "--gamma", type=float, default=0.78,
        help="below 1 reveals shadows; above 1 deepens them",
    )
    parser.add_argument(
        "--black-point", type=float, default=0.05,
        help="fraction treated as pure black; suppresses background noise",
    )
    parser.add_argument("--detail", type=float, default=1.35, help="local detail strength")
    parser.add_argument(
        "--edge-boost", type=float, default=0.24,
        help="preserve boundaries between different colors of similar brightness",
    )
    parser.add_argument(
        "--autocontrast", action="store_true",
        help="stretch a flat image's tonal range (may reveal background noise)",
    )
    parser.add_argument("--invert", action="store_true", help="dark glyphs on light background")
    parser.add_argument(
        "--dither", choices=("ordered", "floyd", "none"), default="floyd",
    )
    parser.add_argument(
        "--style", choices=("texture", "compact"), default="texture",
        help="varied reference-like glyphs or a traditional short ramp",
    )
    color_group = parser.add_mutually_exclusive_group()
    color_group.add_argument(
        "--color", action="store_true", help="render glyphs in source-image colors",
    )
    color_group.add_argument(
        "--ink-color", type=parse_color, metavar="COLOR",
        help="render every glyph in a named or hex color, such as cyan or #39FF14",
    )
    parser.add_argument(
        "--background-color", type=parse_color, metavar="COLOR",
        help="use a named or hex background color instead of black/white",
    )
    parser.add_argument("--font-size", type=int, default=14, help="ASCII glyph size in output image")
    parser.add_argument(
        "-o", "--output", type=Path,
        help="output PNG/JPEG/WebP path (default: IMAGE_ascii.png beside input)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.image.is_file():
        print(f"error: image not found: {args.image}", file=sys.stderr)
        return 2
    output = args.output or args.image.with_name(f"{args.image.stem}_ascii.png")
    if not output.suffix:
        output = output.with_suffix(".png")
    if output.resolve() == args.image.resolve():
        print("error: output path must not overwrite the input image", file=sys.stderr)
        return 2

    width = args.width
    if width < 2 or (args.height is not None and args.height < 1):
        print("error: width must be >= 2 and height must be >= 1", file=sys.stderr)
        return 2
    if (
        args.gamma <= 0
        or args.cell_aspect <= 0
        or args.contrast <= 0
        or args.font_size < 4
        or args.detail < 0
        or args.edge_boost < 0
        or not 0 <= args.black_point < 1
    ):
        print(
            "error: gamma/cell aspect/contrast must be positive, font size >= 4, "
            "detail/edge boost nonnegative, and black point in [0, 1)",
            file=sys.stderr,
        )
        return 2

    try:
        gray, rgb = prepare_image(
            args.image, width, args.height, args.cell_aspect,
            args.contrast, args.gamma, args.black_point, args.detail, args.edge_boost,
            args.invert, args.autocontrast,
        )
    except (UnidentifiedImageError, OSError, ValueError) as error:
        print(f"error: could not read image: {error}", file=sys.stderr)
        return 2
    families = TEXTURE_FAMILIES if args.style == "texture" else COMPACT_FAMILIES
    art = render_ascii(gray, rgb, families, args.dither, False)
    try:
        result = render_ascii_image(
            art,
            rgb,
            args.font_size,
            args.color,
            args.invert,
            args.ink_color,
            args.background_color,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        result.save(output)
    except (OSError, ValueError) as error:
        print(f"error: could not write output image: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
