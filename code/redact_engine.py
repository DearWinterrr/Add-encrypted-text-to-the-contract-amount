# -*- coding: utf-8 -*-
"""合同金额打码引擎：候选区域自动识别 + 真删除式打码（PyMuPDF）。

供 miwen_gui.py（可视化界面）调用，也可单独批量使用：
    python redact_engine.py 某合同.pdf

识别基于“行 + 文字块”坐标映射，不依赖逐字 search_for，兼容：
- 关键词与金额在同一行（合同金额：128,000.00 / 单价 3500）
- 关键词与金额隔了“（大写）人民币”等修饰词
- 表格里关键词行下方紧跟金额行
- 大写金额（人民币壹拾贰万捌仟元整）
- 扫描件（无文字层）：装有 tesseract 走 OCR；没装则返回空并在清单提示手动框选
"""
from __future__ import annotations

import os
import re
import shutil

import pymupdf as fitz

KEYWORDS = [
    "合同金额", "合同总价", "合同价款", "总金额", "总价款", "合计金额", "价税合计",
    "小写金额", "大写金额", "含税金额", "不含税金额", "单价", "总价",
    "金额", "价款", "价格", "费用", "报酬", "服务费", "尾款", "预付款", "首付款",
    "定金", "保证金", "履约金", "违约金", "赔偿金额", "租金", "月租", "年薪", "月薪",
    "人民币",
]
KEYWORD_EXTEND = 340.0  # 关键词向右扩展上限（PDF point）

_KW_ALT = "|".join(re.escape(k) for k in sorted(KEYWORDS, key=len, reverse=True))
_SEP = r"[ \t\u3000：:＝=，,、。（）()为计约含大小写人民\u00a5¥￥]"

_CN_NUM = "零一二三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟什"

PRECISE_PATTERNS = [
    # 关键词 + 修饰词 + 数字金额（同行，≤14 个修饰字符）
    rf"(?:{_KW_ALT}){_SEP}{{0,14}}\d[\d,，.]*",
    # 关键词行末 + 下一行就是金额（表格）
    rf"(?:{_KW_ALT})[ \t\u3000]{{0,3}}\n[ \t\u3000]{{0,12}}\d[\d,，.]*",
    # 关键词 + 大写金额（“元整”可缺，OCR 断行也能命中）
    rf"(?:{_KW_ALT}){_SEP}{{0,10}}(?:人民币)?[{_CN_NUM}]{{2,}}(?:{_SEP}{{0,4}}(?:元|圆)(?:{_SEP}{{0,2}}(?:整|正))?)?",
    # ¥128,000.00
    r"[\u00a5¥￥]\s*\d[\d,，.]*",
    # 128,000.00 元 / 8万元
    r"\d[\d,，]*\.?\d*\s*(?:元|万元|圆)",
    # 独立大写金额：捌拾贰万伍仟元整 / 人民币柒万元（“元整”可缺）
    rf"(?:人民币)?[{_CN_NUM}]{{3,}}(?:\s*(?:元|圆)\s*(?:整|正)?)?",
]

# 激进模式：所有“看起来像金额”的裸数字（可能误遮编号，可在清单里双击停用）
AGGRESSIVE_PATTERNS = [
    r"\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?",
    r"(?<![\d.])\d+\.\d{1,2}(?![\d.])",
    r"(?<![\d.,])\d{5,}(?![\d.])",
    r"\d+(?:\.\d+)?\s*(?:万|亿)(?:元|人民币)?",
]

FILL_COLORS = {
    "黑色": (0, 0, 0),
    "白色": (1, 1, 1),
    "灰色": (0.45, 0.45, 0.45),
}

_HAS_DIGIT = re.compile(r"\d")
# 金额迹象：阿拉伯数字 / 货币符号 / “元” / 连续两个以上中文大写数字
_MONEY_HINT = re.compile(rf"[\d\u00a5¥￥元]|[{_CN_NUM}]{{2,}}")


# ---------------- tesseract 发现 ----------------
TESSDATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tessdata")


