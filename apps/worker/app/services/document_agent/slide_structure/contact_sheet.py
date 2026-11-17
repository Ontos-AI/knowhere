"""Render slides and tile them into grids, each cell tagged with its slide number."""

from __future__ import annotations

import base64
import io
import math
import os
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from app.services.document_agent.slide_structure.config import GridLayout
from app.services.document_agent.visual import render_pages, visual_output_dir

SLIDE_STRUCTURE_VISUAL_DIR = "slide_structure"

# Grid drawing. The prompts refer to the red number tag at each cell's top-left.
_CELL_GAP_PX = 8
_TAG_FONT_MIN_PX = 14
_TAG_FONT_WIDTH_DIVISOR = 14
_TAG_PADDING_PX = (4, 2)
_TAG_FILL = "#d00"
_CELL_OUTLINE = "#888"


class ContactSheets:
    """Renders each slide once per thumbnail width and tiles slides into grids."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._width_by_page = {
            int(feature.page): float(feature.width)
            for feature in ctx.blackboard.page_features
        }
        self._paths: dict[tuple[int, int], str] = {}
        self._out_dir = visual_output_dir(ctx, folder_name=SLIDE_STRUCTURE_VISUAL_DIR)

    def grids(self, slides: list[int], layout: GridLayout, *, name: str) -> list[str]:
        """Tile ``slides`` into as many grids as needed; return base64 PNGs."""
        self._render(slides, layout.thumb_width)
        images: list[str] = []
        for index, start in enumerate(range(0, len(slides), layout.cells), start=1):
            chunk = slides[start : start + layout.cells]
            grid = self._tile(chunk, layout)
            grid.save(os.path.join(self._out_dir, f"{name}_{index:02d}.png"))
            images.append(_png_base64(grid))
        return images

    def _render(self, slides: list[int], width: int) -> None:
        missing = [page for page in slides if (width, page) not in self._paths]
        if not missing:
            return
        widest = max(self._width_by_page[page] for page in missing)
        dpi = math.ceil(width * 72 / widest)
        for item in render_pages(
            self._ctx,
            missing,
            folder_name=SLIDE_STRUCTURE_VISUAL_DIR,
            prefix=f"w{width}",
            dpi=dpi,
        ):
            self._paths[(width, int(item["page"]))] = str(item["png_path"])

    def _thumb(self, page: int, width: int) -> Image.Image:
        with Image.open(self._paths[(width, page)]) as image:
            rgb = image.convert("RGB")
        height = round(rgb.height * width / rgb.width)
        return rgb.resize((width, height))

    def _tile(self, slides: list[int], layout: GridLayout) -> Image.Image:
        width = layout.thumb_width
        thumbs = [self._thumb(page, width) for page in slides]
        cell_height = max(thumb.height for thumb in thumbs)
        cols = min(layout.cols, len(slides))
        rows = math.ceil(len(slides) / cols)
        grid = Image.new(
            "RGB",
            (
                cols * (width + _CELL_GAP_PX) + _CELL_GAP_PX,
                rows * (cell_height + _CELL_GAP_PX) + _CELL_GAP_PX,
            ),
            "white",
        )
        draw = ImageDraw.Draw(grid)
        font = ImageFont.load_default(
            size=max(_TAG_FONT_MIN_PX, width // _TAG_FONT_WIDTH_DIVISOR)
        )
        pad_x, pad_y = _TAG_PADDING_PX
        for position, (page, thumb) in enumerate(zip(slides, thumbs)):
            x = _CELL_GAP_PX + (position % cols) * (width + _CELL_GAP_PX)
            y = _CELL_GAP_PX + (position // cols) * (cell_height + _CELL_GAP_PX)
            grid.paste(thumb, (x, y))
            draw.rectangle(
                [x, y, x + width - 1, y + thumb.height - 1], outline=_CELL_OUTLINE
            )
            label = str(page)
            _left, _top, right, bottom = draw.textbbox((0, 0), label, font=font)
            draw.rectangle(
                [x, y, x + right + 2 * pad_x, y + bottom + 2 * pad_y], fill=_TAG_FILL
            )
            draw.text((x + pad_x, y + pad_y), label, fill="white", font=font)
        return grid


def _png_base64(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()
