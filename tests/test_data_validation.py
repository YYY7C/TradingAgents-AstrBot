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
