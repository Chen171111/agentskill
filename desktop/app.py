"""Quant Workbench Tkinter 界面。"""
from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from desktop.service import (
    PROJECT_ROOT,
    BacktestConfig,
    discover_projects,
    load_settings,
    project_status,
    save_settings,
)

BG = "#F3F6FB"
CARD = "#FFFFFF"
TEXT = "#172033"
MUTED = "#64748B"
ACCENT = "#2563EB"
BORDER = "#DDE5EF"


class QuantDesktop(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Quant Workbench · A 股策略研究台")
        self.geometry("1280x800")
        self.minsize(1080, 700)
        self.configure(bg=BG)
        self.projects = discover_projects()
        self.project_by_label = {p.label: p for p in self.projects}
        self.events = queue.Queue()
        self.proc = None
        self.tempdir = None
        self.cancel_requested = False
        self.last_result = None
        self.last_config = None
        self.runs = []
        self.current_page = None
        self._init_styles()
        self._init_vars()
        self._build_shell()
        self._load_form_settings()
        self.show_page("overview")
        self.after(100, self._poll_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _init_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        base = ("Microsoft YaHei UI", 10)
        style.configure(".", font=base, background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 22, "bold"))
        style.configure("SubTitle.TLabel", font=("Microsoft YaHei UI", 11), foreground=MUTED)
        style.configure("Kpi.TLabel", font=("Microsoft YaHei UI", 16, "bold"),
                        background=CARD, foreground=TEXT)
        style.configure("TButton", padding=(12, 7), borderwidth=0)
        style.configure("Accent.TButton", background=ACCENT, foreground="#FFFFFF",
                        font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#1D4ED8"), ("disabled", "#A7B7D8")])
        style.configure("Nav.TButton", anchor="w", padding=(18, 12), background="#172033",
                        foreground="#D7E1F0", font=("Microsoft YaHei UI", 11))
        style.map("Nav.TButton", background=[("active", "#243553")], foreground=[("active", "#FFFFFF")])
        style.configure("TEntry", padding=5)
        style.configure("TCombobox", padding=4)
        style.configure("Treeview", rowheight=30, background="#FFFFFF", fieldbackground="#FFFFFF")
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TLabelframe", background=CARD, bordercolor=BORDER)
        style.configure("TLabelframe.Label", background=CARD, foreground=TEXT,
                        font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TCheckbutton", background=CARD)
        style.configure("Horizontal.TProgressbar", background=ACCENT)

    def _init_vars(self):
        self.project_var = tk.StringVar(value=self.projects[0].label)
        self.strategy_var = tk.StringVar(value="etf_rotation")
        self.pool_var = tk.StringVar(value="ETF全球")
        self.start_var = tk.StringVar(value="20190101")
        self.end_var = tk.StringVar(value="20260911")
        self.topk_var = tk.StringVar(value="5")
        self.rebalance_var = tk.StringVar(value="5")
        self.weight_var = tk.StringVar(value="tilt")
        self.tilt_var = tk.StringVar(value="0.45")
        self.dd_var = tk.BooleanVar(value=True)
        self.vol_var = tk.StringVar(value="0.15")
        self.run_status_var = tk.StringVar(value="就绪 · 仅研究回测，不执行任何交易")

    def _build_shell(self):
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        side = tk.Frame(self, bg="#172033", width=210)
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_propagate(False)
        tk.Label(side, text="QUANT", bg="#172033", fg="#FFFFFF",
                 font=("Microsoft YaHei UI", 22, "bold")).pack(anchor="w", padx=22, pady=(28, 0))
        tk.Label(side, text="WORKBENCH", bg="#172033", fg="#7EA4F8",
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=24, pady=(0, 28))
        for key, label in (("overview", "项目总览"), ("backtest", "策略回测"),
                           ("results", "研究结果"), ("settings", "运行环境")):
            ttk.Button(side, text=label, style="Nav.TButton",
                       command=lambda k=key: self.show_page(k)).pack(fill="x", padx=10, pady=3)
        tk.Label(side, text="研究 / 回测 / 监控\n不包含下单功能", bg="#172033", fg="#91A0B8",
                 justify="left", font=("Microsoft YaHei UI", 9)).pack(
                     side="bottom", anchor="w", padx=22, pady=24)
        self.content = ttk.Frame(self)
        self.content.grid(row=0, column=1, sticky="nsew", padx=26, pady=22)
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)
        self.pages = {
            "overview": ttk.Frame(self.content),
            "backtest": ttk.Frame(self.content),
            "results": ttk.Frame(self.content),
            "settings": ttk.Frame(self.content),
        }
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")
        self._build_overview(self.pages["overview"])
        self._build_backtest(self.pages["backtest"])
        self._build_results(self.pages["results"])
        self._build_settings(self.pages["settings"])

    def show_page(self, key):
        self.current_page = key
        self.pages[key].tkraise()
        if key == "overview":
            self._refresh_overview()
        elif key == "backtest" and self.last_result:
            self.draw_chart()

    def _header(self, parent, title, subtitle):
        ttk.Label(parent, text=title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(parent, text=subtitle, style="SubTitle.TLabel").pack(anchor="w", pady=(3, 18))

    def _build_overview(self, page):
        self._header(page, "项目总览", "统一查看两条研究线；现有引擎、数据与风控口径保持不变")
        banner = tk.Frame(page, bg="#E8F0FE", padx=16, pady=12)
        banner.pack(fill="x", pady=(0, 16))
        tk.Label(banner, text="安全边界：桌面端只运行回测与只读监控，不连接同花顺、不提交委托。",
                 bg="#E8F0FE", fg="#1E4F9E", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        self.overview_cards = ttk.Frame(page)
        self.overview_cards.pack(fill="x")
        actions = ttk.LabelFrame(page, text="现有看板")
        actions.pack(fill="x", pady=(18, 0))
        for project in self.projects:
            row = ttk.Frame(actions, style="Card.TFrame")
            row.pack(fill="x", padx=12, pady=8)
            ttk.Label(row, text=project.label, style="Card.TLabel").pack(side="left")
            ttk.Button(row, text="打开 HTML 看板",
                       command=lambda p=project: self._open_dashboard(p)).pack(side="right")
        note = ("策略研究采用“基线 + 独立变体”方式。多周期 ETF 策略仅用于回测，"
                "不会替换默认模拟盘策略；个股回测使用 quant2 原引擎与既有数据口径。")
        ttk.Label(page, text=note, wraplength=880, style="Muted.TLabel").pack(anchor="w", pady=20)

    def _refresh_overview(self):
        for child in self.overview_cards.winfo_children():
            child.destroy()
        for project in self.projects:
            info = project_status(project)
            card = ttk.Frame(self.overview_cards, style="Card.TFrame", padding=18)
            card.pack(fill="x", pady=5)
            top = ttk.Frame(card, style="Card.TFrame")
            top.pack(fill="x")
            ttk.Label(top, text=project.label, style="Card.TLabel",
                      font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
            color = "#B42318" if info["alerts"] else "#16734A"
            tk.Label(top, text=info["status"], bg=CARD, fg=color,
                     font=("Microsoft YaHei UI", 10, "bold")).pack(side="right")
            detail = info["updated"] or "尚无状态文件"
            if info["alerts"]:
                detail += " · " + ", ".join(info["alerts"])
            ttk.Label(card, text=detail, style="CardMuted.TLabel").pack(anchor="w", pady=(6, 2))
            ttk.Label(card, text=project.root, style="CardMuted.TLabel").pack(anchor="w")

    def _build_backtest(self, page):
        self._header(page, "策略回测", "参数化运行 · 子进程隔离 · 实时日志 · 净值与基准对比")
        body = ttk.Frame(page)
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)
        controls = ttk.Frame(body, style="Card.TFrame", padding=16)
        controls.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        output = ttk.Frame(body)
        output.grid(row=0, column=1, sticky="nsew")
        output.grid_rowconfigure(2, weight=1)
        output.grid_columnconfigure(0, weight=1)
        self.project_combo = self._combo(controls, "项目", self.project_var,
                                         list(self.project_by_label), 0)
        self.project_combo.bind("<<ComboboxSelected>>", self._project_changed)
        self.strategy_combo = self._combo(controls, "策略", self.strategy_var,
                                          ["etf_rotation", "etf_rotation_multi"], 1)
        self.pool_combo = self._combo(controls, "ETF 池", self.pool_var,
                                      ["ETF全球", "ETF宽基", "ETF行业", "ETF防御", "ETF稳健"], 2)
        self._entry(controls, "开始日期", self.start_var, 3)
        self._entry(controls, "结束日期", self.end_var, 4)
        self._entry(controls, "持仓数量 TopK", self.topk_var, 5)
        self._entry(controls, "调仓周期（交易日）", self.rebalance_var, 6)
        self.weight_combo = self._combo(controls, "个股权重模式", self.weight_var,
                                        ["equal", "rank", "tilt"], 7)
        self.tilt_entry = self._entry(controls, "tilt 分数下限", self.tilt_var, 8)
        self.dd_check = ttk.Checkbutton(controls, text="ETF 回撤熔断", variable=self.dd_var)
        self.dd_check.grid(row=9, column=0, columnspan=2, sticky="w", pady=5)
        self.vol_entry = self._entry(controls, "ETF 波动率目标（0=关）", self.vol_var, 10)
        self.weight_label = controls.grid_slaves(row=7, column=0)[0]
        self.tilt_label = controls.grid_slaves(row=8, column=0)[0]
        self.vol_label = controls.grid_slaves(row=10, column=0)[0]
        ttk.Separator(controls).grid(row=11, column=0, columnspan=2, sticky="ew", pady=12)
        self.run_button = ttk.Button(controls, text="开始回测", style="Accent.TButton",
                                     command=self.start_backtest)
        self.run_button.grid(row=12, column=0, sticky="ew", padx=(0, 4))
        self.cancel_button = ttk.Button(controls, text="取消", command=self.cancel_backtest,
                                        state="disabled")
        self.cancel_button.grid(row=12, column=1, sticky="ew", padx=(4, 0))
        self.progress = ttk.Progressbar(controls, mode="indeterminate")
        self.progress.grid(row=13, column=0, columnspan=2, sticky="ew", pady=(12, 6))
        ttk.Label(controls, textvariable=self.run_status_var, wraplength=280,
                  style="CardMuted.TLabel").grid(row=14, column=0, columnspan=2, sticky="w")
        self._build_metric_cards(output)
        self.chart = tk.Canvas(output, bg=CARD, highlightthickness=1, highlightbackground=BORDER,
                               height=280)
        self.chart.grid(row=1, column=0, sticky="ew", pady=(12, 12))
        self.chart.bind("<Configure>", lambda _e: self.draw_chart())
        log_frame = ttk.LabelFrame(output, text="运行日志")
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.grid_rowconfigure(0, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="word", height=10, bg="#0F172A", fg="#D5E1F2",
                                insertbackground="#FFFFFF", relief="flat", padx=10, pady=8,
                                font=("Consolas", 9))
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set, state="disabled")
        self._project_changed()

    def _combo(self, parent, label, variable, values, row):
        ttk.Label(parent, text=label, style="Card.TLabel").grid(
            row=row, column=0, sticky="w", pady=5, padx=(0, 12))
        widget = ttk.Combobox(parent, textvariable=variable, values=values,
                              state="readonly", width=23)
        widget.grid(row=row, column=1, sticky="ew", pady=5)
        return widget

    def _entry(self, parent, label, variable, row):
        ttk.Label(parent, text=label, style="Card.TLabel").grid(
            row=row, column=0, sticky="w", pady=5, padx=(0, 12))
        widget = ttk.Entry(parent, textvariable=variable, width=25)
        widget.grid(row=row, column=1, sticky="ew", pady=5)
        return widget

    def _build_metric_cards(self, parent):
        frame = ttk.Frame(parent)
        frame.grid(row=0, column=0, sticky="ew")
        self.metric_vars = {}
        names = [("年化收益", "年化收益"), ("累计收益", "累计收益"), ("最大回撤", "最大回撤"),
                 ("夏普比率", "夏普比率"), ("年化波动", "年化波动"), ("超额收益", "超额收益")]
        for i, (key, label) in enumerate(names):
            frame.grid_columnconfigure(i, weight=1, uniform="metrics")
            card = ttk.Frame(frame, style="Card.TFrame", padding=6)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 4, 0))
            ttk.Label(card, text=label, style="CardMuted.TLabel").pack(anchor="center")
            var = tk.StringVar(value="—")
            self.metric_vars[key] = var
            ttk.Label(card, textvariable=var, style="Kpi.TLabel").pack(anchor="center", pady=(3, 0))

    def _project_changed(self, _event=None):
        is_etf = self.project_var.get().endswith("agentskill")
        if is_etf:
            self.strategy_combo.configure(values=["etf_rotation", "etf_rotation_multi"])
            if self.strategy_var.get() not in {"etf_rotation", "etf_rotation_multi"}:
                self.strategy_var.set("etf_rotation")
            self.pool_combo.configure(state="readonly")
            self.dd_check.configure(state="normal")
            self.vol_entry.configure(state="normal")
            self.weight_combo.configure(state="disabled")
            self.tilt_entry.configure(state="disabled")
        else:
            self.strategy_combo.configure(values=["stock_multifactor"])
            self.strategy_var.set("stock_multifactor")
            self.pool_combo.configure(state="disabled")
            self.dd_check.configure(state="disabled")
            self.vol_entry.configure(state="disabled")
            self.weight_combo.configure(state="readonly")
            self.tilt_entry.configure(state="normal")

    def _form_config(self) -> BacktestConfig:
        project = self.project_by_label[self.project_var.get()]
        return BacktestConfig(
            kind=project.kind, strategy=self.strategy_var.get(), pool=self.pool_var.get(),
            start=self.start_var.get(), end=self.end_var.get(), topk=int(self.topk_var.get()),
            rebalance=int(self.rebalance_var.get()), weight_mode=self.weight_var.get(),
            tilt_min=float(self.tilt_var.get()), dd_circuit=bool(self.dd_var.get()),
            vol_target=float(self.vol_var.get()),
        ).validate()

    def start_backtest(self):
        if self.proc is not None:
            messagebox.showinfo("回测进行中", "请先等待当前回测结束，或点击取消。")
            return
        try:
            cfg = self._form_config()
        except (ValueError, TypeError) as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        project = self.project_by_label[self.project_var.get()]
        self._save_form_settings(cfg)
        self.last_config = cfg
        self.cancel_requested = False
        self.tempdir = tempfile.TemporaryDirectory(prefix="quant-workbench-")
        output = Path(self.tempdir.name) / "result.json"
        command = [sys.executable, "-u", "-m", "desktop.worker",
                   "--project-root", project.root, "--config", json.dumps(cfg.to_dict()),
                   "--output", str(output)]
        env = os.environ.copy()
        env.update({"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"})
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.proc = subprocess.Popen(
                command, cwd=str(PROJECT_ROOT), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                bufsize=1, env=env, creationflags=flags)
        except OSError as exc:
            self.proc = None
            self.tempdir.cleanup()
            messagebox.showerror("启动失败", str(exc))
            return
        self._log_clear()
        self._log_append(f"$ {' '.join(command)}\n")
        self.run_status_var.set("运行中 · 可随时取消；不会写入交易账本")
        self.run_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress.start(12)
        threading.Thread(target=self._reader, args=(self.proc, output), daemon=True).start()

    def _reader(self, proc, output):
        try:
            for line in proc.stdout:
                self.events.put(("log", line))
            rc = proc.wait()
            payload = None
            if output.exists():
                with output.open("r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            self.events.put(("done", (rc, payload)))
        except (OSError, ValueError, TypeError) as exc:
            self.events.put(("done", (-1, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})))

    def _poll_events(self):
        while not self.events.empty():
            kind, value = self.events.get_nowait()
            if kind == "log":
                self._log_append(value)
            else:
                self._finish_backtest(*value)
        if self.winfo_exists():
            self.after(100, self._poll_events)

    def _finish_backtest(self, rc, payload):
        if self.tempdir is not None:
            self.tempdir.cleanup()
            self.tempdir = None
        self.proc = None
        self.progress.stop()
        self.run_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        if self.cancel_requested:
            self.run_status_var.set("已取消 · 未修改任何策略或交易状态")
            return
        if rc != 0 or not payload or not payload.get("ok"):
            error = (payload or {}).get("error") or f"子进程退出码 {rc}，且没有结果文件"
            self.run_status_var.set("回测失败")
            self._log_append("\n" + error + "\n")
            messagebox.showerror("回测失败", error)
            return
        self.last_result = payload["result"]
        self._show_result(self.last_result)
        self.runs.insert(0, {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "config": self.last_config.to_dict(), "result": self.last_result})
        self._refresh_runs()
        self.run_status_var.set(f"完成 · {self.last_result['engine']}")
        self._log_append("\n[OK] 结果已载入，可在“研究结果”导出 JSON。\n")

    def cancel_backtest(self):
        if self.proc is None:
            return
        self.cancel_requested = True
        self.run_status_var.set("正在取消…")
        try:
            self.proc.terminate()
        except OSError as exc:
            self._log_append(f"取消请求发送失败：{exc}\n")

    def _show_result(self, result):
        metrics = result["metrics"]
        for key, var in self.metric_vars.items():
            value = metrics.get(key)
            if value is None:
                var.set("—")
            elif key == "夏普比率":
                var.set(f"{value:.2f}")
            else:
                var.set(f"{value:+.2f}%" if key in {"年化收益", "累计收益", "超额收益"}
                        else f"{value:.2f}%")
        self.draw_chart()

    def draw_chart(self):
        if not hasattr(self, "chart"):
            return
        self.chart.delete("all")
        width = max(self.chart.winfo_width(), 500)
        height = max(self.chart.winfo_height(), 240)
        self.chart.create_text(18, 14, anchor="nw", text="净值曲线",
                               fill=TEXT, font=("Microsoft YaHei UI", 11, "bold"))
        if not self.last_result:
            self.chart.create_text(width / 2, height / 2, text="运行一次回测后显示净值曲线",
                                   fill=MUTED, font=("Microsoft YaHei UI", 11))
            return
        series = self.last_result["series"]
        dates, equity = series["dates"], series["equity"]
        benchmark = series.get("benchmark")
        values = [float(v) for v in equity if v is not None and math.isfinite(float(v))]
        if benchmark:
            values += [float(v) for v in benchmark if v is not None and math.isfinite(float(v))]
        if len(dates) < 2 or not values:
            self.chart.create_text(width / 2, height / 2, text="有效净值点不足", fill=MUTED)
            return
        left, right, top, bottom = 58, width - 24, 48, height - 34
        lo, hi = min(values), max(values)
        pad = max((hi - lo) * 0.10, 0.01)
        lo, hi = lo - pad, hi + pad
        for i in range(5):
            y = top + (bottom - top) * i / 4
            value = hi - (hi - lo) * i / 4
            self.chart.create_line(left, y, right, y, fill="#E8EDF4")
            self.chart.create_text(left - 8, y, anchor="e", text=f"{value:.2f}",
                                   fill=MUTED, font=("Consolas", 8))
        n = len(dates)

        def line(points, color):
            coords = []
            step = max(1, len(points) // 800)
            for idx in range(0, len(points), step):
                value = points[idx]
                if value is None or not math.isfinite(float(value)):
                    continue
                x = left + (right - left) * idx / max(n - 1, 1)
                y = bottom - (float(value) - lo) / (hi - lo) * (bottom - top)
                coords.extend((x, y))
            if len(coords) >= 4:
                self.chart.create_line(*coords, fill=color, width=2, smooth=False)

        line(equity, ACCENT)
        if benchmark:
            line(benchmark, "#94A3B8")
        for idx in sorted({0, n // 2, n - 1}):
            x = left + (right - left) * idx / max(n - 1, 1)
            self.chart.create_text(x, bottom + 18, text=dates[idx], fill=MUTED,
                                   font=("Consolas", 8))
        legend_x = width - 150
        self.chart.create_rectangle(legend_x, 14, legend_x + 20, 17, fill=ACCENT, outline="")
        self.chart.create_text(legend_x + 26, 15, anchor="nw", text="策略", fill=MUTED)
        if benchmark:
            bench_x = width - 80
            self.chart.create_rectangle(bench_x, 14, bench_x + 20, 17, fill="#94A3B8", outline="")
            self.chart.create_text(bench_x + 26, 15, anchor="nw", text="基准", fill=MUTED)

    def _log_clear(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _log_append(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _build_results(self, page):
        self._header(page, "研究结果", "本次桌面会话内的回测记录；导出文件包含参数、指标与净值序列")
        columns = ("time", "project", "strategy", "period", "annual", "sharpe", "mdd", "engine")
        headings = ("时间", "项目", "策略", "区间", "年化", "夏普", "最大回撤", "引擎")
        self.run_tree = ttk.Treeview(page, columns=columns, show="headings", height=16)
        widths = (150, 85, 145, 180, 90, 75, 100, 260)
        for col, heading, width in zip(columns, headings, widths, strict=True):
            self.run_tree.heading(col, text=heading)
            self.run_tree.column(col, width=width, anchor="center")
        self.run_tree.pack(fill="both", expand=True)
        self.run_tree.bind("<Double-1>", self._load_selected_run)
        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="查看净值", command=self._load_selected_run).pack(side="left")
        ttk.Button(actions, text="导出 JSON", command=self._export_selected).pack(side="left", padx=8)
        ttk.Label(actions, text="双击记录可回看图表；历史权威产物不会被覆盖。",
                  style="Muted.TLabel").pack(side="right")

    def _refresh_runs(self):
        self.run_tree.delete(*self.run_tree.get_children())
        for i, run in enumerate(self.runs):
            cfg, metrics = run["config"], run["result"]["metrics"]
            self.run_tree.insert("", "end", iid=str(i), values=(
                run["time"], cfg["kind"], cfg["strategy"], f"{cfg['start']}~{cfg['end']}",
                f"{metrics.get('年化收益', 0):.2f}%", f"{metrics.get('夏普比率', 0):.2f}",
                f"{metrics.get('最大回撤', 0):.2f}%", run["result"]["engine"]))

    def _selected_run(self):
        selection = self.run_tree.selection()
        if not selection:
            messagebox.showinfo("未选择", "请先选择一条回测记录。")
            return None
        return self.runs[int(selection[0])]

    def _load_selected_run(self, _event=None):
        run = self._selected_run()
        if run:
            self.last_result = run["result"]
            self._show_result(self.last_result)
            self.show_page("backtest")

    def _export_selected(self):
        run = self._selected_run()
        if not run:
            return
        path = filedialog.asksaveasfilename(
            title="导出回测结果", defaultextension=".json",
            initialfile=f"backtest_{run['config']['strategy']}_{run['config']['start']}_{run['config']['end']}.json",
            filetypes=[("JSON", "*.json")])
        if not path:
            return
        with Path(path).open("w", encoding="utf-8") as fh:
            json.dump(run, fh, ensure_ascii=False, indent=2, allow_nan=False)
        self.run_status_var.set(f"已导出：{path}")

    def _build_settings(self, page):
        self._header(page, "运行环境", "零新增 Python 依赖；所有计算复用项目已有模块")
        card = ttk.Frame(page, style="Card.TFrame", padding=18)
        card.pack(fill="x")
        rows = [("桌面框架", "Tkinter / ttk（Python 标准库）"),
                ("回测隔离", "desktop.worker 独立子进程；UTF-8；可取消"),
                ("ETF 引擎", "agentskill.pipeline.run_backtest"),
                ("个股引擎", "quant2.tools.backtest_stock.run"),
                ("配置保存", "系统用户配置目录 QuantWorkbench/settings.json"),
                ("交易边界", "无交易入口、无账户读取、无委托提交"),
                ("参考架构", "Qlib 工作流 / VeighNa 桌面与风控 / bt 组合式回测")]
        for i, (key, value) in enumerate(rows):
            ttk.Label(card, text=key, style="CardMuted.TLabel").grid(
                row=i, column=0, sticky="nw", padx=(0, 24), pady=6)
            ttk.Label(card, text=value, style="Card.TLabel", wraplength=700).grid(
                row=i, column=1, sticky="nw", pady=6)
        ttk.Label(page, text="提示：首次启动若系统 Python 缺少 pandas，请使用项目已有的 E:\\Python\\python.exe。",
                  style="Muted.TLabel").pack(anchor="w", pady=16)

    def _open_dashboard(self, project):
        path = Path(project.root) / "dashboard" / "index.html"
        if not path.is_file():
            messagebox.showerror("看板不存在", str(path))
            return
        os.startfile(path)

    def _load_form_settings(self):
        try:
            saved = load_settings().get("backtest")
            if not saved:
                return
            cfg = BacktestConfig.from_dict(saved)
            project = next((p for p in self.projects if p.kind == cfg.kind), self.projects[0])
            self.project_var.set(project.label)
            self.strategy_var.set(cfg.strategy)
            self.pool_var.set(cfg.pool)
            self.start_var.set(cfg.start)
            self.end_var.set(cfg.end)
            self.topk_var.set(str(cfg.topk))
            self.rebalance_var.set(str(cfg.rebalance))
            self.weight_var.set(cfg.weight_mode)
            self.tilt_var.set(str(cfg.tilt_min))
            self.dd_var.set(cfg.dd_circuit)
            self.vol_var.set(str(cfg.vol_target))
            self._project_changed()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.run_status_var.set(f"配置未载入：{exc}")

    def _save_form_settings(self, cfg=None):
        try:
            cfg = cfg or self._form_config()
            save_settings({"backtest": cfg.to_dict()})
        except (OSError, ValueError, TypeError) as exc:
            self.run_status_var.set(f"配置未保存：{exc}")

    def _on_close(self):
        if self.proc is not None:
            self.cancel_requested = True
            try:
                self.proc.terminate()
            except OSError as exc:
                self.run_status_var.set(f"退出时终止子进程失败：{exc}")
        self._save_form_settings()
        self.destroy()


def main():
    app = QuantDesktop()
    app.mainloop()


if __name__ == "__main__":
    main()
