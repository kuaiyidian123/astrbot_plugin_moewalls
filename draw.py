"""搜索结果预览图渲染。

把若干张缩略图拼成一张带序号徽标的网格图，方便用户在聊天里按序号选择。
标题为英文，使用系统内置的无衬线字体即可，找不到时回退到 PIL 默认字体。
"""

import io
from typing import Dict, List, Optional

from PIL import Image, ImageDraw, ImageFont

THUMB_W, THUMB_H = 360, 203      # 单个缩略图尺寸（约 16:9）
GAP = 14                          # 单元格间距
PADDING = 18                      # 画布四周留白
TITLE_H = 40                      # 标题条高度
BG_COLOR = (24, 26, 32)
TITLE_BG = (36, 39, 48)
TEXT_COLOR = (235, 238, 245)
BADGE_BG = (222, 58, 58)
BADGE_TEXT = (255, 255, 255)

# 按优先级尝试的系统字体（Windows / macOS / Linux 常见路径）
_FONT_CANDIDATES = (
    "arialbd.ttf",
    "arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

# 支持中文的字体（标题用，避免中文显示成方块）
_CJK_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)


def _load_font(size: int, prefer_cjk: bool = False):
    """加载指定字号字体；prefer_cjk 时优先中文字体，全部失败时回退 PIL 默认位图字体"""
    candidates = (_CJK_FONT_CANDIDATES + _FONT_CANDIDATES) if prefer_cjk else _FONT_CANDIDATES
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """等比缩放并居中裁剪到目标尺寸（cover 效果）"""
    iw, ih = img.size
    if iw <= 0 or ih <= 0:
        return Image.new("RGB", (w, h), BG_COLOR)
    scale = max(w / iw, h / ih)
    nw, nh = max(1, int(iw * scale + 0.5)), max(1, int(ih * scale + 0.5))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _fit_title(text: str, font, max_w: int, draw: ImageDraw.ImageDraw) -> str:
    """把标题裁剪到指定宽度内，超出部分用省略号结尾"""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        if draw.textlength(text, font=font) <= max_w:
            return text
        while text and draw.textlength(text + "…", font=font) > max_w:
            text = text[:-1]
        return text + "…"
    except Exception:
        # 某些字体不支持 textlength 时退化为按字符数截断
        return text[:32] + ("…" if len(text) > 32 else "")


def draw_search_result_image(
    items: List[Dict],
    out_path: str,
    cols: int = 4,
    start_index: int = 1,
) -> Optional[str]:
    """把搜索结果拼成一张预览图并保存为 JPEG。

    items: [{"thumb": bytes, "title": str}, ...]，thumb 为缩略图原始字节。
    返回保存路径；无有效缩略图时返回 None。
    """
    valid = [it for it in items if it.get("thumb")]
    if not valid:
        return None

    n = len(valid)
    cols = max(1, min(int(cols or 4), n))
    rows = (n + cols - 1) // cols
    width = PADDING * 2 + cols * THUMB_W + (cols - 1) * GAP
    height = PADDING * 2 + rows * (THUMB_H + TITLE_H) + (rows - 1) * GAP

    canvas = Image.new("RGB", (width, height), BG_COLOR)
    draw = ImageDraw.Draw(canvas)
    num_font = _load_font(30)
    title_font = _load_font(21, prefer_cjk=True)

    for i, item in enumerate(valid):
        row, col = divmod(i, cols)
        x = PADDING + col * (THUMB_W + GAP)
        y = PADDING + row * (THUMB_H + TITLE_H + GAP)

        try:
            thumb = Image.open(io.BytesIO(item["thumb"])).convert("RGB")
        except Exception:
            continue
        canvas.paste(_cover(thumb, THUMB_W, THUMB_H), (x, y))

        # 左上角序号徽标
        label = str(i + start_index)
        try:
            bbox = draw.textbbox((0, 0), label, font=num_font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            ox, oy = bbox[0], bbox[1]
        except Exception:
            tw, th, ox, oy = 16, 22, 0, 0
        pad = 9
        draw.rectangle(
            [x + 6, y + 6, x + 6 + tw + pad * 2, y + 6 + th + pad * 2],
            fill=BADGE_BG,
        )
        draw.text((x + 6 + pad - ox, y + 6 + pad - oy), label, font=num_font, fill=BADGE_TEXT)

        # 底部标题条
        draw.rectangle([x, y + THUMB_H, x + THUMB_W, y + THUMB_H + TITLE_H], fill=TITLE_BG)
        title = _fit_title(item.get("title", ""), title_font, THUMB_W - 16, draw)
        try:
            tb = draw.textbbox((0, 0), title, font=title_font)
            ty = y + THUMB_H + (TITLE_H - (tb[3] - tb[1])) // 2 - tb[1]
        except Exception:
            ty = y + THUMB_H + 11
        draw.text((x + 8, ty), title, font=title_font, fill=TEXT_COLOR)

    canvas.save(out_path, "JPEG", quality=92)
    return out_path
