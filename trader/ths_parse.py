# -*- coding: utf-8 -*-
"""同花顺表格 OCR 文本的解析层 —— **纯函数，无 win32 依赖**。

为什么单独成模块（2026-09-21）
------------------------------
这些解析函数原来和 win32 自动化混在 `trader/ths_uia.py` 里，而后者在**模块顶层**
`import win32gui / pywinauto` —— 只有 32 位 Python 能加载它。后果是
「持仓/成交行解析」这条**最容易静默出错**的逻辑进不了 64 位跑的
`tools/selftest.py`，历次踩坑都只能人工连客户端复现：

    2026-09-21 14:50  601091「C沈鼓」的名称被 OCR 读成拉丁串 'Cit'
                     → 旧守卫「行内必须含汉字」把整行持仓**静默丢弃**
                     → 只读到 1 只持仓 → 对账偏差 95.61% → 全天拒单、零成交。

拆出来后，`selftest` 能用**真实 OCR 原文**做回归断言（夹具见
`tools/test_ths_parse.py`），不必再依赖人肉复现。

向后兼容：`trader.ths_uia` 仍 re-export 这里的**公开解析 API**
（`parse_position` / `parse_trades` / `pos_frame_sane` / `parse_confirm_text`），
`from trader.ths_uia import parse_position` 照旧可用。
"""
import re

# 表头/汇总/资金行里必然出现的词 —— 命中即跳过，避免被误当持仓行
_BAN_WORDS = ("资产", "可用", "当日", "总资产", "股票市值", "资金", "证券代码",
              "证券名称", "操作", "成交", "委托", "撤", "市场", "代码", "入", "出",
              "合同编号", "委托时间", "交易市场")


