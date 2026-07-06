import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd


class _Logger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_api_module.logger = _Logger()
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules.setdefault("astrbot.api", astrbot_api_module)

PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_tradingagents.data_fetcher import DataFetcher


class DataValidationTest(unittest.TestCase):
    def _make_us_fundamentals_fetcher(self, info, income=None, balance=None):
        fetcher = DataFetcher.__new__(DataFetcher)
        fetcher._akshare_available = False
        fetcher._yfinance_available = True

        stock = types.SimpleNamespace(
            income_stmt=income if income is not None else pd.DataFrame(),
            balance_sheet=balance if balance is not None else pd.DataFrame(),
        )

        async def fake_get_yfinance_info(_yf, _ticker, timeout=30):
            return stock, info

        fetcher._get_yfinance_info = fake_get_yfinance_info
        return fetcher

    def test_us_fundamentals_uses_yfinance_margin_aliases(self):
        info = {
            "shortName": "Marvell Technology, Inc.",
            "quoteType": "EQUITY",
            "trailingPE": 84.00342,
            "forwardPE": 39.7043,
            "priceToBook": 11.790521,
            "priceToSalesTrailing12Months": 24.61595,
            "enterpriseToEbitda": 79.651,
            "marketCap": 214580000000,
            "enterpriseValue": 216420000000,
            "trailingEps": 2.92,
            "forwardEps": 6.17792,
            "netIncomeToCommon": 2526700032,
            "totalRevenue": 8717100032,
            "grossMargins": 0.51502997,
            "operatingMargins": 0.14479999,
            "profitMargins": 0.28986,
        }
        fetcher = self._make_us_fundamentals_fetcher(info)
        fake_yfinance = types.ModuleType("yfinance")

        with patch.dict(sys.modules, {"yfinance": fake_yfinance}):
            data = asyncio.run(fetcher.get_fundamentals("MRVL", "2026-07-03"))

        self.assertIn("| 毛利率 | 51.50% |", data)
        self.assertIn("| 营业利润率 | 14.48% |", data)
        self.assertIn("| 净利率 | 28.99% |", data)

    def test_us_fundamentals_calculates_missing_margins_from_income_statement(self):
        info = {
            "shortName": "Marvell Technology, Inc.",
            "quoteType": "EQUITY",
            "trailingPE": 84.00342,
            "totalRevenue": 8194600000,
            "profitMargins": 0.28986,
        }
        income = pd.DataFrame(
            {
                pd.Timestamp("2026-01-31"): {
                    "Total Revenue": 8194600000,
                    "Gross Profit": 4180700000,
                    "Operating Income": 1338400000,
                }
            }
        )
        fetcher = self._make_us_fundamentals_fetcher(info, income=income)
        fake_yfinance = types.ModuleType("yfinance")

        with patch.dict(sys.modules, {"yfinance": fake_yfinance}):
            data = asyncio.run(fetcher.get_fundamentals("MRVL", "2026-07-03"))

        self.assertIn("| 毛利率 | 51.02% |", data)
        self.assertIn("| 营业利润率 | 16.33% |", data)

    def test_yfinance_fallback_success_text_is_valid_market_data(self):
        data = """## 美股市场数据

**股票代码**: DRAM

### 公司信息（yfinance）
| 指标 | 数值 |
|------|------|
| 公司名称 | Roundhill Memory ETF |

---
*数据来源: yfinance（akshare不可用时的备选）*
"""

        result = DataFetcher.__new__(DataFetcher)._check_data_valid(data, "市场数据")

        self.assertTrue(result["valid"], result)

    def test_sina_us_daily_data_formats_as_valid_market_data(self):
        hist_df = pd.DataFrame(
            [
                {
                    "date": pd.Timestamp("2026-07-01").date(),
                    "open": 67.66,
                    "high": 68.69,
                    "low": 65.80,
                    "close": 65.86,
                    "volume": 72237410,
                },
                {
                    "date": pd.Timestamp("2026-07-02").date(),
                    "open": 64.55,
                    "high": 65.75,
                    "low": 58.895,
                    "close": 60.63,
                    "volume": 98441114,
                },
            ]
        )
        fetcher = DataFetcher.__new__(DataFetcher)

        data = fetcher._format_us_sina_market_data(
            "DRAM",
            "2026-07-03",
            {"market_name": "美股", "currency_name": "美元", "currency_symbol": "$"},
            hist_df,
        )

        self.assertIn("新浪财经", data)
        self.assertIn("2026-07-02", data)
        self.assertTrue(fetcher._check_data_valid(data, "市场数据")["valid"])

    def test_tencent_kline_ignores_dividend_metadata_field(self):
        response = Mock()
        response.text = """{
            "code": 0,
            "msg": "",
            "data": {
                "sz000408": {
                    "qfqday": [
                        ["2026-04-17", "86.400", "84.850", "86.400", "84.100", "136692.000", {"FHcontent": "10派15元"}],
                        ["2026-04-18", "84.850", "85.200", "85.500", "84.200", "100000.000"]
                    ]
                }
            }
        }"""
        response.raise_for_status.return_value = None

        with patch("requests.get", return_value=response):
            df = DataFetcher.__new__(DataFetcher)._tencent_kline("000408", 90)

        self.assertEqual(["日期", "开盘", "收盘", "最高", "最低", "成交量"], list(df.columns))
        self.assertEqual(2, len(df))
        self.assertEqual("2026-04-17", df.iloc[0]["日期"])
        self.assertEqual(84.85, df.iloc[0]["收盘"])
        self.assertEqual(136692.0, df.iloc[0]["成交量"])


if __name__ == "__main__":
    unittest.main()
