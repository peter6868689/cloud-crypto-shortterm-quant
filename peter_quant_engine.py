# -*- coding: utf-8 -*-
# ==========================================================================
#   PETER 独家量化引擎 (Peter's Edge Engine)
#   ---------------------------------------------------------------
#   设计血统:  40% 机构级风控 (硬止损 / 仓位法 / 杠杆上锁)
#              60% Peter 本人交易思想 (做空过热为主 · 主流币 · 拿住多日波段)
#
#   数据照出来的 Peter (128 笔账本结论, 用代码固化):
#     · 做空是天赋:   空 +2303 / 多 −1404  ->  系统偏向做空过热, 做多抬高门槛
#     · 钱在多日波段:  >48h 持仓 +1414     ->  让赢单跑 (移动止损锁利)
#     · 血在炼狱区:    2–48h 扛单 −690     ->  不在盈利轨道就强制减仓/离场
#     · 手续费吃净利:  −900 ≈ 全部净利     ->  限制单日开仓 + 过滤冲动 scalp
#     · 极端杠杆=死:   100x 0胜率 −1895    ->  默认 3x, 共振才 5x, 硬上限 5x
#
#   止损哲学 (按 Peter 要求, 比机构宽):
#     · 止损位按 ATR 放宽 (默认 3.0×ATR), 给大行情呼吸空间
#     · 防插针: 只有 15m K线"收盘"破位才算止损, 瞬间插针拉回不被扫
#     · 灾难性硬上限: 万一跳空/急跌, 亏损达 1.8× 计划风险即无条件砍, 杜绝 −1888
#
#   形态: 实时自动盯盘 + 模拟盘自动跟踪 (10000 USDT 起始, 全程合约)
# ==========================================================================

from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote
from urllib.request import Request, urlopen

# ---------- 第三方库容错导入 (缺啥都不让整体崩) ----------
try:
    import numpy as np
except Exception:
    np = None
try:
    import pandas as pd
except Exception:
    pd = None
try:
    import ccxt
except Exception:
    ccxt = None
try:
    import requests
except Exception:
    requests = None


# ==========================================================================
#   配置中枢 (一处定义, 全局共用 —— 想改性格, 改这里就够)
# ==========================================================================

# --- 资金与盯盘节奏 --- #
INIT_CAPITAL: float = 10_000.0        # 起始本金 (USDT)
POLL_SECONDS: int = 60                # 盯盘轮询间隔 (秒). AI 的优势: 盯得比人勤
SIGNAL_TF: str = "15m"                # 信号判定级别
CONTEXT_TF: str = "4h"                # 趋势/宏观背景级别
SIGNAL_BARS: int = 240                # 拉取信号级 K 线根数
CONTEXT_BARS: int = 240               # 拉取背景级 K 线根数

# --- 关注池 (只做主流币 —— 账本证明: 主流 +1282 / 股票代币 −384) --- #
#   用永续合约符号 (OKX swap), 这样能顺带拿到资金费率
WATCHLIST: List[str] = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "DOGE/USDT:USDT",
    "TON/USDT:USDT",
    "ZEC/USDT:USDT",
]
MACRO_SYMBOL: str = "BTC/USDT:USDT"   # 宏观风向标 (做多需 BTC 不在强空头)

# --- 杠杆纪律 (Peter 复盘结论 + 数据双重背书) --- #
LEV_BASE: int = 3                     # 默认杠杆
LEV_HIGH: int = 5                     # 多信号共振的"好机会"才升档
LEV_CAP: int = 5                      # 硬上限, 代码层面绝不越过

# --- 仓位法 (机构借鉴: 每单只赌固定比例本金) --- #
RISK_PCT_PER_TRADE: float = 0.015     # 每单计划风险 = 1.5% 净值 (到止损就亏这么多)
MAX_MARGIN_PCT: float = 0.25          # 单仓保证金不超过 25% 净值 (防单押)
MAX_CONCURRENT: int = 4               # 同时最多持仓数

# --- 止损 (比机构宽 + 防插针 + 灾难性兜底) --- #
STOP_ATR_MULT: float = 3.0            # 初始止损 = 入场价 ± 3.0×ATR (宽, 给行情呼吸)
CATASTROPHIC_MULT: float = 1.8        # 亏损达 1.8× 计划风险即无条件砍 (跳空兜底)
CONFIRM_ON_CLOSE: bool = True         # True=只认 15m 收盘破位 (防插针), 灾难线除外

# --- 移动止损 (让赢单跑, 锁住多日波段 edge) --- #
TRAIL_ACTIVATE_ATR: float = 2.0       # 盈利达 2.0×ATR 启动移动止损
TRAIL_ATR_MULT: float = 2.5           # 移动止损跟随距离 = 2.5×ATR

# --- 炼狱区清理 (2–48h 扛单是 Peter 最大失血点) --- #
LIMBO_SOFT_HRS: float = 2.0           # 持仓 >2h 仍不working: 减半仓
LIMBO_HARD_HRS: float = 12.0          # 持仓 >12h 仍未盈利: 全部离场
LIMBO_MAX_HRS: float = 48.0           # 持仓 >48h 未进入强盈利(移动止损): 离场
LIMBO_WORK_ATR: float = 0.3           # "working"门槛: 浮盈 > 0.3×ATR 才算在轨道上

# --- 反过度交易 (手续费闸门) --- #
MAX_OPENS_PER_DAY: int = 6            # 单日最多开仓数
REOPEN_COOLDOWN_MIN: float = 30.0     # 同币种平仓后冷却 (分钟), 防来回拍
MIN_SIGNAL_GAP_ATR: float = 0.0       # 预留: 信号最小幅度门槛

# --- 信号触发阈值 (Peter 60%: 做空门槛低, 做多门槛高) --- #
SHORT_OPEN_TH: int = 4                # 做空共振分 >=4 开仓 (你的天赋, 门槛低)
LONG_OPEN_TH: int = 6                 # 做多共振分 >=6 才开 (你的弱项, 门槛高)
HIGH_CONV_SHORT: int = 6              # 做空 >=6 视为高确信 -> 5x
HIGH_CONV_LONG: int = 8               # 做多 >=8 视为高确信 -> 5x

# --- 交易成本 (模拟盘如实扣减, 贴近账本) --- #
TAKER_FEE: float = 0.0005             # 单边吃单费率 0.05% (账本反推: 来回0.10%, 单边0.05%, 完全吻合)
SLIPPAGE: float = 0.0002              # 单边滑点估计 (留薄缓冲; 设0则与账本完全对齐)

# ==========================================================================
#   2026-06 强化包 (反弹就跑 / 防猎杀 / 消息冲击) —— 每块独立开关, 可单独回滚
#   触发动机: 2026-06-09 实盘复盘 —— 快打单被宽尾磨平(BTC持8h只+0.6U)、深跌反复接刀
# ==========================================================================

# --- (反弹就跑) 离场认 runner: 非runner迎反弹止盈, 只有真波段才宽尾让利跑 --- #
EXIT_RUNNER_AWARE: bool = True        # 总开关: 区分快打/波段两种离场哲学
TAKE_PROFIT_ATR: float = 1.8          # 非runner(快打)止盈目标: 浮盈达此ATR直接落袋
SCALP_TRAIL_ACTIVATE_ATR: float = 0.8 # 非runner更早启动"紧"移动止损(锁反弹利润)
SCALP_TRAIL_ATR: float = 0.6          # 非runner移动止损紧跟随(远小于runner的2.5)

# --- (防猎杀) 结构外止损 + 破位缓冲确认 --- #
STOP_STRUCTURE: bool = True           # 止损放到近端摆动高/低之外(躲止损扎堆区)
STOP_SWING_LOOKBACK: int = 20         # 摆动高/低回看根数 (15m)
STOP_STRUCTURE_BUFFER_ATR: float = 0.5  # 摆动点之外再加这么多ATR缓冲
STOP_CONFIRM_BUFFER_ATR: float = 0.2  # 初始止损须破过 ±此ATR 才算(防勉强插针破位; 移动止损不加缓冲)

# --- (消息冲击) 纯价格行为版 risk-off 门: 突发波动中别接飞刀, 顺势放行 --- #
SHOCK_DETECT: bool = True             # 总开关
SHOCK_ATR_LOOKBACK: int = 48          # ATR中位数回看根数 (12h@15m)
SHOCK_ATR_RATIO: float = 1.8          # 当前ATR/中位数 超此倍 = 波动突跳(疑似消息冲击)
SHOCK_RET_BARS: int = 4               # 短时动量回看根数 (1h@15m), 用来定冲击方向
SHOCK_RET_PCT: float = 0.02           # 该窗口涨/跌超此幅算"急涨/急跌"
SHOCK_PAUSE_NEW: bool = True          # 冲击中暂停"逆冲击方向"开仓(跌时抄底/涨时追空一律等波动落定)

# ==========================================================================
#   2026-06-10 强化包 v2 (盈利棘轮 / 防猎杀v2 / 真·消息面) —— 每块独立开关
#   触发动机: 2026-06-10 复盘 —— ①早盘多单冲到 +0.7ATR 没锁, 全还回去还套了36h
#            ②打完止损就往上(被猎杀) ③以色列要打伊朗这类消息纯价格门抓不住
# ==========================================================================

# --- (锁早盘利润 / 反弹就跑 v2) 盈利棘轮: 保本 + 回吐保护 + 快打时效 --- #
PROFIT_RATCHET: bool = True            # 总开关: 把"赚到的"焊死, 赢单不许变亏单/不许全还回去
BREAKEVEN_ARM_ATR: float = 0.5         # 浮盈达此 -> 止损上移到保本线(赢单从此不会变亏单)
BREAKEVEN_LOCK_ATR: float = 0.10       # 保本线设在入场价之上(空单之下)这么点, 盖住来回手续费
GIVEBACK_ARM_ATR: float = 0.6          # 峰值浮盈达此 -> 启动"回吐保护"(就为抓住早盘那一波)
GIVEBACK_FRAC: float = 0.5             # 非runner: 从峰值回吐超过此比例立即落袋
GIVEBACK_ARM_ATR_RUNNER: float = 3.0   # runner 峰值达此(大波段)才启用回吐保护, 之前交给宽尾移动止损
GIVEBACK_FRAC_RUNNER: float = 0.45     # runner: 大盈利后回吐近半也保护, 别把大波段坐穿
SCALP_MAX_HRS: float = 6.0             # 非runner(快打)最长时效: 超时未成势直接清(别熬成两日套牢)

# --- (刀落地确认) 深跌区逆势抄底: 须等企稳信号才进, 不接下落中的刀 --- #
#   触发场景: BTC 宏观还在跌、靠深跌区"可逢低多"放行的逆势多单 -> 必须先看到企稳。
DIP_NEEDS_CONFIRM: bool = True         # 总开关
DIP_RSI_TURN_BARS: int = 3             # 15m RSI 与几根前比, 回头向上 = 一个企稳信号

