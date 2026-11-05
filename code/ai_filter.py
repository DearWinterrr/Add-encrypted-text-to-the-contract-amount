# -*- coding: utf-8 -*-
"""AI 复核模块：调用千问（阿里云百炼 DashScope）大模型，对自动识别结果做二次筛选。

作用（在本地规则识别之后叠加一层 AI 判断）：
1. 排除误报 —— 电话号码、设备型号/规格、证件号、日期、数量单位、百分比等非金额候选，自动停用不打码
2. 补充漏报 —— 找出候选没盖住的金额/中文大写金额，自动定位加入打码清单

说明：
- 只发送“涉及候选的页面纯文本 + 候选内容”，手动条目不会被修改
- API Key 来源：环境变量 DASHSCOPE_API_KEY（或 QWEN_API_KEY），或界面「AI 设置」保存到 config.json
- 数据会发送到云端、按 token 计费；不用 AI 时本模块完全不参与，程序仍可离线运行
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

import pymupdf as fitz

API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_MODEL = "qwen-plus"
MODELS = ["qwen-plus", "qwen-turbo", "qwen-max"]
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

AUTO_SOURCES = ("自动", "关键词", "激进", "OCR")

SYSTEM_PROMPT = (
    "你是合同保密信息审核助手。用户会给出合同某一页的纯文本，和一组已被程序自动标记为“将要打码”"
    "的候选项（从 1 连续编号）。请逐项判断：\n"
    "一、skip（排除，不打码）：候选内容本质不是金额，包括——固定电话/手机号码、设备型号/规格/序列号/"
    "产品编号、身份证号、统一社会信用代码、银行账号、税号、发票号、日期/时间、纯数量与单位（如“20件”"
    "“3台”）、百分比/利率/税率、页码/条款号。\n"
    "二、keep（保留打码）：小写金额、带 ¥/￥/元的金额、含金额的“合同金额：xxx”整行、中文大写金额"
    "（如 人民币捌拾贰万伍仟元整）。\n"
    "三、extra（补充漏网金额）：从页文本中再找“未被候选覆盖的完整金额或大写金额片段”，原样摘录文本"
    "即可；没有就返回空数组；把握不准就不要列，宁可漏补也不要误伤。\n"
    "只输出一个 JSON 对象，不要任何解释或代码围栏，格式：\n"
    '{"keep":[1,3],"skip":[{"id":2,"reason":"手机号码"}],"extra":["人民币捌拾贰万伍仟元整"]}'
)


# ---------------- 配置 ----------------
def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(**kv) -> None:
    cfg = load_config()
    cfg.update({k: v for k, v in kv.items() if v is not None})
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def get_api_key() -> str:
    for env in ("DASHSCOPE_API_KEY", "QWEN_API_KEY", "MIWEN_API_KEY"):
        v = os.environ.get(env)
        if v and v.strip():
            return v.strip()
    return (load_config().get("api_key") or "").strip()


def get_model() -> str:
    return (load_config().get("model") or DEFAULT_MODEL).strip()


# ---------------- API 调用 ----------------
def _chat(messages, api_key: str, model: str, timeout: int = 90):
    """返回 (content, error)。"""
    payload = {"model": model, "messages": messages,
               "temperature": 0.1, "max_tokens": 1500}
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            obj = json.loads(r.read().decode("utf-8"))
        return obj["choices"][0]["message"]["content"], None
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:180]
        except Exception:
            pass
        return None, f"HTTP {e.code} {detail}"
    except Exception as e:  # URLError / 超时 / 返回结构异常
        return None, f"{type(e).__name__}: {e}"


def _extract_json(text: str):
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except Exception:
        return None


def _review_json(user_text: str, api_key: str, model: str):
    """调用模型并解析；认证错误立即返回，其余情况重试一次。返回 (data|None, error|None)。"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text}]
    err = None
    for attempt in (1, 2):
        content, err = _chat(messages, api_key, model)
        if content is None:
            if err and ("401" in err or "403" in err):
                return None, err
            if attempt == 1:
                time.sleep(1.5)
            continue
        data = _extract_json(content)
        if isinstance(data, dict) and ("skip" in data or "keep" in data or "extra" in data):
            return data, None
        messages.append({"role": "assistant", "content": content[:300]})
        messages.append({"role": "user", "content": "只输出规定格式的 JSON 对象，不要其他文字。"})
    return None, err or "模型返回不是合法 JSON"


