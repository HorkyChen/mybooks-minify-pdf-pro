"""
PDF 页面渲染、图像处理与压缩编码。

移植自 MyBooks 仓库 tools/pdf（minify_pdf.py + pdf_image_processor.py），
去掉依赖 OpenCV 的 inpaint 与默认关闭的 opening，其余处理链路保持一致。
"""

import io
import math
import shutil
import struct
import subprocess
import tempfile
import zlib
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional

import fitz
from PIL import Image
from PIL import ImageFilter
from PIL import ImageOps
from PIL import features

DEFAULT_RENDER_DPI = 150
MORPHOLOGY_SUPERSAMPLE_FACTOR = 2
JPEG_SUFFIXES = {".jpg", ".jpeg"}

MAX_WIDTH_MIN = 600
MAX_WIDTH_MAX = 2048
QUALITY_MIN = 60
QUALITY_MAX = 100
ALLOWED_DILATE = (0.0, 0.25, 0.5, 1.0)
ALLOWED_BLUR = (0.0, 1.0, 2.0)
GAMMA_MIN = 0.4
GAMMA_MAX = 2.5


def otsu_threshold(gray_img: Image.Image) -> int:
    hist = gray_img.histogram()
    total = sum(hist)
    if total == 0:
        return 127

    sum_total = sum(i * count for i, count in enumerate(hist))
    sum_bg = 0
    weight_bg = 0
    var_max = -1.0
    threshold = 127

    for i, count in enumerate(hist):
        weight_bg += count
        if weight_bg == 0:
            continue
        weight_fg = total - weight_bg
        if weight_fg == 0:
            break

        sum_bg += i * count
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_total - sum_bg) / weight_fg
        var_between = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
        if var_between > var_max:
            var_max = var_between
            threshold = i

    return threshold


def _to_binary(img: Image.Image, bilevel_source: bool = False) -> Image.Image:
    gray = img.convert("L")
    if bilevel_source:
        # 源图本就是两级色（1-bit，常见为 Indexed 调色板），按墨色与纸色的中点切分
        # 才能还原原始点阵；Otsu 在整页大面积留白时会被纸色拉高阈值，把字明显加粗。
        low, high = gray.getextrema()
        threshold = (low + high) // 2 if high - low >= 16 else otsu_threshold(gray)
    else:
        threshold = otsu_threshold(gray)
    return gray.point(lambda x: 255 if x > threshold else 0, mode="1")


def _apply_dct_denoise(img: Image.Image, strength: int) -> Image.Image:
    for _ in range(strength):
        img = img.filter(ImageFilter.MedianFilter(size=3))
    return img


def _auto_correct_by_histogram(img: Image.Image) -> Image.Image:
    return ImageOps.autocontrast(img, cutoff=1)


def _apply_gamma(img: Image.Image, gamma: float) -> Image.Image:
    """按 gamma 曲线调整中间调：gamma > 1 提亮，gamma < 1 压暗，1 为原样。"""
    if gamma <= 0 or math.isclose(gamma, 1.0, rel_tol=0, abs_tol=1e-6):
        return img
    if img.mode not in ("RGB", "L"):
        return img
    inverse = 1.0 / gamma
    lut = [
        min(255, max(0, round(255 * ((value / 255.0) ** inverse))))
        for value in range(256)
    ]
    return img.point(lut * len(img.getbands()))


def _cleanup_gray_background(img: Image.Image, max_brightness: int) -> Image.Image:
    gray = img.convert("L")
    return gray.point(lambda value: 255 if value > max_brightness else value)


def dilate_scale_and_radius(dilate: float) -> tuple[int, int]:
    fraction = Fraction(str(dilate)).limit_denominator(4)
    if fraction.denominator not in (1, 2, 4) or not math.isclose(
        float(fraction), dilate, rel_tol=0, abs_tol=1e-6
    ):
        raise ValueError("dilate 只支持 0.25 像素步进")
    return fraction.denominator, fraction.numerator


def _apply_dilation(img: Image.Image, dilate: float) -> Image.Image:
    if dilate <= 0:
        return img

    scale, radius = dilate_scale_and_radius(dilate)
    original_size = img.size
    if scale > 1:
        img = img.resize(
            (img.width * scale, img.height * scale), resample=Image.Resampling.LANCZOS
        )

    size = radius * 2 + 1
    inverted = ImageOps.invert(img)
    dilated = ImageOps.invert(inverted.filter(ImageFilter.MaxFilter(size=size)))
    if scale > 1:
        dilated = dilated.resize(original_size, resample=Image.Resampling.LANCZOS)
    return dilated


def _apply_closing(img: Image.Image) -> Image.Image:
    original_size = img.size
    scale = MORPHOLOGY_SUPERSAMPLE_FACTOR
    enlarged = img.resize(
        (img.width * scale, img.height * scale), resample=Image.Resampling.LANCZOS
    )
    foreground = ImageOps.invert(enlarged)
    processed = foreground.filter(ImageFilter.MaxFilter(size=3)).filter(
        ImageFilter.MinFilter(size=3)
    )
    result = ImageOps.invert(processed)
    return result.resize(original_size, resample=Image.Resampling.LANCZOS)


