# -*- coding: utf-8 -*-
# ==========================================================================
#   PETER 独家量化引擎 —— 实盘执行模块 (OKX 子账户)
#   ------------------------------------------------------------------
#   复用 peter_quant_engine 的全部大脑 (信号/周期/深度层/牛市确认/风控),
#   只把"虚拟下单"换成"真实下单", 并叠三道安全闸:
#     ① 三级解锁:  --check 只读连通 -> --live 空跑(只打印不下单) -> --live --arm 真下单
#     ② 交易所止损: 开仓即附带 SL 到交易所, 程序崩了仓位也有止损兜底, 不裸奔
#     ③ 单日亏损熔断 + KILL 一键平仓 + 杠杆硬锁(≤LEV_CAP), 杜绝再现 100x 爆仓
#
#   用法:
#     python peter_quant_live.py --check          # 只读: 连通/余额/持仓, 零下单
#     python peter_quant_live.py --live           # 空跑: 算信号但只打印"将要下的单"
#     python peter_quant_live.py --live --arm      # 实盘: 真下单 (确认无误再加 --arm)
# ==========================================================================

from __future__ import annotations

import argparse
import os
import time
import traceback
from typing import Dict, Optional

try:
    import ccxt
except Exception:
    ccxt = None

import peter_quant_engine as E   # 复用全部大脑

# --- 实盘专属配置 --- #
KEYS_FILE = os.path.join(E._DIR, "okx_keys.env")
LIVE_STATE = os.path.join(E._DIR, "peter_quant_live_state.json")
LIVE_TRADES = os.path.join(E._DIR, "peter_quant_live_trades.csv")
KILL_FILE = os.path.join(E._DIR, "KILL")          # 这个文件一出现 -> 立即全平并停机
DAILY_LOSS_HALT_PCT = 0.08                          # 单日回撤达 8% -> 当日熔断停手并全平
MARGIN_MODE = "isolated"                            # 逐仓 (与 Peter 账本一致)


