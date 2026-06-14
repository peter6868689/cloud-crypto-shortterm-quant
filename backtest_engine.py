# -*- coding: utf-8 -*-
"""
真·回测 (Faithful Backtest) —— 直接驱动 peter_quant_engine 的真实逻辑。
================================================================================
为什么重写: 旧 backtest_*.py 是【另写的近似版】, 不跑引擎真实函数, 验证的不是实盘逻辑。
本回测用一个"历史行情服务器"(BTMarket) 喂给引擎的真实函数:
    detect_regime / compute_features / generate_signal / PaperAccount.manage/open/close
逐根 15m K 线重放, 严格因果(只用 <= 当前时刻的数据), 输出真实绩效:
    交易数 / 胜率 / 盈亏比 / 期望值(每笔) / 利润因子PF / 最大回撤 / 多空分别。

诚实局限:
  · 交易所只给约几千根 15m -> 只覆盖最近 1~2 个月(非完整牛熊), 是"当前这段行情"的体检。
  · 历史资金费/逐笔不取 -> funding=None(该因子在回测里不计分); FNG 用 alternative.me 历史日值。
  · 决策点用"该 15m 收盘价"成交(标准 bar 回测口径), 手续费+滑点按引擎常量如实扣。
运行: .venv/bin/python backtest_engine.py
"""
import sys
import os
import io
import time
import pickle
import contextlib
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

import peter_quant_engine as E

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_data_cache.pkl")

WATCH = E.WATCHLIST
MACRO = E.MACRO_SYMBOL
WANT_15M = 4000                     # OKX 15m 大约能给到的根数 (~41 天)

# 直接用 OKX 公共 REST 拉历史K线 (绕开 ccxt 对 OKX 市场加载的 keysort bug, 更稳)
OKX_HOST = "https://www.okx.com"
_BAR = {"15m": "15m", "4h": "4H", "1d": "1D"}


def _inst_id(symbol):
    return f"{symbol.split('/')[0]}-USDT-SWAP"     # BTC/USDT:USDT -> BTC-USDT-SWAP


def paginate(symbol, tf, want):
    inst, bar = _inst_id(symbol), _BAR[tf]
    rows = {}
    cursor = None                                   # after 游标: 取比该ts更早的记录
    url = f"{OKX_HOST}/api/v5/market/history-candles"
    while len(rows) < want:
        params = {"instId": inst, "bar": bar, "limit": "100"}
        if cursor:
            params["after"] = cursor
        last = None
        for _ in range(6):                          # 容错重试
            try:
                r = requests.get(url, params=params, timeout=15).json()
                if r.get("code") != "0":
                    raise RuntimeError(r.get("msg", r))
                data = r["data"]
                break
            except Exception as e:
                last = e
                time.sleep(1.5)
        else:
            raise RuntimeError(f"{symbol} {tf} 拉取反复失败: {last}")
        if not data:
            break
        for x in data:
            rows[int(x[0])] = x
        cursor = data[-1][0]                         # 本批最旧 ts -> 下页更早
        if len(data) < 100:
            break
        time.sleep(0.12)                             # 轻微限速
    items = [rows[k] for k in sorted(rows)][-want:]
    df = pd.DataFrame(
        [[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])] for x in items],
        columns=["t", "open", "high", "low", "close", "volume"])
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    return df.set_index("t")


def load_fng_history():
    """alternative.me 全量历史 FNG -> {YYYY-MM-DD: int}。失败返回空(回测里 FNG 不计分)。"""
    try:
        res = requests.get("https://api.alternative.me/fng/?limit=0&format=json", timeout=15).json()
        out = {}
        for row in res["data"]:
            d = datetime.fromtimestamp(int(row["timestamp"]), tz=timezone.utc).strftime("%Y-%m-%d")
            out[d] = int(row["value"])
        return out
    except Exception as e:
        print(f"⚠️ FNG 历史拉取失败({e}); 回测中 FNG 不计分")
        return {}