def _scale_to_width(img: Image.Image, max_width: int) -> Image.Image:
    if max_width <= 0 or img.width <= max_width:
        return img
    height = max(1, round(img.height * max_width / img.width))
    return img.resize((max_width, height), resample=Image.Resampling.LANCZOS)


@dataclass(frozen=True)
class ImageProcessOptions:
    to_bw: bool = False
    to_gray: bool = False
    auto_correct: bool = False
    max_brightness: Optional[int] = None
    dilate: float = 0.0
    blur: float = 0.0
    closing: bool = False
    dct_denoise: int = 0
    max_width: Optional[int] = None
    gamma: float = 1.0

    def __post_init__(self) -> None:
        if self.to_bw and self.to_gray:
            raise ValueError("黑白与灰度不能同时使用")
        if self.max_brightness is not None and not self.to_gray:
            raise ValueError("背景亮度阈值仅用于灰度模式")
        if self.max_brightness is not None and not 0 <= self.max_brightness <= 255:
            raise ValueError("背景亮度阈值必须在 0-255 之间")
        if not GAMMA_MIN <= self.gamma <= GAMMA_MAX:
            raise ValueError("gamma 必须在 %s-%s 之间" % (GAMMA_MIN, GAMMA_MAX))
        if self.blur < 0:
            raise ValueError("blur 不能小于 0")
        if self.dilate < 0:
            raise ValueError("dilate 不能小于 0")
        dilate_scale_and_radius(self.dilate)
        if not 0 <= self.dct_denoise <= 3:
            raise ValueError("DCT 去噪强度必须在 0-3 之间")
        if self.max_width is not None and self.max_width <= 0:
            raise ValueError("最大宽度必须为正整数")


class PdfImageProcessor:
    def __init__(self, options: Optional[ImageProcessOptions] = None) -> None:
        self.options = options or ImageProcessOptions()

    def process(self, img: Image.Image, has_dct_image: bool = False,
                bilevel_source: bool = False) -> Image.Image:
        options = self.options
        if options.max_width is not None:
            img = _scale_to_width(img, options.max_width)
        if img.mode not in ("RGB", "L", "1"):
            img = img.convert("RGB")

        if options.dct_denoise and has_dct_image:
            img = _apply_dct_denoise(img, options.dct_denoise)
        if options.auto_correct:
            img = _auto_correct_by_histogram(img)
        if options.gamma != 1.0:
            img = _apply_gamma(img, options.gamma)
        if options.closing:
            img = _apply_closing(img)
        if options.dilate > 0:
            img = _apply_dilation(img, options.dilate)
        if options.blur > 0:
            img = img.filter(ImageFilter.GaussianBlur(radius=options.blur))

        if options.to_bw:
            return _to_binary(img, bilevel_source)
        if options.to_gray:
            img = img.convert("L")
            if options.max_brightness is not None:
                img = _cleanup_gray_background(img, options.max_brightness)
            return img
        return img if img.mode == "RGB" else img.convert("RGB")


def is_bilevel_page(page: fitz.Page) -> bool:
    images = page.get_images(full=True)
    return bool(images) and all(image[4] == 1 for image in images)


def page_has_dct_image(page: fitz.Page) -> bool:
    return any("DCTDecode" in (image[8] or "") for image in page.get_images(full=True))


def _single_full_page_image(page: fitz.Page) -> Optional[dict]:
    """整页只铺一张图时返回该图信息，否则返回 None。"""
    images = page.get_image_info()
    if len(images) != 1:
        return None

    image = images[0]
    bbox = fitz.Rect(image["bbox"])
    page_rect = page.rect
    if (
        bbox.x0 > page_rect.x0 + page_rect.width * 0.05
        or bbox.y0 > page_rect.y0 + page_rect.height * 0.05
        or bbox.x1 < page_rect.x1 - page_rect.width * 0.05
        or bbox.y1 < page_rect.y1 - page_rect.height * 0.05
    ):
        return None
    return image


def page_native_dpi(page: fitz.Page) -> Optional[float]:
    """整页单图时返回该图的原生 DPI，其他页面返回 None。"""
    image = _single_full_page_image(page)
    if image is None:
        return None

    bbox = fitz.Rect(image["bbox"])
    if bbox.width <= 0 or bbox.height <= 0:
        return None
    image_dpi = min(
        image["width"] * 72 / bbox.width, image["height"] * 72 / bbox.height
    )
    return image_dpi if image_dpi > 0 else None


def page_native_width(page: fitz.Page) -> Optional[int]:
    """整页单图时返回该图原生分辨率对应的页面像素宽度（避免预览放大超采样）。"""
    image_dpi = page_native_dpi(page)
    if image_dpi is None:
        return None
    return max(1, round(page.rect.width * image_dpi / 72))


