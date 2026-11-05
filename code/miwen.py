import re
import os
import fitz  # PyMuPDF
import pdfplumber
from pdf2image import convert_from_path
from PIL import Image
import pytesseract


def ocr_pdf(pdf_path: str, dpi: int = 200) -> list[dict]:
    """
    对 PDF 做 OCR，返回每一页的文字块：
    [{"page": 0, "text": "...", "bbox": (x0, y0, x1, y1)}, ...]
    """
    images = convert_from_path(pdf_path, dpi=dpi)
    results = []

    for page_idx, image in enumerate(images):
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
        n = len(data["text"])
        width, height = image.size

        for i in range(n):
            text = data["text"][i].strip()
            if not text:
                continue

            x = data["left"][i]
            y = data["top"][i]
            w = data["width"][i]
            h = data["height"][i]

            # 转换到 PDF 页面坐标
            page_w = fitz.open(pdf_path)[page_idx].rect.width
            page_h = fitz.open(pdf_path)[page_idx].rect.height

            scale_x = page_w / width
            scale_y = page_h / height

            x0 = x * scale_x
            y0 = y * scale_y
            x1 = (x + w) * scale_x
            y1 = (y + h) * scale_y

            results.append({
                "page": page_idx,
                "text": text,
                "bbox": (x0, y0, x1, y1),
            })

    return results


def find_amount_blocks(pdf_path: str, keywords=None, amount_patterns=None):
    """
    返回需要打码的区域：
    [{"page": 0, "bbox": (x0, y0, x1, y1), "matched_text": "..."}]
    """
    if keywords is None:
        keywords = [
            "合同金额",
            "总金额",
            "合计金额",
            "人民币",
            "小写金额",
            "含税金额",
            "不含税金额",
            "总价",
            "单价",
        ]

    if amount_patterns is None:
        amount_patterns = [
            # 合同金额：¥128,000.00
            r"(合同金额|总金额|合计金额|总价)\s*[:：]\s*¥?\s*\d[\d,.]*",
            # ¥128,000.00
            r"¥\s*\d[\d,.]*",
            # 128,000.00 元
            r"\d[\d,.]*\s*元",
            # 人民币壹拾贰万捌仟元整
            r"人民币[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343\u4e07\u4ebf\u96f6\u58f9\u8d30\u53c1\u8086\u4f0d\u9646\u67d2\u634c\u7396\u5eff\u5345\u5eff\u53c1\u56db\u4f0d\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343\u4e07\u4ebf\u5146]+元[\u6574\u6b63]?",
        ]

    redact_blocks = []

    # 优先用 pdfplumber 提取文字坐标，速度更快
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_idx, page in enumerate(pdf.pages):
                words = page.extract_words()
                if not words:
                    continue

                full_text = page.get_text()

                for pattern in amount_patterns:
                    for match in re.finditer(pattern, full_text):
                        matched = match.group()
                        x0 = match.start()
                        x1 = match.end()

                        # 粗略定位：找到包含该文本的单词区域
                        for word in words:
                            if matched in word["text"]:
                                redact_blocks.append({
                                    "page": page_idx,
                                    "bbox": (
                                        word["x0"],
                                        word["top"],
                                        word["x1"],
                                        word["bottom"],
                                    ),
                                    "matched_text": matched,
                                })

                # 再根据关键词扩展打码范围
                for kw in keywords:
                    if kw in full_text:
                        for word in words:
                            if kw in word["text"]:
                                # 把关键词后面一段也遮住
                                redact_blocks.append({
                                    "page": page_idx,
                                    "bbox": (
                                        word["x0"],
                                        word["top"],
                                        min(word["x0"] + 300, page.width),
                                        word["bottom"],
                                    ),
                                    "matched_text": word["text"],
                                })

        if redact_blocks:
            return redact_blocks

    except Exception:
        pass

    # 如果 pdfplumber 找不到，再尝试 OCR
    ocr_results = ocr_pdf(pdf_path)
    full_text_by_page = {}
    for item in ocr_results:
        full_text_by_page.setdefault(item["page"], "")
        full_text_by_page[item["page"]] += item["text"] + " "

    for page_idx, text in full_text_by_page.items():
        for pattern in amount_patterns:
            for match in re.finditer(pattern, text):
                matched = match.group()
                for item in ocr_results:
                    if item["page"] == page_idx and matched in item["text"]:
                        redact_blocks.append({
                            "page": page_idx,
                            "bbox": item["bbox"],
                            "matched_text": matched,
                        })

    return redact_blocks


def redact_pdf(input_path: str, output_path: str, blocks: list[dict]):
    """
    在 PDF 上绘制红色遮挡块，并删除底层文字。
    """
    doc = fitz.open(input_path)

    for block in blocks:
        page = doc[block["page"]]
        rect = fitz.Rect(block["bbox"])

        # 稍微扩大一点，避免遮挡不全
        rect = rect + fitz.Rect(-2, -2, 2, 2)

        # 绘制红色遮挡块
        page.draw_rect(
            rect,
            color=(1, 0, 0),
            fill=(1, 0, 0),
            width=0,
        )

        # 删除遮挡区域内的文字，防止被复制
        page.redact_annotated()
        page.apply_redactions()

    doc.save(output_path)
    doc.close()


def process_contract(input_path: str, output_dir: str = "./output"):
    os.makedirs(output_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    output_path = os.path.join(output_dir, f"{base_name}_已打码.pdf")

    print(f"正在处理：{input_path}")
    blocks = find_amount_blocks(input_path)

    if not blocks:
        print("未找到需要打码的金额内容。")
        return output_path

    print(f"找到 {len(blocks)} 处待打码区域：")
    for b in blocks:
        print(f"  第 {b['page'] + 1} 页：{b['matched_text']}")

    redact_pdf(input_path, output_path, blocks)
    print(f"已保存：{output_path}")
    return output_path


if __name__ == "__main__":
    # 单个文件处理
    process_contract("合同.pdf")

    # 如果要批量处理整个文件夹：
    # input_folder = "./contracts"
    # for name in os.listdir(input_folder):
    #     if name.lower().endswith(".pdf"):
    #         process_contract(os.path.join(input_folder, name))