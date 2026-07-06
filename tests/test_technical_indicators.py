import sys
import unittest
from pathlib import Path

import pandas as pd


PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_tradingagents.utils.technical_indicators import (
    add_technical_indicators,
    format_technical_indicators,
    normalize_kline_df,
)


class TechnicalIndicatorsTest(unittest.TestCase):
    def test_calculates_common_indicators_from_normalized_kline(self):
        df = pd.DataFrame(
            {
                "date": pd.date_range("2026-01-01", periods=90, freq="D"),
                "open": [100 + i * 0.2 for i in range(90)],
                "high": [101 + i * 0.2 for i in range(90)],
                "low": [99 + i * 0.2 for i in range(90)],
                "close": [100 + i * 0.2 for i in range(90)],
                "volume": [1000000 + i for i in range(90)],
            }
        )

        result = add_technical_indicators(df)
        latest = result.iloc[-1]

        for column in [
            "ma5",
            "ma10",
            "ma20",
            "ma60",
            "macd_dif",
            "macd_dea",
            "macd",
            "rsi14",
            "kdj_k",
            "kdj_d",
            "kdj_j",
            "boll_mid",
            "boll_upper",
            "boll_lower",
        ]:
            self.assertIn(column, result.columns)
            self.assertFalse(pd.isna(latest[column]), column)

        self.assertAlmostEqual(latest["ma5"], sum(df["close"].tail(5)) / 5)

    def test_format_marks_insufficient_long_window_data(self):
        df = pd.DataFrame(
            {
                "date": pd.date_range("2026-01-01", periods=12, freq="D"),
                "open": [10 + i for i in range(12)],
                "high": [11 + i for i in range(12)],
                "low": [9 + i for i in range(12)],
                "close": [10 + i for i in range(12)],
                "volume": [1000 + i for i in range(12)],
            }
        )

        text = format_technical_indicators(add_technical_indicators(df), "¥")

        self.assertIn("### 技术指标", text)
        self.assertIn("MA5", text)
        self.assertIn("MA60", text)
        self.assertIn("N/A（数据不足）", text)

    def test_normalizes_chinese_and_yfinance_columns(self):
        china_df = pd.DataFrame(
            [
                {
                    "日期": "2026-07-01",
                    "开盘": 10,
                    "最高": 11,
                    "最低": 9,
                    "收盘": 10.5,
                    "成交量": 12345,
                }
            ]
        )
        yf_df = pd.DataFrame(
            {
                "Open": [10],
                "High": [11],
                "Low": [9],
                "Close": [10.5],
                "Volume": [12345],
            },
            index=[pd.Timestamp("2026-07-01")],
        )

        for normalized in [normalize_kline_df(china_df), normalize_kline_df(yf_df)]:
            self.assertEqual(
                ["date", "open", "high", "low", "close", "volume"],
                list(normalized.columns),
            )
            self.assertEqual(float(normalized.iloc[0]["close"]), 10.5)


if __name__ == "__main__":
    unittest.main()