class BTMarket:
    """历史行情服务器: 接口与 MarketData 一致, 但只返回 <= 当前 sim 时刻的数据(严格因果)。"""

    def __init__(self, data, fng_hist):
        self.data = data
        self.fng_hist = fng_hist
        self.now_ts = None
        self._fng_cache = (0.0, None, "")     # 兼容引擎对 _fng_cache 的引用

    def ohlcv(self, symbol, tf, limit):
        df = self.data.get(symbol, {}).get(tf)
        if df is None:
            return None
        df = df[df.index <= self.now_ts]
        if len(df) == 0:
            return None
        return df.iloc[-limit:].copy()

    def last_price(self, symbol):
        df = self.ohlcv(symbol, "15m", 1)
        return float(df["close"].iloc[-1]) if df is not None and len(df) else None

    def funding(self, symbol):
        return None                            # 历史 funding 不取, 该因子回测里不计分

    def fng(self):
        d = self.now_ts.strftime("%Y-%m-%d")
        v = self.fng_hist.get(d)
        self._fng_cache = (time.time(), v, str(v) if v is not None else "NA")
        return (v, str(v)) if v is not None else (None, "NA")


def macro_trend_up(bt):
    df = bt.ohlcv(MACRO, E.CONTEXT_TF, E.CONTEXT_BARS)
    if df is None or len(df) < 60:
        return True
    fast = df["close"].ewm(span=12, adjust=False).mean().iloc[-1]
    slow = df["close"].ewm(span=48, adjust=False).mean().iloc[-1]
    return bool(fast > slow)


