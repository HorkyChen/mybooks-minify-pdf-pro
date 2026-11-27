# PDF 瘦身（PRO）

用于优化PDF大小及质量的MyBooks外部工具。从书库选书或上传 PDF，逐页预览处理效果，调参后把页面渲染成图片重新编码，生成更小的 PDF；确认后可直接替换书库中的原文件。

## 功能

- 书库搜索选书，或直接上传 PDF
- 逐页预览处理结果，支持跳页处理与删页
- 色彩模式：保留彩色 / 灰度 / 黑白
- 图像处理：自动对比度、背景亮度阈值、Gamma、限定页宽缩放、笔画加粗（dilate）、高斯模糊、闭运算、DCT 去噪
- 编码：JPEG 2000（环境支持时默认使用，可切换无损模式）；黑白页在 Flate / JPEG 2000 / JBIG2 中取最小
- 生成后对比新旧体积，可下载或写回书库

## 目录结构

```
minify_pdf_pro/
├── manifest.json          # 工具元数据（tool_id、入口、API 路由等）
├── .toolbuilder.json      # 构建配置（无前端构建步骤）
├── icon.png               # 工具图标
├── backend/
│   ├── tool.py            # 入口类 MinifyPdfPro 与各 HTTP Handler
│   └── pdf_engine.py      # 页面渲染、图像处理与 PDF 编码
├── frontend/
│   ├── index.html         # 入口页面（自包含静态站点）
│   ├── lib/               # 主题样式与 i18n 脚本
│   └── locales/           # 文案（当前仅有 zh）
├── tests/                 # 处理前后对比示例图，人工核对效果用
└── dist/                  # mytool build 产物（已在 .gitignore 中）
```

## 依赖

- 运行在 MyBooks 环境内（依赖 `webserver`、Calibre、任务与存储服务），无法脱离 MyBooks 单独运行
- `PyMuPDF`（`fitz`）：页面渲染与 PDF 读写
- `Pillow`：图像处理与 JPEG 2000 编码；JPEG 2000 需要 Pillow 自带 OpenJPEG 支持
- `jbig2`（可选）：安装后黑白页会额外尝试 JBIG2 编码并取最优结果；未安装则自动跳过

## 构建与安装

```bash
mytool validate .   # 校验 manifest 与目录结构
mytool build        # 生成 dist/minify_pdf_pro-<revision>.zip 并打印 sha256
```

在开启了开发者模式（`ENABLE_TOOLBOX_DEV_MODE`）的 MyBooks 实例中，进入 `/admin/toolbox` 上传该 zip 安装，重启进程后访问 `/toolbox/minify_pdf_pro` 使用。

## API 路由

| 路径 | Handler | 说明 |
|---|---|---|
| `search` | `tool.SearchHandler` | 搜索书库中的 PDF |
| `info` | `tool.InfoHandler` | 读取书籍 / 上传文件的页面与格式信息 |
| `upload` | `tool.UploadHandler` | 上传本地 PDF |
| `render` | `tool.RenderHandler` | 按当前参数渲染指定页预览图 |
| `start` | `tool.StartHandler` | 创建处理任务 |
| `status` | `tool.StatusHandler` | 查询任务进度与结果 |
| `download` | `tool.DownloadHandler` | 下载处理后的 PDF |

## 参数

| 参数 | 范围 | 说明 |
|---|---|---|
| `mode` | `color` / `gray` / `bw` | 色彩模式 |
| `max_width` | 600–2048 | 最大宽度，未设置时按页面原生分辨率渲染 |
| `quality` | 60–100 | 有损编码质量；JPEG 2000 无损模式下不生效 |
| `lossless` | 布尔 | JPEG 2000 无损模式，文件通常更大 |
| `auto` | 布尔 | 自动对比度 |
| `max_brightness` | 0–255 | 灰度模式的背景亮度阈值，高于该值变白 |
| `gamma` | 0.4–2.5 | 大于 1 提亮中间调，小于 1 压暗 |
| `dilate` | 0 / 0.25 / 0.5 / 1 | 笔画加粗 |
| `blur` | 0 / 1 / 2 | 高斯模糊 |
| `closing` | 布尔 | 闭运算连接断笔 |
| `dct_denoise` | 0–3 | 仅对含 JPEG 图片的页面做 3x3 中值滤波 |
| `skip_pages` | 如 `1,3,-1` | 这些页保留但不做图像处理 |
| `drop_pages` | 如 `2,5,-1` | 这些页从输出中删除 |

## License

BSD 2-Clause，见 [LICENSE](LICENSE)。
