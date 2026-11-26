"""
PDF瘦身 PRO —— MyBooks Toolbox 工具后端。

在内置 PDF 瘦身工具的能力基础上，移植 tools/pdf 的最新图像处理链路：
支持灰度/黑白、自动对比度、背景亮度阈值、缩放、高斯模糊、笔画加粗（dilate）、
闭运算、DCT 去噪、JPEG 2000 无损编码，并提供书库选书、逐页预览与直接替换书库 PDF。
"""

import io
import json
import logging
import math
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import fitz

from webserver.handlers.base import BaseHandler, js, is_admin
from webserver.i18n import _
from webserver.services import AsyncService
from webserver.toolbox.base_tool import BaseTool

from .pdf_engine import (
    ALLOWED_BLUR,
    ALLOWED_DILATE,
    GAMMA_MAX,
    GAMMA_MIN,
    MAX_WIDTH_MAX,
    MAX_WIDTH_MIN,
    QUALITY_MAX,
    QUALITY_MIN,
    ImageProcessOptions,
    is_bilevel_page,
    jpeg2000_available,
    page_native_width,
    render_page,
    save_pdf,
)

MAX_SEARCH_RESULTS = 50
WORK_RETENTION_SECONDS = 7 * 24 * 60 * 60
CLEANUP_INTERVAL_SECONDS = 10 * 60
PREVIEW_WIDTH_MIN = 320
PREVIEW_WIDTH_MAX = 4096
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_QUERY_KEYS = (
    "mode",
    "max_width",
    "quality",
    "lossless",
    "auto",
    "max_brightness",
    "dilate",
    "blur",
    "closing",
    "dct_denoise",
    "gamma",
    "skip_pages",
    "drop_pages",
)