# --- (连亏熔断) 同方向接连止损 -> 拉闸停开该方向, 防"止损了还接着开"死循环 --- #
LOSS_BREAKER: bool = True              # 总开关
LOSS_BREAKER_N: int = 3               # 回看窗口内同方向亏损平仓达此笔数 -> 拉闸
LOSS_BREAKER_HRS: float = 6.0          # 回看窗口(小时); 窗内该方向若有盈利单则不算死循环(解闸)

# --- (趋势做空) 熊市顺势空反弹/空破位 —— Peter 核心 edge(账本 空+2303), 补"只会空过热"的缺口 --- #
#   老做空只认"超买"(RSI>72); 单边下跌没有超买 -> 做空永不触发, 只剩做多在接刀。
#   趋势做空: 确认下跌趋势(BEAR+4h空+未企稳)里, 价格反弹到阻力(快线)被打回 = 顺势空机会。
TREND_SHORT: bool = True               # 总开关
TS_BOUNCE_LOOKBACK: int = 12           # 近端低点回看(15m根, ~3h): 衡量是否已从低点反弹上来
TS_BOUNCE_MIN_ATR: float = 1.0         # 须从近端低点反弹≥此ATR, 才算"有反弹可空"(否则在砸盘途中不追空)
TS_RESIST_BAND_ATR: float = 0.5        # 价格升到距 EMA20 此ATR内 = 摸到阻力(快线)

# --- (防猎杀 v2) 止损猎杀识别: 被扫后快速收复 -> 警示 + 解除冷却(信号成立可立即重进) --- #
HUNT_REENTRY: bool = True              # 总开关
HUNT_WATCH_MIN: float = 45.0           # 止损后多少分钟内收复算"疑似猎杀"(约3根15m)
HUNT_RECLAIM_ATR: float = 0.5          # 价格反向越过原止损位这么多ATR算"已收复"

# --- (消息冲击探测器 v2) 真·新闻面: 抓地缘/监管/暴雷等对风险资产的瞬时冲击 --- #
#   纯免费、无 API Key、全程容错; 拉不到网就自动降级回纯价格冲击门 (SHOCK_DETECT)。
MYSTIC_ENABLED: bool = True            # 玄学定力(易经为主+五行辅): 仅微调runner持有, 绝不开仓
NEWS_SHOCK: bool = True                # 总开关: 只盯"全市场量化都会动"的大事(关税反复/黑天鹅), 小打小闹不理
NEWS_REFRESH_SEC: int = 300            # 新闻拉取间隔 (5min: 突发够快, 又不刷爆源)
NEWS_WINDOW_MIN: float = 90.0          # 滚动窗口: 只看最近这么久的新闻 (旧闻自动衰减出局)
NEWS_TIER1_SHOCK_HITS: int = 6         # 命中 TIER1(硬冲击)的不同标题数达此 = SHOCK (立刻行动)
NEWS_TIER1_WATCH_HITS: int = 2         # 达此(或软风险分够) = WATCH
NEWS_WATCH_PTS: int = 10               # 软风险加权分达此 = WATCH (没硬冲击但风险情绪明显走弱)
NEWS_RISKON_OFFSET: int = 4            # risk-on 命中数 ≥此 则把姿态降一档 (停火/降息等利好对冲)
NEWS_ACT_DERISK_LONGS: bool = True     # SHOCK 时把所有多单止损立即收到保本(逆风减险)
NEWS_HTTP_TIMEOUT: float = 8.0         # 单源拉取超时秒数 (任何源失败都跳过, 不阻塞盯盘)
# 关键词分两档 (标题小写后子串匹配; 每条标题只记命中的最高档, 防一条多词累加爆分):
#   TIER1 硬冲击 = 战争/黑客/系统性暴雷, 是"立刻行动"的真触发;
#   TIER2 软风险 = 暴跌评论/监管/鹰派, 多为价格叙事, 只升 WATCH。
NEWS_TIER1_KW: Tuple[str, ...] = (
    # 地缘/战争/系统性
    "airstrike", "missile", "invade", "invasion", "nuclear", "bomb", "retaliat",
    "strikes iran", "strike on", "attacks iran", "attack on", "ground offensive",
    "declares war", "act of war", "hack", "exploit", "drained", "depeg", "insolven",
    "bankrupt", "sec sues", "sec charges", "emergency rate", "default on",
    # 关税/贸易战/特朗普反复(TACO) —— 全市场风险资产瞬时联动的大事
    "tariff", "tariffs", "trade war", "taco trade", "trump pauses", "trump threatens",
    "export ban", "export curb", "trump tariff",
    # 黑天鹅/系统性挤兑
    "black swan", "flash crash", "bank run", "contagion", "circuit breaker",
    "liquidation cascade", "stablecoin depeg",
)
NEWS_TIER2_KW: Tuple[str, ...] = (
    "war", "attack", "escalat", "conflict", "sanction", "ceasefire collaps",
    "crash", "selloff", "sell-off", "plunge", "tumble", "rout", "liquidat",
    "lawsuit", "breach", "fraud", "ban on", "halt trading", "ftx",
    "rate hike", "hawkish", "inflation surge", "powell", "fomc",
)
NEWS_RISKON_KW: Tuple[str, ...] = (
    "ceasefire", "truce", "peace deal", "de-escalat", "rate cut", "dovish",
    "etf approv", "etf approved", "rebound", "relief rally",
)
_GNEWS = "https://news.google.com/rss/search?q={}&hl=en-US&gl=US&ceid=US:en"
NEWS_SOURCES: List[str] = [
    _GNEWS.format(quote("crypto bitcoin (war OR Iran OR Israel OR hack OR SEC OR crash OR ban)")),
    _GNEWS.format(quote("Israel Iran strike attack war escalation")),
    _GNEWS.format(quote("Trump tariff trade war China (markets OR stocks OR crypto)")),
    _GNEWS.format(quote("Federal Reserve rate decision inflation Powell")),
    "https://cointelegraph.com/rss",
]

# --- 状态落盘 --- #
_DIR: str = os.path.dirname(os.path.abspath(__file__))
STATE_FILE: str = os.path.join(_DIR, "peter_quant_state.json")
TRADE_LOG: str = os.path.join(_DIR, "peter_quant_trades.csv")

# --- 日志自动轮转 (循环模式日志会持续增长, 超限只留尾部, 无需人工清理) --- #
#   注意: 这两个文件名须与 com.peter.quant.plist 里的 StdOut/StdErr 一致
RUN_LOG: str = os.path.join(_DIR, "quant_run.log")
ERR_LOG: str = os.path.join(_DIR, "quant_err.log")
LOG_MAX_BYTES: int = 10 * 1024 * 1024   # 超过 10MB 触发轮转
LOG_KEEP_LINES: int = 800               # 轮转后保留最近多少行

FNG_REFRESH_SEC: int = 1800           # 恐慌贪婪指数刷新间隔 (半小时)


def rotate_logs() -> None:
    """日志超过上限就截断, 只保留尾部最近 LOG_KEEP_LINES 行。

    launchd 以 O_APPEND 重定向 stdout/stderr, 截断后下次写入会自动落到新 EOF,
    不会产生空洞文件 —— 所以这里直接读尾部、覆写即可, 与正在写日志的本进程兼容。
    """
    for path in (RUN_LOG, ERR_LOG):
        try:
            if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
                with open(path, "r", encoding="utf-8", errors="ignore") as fp:
                    tail = fp.readlines()[-LOG_KEEP_LINES:]
                with open(path, "w", encoding="utf-8") as fp:
                    fp.write(f"# --- 日志已自动轮转 {ts_str()}, 仅保留最近 {len(tail)} 行 ---\n")
                    fp.writelines(tail)
        except Exception:
            pass  # 轮转失败绝不影响盯盘


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ts_str(dt: Optional[datetime] = None) -> str:
    return (dt or now_utc()).strftime("%Y-%m-%d %H:%M:%S")


# ==========================================================================
#   模块 1: 数据底座 (实时行情 / 多周期 K 线 / 资金费率)
# ==========================================================================
class MarketData:
    """封装 OKX 公共接口: 现价 / 多周期 K 线 / 资金费率 / 情绪。全程容错。"""

    def __init__(self) -> None:
        self.ex = None
        if ccxt is not None:
            try:
                self.ex = ccxt.okx({"timeout": 15000, "enableRateLimit": True})
            except Exception as e:
                print(f"⚠️ 初始化交易所失败: {e}")
        self._fng_cache: Tuple[float, Optional[int], str] = (0.0, None, "未取")

    def ohlcv(self, symbol: str, timeframe: str, limit: int):
        """拉 K 线 -> DataFrame[open,high,low,close,volume], 失败 None。"""
        if self.ex is None or pd is None:
            return None
        try:
            raw = self.ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            if not raw:
                return None
            df = pd.DataFrame(raw, columns=["t", "open", "high", "low", "close", "volume"])
            df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
            return df.set_index("t")
        except Exception as e:
            print(f"   ⚠️ {symbol} {timeframe} K线异常: {e}")
            return None

    def last_price(self, symbol: str) -> Optional[float]:
        if self.ex is None:
            return None
        try:
            return float(self.ex.fetch_ticker(symbol)["last"])
        except Exception:
            return None

    def funding(self, symbol: str) -> Optional[float]:
        if self.ex is None:
            return None
        try:
            fr = self.ex.fetch_funding_rate(symbol)
            v = fr.get("fundingRate")
            return float(v) if v is not None else None
        except Exception:
            return None

    def fng(self) -> Tuple[Optional[int], str]:
        """币圈恐慌与贪婪指数 (带缓存). 极贪 -> 偏空, 极恐 -> 偏多。"""
        last_t, last_v, last_s = self._fng_cache
        if requests is None:
            return None, "缺 requests"
        if time.time() - last_t < FNG_REFRESH_SEC and last_v is not None:
            return last_v, last_s
        try:
            res = requests.get("https://api.alternative.me/fng/", timeout=6).json()
            v = int(res["data"][0]["value"])
            cls = res["data"][0]["value_classification"]
            s = f"{v} ({cls})"
            self._fng_cache = (time.time(), v, s)
            return v, s
        except Exception:
            return last_v, last_s or "获取失败"


# ==========================================================================
#   模块 1.5: 消息冲击探测器 (真·新闻面 —— 抓"以色列打伊朗"这类瞬时风险冲击)
#   ------------------------------------------------------------------
#   Peter 诉求: 一旦有消息对币圈/风险资产造成巨大影响, 立刻行动, 别等价格砸下来。
#   做法: 拉免费 RSS(无 Key) -> 90min 滚动窗口 -> 关键词加权打 risk-off/risk-on 分
#         -> 给出 CALM/WATCH/SHOCK 三档姿态。SHOCK 时: 禁新多单 + 把多单止损收到保本。
#   与纯价格冲击门(SHOCK_DETECT)互补: 新闻定"风险姿态", 价格行为定"是否已经砸下来"。
#   全程容错: 任何源拉不到都跳过; 全拉不到则降级为 CALM, 不影响其余盯盘。
# ==========================================================================
@dataclass
class NewsState:
    level: str = "CALM"                 # CALM 平静 / WATCH 警戒 / SHOCK 冲击
    riskoff: int = 0                    # risk-off 加权分
    riskon: int = 0                     # risk-on 加权分
    headlines: List[str] = field(default_factory=list)  # 触发分数的标题 (前几条)
    detail: str = "未取"

    @property
    def is_shock(self) -> bool:
        return self.level == "SHOCK"

    @property
    def net(self) -> int:
        return self.riskoff - self.riskon


