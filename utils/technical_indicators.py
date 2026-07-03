"""通用技术指标计算工具。"""
from __future__ import annotations

from typing import Dict

import pandas as pd


REQUIRED_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


def normalize_kline_df(df: pd.DataFrame) -> pd.DataFrame:
    """将不同数据源的 K 线字段归一化为 date/open/high/low/close/volume。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=REQUIRED_COLUMNS)

    data = df.copy()
    if "date" not in data.columns and "日期" not in data.columns and "Date" not in data.columns:
        data = data.reset_index()

    column_map = {
        "date": ["date", "日期", "Date", "index"],
        "open": ["open", "开盘", "Open"],
        "high": ["high", "最高", "High"],
        "low": ["low", "最低", "Low"],
        "close": ["close", "收盘", "Close"],
        "volume": ["volume", "成交量", "Volume"],
    }

    normalized = pd.DataFrame()
    for target, candidates in column_map.items():
        source = next((name for name in candidates if name in data.columns), None)
        if source is None:
            normalized[target] = pd.NA
        else:
            normalized[target] = data[source]

    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    for column in ["open", "high", "low", "close", "volume"]:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")

    normalized = normalized.dropna(subset=["date", "close"]).sort_values("date")
    return normalized[REQUIRED_COLUMNS].reset_index(drop=True)


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1 / float(period), adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / float(period), adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.mask((avg_loss == 0) & (avg_gain > 0), 100)
    rsi = rsi.mask((avg_loss == 0) & (avg_gain == 0), 50)
    return rsi


def _kdj(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.DataFrame:
    low_min = low.rolling(window=9, min_periods=9).min()
    high_max = high.rolling(window=9, min_periods=9).max()
    rsv = (close - low_min) / (high_max - low_min).replace(0, pd.NA) * 100

    k_values = []
    d_values = []
    last_k = 50.0
    last_d = 50.0
    for value in rsv:
        if pd.isna(value):
            k_values.append(float("nan"))
            d_values.append(float("nan"))
            continue
        current_k = (2 / 3) * last_k + (1 / 3) * float(value)
        current_d = (2 / 3) * last_d + (1 / 3) * current_k
        k_values.append(current_k)
        d_values.append(current_d)
        last_k = current_k
        last_d = current_d

    k = pd.Series(k_values, index=close.index, dtype="float64")
    d = pd.Series(d_values, index=close.index, dtype="float64")
    j = 3 * k - 2 * d
    return pd.DataFrame({"kdj_k": k, "kdj_d": d, "kdj_j": j})


def add_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """为归一化 K 线数据添加常用技术指标。"""
    data = normalize_kline_df(df)
    if data.empty:
        return data

    close = data["close"]
    for period in [5, 10, 20, 60]:
        data[f"ma{period}"] = close.rolling(window=period, min_periods=period).mean()

    ema12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
    data["macd_dif"] = ema12 - ema26
    data["macd_dea"] = data["macd_dif"].ewm(span=9, adjust=False, min_periods=9).mean()
    data["macd"] = (data["macd_dif"] - data["macd_dea"]) * 2

    data["rsi14"] = _rsi(close, 14)

    kdj = _kdj(data["high"], data["low"], close)
    for column in kdj.columns:
        data[column] = kdj[column]

    boll_mid = close.rolling(window=20, min_periods=20).mean()
    boll_std = close.rolling(window=20, min_periods=20).std()
    data["boll_mid"] = boll_mid
    data["boll_upper"] = boll_mid + 2 * boll_std
    data["boll_lower"] = boll_mid - 2 * boll_std

    return data


def _fmt_number(value, decimals: int = 2, prefix: str = "") -> str:
    if pd.isna(value):
        return "N/A（数据不足）"
    return f"{prefix}{float(value):.{decimals}f}"


def latest_indicator_values(df: pd.DataFrame) -> Dict[str, str]:
    """返回最新一日指标值，便于测试或调用方自定义展示。"""
    data = add_technical_indicators(df)
    if data.empty:
        return {}
    latest = data.iloc[-1]
    return {
        "date": latest["date"].strftime("%Y-%m-%d") if hasattr(latest["date"], "strftime") else str(latest["date"]),
        "ma5": _fmt_number(latest.get("ma5")),
        "ma10": _fmt_number(latest.get("ma10")),
        "ma20": _fmt_number(latest.get("ma20")),
        "ma60": _fmt_number(latest.get("ma60")),
        "macd_dif": _fmt_number(latest.get("macd_dif"), 4),
        "macd_dea": _fmt_number(latest.get("macd_dea"), 4),
        "macd": _fmt_number(latest.get("macd"), 4),
        "rsi14": _fmt_number(latest.get("rsi14")),
        "kdj_k": _fmt_number(latest.get("kdj_k")),
        "kdj_d": _fmt_number(latest.get("kdj_d")),
        "kdj_j": _fmt_number(latest.get("kdj_j")),
        "boll_upper": _fmt_number(latest.get("boll_upper")),
        "boll_mid": _fmt_number(latest.get("boll_mid")),
        "boll_lower": _fmt_number(latest.get("boll_lower")),
    }


def format_technical_indicators(df: pd.DataFrame, currency_symbol: str = "") -> str:
    """格式化最新一日技术指标为 Markdown 表格。"""
    data = add_technical_indicators(df)
    if data.empty:
        return "### 技术指标\n暂无足够 K 线数据计算技术指标\n"

    latest = data.iloc[-1]
    latest_date = latest["date"].strftime("%Y-%m-%d") if hasattr(latest["date"], "strftime") else str(latest["date"])
    rows = [
        ("最新指标日期", latest_date),
        ("MA5", _fmt_number(latest.get("ma5"), prefix=currency_symbol)),
        ("MA10", _fmt_number(latest.get("ma10"), prefix=currency_symbol)),
        ("MA20", _fmt_number(latest.get("ma20"), prefix=currency_symbol)),
        ("MA60", _fmt_number(latest.get("ma60"), prefix=currency_symbol)),
        ("MACD DIF", _fmt_number(latest.get("macd_dif"), 4)),
        ("MACD DEA", _fmt_number(latest.get("macd_dea"), 4)),
        ("MACD柱", _fmt_number(latest.get("macd"), 4)),
        ("RSI14", _fmt_number(latest.get("rsi14"))),
        ("KDJ-K", _fmt_number(latest.get("kdj_k"))),
        ("KDJ-D", _fmt_number(latest.get("kdj_d"))),
        ("KDJ-J", _fmt_number(latest.get("kdj_j"))),
        ("BOLL上轨", _fmt_number(latest.get("boll_upper"), prefix=currency_symbol)),
        ("BOLL中轨", _fmt_number(latest.get("boll_mid"), prefix=currency_symbol)),
        ("BOLL下轨", _fmt_number(latest.get("boll_lower"), prefix=currency_symbol)),
    ]

    text = "### 技术指标（基于最近约 90 自然日 K 线计算）\n"
    text += "| 指标 | 数值 |\n|------|------|\n"
    for name, value in rows:
        text += f"| {name} | {value} |\n"
    return text