# ---------------- 定位补充文本 ----------------
def _locate(doc, needle: str):
    """在已打开文档中按行定位 AI 摘录的金额文本。"""
    import redact_engine as eng
    clean = re.sub(r"\s+", "", needle)
    out = []
    if len(clean) < 2:
        return out
    for pno, page in enumerate(doc):
        for line in eng._page_lines(page):
            text = "".join(t for _, t in line)
            if clean in re.sub(r"\s+", "", text):
                rect = fitz.Rect()
                for r, _ in line:
                    rect |= r
                out.append({"page": pno,
                            "rect": eng._pad((rect.x0, rect.y0, rect.x1, rect.y1)),
                            "text": text[:40], "source": "AI补充", "enabled": True})
                break
    return out


# ---------------- 主入口 ----------------
def review_pdf(pdf_path: str, blocks: list[dict], api_key: str, model: str = DEFAULT_MODEL):
    """对一个文件的自动候选做 AI 复核。

    返回 (new_blocks, report)：
    - new_blocks：skip 命中的条目被置 enabled=False 并标注来源；extra 命中的金额作为
      新条目（来源“AI补充”）加入；原 blocks 不被修改（内部先深拷贝）。
    - report：{"skipped":[(文本,原因)], "added":[文本], "failed_pages":[页码], "error":str|None}
    """
    report: dict = {"skipped": [], "added": [], "failed_pages": [], "error": None}
    new_blocks = [dict(b) for b in blocks]

    by_page: dict[int, list[int]] = {}
    for gid, b in enumerate(new_blocks):
        if str(b.get("source", "")) in AUTO_SOURCES and b.get("enabled", True):
            by_page.setdefault(b["page"], []).append(gid)
    if not by_page:
        report["error"] = "没有可复核的自动候选（可能全是手动条目），已跳过。"
        return new_blocks, report

    import redact_engine as eng
    seen_keys = {(b["page"], tuple(round(v, 1) for v in b["rect"])) for b in new_blocks}
    doc = fitz.open(pdf_path)
    try:
        for pno in sorted(by_page):
            gids = by_page[pno]
            page = doc[pno]
            cand_lines = []
            for i, gid in enumerate(gids, 1):
                b = new_blocks[gid]
                clip = fitz.Rect(b["rect"]) & page.rect
                t = re.sub(r"\s+", "", page.get_textbox(clip)) or b.get("text", "")
                cand_lines.append(f"{i}. [{b['source']}] {t[:80]}")
            full = page.get_text()
            if len(full) > 9000:
                full = full[:9000] + "\n…（内容过长已截断）"
            user = (f"【第{pno + 1}页纯文本】\n{full}\n\n"
                    f"【候选项，编号1~{len(gids)}】\n" + "\n".join(cand_lines))

            data, err = _review_json(user, api_key, model)
            if data is None:
                if err and ("401" in err or "403" in err):
                    report["error"] = f"API Key 无效或无权限：{err}"
                    return new_blocks, report
                report["failed_pages"].append(pno + 1)
                continue

            for item in data.get("skip", []) or []:
                raw_id = item.get("id") if isinstance(item, dict) else item
                reason = (item.get("reason", "") if isinstance(item, dict) else "") or "非金额"
                try:
                    idx = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if 1 <= idx <= len(gids):
                    b = new_blocks[gids[idx - 1]]
                    b["enabled"] = False
                    b["source"] = f"AI排除·{str(reason)[:10]}"
                    report["skipped"].append((b.get("text", ""), str(reason)[:12]))

            for txt in data.get("extra", []) or []:
                if not isinstance(txt, str) or not txt.strip():
                    continue
                report["added"].append(txt.strip()[:40])
                for hit in _locate(doc, txt):
                    key = (hit["page"], tuple(round(v, 1) for v in hit["rect"]))
                    if key not in seen_keys:
                        seen_keys.add(key)
                        new_blocks.append(hit)
    finally:
        doc.close()

    new_blocks.sort(key=lambda b: (b["page"], b["rect"][1], b["rect"][0]))
    return new_blocks, report
