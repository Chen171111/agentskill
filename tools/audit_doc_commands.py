"""文档命令 vs 脚本真实参数 —— 全量覆盖审计。

**为什么需要它**（2026-09-17 的教训）
------------------------------------
项目里 28 份文档、69 个脚本，文档中的「复现命令」是**手写的**，
没有任何机制保证它和脚本的 `argparse` 定义一致。实际已经踩到：

- `docs/项目运行手册.md` 写 `test_industry_neutral.py --topk 20`，
  但该脚本定义的是 `--topn`（`--topk` 属 `backtest_stock.py`）→ **该命令直接 argparse 报错**
- 送转口径变更后，4 个脚本加了 `--adj-mode`，但文档里的命令**没跟着加** →
  照文档跑就会静默用默认口径（`correct`），**无法复现文档里的旧数字**

**这个脚本做什么**
------------------
1. 从 `tools/*.py` 的源码里静态提取每个脚本的 `add_argument` 参数名（不 import，不执行）
2. 扫描 `docs/*.md` + `joinquant/*.md` 里的 `$PY ...` 命令块（含 `\\` 续行）
3. 交叉比对，报三类问题：
   - **A 未知参数**：命令里用了脚本没定义的 flag（照跑必报错）
   - **B 口径未透传**：脚本有 `--adj-mode` 但该命令没带 → 静默用默认值
   - **C 脚本不存在**：命令引用了不存在的文件
4. 额外报 D：`--out/--out-prefix` 缺省，且缺省名与**历史 legacy 产物**同名 →
   跑一次就把旧产物覆盖掉（本项目需要 A/B 对照，这是真损失）

**用法**
-------
    $PY tools/audit_doc_commands.py            # 全量审计
    $PY tools/audit_doc_commands.py --json out.json

**注意**：纯静态分析，只读，不改任何文件、不跑任何回测。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")

# 会消费股息率面板（dy_ttm/dy_fwd）的脚本 —— 这些脚本的 --adj-mode 必须显式传，
# 否则口径不可复现（见 docs/个股线_送转调整口径缺陷.md §五·补·二·边界 1）
ADJ_SENSITIVE = {
    "test_dividend_factor.py", "backtest_dividend.py", "sweep_hyst.py",
    "test_industry_neutral.py", "sweep_dividend_into_mf.py",
    "diag_dividend_into_mf.py", "diag_industry_hyst_overlap.py",
    "paper_track_dividend.py", "cmp_selection_overlap.py", "cmp_adj_mode.py",
}

# 需要 A/B 对照的脚本：默认 --out 名一旦被新口径覆盖，旧口径证据就永久消失
AB_COMPARE = {
    "sweep_hyst.py": "results/hyst_sweep.csv",
    "sweep_dividend_into_mf.py": "results/dividend_into_mf.csv",
    "backtest_dividend.py": "results/dividend_backtest.csv",
    "test_dividend_factor.py": "results/dividend_factor_ic.csv",
    "diag_industry_hyst_overlap.py": "results/diag_ind_hyst_overlap.csv",
    "diag_dividend_into_mf.py": "results/dividend_into_mf_diag.csv",
}

ARG_RE = re.compile(r'add_argument\(\s*"(--[A-Za-z0-9_-]+)"')
CMD_RE = re.compile(r'\$PY\s+(?:-u\s+)?(tools/[A-Za-z0-9_]+\.py|joinquant/[A-Za-z0-9_]+\.py|main\.py)(.*)')


def script_flags() -> dict[str, set[str]]:
    out = {}
    for fn in sorted(os.listdir(TOOLS)):
        if not fn.endswith(".py"):
            continue
        src = open(os.path.join(TOOLS, fn), encoding="utf-8").read()
        out[fn] = set(ARG_RE.findall(src))
    return out


def doc_commands() -> list[dict]:
    """扫 docs/ 与 joinquant/ 下的 markdown，抽出所有 $PY ... 命令（含续行与代码块）。"""
    cmds = []
    for sub in ("docs", "joinquant"):
        d = os.path.join(ROOT, sub)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".md"):
                continue
            path = os.path.join(d, fn)
            lines = open(path, encoding="utf-8").read().splitlines()
            i = 0
            while i < len(lines):
                m = CMD_RE.search(lines[i])
                if not m:
                    i += 1
                    continue
                start = i + 1
                body = lines[i]
                while body.rstrip().endswith("\\") and i + 1 < len(lines):
                    i += 1
                    body = body.rstrip()[:-1] + " " + lines[i]
                body = body.replace("\\", " ")
                flags = re.findall(r'(--[A-Za-z0-9_-]+)', body)
                # `audit-ignore` 行内注释 = 有意保留的反例（例如"修复前会报错"的复现用例、
                # 说明"会覆盖历史产物"的示范命令）→ 不计入问题清单
                if "audit-ignore" in body:
                    i += 1
                    continue
                cmds.append({"doc": f"{sub}/{fn}", "line": start,
                             "script": m.group(1), "flags": flags})
                i += 1
    return cmds


def classify(flag: str, script: str, flags: set[str], thresholds: dict[str, str]) -> str:
    """区分「未知参数」与「取值型 token 被误认」。"""
    return "unknown" if flag not in flags else "known"


def collect_problems(cmds=None, flags_by_script=None) -> dict:
    """跑完整审计，返回四类问题（供 `main()` 打印、也供 `tools/selftest.py` 断言）。"""
    flags_by_script = flags_by_script or script_flags()
    cmds = doc_commands() if cmds is None else cmds

    problems: dict[str, list] = {"A_未知参数": [], "B_口径未透传": [],
                                 "C_脚本不存在": [], "D_会覆盖A/B产物": []}
    for c in cmds:
        base = os.path.basename(c["script"])
        if base not in flags_by_script:
            if not os.path.exists(os.path.join(ROOT, c["script"])):
                problems["C_脚本不存在"].append(c)
            continue
        valid = flags_by_script[base]
        all_valid = set().union(*flags_by_script.values()) | {"--ths", "--dry"}
        for f in c["flags"]:
            if f in valid:
                continue
            tag = "A_未知参数" if f not in all_valid else "A_参数写串"
            problems["A_未知参数"].append({**c, "flag": f, "tag": tag})

        if base in ADJ_SENSITIVE and base not in ("cmp_adj_mode.py", "cmp_selection_overlap.py"):
            if "--adj-mode" not in c["flags"] and "--adj-mode" in valid:
                problems["B_口径未透传"].append(c)

        if base in AB_COMPARE and "--out" not in c["flags"] and "--out-prefix" not in c["flags"]:
            problems["D_会覆盖A/B产物"].append({**c, "default_out": AB_COMPARE[base]})
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="文档命令 vs 脚本参数 覆盖审计")
    ap.add_argument("--json", default=None, help="把结果写一份 JSON")
    args = ap.parse_args(argv)

    flags_by_script = script_flags()
    cmds = doc_commands()
    problems = collect_problems(cmds, flags_by_script)

    # ---- 输出 ----
    def hdr(t):
        print("\n" + "=" * 100)
        print("  " + t)
        print("=" * 100)

    hdr(f"扫描范围：docs/ + joinquant/ 共 {len(cmds)} 条 $PY 命令"
        f"，tools/ 共 {len(flags_by_script)} 个脚本")

    hdr(f"A. 参数对不上（{len(problems['A_未知参数'])} 条）")
    for p in problems["A_未知参数"]:
        print(f"  [{p['tag']}] {p['doc']}:{p['line']}  {p['script']}  →  {p['flag']}")

    hdr(f"B. 口径未透传 --adj-mode（{len(problems['B_口径未透传'])} 条）")
    for p in problems["B_口径未透传"]:
        print(f"  {p['doc']}:{p['line']}  {p['script']}   ← 会静默用默认口径")

    hdr(f"C. 引用了不存在的脚本（{len(problems['C_脚本不存在'])} 条）")
    for p in problems["C_脚本不存在"]:
        print(f"  {p['doc']}:{p['line']}  {p['script']}")

    hdr(f"D. 缺 --out，会覆盖同名历史产物（{len(problems['D_会覆盖A/B产物'])} 条）")
    for p in problems["D_会覆盖A/B产物"]:
        print(f"  {p['doc']}:{p['line']}  {p['script']}  默认写出 {p['default_out']}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(problems, fh, ensure_ascii=False, indent=2)
        print(f"\n  JSON 已写出 {args.json}")

    print("\n  说明：A 类照跑必报错；B 类会**静默**用默认口径（结果对不上文档却不报警）；")
    print("        D 类会把旧口径产物原地覆盖 → 之后无法做 A/B 对照。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