def find_tesseract():
    """返回 tesseract 可执行文件路径，没装则 None。"""
    for env in ("MIWEN_TESSERACT", "TESSERACT_CMD"):
        p = os.environ.get(env)
        if p and os.path.exists(p):
            return p
    exe = shutil.which("tesseract")
    if exe:
        return exe
    local = os.environ.get("LOCALAPPDATA", "")
    roots = [r"C:\Program Files\Tesseract-OCR", r"C:\Program Files (x86)\Tesseract-OCR",
             os.path.join(local, "Programs", "Tesseract-OCR") if local else ""]
    for root in roots:
        if not root:
            continue
        cand = os.path.join(root, "tesseract.exe")
        if os.path.exists(cand):
            return cand
    return None


def _ocr_langs():
    """语言优先级：项目 tessdata 里有 chi_sim 就用中文+英文，否则退回 eng。"""
    if os.path.exists(os.path.join(TESSDATA_DIR, "chi_sim.traineddata")):
        os.environ["TESSDATA_PREFIX"] = TESSDATA_DIR
        return ["chi_sim+eng", "chi_sim", "eng", ""]
    return ["eng", ""]


# ---------------- 文字层读取 ----------------
def _page_lines(page):
    """该页每行 → [(rect, text), ...] 文字块，按位置排序。"""
    lines = []
    for blk in page.get_text("dict")["blocks"]:
        if blk.get("type") != 0:
            continue
        for ln in blk["lines"]:
            spans = [(fitz.Rect(sp["bbox"]), sp["text"])
                     for sp in ln["spans"] if sp["text"].strip()]
            if spans:
                spans.sort(key=lambda s: s[0].x0)
                lines.append(spans)
    lines.sort(key=lambda s: (round(s[0][0].y0, 1), s[0][0].x0))
    return lines


def _lines_text(lines):
    """行拼成“去空格”的紧凑文本（字间空格、逐字拆块也能连续匹配）。
    返回 (紧凑文本, 每行字符区间, 每块(起,止,rect,紧凑text))。"""
    parts, line_ranges, span_ranges = [], [], []
    pos = 0
    for line in lines:
        line_start = pos
        seg, srs = [], []
        for rect, text in line:
            ctext = re.sub(r"\s+", "", text)
            if ctext:
                srs.append((pos, pos + len(ctext), rect, ctext))
                pos += len(ctext)
                seg.append(ctext)
        parts.append("".join(seg))
        line_ranges.append((line_start, pos))
        pos += 1  # 行间 \n
        span_ranges.append(srs)
    return "\n".join(parts), line_ranges, span_ranges


def _rect_from_match(span_ranges, line_ranges, start, end):
    """正则命中的字符区间 → 命中的文字块 [(rect, text)]（可跨行）。"""
    cand = []
    for li, (ls, le) in enumerate(line_ranges):
        if le <= start or ls >= end:
            continue
        for ss, se, rect, text in span_ranges[li]:
            if se <= start or ss >= end:
                continue
            cand.append((rect, text))
    return cand


def _union(items):
    r = fitz.Rect(items[0][0])
    for rect, _ in items[1:]:
        r |= rect
    return r


def _keyword_rect(page, line, compact_text, kw):
    """关键词命中：从关键词所在块遮到同行右端；同行无金额迹象则不遮。
    compact_text 为该行去空格文本，块长度按同一口径累加以对齐偏移。"""
    idx = compact_text.find(kw)
    if idx < 0 or not _MONEY_HINT.search(compact_text[idx + len(kw):]):
        return None
    rect, start_x, offset = fitz.Rect(), None, 0
    for srect, stext in line:
        end = offset + len(re.sub(r"\s+", "", stext))
        if end > idx:
            if start_x is None:
                start_x = srect.x0
            rect |= srect
        offset = end
    if start_x is None:
        return None
    x1 = min(rect.x1, page.rect.width)
    return fitz.Rect(start_x, rect.y0, x1, rect.y1)


