# -*- coding: utf-8 -*-
"""合同打码可视化工具（Tkinter 界面）。

运行：  python miwen_gui.py
流程：  添加多个 PDF → 批量识别 →（可选）AI 复核筛选 → 逐个文件复核
        （手动补打/停用，结果按文件保留）→ 批量导出“原名称密文.pdf”
        也可对单个文件「识别当前」后「另存为」。

依赖：PyMuPDF、Pillow。扫描件 OCR 需系统安装 tesseract（未装则拖框补充）。
AI 复核（可选）：阿里云百炼 DashScope（千问）API，需在「AI 设置」填 DASHSCOPE_API_KEY。
"""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pymupdf as fitz
from PIL import ImageTk

import ai_filter
import redact_engine as eng

ZOOM_MIN, ZOOM_MAX = 0.8, 4.0


class RedactApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("合同金额打码工具")
        root.geometry("1280x800")

        self.src_path = ""
        self.blocks = []
        # 每个文件独立保存：{"blocks": [...], "page": int, "recognized": bool, "edited": bool}
        self.file_paths: list[str] = []
        self.file_data: dict[str, dict] = {}
        self.masked_doc = None
        self.zoom = 1.6
        self.page = 0
        self.tk_img = None
        self.drag_start = None
        self.auto_mode = True
        self._img_height = 0
        self.tasks = queue.Queue()
        self.busy = False
        self._pending = 0
        self._detect_mode = "single"
        self._batch_errors: list[str] = []
        self.has_text_layer = True
        self._export_results: list = []
        self._detected_ok: list[str] = []
        self._ai_pending: list[str] = []
        self._ai_key = ""
        self._ai_manual = False

        self._build_ui()
        tess = eng.find_tesseract()
        self.status.set("OCR：" + (os.path.basename(tess) if tess
                                   else "未检测到 tesseract，扫描件请用拖框手动补打码"))
        self.root.after(120, self._poll_tasks)

    # ================= UI =================
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")
        ttk.Button(top, text="添加 PDF", command=self.add_files).pack(side="left")
        ttk.Button(top, text="移除选中", command=self.remove_selected).pack(side="left", padx=6)
        ttk.Button(top, text="清空列表", command=self.clear_files).pack(side="left")

        ttk.Label(top, text="打码颜色").pack(side="left", padx=(20, 4))
        self.color_var = tk.StringVar(value="黑色")
        ttk.Combobox(top, textvariable=self.color_var, state="readonly", width=5,
                     values=list(eng.FILL_COLORS)).pack(side="left")

        self.aggr_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="宁可多遮", variable=self.aggr_var).pack(side="left", padx=(14, 0))

        ttk.Button(top, text="识别当前", command=self.start_redact).pack(side="left", padx=(14, 0))
        ttk.Button(top, text="批量识别", command=self.batch_detect).pack(side="left", padx=6)

        self.ai_var = tk.BooleanVar(value=bool(ai_filter.load_config().get("ai_auto", False)))
        ttk.Checkbutton(top, text="识别后自动AI复核", variable=self.ai_var).pack(side="left", padx=(10, 0))
        ttk.Button(top, text="AI复核当前", command=self.ai_review_current).pack(side="left", padx=6)
        ttk.Button(top, text="AI设置", command=self.open_ai_settings).pack(side="left")

        ttk.Button(top, text="批量导出", command=self.batch_export).pack(side="left", padx=6)

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # 左：文件列表 + 待打码清单
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 8))
        ttk.Label(left, text="文件列表（✔已识别 *含手动编辑）").pack(anchor="w")
        self.file_list = tk.Listbox(left, height=6, width=38, exportselection=False)
        self.file_list.pack(fill="x")
        self.file_list.bind("<<ListboxSelect>>", self.on_file_select)
        self.file_list.bind("<Double-1>", self.on_file_double)

        ttk.Label(left, text="待打码条目（双击=启用/停用，选中可删除）").pack(anchor="w", pady=(8, 2))
        self.tree = ttk.Treeview(left, columns=("page", "text", "src"),
                                 show="tree headings", height=13)
        self.tree.heading("#0", text="✓")
        self.tree.column("#0", width=34, anchor="center", stretch=False)
        for name, head, w in (("page", "页", 40), ("text", "内容", 130), ("src", "来源", 66)):
            self.tree.heading(name, text=head)
            self.tree.column(name, width=w, anchor="w", stretch=False)
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(fill="both", expand=True, side="left")
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.toggle_row)
        self.tree.bind("<<TreeviewSelect>>", self.jump_to_row)
        self.tree.tag_configure("off", foreground="#999999")

        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="删除选中条目", command=self.delete_rows).pack(side="left")
        ttk.Button(btns, text="全部停用", command=self.disable_all).pack(side="left", padx=6)
        ttk.Button(btns, text="全部启用", command=self.enable_all).pack(side="left")

        # 中：预览
        mid = ttk.Frame(body)
        mid.grid(row=0, column=1, sticky="nsew")
        bar = ttk.Frame(mid)
        bar.pack(fill="x")
        ttk.Button(bar, text="◀ 上一页", command=lambda: self.goto_page(-1)).pack(side="left")
        self.page_var = tk.StringVar(value="0 / 0")
        ttk.Label(bar, textvariable=self.page_var, width=10, anchor="center").pack(side="left")
        ttk.Button(bar, text="下一页 ▶", command=lambda: self.goto_page(1)).pack(side="left")
        ttk.Button(bar, text="缩小", command=lambda: self.set_zoom(-0.3)).pack(side="left", padx=(12, 2))
        ttk.Button(bar, text="放大", command=lambda: self.set_zoom(0.3)).pack(side="left")
        ttk.Button(bar, text="另存为「原名称密文.pdf」", command=self.save_as).pack(side="right")

        wrap = ttk.Frame(mid)
        wrap.pack(fill="both", expand=True, pady=6)
        self.canvas = tk.Canvas(wrap, bg="#808080", highlightthickness=0, cursor="cross")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        ttk.Label(mid, foreground="#2b6cb0",
                  text="Ctrl+滚轮=以鼠标为中心缩放；滚轮=翻页滚动；拖框补打漏掉的金额。").pack(anchor="w")

        # 右：手动添加
        right = ttk.LabelFrame(body, text="手动添加密文", padding=8)
        right.grid(row=0, column=2, sticky="ns", padx=(8, 0))
        ttk.Label(right, text="① 输入要遮蔽的文字").pack(anchor="w")
        self.text_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.text_var, width=26).pack(fill="x", pady=(2, 4))
        ttk.Button(right, text="按文字添加", command=self.add_by_text).pack(fill="x")

        ttk.Label(right, text="② 指定页码 + 区域坐标").pack(anchor="w", pady=(12, 2))
        ttk.Label(right, foreground="#666666", text="页码：").pack(anchor="w")
        self.page_var2 = tk.StringVar()
        ttk.Entry(right, textvariable=self.page_var2, width=8).pack(anchor="w")
        ttk.Label(right, foreground="#666666", text="区域 x0,y0,x1,y1：").pack(anchor="w")
        self.rect_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.rect_var, width=26).pack(anchor="w", pady=(2, 4))
        ttk.Button(right, text="按区域添加", command=self.add_by_rect).pack(fill="x")

        ttk.Label(right, foreground="#666666", justify="left", wraplength=200,
                  text="拖框后坐标会自动填到 ②，可微调数值再点「按区域添加」。").pack(anchor="w", pady=(10, 0))

        self.status = tk.StringVar(value="请先添加 PDF 文件；添加多个后可「批量识别」。")
        ttk.Label(self.root, textvariable=self.status, anchor="w", relief="groove",
                  padding=(8, 4)).pack(fill="x")

    # ================= 文件列表（按文件独立保存结果） =================
    def add_files(self):
        paths = filedialog.askopenfilenames(title="选择合同 PDF",
                                            filetypes=[("PDF 文件", "*.pdf")])
        added = False
        for p in paths:
            if p not in self.file_paths:
                self.file_paths.append(p)
                added = True
        self._refresh_file_list()
        if added and not self.src_path and self.file_paths:
            self.load_file(self.file_paths[0])
        elif added:
            self.status.set(f"文件列表共 {len(self.file_paths)} 个，可点「批量识别」一次识别全部。")

    def remove_selected(self):
        for i in reversed(self.file_list.curselection()):
            p = self.file_paths.pop(i)
            self.file_data.pop(p, None)
        self._refresh_file_list()

    def clear_files(self):
        self._stash()
        self.file_paths = []
        self.file_data = {}
        self._refresh_file_list()

    def on_file_select(self, _e):
        sel = self.file_list.curselection()
        if sel and sel[0] < len(self.file_paths):
            self.load_file(self.file_paths[sel[0]])

    def on_file_double(self, _e):
        sel = self.file_list.curselection()
        if not sel or sel[0] >= len(self.file_paths):
            return
        p = self.file_paths[sel[0]]
        if p != self.src_path:
            self.load_file(p)
        d = self.file_data.get(p, {})
        if not d.get("recognized") and not self.busy:
            self.start_redact()

    def _refresh_file_list(self):
        self.file_list.delete(0, "end")
        for p in self.file_paths:
            d = self.file_data.get(p, {})
            en = len([b for d_ in [d] for b in d_.get("blocks", []) if b.get("enabled", True)])
            if d.get("recognized"):
                ai = "AI" if d.get("ai_reviewed") else "  "
                mark = f"✔ {en:>3}处 {ai}" + (" *" if d.get("edited") else "  ")
            else:
                mark = "· 未识别"
            self.file_list.insert("end", f"{mark}  {os.path.basename(p)}")
        idx = self._current_index()
        if idx >= 0:
            self.file_list.selection_clear(0, "end")
            self.file_list.selection_set(idx)
            self.file_list.see(idx)

    def _current_index(self):
        try:
            return self.file_paths.index(self.src_path)
        except ValueError:
            return -1

    def _stash(self):
        """把当前文件的清单/页码存回 per-file 数据，防止切换文件时丢失。"""
        if self.src_path:
            d = self.file_data.setdefault(self.src_path, {"recognized": False})
            d["blocks"] = self.blocks
            d["page"] = self.page
            if not self.auto_mode:
                d["edited"] = True

    def _mark_edited(self):
        self.auto_mode = False
        if self.src_path:
            self.file_data.setdefault(self.src_path, {"recognized": False})["edited"] = True
        self._refresh_file_list()

    def load_file(self, path):
        if path == self.src_path:
            return
        self._stash()
        self.src_path = path
        d = self.file_data.get(path)
        if d:
            self.blocks = d.get("blocks", [])
            self.page = d.get("page", 0)
            self.auto_mode = not d.get("edited", False)
        else:
            self.blocks = []
            self.page = 0
            self.auto_mode = True
        self._close_masked()
        self._rebuild_masked()
        self._refresh_tree()
        self._render()
        try:
            self.has_text_layer = not eng.textless_pages(path)
        except Exception:
            self.has_text_layer = True
        d = self.file_data.get(path, {})
        if d.get("recognized"):
            note = "（含手动编辑）" if d.get("edited") else ""
            n = len([b for b in d["blocks"] if b.get("enabled", True)])
            self.status.set(f"已切换：{os.path.basename(path)}　已有 {n} 处启用密文{note}，可继续复核或直接批量导出。")
        else:
            tip = "" if self.has_text_layer else "（含扫描件页面）"
            self.status.set(f"已打开：{os.path.basename(path)}　点「识别当前」或「批量识别」。{tip}")

    # ================= 识别 =================
    def start_redact(self):
        if not self.src_path:
            messagebox.showinfo("提示", "请先添加并选择一个 PDF 文件。")
            return
        if self.busy:
            self.status.set("正在处理，请稍候…")
            return
        d = self.file_data.get(self.src_path, {})
        if d.get("recognized") and d.get("edited"):
            if not messagebox.askyesno("覆盖确认",
                                      "该文件已有手动编辑的密文，重新识别会丢弃这些编辑。继续？"):
                return
        self.busy = True
        self._pending = 1
        self._detect_mode = "single"
        self._detected_ok = []
        self.status.set(f"正在识别：{os.path.basename(self.src_path)} …")
        threading.Thread(target=self._detect_worker,
                         args=(self.src_path, self.aggr_var.get()), daemon=True).start()

    def batch_detect(self):
        if self.busy:
            self.status.set("正在处理，请稍候…")
            return
        if not self.file_paths:
            messagebox.showinfo("提示", "文件列表为空，请先「添加 PDF」。")
            return
        self._stash()
        todo = [p for p in self.file_paths
                if not self.file_data.get(p, {}).get("recognized")]
        skipped = len(self.file_paths) - len(todo)
        if not todo:
            self.status.set(f"全部 {skipped} 个文件都已识别，可逐个复核后「批量导出」。")
            return
        self.busy = True
        self._pending = len(todo)
        self._detect_mode = "batch"
        self._batch_errors = []
        self._detected_ok = []
        extra = f"，跳过已识别的 {skipped} 个" if skipped else ""
        self.status.set(f"批量识别 {len(todo)} 个文件{extra} …")
        threading.Thread(target=self._detect_multi_worker,
                         args=(todo, self.aggr_var.get()), daemon=True).start()

    def _detect_worker(self, path, aggr):
        try:
            found = eng.find_candidates(path, aggressive=aggr)
            self.tasks.put(("detected", path, found, None))
        except Exception as exc:  # noqa: BLE001
            self.tasks.put(("detected", path, [], f"{type(exc).__name__}: {exc}"))

    def _detect_multi_worker(self, paths, aggr):
        total = len(paths)
        for i, p in enumerate(paths, 1):
            self.tasks.put(("progress", f"批量识别 {i}/{total}：{os.path.basename(p)}"))
            try:
                found = eng.find_candidates(p, aggressive=aggr)
                self.tasks.put(("detected", p, found, None))
            except Exception as exc:  # noqa: BLE001
                self.tasks.put(("detected", p, [], f"{type(exc).__name__}: {exc}"))

    def _on_detected(self, path, found, err):
        self._pending = max(0, self._pending - 1)
        if err:
            self._batch_errors.append(f"{os.path.basename(path)}：{err}")
            if path == self.src_path:
                messagebox.showerror("识别失败", err)
            if self._pending == 0:
                self.busy = False
            return
        d = self.file_data.setdefault(path, {})
        d["blocks"] = found
        d["recognized"] = True
        d["edited"] = False
        d.setdefault("page", 0)
        if path not in self._detected_ok:
            self._detected_ok.append(path)
        if path == self.src_path:
            self.blocks = found
            self.auto_mode = True
            self._refresh_tree()
            self._rebuild_masked()
            self._render()
        if self._pending == 0:
            self.busy = False
            self._refresh_file_list()
            n_all = len(self.file_paths)
            ok = sum(1 for p in self.file_paths
                     if self.file_data.get(p, {}).get("recognized"))
            total_blocks = sum(len(v.get("blocks", [])) for v in self.file_data.values())
            if self._detect_mode == "batch":
                self.status.set(f"批量识别完成：{ok}/{n_all} 个文件已识别，共 {total_blocks} 处候选。"
                                + ("有失败文件，见弹窗。" if self._batch_errors else "逐个点文件复核，然后「批量导出」。"))
                if self._batch_errors:
                    messagebox.showwarning("部分文件识别失败", "\n".join(self._batch_errors[:8]))
            else:
                if not found:
                    self.status.set(f"{os.path.basename(path)}：没识别到金额，请拖框手动补打码。")
                else:
                    self.status.set(f"{os.path.basename(path)}：识别 {len(found)} 处，请复核。")
            if self.ai_var.get():
                key = ai_filter.get_api_key()
                if key:
                    todo = [p for p in self._detected_ok if p in self.file_paths]
                    if todo:
                        self._start_ai(todo, key, manual=False)
                else:
                    self.status.set(self.status.get() + "　⚠已勾选自动AI复核但未配置 API Key，已跳过。")
            self._detected_ok = []
        else:
            self._refresh_file_list()

    # ================= AI 复核 =================
    def _check_api_key(self) -> str | None:
        key = ai_filter.get_api_key()
        if not key:
            if messagebox.askyesno("需要 API Key",
                                  "还没有配置千问（DashScope）API Key。\n\n"
                                  "现在打开「AI 设置」填写吗？\n"
                                  "（Key 在 https://bailian.console.aliyun.com 免费申请，sk- 开头）"):
                self.open_ai_settings()
            key = ai_filter.get_api_key()
        return key or None

    def ai_review_current(self):
        if self.busy:
            self.status.set("正在处理，请稍候…")
            return
        if not self.src_path:
            messagebox.showinfo("提示", "请先添加并选择一个 PDF 文件。")
            return
        self._stash()
        d = self.file_data.get(self.src_path)
        if not d or not d.get("blocks"):
            messagebox.showinfo("提示", "当前文件还没有识别结果，请先「识别当前」。")
            return
        key = self._check_api_key()
        if not key:
            return
        self._start_ai([self.src_path], key, manual=True)

    def _start_ai(self, paths, key, manual=False):
        """把待复核文件排入串行队列（手动按钮与识别后自动复核都走这里）。"""
        todo = [p for p in paths if self.file_data.get(p, {}).get("blocks")]
        if not todo:
            if manual:
                self.status.set("没有可复核的识别结果。")
            return
        self._ai_pending = todo
        self._ai_key = key
        self._ai_manual = manual
        self.busy = True
        self.status.set(f"AI 复核 {len(todo)} 个文件（{ai_filter.get_model()}）…"
                        "涉及页面文本将发送到云端")
        threading.Thread(target=self._ai_worker,
                         args=(list(todo), key, ai_filter.get_model()), daemon=True).start()

    def _ai_worker(self, paths, key, model):
        total = len(paths)
        for i, p in enumerate(paths, 1):
            self.tasks.put(("progress", f"AI 复核 {i}/{total}：{os.path.basename(p)}"))
            blocks = list(self.file_data.get(p, {}).get("blocks", []))
            try:
                new_blocks, report = ai_filter.review_pdf(p, blocks, key, model)
                self.tasks.put(("ai_done", p, new_blocks, report))
            except Exception as exc:  # noqa: BLE001
                self.tasks.put(("ai_done", p, None,
                                {"error": f"{type(exc).__name__}: {exc}"}))

    def _on_ai_done(self, path, new_blocks, report):
        if self._ai_pending and self._ai_pending[0] == path:
            self._ai_pending.pop(0)
        last = not self._ai_pending
        err = report.get("error") or "" if report else ""
        if new_blocks is None and ("401" in err or "403" in err):
            self.busy = False
            self._ai_pending = []
            self.status.set(f"AI 复核中止：{err[:150]}")
            messagebox.showerror("API Key 无效", err[:600])
            return
        if new_blocks is not None:
            d = self.file_data.setdefault(path, {})
            d["blocks"] = new_blocks
            d["recognized"] = True
            d["ai_reviewed"] = True
            if path == self.src_path:
                self.blocks = new_blocks
                self._mark_edited()
                self._refresh_tree()
                self._rebuild_masked()
                self._render()
            self._refresh_file_list()
            self._log_ai_report(path, report, manual=self._ai_manual)
        else:
            self.status.set(f"AI 复核失败：{os.path.basename(path)}　{err[:120]}")
            if self._ai_manual and last:
                messagebox.showerror("AI 复核失败", err[:600] or "未知错误")
        if last:
            self.busy = False
        return last

    def _log_ai_report(self, path, report, manual=False):
        parts = []
        if report.get("skipped"):
            parts.append(f"排除误报 {len(report['skipped'])} 处")
        if report.get("added"):
            parts.append(f"补充漏打 {len(report['added'])} 处")
        if report.get("failed_pages"):
            parts.append(f"第{'、'.join(map(str, report['failed_pages']))}页失败")
        if report.get("error"):
            parts.append(f"⚠{str(report['error'])[:60]}")
        msg = f"AI复核 {os.path.basename(path)}：" + ("，".join(parts) or "无调整")
        if report.get("skipped"):
            msg += "\n排除：" + "；".join(f"{(t or '')[:12]}({r})"
                                        for t, r in report["skipped"][:6])
        if report.get("added"):
            msg += "\n补充：" + "；".join(t[:12] for t in report["added"][:6])
        if manual and (report.get("skipped") or report.get("added")):
            messagebox.showinfo("AI 复核结果", msg)
        self.status.set(msg.replace("\n", "　"))

    def open_ai_settings(self):
        cfg = ai_filter.load_config()
        dlg = tk.Toplevel(self.root)
        dlg.title("AI 复核设置（千问 / DashScope）")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()
        pad = dict(padx=12, pady=4)

        ttk.Label(dlg, text="API Key（阿里云百炼控制台申请，sk- 开头）",
                  ).pack(anchor="w", **pad)
        key_var = tk.StringVar(value=cfg.get("api_key", "") or ai_filter.get_api_key())
        ent = ttk.Entry(dlg, textvariable=key_var, width=52, show="*")
        ent.pack(fill="x", **pad)
        show_var = tk.BooleanVar(value=False)

        def _toggle():
            ent.configure(show="" if show_var.get() else "*")
        ttk.Checkbutton(dlg, text="显示 Key", variable=show_var,
                        command=_toggle).pack(anchor="w", **pad)

        ttk.Label(dlg, text="模型").pack(anchor="w", **pad)
        model_var = tk.StringVar(value=cfg.get("model", ai_filter.DEFAULT_MODEL))
        ttk.Combobox(dlg, textvariable=model_var, state="readonly", width=16,
                     values=ai_filter.MODELS).pack(anchor="w", **pad)

        ttk.Label(dlg, foreground="#b45309", justify="left", wraplength=430,
                  text="提示：开启后，涉及候选项的页面文本会发送到阿里云千问 API 做二次判断，"
                       "合同内容将离开本机；按 token 计费，百炼新用户有免费额度。").pack(anchor="w", **pad)

        row = ttk.Frame(dlg)
        row.pack(pady=8)

        def _save():
            ai_filter.save_config(api_key=key_var.get().strip() or None,
                                  model=model_var.get())
            self.ai_var.set(True)
            dlg.destroy()
            self.status.set("AI 设置已保存（config.json），可点「AI复核当前」试用。")

        def _test():
            key = key_var.get().strip()
            if not key:
                messagebox.showwarning("测试", "先填 API Key。", parent=dlg)
                return
            btn_test.configure(state="disabled", text="测试中…")

            def _w():
                try:
                    content, err = ai_filter._chat(
                        [{"role": "user", "content": "只回复两个字：可用"}],
                        key, model_var.get(), timeout=30)
                    ok = content is not None and "可用" in content
                    msg = "连接成功，模型可用。" if ok else f"失败：{err or content!r}"
                except Exception as exc:  # noqa: BLE001
                    msg = f"异常：{exc}"
                dlg.after(0, lambda: (btn_test.configure(state="normal", text="测试连接"),
                                      messagebox.showinfo("测试连接", msg, parent=dlg)))
            threading.Thread(target=_w, daemon=True).start()

        btn_test = ttk.Button(row, text="测试连接", command=_test)
        btn_test.pack(side="left", padx=6)
        ttk.Button(row, text="保存", command=_save).pack(side="left", padx=6)
        ttk.Button(row, text="取消", command=dlg.destroy).pack(side="left", padx=6)

    # ================= 清单 =================
    def _refresh_tree(self):
        sel = {int(i) for i in self.tree.selection()}
        self.tree.delete(*self.tree.get_children())
        for i, b in enumerate(self.blocks):
            mark = "✓" if b.get("enabled", True) else "—"
            self.tree.insert("", "end", iid=str(i), text=mark,
                             values=(b["page"] + 1, (b["text"] or "（框选区域）")[:22],
                                     b["source"]),
                             tags=("off",) if mark == "—" else ())
        for i in sel:
            if i < len(self.blocks):
                self.tree.selection_set(str(i))

    def toggle_row(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        b = self.blocks[int(iid)]
        b["enabled"] = not b.get("enabled", True)
        self._mark_edited()
        self._refresh_tree()
        self._rebuild_masked()
        self._render()

    def delete_rows(self):
        for iid in self.tree.selection():
            self.blocks.pop(int(iid))
        self._resort()
        self._mark_edited()
        self._refresh_tree()
        self._rebuild_masked()
        self._render()

    def disable_all(self):
        for b in self.blocks:
            b["enabled"] = False
        self._mark_edited()
        self._refresh_tree()
        self._rebuild_masked()
        self._render()

    def enable_all(self):
        for b in self.blocks:
            b["enabled"] = True
        self._mark_edited()
        self._refresh_tree()
        self._rebuild_masked()
        self._render()

    def _resort(self):
        self.blocks.sort(key=lambda b: (b["page"], b["rect"][1], b["rect"][0]))

    def jump_to_row(self, _e):
        sel = self.tree.selection()
        if not sel:
            return
        b = self.blocks[int(sel[0])]
        if b["page"] != self.page:
            self.page = b["page"]
            self._render()

    # ================= 手动添加 =================
    def add_block(self, page, rect, label, source):
        self.blocks.append({"page": page, "rect": tuple(round(float(v), 1) for v in rect),
                            "text": label, "source": source, "enabled": True})
        self._resort()
        self._mark_edited()
        self._refresh_tree()
        self._rebuild_masked()
        self._render()
        return len(self.blocks) - 1

    def add_by_text(self):
        needle = self.text_var.get().strip()
        if not self.src_path:
            self.status.set("请先选择 PDF 文件。")
            return
        if not needle:
            self.status.set("① 里要先输入要遮蔽的文字。")
            return
        found = eng.search_text_rects(self.src_path, needle)
        if not found:
            self.status.set(f"没找到「{needle}」；这份 PDF 可能没有文字层，请改用拖框。")
            return
        for b in found:
            self.blocks.append(b)
        self._resort()
        self._mark_edited()
        self._refresh_tree()
        self._rebuild_masked()
        self.page = found[0]["page"]
        self._render()
        self.status.set(f"按文字新增 {len(found)} 处：{needle}")

    def add_by_rect(self):
        if not self.src_path:
            self.status.set("请先选择 PDF 文件。")
            return
        try:
            page = int(self.page_var2.get().strip()) - 1
            vals = [float(v) for v in
                    self.rect_var.get().replace("，", ",").replace(" ", "").split(",")]
        except ValueError:
            self.status.set("请填页码，以及 x0,y0,x1,y1 四个数字（英文逗号分隔）。")
            return
        if len(vals) != 4 or page < 0 or vals[2] <= vals[0] or vals[3] <= vals[1]:
            self.status.set("区域应为 x0,y0,x1,y1 且 x1>x0、y1>y0。")
            return
        idx = self.add_block(page, vals, "（手动区域）", "手动·区域")
        self._select_row(idx)
        self.status.set(f"已加入第 {page + 1} 页的手动区域（清单第 {idx + 1} 条）。")

    def _select_row(self, idx):
        if idx < len(self.blocks):
            self.tree.selection_set(str(idx))
            self.tree.see(str(idx))

    # ================= 拖框 =================
    def _canvas_point(self, event):
        """屏幕事件坐标 → 画布坐标（含滚动量），放大滚动后也不偏移。"""
        return self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)

    def on_press(self, event):
        if self.src_path:
            self.drag_start = self._canvas_point(event)

    def on_drag(self, event):
        if not self.drag_start:
            return
        x0, y0 = self.drag_start
        x1, y1 = self._canvas_point(event)
        self.canvas.delete("rubber")
        self.canvas.create_rectangle(x0, y0, x1, y1,
                                     outline="#1e90ff", width=2, dash=(4, 3), tags="rubber")

    def on_release(self, event):
        if not self.src_path:
            self.drag_start = None
            return
        start = self.drag_start
        self.canvas.delete("rubber")
        self.drag_start = None
        if not start:
            return
        x0, y0 = start
        x1, y1 = self._canvas_point(event)
        if abs(x1 - x0) < 4 or abs(y1 - y0) < 4:
            return
        rect = [min(x0, x1) / self.zoom, min(y0, y1) / self.zoom,
                max(x0, x1) / self.zoom, max(y0, y1) / self.zoom]
        self.rect_var.set(", ".join(f"{v:.1f}" for v in rect))
        self.page_var2.set(str(self.page + 1))
        if messagebox.askyesno("补充密文",
                               f"把第 {self.page + 1} 页的框选区域加入待打码清单？\n\n"
                               f"坐标：{', '.join(f'{v:.0f}' for v in rect)}"):
            idx = self.add_block(self.page, rect, "（框选区域）", "手动·框选")
            self._select_row(idx)
            self.status.set(f"框选已加入（第 {self.page + 1} 页，清单第 {idx + 1} 条）。")

    def on_wheel(self, event):
        if event.state & 0x0004:          # 按住 Ctrl = 缩放，否则滚动
            frac = self._view_fraction(event)
            self.set_zoom(0.2 if event.delta > 0 else -0.2, anchor=frac)
        else:
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _view_fraction(self, event):
        """记下鼠标处在页面的相对高度，缩放后保持该点不跑偏。"""
        top = self.canvas.canvasy(0)
        height = max(1, self._img_height)
        return min(1.0, max(0.0, (top + event.y) / height))

    # ================= 预览 =================
    def goto_page(self, step):
        total = self._page_count()
        if not total:
            return
        self.page = max(0, min(total - 1, self.page + step))
        self._render()

    def set_zoom(self, delta, anchor=None):
        old = self.zoom
        self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, round(self.zoom + delta, 2)))
        if self.zoom == old:
            return
        keep = anchor if anchor is not None else self._viewport_center_fraction()
        self._render()
        if anchor is not None and self._img_height:
            view = max(1, self.canvas.winfo_height())
            top = max(0.0, keep * self._img_height - view / 2.0)
            self.canvas.yview_moveto(min(1.0, top / self._img_height))

    def _viewport_center_fraction(self):
        if not self._img_height:
            return 0.0
        top = self.canvas.canvasy(0)
        view = max(1, self.canvas.winfo_height())
        return min(1.0, max(0.0, (top + view / 2.0) / self._img_height))

    def _page_count(self):
        if self.masked_doc is not None:
            return self.masked_doc.page_count
        if self.src_path and os.path.exists(self.src_path):
            d = fitz.open(self.src_path)
            n = d.page_count
            d.close()
            return n
        return 0

    def _close_masked(self):
        if self.masked_doc is not None:
            try:
                self.masked_doc.close()
            except Exception:
                pass
            self.masked_doc = None

    def _rebuild_masked(self):
        if not self.src_path:
            return
        try:
            self._close_masked()
            self.masked_doc = eng.open_masked(self.src_path, self.blocks, self.color_var.get())
        except Exception as exc:  # noqa: BLE001
            self.masked_doc = None
            self.status.set(f"预览生成失败：{exc}")

    def _render(self):
        if not self.src_path:
            self.canvas.delete("all")
            self.page_var.set("0 / 0")
            return
        if self.masked_doc is None:
            self._rebuild_masked()
        if self.masked_doc is None:
            return
        try:
            total = self.masked_doc.page_count
            self.page = max(0, min(self.page, total - 1))
            img = eng.render_page(self.masked_doc, self.page, self.zoom)
            if self.auto_mode:
                eng.draw_outlines(img, self.blocks, self.page, self.zoom)
            self.tk_img = ImageTk.PhotoImage(img)
            self._img_height = img.height
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor="nw", image=self.tk_img)
            self.canvas.configure(scrollregion=(0, 0, img.width, img.height))
            self.page_var.set(f"{self.page + 1} / {total}")
        except Exception as exc:  # noqa: BLE001
            self.status.set(f"预览失败：{exc}")

    # ================= 导出 =================
    def save_as(self):
        if not self.src_path:
            messagebox.showinfo("提示", "请先添加 PDF 文件。")
            return
        self._rebuild_masked()
        if self.masked_doc is None:
            return
        enabled = [b for b in self.blocks if b.get("enabled", True)]
        if not enabled and not messagebox.askyesno("确认", "当前没有启用任何密文区域，仍要另存？"):
            return
        out = filedialog.asksaveasfilename(
            title="另存为打码后的 PDF", defaultextension=".pdf",
            initialdir=os.path.dirname(self.src_path),
            initialfile=eng.auto_output_path(self.src_path),
            filetypes=[("PDF 文件", "*.pdf")])
        if not out:
            self.status.set("已取消另存。")
            return
        try:
            self.masked_doc.save(out, garbage=3, deflate=True)
        except PermissionError:
            messagebox.showerror("保存失败", "目标文件正被其他程序占用（例如已打开的阅读器），请换个文件名。")
            return
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc))
            return
        self.status.set(f"已另存：{out}")
        messagebox.showinfo("完成", f"已输出打码文件：\n{out}\n\n共 {len(enabled)} 处密文。")

    def batch_export(self):
        if self.busy:
            self.status.set("正在处理，请稍候…")
            return
        if not self.file_paths:
            messagebox.showinfo("提示", "文件列表为空。")
            return
        self._stash()
        snap, skipped = [], []
        for p in self.file_paths:
            d = self.file_data.get(p, {})
            blocks = list(d.get("blocks", []))
            if not blocks:
                skipped.append(os.path.basename(p))
                continue
            snap.append((p, blocks, bool(d.get("recognized"))))
        if not snap:
            messagebox.showinfo("批量导出", "还没有任何识别结果，请先「批量识别」。")
            return
        msg = f"将按每个文件当前的清单（含手动补充与停用状态）导出 {len(snap)} 个文件。"
        if skipped:
            msg += f"\n\n跳过未识别文件 {len(skipped)} 个：{', '.join(skipped[:5])}"
            if len(skipped) > 5:
                msg += " …"
        if not messagebox.askyesno("批量导出确认", msg + "\n\n选择输出目录后开始导出？"):
            return
        out_dir = filedialog.askdirectory(title="选择批量输出目录")
        if not out_dir:
            self.status.set("已取消批量导出。")
            return
        self.busy = True
        self.status.set(f"批量导出 {len(snap)} 个文件…")
        self._export_results = []
        threading.Thread(target=self._export_worker,
                         args=(snap, out_dir, self.color_var.get()), daemon=True).start()

    def _export_worker(self, snap, out_dir, color):
        total = len(snap)
        for i, (p, blocks, rec) in enumerate(snap, 1):
            self.tasks.put(("progress", f"批量导出 {i}/{total}：{os.path.basename(p)}"))
            try:
                en = len([b for b in blocks if b.get("enabled", True)])
                doc = eng.open_masked(p, blocks, color)
                doc.save(os.path.join(out_dir, eng.auto_output_path(p)),
                         garbage=3, deflate=True)
                doc.close()
                self.tasks.put(("export_one", os.path.basename(p), en, rec, None))
            except Exception as exc:  # noqa: BLE001
                self.tasks.put(("export_one", os.path.basename(p), 0, False,
                                f"{type(exc).__name__}: {exc}"))
        self.tasks.put(("export_done",))

    def _poll_tasks(self):
        try:
            while True:
                msg = self.tasks.get_nowait()
                kind = msg[0]
                if kind == "detected":
                    self._on_detected(msg[1], msg[2], msg[3])
                elif kind == "progress":
                    self.status.set(msg[1])
                elif kind == "ai_done":
                    self._on_ai_done(msg[1], msg[2], msg[3])
                elif kind == "export_one":
                    self._export_results.append(msg[1:])
                elif kind == "export_done":
                    self.busy = False
                    self._finish_export()
        except queue.Empty:
            pass
        self.root.after(120, self._poll_tasks)

    def _finish_export(self):
        res = self._export_results
        self._export_results = []
        ok = [r for r in res if r[3] is None]
        bad = [r for r in res if r[3] is not None]
        lines = []
        for name, n, rec, _ in ok:
            lines.append(f"✔ {name}：{n} 处" + ("" if rec else "（未自动识别，仅手动打码）"))
        for name, _, _, err in bad:
            lines.append(f"✘ {name}：{err}")
        self.status.set(f"批量导出完成：成功 {len(ok)} 个，失败 {len(bad)} 个。")
        (messagebox.showwarning if bad else messagebox.showinfo)(
            "批量导出结果", "\n".join(lines[:20]) or "无")


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    RedactApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