def render_page(
    page: fitz.Page,
    options: ImageProcessOptions,
    max_width: Optional[int],
) -> Image.Image:
    src_width = float(page.rect.width)
    if max_width and src_width > 0:
        scale = max_width / src_width
    else:
        # 未限定页宽：扫描页按嵌入图原生分辨率渲染，不额外缩放。
        scale = (page_native_dpi(page) or DEFAULT_RENDER_DPI) / 72.0
    scale = max(scale, 0.01)

    matrix = fitz.Matrix(scale, scale)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    mode = "RGB" if pix.n >= 3 else "L"
    img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)

    return PdfImageProcessor(options).process(
        img,
        has_dct_image=page_has_dct_image(page),
        bilevel_source=is_bilevel_page(page),
    )


def _quality_to_db(quality: int) -> float:
    return 20.0 + quality * 0.2


def _jbig2_encoder_path() -> Optional[str]:
    return shutil.which("jbig2")


def _encode_jbig2(img: Image.Image, encoder_path: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="minify-pdf-jbig2-") as temp_dir:
        input_path = Path(temp_dir) / "page.png"
        img.save(input_path, format="PNG")
        result = subprocess.run(
            [encoder_path, "-p", str(input_path)],
            capture_output=True,
            check=False,
        )

    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"jbig2 编码失败（退出码 {result.returncode}）：{detail}")

    encoded = result.stdout
    if len(encoded) < 27:
        raise RuntimeError("jbig2 返回了不完整的 PDF 数据流")
    width, height, _, _ = struct.unpack(">IIII", encoded[11:27])
    if (width, height) != img.size:
        raise RuntimeError(f"jbig2 返回的尺寸不符：{width} x {height}")
    return encoded


def _bilevel_encoding_candidates(
    img: Image.Image,
    use_jpeg2000: bool,
    jbig2_encoder: Optional[str],
) -> dict[str, bytes]:
    candidates = {"FlateDecode": zlib.compress(img.tobytes(), level=9)}
    if use_jpeg2000:
        buffer = io.BytesIO()
        img.convert("L").save(buffer, format="JPEG2000", irreversible=False)
        candidates["JPXDecode"] = buffer.getvalue()
    if jbig2_encoder is not None:
        candidates["JBIG2Decode"] = _encode_jbig2(img, jbig2_encoder)
    return candidates


def save_pdf(
    images: list[Image.Image],
    output_pdf: Path,
    quality: int,
    use_jpeg2000: bool,
    page_sizes: Optional[list[tuple[float, float]]] = None,
    lossless: bool = False,
    progress: Optional[Callable[[int], None]] = None,
) -> Counter[str]:
    merged = fitz.open()
    filter_counts: Counter[str] = Counter()
    has_bilevel_pages = any(img.mode == "1" for img in images)
    jbig2_encoder = _jbig2_encoder_path() if has_bilevel_pages else None
    total = len(images) or 1
    try:
        for index, img in enumerate(images):
            buffer = io.BytesIO()
            page_width, page_height = (
                page_sizes[index]
                if page_sizes is not None
                else (img.width * 72 / DEFAULT_RENDER_DPI, img.height * 72 / DEFAULT_RENDER_DPI)
            )
            if img.mode == "1":
                candidates = _bilevel_encoding_candidates(img, use_jpeg2000, jbig2_encoder)
                filter_name, stream_data = min(
                    candidates.items(), key=lambda candidate: len(candidate[1])
                )
                page = merged.new_page(width=page_width, height=page_height)
                if filter_name == "JPXDecode":
                    page.insert_image(page.rect, stream=stream_data)
                else:
                    img.save(buffer, format="PNG", optimize=True)
                    xref = page.insert_image(page.rect, stream=buffer.getvalue())
                    merged.update_stream(xref, stream_data, compress=False)
                    merged.xref_set_key(xref, "Filter", f"/{filter_name}")
                filter_counts[filter_name] += 1
            elif use_jpeg2000:
                if lossless:
                    img.save(buffer, format="JPEG2000", irreversible=False)
                else:
                    img.save(
                        buffer,
                        format="JPEG2000",
                        quality_mode="dB",
                        quality_layers=[_quality_to_db(quality)],
                        irreversible=True,
                    )
                page = merged.new_page(width=page_width, height=page_height)
                page.insert_image(page.rect, stream=buffer.getvalue())
                filter_counts["JPXDecode"] += 1
            else:
                save_options = {"optimize": True, "resolution": img.width * 72 / page_width}
                save_options["quality"] = quality
                img.save(buffer, format="PDF", **save_options)
                page_doc = fitz.open(stream=buffer.getvalue(), filetype="pdf")
                merged.insert_pdf(page_doc)
                page_doc.close()
                filter_counts["DCTDecode"] += 1
            if progress is not None:
                progress(int((index + 1) * 100 / total))
        merged.save(str(output_pdf))
    finally:
        merged.close()
    return filter_counts


def jpeg2000_available() -> bool:
    return features.check_codec("jpg_2000")