def load_all():
    print(f"拉取历史数据 ({len(WATCH)} 币 × 15m/4h/1d)…", flush=True)
    data = {}
    for s in WATCH:
        data[s] = {
            "15m": paginate(s, "15m", WANT_15M),
            "4h": paginate(s, "4h", WANT_15M // 16 + 320),
            "1d": paginate(s, "1d", 400),
        }
        print(f"  {s}: 15m {len(data[s]['15m'])} / 4h {len(data[s]['4h'])} / 1d {len(data[s]['1d'])}", flush=True)
    return data


def get_data(use_cache=True):
    """拉数据(或读缓存)。缓存让多变体实验秒级复用同一份历史, 无需反复拉网。"""
    if use_cache and os.path.exists(CACHE):
        with open(CACHE, "rb") as fp:
            data, fng_hist = pickle.load(fp)
        print(f"(用缓存数据 {os.path.basename(CACHE)})")
        return data, fng_hist
    data = load_all()
    fng_hist = load_fng_history()
    try:
        with open(CACHE, "wb") as fp:
            pickle.dump((data, fng_hist), fp)
    except Exception:
        pass
    return data, fng_hist


def simulate(data, fng_hist, overrides=None, quiet=True):
    """跑一遍回测。overrides: {引擎常量名: 值} 临时覆盖(跑完还原), 用来做单变量实验。
    返回 (acct, curve, start_ts, end_ts)。"""
    overrides = overrides or {}
    saved = {k: getattr(E, k) for k in overrides}
    for k, v in overrides.items():
        setattr(E, k, v)
    bt = BTMarket(data, fng_hist)
    real_now = E.now_utc
    E.now_utc = lambda: bt.now_ts.to_pydatetime()
    sink = io.StringIO() if quiet else sys.stdout
    try:
        with contextlib.redirect_stdout(sink):
            clock = data[MACRO]["15m"].index
            start = 320
            bt.now_ts = clock[start]
            acct = E.PaperAccount()
            regime, regime_day, curve = None, None, []
            for i in range(start, len(clock)):
                t = clock[i]
                bt.now_ts = t
                day = t.strftime("%Y-%m-%d")
                if day != regime_day:
                    regime = E.detect_regime(bt)
                    regime_day = day
                macro_up = macro_trend_up(bt)
                fng_v, _ = bt.fng()
                prices, feats, barclose = {}, {}, {}
                for s in WATCH:
                    d15 = bt.ohlcv(s, E.SIGNAL_TF, E.SIGNAL_BARS)
                    d4h = bt.ohlcv(s, E.CONTEXT_TF, E.CONTEXT_BARS)
                    if d15 is None or d4h is None or len(d15) < 60 or len(d4h) < 60:
                        continue
                    f = E.compute_features(s, d15, d4h, None)
                    if f is None:
                        continue
                    prices[s] = f.price
                    feats[s] = f
                    barclose[s] = float(d15["close"].iloc[-2]) if len(d15) >= 2 else f.price
                for s in list(acct.positions.keys()):
                    if s not in prices:
                        continue
                    reason = acct.manage(acct.positions[s], prices[s], barclose.get(s, prices[s]))
                    if reason:
                        acct.close(s, prices[s], reason)
                acct.check_hunt(prices)
                for s, f in feats.items():
                    if s in acct.positions:
                        continue
                    sig = E.generate_signal(f, fng_v, macro_up, regime, None)
                    if sig.side is None:
                        continue
                    locked, _ = acct.side_locked(sig.side)
                    if locked:
                        continue
                    ok, _ = acct.can_open(s)
                    if ok:
                        acct.open(sig, f)
                curve.append((t, acct.mark_to_market(prices)))
    finally:
        E.now_utc = real_now
        for k, v in saved.items():
            setattr(E, k, v)
    return acct, curve, clock[start], clock[-1]


def metrics(acct, curve, data, t0, t1):
    eqs = np.array([e for _, e in curve], dtype=float) if curve else np.array([E.INIT_CAPITAL])
    peak = np.maximum.accumulate(eqs)
    closed = acct.closed
    wins = [c for c in closed if c.pnl > 0]
    gw = sum(c.pnl for c in wins)
    gl = sum(c.pnl for c in closed if c.pnl <= 0)
    avg_w = gw / len(wins) if wins else 0.0
    nl = len(closed) - len(wins)
    avg_l = gl / nl if nl else 0.0
    bdf = data[MACRO]["15m"]
    bdf = bdf[(bdf.index >= t0) & (bdf.index <= t1)]
    bh = (float(bdf["close"].iloc[-1]) / float(bdf["close"].iloc[0]) - 1) * 100 if len(bdf) > 1 else 0.0
    return {
        "ret": (acct.equity / E.INIT_CAPITAL - 1) * 100,
        "maxdd": float(((eqs - peak) / peak).min() * 100),
        "n": len(closed), "open": len(acct.positions),
        "fees": sum(c.fees for c in closed),
        "wr": (len(wins) / len(closed) * 100) if closed else 0.0,
        "rr": (avg_w / abs(avg_l)) if avg_l < 0 else float("inf"),
        "pf": (gw / abs(gl)) if gl < 0 else float("inf"),
        "btc_bh": bh,
    }


def report(acct, curve, t0, t1, btc_bh):
    closed = acct.closed
    n = len(closed)
    print("\n" + "=" * 72)
    print(f"真·回测报告  ({t0:%Y-%m-%d} → {t1:%Y-%m-%d}, 15m 级, 全套真实逻辑)")
    print("=" * 72)

    eqs = np.array([e for _, e in curve], dtype=float) if curve else np.array([E.INIT_CAPITAL])
    peak = np.maximum.accumulate(eqs)
    max_dd = float(((eqs - peak) / peak).min() * 100)
    ret = (acct.equity / E.INIT_CAPITAL - 1) * 100
    # ★ 账户真实净值才是唯一裁判 (含减半实现亏损/手续费/期末未平仓)
    print(f"【账户真实结果·以此为准】净值 {acct.equity:,.1f}U  收益 {ret:+.2f}%  | 最大回撤 {max_dd:.1f}%")
    print(f"  对照: 同窗口 BTC 买入持有 {btc_bh:+.2f}%  ({'跑赢' if ret > btc_bh else '跑输'}买币 {abs(ret-btc_bh):.1f}个点)")
    open_pos = len(acct.positions)
    print(f"  交易频率: {n} 笔全平 + 期末未平 {open_pos} 笔  (窗口约 41 天)")

    if n == 0:
        print("区间内无完整平仓交易。")
        print("=" * 72)
        return

    def block(name, trades):
        if not trades:
            print(f"  {name}: 无"); return
        m = len(trades)
        wins = [c for c in trades if c.pnl > 0]
        gross_win = sum(c.pnl for c in wins)
        gross_loss = sum(c.pnl for c in trades if c.pnl <= 0)
        wr = len(wins) / m * 100
        avg_win = (gross_win / len(wins)) if wins else 0.0
        n_loss = m - len(wins)
        avg_loss = (gross_loss / n_loss) if n_loss else 0.0
        pf = (gross_win / abs(gross_loss)) if gross_loss < 0 else float("inf")
        rr = (avg_win / abs(avg_loss)) if avg_loss < 0 else float("inf")
        print(f"  {name}: {m}笔 | 胜率 {wr:.0f}% | 盈亏比 {rr:.2f} | PF {pf:.2f} | 净 {sum(c.pnl for c in trades):+.1f}U")

    # 已平仓统计 —— 仅供诊断, 不等于账户结果 (漏了减半亏损/手续费/未平仓)
    closed_net = sum(c.pnl for c in closed)
    fees = sum(c.fees for c in closed)
    gap = (acct.equity - E.INIT_CAPITAL) - closed_net
    print("-" * 72)
    print(f"【已平仓诊断·不代表账户结果】完整平仓累计 {closed_net:+.1f}U, 手续费 {fees:.0f}U")
    block("  全部", closed)
    block("  做多", [c for c in closed if c.side == "long"])
    block("  做空", [c for c in closed if c.side == "short"])
    print(f"  ⚠️ 已平仓累计({closed_net:+.0f}) 与 账户净值变化({acct.equity-E.INIT_CAPITAL:+.0f}) 的差额 {gap:+.0f}U")
    print(f"     = 炼狱减半的实现亏损 + 开仓手续费 + 期末未平仓浮亏 (这些才是真正的失血点)")
    print("-" * 72)
    verdict = "✅ 真实净值为正" if ret > 0 else "❌ 真实净值为负 (这套逻辑当前不赚钱, 不能上实盘)"
    print(f"结论: {verdict} —— 以账户净值 {ret:+.2f}% 为准, 别看每笔'期望值'")
    print("=" * 72)


# 待测假设 (每个=单一、稳健的结构改动, 防过拟合; 针对手续费/churn 与 盈亏比0.54)
VARIANTS = [
    ("基线(现状)", {}),
    ("①关2h炼狱减半", {"LIMBO_SOFT_HRS": 9999.0}),
    ("②冷却30→180min", {"REOPEN_COOLDOWN_MIN": 180.0}),
    ("③日开仓6→3", {"MAX_OPENS_PER_DAY": 3}),
    ("④让赢单跑(止盈1.8→3.5,回吐arm0.6→1.5)", {"TAKE_PROFIT_ATR": 3.5, "GIVEBACK_ARM_ATR": 1.5}),
    ("⑤①+②+③ churn组合", {"LIMBO_SOFT_HRS": 9999.0, "REOPEN_COOLDOWN_MIN": 180.0, "MAX_OPENS_PER_DAY": 3}),
]


def main():
    data, fng_hist = get_data()
    # 基线: 详细报告
    acct, curve, t0, t1 = simulate(data, fng_hist, overrides=None, quiet=True)
    report(acct, curve, t0, t1, metrics(acct, curve, data, t0, t1)["btc_bh"])

    # 变体对比
    print("\n" + "=" * 88)
    print("单变量实验对比 (只保留'真实净值↑ 且 回撤↓'的; 注意防过拟合: 41天=单一行情段)")
    print("=" * 88)
    print(f"{'方案':<34}{'净值%':>9}{'回撤%':>9}{'笔数':>7}{'手续费':>8}{'胜率':>7}{'盈亏比':>8}")
    print("-" * 88)
    base = None
    for name, ov in VARIANTS:
        a, c, s0, s1 = simulate(data, fng_hist, overrides=ov, quiet=True)
        m = metrics(a, c, data, s0, s1)
        if base is None:
            base = m
        tag = ""
        if name != "基线(现状)":
            better = (m["ret"] > base["ret"] + 0.5) and (m["maxdd"] >= base["maxdd"] - 0.5)
            tag = "  ✅更好" if better else ("  ~持平" if abs(m["ret"] - base["ret"]) <= 0.5 else "  ❌更差")
        rr = f"{m['rr']:.2f}" if m["rr"] != float("inf") else "∞"
        print(f"{name:<34}{m['ret']:>8.1f}%{m['maxdd']:>8.1f}%{m['n']:>7}{m['fees']:>8.0f}"
              f"{m['wr']:>6.0f}%{rr:>8}{tag}")
    print("=" * 88)
    print("提示: BTC 同期买入持有 %.1f%%。任何'更好'仍需在别的行情段复核, 单段漂亮≠真 edge。" % base["btc_bh"])


if __name__ == "__main__":
    main()