class NewsShockSensor:
    """轮询免费新闻源, 维护一个时间窗内的标题集合, 关键词加权出风险姿态。"""

    def __init__(self) -> None:
        self._cache = NewsState()
        self._ts = 0.0
        self._seen: Dict[str, datetime] = {}   # 标题 -> 首次见到时间 (做滚动窗口)

    @staticmethod
    def _fetch(url: str) -> List[str]:
        """拉一个 RSS/JSON 源, 抽出 <title>。失败返回空, 绝不抛。
        优先用 requests(自带 certifi, 解决 macOS 上 urllib 的 SSL 根证书问题),
        无 requests 时退回 urllib + 宽松 SSL 上下文 (VPS 上系统证书一般没问题)。"""
        raw = None
        headers = {"User-Agent": "Mozilla/5.0 (quant-news)"}
        try:
            if requests is not None:
                raw = requests.get(url, headers=headers, timeout=NEWS_HTTP_TIMEOUT).text
            else:
                import ssl
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                req = Request(url, headers=headers)
                with urlopen(req, timeout=NEWS_HTTP_TIMEOUT, context=ctx) as resp:
                    raw = resp.read().decode("utf-8", "ignore")
        except Exception:
            return []
        if not raw:
            return []
        titles = []
        for m in re.findall(r"<title>(.*?)</title>", raw, re.I | re.S):
            t = re.sub(r"<!\[CDATA\[|\]\]>", "", m)
            t = re.sub(r"<[^>]+>", "", t).strip()
            if t and len(t) > 8:
                titles.append(t)
        return titles[:40]   # 每源只取前 40 条, 够覆盖近期头条

    @staticmethod
    def _is_noise(low: str) -> bool:
        """Google News 会把搜索词本身/频道名当 <title> 回显, 而我的搜索词里全是
        war/Iran/crash 这类关键词 -> 会自己打自己。这里把这类噪音标题剔掉。"""
        return ("google news" in low) or (low.startswith('"') and low.endswith('"'))

    @classmethod
    def _score(cls, titles: List[str]) -> Tuple[int, int, int, List[str]]:
        """返回 (tier1命中标题数, 软风险加权分, risk-on命中数, 触发标题列表)。
        关键: 去重 + 每条标题只记命中的最高档(不累加), 防一条多词把分数刷爆。"""
        t1_hits = pts = ron_hits = 0
        hit_off, seen = [], set()
        for t in titles:
            low = t.lower()
            if cls._is_noise(low) or low in seen:
                continue
            seen.add(low)
            if any(k in low for k in NEWS_TIER1_KW):        # 硬冲击: 一条记 1 命中 + 3 分
                t1_hits += 1; pts += 3; hit_off.append(t)
            elif any(k in low for k in NEWS_TIER2_KW):       # 软风险: 只 +1 分
                pts += 1; hit_off.append(t)
            if any(k in low for k in NEWS_RISKON_KW):        # risk-on(利好)对冲
                ron_hits += 1
        return t1_hits, pts, ron_hits, hit_off

    def state(self) -> NewsState:
        if time.time() - self._ts < NEWS_REFRESH_SEC and self._cache.detail != "未取":
            return self._cache
        self._ts = time.time()
        # 1) 拉全部源 -> 并入滚动窗口 (记录首次见到时间)
        now = now_utc()
        for url in NEWS_SOURCES:
            for t in self._fetch(url):
                self._seen.setdefault(t, now)
        # 2) 淘汰超出窗口的旧闻
        cutoff = now - timedelta(minutes=NEWS_WINDOW_MIN)
        self._seen = {t: ts for t, ts in self._seen.items() if ts >= cutoff}
        if not self._seen:
            self._cache = NewsState(detail="无新闻数据(网络不通?降级纯价格门)")
            return self._cache
        # 3) 对窗口内全部标题打分 (去重+分档+每条只记最高档)
        t1_hits, pts, ron_hits, hit_off = self._score(list(self._seen.keys()))
        if t1_hits >= NEWS_TIER1_SHOCK_HITS:
            level = "SHOCK"
        elif t1_hits >= NEWS_TIER1_WATCH_HITS or pts >= NEWS_WATCH_PTS:
            level = "WATCH"
        else:
            level = "CALM"
        # risk-on(停火/降息/ETF获批等)够多 -> 姿态降一档对冲
        if ron_hits >= NEWS_RISKON_OFFSET:
            level = {"SHOCK": "WATCH", "WATCH": "CALM", "CALM": "CALM"}[level]
        # 取最具代表性的触发标题 (窗口内最近优先)
        hit_off_sorted = sorted(set(hit_off), key=lambda t: self._seen.get(t, now), reverse=True)
        top = [h[:70] for h in hit_off_sorted[:3]]
        face = {"CALM": "🟢平静", "WATCH": "🟠警戒", "SHOCK": "🔴冲击"}[level]
        detail = (f"{face} 硬冲击{t1_hits}条/软风险{pts}分/利好{ron_hits}条 · 窗口{len(self._seen)}条")
        if top:
            detail += " · 头条: " + " | ".join(top[:2])
        self._cache = NewsState(level, pts, ron_hits, top, detail)
        return self._cache


# ==========================================================================
#   模块 2: 特征工程 (指标 -> 信号原料, 因果, 不前视)
# ==========================================================================
def _rsi(close, n: int = 14):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(n).mean()
    loss = (-delta.clip(upper=0)).rolling(n).mean()
    rs = gain / (loss + 1e-12)
    return 100 - 100 / (1 + rs)


def _atr(df, n: int = 14):
    high, low, close = df["high"], df["low"], df["close"]
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


@dataclass
class Features:
    symbol: str
    price: float
    atr: float                 # 15m ATR (绝对价格)
    atr_pct: float             # ATR / price
    rsi15: float
    rsi4h: float
    overext_z: float           # 价格相对 MA50 的 ATR 标准化偏离 (+超买/−超卖)
    trend4h_up: bool           # 4h 短均线在长均线之上
    funding: Optional[float]
    macd_hist: float
    swing_high: Optional[float] = None   # 近端摆动高 (结构止损用)
    swing_low: Optional[float] = None    # 近端摆动低 (结构止损用)
    atr_ratio: float = 1.0               # 当前ATR / 近端ATR中位数 (波动突跳=消息冲击代理)
    ret_fast: float = 0.0                # 短时动量 (冲击方向: 急跌为负/急涨为正)
    stabilizing: bool = False            # 企稳信号(刀落地): RSI回头/收复快线/看涨反转K (深跌抄底前置)
    stabilize_why: str = ""              # 企稳依据 (看板/日志用)
    bounced_to_resist: bool = False      # 下跌中已反弹到阻力(快线EMA20)区 = 有反弹可空 (趋势做空)
    rolling_over: bool = False           # 反弹乏力/看跌反转(RSI回落/EMA20被压回/看跌K) (趋势做空触发)
    rollover_why: str = ""               # 趋势做空依据


