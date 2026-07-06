"""
市场分析师 - 负责技术面分析
"""
from typing import Dict

from astrbot.api import logger

from .base import BaseAnalyst


class MarketAnalyst(BaseAnalyst):
    """市场分析师 - 复刻原始项目的市场分析师角色"""
    
    def __init__(self, llm, data_fetcher):
        super().__init__(llm, data_fetcher)
    
    def _get_system_prompt(self) -> str:
        return """你是一位专业的股票技术分析师，负责分析股票的技术面情况。

## 职责
1. 分析价格趋势（上涨/下跌/震荡）
2. 分析成交量变化
3. 分析均线系统（MA5/MA10/MA20/MA60）
4. 分析MACD、KDJ、RSI等技术指标
5. 识别支撑位和压力位
6. 给出技术面投资建议

## 输出格式
请使用以下格式输出分析报告：

### 技术面分析报告

## 📊 股票基本信息
- 公司名称：XXX
- 股票代码：XXX
- 所属市场：XXX

## 📈 技术指标分析
[在这里分析移动平均线、MACD、RSI、布林带等技术指标，提供具体数值]

## 📉 价格趋势分析
[在这里分析价格趋势，考虑市场特点]

## 🔍 支撑位与压力位
- 支撑位：XXX
- 压力位：XXX

## 💭 技术面投资建议
[在这里给出明确的投资建议：买入/持有/卖出]

## ⚠️ 技术面风险提示
[提示技术面上的风险点]

---
重要提醒：
- 必须使用上述格式输出，不要自创标题格式
- 所有价格数据使用指定货币单位表示
- 确保分析中正确使用公司名称和股票代码
- 不要在标题中使用"技术分析报告"等自创标题
- 如果有明确的技术面投资建议（买入/持有/卖出），请明确标注
- 必须优先使用市场数据中“技术指标”小节的真实计算值
- 如果指标为 N/A、数据不足或未提供，必须写“数据不足，无法判断”，禁止自行补算或编造数值
"""
    
    async def analyze(self, ticker: str, trade_date: str) -> str:
        """执行市场分析（自行获取数据）"""
        market_info, normalized_ticker, stock_name = self._resolve_ticker_info(ticker)
        logger.info(f"市场分析师开始分析: {ticker}")

        market_data = await self.data_fetcher.get_market_data(normalized_ticker, trade_date)

        prompt = self._build_analysis_prompt(
            ticker=normalized_ticker,
            trade_date=trade_date,
            market_info=market_info,
            market_data=market_data,
            extra_context=self._get_extra_context(stock_name, normalized_ticker, market_info)
        )

        report = await self._call_llm(prompt)
        logger.info(f"市场分析师完成分析: {ticker}, 报告长度: {len(report)}")
        return report

    async def analyze_with_data(self, ticker: str, trade_date: str, market_data_raw: str) -> str:
        """使用预获取的市场数据执行分析"""
        market_info, normalized_ticker, stock_name = self._resolve_ticker_info(ticker)
        logger.info(f"市场分析师开始分析（预取数据）: {ticker}")

        prompt = self._build_analysis_prompt(
            ticker=normalized_ticker,
            trade_date=trade_date,
            market_info=market_info,
            market_data=market_data_raw,
            extra_context=self._get_extra_context(stock_name, normalized_ticker, market_info)
        )

        report = await self._call_llm(prompt)
        logger.info(f"市场分析师完成分析: {ticker}, 报告长度: {len(report)}")
        return report

    def _get_extra_context(self, stock_name: str, ticker: str, market_info: Dict) -> str:
        """获取额外上下文"""
        return f"""## 分析要求
请重点关注 {stock_name}（{ticker}）的技术面分析要点：

1. **价格走势**：判断当前趋势（上升/下降/盘整）
2. **成交量配合**：分析成交量与价格走势的关系
3. **均线系统**：分析不同周期均线的交叉和排列
4. **技术指标**：MACD、KDJ、RSI等指标的信号
5. **支撑压力**：识别关键的支撑位和压力位

## 数据约束
- 优先引用“技术指标”小节中的真实计算值
- 缺失的技术指标不得自行推算具体数值
- 数据不足时明确写“数据不足，无法判断”

## 市场特点
- 市场类型：{market_info['market_name']}
- 货币单位：{market_info['currency_name']}（{market_info['currency_symbol']}）

请基于上述数据，进行专业的技术面分析。
"""