def _to_float(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _clean_code(raw):
    code = str(raw).zfill(6)
    return code if code.startswith(("5", "6", "0", "1", "3")) else None


def parse_confirm_text(text) -> dict:
    """解析「委托确认」弹窗正文 → {code, name, price, qty, amount}。

    弹窗样本（富文本剥标签后）：
        证券代码：510880(红利ETF华泰柏瑞)
        买入价格：3.427 (卖一)
        买入数量：100
        预估金额：347.700
    """
    out = {}
    m = re.search(r"证券代码[：:]\s*(\d{6})(?:\s*[（(]([^）)]*)[）)])?", text or "")
    if m:
        out["code"] = m.group(1)
        if m.group(2):
            out["name"] = m.group(2).strip()
    m = re.search(r"(?:买入|卖出)价格[：:]\s*([\d.]+)", text or "")
    if m:
        out["price"] = _to_float(m.group(1))
    m = re.search(r"(?:买入|卖出)数量[：:]\s*([\d,]+)", text or "")
    if m:
        out["qty"] = int(m.group(1).replace(",", ""))
    m = re.search(r"预估金额[：:]\s*([\d,.]+)", text or "")
    if m:
        out["amount"] = _to_float(m.group(1))
    return out


def pos_frame_sane(pos) -> bool:
    """帧级健全性校验：行内自洽 + 无负价（2026-09-24 事故新增）。

    背景（2026-09-24 14:50 实际坏帧，连拍 4 帧全坏 → 对账失败拒单一整天）：
        513100 读成 price=-192.39  ← 负价：盈亏列被 OCR 串位读成「市价」
        513500 读成 qty=500 / available=21200 / frozen=21200
                                 ← 串列：可用+冻结=42400 ≠ 余额 500，完全不自洽
        510880 整行丢失（弹窗遮挡表格的一部分）

    判据（任一命中即整帧判坏，须重拍，不能让坏行混进账本）：
      ① 任一行 price < 0（市价不可能为负，负数必是串列）；
      ② 任一行 available + frozen != qty（资金股票页恒有 余额=可用+冻结，
         见 2026-09-21 实拍夹具：601091 当日买入 0+2400=2400 ✓）。
    """
    for p in (pos or {}).values():
        px = p.get("price")
        if px is not None and float(px) < 0:
            return False
        q = int(p.get("qty") or 0)
        a = int(p.get("available") or 0)
        f = int(p.get("frozen") or 0)
        if q > 0 and a + f != q:
            return False
    return True


def parse_position(text) -> dict:
    """OCR 持仓表 → {code: {qty, available, frozen, cost, price}}。

    持仓列序：证券代码 证券名称 股票余额 可用余额 冻结数量 成本价 市价 盈亏 ...
    取「股票余额」为 qty、「成本价」为 cost、「市价」为 price。
    """
    out = {}
    for ln in text.splitlines():
        m = re.search(r"\b\d{6}\b", ln)
        if not m:
            continue
        if any(b in ln for b in _BAN_WORDS):
            continue
        # ⚠️ 不能要求「行内必须含汉字」（2026-09-21 修复）
        # ------------------------------------------------
        # 原守卫 `not re.search(r"[\u4e00-\u9fff]", ln): continue` 的隐含假设是
        # 「持仓行必有中文名称」，**该假设是错的**：同花顺新股用『C』前缀（上市未满
        # 5 个交易日），OCR 会把「C沈鼓」整段读成拉丁串（实测读成 'Cit'），
        # 于是整行持仓被静默丢弃 —— 表现为「只有 1 只持仓」，对账偏差 95.61% 而拒单
        # （2026-09-21 14:50 实际发生：601091 2400 股 / 90,528 元市值凭空消失）。
        # 改判据：无中文时要求代码出现在**行首附近**（持仓表首列是操作勾选框，
        # 代码紧随其后，实测 index≈2；阈值 16 留足 OCR 抖动余量），
        # 表头行/汇总行本就无 6 位代码，仍被拦住。
        if not re.search(r"[\u4e00-\u9fff]", ln) and m.start() > 16:
            continue
        code = _clean_code(m.group(0))
        if not code:
            continue
        # ⚠️ 先剔掉「证券名称」类 token（2026-09-28 修复，4 个交易日停摆的真因）
        # ------------------------------------------------------------------
        # 第一版直接 `ln.split(code)[-1]` 后取 `nums[0]` 当「股票余额」，隐含假设
        # 「代码之后第一个数字就是数量」——**该假设是错的**：证券名称里可能夹数字，
        # 而名称里的数字紧贴字母（如标普500ETF 被 OCR 读成 `tPF500ETF`），会被
        # 数字正则整块扫进去 → `nums[0]=500`（名称里的 500）→ **整行右移一位**：
        #     真实  21200 / 21200 / 0 / 2.736 / 2.684
        #     读成  500   / 21200 / 21200 / 0.0 / 2.736
        # 后果（2026-09-22 买入 513500「标普500ETF」后）：
        #   qty 被读成 500，与资金栏市值偏差 48% → 下单前对账**每天必失败**。
        #   `513100`（纳指ETF国）名称无数字，所以它一直正常 —— 这也是为什么
        #   09-22 之前（持仓是 510880 红利ETF，名称无数字）从未出现。
        # 处理：删掉「以字母/汉字开头」的连续字母数字串（= 名称），再做数字提取。
        # 注意必须要求**以字母或汉字开头**，否则会把 `-1098.590` 这类负号数字也删掉。
        tail = re.sub(r"[A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff]*", " ",
                      ln.split(code, 1)[-1])
        nums = re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", tail)
        # 持仓行至少要有「股票余额/可用/冻结/成本价」4 个数；数量必须为正。
        # OCR 抖动会产生 1~2 个数字的碎片行（实测出现过 qty=1/cost=0 的假持仓），
        # 这类行一旦被当成持仓，会让引擎严重误判仓位。
        if len(nums) < 4:
            continue
        # 二级保险：真实持仓必然满足 `股票余额 == 可用 + 冻结`（买入冻结只挪列不改总量）。
        # 若名称剔除后仍有残留错位，用该约束在候选偏移里选唯一自洽的那个；
        # 全都对不上（当日 T+1 买入等情形）则退回偏移 0，保持与旧行为一致。
        def _i(s):
            return int(float(s.replace(",", "")))

        off = 0
        for _k in range(0, min(3, len(nums) - 4) + 1):
            try:
                if _i(nums[_k]) == _i(nums[_k + 1]) + _i(nums[_k + 2]):
                    off = _k
                    break
            except (ValueError, IndexError):
                continue
        nums = nums[off:]
        qty = _i(nums[0])
        if qty <= 0:
            continue
        out[code] = {
            "qty": qty,                                                # 股票余额
            "available": _i(nums[1]),
            "frozen": _i(nums[2]),
            "cost": _to_float(nums[3]),                                # 成本价
            "price": _to_float(nums[4]) if len(nums) > 4 else None,    # 市价
        }
    return out


# 委托表「交易市场」列的取值（实测：上海A股 / 深圳A股 / 北京A股）
_MARKET_RE = re.compile(r"(?:上海|深圳|北京)A股|(?:沪|深|北)[A-Za-z]?股")
# 委托表「操作 / 状态」列的取值（OCR 会读坏，仅作名称截断与状态判据的辅助）
_STATE_WORDS = ("全部成交", "部分成交", "未成交", "已成", "已报", "已撤",
                "废单", "待撤", "买入", "卖出")


def _tail_six(tail):
    """委托表行尾锚定 → [委托数量, 成交数量, 委托价格, 成交均价, 撤消数量, 合同编号]。

    锚不上返回 None（交由 `parse_trades` 退回「关键词起点」路径，兼容成交表）。

    ⚠️ 为什么必须**从行尾锚定**，而不是「从操作/状态关键词起点抽数字」
    ==============================================================
    2026-09-29 实盘连拍 5 帧「当日委托」页，逐帧对照 OCR 原文后定案：

        帧  操作列(应为「卖出」)   状态列(应为「全部成交」)
        0   '32h'         ✗       「全部成交」        ✓
        1   'SH SS8aba'   ✗       'SS8aba'           ✗  ← 状态列也读坏
        2   'Si SHBatae'  ✗       'SHBatae'          ✗  ← 状态列也读坏
        3   '32k'         ✗       「全部成交」        ✓
        4   'Sik'         ✗       「全部成交」        ✓

    **操作列 5/5 帧全部读坏**，所以任何以「买入/卖出」为起点的方案都必然失败；
    而旧 `_op` 词表**还漏了「全部成交」**（只有「已成」），于是 3 帧也一起失败。
    失败的后果是**整行数字左移一位** —— 名称「标普500ETF」里的 500 被当成
    委托数量，**「委托数量」被当成「成交数量」**：

        帧 4 旧解析 → qty=500 filled_qty=100 price=100.0 avg_price=2.688 deal_id='0'
        帧 4 正确值 → qty=100 filled_qty=100 price=2.688 avg_price=2.690 deal_id=6285045859

    对「未成交」行（夹具 6）更致命：filled_qty 被写成 100 而实际 **0 成交**
    → `orders` 台账写出**假成交**，与「报告写 325 天其实只转了 297 天」同类。

    而**行尾 6 列在 5/5 帧里都稳定读出**，且紧邻「交易市场」列。故以市场词为锚、
    向前取 6 个数字。合同编号额外要求 ≥9 位整数（实测 9~10 位），
    以排除「当日成交」表（其尾部是 成交金额/成交编号/委托编号，编号仅 7 位）。

    参数
    ----
    tail : 行内 6 位代码**之后**的全部文本
    """
    m = _MARKET_RE.search(tail)
    if m:
        seg = tail[:m.start()]
    else:
        # 无市场词：只有「最后一个数字是 ≥9 位整数」才敢按委托表尾部锚定
        all_nums = re.findall(r"\d[\d,]*(?:\.\d+)?", tail)
        if not all_nums:
            return None
        if len(all_nums[-1].replace(",", "").split(".")[0]) < 9:
            return None
        seg = tail
    nums = re.findall(r"\d[\d,]*(?:\.\d+)?", seg)
    if len(nums) < 6:
        return None
    six = nums[-6:]
    if len(six[5].replace(",", "").split(".")[0]) < 9:
        return None
    return [_to_float(x) for x in six]


def _name_of(tail):
    """从行内代码之后的文本里切出证券名称（名称列是单列，后随其它列）。

    ⚠️ 不得用「买入|卖出」分割（2026-09-29）：操作列 5/5 帧被 OCR 读坏，
    分割不生效时名称会把状态与数字列整段吞进来（实测 `'2S500ETF32k全'`）。
    改为：先按 ≥2 连续空白切出名称列，再剔除操作/状态词。
    """
    seg = re.split(r"\s{2,}", tail, maxsplit=1)[0]
    for kw in _STATE_WORDS:
        seg = seg.split(kw)[0]
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", seg)[:12] or None


def parse_trades(text) -> list:
    """OCR 当日委托/成交表 → [{code, name, side, status, qty, filled_qty, price, avg_price, deal_id}]。

    委托表列序：委托时间 证券代码 证券名称 操作 状态 委托数量 成交数量 委托价格 成交均价 撤消数量 合同编号 交易市场
    成交表列序：成交时间 证券代码 证券名称 操作 成交数量 成交均价 成交金额 成交编号 委托编号

    两种表**列数不同**，用 `_tail_six` 的尾部锚定区分（委托表锚得上，成交表锚不上）。
    """
    rows = []
    for ln in text.splitlines():
        m = re.search(r"\b\d{6}\b", ln)
        if not m:
            continue
        # 同 parse_position：不得要求「行内必须含汉字」。新股「C沈鼓」的『C』前缀
        # 会让 OCR 把整段名称读成拉丁串；一旦状态列（已成/已报）也被读坏，
        # 整条成交/委托记录会静默消失 → sync_fill 拿不到真实成交价，台账失真。
        # 无中文时要求代码仍在行首附近（成交时间列 8 字符 + 空白，实测 index≈9）。
        if not re.search(r"[\u4e00-\u9fff]", ln) and m.start() > 24:
            continue
        code = _clean_code(m.group(0))
        if not code:
            continue
        tail = ln.split(code, 1)[-1]

        six = _tail_six(tail)
        if six is not None:
            qty_f, fill_f, price_f, avg_f = six[0], six[1], six[2], six[3]
            deal_id = str(int(six[5])) if six[5] is not None else None
        else:
            # 成交表路径：从操作/状态关键词起点取数字（成交表操作列**含中文**
            # 「买入/卖出」，实测夹具 2 能读出；委托表才读不出）
            _op = re.search(r"买入|卖出|已报|已成|全部成交|部分成交|未成交|已撤|废单|待撤",
                            tail)
            seg = tail[_op.start():] if _op else tail
            nums_f = [_to_float(x)
                      for x in re.findall(r"\d[\d,]*(?:\.\d+)?", seg)]

            def g(i):
                return nums_f[i] if i < len(nums_f) else None

            qty_f, fill_f, price_f, avg_f = g(0), g(1), g(2), g(3)
            deal_id = str(int(g(5))) if g(5) is not None else None

        qty_i = int(qty_f) if qty_f is not None else None
        fill_i = int(fill_f) if fill_f is not None else 0

        # 状态：文本判据优先；**文本列被 OCR 读坏时用数量关系兜底**
        # （5 帧实测有 2 帧「全部成交」被读成 'SS8aba'/'SHBatae'，
        #   若不兜底，已成会被判成「已报」→ sync_fill 的 status 永远停在 submitted）
        if "已撤" in ln or "废单" in ln:
            status = "已撤"
        elif "部分成交" in ln:
            status = "部分成交"
        elif "全部成交" in ln or "已成" in ln:
            status = "已成"
        elif qty_i and fill_i and fill_i >= qty_i:
            status = "已成"
        else:
            status = "已报"

        rows.append({
            "code": code,
            "name": _name_of(tail),
            "side": "买入" if "买入" in ln else ("卖出" if "卖出" in ln else None),
            "status": status,
            "qty": qty_i,                       # 委托数量
            "filled_qty": fill_i,               # 成交数量
            "price": price_f,                   # 委托价格
            "avg_price": avg_f,                 # 成交均价
            "deal_id": deal_id,                 # 合同编号
        })
    return rows


def match_order_row(rows, code, side, qty=None):
    """从当日委托行里挑出**属于这一笔订单**的那一行；挑不到返回 None。

    ⚠️ 为什么必须同时匹配「方向」（2026-09-29 核对 `Order` 语义时发现）
    ------------------------------------------------------------------
    当日委托表里**同一代码可以有多行**，且方向可能相反：

    - 部分成交后补单 / 同一天重跑（上一笔还挂着没成交）
    - 用户手工委托 + 自动交易委托落在同一只标的上

    旧 `UiaThsBroker.sync_fill()` 只按 code 匹配、命中即 `return` 第一行 ——
    若第一行是**反向**委托，就会拿卖单的成交价/成交量去修正买单：
    `orders` 表是**审计事实记录**，一旦串向，"成交价/成交量"就全是假的。
    （资金与持仓有 `reconcile()` 回读券商兜底，所以不会错单，但台账会说谎。）

    匹配优先级（逐级放宽，OCR 丢字段时不至于完全失配）
    --------------------------------------------------
    1. code + 方向 + 委托数量   最精确
    2. code + 方向
    3. code                     仅当**所有**候选行都没读出方向时才用（等价旧行为）

    关键：只要候选行里存在**方向明确且相反**的行，就**不**回退到第 3 级 ——
    宁可返回 None（台账停在 submitted，诚实地表示"回读不到"），也不用错行。

    ⚠️ 实测补充（2026-09-29 已成交卖单连拍 5 帧，逐帧核对 OCR 原文）
    ----------------------------------------------------------------
    **第 1/2 级几乎不会生效**：委托表的「操作」列 5/5 帧被 OCR 读坏
    （「卖出」→ 'Sik' / '32h' / 'SH SS8aba' / 'Si SHBatae' / '32k'），
    `side` 恒为 None → 实际走的**永远是第 3 级兜底**。
    所以：
      - 方向匹配是**保险**（防「同日同标的出现方向明确的多行」），不是主路径；
      - 兜底命中时 `sync_fill` 会打一条告警，让「靠兜底匹配的」这件事在日志里可见；
      - 真正的防错主力是**尾部锚定后的字段真值**（`_tail_six`），
        它保证即便方向读不出，拿到的 qty/filled_qty/price/avg_price 也是对的。

    参数
    ----
    rows : parse_trades() 的返回值（或任何含 code/side/qty 的 dict 列表）
    code : 订单代码，带不带市场后缀都行（"510880" / "510880.SH"）
    side : "buy"/"sell"，或已经是中文的 "买入"/"卖出"
    qty  : 委托数量（int）
    """
    code6 = str(code).split(".")[0].zfill(6)
    want = "买入" if side in ("buy", "买入") else "卖出"
    cands = [r for r in (rows or [])
             if str(r.get("code") or "").split(".")[0].zfill(6) == code6]
    if not cands:
        return None

    if qty is not None:
        for r in cands:
            if r.get("side") == want and r.get("qty") is not None \
                    and int(r["qty"]) == int(qty):
                return r
    for r in cands:
        if r.get("side") == want:
            return r
    # 兜底：整批候选行都没读出方向（OCR 丢了「操作」列）→ 只能取第一行
    if all(r.get("side") is None for r in cands):
        return cands[0]
    return None