def compute_features(symbol: str, df15, df4h, funding: Optional[float]) -> Optional[Features]:
    """把多周期 K 线压缩成一组决策原料。任一缺失则返回 None。"""
    if df15 is None or len(df15) < 60 or df4h is None or len(df4h) < 60:
        return None
    c15 = df15["close"]
    price = float(c15.iloc[-1])

    atr_series = _atr(df15, 14)
    atr = float(atr_series.iloc[-1])
    if not math.isfinite(atr) or atr <= 0:
        return None

    rsi15 = float(_rsi(c15, 14).iloc[-1])
    rsi4h = float(_rsi(df4h["close"], 14).iloc[-1])

    ma50 = c15.rolling(50).mean()
    overext_z = float((price - ma50.iloc[-1]) / atr) if math.isfinite(ma50.iloc[-1]) else 0.0

    ma_fast = df4h["close"].ewm(span=12, adjust=False).mean().iloc[-1]
    ma_slow = df4h["close"].ewm(span=48, adjust=False).mean().iloc[-1]
    trend4h_up = bool(ma_fast > ma_slow)

    ema12 = c15.ewm(span=12, adjust=False).mean()
    ema26 = c15.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    macd_hist = float((dif - dea).iloc[-1] * 2)

    # 摆动结构: 近端高/低 (止损放到它之外, 躲开止损扎堆的猎杀区)
    lb = STOP_SWING_LOOKBACK
    swing_high = float(df15["high"].iloc[-lb:].max()) if len(df15) >= lb else None
    swing_low = float(df15["low"].iloc[-lb:].min()) if len(df15) >= lb else None
    # 波动突跳比: 当前ATR / 近端ATR中位数 —— 消息冲击的纯价格代理
    atr_med = float(atr_series.iloc[-SHOCK_ATR_LOOKBACK:].median())
    atr_ratio = (atr / atr_med) if (math.isfinite(atr_med) and atr_med > 0) else 1.0
    # 短时动量: 用来判定冲击方向 (急跌则禁抄底, 急涨则禁追空)
    ret_fast = (price / float(c15.iloc[-1 - SHOCK_RET_BARS]) - 1.0) if len(c15) > SHOCK_RET_BARS else 0.0

    # ---- 企稳信号(刀落地): 深跌抄底前置确认, 任一成立即算企稳 ----
    #   全部用"已收盘"K线(iloc[-2])判定, 因果不前视; 最后一根可能未收盘不参与反转判定。
    stabilizing, why = False, ""
    rsi_ser = _rsi(c15, 14)
    # ① 15m RSI 回头向上 (已不再创新低): 现值 > DIP_RSI_TURN_BARS 根前
    if len(rsi_ser) > DIP_RSI_TURN_BARS and math.isfinite(rsi_ser.iloc[-1 - DIP_RSI_TURN_BARS]):
        if rsi15 > float(rsi_ser.iloc[-1 - DIP_RSI_TURN_BARS]):
            stabilizing, why = True, f"RSI回头({float(rsi_ser.iloc[-1-DIP_RSI_TURN_BARS]):.0f}->{rsi15:.0f})"
    # ② 价格收复 15m 快线 (EMA12)
    if not stabilizing and math.isfinite(ema12.iloc[-1]) and price > float(ema12.iloc[-1]):
        stabilizing, why = True, "收复15m快线"
    # ③ 最近一根已收盘 15m 看涨反转K (锤子: 长下影+收在上半 / 看涨吞没)
    if not stabilizing and len(df15) >= 3:
        o, h, l, cl = (float(df15["open"].iloc[-2]), float(df15["high"].iloc[-2]),
                       float(df15["low"].iloc[-2]), float(df15["close"].iloc[-2]))
        po, pc = float(df15["open"].iloc[-3]), float(df15["close"].iloc[-3])
        rng = max(h - l, 1e-12); body = abs(cl - o); lower_wick = min(o, cl) - l
        hammer = (lower_wick >= 2 * body) and (cl >= l + 0.5 * rng)         # 锤子线
        engulf = (pc < po) and (cl > o) and (cl >= po) and (o <= pc)         # 看涨吞没
        if hammer or engulf:
            stabilizing, why = True, "看涨反转K(" + ("锤子" if hammer else "吞没") + ")"

    # ---- 趋势做空原料(空反弹): 下跌中反弹到阻力(EMA20)被打回 ----
    #   下跌途中(价<EMA50慢均线) + 已从近端低点反弹起来(有肉可空) + 升到EMA20快线阻力区。
    ema20 = c15.ewm(span=20, adjust=False).mean()
    ema20_now = float(ema20.iloc[-1])
    ema50_now = float(c15.ewm(span=50, adjust=False).mean().iloc[-1])
    recent_low = float(df15["low"].iloc[-TS_BOUNCE_LOOKBACK:].min()) if len(df15) >= TS_BOUNCE_LOOKBACK else price
    below_mean = price < ema50_now                                         # 仍在慢均线下方=下跌结构
    bounced = (price - recent_low) >= TS_BOUNCE_MIN_ATR * atr               # 已从近端低点反弹≥1ATR
    at_resist = price >= ema20_now - TS_RESIST_BAND_ATR * atr               # 升到EMA20阻力区(从下方摸上来)
    bounced_to_resist = bool(below_mean and bounced and at_resist)
    # 反弹乏力/看跌反转(任一): RSI收盘转头向下 / EMA20冲高被压回 / 看跌反转K(射击之星或看跌吞没)
    rolling_over, ro_why = False, ""
    if len(rsi_ser) >= 3 and float(rsi_ser.iloc[-2]) < float(rsi_ser.iloc[-3]) and rsi15 < 60:
        rolling_over, ro_why = True, "RSI回落"
    if not rolling_over and len(df15) >= 2:
        ph, pcl = float(df15["high"].iloc[-2]), float(df15["close"].iloc[-2])
        if ph >= ema20_now and pcl < ema20_now:                            # 上影冲破EMA20但收回其下
            rolling_over, ro_why = True, "EMA20被压回"
    if not rolling_over and len(df15) >= 3:
        o, h, l, cl = (float(df15["open"].iloc[-2]), float(df15["high"].iloc[-2]),
                       float(df15["low"].iloc[-2]), float(df15["close"].iloc[-2]))
        po, pc = float(df15["open"].iloc[-3]), float(df15["close"].iloc[-3])
        rng = max(h - l, 1e-12); body = abs(cl - o); upper_wick = h - max(o, cl)
        star = (upper_wick >= 2 * body) and (cl <= l + 0.5 * rng)           # 射击之星(长上影+收下半)
        beng = (pc > po) and (cl < o) and (cl <= po) and (o >= pc)          # 看跌吞没
        if star or beng:
            rolling_over, ro_why = True, "看跌反转K(" + ("射击星" if star else "吞没") + ")"

    return Features(symbol, price, atr, atr / price, rsi15, rsi4h,
                    overext_z, trend4h_up, funding, macd_hist,
                    swing_high, swing_low, atr_ratio, ret_fast,
                    stabilizing, why, bounced_to_resist, rolling_over, ro_why)


def initial_stop(side: str, entry: float, f: "Features") -> float:
    """初始止损价: ATR 宽止损为底, 若开启结构止损则放到近端摆动高/低之外 + 缓冲。
    放结构之外是为了躲开"止损扎堆"的猎杀位 (插针专打摆动点)。实盘与模拟共用此函数。"""
    atr_dist = STOP_ATR_MULT * f.atr
    if side == "short":
        stop = entry + atr_dist
        if STOP_STRUCTURE and f.swing_high is not None and math.isfinite(f.swing_high):
            stop = max(stop, f.swing_high + STOP_STRUCTURE_BUFFER_ATR * f.atr)   # 取更高(更远)
    else:
        stop = entry - atr_dist
        if STOP_STRUCTURE and f.swing_low is not None and math.isfinite(f.swing_low):
            stop = min(stop, f.swing_low - STOP_STRUCTURE_BUFFER_ATR * f.atr)    # 取更低(更远)
    return stop


# ==========================================================================
#   模块 2.5: 市场周期判定 (牛/熊/震荡 —— 让引擎自适应大节奏)
#   ------------------------------------------------------------------
#   Peter 只经历过熊市, "高就空"是熊市肌肉记忆; 牛市来了须自动切成偏多,
#   否则会在多头行情里逆势做空。用加密圈公认、可由免费日线算出的周期指标:
#     · 200日均线 (牛熊分界线)        · 50/200 金叉死叉
#     · 牛市支撑带 (20周SMA + 21周EMA) · 200日线斜率 (趋势方向)
#     · 90日动量                       · Mayer 倍数 (价/200日, 过热/深值)
#   减半周期仅作背景参考 (日线趋势比日历更可靠, 不参与打分)。
# ==========================================================================
REGIME_REFRESH_SEC: int = 3600        # 周期判定刷新间隔 (日线变化慢, 一小时足够)
HALVING_DATE = datetime(2024, 4, 20, tzinfo=timezone.utc)  # 上次比特币减半
BULL_CONFIRM_DAYS: int = 15           # 牛市确认: 结构条件须连续站稳这么多天才解锁"长拿多单"
LONG_RUNNER_CONV_BULL: int = 5        # 确认牛市里, 顺势多单达此共振分即可当波段拿


@dataclass
class Regime:
    label: str                 # BULL 牛 / BEAR 熊 / NEUTRAL 震荡
    score: int                 # 周期分 (−6..+6)
    short_th: int              # 该周期下做空开仓门槛
    long_th: int               # 该周期下做多开仓门槛
    long_needs_macro: bool     # 做多是否必须顺 4h (熊市须, 牛市可逢低多)
    short_runner_ok: bool      # 是否允许把空单当多日波段拿 (牛市禁)
    long_runner_ok: bool       # 是否允许把多单当多日波段拿 (仅"已确认牛市"才True)
    mayer: float
    short_size_mult: float     # 做空仓位系数 (深跌区缩仓: 跌透了空头危险, 反弹凶)
    bull_confirmed: bool       # 牛市是否已严格确认 (结构持续15天+); 只有它True才敢长拿多单
    detail: str
    bottoming: bool = False    # 深跌区是否已现企稳(收复20日线+10日线上行); False=刀还在落, 趋势做空可用


def detect_regime(md: "MarketData") -> Regime:
    """用 BTC 日线判定大周期, 并给出该周期下的自适应参数。"""
    neutral = Regime("NEUTRAL", 0, 5, 5, True, True, False, 1.0, 1.0, False, "日线数据不足, 按震荡处理")
    df = md.ohlcv(MACRO_SYMBOL, "1d", 320)
    if df is None or len(df) < 210:
        return neutral
    c = df["close"]
    price = float(c.iloc[-1])
    sma200 = c.rolling(200).mean()
    sma50 = c.rolling(50).mean()
    sma140 = c.rolling(140).mean()                       # 20 周 ≈ 140 日
    ema147 = c.ewm(span=147, adjust=False).mean()        # 21 周 ≈ 147 日
    mayer = price / float(sma200.iloc[-1])
    slope = float(sma200.iloc[-1] - sma200.iloc[-21])    # 200日线近20日斜率
    ret90 = price / float(c.iloc[-91]) - 1.0
    band = max(float(sma140.iloc[-1]), float(ema147.iloc[-1]))  # 牛市支撑带

    score = 0
    score += 2 if price > float(sma200.iloc[-1]) else -2
    score += 1 if float(sma50.iloc[-1]) > float(sma200.iloc[-1]) else -1
    score += 1 if price > band else -1
    score += 1 if slope > 0 else -1
    score += 1 if ret90 > 0 else -1

    if score >= 3:
        label = "BULL"
        # 牛市: 做多门槛降, 做空门槛升; 多单可逢低(不必顺4h)且能当波段; 空单只快打不长留
        short_th, long_th, long_macro, srun, lrun = 6, 4, False, False, True
    elif score <= -3:
        label = "BEAR"
        # 熊市: 维持你熟悉的偏空; 多单须顺4h且不长留; 空单可当多日波段
        short_th, long_th, long_macro, srun, lrun = 4, 6, True, True, False
    else:
        label = "NEUTRAL"
        # 震荡: 两边对称, 你说的"多空都开"
        short_th, long_th, long_macro, srun, lrun = 5, 5, True, True, True

    # ---------- 牛市严格确认 (Peter 铁律: 一定确认是真牛才敢长拿, 防反弹陷阱) ---------- #
    #   反弹 vs 真牛的分水岭 = 200日线"本身在向上拐"。8万的反弹若200日线还向下, 不算牛。
    #   四条结构条件须【连续站稳 BULL_CONFIRM_DAYS 天】才确认; 慢进快出。
    band_series = pd.concat([sma140, ema147], axis=1).max(axis=1)
    rising200 = sma200 > sma200.shift(20)                 # 200日线向上拐 (最关键的防陷阱)
    bull_struct = (rising200 & (sma50 > sma200) & (c > band_series) & (c > sma200))
    try:
        bull_confirmed = bool(bull_struct.iloc[-BULL_CONFIRM_DAYS:].all()
                              and len(bull_struct.dropna()) >= BULL_CONFIRM_DAYS)
    except Exception:
        bull_confirmed = False
    # 长拿多单的钥匙: 仅"已确认牛市"才交出 (看起来像牛但没熬够 -> 只许快打)
    lrun = bool(bull_confirmed)

    # Mayer 过热保护: 即便趋势偏多, 价格远离200日线(>2.4)时放开做空门槛防接盘
    if label == "BULL" and mayer > 2.4:
        short_th = 5

    # ---------- 极值信息层 (Mayer 仅展示, 不再做方向覆盖) ---------- #
    # ★ 2026-06-12 重构 (做减法, 回到纯技术): 老"深度感知层"在 Mayer 低时【收手做空+放开做多】,
    #   把系统从"顺势做空"硬掰成"逆势抄底", 是这几天实盘 100% 做多巨亏的根因(人为预测"该反弹了")。
    #   已彻底移除方向覆盖 —— 方向只由趋势(regime/4h)决定, 绝不再用 Mayer 猜底。
    #   Mayer 仅留作展示; bottoming(收复20日线+10日线上行) 仅供趋势做空判断"是否别再空了"。
    sma20 = c.rolling(20).mean()
    sma10 = c.rolling(10).mean()
    reclaim20 = price > float(sma20.iloc[-1])                       # 收复20日线 = 下跌至少已暂停
    rising10 = float(sma10.iloc[-1]) > float(sma10.iloc[-4])        # 10日线转头向上 = 短动量回升
    bottoming = bool(reclaim20 and rising10)                        # 两者皆备才算"刀已落地"(仅趋势做空用)
    short_size_mult = 1.0                                           # 不再按 Mayer 缩仓; 风险由固定1.5%/单+止损管
    if mayer < 0.82:
        depth = "🩸深跌值区" + ("·已现企稳(收20日线)" if bottoming else "·仍在下跌")
    elif mayer < 0.90:
        depth = "偏深" + ("·已现企稳" if bottoming else "·仍在下跌")
    elif mayer > 1.15:
        depth = "高位"
    else:
        depth = "中位"

    months = (now_utc() - HALVING_DATE).days / 30.4
    bull_tag = ("✅已确认牛市·可长拿多单" if bull_confirmed
                else ("⏳牛市待确认·多单只快打" if label == "BULL" else ""))
    detail = (f"分{score:+d} | 价/200日 {mayer:.2f}(Mayer)·{depth} | 90日 {ret90*100:+.0f}% | "
              f"{'价在' if price > band else '价跌破'}牛市支撑带 | 减半后{months:.0f}月{(' | ' + bull_tag) if bull_tag else ''}")
    return Regime(label, score, short_th, long_th, long_macro, srun, lrun,
                  mayer, short_size_mult, bull_confirmed, detail, bottoming=bottoming)