# ==========================================================================
#   密钥加载
# ==========================================================================
def load_keys(path: str = KEYS_FILE) -> Dict[str, str]:
    keys = {}
    if not os.path.exists(path):
        return keys
    with open(path, "r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            keys[k.strip()] = v.strip()
    return keys


def make_client(keys: Dict[str, str]):
    if ccxt is None:
        raise RuntimeError("缺少 ccxt")
    need = ["OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"]
    missing = [k for k in need if not keys.get(k) or "粘贴" in keys.get(k, "") or "填你" in keys.get(k, "")]
    if missing:
        raise RuntimeError(f"密钥未填写完整: {missing} —— 请编辑 {KEYS_FILE}")
    ex = ccxt.okx({
        "apiKey": keys["OKX_API_KEY"],
        "secret": keys["OKX_API_SECRET"],
        "password": keys["OKX_API_PASSPHRASE"],
        "timeout": 15000,
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    ex.load_markets()
    return ex


# ==========================================================================
#   实盘账户 (继承模拟盘的全部决策逻辑, 只重写"下单"这几处)
# ==========================================================================
class LiveAccount(E.PaperAccount):
    def __init__(self, client, start_capital: float, armed: bool) -> None:
        super().__init__()
        self.ex = client
        self.armed = armed                 # False = 空跑(只打印), True = 真下单
        self.halted = False                # 单日熔断标志
        self.day_start_equity = start_capital
        self.day = E.now_utc().strftime("%Y-%m-%d")
        self._sync_balance(initial=start_capital)

    # ---------- 真实余额 ---------- #
    def _sync_balance(self, initial: Optional[float] = None) -> None:
        try:
            bal = self.ex.fetch_balance()
            usdt = bal.get("USDT", {})
            self.equity = float(usdt.get("total") or initial or E.INIT_CAPITAL)
            self.cash = float(usdt.get("free") or self.equity)
        except Exception as e:
            print(f"⚠️ 同步余额失败(用上次值): {e}")

    def mark_to_market(self, prices):
        self._sync_balance()
        return self.equity

    # ---------- 合约张数换算 ---------- #
    def _to_contracts(self, symbol: str, coin_qty: float) -> Optional[float]:
        try:
            m = self.ex.market(symbol)
            ctval = float(m.get("contractSize") or 1)
            contracts = coin_qty / ctval
            contracts = float(self.ex.amount_to_precision(symbol, contracts))
            mn = m["limits"]["amount"].get("min")
            if mn and contracts < float(mn):
                contracts = float(mn)
            return contracts if contracts > 0 else None
        except Exception as e:
            print(f"⚠️ {symbol} 张数换算失败: {e}")
            return None

    # ---------- 开仓 (真实下单 + 交易所止损) ---------- #
    def open(self, sig: "E.Signal", f: "E.Features") -> Optional["E.Position"]:
        if self.halted:
            return None
        ok, _ = self.can_open(sig.symbol)
        if not ok:
            return None

        entry = f.price
        leverage = min(sig.leverage, E.LEV_CAP)             # 杠杆硬锁
        stop = E.initial_stop(sig.side, entry, f)           # 结构外止损 (与模拟盘共用, 防猎杀)
        stop_dist = abs(entry - stop)
        stop_frac = stop_dist / entry
        if stop_frac <= 0:
            return None
        risk_dollar = E.RISK_PCT_PER_TRADE * self.equity * sig.size_mult
        notional = risk_dollar / stop_frac
        margin = notional / leverage
        margin_cap = E.MAX_MARGIN_PCT * self.equity
        if margin > margin_cap:
            margin = margin_cap; notional = margin * leverage; risk_dollar = notional * stop_frac
        if margin > self.cash:
            return None
        coin_qty = notional / entry
        contracts = self._to_contracts(sig.symbol, coin_qty)
        if not contracts:
            return None
        side = "sell" if sig.side == "short" else "buy"

        intent = (f"{'🔴真下单' if self.armed else '🟡空跑(不下单)'} 开 {sig.symbol} {sig.side.upper()} "
                  f"{leverage}x | {contracts}张≈{notional:.1f}U | 入≈{entry:.4f} 止损{stop:.4f} | 共振{sig.conviction}"
                  f"{f' 缩仓×{sig.size_mult:g}' if sig.size_mult < 1 else ''}")
        print(intent)
        if sig.reasons:
            print(f"     理由: {'; '.join(sig.reasons)}")

        if not self.armed:
            return None    # 空跑: 到此为止, 不真正建仓

        try:
            self.ex.set_leverage(leverage, sig.symbol, params={"mgnMode": MARGIN_MODE})
        except Exception as e:
            print(f"   ⚠️ 设杠杆失败(继续, 用账户默认): {e}")
        opp = "buy" if sig.side == "short" else "sell"   # 平仓/止损方向
        # 1) 市价入场 (不附带止损 —— OKX 对附带止损方向校验有坑, 改两步法)
        try:
            order = self.ex.create_order(sig.symbol, "market", side, contracts, None,
                                         params={"tdMode": MARGIN_MODE})
            fill = float(order.get("average") or order.get("price") or entry)
        except Exception as e:
            print(f"   ❌ 开仓下单失败: {e}")
            return None
        # 2) 立刻单独挂止损单到交易所; 挂不上就马上平掉刚开的仓, 绝不留"裸仓"
        try:
            self.ex.create_order(
                sig.symbol, "market", opp, contracts, None,
                params={"tdMode": MARGIN_MODE, "reduceOnly": True,
                        "stopLossPrice": float(self.ex.price_to_precision(sig.symbol, stop))})
        except Exception as e:
            print(f"   ⚠️ 止损单挂失败 -> 立即平掉刚开的仓(不留裸仓): {e}")
            try:
                self.ex.create_order(sig.symbol, "market", opp, contracts, None,
                                     params={"tdMode": MARGIN_MODE, "reduceOnly": True})
            except Exception as e2:
                print(f"   ❌ 紧急平仓也失败, 请手动检查持仓!: {e2}")
            return None

        self.opens_today += 1
        pos = E.Position(
            symbol=sig.symbol, side=sig.side, entry=fill, qty=coin_qty, leverage=leverage,
            margin=margin, stop=stop, atr=f.atr, risk_dollar=risk_dollar, open_ts=E.ts_str(),
            runner=sig.runner, conviction=sig.conviction, extreme=fill,
            open_reason="; ".join(sig.reasons))
        self.positions[sig.symbol] = pos
        self._sync_balance()
        print(f"   ✅ 已成交 @ {fill:.4f} | 交易所止损已挂 {stop:.4f}")
        self._log_open(pos)
        return pos

    # ---------- 平仓 (真实市价 reduceOnly) ---------- #
    def close(self, symbol: str, price: float, reason: str) -> None:
        pos = self.positions.get(symbol)
        if pos is None:
            return
        if self.armed:
            contracts = self._to_contracts(symbol, pos.qty)
            side = "buy" if pos.side == "short" else "sell"
            try:
                self.ex.create_order(symbol, "market", side, contracts, None,
                                     params={"tdMode": MARGIN_MODE, "reduceOnly": True})
            except Exception as e:
                print(f"   ❌ 平仓下单失败 {symbol}: {e}")
                return
            # 撤掉残留的交易所止损单 (平仓后通常自动撤, 这里兜底)
            try:
                for a in self.ex.fetch_open_orders(symbol, params={"ordType": "conditional"}):
                    try:
                        self.ex.cancel_order(a["id"], symbol, params={"ordType": "conditional"})
                    except Exception:
                        pass
            except Exception:
                pass
        else:
            print(f"🟡空跑 将平 {symbol} {pos.side.upper()} | {reason}")
            return
        super().close(symbol, price, reason)   # 复用记账/日志
        self._sync_balance()

    # ---------- 炼狱减半 (真实部分平仓) ---------- #
    def _scale_half(self, pos: "E.Position", price: float) -> None:
        if self.armed:
            contracts = self._to_contracts(pos.symbol, pos.qty / 2.0)
            side = "buy" if pos.side == "short" else "sell"
            try:
                self.ex.create_order(pos.symbol, "market", side, contracts, None,
                                     params={"tdMode": MARGIN_MODE, "reduceOnly": True})
            except Exception as e:
                print(f"   ❌ 减半下单失败 {pos.symbol}: {e}")
                return
        super()._scale_half(pos, price)
        self._sync_balance()

    # ---------- 全平 (熔断/KILL 用) ---------- #
    def flatten_all(self, prices: Dict[str, float], reason: str) -> None:
        for sym in list(self.positions.keys()):
            px = prices.get(sym, self.positions[sym].entry)
            self.close(sym, px, reason)

    # ---------- 状态文件用实盘专属路径 ---------- #
    def save(self) -> None:
        global_state = E.STATE_FILE
        E.STATE_FILE = LIVE_STATE
        try:
            super().save()
        finally:
            E.STATE_FILE = global_state

    def _log_open(self, p):
        _swap_log(super()._log_open, p)

    def _log_close(self, c):
        _swap_log(super()._log_close, c)


def _swap_log(fn, arg):
    g = E.TRADE_LOG
    E.TRADE_LOG = LIVE_TRADES
    try:
        fn(arg)
    finally:
        E.TRADE_LOG = g


# ==========================================================================
#   实盘引擎 (在每轮循环最前面插入安全检查)
# ==========================================================================
class LiveEngine(E.Engine):
    def __init__(self, client, start_capital: float, armed: bool) -> None:
        E.INIT_CAPITAL = start_capital     # 让看板收益%以实盘本金为基数(否则按1万算出-97%)
        self.md = E.MarketData()
        self.acct = LiveAccount(client, start_capital, armed)
        self._regime = None
        self._regime_ts = 0.0

    def safety_check(self) -> bool:
        """返回 False 表示应停机。每轮 cycle 之前调用。"""
        a = self.acct
        # KILL 文件: 立即全平 + 停机
        if os.path.exists(KILL_FILE):
            print("🛑 检测到 KILL 文件 —— 立即全平并停机!")
            prices = {s: self.md.last_price(s) for s in a.positions}
            a.flatten_all({k: v for k, v in prices.items() if v}, "KILL一键平仓")
            return False
        # 跨日重置熔断
        today = E.now_utc().strftime("%Y-%m-%d")
        if today != a.day:
            a.day = today; a.halted = False
            a._sync_balance()
            a.day_start_equity = a.equity
        # 单日亏损熔断
        a._sync_balance()
        if not a.halted and a.equity <= a.day_start_equity * (1 - DAILY_LOSS_HALT_PCT):
            a.halted = True
            print(f"🚨 单日亏损达 {DAILY_LOSS_HALT_PCT*100:.0f}% (净值 {a.equity:.1f} ≤ "
                  f"{a.day_start_equity*(1-DAILY_LOSS_HALT_PCT):.1f}) —— 熔断: 全平 + 今日停手!")
            prices = {s: self.md.last_price(s) for s in a.positions}
            a.flatten_all({k: v for k, v in prices.items() if v}, "单日熔断全平")
        return True

    def run_loop(self, interval: int) -> None:
        mode = "🔴 实盘真下单" if self.acct.armed else "🟡 空跑(只打印不下单)"
        print(f"🚀 Peter 实盘引擎启动 [{mode}] | 盯盘 {len(E.WATCHLIST)} 币 | 间隔 {interval}s | "
              f"起始净值 {self.acct.equity:.2f}U | KILL文件: {KILL_FILE}")
        while True:
            try:
                E.rotate_logs()
                if not self.safety_check():
                    break
                self.cycle()
            except KeyboardInterrupt:
                print("\n🛑 收到停止信号 (持仓与交易所止损保留)。")
                break
            except Exception:
                print("⚠️ 本轮异常 (已跳过):")
                traceback.print_exc()
            time.sleep(interval)


# ==========================================================================
#   只读连通检查 (零下单, 用来验证密钥/权限/余额/持仓)
# ==========================================================================
def run_check(ex) -> None:
    print("=" * 60)
    try:
        bal = ex.fetch_balance()
        usdt = bal.get("USDT", {})
        print(f"✅ 连接成功 | USDT 总额 {usdt.get('total')} | 可用 {usdt.get('free')}")
    except Exception as e:
        print(f"❌ 取余额失败 (检查密钥/权限/IP白名单): {e}"); return
    try:
        pos = [p for p in ex.fetch_positions() if p.get("contracts")]
        print(f"✅ 当前持仓 {len(pos)} 笔" + ("" if pos else " (空仓)"))
        for p in pos:
            print(f"   {p['symbol']} {p.get('side')} {p.get('contracts')}张 @ {p.get('entryPrice')}")
    except Exception as e:
        print(f"⚠️ 取持仓失败: {e}")
    print("如果以上都正常, 就可以 --live 空跑了。")
    print("=" * 60)


# ==========================================================================
#   入口
# ==========================================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="Peter 独家量化引擎 —— 实盘执行")
    ap.add_argument("--check", action="store_true", help="只读连通检查 (零下单)")
    ap.add_argument("--live", action="store_true", help="实盘盯盘 (默认空跑, 加 --arm 才真下单)")
    ap.add_argument("--arm", action="store_true", help="真正下单 (危险: 确认无误再加)")
    ap.add_argument("--interval", type=int, default=E.POLL_SECONDS)
    args = ap.parse_args()

    keys = load_keys()
    try:
        ex = make_client(keys)
    except Exception as e:
        print(f"❌ {e}")
        return

    if args.check:
        run_check(ex)
        return
    if args.live:
        cap = float(keys.get("OKX_LIVE_CAPITAL") or E.INIT_CAPITAL)
        eng = LiveEngine(ex, cap, armed=args.arm)
        eng.run_loop(args.interval)
        return
    print("请加 --check (只读) 或 --live (盯盘, 默认空跑) / --live --arm (真下单)")


if __name__ == "__main__":
    main()
