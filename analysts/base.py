"""分析师基类。"""
from abc import ABC, abstractmethod
from typing import Dict, Tuple

from astrbot.api import logger

from ..utils.stock_utils import StockUtils


class BaseAnalyst(ABC):
    """分析师基类"""
    
    def __init__(self, llm, data_fetcher):
        """
        初始化分析师
        
        Args:
            llm: LLM实例，需支持 async __call__
            data_fetcher: DataFetcher实例
        """
        self.llm = llm
        self.data_fetcher = data_fetcher
    
    @abstractmethod
    async def analyze(self, ticker: str, trade_date: str) -> str:
        """
        执行分析并返回报告
        
        Args:
            ticker: 股票代码
            trade_date: 交易日期 YYYY-MM-DD
            
        Returns:
            分析报告文本
        """
        pass

    def _resolve_ticker_info(self, ticker: str) -> Tuple[Dict, str, str]:
        """统一获取市场信息、标准化代码和股票名称。

        所有子类的 analyze / analyze_with_data 方法都需要这三项，
        抽取到基类避免每个子类重复调用。

        Returns:
            (market_info, normalized_ticker, stock_name)
        """
        market_info = StockUtils.get_market_info(ticker)
        normalized_ticker = market_info['normalized_ticker']
        stock_name = StockUtils.get_stock_name(ticker)
        return market_info, normalized_ticker, stock_name
    
    async def _call_llm(self, prompt: str) -> str:
        """调用LLM生成回复，异常向上传播由调用方处理。"""
        result = await self.llm(prompt)
        return result
    
    def _get_system_prompt(self) -> str:
        """获取系统提示词 - 子类可重写"""
        return """你是一位专业的金融分析师，负责分析股票并生成详细的投资报告。
请使用中文输出，使用Markdown格式。
重要数据用**加粗**标注。
"""
    
    def _build_analysis_prompt(self, ticker: str, trade_date: str, 
                               market_info: Dict, market_data: str = "",
                               fundamentals_data: str = "", news_data: str = "",
                               sentiment_data: str = "", extra_context: str = "",
                               is_etf: bool = False) -> str:
        """
        构建分析提示词 - 子类可重写
        
        Args:
            ticker: 股票代码
            trade_date: 交易日期
            market_info: 市场信息
            market_data: 市场数据
            fundamentals_data: 基本面数据
            news_data: 新闻数据
            sentiment_data: 情绪数据
            extra_context: 额外上下文
            is_etf: 是否为ETF
        """
        from ..utils.stock_utils import StockUtils
        
        stock_name = StockUtils.get_stock_name(ticker)
        market_name = market_info.get('market_name', '未知')
        currency = market_info.get('currency_name', '未知')
        currency_symbol = market_info.get('currency_symbol', '')
        
        # ETF模式下优先使用ETF专用系统提示词
        system_prompt = self._get_system_prompt()
        if is_etf and hasattr(self, '_get_etf_system_prompt'):
            etf_prompt = self._get_etf_system_prompt()
            if etf_prompt:
                system_prompt = etf_prompt
        
        prompt = f"""{system_prompt}

## 事实约束
- 只能使用下方数据源中明确提供的信息、数字和日期。
- 如果某项指标缺失、为 N/A 或数据不足，必须写“数据不足，无法判断”，禁止推测或编造具体数值。
- 禁止编造未来事件、财务指标、行业均值、支撑位、压力位或新闻事实。

## 分析对象
- {'基金' if is_etf else '股票'}名称: {stock_name}
- {'基金' if is_etf else '股票'}代码: {ticker}
- 所属市场: {market_name}
- 分析日期: {trade_date}
- 计价货币: {currency}（{currency_symbol}）

{extra_context}

"""
        
        if market_data:
            prompt += f"## 市场数据\n{market_data}\n\n"
        
        if fundamentals_data:
            prompt += f"## 基本面数据\n{fundamentals_data}\n\n"
        
        if news_data:
            prompt += f"## 新闻数据\n{news_data}\n\n"
        
        if sentiment_data:
            prompt += f"## 情绪数据\n{sentiment_data}\n\n"
        
        return prompt