# ---------------- 候选识别 ----------------
def find_candidates(pdf_path: str, aggressive: bool = True):
    """返回候选打码区域列表：
    [{"page":0,"rect":(x0,y0,x1,y1),"text":"...","source":"自动|关键词|激进|OCR","enabled":True}]
    """
    precise = [re.compile(p) for p in PRECISE_PATTERNS]
    aggr = [re.compile(p) for p in AGGRESSIVE_PATTERNS] if aggressive else []

    doc = fitz.open(pdf_path)
    blocks, seen = [], set()

    def add(pno, rect, label, source):
        if rect is None:
            return
        rect = rect & doc[pno].rect
        if rect.is_empty or rect.is_infinite:
            return
        r = (round(rect.x0, 1), round(rect.y0, 1), round(rect.x1, 1), round(rect.y1, 1))
        if r[2] <= r[0] or r[3] <= r[1]:
            return
        if (pno, r) in seen:
            return
        seen.add((pno, r))
        blocks.append({"page": pno, "rect": r, "text": (label or "").strip()[:40],
                       "source": source, "enabled": True})

    textless = []
    for pno, page in enumerate(doc):
        lines = _page_lines(page)
        if not lines:
            textless.append(pno)
            continue
        text, line_ranges, span_ranges = _lines_text(lines)

        for group, src in ((precise, "自动"), (aggr, "激进")):
            for pat in group:
                for m in pat.finditer(text):
                    cand = _rect_from_match(span_ranges, line_ranges, m.start(), m.end())
                    if not cand:
                        continue
                    with_digits = [(r, t) for r, t in cand if _HAS_DIGIT.search(t)]
                    add(pno, _union(with_digits or cand), m.group(), src)

        for line in lines:
            lt = "".join(t for _, t in line)
            compact = re.sub(r"\s+", "", lt)
            if not _MONEY_HINT.search(compact):
                continue
            for kw in KEYWORDS:
                if kw in compact:
                    add(pno, _keyword_rect(page, line, compact, kw), compact, "关键词")

    if textless:
        for pno, rect, label in _ocr_pages(doc, textless, precise + aggr):
            add(pno, rect, label, "OCR")

    doc.close()
    blocks = _drop_contained(blocks)
    blocks.sort(key=lambda b: (b["page"], b["rect"][1], b["rect"][0]))
    return blocks


def _drop_contained(blocks):
    """丢掉被同页另一区域完全包含的小块，避免清单重复。"""
    def inside(inner, outer):
        return (outer[0] <= inner[0] + 0.5 and outer[1] <= inner[1] + 0.5
                and outer[2] >= inner[2] - 0.5 and outer[3] >= inner[3] - 0.5)
    keep = []
    for i, b in enumerate(blocks):
        if not any(j != i and b["page"] == o["page"] and inside(b["rect"], o["rect"])
                   for j, o in enumerate(blocks)):
            keep.append(b)
    return keep


def textless_pages(pdf_path: str):
    """返回没有文字层（扫描件）的页码列表，用于界面提示。"""
    doc = fitz.open(pdf_path)
    out = [p for p in range(doc.page_count) if not _page_lines(doc[p])]
    doc.close()
    return out


def _ocr_pages(doc, page_numbers, pats, dpi: int = 200):
    """无文字层页面 OCR；没装 tesseract 返回空列表。"""
    results = []
    exe = find_tesseract()
    if not page_numbers or not exe:
        return results
    try:
        import pytesseract
        from PIL import Image
    except Exception:
        return results
    pytesseract.pytesseract.tesseract_cmd = exe

    zoom = dpi / 72.0
    inv = 1.0 / zoom
    langs = _ocr_langs()
    for pno in page_numbers:
        page = doc[pno]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        data = None
        for lang in langs:
            try:
                if lang:
                    data = pytesseract.image_to_data(img, lang=lang,
                                                     output_type=pytesseract.Output.DICT)
                else:
                    data = pytesseract.image_to_data(img,
                                                     output_type=pytesseract.Output.DICT)
                break
            except Exception:
                continue
        if data is None:
            continue

        groups = {}
        for i in range(len(data["text"])):
            t = data["text"][i].strip()
            if not t:
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            x0, y0 = data["left"][i], data["top"][i]
            box = [x0, y0, x0 + data["width"][i], y0 + data["height"][i]]
            if key in groups:
                toks, bb = groups[key]
                toks.append(t)
                bb[0], bb[1] = min(bb[0], x0), min(bb[1], y0)
                bb[2], bb[3] = max(bb[2], box[2]), max(bb[3], box[3])
            else:
                groups[key] = ([t], box)

        for toks, bb in groups.values():
            line_text = " ".join(toks)
            for pat in pats:
                if pat.search(line_text) or pat.search(line_text.replace(" ", "")):
                    rect = fitz.Rect(bb[0] * inv, bb[1] * inv,
                                     bb[2] * inv, bb[3] * inv) & page.rect
                    results.append((pno, rect, line_text))
                    break
    return results