# ==========================================================================
#   模块 3: 玄学钩子 (易经为主、五行为辅 —— 仅给"长线持有"加分, 绝不单独触发开仓)
# ==========================================================================
_BAGUA = ("乾", "兑", "离", "震", "巽", "坎", "艮", "坤")    # 先天八卦序 0-7
#   卦象"进取/持有"基调: 乾离震 利进取(+1) · 坎艮 主险阻(−1) · 兑巽坤 中性(0)
_TRIGRAM_TONE = (1, 0, 1, 1, 0, -1, -1, 0)


def yijing_hold_score(symbol: str, dt: datetime) -> Tuple[int, str]:
    """玄学定力(易经为主、五行为辅)。只在"方向已对、已盈利"时, 微调是否把它当多日波段拿住,
    【永不】凭空触发开仓, 只调节持有风格。纯确定性、无随机、可复现。返回 (分, 说明)。
    占比: 易经(上下卦基调, ±2)为主 + 五行(日支生克, ±1)为辅, 合计 -3..+3。"""
    base = datetime(2000, 1, 1, tzinfo=timezone.utc)
    day_idx = (dt - base).days
    coin = symbol.split("/")[0]
    coin_seed = sum(ord(ch) for ch in coin)
    # —— 易经(主): 由日期+币种"起卦", 上下两卦的基调相加 (−2..+2) ——
    lower = day_idx % 8
    upper = (day_idx // 8 + coin_seed) % 8
    yijing = _TRIGRAM_TONE[upper] + _TRIGRAM_TONE[lower]
    gua = f"上{_BAGUA[upper]}下{_BAGUA[lower]}"
    # —— 五行(辅): 日支五行 与 币种命格五行 生克, 仅 ±1 ——
    elem_map = {"BTC": 4, "ETH": 2, "SOL": 1, "DOGE": 0, "TON": 3, "ZEC": 4}  # 水0火1木2土3金4
    coin_elem = elem_map.get(coin, day_idx % 5)
    day_elem = (day_idx % 12) % 5
    sheng = {0: 2, 2: 1, 1: 3, 3: 4, 4: 0}  # 水生木 木生火 火生土 土生金 金生水
    ke = {0: 1, 1: 4, 4: 2, 2: 3, 3: 0}     # 水克火 火克金 金克木 木克土 土克水
    wuxing = 1 if sheng.get(day_elem) == coin_elem else (-1 if ke.get(day_elem) == coin_elem else 0)
    return yijing + wuxing, f"易经{gua}({yijing:+d})·五行({wuxing:+d})"


# ==========================================================================
#   模块 4: 信号引擎 (Peter 60% —— 做空过热为主, 做多高门槛, 共振计分)
# ==========================================================================
@dataclass
class Signal:
    symbol: str
    side: Optional[str]        # 'short' / 'long' / None
    conviction: int            # 共振分
    leverage: int              # 建议杠杆 (3 / 5)
    runner: bool               # 是否按多日波段拿住 (让赢单跑)
    size_mult: float = 1.0     # 仓位系数 (深跌区做空缩仓)
    reasons: List[str] = field(default_factory=list)


def generate_signal(f: Features, fng: Optional[int], macro_trend_up: bool,
                    regime: "Regime", news: "Optional[NewsState]" = None) -> Signal:
    """
    打分制 + 周期自适应. 熊市偏空(你的天赋)、牛市偏多(交给机器)、震荡两边打。
    门槛与"能否当波段拿住"都随 regime 切换; 分数本身决定杠杆 (越共振越敢上 5x)。
    news: 真·消息面姿态 (SHOCK 禁逆风新单 / WATCH 抬多单门槛); None 则不参与 (兼容旧调用)。
    """
    sig = Signal(symbol=f.symbol, side=None, conviction=0, leverage=LEV_BASE, runner=False)

    # ---------- 做空共振 (逮过热) ---------- #
    s, sr = 0, []
    if f.rsi15 > 72:
        s += 2; sr.append(f"15m RSI {f.rsi15:.0f} 超买")
    if f.rsi4h > 70:
        s += 1; sr.append(f"4h RSI {f.rsi4h:.0f} 过热")
    if f.overext_z > 2.0:
        s += 2; sr.append(f"价格高于均线 {f.overext_z:.1f}×ATR (拉伸)")
    if f.funding is not None and f.funding > 0.0003:
        s += 1; sr.append(f"资金费 {f.funding*100:.3f}% 多头拥挤")
    if fng is not None and fng >= 75:
        s += 2; sr.append(f"FNG {fng} 极度贪婪")
    if f.macd_hist < 0 and f.overext_z > 1.0:
        s += 1; sr.append("MACD 高位转弱")

    # ---------- 做多共振 (抄超卖, 须顺宏观) ---------- #
    l, lr = 0, []
    if f.rsi15 < 28:
        l += 2; lr.append(f"15m RSI {f.rsi15:.0f} 超卖")
    if f.rsi4h < 35:
        l += 1; lr.append(f"4h RSI {f.rsi4h:.0f} 偏冷")
    if f.overext_z < -2.0:
        l += 2; lr.append(f"价格低于均线 {f.overext_z:.1f}×ATR (深跌)")
    if f.funding is not None and f.funding < -0.0002:
        l += 1; lr.append(f"资金费 {f.funding*100:.3f}% 空头拥挤")
    if fng is not None and fng <= 20:
        l += 2; lr.append(f"FNG {fng} 极度恐慌")
    if f.trend4h_up:
        l += 1; lr.append("4h 趋势向上")
    if macro_trend_up:
        l += 1; lr.append("BTC 宏观未走空")

    # ---------- 趋势做空 (熊市顺势空反弹 —— Peter 核心 edge, 补"只会空过热") ---------- #
    #   只在确认下跌(BEAR + 4h空 + 未企稳)里启用; 必须"已反弹到阻力 + 反弹乏力/看跌反转"两者皆备,
    #   即"空反弹"而非"砸盘途中追空"。基础 4 分(达 BEAR 门槛即开), 额外共振升杠杆/转波段。
    ts, tsr = 0, []
    if (TREND_SHORT and regime.label == "BEAR" and (not f.trend4h_up)
            and (not regime.bottoming) and f.bounced_to_resist and f.rolling_over):
        ts = 4
        tsr = ["反弹至 EMA20 阻力区", f"反弹乏力·{f.rollover_why}"]
        if f.macd_hist < 0:
            ts += 1; tsr.append("MACD 转弱")
        if f.funding is not None and f.funding > 0.00005:
            ts += 1; tsr.append(f"反弹中多头追高(资金费 {f.funding*100:.3f}%)")
        if f.rsi4h < 45:
            ts += 1; tsr.append(f"4h RSI {f.rsi4h:.0f} 偏弱(顺势)")
        tsr.append("[趋势做空·熊市顺势空反弹]")
    if ts > s:                      # 取占优做空设定: 下跌里通常是趋势空在触发(过热空 s≈0)
        s, sr = ts, tsr

    # ---------- 仲裁: 取占优方, 套【周期自适应】门槛 ---------- #
    long_ok = (l >= regime.long_th) and (macro_trend_up or not regime.long_needs_macro)
    if s >= regime.short_th and s >= l:
        sig.side = "short"; sig.conviction = s; sig.reasons = sr
        sig.size_mult = regime.short_size_mult       # 深跌区自动缩仓
        sig.reasons.append(f"[{regime.label}周期·做空门槛{regime.short_th}"
                           f"{f'·缩仓×{regime.short_size_mult:g}' if regime.short_size_mult < 1.0 else ''}]")
        sig.leverage = LEV_HIGH if s >= HIGH_CONV_SHORT else LEV_BASE
        # 空单当波段拿住: 须高确信 + 4h不强多 + 该周期允许(牛市/深跌区禁空单长留)
        sig.runner = (s >= HIGH_CONV_SHORT) and (not f.trend4h_up) and regime.short_runner_ok
    elif long_ok:
        # ---- 刀落地确认: 逆势抄底(BTC宏观还在跌、靠深跌区"可逢低多"放行)须先企稳 ----
        #   不再"见超卖就买"; 没看到企稳信号 = 刀还在落 -> 不接, 等下一根。
        counter_trend_dip = (not macro_trend_up) and (not regime.long_needs_macro)
        if DIP_NEEDS_CONFIRM and counter_trend_dip and not f.stabilizing:
            sig.side = None
            sig.conviction = l
            sig.reasons = lr + ["⛔深跌逆势抄底·未见企稳(RSI回头/收复快线/反转K) -> 等刀落地, 不接飞刀"]
            return sig
        sig.side = "long"; sig.conviction = l; sig.reasons = lr
        if counter_trend_dip and f.stabilizing:
            sig.reasons.append(f"✅企稳确认·{f.stabilize_why}")
        sig.reasons.append(f"[{regime.label}周期·做多门槛{regime.long_th}"
                           f"{'·须顺宏观' if regime.long_needs_macro else '·可逢低多'}]")
        sig.leverage = LEV_HIGH if l >= HIGH_CONV_LONG else LEV_BASE
        # 多单当波段拿住的钥匙: 必须"已确认牛市"(regime.long_runner_ok 已等于 bull_confirmed)。
        #   确认牛市里更敢拿: 顺势 + 共振达 LONG_RUNNER_CONV_BULL 即可当波段 (放大牛市利润);
        #   未确认时这把钥匙是关的 -> 多单一律只快打, 防反弹陷阱重仓被埋。
        sig.runner = regime.long_runner_ok and f.trend4h_up and (
            l >= HIGH_CONV_LONG or l >= LONG_RUNNER_CONV_BULL)
        if sig.runner:
            sig.reasons.append("✅确认牛市·顺势多单当波段拿")
    else:
        sig.side = None
        sig.conviction = max(s, l)

    # ---------- 真·消息面门 (只管大事: 关税反复/黑天鹅等全市场量化都会动的消息) ---------- #
    #   只在 SHOCK(真·大事) 才动作: 禁逆风新多单(空单顺 risk-off 风不拦)。
    #   WATCH 只在看板提示、不拦单 —— Peter: "小打小闹无所谓"。
    if NEWS_SHOCK and news is not None and news.is_shock and sig.side == "long":
        sig.reasons.append(f"📰🔴消息大事 risk-off({news.net:+d}) -> 暂停新多单")
        sig.side = None
        return sig

    # ---------- 价格行为冲击门: 突发波动中别接飞刀 (顺势放行) ---------- #
    #   纯价格代理: ATR 突跳=有事发生; 急跌则禁抄底多、急涨则禁追空 (逆冲击方向暂停)。
    #   与上面的新闻门互补: 新闻定姿态, 这里确认"是否已经砸下来"。顺冲击方向的单仍放行。
    if SHOCK_DETECT and sig.side and f.atr_ratio >= SHOCK_ATR_RATIO:
        against = ((f.ret_fast <= -SHOCK_RET_PCT and sig.side == "long")
                   or (f.ret_fast >= SHOCK_RET_PCT and sig.side == "short"))
        if against and SHOCK_PAUSE_NEW:
            sig.reasons.append(
                f"⚡冲击门·ATR×{f.atr_ratio:.1f}突跳+急{'跌' if f.ret_fast < 0 else '涨'}{f.ret_fast*100:+.1f}%"
                f" -> 逆向暂停开仓(别接飞刀)")
            sig.side = None
            return sig

    # ---------- 玄学定力(易经为主、五行为辅): 仅在已成型的 runner 上微调持有倾向 ---------- #
    if MYSTIC_ENABLED and sig.side and sig.runner:
        wx, why = yijing_hold_score(f.symbol, now_utc())
        if wx < 0:
            sig.runner = False
            sig.reasons.append(f"玄学 {why} 不利久持 -> 降级落袋")
        elif wx > 0:
            sig.reasons.append(f"玄学 {why} 利久持 -> 适合波段拿")

    return sig


# ==========================================================================
#   模块 5: 模拟盘账户 (机构风控落地: 仓位 / 止损 / 移动 / 炼狱清理)
# ==========================================================================
@dataclass
class Position:
    symbol: str
    side: str                  # long / short
    entry: float
    qty: float                 # 张数 = notional / entry (币本位数量)
    leverage: int
    margin: float              # 占用保证金
    stop: float                # 当前止损价 (会随移动止损更新)
    atr: float                 # 入场时 ATR (锚定止损/移动)
    risk_dollar: float         # 计划风险 (到初始止损的亏损额)
    open_ts: str
    runner: bool
    conviction: int
    scaled: bool = False       # 是否已因炼狱减过半仓
    trailing: bool = False     # 移动止损是否已启动
    extreme: float = 0.0       # 已达到的最有利价 (long=最高, short=最低)
    breakeven: bool = False    # 是否已把止损推到保本线 (赢单不再变亏单)
    news_derisk: bool = False  # 是否曾因消息冲击被动收紧止损 (看板标注)
    open_reason: str = ""

    def notional(self) -> float:
        return self.qty * self.entry

    def unrealized(self, price: float) -> float:
        d = (price - self.entry) if self.side == "long" else (self.entry - price)
        return d * self.qty

    def unrealized_atr(self, price: float) -> float:
        d = (price - self.entry) if self.side == "long" else (self.entry - price)
        return d / self.atr if self.atr else 0.0

    def age_hours(self) -> float:
        try:
            t0 = datetime.strptime(self.open_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            return (now_utc() - t0).total_seconds() / 3600.0
        except Exception:
            return 0.0


@dataclass
class ClosedTrade:
    symbol: str
    side: str
    entry: float
    exit: float
    qty: float
    leverage: int
    pnl: float
    pnl_pct: float
    fees: float
    open_ts: str
    close_ts: str
    hold_hours: float
    reason: str
    conviction: int


class PaperAccount:
    """10000 USDT 模拟盘. 自动开/平/盯, 所有机构风控在此落地。"""

    def __init__(self) -> None:
        self.equity: float = INIT_CAPITAL
        self.cash: float = INIT_CAPITAL          # 可用 (未占用保证金)
        self.positions: Dict[str, Position] = {}
        self.closed: List[ClosedTrade] = []
        self.opens_today: int = 0
        self.opens_day: str = now_utc().strftime("%Y-%m-%d")
        self.last_close_ts: Dict[str, str] = {}  # 币种 -> 上次平仓时间 (冷却)
        self.hunt_watch: Dict[str, dict] = {}    # 币种 -> 被止损后的猎杀观察 (内存态, 不落盘)

    # ---------------- 落盘 / 读盘 ---------------- #
    def save(self) -> None:
        data = {
            "equity": self.equity, "cash": self.cash,
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "closed_count": len(self.closed),
            "opens_today": self.opens_today, "opens_day": self.opens_day,
            "last_close_ts": self.last_close_ts,
            "updated": ts_str(),
        }
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ 状态落盘失败: {e}")

    def load(self) -> None:
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            self.equity = data.get("equity", INIT_CAPITAL)
            self.cash = data.get("cash", INIT_CAPITAL)
            self.opens_today = data.get("opens_today", 0)
            self.opens_day = data.get("opens_day", now_utc().strftime("%Y-%m-%d"))
            self.last_close_ts = data.get("last_close_ts", {})
            self.positions = {k: Position(**v) for k, v in data.get("positions", {}).items()}
            print(f"✅ 已载入状态: 净值 {self.equity:.2f}, 持仓 {len(self.positions)} 笔")
        except Exception as e:
            print(f"⚠️ 状态读盘失败 (按全新开始): {e}")

    # ---------------- 工具 ---------------- #
    def _roll_day(self) -> None:
        today = now_utc().strftime("%Y-%m-%d")
        if today != self.opens_day:
            self.opens_day = today
            self.opens_today = 0

    def mark_to_market(self, prices: Dict[str, float]) -> float:
        floating = sum(p.unrealized(prices[s]) for s, p in self.positions.items() if s in prices)
        used_margin = sum(p.margin for p in self.positions.values())
        self.equity = self.cash + used_margin + floating
        return self.equity

    # ---------------- 开仓 (机构仓位法 + 杠杆上锁) ---------------- #
    def can_open(self, symbol: str) -> Tuple[bool, str]:
        self._roll_day()
        if symbol in self.positions:
            return False, "已持仓"
        if len(self.positions) >= MAX_CONCURRENT:
            return False, f"已达并发上限 {MAX_CONCURRENT}"
        if self.opens_today >= MAX_OPENS_PER_DAY:
            return False, f"今日开仓已达上限 {MAX_OPENS_PER_DAY} (手续费闸门)"
        lc = self.last_close_ts.get(symbol)
        if lc:
            try:
                t0 = datetime.strptime(lc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if (now_utc() - t0).total_seconds() / 60.0 < REOPEN_COOLDOWN_MIN:
                    return False, f"冷却中 (<{REOPEN_COOLDOWN_MIN:.0f}min)"
            except Exception:
                pass
        return True, "ok"

    def side_locked(self, side: str) -> Tuple[bool, str]:
        """连亏熔断: 回看窗口内同方向若接连止损(且无盈利单), 拉闸停开该方向。
        防"止损了还接着开多"的死循环。盈利单会解闸 (说明方向开始对了)。
        注: 只看本次运行内已平仓记录 (self.closed), 重启清零, 窗口仅 6h 故影响很小。"""
        if not LOSS_BREAKER:
            return False, ""
        cutoff = now_utc() - timedelta(hours=LOSS_BREAKER_HRS)
        losses, wins = 0, 0
        for c in self.closed:
            if c.side != side:
                continue
            try:
                t = datetime.strptime(c.close_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if t < cutoff:
                continue
            if c.pnl > 0:
                wins += 1
            elif c.pnl < 0:
                losses += 1
        if wins == 0 and losses >= LOSS_BREAKER_N:
            return True, (f"连亏熔断·{LOSS_BREAKER_HRS:.0f}h内做{'多' if side=='long' else '空'}"
                          f"连续止损{losses}笔且无盈利 -> 暂停该方向 (等方向转好/窗口滑过)")
        return False, ""

    def open(self, sig: Signal, f: Features) -> Optional[Position]:
        ok, why = self.can_open(sig.symbol)
        if not ok:
            return None

        entry = f.price
        leverage = min(sig.leverage, LEV_CAP)
        # 止损位: 结构外 + ATR (躲开止损扎堆区, 防被插针猎杀) —— 见 initial_stop
        stop = initial_stop(sig.side, entry, f)
        stop_dist = abs(entry - stop)
        stop_frac = stop_dist / entry
        if stop_frac <= 0:
            return None

        # 仓位法: notional 让"到止损 = 1.5% 净值"; 深跌区做空再乘缩仓系数
        risk_dollar = RISK_PCT_PER_TRADE * self.equity * sig.size_mult
        notional = risk_dollar / stop_frac
        margin = notional / leverage
        # 保证金上限约束 (防单押)
        margin_cap = MAX_MARGIN_PCT * self.equity
        if margin > margin_cap:
            margin = margin_cap
            notional = margin * leverage
            risk_dollar = notional * stop_frac
        if margin > self.cash:
            return None  # 可用不足

        qty = notional / entry
        fee_open = notional * (TAKER_FEE + SLIPPAGE)
        self.cash -= (margin + fee_open)
        self.opens_today += 1

        pos = Position(
            symbol=sig.symbol, side=sig.side, entry=entry, qty=qty, leverage=leverage,
            margin=margin, stop=stop, atr=f.atr, risk_dollar=risk_dollar,
            open_ts=ts_str(), runner=sig.runner, conviction=sig.conviction,
            extreme=entry, open_reason="; ".join(sig.reasons),
        )
        self.positions[sig.symbol] = pos
        print(f"🟢 开仓 {sig.symbol} {sig.side.upper()} {leverage}x @ {entry:.4f} "
              f"| 止损 {stop:.4f} ({stop_dist/f.atr:.1f}×ATR) | 张数 {qty:.4f} "
              f"| 风险 {risk_dollar:.1f}U | 共振 {sig.conviction} | {'波段拿住' if sig.runner else '快进快出'}")
        if sig.reasons:
            print(f"     理由: {pos.open_reason}")
        self._log_open(pos)
        return pos

    # ---------------- 盯仓 (止损 / 移动 / 灾难 / 炼狱) ---------------- #
    def manage(self, pos: Position, f_price: float, bar_close: float) -> Optional[str]:
        """
        返回平仓原因字符串则触发离场; None 则继续持有。
        f_price: 实时价 (用于灾难线 / 移动止损推进)
        bar_close: 最近一根 15m 收盘价 (用于防插针的止损确认)
        """
        # 更新最有利价
        if pos.side == "long":
            pos.extreme = max(pos.extreme, f_price)
        else:
            pos.extreme = min(pos.extreme, f_price)

        # --- (1) 灾难性硬上限: 无条件, 不等收盘 (跳空/急跌兜底, 杜绝 −1888) --- #
        floating = pos.unrealized(f_price)
        if floating <= -CATASTROPHIC_MULT * pos.risk_dollar:
            return f"灾难止损 (浮亏 {floating:.1f}U ≥ {CATASTROPHIC_MULT}×计划风险)"

        prof_atr = pos.unrealized_atr(f_price)
        # 峰值浮盈(ATR): 用已记录的最有利价算, 是"回吐保护/保本"的锚
        peak_atr = (((pos.extreme - pos.entry) if pos.side == "long"
                     else (pos.entry - pos.extreme)) / pos.atr) if pos.atr > 0 else 0.0

        # --- (2) 盈利棘轮: 保本 + 回吐保护 (核心修复"早盘赚了不跑又全还回去") --- #
        if PROFIT_RATCHET:
            # 2a. 保本止损: 一旦像样盈利, 止损推到入场价之上(空单之下)一点 -> 赢单永不变亏单
            if peak_atr >= BREAKEVEN_ARM_ATR and not pos.breakeven:
                pos.breakeven = True
            if pos.breakeven:
                lock = BREAKEVEN_LOCK_ATR * pos.atr
                if pos.side == "long":
                    pos.stop = max(pos.stop, pos.entry + lock)
                else:
                    pos.stop = min(pos.stop, pos.entry - lock)
            # 2b. 回吐保护: 峰值够高后, 从峰值回吐过半立即落袋 (就为抓住冲高那一波)
            g_arm = GIVEBACK_ARM_ATR_RUNNER if pos.runner else GIVEBACK_ARM_ATR
            g_frac = GIVEBACK_FRAC_RUNNER if pos.runner else GIVEBACK_FRAC
            if peak_atr >= g_arm and prof_atr <= peak_atr * (1.0 - g_frac):
                return (f"止盈·回吐保护 (峰值 {peak_atr:.2f}ATR 回吐至 {prof_atr:.2f}ATR, "
                        f"守住峰值 {(1 - g_frac) * 100:.0f}%)")

        # --- (3) 离场: 区分 快打(反弹就跑) vs 波段runner(让赢单跑) --- #
        if EXIT_RUNNER_AWARE and not pos.runner:
            # 快打/反弹单: 迎反弹止盈, 紧移动止损锁利, 绝不拿成波段
            if prof_atr >= TAKE_PROFIT_ATR:
                return f"止盈·反弹落袋 (浮盈 {prof_atr:.2f}ATR ≥ {TAKE_PROFIT_ATR})"
            if prof_atr >= SCALP_TRAIL_ACTIVATE_ATR:
                pos.trailing = True
            if pos.trailing:
                if pos.side == "long":
                    pos.stop = max(pos.stop, pos.extreme - SCALP_TRAIL_ATR * pos.atr)
                else:
                    pos.stop = min(pos.stop, pos.extreme + SCALP_TRAIL_ATR * pos.atr)
        else:
            # 波段 runner: 宽尾让利跑, 锁住多日波段 edge
            if prof_atr >= TRAIL_ACTIVATE_ATR:
                pos.trailing = True
            if pos.trailing:
                if pos.side == "long":
                    pos.stop = max(pos.stop, pos.extreme - TRAIL_ATR_MULT * pos.atr)
                else:
                    pos.stop = min(pos.stop, pos.extreme + TRAIL_ATR_MULT * pos.atr)

        # --- (4) 常规止损: 防插针(收盘破位) + 初始止损带破位缓冲 --- #
        #   缓冲只在"尚未盈利保护"时给(防勉强插针); 已进入移动/保本则要锁得快, 不再给缓冲。
        ref = bar_close if CONFIRM_ON_CLOSE else f_price
        protected = pos.trailing or pos.breakeven
        buf = (STOP_CONFIRM_BUFFER_ATR * pos.atr) if not protected else 0.0
        if pos.side == "long" and ref <= pos.stop - buf:
            tag = "移动" if pos.trailing else ("保本" if pos.breakeven else "")
            return f"{tag}止损 (收盘 {ref:.4f} ≤ {pos.stop:.4f}{f'−{buf:.4f}缓冲' if buf else ''})"
        if pos.side == "short" and ref >= pos.stop + buf:
            tag = "移动" if pos.trailing else ("保本" if pos.breakeven else "")
            return f"{tag}止损 (收盘 {ref:.4f} ≥ {pos.stop:.4f}{f'+{buf:.4f}缓冲' if buf else ''})"

        # --- (5) 炼狱区清理: 2–48h 扛单是 Peter 最大失血点 --- #
        age = pos.age_hours()
        working = prof_atr >= LIMBO_WORK_ATR
        if not pos.trailing:  # 已进入移动止损=强 runner, 豁免炼狱清理
            # 快打时效: 非runner超时未成势直接清, 别熬成两日套牢 (修复多单被坐36h)
            if EXIT_RUNNER_AWARE and not pos.runner and age >= SCALP_MAX_HRS:
                return f"快打时效·{SCALP_MAX_HRS:.0f}h 未成势清仓 (持 {age:.1f}h, 浮盈 {prof_atr:.2f}ATR)"
            if age >= LIMBO_MAX_HRS:
                return f"炼狱清理·48h 未成势离场 (持 {age:.1f}h, 浮盈 {prof_atr:.2f}ATR)"
            if age >= LIMBO_HARD_HRS and prof_atr <= 0:
                return f"炼狱清理·12h 仍亏离场 (持 {age:.1f}h)"
            if age >= LIMBO_SOFT_HRS and not working and not pos.scaled:
                self._scale_half(pos, f_price)  # 减半仓, 不全离
                return None
        return None

    def _scale_half(self, pos: Position, price: float) -> None:
        """炼狱软清理: 砍掉一半仓位, 锁风险, 留一半看后续。"""
        cut_qty = pos.qty / 2.0
        realized = pos.unrealized(price) / 2.0
        fee = cut_qty * price * (TAKER_FEE + SLIPPAGE)
        freed_margin = pos.margin / 2.0
        self.cash += freed_margin + realized - fee
        pos.qty -= cut_qty
        pos.margin -= freed_margin
        pos.risk_dollar /= 2.0
        pos.scaled = True
        print(f"🟡 炼狱减半 {pos.symbol} (持 {pos.age_hours():.1f}h 未成势) "
              f"| 落 {realized - fee:+.1f}U | 余张数 {pos.qty:.4f}")

    def close(self, symbol: str, price: float, reason: str) -> None:
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return
        realized = pos.unrealized(price)
        fee = pos.notional() * (TAKER_FEE + SLIPPAGE)  # 平仓单边
        net = realized - fee
        self.cash += pos.margin + net
        hold = pos.age_hours()
        pnl_pct = net / pos.margin * 100.0 if pos.margin else 0.0
        ct = ClosedTrade(
            symbol=symbol, side=pos.side, entry=pos.entry, exit=price, qty=pos.qty,
            leverage=pos.leverage, pnl=net, pnl_pct=pnl_pct, fees=fee,
            open_ts=pos.open_ts, close_ts=ts_str(), hold_hours=hold,
            reason=reason, conviction=pos.conviction,
        )
        self.closed.append(ct)
        self.last_close_ts[symbol] = ts_str()
        emoji = "✅" if net > 0 else "❌"
        print(f"{emoji} 平仓 {symbol} {pos.side.upper()} @ {price:.4f} | {net:+.1f}U ({pnl_pct:+.1f}%) "
              f"| 持 {hold:.1f}h | {reason}")
        self._log_close(ct)

    # ---------------- 防猎杀: 止损被扫后快速收复的识别 ---------------- #
    def check_hunt(self, prices: Dict[str, float]) -> None:
        """被(非灾难)止损扫出后, 若价格在窗口内反向收复原止损位 -> 判定疑似猎杀:
        打印警示并解除该币冷却, 让正常信号逻辑可立即重进 (回应"打完止损就往上")。"""
        if not HUNT_REENTRY:
            return
        for sym in list(self.hunt_watch.keys()):
            w = self.hunt_watch[sym]
            px = prices.get(sym)
            if px is None:
                continue
            mins = (now_utc() - w["ts"]).total_seconds() / 60.0
            if mins > HUNT_WATCH_MIN:
                self.hunt_watch.pop(sym, None)           # 窗口过了仍没收复 = 真破位, 不是猎杀
                continue
            reclaim = HUNT_RECLAIM_ATR * w["atr"]
            recovered = ((w["side"] == "long" and px >= w["stop"] + reclaim)
                         or (w["side"] == "short" and px <= w["stop"] - reclaim))
            if recovered:
                self.last_close_ts.pop(sym, None)        # 解除冷却
                self.hunt_watch.pop(sym, None)
                print(f"🪤 疑似猎杀 {sym}: 止损被扫后 {mins:.0f}min 内收复 "
                      f"(现 {px:.4f} 越过原止损 {w['stop']:.4f} 达 {reclaim:.4f}) "
                      f"-> 已解除冷却, 原方向信号成立即可立即重进")

    # ---------------- 消息冲击: 逆风减险 (把多单止损收到保本) ---------------- #
    def derisk_for_news(self, prices: Dict[str, float], news: "NewsState") -> None:
        """新闻面 SHOCK(risk-off) 时立刻行动: 把所有多单止损收到保本线(不超过现价, 不裸夯市),
        多单变成"涨了锁利、跌回保本就走"。空单顺风不动。"""
        if not (NEWS_ACT_DERISK_LONGS and news.is_shock):
            return
        touched = []
        for sym, pos in self.positions.items():
            if pos.side != "long":
                continue
            px = prices.get(sym)
            if px is None:
                continue
            be = pos.entry + BREAKEVEN_LOCK_ATR * pos.atr
            new_stop = min(be, px)                       # 不抬到现价之上 -> 不会瞬间夯市
            if new_stop > pos.stop:                      # 只收紧, 不放松
                pos.stop = new_stop
                pos.breakeven = True
                pos.news_derisk = True
                touched.append(sym.split("/")[0])
        if touched:
            print(f"📰🔴 消息冲击(risk-off) -> 多单逆风减险, 止损收到保本: {', '.join(touched)} "
                  f"| {news.detail}")

    # ---------------- 交易日志 (CSV, 贴近账本格式) ---------------- #
    def _log_open(self, p: Position) -> None:
        new = not os.path.exists(TRADE_LOG)
        try:
            with open(TRADE_LOG, "a", encoding="utf-8") as fp:
                if new:
                    fp.write("事件,时间,币种,方向,杠杆,价格,张数,止损,共振,理由\n")
                fp.write(f"开仓,{p.open_ts},{p.symbol},{p.side},{p.leverage},{p.entry:.4f},"
                         f"{p.qty:.4f},{p.stop:.4f},{p.conviction},{p.open_reason}\n")
        except Exception:
            pass

    def _log_close(self, c: ClosedTrade) -> None:
        try:
            with open(TRADE_LOG, "a", encoding="utf-8") as fp:
                fp.write(f"平仓,{c.close_ts},{c.symbol},{c.side},{c.leverage},{c.exit:.4f},"
                         f"{c.qty:.4f},,{c.conviction},PnL={c.pnl:+.1f}U;持{c.hold_hours:.1f}h;{c.reason}\n")
        except Exception:
            pass

    # ---------------- 绩效 ---------------- #
    def stats(self) -> Dict:
        n = len(self.closed)
        if n == 0:
            return {"n": 0, "equity": self.equity}
        wins = [c for c in self.closed if c.pnl > 0]
        total = sum(c.pnl for c in self.closed)
        fees = sum(c.fees for c in self.closed)
        return {
            "n": n, "equity": self.equity,
            "winrate": len(wins) / n * 100.0,
            "total_pnl": total, "fees": fees,
            "ret_pct": (self.equity / INIT_CAPITAL - 1) * 100.0,
        }


# ==========================================================================
#   模块 6: 盯盘主循环 (调度: 取数 -> 盯仓 -> 找新机会 -> 看板)
# ==========================================================================
class Engine:
    _news_sensor: "Optional[NewsShockSensor]" = None   # 类属性: 实盘子类不调super也安全

    def __init__(self) -> None:
        self.md = MarketData()
        self.acct = PaperAccount()
        self.acct.load()
        self._regime: Optional[Regime] = None
        self._regime_ts: float = 0.0

    def news(self) -> "NewsState":
        """真·消息面姿态 (惰性建传感器, 内部自带 5min 缓存; 全程容错)。"""
        if not NEWS_SHOCK:
            return NewsState(detail="未启用")
        if self._news_sensor is None:
            self._news_sensor = NewsShockSensor()
        try:
            return self._news_sensor.state()
        except Exception:
            return NewsState(detail="新闻源异常(降级纯价格门)")

    def regime(self) -> Regime:
        """带缓存的市场周期判定 (日线变化慢, 每小时刷新一次)。"""
        if self._regime is None or time.time() - self._regime_ts > REGIME_REFRESH_SEC:
            self._regime = detect_regime(self.md)
            self._regime_ts = time.time()
        return self._regime

    def _macro_trend_up(self) -> bool:
        df = self.md.ohlcv(MACRO_SYMBOL, CONTEXT_TF, CONTEXT_BARS)
        if df is None or len(df) < 60:
            return True  # 取不到不误杀
        fast = df["close"].ewm(span=12, adjust=False).mean().iloc[-1]
        slow = df["close"].ewm(span=48, adjust=False).mean().iloc[-1]
        return bool(fast > slow)

    def cycle(self) -> None:
        fng_v, fng_s = self.md.fng()
        macro_up = self._macro_trend_up()
        regime = self.regime()
        news = self.news()
        prices: Dict[str, float] = {}
        feats: Dict[str, Features] = {}
        bar_close: Dict[str, float] = {}

        for sym in WATCHLIST:
            df15 = self.md.ohlcv(sym, SIGNAL_TF, SIGNAL_BARS)
            df4h = self.md.ohlcv(sym, CONTEXT_TF, CONTEXT_BARS)
            fund = self.md.funding(sym)
            f = compute_features(sym, df15, df4h, fund)
            price = self.md.last_price(sym)
            if f is None or price is None:
                continue
            prices[sym] = price
            feats[sym] = f
            # 用倒数第二根 15m 的收盘做"已确认收盘价"(最后一根可能未收盘)
            try:
                bar_close[sym] = float(df15["close"].iloc[-2])
            except Exception:
                bar_close[sym] = price

        # --- 0) 消息冲击优先: SHOCK(risk-off) 时立刻把多单逆风减险 (收到保本) --- #
        self.acct.derisk_for_news(prices, news)

        # --- 1) 先盯已有仓位 (风控优先) --- #
        for sym in list(self.acct.positions.keys()):
            if sym not in prices:
                continue
            pos = self.acct.positions[sym]
            reason = self.acct.manage(pos, prices[sym], bar_close.get(sym, prices[sym]))
            if reason:
                # 防猎杀: 记下被(非灾难/非止盈)止损扫出的位置, 后续快速收复=疑似猎杀
                if (HUNT_REENTRY and ("止损" in reason)
                        and ("灾难" not in reason) and ("回吐" not in reason)):
                    self.acct.hunt_watch[sym] = {"side": pos.side, "stop": pos.stop,
                                                 "atr": pos.atr, "ts": now_utc()}
                self.acct.close(sym, prices[sym], reason)

        # --- 1.5) 猎杀识别: 被扫后快速收复 -> 解除冷却(原方向信号成立可立即重进) --- #
        self.acct.check_hunt(prices)

        # --- 2) 再找新机会 --- #
        for sym, f in feats.items():
            if sym in self.acct.positions:
                continue
            sig = generate_signal(f, fng_v, macro_up, regime, news)
            if sig.side is None:
                continue
            locked, lk_why = self.acct.side_locked(sig.side)
            if locked:
                continue   # 连亏熔断: 该方向拉闸, 不开 (看板会显示原因)
            ok, _ = self.acct.can_open(sym)
            if ok:
                self.acct.open(sig, f)

        # --- 3) 结算 + 看板 --- #
        self.acct.mark_to_market(prices)
        self.acct.save()
        self._dashboard(prices, feats, fng_s, macro_up, regime, news)

    def _dashboard(self, prices, feats, fng_s, macro_up, regime, news=None) -> None:
        st = self.acct.stats()
        print("\n" + "=" * 78)
        print(f"🕒 {ts_str()}  |  净值 {self.acct.equity:,.2f} USDT "
              f"(收益 {(self.acct.equity/INIT_CAPITAL-1)*100:+.2f}%)  |  可用 {self.acct.cash:,.2f}")
        regime_face = {"BULL": "🐂牛市·偏多", "BEAR": "🐻熊市·偏空", "NEUTRAL": "🦀震荡·两边打"}[regime.label]
        print(f"   周期 {regime_face} ({regime.detail})")
        if news is not None and NEWS_SHOCK:
            posture = ("‹立刻行动: 禁新多单·多单收保本›" if news.is_shock
                       else ("‹警戒: 多单门槛+1›" if news.level == "WATCH" else "‹常规›"))
            print(f"   消息 {news.detail} {posture}")
        print(f"   性格 做空门槛≥{regime.short_th}{f'(仓×{regime.short_size_mult:g})' if regime.short_size_mult < 1.0 else ''}"
              f" · 做多门槛≥{regime.long_th}"
              f"{' · 多单可逢低' if not regime.long_needs_macro else ' · 多单须顺势'}"
              f"{' · 多单可长留' if regime.long_runner_ok else ''}"
              f"{' · 空单只快打' if not regime.short_runner_ok else ''}")
        print(f"   情绪 FNG={fng_s}  |  BTC 4h={'多头⬆' if macro_up else '空头⬇'}  "
              f"|  今日开仓 {self.acct.opens_today}/{MAX_OPENS_PER_DAY}")
        for _sd in ("long", "short"):
            _lk, _why = self.acct.side_locked(_sd)
            if _lk:
                print(f"   🔌 {_why}")
        if st["n"] > 0:
            print(f"   已平 {st['n']} 笔 | 胜率 {st['winrate']:.0f}% | 累计盈亏 {st['total_pnl']:+.1f}U "
                  f"| 手续费 {st['fees']:.1f}U")

        if self.acct.positions:
            print("   ── 持仓 " + "─" * 60)
            for sym, p in self.acct.positions.items():
                px = prices.get(sym, p.entry)
                fl = p.unrealized(px)
                tag = "📈runner" if p.trailing else ("波段" if p.runner else "短")
                if p.breakeven:
                    tag += "·已保本"
                if p.news_derisk:
                    tag += "·消息减险"
                print(f"   {sym:16} {p.side.upper():5} {p.leverage}x @ {p.entry:.4f} 现 {px:.4f} "
                      f"| 浮盈 {fl:+.1f}U ({p.unrealized_atr(px):+.1f}ATR) "
                      f"| 止损 {p.stop:.4f} | 持 {p.age_hours():.1f}h | {tag}")
        else:
            print("   (空仓观望)")

        # 候选雷达: 当前各币的共振分 (看得见为什么没开)
        radar = []
        for sym, f in feats.items():
            if sym in self.acct.positions:
                continue
            sig = generate_signal(f, self.md._fng_cache[1], macro_up, regime, news)
            mark = sig.side.upper() if sig.side else "—"
            radar.append(f"{sym.split('/')[0]}:{mark}{sig.conviction}")
        if radar:
            print("   雷达 " + " · ".join(radar))
        print("=" * 78)

    def run_loop(self, interval: int) -> None:
        print(f"🚀 Peter 独家引擎启动 | 盯盘 {len(WATCHLIST)} 币 | 间隔 {interval}s | Ctrl-C 停止")
        while True:
            try:
                rotate_logs()      # 每轮先体检日志, 超限自动瘦身
                self.cycle()
            except KeyboardInterrupt:
                print("\n🛑 收到停止信号, 已保存状态。")
                self.acct.save()
                break
            except Exception:
                print("⚠️ 本轮异常 (已跳过, 不中断盯盘):")
                traceback.print_exc()
            time.sleep(interval)

    def run_once(self) -> None:
        self.cycle()


# ==========================================================================
#   入口
# ==========================================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="Peter 独家量化引擎 (实时盯盘 + 模拟盘)")
    ap.add_argument("--once", action="store_true", help="只跑一轮 (调试/查看当前局面)")
    ap.add_argument("--interval", type=int, default=POLL_SECONDS, help=f"盯盘间隔秒数 (默认 {POLL_SECONDS})")
    ap.add_argument("--reset", action="store_true", help="清空模拟盘, 重置为初始本金")
    args = ap.parse_args()

    if args.reset:
        for f in (STATE_FILE, TRADE_LOG):
            if os.path.exists(f):
                os.remove(f)
        print("♻️ 已重置模拟盘。")

    if ccxt is None or pd is None or np is None:
        print("❌ 缺少 ccxt / pandas / numpy, 无法运行。请在 .venv 中安装。")
        return

    eng = Engine()
    if args.once:
        eng.run_once()
    else:
        eng.run_loop(args.interval)


if __name__ == "__main__":
    main()