def _calibre_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _bool_value(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def _int_value(value, option: str, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError) as err:
        raise ValueError(_("%s 必须是数字") % option) from err


def _float_value(value, option: str, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as err:
        raise ValueError(_("%s 必须是数字") % option) from err


def _text_value(value) -> str:
    return str(value).strip() if value is not None else ""


def _image_color_info(colorspace: str, alternate_colorspace: str,
                      bits_per_component: int) -> tuple:
    colorspace = (alternate_colorspace or colorspace or "").lower()
    if "rgb" in colorspace:
        return "RGB", 3
    if "cmyk" in colorspace:
        return "CMYK", 4
    if "gray" in colorspace:
        return ("BW" if bits_per_component == 1 else "Gray"), 1
    if "indexed" in colorspace:
        return "Indexed", 1
    return colorspace or "Unknown", None


_PDF_DATE_RE = re.compile(r"D:(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?")


def _created_at(path: str, metadata: dict) -> str:
    match = _PDF_DATE_RE.search((metadata or {}).get("creationDate") or "")
    if match:
        year, month, day, hour, minute, second = match.groups()
        text = f"{year}-{month}-{day}"
        if hour and minute:
            text += f" {hour}:{minute}"
            if second:
                text += f":{second}"
        return text
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return ""


def _add_format_entry(entries: dict, order: list, key: tuple, base: dict,
                      page_number: int) -> None:
    if key not in entries:
        entry = dict(base)
        entry["pages"] = []
        entries[key] = entry
        order.append(key)
    pages = entries[key]["pages"]
    if page_number not in pages:
        pages.append(page_number)


def _collect_page_formats(doc: fitz.Document, limit: int = 5) -> list:
    entries: dict = {}
    order: list = []
    analyzed = min(limit, len(doc))
    for page_index in range(analyzed):
        page = doc.load_page(page_index)
        page_number = page_index + 1
        images = page.get_images(full=True)
        if not images:
            _add_format_entry(
                entries, order, ("text",), {"kind": "text"}, page_number
            )
            continue

        page_added = False
        fallback = None
        for image in images:
            xref, width, height, bits = image[0], image[2], image[3], image[4]
            try:
                filter_type, filter_value = doc.xref_get_key(xref, "Filter")
            except Exception:  # noqa: BLE001
                continue
            decoder = (
                " + ".join(filter_value.strip("[]").replace("/", "").split())
                if filter_type != "null" and filter_value else "Unfiltered"
            )
            color_mode, channels = _image_color_info(
                image[5], image[6], bits
            )
            fallback = {
                "kind": "image",
                "decoder": decoder,
                "dpi_x": None,
                "dpi_y": None,
                "width": width,
                "height": height,
                "color_mode": color_mode,
                "channels": channels,
            }
            for _, matrix in page.get_image_rects(xref, transform=True):
                width_pt = math.hypot(matrix.a, matrix.b)
                height_pt = math.hypot(matrix.c, matrix.d)
                if width_pt <= 0 or height_pt <= 0:
                    continue
                dpi_x = round(width * 72 / width_pt, 1)
                dpi_y = round(height * 72 / height_pt, 1)
                base = dict(fallback, dpi_x=dpi_x, dpi_y=dpi_y)
                key = ("image", decoder, dpi_x, dpi_y, width, height,
                       color_mode, channels)
                _add_format_entry(entries, order, key, base, page_number)
                page_added = True

        if not page_added and fallback is not None:
            key = ("image", fallback["decoder"], None, None,
                   fallback["width"], fallback["height"],
                   fallback["color_mode"], fallback["channels"])
            _add_format_entry(entries, order, key, fallback, page_number)

    return [entries[key] for key in order]


def _parse_page_selection(page_spec: str, total_pages: int, option_name: str) -> set:
    if not page_spec.strip():
        return set()

    parsed = set()
    for token in [item.strip() for item in page_spec.split(",") if item.strip()]:
        try:
            number = int(token)
        except ValueError as err:
            raise ValueError(_("%s 中的页码不合法：%s") % (option_name, token)) from err

        if number == 0:
            raise ValueError(_("%s 中不能使用页码 0") % option_name)

        index = number - 1 if number > 0 else total_pages + number
        if index < 0 or index >= total_pages:
            raise ValueError(_("%s 中的页码超出范围：%s") % (option_name, token))
        parsed.add(index)

    return parsed


@dataclass(frozen=True)
class ToolParams:
    mode: str = "gray"
    max_width: Optional[int] = None
    quality: int = 85
    lossless: bool = False
    auto_correct: bool = True
    max_brightness: Optional[int] = 200
    dilate: float = 0.0
    blur: float = 0.0
    closing: bool = False
    dct_denoise: int = 0
    gamma: float = 1.0
    skip_pages: str = ""
    drop_pages: str = ""

    def image_options(self, page: fitz.Page, skip_filters: bool) -> ImageProcessOptions:
        to_bw = (self.mode == "bw" or is_bilevel_page(page)) and not skip_filters
        to_gray = self.mode == "gray" and not skip_filters and not to_bw
        return ImageProcessOptions(
            to_bw=to_bw,
            to_gray=to_gray,
            auto_correct=self.auto_correct and not skip_filters,
            max_brightness=self.max_brightness if to_gray else None,
            dilate=self.dilate if not skip_filters else 0.0,
            blur=self.blur if not skip_filters else 0.0,
            closing=self.closing and not skip_filters,
            dct_denoise=self.dct_denoise,
            gamma=self.gamma if not skip_filters else 1.0,
        )


def parse_params(raw: Optional[dict]) -> ToolParams:
    raw = raw or {}

    mode = _text_value(raw.get("mode")) or "gray"
    if mode not in ("color", "gray", "bw"):
        raise ValueError(_("色彩模式不合法"))

    max_width = _int_value(raw.get("max_width"), _("最大宽度"))
    if max_width is not None and not MAX_WIDTH_MIN <= max_width <= MAX_WIDTH_MAX:
        raise ValueError(_("最大宽度必须在 %d-%d 之间") % (MAX_WIDTH_MIN, MAX_WIDTH_MAX))

    quality = _int_value(raw.get("quality"), _("压缩质量"), 85)
    if not QUALITY_MIN <= quality <= QUALITY_MAX:
        raise ValueError(_("压缩质量必须在 %d-%d 之间") % (QUALITY_MIN, QUALITY_MAX))

    dilate = round(_float_value(raw.get("dilate"), "dilate"), 4)
    if dilate not in ALLOWED_DILATE:
        raise ValueError(_("dilate 只支持 0、0.25、0.5、1"))

    blur = round(_float_value(raw.get("blur"), "blur"), 4)
    if blur not in ALLOWED_BLUR:
        raise ValueError(_("blur 只支持 0、1、2"))

    max_brightness = None
    if mode == "gray":
        max_brightness = _int_value(raw.get("max_brightness"), _("背景亮度阈值"), 200)
        if not 0 <= max_brightness <= 255:
            raise ValueError(_("背景亮度阈值必须在 0-255 之间"))

    dct_denoise = _int_value(raw.get("dct_denoise"), _("DCT 去噪强度"), 0)
    if not 0 <= dct_denoise <= 3:
        raise ValueError(_("DCT 去噪强度必须在 0-3 之间"))

    gamma = round(_float_value(raw.get("gamma"), _("Gamma 值"), 1.0), 3)
    if not GAMMA_MIN <= gamma <= GAMMA_MAX:
        raise ValueError(
            _("Gamma 值必须在 %s-%s 之间") % (GAMMA_MIN, GAMMA_MAX)
        )

    return ToolParams(
        mode=mode,
        max_width=max_width,
        quality=quality,
        lossless=_bool_value(raw.get("lossless")),
        auto_correct=_bool_value(raw.get("auto")),
        max_brightness=max_brightness,
        dilate=dilate,
        blur=blur,
        closing=_bool_value(raw.get("closing")),
        dct_denoise=dct_denoise,
        gamma=gamma,
        skip_pages=_text_value(raw.get("skip_pages")),
        drop_pages=_text_value(raw.get("drop_pages")),
    )


def _safe_filename(name: str, suffix: str = "_minify.pdf") -> str:
    base = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name or "").strip() or "PDF"
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    return f"{base}{suffix}"


class MinifyPdfPro(BaseTool):
    service_item_name = "PDF瘦身 PRO"

    _jobs: dict = {}
    _jobs_order: list = []
    _jobs_lock = threading.Lock()
    _MAX_JOBS = 20

    _cleanup_lock = threading.Lock()
    _last_cleanup = 0.0

    @staticmethod
    def info():
        return {
            "tool_id": "minify_pdf_pro",
            "name": "PDF瘦身 PRO",
            "description": "从书库选书或上传 PDF，逐页预览处理效果，支持灰度/黑白、缩放、"
            "模糊、加粗、无损编码等参数；可将更小的结果直接写回书库。",
            "revision": "0.1.0",
            "author": "Horky",
            "publish_date": "2026-11-26",
            "repo_url": "https://github.com/HorkyChen/mybooks-minify-pdf-pro",
        }

    # ------------------------------------------------------------------
    # 工作目录与任务状态
    # ------------------------------------------------------------------

    def work_root(self) -> str:
        return self.api.storage.get_work_dir("")

    def uploads_dir(self) -> str:
        path = os.path.join(self.work_root(), "uploads")
        os.makedirs(path, exist_ok=True)
        return path

    def upload_path(self, upload_id: str) -> str:
        if not _JOB_ID_RE.match(upload_id or ""):
            raise ValueError(_("上传文件 ID 不合法"))
        return os.path.join(self.uploads_dir(), f"{upload_id}.pdf")

    @classmethod
    def _put_job(cls, job_id: str, data: dict) -> None:
        with cls._jobs_lock:
            cls._jobs[job_id] = data
            if job_id not in cls._jobs_order:
                cls._jobs_order.append(job_id)
            while len(cls._jobs_order) > cls._MAX_JOBS:
                oldest = cls._jobs_order.pop(0)
                cls._jobs.pop(oldest, None)

    @classmethod
    def _update_job(cls, job_id: str, **changes) -> None:
        with cls._jobs_lock:
            job = cls._jobs.get(job_id)
            if job is not None:
                job.update(changes)

    @classmethod
    def _get_job(cls, job_id: str) -> Optional[dict]:
        with cls._jobs_lock:
            job = cls._jobs.get(job_id)
            return dict(job) if job else None

    def _cleanup_old_work_dirs(self) -> None:
        """清理工具产生的临时文件。

        每次执行工具操作时都会调用，但最多每 ``CLEANUP_INTERVAL_SECONDS`` 秒真正扫描
        一次，避免高频请求反复遍历目录。清理内容：

        - ``uploads`` 下超过保留期的上传文件；
        - 不属于内存中任务的 ``job-*`` 目录（进程重启后这些任务已无法下载，直接删除）；
        - 内存中任务对应、但超过保留期的 ``job-*`` 目录。
        """
        now = time.time()
        cls = type(self)
        with self._cleanup_lock:
            if now - cls._last_cleanup < CLEANUP_INTERVAL_SECONDS:
                return
            # 记录在类上：每次请求都会新建工具实例，节流状态必须跨实例共享。
            cls._last_cleanup = now
        try:
            root = self.work_root()
            with self._jobs_lock:
                known_jobs = set(self._jobs)
            for name in os.listdir(root):
                path = os.path.join(root, name)
                try:
                    if name == "uploads":
                        self._cleanup_uploads(path, now)
                        continue
                    if not name.startswith("job-") or not os.path.isdir(path):
                        continue
                    if name[len("job-"):] not in known_jobs:
                        # 内存里已没有这条任务，结果文件再也无法下载。
                        shutil.rmtree(path, ignore_errors=True)
                        logging.info("[minify_pdf_pro] 已清理无主任务目录：%s", path)
                        continue
                    if now - os.path.getmtime(path) <= WORK_RETENTION_SECONDS:
                        continue
                except OSError:
                    continue
                shutil.rmtree(path, ignore_errors=True)
                logging.info("[minify_pdf_pro] 已清理过期目录：%s", path)
        except Exception as err:  # noqa: BLE001
            logging.warning("[minify_pdf_pro] 清理临时目录失败：%s", err)

    def _remove_job_dir(self, job_id: str) -> None:
        """删除某个任务的临时目录（仅用于不可能再提供下载的任务）。"""
        path = os.path.join(self.work_root(), "job-" + job_id)
        shutil.rmtree(path, ignore_errors=True)

    @staticmethod
    def _cleanup_uploads(directory: str, now: float) -> None:
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            try:
                if not os.path.isfile(path):
                    continue
                if now - os.path.getmtime(path) <= WORK_RETENTION_SECONDS:
                    continue
                os.remove(path)
            except OSError:
                continue

    def _resolve_source(self, source: dict) -> str:
        source_type = (source or {}).get("type")
        if source_type == "book":
            book_id = source.get("book_id")
            if isinstance(book_id, bool) or not isinstance(book_id, int) or book_id <= 0:
                raise ValueError(_("书籍 ID 不合法"))
            path = self.api.calibre.format_abspath(book_id, "PDF")
            if not path or not os.path.exists(path):
                raise RuntimeError(_("找不到该书籍的 PDF 文件"))
            return path
        if source_type == "upload":
            path = self.upload_path(str(source.get("upload_id") or ""))
            if not os.path.exists(path):
                raise RuntimeError(_("上传的 PDF 已过期，请重新上传"))
            return path
        raise ValueError(_("请先选择书籍或上传 PDF"))

    @staticmethod
    def _pdf_info(path: str) -> dict:
        doc = fitz.open(path)
        try:
            page_count = len(doc)
            width = height = 0
            if page_count:
                page = doc.load_page(0)
                width = round(page.rect.width, 2)
                height = round(page.rect.height, 2)
            created_at = _created_at(path, doc.metadata or {})
            page_formats = _collect_page_formats(doc, 5)
        finally:
            doc.close()
        return {
            "page_count": page_count,
            "file_size": os.path.getsize(path),
            "page_width": width,
            "page_height": height,
            "created_at": created_at,
            "pages_analyzed": min(5, page_count),
            "page_formats": page_formats,
        }

    # ------------------------------------------------------------------
    # 书库搜索 / 书籍信息
    # ------------------------------------------------------------------

    @AsyncService.register_function
    def search(self, query: str) -> list:
        self._cleanup_old_work_dirs()

        query = (query or "").strip()
        expr = "format:pdf"
        if query:
            expr = 'title:"%s" and format:pdf' % _calibre_literal(query)

        books = self.api.calibre.search_books(
            expr, max_results=MAX_SEARCH_RESULTS, include_comments=False
        )
        results = []
        for book in books:
            book_id = book.get("id")
            if not isinstance(book_id, int):
                continue
            path = self.api.calibre.format_abspath(book_id, "PDF")
            if not path or not os.path.exists(path):
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            results.append({
                "book_id": book_id,
                "title": book.get("title") or "",
                "author": book.get("author") or ", ".join(book.get("authors") or []),
                "size": size,
                "cover": book.get("thumb") or book.get("img") or "",
            })
        return results

    @AsyncService.register_function
    def book_info(self, book_id: int) -> dict:
        if isinstance(book_id, bool) or not isinstance(book_id, int) or book_id <= 0:
            raise ValueError(_("书籍 ID 不合法"))
        path = self.api.calibre.format_abspath(book_id, "PDF")
        if not path or not os.path.exists(path):
            raise RuntimeError(_("找不到该书籍的 PDF 文件"))
        books = self.api.calibre.get_data_as_dict([book_id])
        book = books[0] if books else {}
        info = self._pdf_info(path)
        info["title"] = book.get("title") or f"#{book_id}"
        info["author"] = book.get("author") or ", ".join(book.get("authors") or [])
        return info

    # ------------------------------------------------------------------
    # 预览渲染
    # ------------------------------------------------------------------

    @AsyncService.register_function
    def render_page_image(self, source: dict, page_number: int, params: ToolParams,
                          processed: bool = True,
                          render_width: Optional[int] = None) -> bytes:
        path = self._resolve_source(source)
        doc = fitz.open(path)
        try:
            total = len(doc)
            if total == 0:
                raise ValueError(_("PDF 没有页面"))
            if not 1 <= page_number <= total:
                raise ValueError(_("页码超出范围"))
            index = page_number - 1
            page = doc.load_page(index)
            if processed:
                skip = _parse_page_selection(params.skip_pages, total, _("跳过处理页"))
                options = params.image_options(page, index in skip)
            else:
                options = ImageProcessOptions()
            width = params.max_width
            native_width = page_native_width(page)
            if width is None:
                # 未限定页宽：预览按原生宽度，其次才回落到设备像素。
                width = native_width or render_width
            else:
                if render_width:
                    # 预览按显示端设备像素渲染，但不得低于输出宽度。
                    width = max(width, render_width)
                if native_width:
                    width = min(width, native_width)
            img = render_page(page, options, width)
            buffer = io.BytesIO()
            img.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue()
        finally:
            doc.close()

    # ------------------------------------------------------------------
    # 开始处理
    # ------------------------------------------------------------------

    @AsyncService.register_function
    def start(self, source: dict, mode: str, raw_params: dict, title: str = "") -> dict:
        self._cleanup_old_work_dirs()

        if mode not in ("replace", "download"):
            raise ValueError(_("不支持的处理方式"))
        params = parse_params(raw_params)
        source_type = (source or {}).get("type")
        if source_type == "upload" and mode == "replace":
            raise ValueError(_("上传的文件只能下载，不能替换书库"))

        if source_type == "book":
            book_id = source.get("book_id")
            if isinstance(book_id, bool) or not isinstance(book_id, int) or book_id <= 0:
                raise ValueError(_("书籍 ID 不合法"))
            books = self.api.calibre.get_data_as_dict([book_id])
            book = books[0] if books else None
            if not book:
                raise RuntimeError(_("书籍不存在：ID=%d") % book_id)
            formats = [f.upper() for f in (book.get("available_formats") or [])]
            if "PDF" not in formats:
                raise RuntimeError(_("该书籍没有 PDF 格式"))
            title = book.get("title") or f"book-{book_id}"
        elif source_type == "upload":
            book_id = None
        else:
            raise ValueError(_("请先选择书籍或上传 PDF"))

        path = self._resolve_source(source)
        if params.lossless and not jpeg2000_available():
            raise ValueError(_("当前环境不支持 JPEG 2000，无法使用无损编码"))

        old_size = os.path.getsize(path)
        with fitz.open(path) as doc:
            total = len(doc)
            if total == 0:
                raise ValueError(_("PDF 没有页面"))
            _parse_page_selection(params.skip_pages, total, _("跳过处理页"))
            drop = _parse_page_selection(params.drop_pages, total, _("删除页"))
            if total - len(drop) <= 0:
                raise ValueError(_("所有页面均被删除"))

        job_id = uuid.uuid4().hex
        task_id = self.api.tasks.create_task(progress_data={
            "title": title, "mode": mode,
        })
        self._put_job(job_id, {
            "job_id": job_id,
            "status": "running",
            "stage": "queued",
            "progress": 0,
            "title": title,
            "book_id": book_id,
            "mode": mode,
            "old_size": old_size,
            "new_size": 0,
            "saved": 0,
            "saved_percent": 0,
            "replaced": False,
            "download_ready": False,
            "filename": _safe_filename(title),
            "error": "",
        })

        self._run(job_id, source, mode, params, title, path, old_size, task_id)
        return {"job_id": job_id, "task_id": task_id}

    @AsyncService.register_service
    def _run(self, job_id: str, source: dict, mode: str, params: ToolParams,
             title: str, source_path: str, old_size: int, task_id: int) -> None:
        work_dir = self.api.storage.get_work_dir("job-" + job_id)
        output_path = os.path.join(work_dir, "minified.pdf")
        try:
            self._progress(job_id, task_id, 0, "rendering")
            doc = fitz.open(source_path)
            try:
                total = len(doc)
                skip = _parse_page_selection(params.skip_pages, total, _("跳过处理页"))
                drop = _parse_page_selection(params.drop_pages, total, _("删除页"))
                kept = [index for index in range(total) if index not in drop]
                images = []
                page_sizes = []
                for position, page_index in enumerate(kept):
                    page = doc.load_page(page_index)
                    images.append(
                        render_page(
                            page,
                            params.image_options(page, page_index in skip),
                            params.max_width,
                        )
                    )
                    page_sizes.append((page.rect.width, page.rect.height))
                    self._progress(
                        job_id, task_id,
                        int((position + 1) / len(kept) * 85),
                        "rendering",
                    )
            finally:
                doc.close()

            self._progress(job_id, task_id, 85, "encoding")
            use_jpeg2000 = jpeg2000_available()
            save_pdf(
                images,
                Path(output_path),
                params.quality,
                use_jpeg2000,
                page_sizes,
                params.lossless and use_jpeg2000,
                progress=lambda percent: self._progress(
                    job_id, task_id, 85 + int(percent * 10 / 100), "encoding"
                ),
            )
            self._write_metadata(output_path, title, use_jpeg2000)

            self._progress(job_id, task_id, 95, "comparing")
            if not os.path.exists(output_path):
                raise RuntimeError(_("没有生成输出文件"))
            new_size = os.path.getsize(output_path)

            if mode == "replace" and new_size >= old_size:
                self._remove(output_path)
                self._finish(job_id, task_id, "no_improvement", {
                    "old_size": old_size,
                    "new_size": new_size,
                    "stage": "done",
                })
                self._remove_job_dir(job_id)
                return

            saved = old_size - new_size
            percent = round(saved * 100.0 / old_size, 1) if old_size else 0.0
            stats = {
                "old_size": old_size,
                "new_size": new_size,
                "saved": saved,
                "saved_percent": percent,
            }

            if mode == "replace":
                self._progress(job_id, task_id, 98, "saving")
                self.api.calibre.add_format(
                    int(source["book_id"]), "PDF", output_path, replace=True
                )
                self._remove(output_path)
                stats.update({"replaced": True, "stage": "done"})
            else:
                stats.update({"download_ready": True, "stage": "done"})

            self._finish(job_id, task_id, "completed", stats)
            if mode == "replace":
                # 已写回书库，任务不会再提供下载，目录无需保留。
                self._remove_job_dir(job_id)
            logging.info(
                "[minify_pdf_pro] job=%s 完成：%s -> %s", job_id, old_size, new_size
            )
        except Exception as err:  # noqa: BLE001
            logging.error("[minify_pdf_pro] 处理失败 job=%s: %s", job_id, err)
            self._remove(output_path)
            self._remove_job_dir(job_id)
            self._update_job(
                job_id, status="failed", stage="failed", progress=100, error=str(err)
            )
            try:
                self.api.tasks.complete_task(task_id, error_message=str(err))
            except Exception as task_err:  # noqa: BLE001
                logging.warning("[minify_pdf_pro] 标记任务失败时出错：%s", task_err)

    @staticmethod
    def _write_metadata(output_path: str, title: str, use_jpeg2000: bool) -> None:
        doc = fitz.open(output_path)
        try:
            metadata = doc.metadata or {}
            metadata["creator"] = "MyBooks(https://mybooks.top) PDF Minifier PRO"
            metadata["title"] = title
            doc.set_metadata(metadata)
            if use_jpeg2000:
                doc.xref_set_key(doc.pdf_catalog(), "Version", "/1.5")
            doc.save(output_path, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        finally:
            doc.close()

    def _progress(self, job_id: str, task_id: int, progress: int, stage: str) -> None:
        self._update_job(job_id, progress=progress, stage=stage)
        try:
            job = self._get_job(job_id) or {}
            self.api.tasks.update_progress(task_id, progress, {
                "title": job.get("title"),
                "stage": stage,
            })
        except Exception as err:  # noqa: BLE001
            logging.warning("[minify_pdf_pro] 更新进度失败：%s", err)

    def _finish(self, job_id: str, task_id: int, status: str, extra: dict) -> None:
        changes = {"status": status, "progress": 100}
        changes.update(extra)
        self._update_job(job_id, **changes)
        try:
            job = self._get_job(job_id) or {}
            self.api.tasks.update_progress(task_id, 100, {
                "title": job.get("title"),
                "stage": changes.get("stage", "done"),
                "status": status,
            })
            self.api.tasks.complete_task(task_id)
        except Exception as err:  # noqa: BLE001
            logging.warning("[minify_pdf_pro] 结束任务失败：%s", err)

    @staticmethod
    def _remove(path: str) -> None:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as err:
            logging.warning("[minify_pdf_pro] 删除临时文件失败 %s: %s", path, err)

    def download_file(self, job_id: str) -> tuple:
        self._cleanup_old_work_dirs()
        job = self._get_job(job_id)
        if not job:
            raise RuntimeError(_("任务不存在或已过期"))
        if job.get("status") != "completed" or not job.get("download_ready"):
            raise RuntimeError(_("该任务没有可下载的文件"))
        path = os.path.join(self.api.storage.get_work_dir("job-" + job_id), "minified.pdf")
        if not os.path.exists(path):
            raise RuntimeError(_("文件已过期，请重新处理"))
        return job.get("filename") or "PDF_minify.pdf", path


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------


def _request_params(handler: BaseHandler) -> dict:
    raw = {}
    for key in _QUERY_KEYS:
        value = handler.get_argument(key, None)
        if value is not None:
            raw[key] = value
    return raw


class SearchHandler(BaseHandler):
    @js
    @is_admin
    def get(self):
        try:
            data = MinifyPdfPro().search(self.get_argument("q", ""))
        except Exception as err:  # noqa: BLE001
            logging.error("[minify_pdf_pro] 搜索失败: %s", err)
            return {"err": "search.failed", "msg": _("搜索书籍失败：%s") % err}
        return {"err": "ok", "data": data}


class InfoHandler(BaseHandler):
    @js
    @is_admin
    def get(self):
        book_id = self.get_argument("book_id", "")
        try:
            data = MinifyPdfPro().book_info(int(book_id))
        except (TypeError, ValueError) as err:
            return {"err": "params.invalid", "msg": str(err)}
        except Exception as err:  # noqa: BLE001
            logging.error("[minify_pdf_pro] 读取书籍信息失败: %s", err)
            return {"err": "info.failed", "msg": _("读取书籍信息失败：%s") % err}
        return {"err": "ok", "data": data}


class UploadHandler(BaseHandler):
    @js
    @is_admin
    def post(self):
        if not self.request.files or "file" not in self.request.files:
            return {"err": "params.missing", "msg": _("未上传文件")}

        file_meta = self.request.files["file"][0]
        filename = file_meta.get("filename") or ""
        if not filename.lower().endswith(".pdf"):
            return {"err": "params.invalid", "msg": _("只支持 PDF 文件")}

        tool = MinifyPdfPro()
        upload_id = uuid.uuid4().hex
        path = tool.upload_path(upload_id)
        try:
            with open(path, "wb") as stream:
                stream.write(file_meta["body"])
            info = tool._pdf_info(path)
            if info["page_count"] <= 0:
                raise ValueError(_("PDF 没有页面"))
        except Exception as err:  # noqa: BLE001
            MinifyPdfPro._remove(path)
            logging.error("[minify_pdf_pro] 上传失败: %s", err)
            return {"err": "upload.failed", "msg": _("上传失败：%s") % err}

        info["upload_id"] = upload_id
        info["filename"] = filename
        return {"err": "ok", "data": info}


class RenderHandler(BaseHandler):
    @is_admin
    def get(self):
        try:
            page_number = int(self.get_argument("page", "1"))
            params = parse_params(_request_params(self))
            render_width = _int_value(self.get_argument("render_width", ""), _("预览宽度"))
            if render_width is not None and not (
                PREVIEW_WIDTH_MIN <= render_width <= PREVIEW_WIDTH_MAX
            ):
                raise ValueError(
                    _("预览宽度必须在 %d-%d 之间") % (PREVIEW_WIDTH_MIN, PREVIEW_WIDTH_MAX)
                )
            book_id = self.get_argument("book_id", "")
            upload_id = self.get_argument("upload_id", "")
            if book_id:
                source = {"type": "book", "book_id": int(book_id)}
            elif upload_id:
                source = {"type": "upload", "upload_id": upload_id}
            else:
                raise ValueError(_("请先选择书籍或上传 PDF"))
            data = MinifyPdfPro().render_page_image(
                source,
                page_number,
                params,
                _bool_value(self.get_argument("processed", "0")),
                render_width,
            )
        except ValueError as err:
            self.set_status(400)
            self.set_header("Content-Type", "text/plain; charset=utf-8")
            self.write(str(err))
            return
        except Exception as err:  # noqa: BLE001
            logging.error("[minify_pdf_pro] 渲染页面失败: %s", err)
            self.set_status(500)
            self.set_header("Content-Type", "text/plain; charset=utf-8")
            self.write(str(err))
            return

        self.set_header("Content-Type", "image/png")
        self.set_header("Cache-Control", "no-store")
        self.write(data)


class StartHandler(BaseHandler):
    @js
    @is_admin
    def post(self):
        try:
            body = json.loads(self.request.body or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except (TypeError, ValueError):
            return {"err": "params.invalid", "msg": _("请求体不是合法 JSON")}

        source = body.get("source")
        if not isinstance(source, dict):
            return {"err": "params.invalid", "msg": _("请先选择书籍或上传 PDF")}

        try:
            data = MinifyPdfPro().start(
                source,
                body.get("mode") or "download",
                body.get("params") or {},
                str(body.get("title") or ""),
            )
        except ValueError as err:
            return {"err": "params.invalid", "msg": str(err)}
        except RuntimeError as err:
            return {"err": "start.failed", "msg": str(err)}
        except Exception as err:  # noqa: BLE001
            logging.error("[minify_pdf_pro] 启动任务失败: %s", err)
            return {"err": "start.failed", "msg": _("启动任务失败：%s") % err}
        return {"err": "ok", "data": data}


class StatusHandler(BaseHandler):
    @js
    @is_admin
    def get(self):
        job_id = self.get_argument("job_id", "")
        if not _JOB_ID_RE.match(job_id or ""):
            return {"err": "params.invalid", "msg": _("任务 ID 不合法")}
        job = MinifyPdfPro._get_job(job_id)
        if not job:
            return {"err": "status.not_found", "msg": _("任务不存在或已过期")}
        return {"err": "ok", "data": job}


class DownloadHandler(BaseHandler):
    @is_admin
    def get(self):
        from urllib.parse import quote

        job_id = self.get_argument("job_id", "")
        if not _JOB_ID_RE.match(job_id or ""):
            self.set_status(400)
            self.write("Invalid job_id")
            return

        try:
            filename, path = MinifyPdfPro().download_file(job_id)
        except RuntimeError as err:
            self.set_status(404)
            self.write(str(err))
            return

        ascii_name = "minified.pdf"
        self.set_header("Content-Type", "application/pdf")
        self.set_header(
            "Content-Disposition",
            "attachment; filename=\"%s\"; filename*=UTF-8''%s"
            % (ascii_name, quote(filename)),
        )
        self.set_header("Content-Length", str(os.path.getsize(path)))
        with open(path, "rb") as stream:
            while True:
                chunk = stream.read(256 * 1024)
                if not chunk:
                    break
                self.write(chunk)