def search_text_rects(pdf_path: str, needle: str):
    """按用户输入的文字定位：先按行文本匹配（容忍空格差异），再退回 search_for。"""
    out, doc = [], fitz.open(pdf_path)
    clean = "".join(needle.split())
    for pno, page in enumerate(doc):
        hit = False
        for line in _page_lines(page):
            text = "".join(t for _, t in line)
            if clean in text.replace(" ", "").replace("\u3000", ""):
                rect = fitz.Rect()
                for r, _ in line:
                    rect |= r
                out.append({"page": pno, "rect": _pad((rect.x0, rect.y0,
                                                       rect.x1, rect.y1)),
                            "text": text[:40], "source": "手动·文字", "enabled": True})
                hit = True
        if not hit:
            for rect in page.search_for(needle):
                out.append({"page": pno, "rect": _pad((rect.x0, rect.y0,
                                                       rect.x1, rect.y1)),
                            "text": needle[:40], "source": "手动·文字", "enabled": True})
    doc.close()
    return out


def _pad(rect, margin: float = 1.5):
    x0, y0, x1, y1 = rect
    return (round(x0 - margin, 1), round(y0 - margin, 1),
            round(x1 + margin, 1), round(y1 + margin, 1))


# ---------------- 打码与预览 ----------------
def open_masked(src_path: str, blocks, color: str = "黑色"):
    """在源 PDF 副本上真打码（删除底层文字 + 区域内图像像素化），不写盘。
    返回已打好码的 fitz.Document，由调用方 save 或用于预览渲染。
    """
    doc = fitz.open(src_path)
    fill = FILL_COLORS.get(color, FILL_COLORS["黑色"])
    touched = set()
    for b in blocks:
        if not b.get("enabled", True):
            continue
        page = doc[b["page"]]
        rect = fitz.Rect(b["rect"]) & page.rect
        if rect.is_empty or rect.is_infinite:
            continue
        page.add_redact_annot(rect, fill=fill)
        touched.add(b["page"])
    for pno in touched:
        doc[pno].apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)
    return doc


def render_page(doc, pno: int, zoom: float):
    """把已打码文档的第 pno 页渲染成 PIL.Image（人工复核用）。"""
    from PIL import Image
    pix = doc[pno].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def draw_outlines(img, blocks, pno: int, zoom: float, color=(30, 144, 255)):
    """在预览图上给本页打码区域画蓝框，便于确认位置。"""
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    width = max(2, int(round(zoom)))
    for b in blocks:
        if b["page"] != pno or not b.get("enabled", True):
            continue
        x0, y0, x1, y1 = [v * zoom for v in b["rect"]]
        for off in range(width):
            d.rectangle((x0 - off, y0 - off, x1 + off, y1 + off), outline=color)


def auto_output_path(src_path: str) -> str:
    """输出文件名：原名称 + 密文.pdf。"""
    stem = os.path.splitext(os.path.basename(src_path))[0]
    return f"{stem}密文.pdf"


if __name__ == "__main__":
    import sys
    print("OCR 引擎：", find_tesseract() or "未安装 tesseract（扫描件请手动框选）")
    for path in sys.argv[1:]:
        bs = find_candidates(path)
        print(f"{os.path.basename(path)}: 识别 {len(bs)} 处")
        for b in bs:
            print(f"  p{b['page'] + 1} [{b['source']}] {b['text']!r}")
        doc = open_masked(path, bs)
        out = os.path.join(os.path.dirname(os.path.abspath(path)), auto_output_path(path))
        doc.save(out, garbage=3, deflate=True)
        doc.close()
        print(f"  -> 已保存 {out}")
