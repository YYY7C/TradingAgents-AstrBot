"""统一数据获取模块。"""
import asyncio
from datetime import datetime, timedelta
from typing import Dict

from astrbot.api import logger


class DataFetcher:
    """统一数据获取器"""
    
    def __init__(self):
        self._akshare_available = None
        self._yfinance_available = None
        self._initialized = False
        self._use_curl_cffi = False
        self._curl_requests = None
        self._last_request_time = 0
        # 初始化 HTTP 适配层（不污染全局 requests）
        self._initialize_akshare()

    async def _run_blocking(self, func, *args, timeout: int = 30, **kwargs):
        """在线程池中执行阻塞调用并添加超时控制。"""
        return await asyncio.wait_for(
            asyncio.to_thread(func, *args, **kwargs),
            timeout=timeout,
        )

    async def _run_akshare(self, func, *args, timeout: int = 30, **kwargs):
        """在线程池中执行 akshare 调用（不修改全局 requests.get）。"""
        return await self._run_blocking(func, *args, timeout=timeout, **kwargs)

    def _format_technical_indicators(self, hist_df, currency_symbol: str) -> str:
        """格式化通用技术指标；失败时不影响主行情返回。"""
        try:
            from .utils.technical_indicators import format_technical_indicators

            return "\n" + format_technical_indicators(hist_df, currency_symbol) + "\n"
        except Exception as e:
            logger.warning(f"技术指标计算失败: {e}")
            return "\n### 技术指标\n计算失败，无法提供真实技术指标\n"

    def _make_yfinance_ticker(self, yf, ticker: str, use_requests_session: bool = False):
        """创建 yfinance Ticker；默认保留 yfinance 自带传输层，仅在指定时用 requests 兜底。"""
        if not use_requests_session:
            return yf.Ticker(ticker)

        try:
            import requests

            session = requests.Session()
            session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            })
            return yf.Ticker(ticker, session=session)
        except Exception as e:
            logger.warning(f"创建 yfinance requests Session 失败，使用默认 Ticker: {e}")
            return yf.Ticker(ticker)

    def _is_yfinance_transport_error(self, error: Exception) -> bool:
        """识别 yfinance/curl_cffi 在本机环境里的 TLS 传输初始化异常。"""
        message = str(error)
        transport_markers = (
            "curl: (35)",
            "TLS connect error",
            "OPENSSL_internal",
            "invalid library",
        )
        return any(marker in message for marker in transport_markers)

    async def _get_yfinance_info(self, yf, ticker: str, timeout: int = 30) -> tuple:
        """获取 yfinance info；默认使用 curl_cffi，遇到本机 TLS 异常才 requests 重试。"""
        stock = self._make_yfinance_ticker(yf, ticker)
        try:
            return stock, await self._run_blocking(lambda: stock.info, timeout=timeout)
        except Exception as e:
            if not self._is_yfinance_transport_error(e):
                raise

            logger.warning(
                f"yfinance默认传输层获取 {ticker} 失败，使用 requests Session 重试: {e}"
            )
            stock = self._make_yfinance_ticker(yf, ticker, use_requests_session=True)
            return stock, await self._run_blocking(lambda: stock.info, timeout=timeout)
    
    def _initialize_akshare(self):
        """
        初始化 AKShare 连接 - 使用 curl_cffi 绕过反爬虫（方案 B）。
        不再 patch 全局 requests.get，使用 curl_cffi 替代同步 _safe_get（已移除）。
        """
        if self._initialized:
            return
            
        try:
            # 尝试导入 curl_cffi
            try:
                from curl_cffi import requests as curl_requests
                self._use_curl_cffi = True
                self._curl_requests = curl_requests
                logger.info("🔧 检测到 curl_cffi，将使用它来模拟真实浏览器 TLS 指纹")
            except ImportError:
                self._use_curl_cffi = False
                self._curl_requests = None
                logger.warning("⚠️ curl_cffi 未安装，将使用标准 requests（可能被反爬虫拦截）")
                logger.warning("   建议安装: pip install curl-cffi")

            if self._use_curl_cffi:
                logger.info("🔧 AKShare 已配置 curl_cffi 模拟真实浏览器（Chrome 120）")
            else:
                logger.info("🔧 AKShare 已配置请求延迟和 headers 补丁（0.5秒）")

            self._initialized = True
            logger.info("✅ AKShare 初始化完成")

        except Exception as e:
            logger.error(f"❌ AKShare初始化失败: {e}")
            self._initialized = False  # 允许重试

    # ------------------------------------------------------------------
    # 腾讯接口备用数据源（东方财富 push2 被服务器 IP 封禁时的降级方案）
    # ------------------------------------------------------------------
    @staticmethod
    def _tencent_code_prefix(code: str) -> str:
        """根据 A 股代码判断市场前缀（sz / sh）"""
        if code.startswith(('6', '9', '5')):
            return 'sh'
        return 'sz'

    def _tencent_kline(self, code: str, days: int = 90) -> "pd.DataFrame":
        """通过腾讯接口获取 A 股前复权日 K 线，返回 DataFrame。
        
        列: 日期, 开盘, 收盘, 最高, 最低, 成交量
        """
        import requests, json
        import pandas as pd

        prefix = self._tencent_code_prefix(code)
        url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
        params = {'param': f'{prefix}{code},day,,,{days},qfq'}
        headers = {'User-Agent': 'Mozilla/5.0'}
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = json.loads(resp.text)

        stock_data = data.get('data', {}).get(f'{prefix}{code}', {})
        rows = stock_data.get('qfqday') or stock_data.get('day') or []

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=['日期', '开盘', '收盘', '最高', '最低', '成交量'])
        for col in ('开盘', '收盘', '最高', '最低'):
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df['成交量'] = pd.to_numeric(df['成交量'], errors='coerce')
        return df

    def _tencent_realtime(self, code: str) -> dict:
        """通过腾讯接口获取 A 股实时行情，返回解析后的字典。
        
        字段: 名称, 最新价, 昨收, 今开, 成交量, 成交额, 最高, 最低,
              涨跌额, 涨跌幅, 市盈率, 市净率, 总市值, 流通市值
        """
        import requests

        prefix = self._tencent_code_prefix(code)
        url = f'https://qt.gtimg.cn/q={prefix}{code}'
        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        resp.raise_for_status()
        raw = resp.text

        # 腾讯行情格式: v_sz002299="51~名称~代码~最新~昨收~..."
        parts = raw.split('~')
        if len(parts) < 50:
            return {}

        def _safe_float(idx):
            try:
                v = parts[idx].strip()
                return float(v) if v else None
            except (ValueError, IndexError):
                return None

        return {
            '名称': parts[1],
            '代码': parts[2],
            '最新价': _safe_float(3),
            '昨收': _safe_float(4),
            '今开': _safe_float(5),
            '成交量': _safe_float(6),
            '最高': _safe_float(33) or _safe_float(41),
            '最低': _safe_float(34) or _safe_float(42),
            '成交额': _safe_float(37),
            '涨跌额': _safe_float(31),
            '涨跌幅': _safe_float(32),
            '市盈率': _safe_float(39),
            '总市值': _safe_float(45),
            '流通市值': _safe_float(44),
        }

    def _tencent_stock_list(self) -> "pd.DataFrame":
        """通过腾讯接口获取 A 股列表（名称+代码），用于名称解析缓存。
        
        返回 DataFrame 列: 代码, 名称
        """
        import requests, json
        import pandas as pd

        all_rows = []
        for market in ('sh', 'sz'):
            # 腾讯批量行情接口，一次可取多只
            # 使用 market^A 获取对应市场全部A股
            url = f'https://qt.gtimg.cn/q={market}a'
            resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
            if resp.status_code != 200:
                continue
            raw = resp.text
            # 解析: v_sha...="...~名称~代码~..."
            for line in raw.split(';'):
                line = line.strip()
                if not line or '~' not in line:
                    continue
                parts = line.split('~')
                if len(parts) >= 3:
                    name = parts[1].strip()
                    code = parts[2].strip()
                    if name and code and len(code) == 6 and code.isdigit():
                        all_rows.append({'代码': code, '名称': name})

        return pd.DataFrame(all_rows) if all_rows else pd.DataFrame()
    
    @property
    def akshare_available(self) -> bool:
        """检查akshare是否可用"""
        if self._akshare_available is None:
            try:
                import akshare
                self._akshare_available = True
                logger.info("akshare 已可用")
            except ImportError:
                self._akshare_available = False
                logger.warning("akshare 未安装")
        return self._akshare_available
    
    @property
    def yfinance_available(self) -> bool:
        """检查yfinance是否可用"""
        if self._yfinance_available is None:
            try:
                import yfinance
                self._yfinance_available = True
                logger.info("yfinance 已可用")
            except ImportError:
                self._yfinance_available = False
                logger.warning("yfinance 未安装")
        return self._yfinance_available
    
    async def get_market_data(self, ticker: str, trade_date: str) -> str:
        """
        获取市场数据 - 复刻 get_stock_market_data_unified
        
        Args:
            ticker: 股票代码
            trade_date: 交易日期 YYYY-MM-DD
            
        Returns:
            格式化的市场数据文本
        """
        from .utils.stock_utils import StockUtils
        market_info = StockUtils.get_market_info(ticker)
        
        try:
            if market_info.get('is_etf'):
                return await self._get_etf_market_data(ticker, trade_date, market_info)
            elif market_info['is_china']:
                return await self._get_china_market_data(ticker, trade_date, market_info)
            elif market_info['is_hk']:
                return await self._get_hk_market_data(ticker, trade_date, market_info)
            else:
                return await self._get_us_market_data(ticker, trade_date, market_info)
        except Exception as e:
            logger.error(f"获取市场数据失败: {e}")
            return f"获取市场数据失败: {str(e)}"
    
    async def _get_china_market_data(self, ticker: str, trade_date: str, market_info: Dict) -> str:
        """获取A股市场数据（带重试机制 + 腾讯接口降级）"""
        if not self.akshare_available:
            return "akshare未安装，无法获取A股数据"
        
        from .utils.stock_utils import StockUtils
        import asyncio
        
        try:
            import akshare as ak
            import pandas as pd
            
            code = StockUtils.strip_market_prefix(ticker)
            
            end_date = datetime.strptime(trade_date, '%Y-%m-%d')
            start_date = end_date - timedelta(days=90)
            
            # 重试机制 - 最多尝试3次（akshare / 东方财富）
            df = None
            last_error = None
            use_tencent = False  # 标记是否使用了腾讯源
            
            for attempt in range(3):
                try:
                    logger.info(f"尝试获取A股数据 (尝试 {attempt + 1}/3): {code}")
                    # 使用 to_thread 在线程池中执行同步代码，避免阻塞事件循环
                    df = await self._run_akshare(
                        ak.stock_zh_a_hist,
                        symbol=code,
                        period="daily",
                        start_date=start_date.strftime('%Y%m%d'),
                        end_date=end_date.strftime('%Y%m%d'),
                        adjust="qfq",
                        timeout=30,
                    )
                    if df is not None and not df.empty:
                        logger.info(f"成功获取A股数据(东方财富): {code}")
                        break
                except asyncio.TimeoutError:
                    last_error = "获取数据超时"
                    logger.warning(f"尝试 {attempt + 1} 超时")
                except Exception as e:
                    last_error = str(e)
                    logger.warning(f"尝试 {attempt + 1} 失败: {e}")
                
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)  # 指数退避
            
            # ---------- 降级: 使用腾讯接口 ----------
            if df is None or df.empty:
                logger.info(f"东方财富接口不可用，尝试腾讯接口获取A股数据: {code}")
                try:
                    df = await self._run_blocking(self._tencent_kline, code, 90)
                    if df is not None and not df.empty:
                        use_tencent = True
                        # 添加「涨跌幅」列（腾讯 K 线不含此列）
                        if '涨跌幅' not in df.columns:
                            prev_close = df['收盘'].shift(1)
                            df['涨跌幅'] = ((df['收盘'] - prev_close) / prev_close * 100).round(2)
                        # 添加「成交额」列（近似 = 收盘 * 成交量）
                        if '成交额' not in df.columns:
                            df['成交额'] = (df['收盘'] * df['成交量']).round(2)
                        logger.info(f"成功获取A股数据(腾讯接口): {code}, {len(df)}条")
                except Exception as e:
                    logger.warning(f"腾讯接口也失败: {e}")
                    last_error = f"东方财富: {last_error}; 腾讯: {e}"
            
            if df is None or df.empty:
                return f"""## A股市场数据

**股票代码**: {code}
**交易日期**: {trade_date}
**市场**: {market_info['market_name']}
**状态**: 数据获取失败

### 提示
无法从数据源获取{code}的行情数据。
原因: {last_error or '未知错误'}

**建议**: 
1. 检查网络连接
2. 稍后重试
3. 股票代码可能不存在或已停牌

---
*数据来源: akshare / 腾讯财经*"""
            
            # 获取最新数据
            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else latest
            
            # 计算涨跌
            price_change = latest['收盘'] - prev['收盘']
            pct_change = (price_change / prev['收盘'] * 100) if prev['收盘'] != 0 else 0
            
            source_tag = "腾讯财经" if use_tencent else "东方财富"
            result = f"""## A股市场数据

**股票代码**: {code}
**交易日期**: {trade_date}
**市场**: {market_info['market_name']}
**交易所**: {market_info['exchange']}
**货币**: {market_info['currency_name']}（{market_info['currency_symbol']}）
**数据源**: {source_tag}

### 近期行情
| 日期 | 开盘 | 收盘 | 最高 | 最低 | 成交量 | 成交额 | 涨跌幅 |
|------|------|------|------|------|--------|--------|--------|
| {latest['日期']} | {latest['开盘']} | {latest['收盘']} | {latest['最高']} | {latest['最低']} | {latest['成交量']} | {latest.get('成交额', 'N/A')} | {pct_change:.2f}% |

### 最近5个交易日
"""
            
            for idx, row in df.tail(5).iterrows():
                _pct = row['涨跌幅']
                _pct_str = f"{_pct:+.2f}" if isinstance(_pct, (int, float)) else str(_pct)
                result += f"- **{row['日期']}**: 收{row['收盘']} ({_pct_str}%)\n"

            result += self._format_technical_indicators(df, market_info['currency_symbol'])
            
            # 获取实时行情
            if use_tencent:
                # 使用腾讯接口获取实时行情
                try:
                    rt = await self._run_blocking(self._tencent_realtime, code)
                    if rt:
                        result += f"""
### 实时行情
| 指标 | 数值 |
|------|------|
| 最新价 | {rt.get('最新价', 'N/A')} |
| 涨跌额 | {rt.get('涨跌额', 'N/A')} |
| 涨跌幅 | {rt.get('涨跌幅', 'N/A')}% |
| 成交量 | {rt.get('成交量', 'N/A')} |
| 成交额 | {rt.get('成交额', 'N/A')} |
| 最高 | {rt.get('最高', 'N/A')} |
| 最低 | {rt.get('最低', 'N/A')} |
| 今开 | {rt.get('今开', 'N/A')} |
| 昨收 | {rt.get('昨收', 'N/A')} |
| 市盈率 | {rt.get('市盈率', 'N/A')} |
| 总市值 | {rt.get('总市值', 'N/A')} |
| 流通市值 | {rt.get('流通市值', 'N/A')} |
"""
                except Exception as e:
                    logger.warning(f"腾讯实时行情获取失败: {e}")
            else:
                # 使用 akshare 获取实时行情（东方财富）
                try:
                    spot_df = await self._run_akshare(ak.stock_zh_a_spot_em, timeout=15)
                    spot_result = spot_df[spot_df['代码'] == code]
                    if not spot_result.empty:
                        s = spot_result.iloc[0]
                        result += f"""
### 实时行情
| 指标 | 数值 |
|------|------|
| 最新价 | {s.get('最新价', 'N/A')} |
| 涨跌额 | {s.get('涨跌额', 'N/A')} |
| 涨跌幅 | {s.get('涨跌幅', 'N/A')}% |
| 成交量 | {s.get('成交量', 'N/A')} |
| 成交额 | {s.get('成交额', 'N/A')} |
| 最高 | {s.get('最高', 'N/A')} |
| 最低 | {s.get('最低', 'N/A')} |
| 今开 | {s.get('今开', 'N/A')} |
| 昨收 | {s.get('昨收', 'N/A')} |
| 市盈率(动态) | {s.get('市盈率-动态', 'N/A')} |
| 市净率 | {s.get('市净率', 'N/A')} |
| 总市值 | {s.get('总市值', 'N/A')} |
| 流通市值 | {s.get('流通市值', 'N/A')} |
"""
                except Exception as e:
                    # 东方财富实时接口也被封，降级到腾讯
                    logger.warning(f"东方财富实时行情失败，降级腾讯: {e}")
                    try:
                        rt = await self._run_blocking(self._tencent_realtime, code)
                        if rt:
                            result += f"""
### 实时行情
| 指标 | 数值 |
|------|------|
| 最新价 | {rt.get('最新价', 'N/A')} |
| 涨跌额 | {rt.get('涨跌额', 'N/A')} |
| 涨跌幅 | {rt.get('涨跌幅', 'N/A')}% |
| 成交量 | {rt.get('成交量', 'N/A')} |
| 成交额 | {rt.get('成交额', 'N/A')} |
| 最高 | {rt.get('最高', 'N/A')} |
| 最低 | {rt.get('最低', 'N/A')} |
| 今开 | {rt.get('今开', 'N/A')} |
| 昨收 | {rt.get('昨收', 'N/A')} |
| 市盈率 | {rt.get('市盈率', 'N/A')} |
| 总市值 | {rt.get('总市值', 'N/A')} |
| 流通市值 | {rt.get('流通市值', 'N/A')} |
"""
                    except Exception as e2:
                        logger.warning(f"腾讯实时行情也失败: {e2}")
            
            return result
            
        except ImportError:
            return "akshare未安装，请运行: pip install akshare"
        except Exception as e:
            return f"获取A股数据失败: {str(e)}"
    
    def _pad_hk_code(self, code: str) -> str:
        """将港股代码补零到5位（akshare stock_hk_hist 需要）。
        例如: '0700' -> '00700', '9988' -> '09988', '00700' -> '00700'
        """
        code = code.lstrip('0') if code.startswith('0') and len(code) > 4 else code
        return code.zfill(5)
    
    async def _get_hk_market_data(self, ticker: str, trade_date: str, market_info: Dict) -> str:
        """获取港股市场数据（akshare实时行情 + 历史K线）"""
        if not self.akshare_available:
            return "akshare未安装，无法获取港股数据"
        
        try:
            import akshare as ak
            import pandas as pd
            
            # 提取纯数字代码（0700.HK → 0700）
            code = ticker.replace('.HK', '').replace('HK', '').replace('.', '')
            # akshare stock_hk_spot_em 使用原始代码（如 0700）
            # akshare stock_hk_hist 需要5位补零代码（如 00700）
            padded_code = self._pad_hk_code(code)
            
            # === 1. 获取实时行情 ===
            realtime_text = ""
            try:
                spot_df = await self._run_akshare(ak.stock_hk_spot_em, timeout=30)
                spot_result = spot_df[spot_df['代码'] == code]
                
                if spot_result is None or spot_result.empty:
                    # 尝试用补零代码匹配
                    spot_result = spot_df[spot_df['代码'] == padded_code]
                
                if spot_result is not None and not spot_result.empty:
                    row = spot_result.iloc[0]
                    realtime_text = f"""### 实时行情
| 指标 | 数值 |
|------|------|
| 股票名称 | {row.get('名称', 'N/A')} |
| 最新价 | {row.get('最新价', row.get('现价', 'N/A'))} |
| 涨跌额 | {row.get('涨跌额', 'N/A')} |
| 涨跌幅 | {row.get('涨跌幅', 'N/A')}% |
| 今开 | {row.get('今开', 'N/A')} |
| 最高 | {row.get('最高', 'N/A')} |
| 最低 | {row.get('最低', 'N/A')} |
| 昨收 | {row.get('昨收', 'N/A')} |
| 成交量 | {row.get('成交量', 'N/A')} |
| 成交额 | {row.get('成交额', 'N/A')} |
| 市盈率 | {row.get('市盈率', 'N/A')} |
| 总市值 | {row.get('总市值', 'N/A')} |
"""
                else:
                    realtime_text = "### 实时行情\n暂无实时行情数据\n"
            except Exception as e:
                logger.warning(f"获取港股实时行情失败: {e}")
                realtime_text = f"### 实时行情\n获取失败: {str(e)}\n"
            
            # === 2. 获取历史K线数据 ===
            hist_text = ""
            try:
                end_date = datetime.strptime(trade_date, '%Y-%m-%d')
                start_date = end_date - timedelta(days=90)  # 覆盖 MA60 等常用技术指标
                
                hist_df = await self._run_akshare(
                    ak.stock_hk_hist,
                    symbol=padded_code,
                    period="daily",
                    start_date=start_date.strftime('%Y%m%d'),
                    end_date=end_date.strftime('%Y%m%d'),
                    adjust="qfq",
                    timeout=30,
                )
                
                if hist_df is not None and not hist_df.empty:
                    # 最近5个交易日摘要
                    hist_text = "### 最近5个交易日\n"
                    for idx, row in hist_df.tail(5).iterrows():
                        date_str = row.get('日期', 'N/A')
                        close = row.get('收盘', 'N/A')
                        pct = row.get('涨跌幅', 'N/A')
                        _pct_str = f"{pct:+.2f}" if isinstance(pct, (int, float)) else str(pct)
                        hist_text += f"- **{date_str}**: 收{close} ({_pct_str}%)\n"
                    
                    # 近期行情表（最新一天）
                    latest = hist_df.iloc[-1]
                    hist_text += f"""
### 近期行情
| 日期 | 开盘 | 收盘 | 最高 | 最低 | 成交量 | 成交额 | 涨跌幅 |
|------|------|------|------|------|--------|--------|--------|
| {latest.get('日期', 'N/A')} | {latest.get('开盘', 'N/A')} | {latest.get('收盘', 'N/A')} | {latest.get('最高', 'N/A')} | {latest.get('最低', 'N/A')} | {latest.get('成交量', 'N/A')} | {latest.get('成交额', 'N/A')} | {latest.get('涨跌幅', 'N/A')}% |
"""
                    hist_text += self._format_technical_indicators(hist_df, market_info['currency_symbol'])
                else:
                    hist_text = "### 历史K线\n暂无历史K线数据\n"
            except Exception as e:
                logger.warning(f"获取港股历史K线失败: {e}")
                hist_text = f"### 历史K线\n获取失败: {str(e)}\n"
            
            if not realtime_text and not hist_text:
                return f"暂无港股{code}的行情数据"
            
            return f"""## 港股市场数据

**股票代码**: {code}.HK
**交易日期**: {trade_date}
**市场**: {market_info['market_name']}
**货币**: {market_info['currency_name']}（{market_info['currency_symbol']}）

{realtime_text}
{hist_text}
---
*数据来源: akshare（东方财富）*
"""
                
        except ImportError:
            return "akshare未安装"
        except Exception as e:
            return f"获取港股数据失败: {str(e)}"
    

    def _format_us_sina_market_data(self, ticker: str, trade_date: str, market_info: Dict, hist_df) -> str:
        """格式化 akshare 新浪财经美股日线数据。"""
        import pandas as pd

        if hist_df is None or hist_df.empty:
            return "新浪财经美股日线数据为空"

        df = hist_df.copy()
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'], errors='coerce')
            df = df.dropna(subset=['date']).sort_values('date')
            end_date = pd.to_datetime(trade_date, errors='coerce')
            if pd.notna(end_date):
                df = df[df['date'] <= end_date]
        else:
            df = df.sort_index()
            end_date = pd.to_datetime(trade_date, errors='coerce')
            if pd.notna(end_date):
                df = df[df.index <= end_date]

        if df.empty:
            return "新浪财经美股日线数据为空"

        latest = df.iloc[-1]

        def _fmt_money(value):
            try:
                return f"${float(value):.2f}"
            except Exception:
                return str(value) if value is not None else "N/A"

        def _fmt_number(value):
            try:
                return f"{float(value):,.0f}"
            except Exception:
                return str(value) if value is not None else "N/A"

        def _row_date(row):
            if 'date' in row.index:
                value = row.get('date')
                if hasattr(value, 'strftime'):
                    return value.strftime('%Y-%m-%d')
                return str(value)
            idx = row.name
            return idx.strftime('%Y-%m-%d') if hasattr(idx, 'strftime') else str(idx)

        latest_date = _row_date(latest)
        latest_close = latest.get('close', 'N/A')

        hist_text = "### 最近5个交易日\n"
        close_pct = df['close'].pct_change() * 100 if 'close' in df.columns else None
        for idx, row in df.tail(5).iterrows():
            pct = close_pct.loc[idx] if close_pct is not None and idx in close_pct.index else 0
            pct = 0 if pct != pct else pct
            pct_str = f"{pct:+.2f}" if isinstance(pct, (int, float)) else str(pct)
            hist_text += f"- **{_row_date(row)}**: 收{_fmt_money(row.get('close', 'N/A'))} ({pct_str}%)\n"
        technical_text = self._format_technical_indicators(df, market_info['currency_symbol'])

        return f"""## 美股市场数据

**股票代码**: {ticker}
**交易日期**: {trade_date}
**市场**: {market_info['market_name']}
**货币**: {market_info['currency_name']}（{market_info['currency_symbol']}）

### 日线行情（akshare 新浪财经）
| 指标 | 数值 |
|------|------|
| 最新交易日 | {latest_date} |
| 开盘 | {_fmt_money(latest.get('open', 'N/A'))} |
| 最高 | {_fmt_money(latest.get('high', 'N/A'))} |
| 最低 | {_fmt_money(latest.get('low', 'N/A'))} |
| 收盘 | {_fmt_money(latest_close)} |
| 成交量 | {_fmt_number(latest.get('volume', 'N/A'))} |

{hist_text}
{technical_text}
---
*数据来源: akshare（新浪财经 stock_us_daily）*
"""

    async def _get_us_market_data(self, ticker: str, trade_date: str, market_info: Dict) -> str:
        """获取美股市场数据（akshare主数据源 + 新浪日线兜底 + yfinance备选）"""
        
        # === 1. 尝试 akshare 作为主数据源 ===
        if self.akshare_available:
            try:
                import akshare as ak
                import pandas as pd
                
                # 获取实时行情
                spot_df = await self._run_akshare(ak.stock_us_spot_em, timeout=30)
                
                if spot_df is not None and not spot_df.empty:
                    # 匹配美股代码（akshare格式: "105.AAPL"）
                    matched = spot_df[spot_df['代码'].str.endswith(f'.{ticker}', na=False)]
                    
                    if not matched.empty:
                        row = matched.iloc[0]
                        ak_code = row['代码']  # 完整的 akshare 代码

                        def _row_first(*names):
                            for name in names:
                                value = row.get(name, None)
                                if value is not None:
                                    return value
                            return 'N/A'
                        
                        realtime_text = f"""### 实时行情（akshare）
| 指标 | 数值 |
|------|------|
| 股票名称 | {row.get('名称', 'N/A')} |
| 最新价 | ${_row_first('最新价', '现价')} |
| 涨跌额 | {row.get('涨跌额', 'N/A')} |
| 涨跌幅 | {row.get('涨跌幅', 'N/A')}% |
| 今开 | {_row_first('今开', '开盘价')} |
| 最高 | {_row_first('最高', '最高价')} |
| 最低 | {_row_first('最低', '最低价')} |
| 昨收 | {_row_first('昨收', '昨收价')} |
| 成交量 | {row.get('成交量', 'N/A')} |
| 成交额 | {row.get('成交额', 'N/A')} |
| 总市值 | {row.get('总市值', 'N/A')} |
"""
                        
                        # 获取历史K线
                        hist_text = ""
                        try:
                            end_date = datetime.strptime(trade_date, '%Y-%m-%d')
                            start_date = end_date - timedelta(days=90)
                            
                            hist_df = await self._run_akshare(
                                ak.stock_us_hist,
                                symbol=ak_code,
                                period="daily",
                                start_date=start_date.strftime('%Y%m%d'),
                                end_date=end_date.strftime('%Y%m%d'),
                                adjust="qfq",
                                timeout=30,
                            )
                            
                            if hist_df is not None and not hist_df.empty:
                                hist_text = "\n### 最近5个交易日\n"
                                for idx, hrow in hist_df.tail(5).iterrows():
                                    date_str = hrow.get('日期', 'N/A')
                                    close = hrow.get('收盘', 'N/A')
                                    pct = hrow.get('涨跌幅', 'N/A')
                                    _pct_str = f"{pct:+.2f}" if isinstance(pct, (int, float)) else str(pct)
                                    hist_text += f"- **{date_str}**: 收${close} ({_pct_str}%)\n"
                                hist_text += self._format_technical_indicators(hist_df, market_info['currency_symbol'])
                        except Exception as e:
                            logger.warning(f"获取美股历史K线失败(akshare): {e}")
                            hist_text = "\n### 历史K线\n获取失败\n"
                        
                        return f"""## 美股市场数据

**股票代码**: {ticker}
**交易日期**: {trade_date}
**市场**: {market_info['market_name']}
**货币**: {market_info['currency_name']}（{market_info['currency_symbol']}）

{realtime_text}
{hist_text}
---
*数据来源: akshare（东方财富）*
"""
            except Exception as e:
                logger.warning(f"akshare东方财富获取美股数据失败，尝试新浪财经日线接口: {e}")

            try:
                import akshare as ak

                hist_df = await self._run_akshare(
                    ak.stock_us_daily,
                    symbol=ticker,
                    adjust="qfq",
                    timeout=30,
                )
                return self._format_us_sina_market_data(ticker, trade_date, market_info, hist_df)
            except Exception as e:
                logger.warning(f"akshare新浪获取美股日线失败，回退到yfinance: {e}")
        
        # === 2. 回退到 yfinance ===
        if not self.yfinance_available:
            return "akshare和yfinance均不可用，无法获取美股数据"
        
        try:
            import yfinance as yf
            
            stock, info = await self._get_yfinance_info(yf, ticker, timeout=30)
            
            # 获取历史数据
            end_date = datetime.strptime(trade_date, '%Y-%m-%d')
            start_date = end_date - timedelta(days=90)
            hist = await self._run_blocking(stock.history, start=start_date, end=end_date, timeout=30)
            
            hist_text = ""
            if hist is not None and not hist.empty:
                # 预计算涨跌幅（Series 级别），避免在 iterrows 行上调用 pct_change()
                close_pct = hist['Close'].pct_change() * 100
                hist_text = "### 最近5个交易日\n"
                for idx, row in hist.tail(5).iterrows():
                    date_str = idx.strftime('%Y-%m-%d')
                    close = row['Close']
                    pct = close_pct.loc[idx] if idx in close_pct.index else 0
                    pct = 0 if (pct != pct) else pct  # NaN guard
                    _pct_str = f"{pct:+.2f}" if isinstance(pct, (int, float)) else str(pct)
                    hist_text += f"- **{date_str}**: 收${close:.2f} ({_pct_str}%)\n"
                hist_text += self._format_technical_indicators(hist, market_info['currency_symbol'])
            
            return f"""## 美股市场数据

**股票代码**: {ticker}
**交易日期**: {trade_date}
**市场**: {market_info['market_name']}
**货币**: {market_info['currency_name']}（{market_info['currency_symbol']}）

### 公司信息（yfinance）
| 指标 | 数值 |
|------|------|
| 公司名称 | {info.get('shortName', info.get('longName', 'N/A'))} |
| 当前价格 | ${info.get('currentPrice', info.get('regularMarketPrice', 'N/A'))} |
| 今日开盘 | ${info.get('regularMarketOpen', 'N/A')} |
| 今日最高 | ${info.get('dayHigh', info.get('regularMarketDayHigh', 'N/A'))} |
| 今日最低 | ${info.get('dayLow', info.get('regularMarketDayLow', 'N/A'))} |
| 52周最高 | ${info.get('fiftyTwoWeekHigh', 'N/A')} |
| 52周最低 | ${info.get('fiftyTwoWeekLow', 'N/A')} |
| 成交量 | {info.get('volume', info.get('regularMarketVolume', 'N/A')):,} |
| 总市值 | ${format(info.get('marketCap'), ',.0f') if info.get('marketCap') else 'N/A'} |
| 市盈率(TTM) | {info.get('trailingPE', 'N/A')} |
| 市净率 | {info.get('priceToBook', 'N/A')} |
| 股息收益率 | {info.get('dividendYield', 'N/A') or 'N/A'} |
| EPS | ${info.get('trailingEps', 'N/A')} |

{hist_text}
---
*数据来源: yfinance（akshare不可用时的备选）*
"""
        except ImportError:
            return "yfinance未安装，请运行: pip install yfinance"
        except Exception as e:
            return f"获取美股数据失败: {str(e)}"
    
    async def get_fundamentals(self, ticker: str, trade_date: str) -> str:
        """
        获取基本面数据 - 复刻 get_stock_fundamentals_unified
        """
        from .utils.stock_utils import StockUtils
        market_info = StockUtils.get_market_info(ticker)
        
        try:
            if market_info.get('is_etf'):
                return await self._get_etf_fundamentals(ticker, trade_date, market_info)
            elif market_info['is_china']:
                return await self._get_china_fundamentals(ticker, trade_date, market_info)
            elif market_info['is_hk']:
                return await self._get_hk_fundamentals(ticker, trade_date, market_info)
            else:
                return await self._get_us_fundamentals(ticker, trade_date, market_info)
        except Exception as e:
            logger.error(f"获取基本面数据失败: {e}")
            return f"获取基本面数据失败: {str(e)}"
    
    async def _get_china_fundamentals(self, ticker: str, trade_date: str, market_info: Dict) -> str:
        """获取A股基本面数据"""
        if not self.akshare_available:
            return "akshare未安装，无法获取A股基本面数据"
        
        from .utils.stock_utils import StockUtils
        
        try:
            import akshare as ak
            import pandas as pd
            
            code = StockUtils.strip_market_prefix(ticker)
            
            try:
                # 使用 asyncio.to_thread 在线程池中执行同步API
                # 获取主要财务指标 - stock_financial_abstract 包含所有关键指标
                main_indicators = await self._run_akshare(
                    ak.stock_financial_abstract,
                    symbol=code,
                    timeout=30,
                )
                
                # 构建结果
                result = f"""## A股基本面数据

**股票代码**: {code}
**分析日期**: {trade_date}

"""
                
                valid_indicator_count = 0
                if main_indicators is not None and not main_indicators.empty:
                    # 找到最新一期数据的列
                    cols = main_indicators.columns.tolist()
                    # 跳过 '选项' 和 '指标' 列，最新数据在第3列
                    latest_col = None
                    for col in cols[2:4]:  # 使用最近两期中的一期
                        if '20' in str(col):
                            latest_col = col
                            break
                    
                    if latest_col:
                        result += f"### 财务指标（报告期: {latest_col}）\n\n"
                        result += "| 指标 | 数值 |\n|------|------|\n"
                        
                        # 关键指标列表
                        key_indicators = [
                            ('每股收益', '每股收益'),
                            ('净资产收益率(ROE)', '净资产收益率(ROE)'),
                            ('销售毛利率', '销售毛利率'),
                            ('销售净利率', '销售净利率'),
                            ('资产负债率', '资产负债率'),
                            ('流动比率', '流动比率'),
                            ('速动比率', '速动比率'),
                            ('营业总收入', '营业总收入'),
                            ('净利润', '净利润'),
                            ('扣非净利润', '扣非净利润'),
                            ('经营现金流净额', '经营现金流净额'),
                            ('加权平均ROE', '加权平均净资产收益率'),
                        ]
                        
                        for display_name, data_name in key_indicators:
                            row = main_indicators[main_indicators['指标'] == data_name]
                            if not row.empty:
                                val = row[latest_col].values[0]
                                if pd.notna(val):
                                    valid_indicator_count += 1
                                    if isinstance(val, (int, float)):
                                        if abs(val) > 1e8:  # 亿
                                            result += f"| {display_name} | {val/1e8:.2f}亿 |\n"
                                        elif abs(val) > 1e4:  # 万
                                            result += f"| {display_name} | {val/1e4:.2f}万 |\n"
                                        else:
                                            result += f"| {display_name} | {val:.4f} |\n"
                                    else:
                                        result += f"| {display_name} | {val} |\n"
                                else:
                                    result += f"| {display_name} | N/A |\n"
                            else:
                                result += f"| {display_name} | N/A |\n"

                if valid_indicator_count == 0:
                    return "无法获取A股基本面数据: 财务指标为空"
                 
                return result
                
            except Exception as e:
                logger.error(f"获取A股基本面数据失败: {e}")
                return f"获取A股基本面数据失败: {str(e)}"
                
        except ImportError:
            return "akshare未安装"
        except Exception as e:
            return f"获取A股基本面失败: {str(e)}"
    
    async def _get_hk_fundamentals(self, ticker: str, trade_date: str, market_info: Dict) -> str:
        """获取港股基本面数据（akshare公司信息 + yfinance估值/财务）"""
        code = ticker.replace('.HK', '').replace('HK', '').replace('.', '')
        padded_code = self._pad_hk_code(code)
        
        result_parts = []
        result_parts.append(f"""## 港股基本面数据

**股票代码**: {code}.HK
**分析日期**: {trade_date}
""")
        
        # === 1. akshare: 雪球公司基本信息 ===
        if self.akshare_available:
            try:
                import akshare as ak
                
                info_df = await self._run_akshare(
                    ak.stock_individual_basic_info_hk_xq,
                    symbol=padded_code,
                    timeout=30,
                )
                
                if info_df is not None and not info_df.empty:
                    # 雪球返回的是 item-value 两列格式
                    info_dict = dict(zip(info_df.iloc[:, 0], info_df.iloc[:, 1]))
                    
                    company_text = "### 公司基本信息（雪球）\n| 指标 | 数值 |\n|------|------|\n"
                    
                    key_fields = {
                        '公司名称': '公司名称',
                        '所属行业': '所属行业',
                        '上市日期': '上市日期',
                        '总股本': '总股本',
                        '流通股本': '流通股本',
                        '市值': '市值',
                    }
                    for display, key in key_fields.items():
                        val = info_dict.get(key, 'N/A')
                        if val and str(val) != 'nan':
                            company_text += f"| {display} | {val} |\n"
                    
                    # 添加所有其他字段
                    for item, value in info_dict.items():
                        if item not in key_fields.values() and value and str(value) != 'nan':
                            company_text += f"| {item} | {value} |\n"
                    
                    result_parts.append(company_text)
                else:
                    result_parts.append("### 公司基本信息\n暂无公司基本信息数据\n")
            except Exception as e:
                logger.warning(f"获取港股雪球公司信息失败: {e}")
                result_parts.append(f"### 公司基本信息\n获取失败: {str(e)}\n")
        
        # === 2. yfinance: 估值指标 + 财务报表 ===
        if self.yfinance_available:
            try:
                import yfinance as yf
                
                yf_ticker = f"{code}.HK"
                stock, info = await self._get_yfinance_info(yf, yf_ticker, timeout=30)
                
                if info:
                    # 估值指标
                    valuation_text = """### 估值指标（yfinance）
| 指标 | 数值 |
|------|------|
"""
                    val_fields = [
                        ('市盈率(TTM)', 'trailingPE'),
                        ('市盈率(前瞻)', 'forwardPE'),
                        ('市净率', 'priceToBook'),
                        ('市销率', 'priceToSalesTrailing12Months'),
                        ('总市值', 'marketCap'),
                        ('企业价值', 'enterpriseValue'),
                        ('股息率', 'dividendYield'),
                    ]
                    for display, key in val_fields:
                        val = info.get(key, 'N/A')
                        if val and val != 'N/A':
                            if key in ('marketCap', 'enterpriseValue') and isinstance(val, (int, float)):
                                val = f"${val:,.0f}"
                            elif key == 'dividendYield' and isinstance(val, (int, float)):
                                val = f"{val*100:.2f}%"
                            elif isinstance(val, float):
                                val = f"{val:.2f}"
                        valuation_text += f"| {display} | {val} |\n"
                    
                    result_parts.append(valuation_text)
                    
                    # 盈利能力
                    profit_fields = [
                        ('EPS(TTM)', 'trailingEps'),
                        ('毛利率', 'grossProfitMargin'),
                        ('营业利润率', 'operatingProfitMargin'),
                        ('净利率', 'profitMargins'),
                        ('ROE', 'returnOnEquity'),
                    ]
                    profit_text = """### 盈利能力（yfinance）
| 指标 | 数值 |
|------|------|
"""
                    for display, key in profit_fields:
                        val = info.get(key, 'N/A')
                        if val and val != 'N/A':
                            if key in ('grossProfitMargin', 'operatingProfitMargin', 'profitMargins', 'returnOnEquity') and isinstance(val, (int, float)):
                                val = f"{val*100:.2f}%"
                        profit_text += f"| {display} | {val} |\n"
                    
                    result_parts.append(profit_text)
                    
                    # 财务报表摘要
                    try:
                        income = await self._run_blocking(lambda: stock.income_stmt, timeout=30)
                        if income is not None and not income.empty:
                            fin_text = "\n### 损益表摘要（最近）\n"
                            for idx in income.head(8).index:
                                val = income.loc[idx].iloc[0] if len(income.loc[idx]) > 0 else 'N/A'
                                if isinstance(val, (int, float)) and val != 0:
                                    fin_text += f"- {idx}: ${val:,.0f}\n"
                            result_parts.append(fin_text)
                    except Exception as e:
                        logger.warning(f"获取港股财务报表失败: {e}")
                    
                    result_parts.append("\n*数据来源: akshare（雪球）+ yfinance*\n")
                else:
                    result_parts.append("\n*数据来源: akshare（雪球）*\n")
            except Exception as e:
                logger.warning(f"yfinance获取港股基本面失败: {e}")
                result_parts.append(f"\n### yfinance补充数据\n获取失败: {str(e)}\n")
                result_parts.append("\n*数据来源: akshare（雪球）*\n")
        else:
            result_parts.append("\n⚠️ yfinance未安装，无法获取估值和财务数据。建议安装: pip install yfinance\n")
            result_parts.append("\n*数据来源: akshare（雪球）*\n")
        
        return "\n".join(result_parts)
    
    async def _get_us_fundamentals(self, ticker: str, trade_date: str, market_info: Dict) -> str:
        """获取美股基本面数据（yfinance主 + akshare公司信息补充）"""
        result_parts = []
        result_parts.append(f"""## 美股基本面数据

**股票代码**: {ticker}
**分析日期**: {trade_date}
""")
        
        # === 1. akshare: 雪球公司信息补充 ===
        if self.akshare_available:
            try:
                import akshare as ak
                info_df = await self._run_akshare(
                    ak.stock_individual_basic_info_us_xq,
                    symbol=ticker,
                    timeout=30,
                )
                if info_df is not None and not info_df.empty:
                    info_dict = dict(zip(info_df.iloc[:, 0], info_df.iloc[:, 1]))
                    company_text = "### 公司基本信息（雪球）\n| 指标 | 数值 |\n|------|------|\n"
                    for item, value in info_dict.items():
                        if value and str(value) != 'nan':
                            company_text += f"| {item} | {value} |\n"
                    result_parts.append(company_text)
            except Exception as e:
                logger.warning(f"akshare获取美股公司信息失败: {e}")
        
        # === 2. yfinance: 估值 + 财务报表 ===
        if self.yfinance_available:
            try:
                import yfinance as yf
                
                stock, info = await self._get_yfinance_info(yf, ticker, timeout=30)
                if not isinstance(info, dict):
                    info = {}

                quote_type = str(info.get('quoteType') or '').upper()
                legal_type = str(info.get('legalType') or '').lower()
                is_fund = quote_type in ('ETF', 'MUTUALFUND') or 'fund' in legal_type
                if is_fund:
                    result_parts.append(f"""### 基金基本信息（yfinance）
| 指标 | 数值 |
|------|------|
| 基金名称 | {info.get('longName') or info.get('shortName') or 'N/A'} |
| 基金类型 | {info.get('quoteType', 'N/A')} |
| 基金分类 | {info.get('category', 'N/A')} |
| 基金公司 | {info.get('fundFamily', 'N/A')} |
| 法律类型 | {info.get('legalType', 'N/A')} |
| 基金规模/总资产 | ${format(info.get('totalAssets'), ',.0f') if info.get('totalAssets') else 'N/A'} |
| 净值 | ${info.get('navPrice', 'N/A')} |
| 市盈率(TTM) | {info.get('trailingPE', 'N/A')} |
| 费用率 | {f"{info.get('expenseRatio')*100:.2f}%" if info.get('expenseRatio') else 'N/A'} |
| 收益率 | {f"{info.get('yield')*100:.2f}%" if info.get('yield') else 'N/A'} |
""")
                    result_parts.append("\n*数据来源: yfinance + akshare（雪球）*\n")
                    return "\n".join(result_parts)

                key_fields = [
                    'trailingPE', 'forwardPE', 'priceToBook',
                    'priceToSalesTrailing12Months', 'marketCap',
                    'trailingEps', 'totalRevenue', 'profitMargins',
                ]
                valid_key_count = sum(
                    1 for field in key_fields
                    if info.get(field) not in (None, '', 'N/A')
                )
                
                # 获取财务报表
                financial_text = ""
                try:
                    income = await self._run_blocking(lambda: stock.income_stmt, timeout=30)
                    balance = await self._run_blocking(lambda: stock.balance_sheet, timeout=30)
                    
                    if income is not None and not income.empty:
                        financial_text += "### 损益表（最近一年）\n"
                        for idx, row in income.head(5).iterrows():
                            name = idx
                            value = row.iloc[0] if len(row) > 0 else 'N/A'
                            if isinstance(value, (int, float)) and value > 0:
                                financial_text += f"- {name}: ${value:,.0f}\n"
                    
                    if balance is not None and not balance.empty:
                        financial_text += "\n### 资产负债表（最近一年）\n"
                        for idx, row in balance.head(5).iterrows():
                            name = idx
                            value = row.iloc[0] if len(row) > 0 else 'N/A'
                            if isinstance(value, (int, float)) and value > 0:
                                financial_text += f"- {name}: ${value:,.0f}\n"
                except Exception as e:
                    logger.warning(f"获取美股财务报表失败: {e}")
                
                if valid_key_count == 0 and not financial_text:
                    return "无法获取美股基本面数据: yfinance未返回有效财务指标"

                result_parts.append(f"""### 估值指标（yfinance）
| 指标 | 数值 |
|------|------|
| 市盈率(TTM) | {info.get('trailingPE', 'N/A')} |
| 市盈率(前瞻) | {info.get('forwardPE', 'N/A')} |
| 市净率 | {info.get('priceToBook', 'N/A')} |
| 市销率 | {info.get('priceToSalesTrailing12Months', 'N/A')} |
| EV/EBITDA | {info.get('enterpriseToEbitda', 'N/A')} |
| 市值 | ${format(info.get('marketCap'), ',.0f') if info.get('marketCap') else 'N/A'} |
| 企业价值 | ${format(info.get('enterpriseValue'), ',.0f') if info.get('enterpriseValue') else 'N/A'} |

### 盈利能力（yfinance）
| 指标 | 数值 |
|------|------|
| EPS(TTM) | ${info.get('trailingEps', 'N/A')} |
| EPS(前瞻) | ${info.get('forwardEps', 'N/A')} |
| 净利润 | ${format(info.get('netIncomeToCommon'), ',.0f') if info.get('netIncomeToCommon') else 'N/A'} |
| 收入 | ${format(info.get('totalRevenue'), ',.0f') if info.get('totalRevenue') else 'N/A'} |
| 毛利率 | {f"{info.get('grossProfitMargin')*100:.2f}%" if info.get('grossProfitMargin') else 'N/A'} |
| 营业利润率 | {f"{info.get('operatingProfitMargin')*100:.2f}%" if info.get('operatingProfitMargin') else 'N/A'} |
| 净利率 | {f"{info.get('profitMargins')*100:.2f}%" if info.get('profitMargins') else 'N/A'} |

### 财务数据
{financial_text if financial_text else '暂无详细财务数据'}
""")
                result_parts.append("\n*数据来源: yfinance + akshare（雪球）*\n")
                
            except ImportError:
                return "yfinance未安装"
            except Exception as e:
                logger.warning(f"yfinance获取美股基本面失败: {e}")
                result_parts.append(f"\n### yfinance数据\n获取失败: {str(e)}\n")
        else:
            result_parts.append("\n⚠️ yfinance未安装，无法获取估值和财务数据\n")
        
        return "\n".join(result_parts)
    
    async def get_news(self, ticker: str, trade_date: str) -> str:
        """
        获取新闻数据 - 复刻 get_stock_news_unified
        """
        from .utils.stock_utils import StockUtils
        market_info = StockUtils.get_market_info(ticker)
        
        try:
            if market_info.get('is_etf'):
                return await self._get_etf_news(ticker, trade_date, market_info)
            elif market_info['is_china']:
                return await self._get_china_news(ticker, trade_date, market_info)
            elif market_info['is_hk']:
                return await self._get_hk_news(ticker, trade_date, market_info)
            else:
                return await self._get_us_news(ticker, trade_date, market_info)
        except Exception as e:
            logger.error(f"获取新闻数据失败: {e}")
            return f"获取新闻数据失败: {str(e)}"
    
    async def _get_china_news(self, ticker: str, trade_date: str, market_info: Dict) -> str:
        """获取A股新闻"""
        if not self.akshare_available:
            return "akshare未安装，无法获取A股新闻"
        
        from .utils.stock_utils import StockUtils
        
        try:
            import akshare as ak
            
            code = StockUtils.strip_market_prefix(ticker)
            
            try:
                news_df = await self._run_akshare(ak.stock_news_em, symbol=code, timeout=30)
                
                if news_df is not None and not news_df.empty:
                    news_text = f"## A股新闻数据\n\n**股票代码**: {code}\n**日期**: {trade_date}\n\n### 近期新闻\n"
                    
                    for idx, row in news_df.head(10).iterrows():
                        news_time = row.get('发布时间', 'N/A')
                        news_title = row.get('新闻标题', 'N/A')
                        news_url = row.get('链接', '')
                        news_text += f"- **{news_time}**: {news_title}\n"
                    
                    return news_text
                else:
                    return f"暂无{code}的新闻数据"
                    
            except Exception as e:
                return f"获取A股新闻失败: {str(e)}"
                
        except ImportError:
            return "akshare未安装"
        except Exception as e:
            return f"获取A股新闻失败: {str(e)}"

    def _format_yfinance_news_item(self, item: Dict) -> str:
        """格式化 yfinance 新闻条目，兼容新旧返回结构。"""
        content = item.get('content') if isinstance(item.get('content'), dict) else item
        title = content.get('title') or item.get('title')
        if not title or title == 'N/A':
            return ""

        provider = content.get('provider') if isinstance(content.get('provider'), dict) else {}
        publisher = (
            content.get('publisher')
            or item.get('publisher')
            or provider.get('displayName')
            or provider.get('sourceId')
            or 'N/A'
        )

        pub_ts = (
            content.get('providerPublishTime')
            or item.get('providerPublishTime')
            or content.get('pubDate')
            or item.get('pubDate')
            or content.get('displayTime')
            or ''
        )
        if isinstance(pub_ts, (int, float)) and pub_ts:
            try:
                from datetime import timezone
                pub_date = datetime.fromtimestamp(int(pub_ts), tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
            except Exception:
                pub_date = str(pub_ts)
        else:
            pub_date = str(pub_ts) if pub_ts else 'N/A'

        url = ''
        for key in ('clickThroughUrl', 'canonicalUrl'):
            value = content.get(key)
            if isinstance(value, dict) and value.get('url'):
                url = value.get('url')
                break
        if not url:
            url = content.get('link') or item.get('link') or ''

        suffix = f" - {url}" if url else ""
        return f"- **{publisher}** ({pub_date}): {title}{suffix}\n"
    
    async def _get_hk_news(self, ticker: str, trade_date: str, market_info: Dict) -> str:
        """获取港股新闻（yfinance）"""
        code = ticker.replace('.HK', '').replace('HK', '').replace('.', '')
        
        if not self.yfinance_available:
            return "暂无港股新闻数据（需要付费数据源）"
        
        try:
            import yfinance as yf
            
            yf_ticker = f"{code}.HK"
            stock = yf.Ticker(yf_ticker)
            news = await self._run_blocking(lambda: stock.news, timeout=30)
            
            if news and len(news) > 0:
                news_text = f"""## 港股新闻数据

**股票代码**: {code}.HK
**日期**: {trade_date}

### 近期新闻
"""
                for item in news[:10]:
                    news_text += self._format_yfinance_news_item(item)
                
                if "): " not in news_text:
                    return f"暂无港股{code}.HK的有效新闻数据"
                news_text += "\n---\n*数据来源: yfinance*\n"
                return news_text
            else:
                return f"暂无港股{code}.HK的新闻数据"
                
        except Exception as e:
            logger.warning(f"获取港股新闻失败: {e}")
            return f"暂无港股新闻数据（获取失败: {str(e)}）"
    
    async def _get_us_news(self, ticker: str, trade_date: str, market_info: Dict) -> str:
        """获取美股新闻"""
        if not self.yfinance_available:
            return "yfinance未安装，无法获取美股新闻"
        
        try:
            import yfinance as yf
            
            stock = yf.Ticker(ticker)
            news = await self._run_blocking(lambda: stock.news, timeout=30)
            
            if news and len(news) > 0:
                news_text = f"## 美股新闻数据\n\n**股票代码**: {ticker}\n**日期**: {trade_date}\n\n### 近期新闻\n"
                
                for item in news[:10]:
                    news_text += self._format_yfinance_news_item(item)
                
                if "): " not in news_text:
                    return f"暂无{ticker}的有效新闻数据"
                return news_text
            else:
                return f"暂无{ticker}的新闻数据"
                
        except ImportError:
            return "yfinance未安装"
        except Exception as e:
            return f"获取美股新闻失败: {str(e)}"
    
    async def get_sentiment(self, ticker: str, trade_date: str) -> str:
        """
        获取情绪数据 - 复刻 get_stock_sentiment_unified
        """
        from .utils.stock_utils import StockUtils
        market_info = StockUtils.get_market_info(ticker)
        
        try:
            if market_info.get('is_etf'):
                return await self._get_etf_sentiment(ticker, trade_date, market_info)
            elif market_info['is_china']:
                return await self._get_china_sentiment(ticker, trade_date, market_info)
            elif market_info['is_hk']:
                return await self._get_hk_sentiment(ticker, trade_date, market_info)
            else:
                return await self._get_us_sentiment(ticker, trade_date, market_info)
        except Exception as e:
            logger.error(f"获取情绪数据失败: {e}")
            return f"获取情绪数据失败: {str(e)}"
    
    async def _get_china_sentiment(self, ticker: str, trade_date: str, market_info: Dict) -> str:
        """获取A股情绪数据"""
        # A股情绪数据（东方财富等）
        if not self.akshare_available:
            return "akshare未安装，无法获取A股情绪数据"
        
        from .utils.stock_utils import StockUtils
        
        try:
            import akshare as ak
            
            code = StockUtils.strip_market_prefix(ticker)
            
            try:
                # 资金流向数据可作为情绪参考
                df = await self._run_akshare(
                    ak.stock_individual_fund_flow,
                    stock=code,
                    market="sh" if code.startswith(('600', '601', '603', '688')) else "sz",
                    timeout=30,
                )
                
                if df is not None and not df.empty:
                    sentiment_text = f"## A股情绪数据\n\n**股票代码**: {code}\n**日期**: {trade_date}\n\n### 资金流向\n"
                    
                    for idx, row in df.tail(5).iterrows():
                        date = row.get('日期', 'N/A')
                        net = row.get('今日主力净流入-净额', 'N/A')
                        net_pct = row.get('今日主力净流入-净占比', 'N/A')
                        sentiment_text += f"- **{date}**: 主力净流入 {net} ({net_pct}%)\n"
                    
                    sentiment_text += """
### 情绪分析
资金流向可反映市场情绪：
- 主力净流入 > 0：表示机构看多
- 主力净流入 < 0：表示机构看空
- 关注连续净流入/净流出天数
"""
                    return sentiment_text
                else:
                    return f"暂无{code}的情绪数据"
                    
            except Exception as e:
                return f"获取A股情绪数据失败: {str(e)}"
                
        except ImportError:
            return "akshare未安装"
        except Exception as e:
            return f"获取A股情绪失败: {str(e)}"
    
    async def _get_hk_sentiment(self, ticker: str, trade_date: str, market_info: Dict) -> str:
        """获取港股情绪数据（yfinance 分析师评级 + 推荐）"""
        code = ticker.replace('.HK', '').replace('HK', '').replace('.', '')
        
        if not self.yfinance_available:
            return "暂无港股情绪数据（需要付费数据源）"
        
        try:
            import yfinance as yf
            
            yf_ticker = f"{code}.HK"
            stock, info = await self._get_yfinance_info(yf, yf_ticker, timeout=30)
            
            sentiment_parts = [f"""## 港股情绪数据

**股票代码**: {code}.HK
**日期**: {trade_date}

### 分析师情绪
| 指标 | 数值 |
|------|------|
"""]
            
            # 分析师评级
            rec_key = info.get('recommendationKey', 'N/A')
            target_price = info.get('targetMeanPrice', 'N/A')
            num_analysts = info.get('numberOfAnalystOpinions', 'N/A')
            current_price = info.get('currentPrice', info.get('regularMarketPrice', 'N/A'))
            
            sentiment_parts[0] += f"| 分析师评级 | {rec_key} |\n"
            sentiment_parts[0] += f"| 目标均价 | {target_price} |\n"
            sentiment_parts[0] += f"| 分析师数量 | {num_analysts} |\n"
            sentiment_parts[0] += f"| 当前价格 | {current_price} |\n"
            
            # 如果有目标价和当前价，计算上行空间
            if isinstance(target_price, (int, float)) and isinstance(current_price, (int, float)) and current_price > 0:
                upside = (target_price - current_price) / current_price * 100
                sentiment_parts[0] += f"| 上行空间 | {upside:+.2f}% |\n"
            
            # 获取评级变动
            try:
                recommendations = await self._run_blocking(
                    lambda: getattr(stock, 'recommendations', None),
                    timeout=30,
                )
                if recommendations is not None and not recommendations.empty:
                    rec_text = "\n### 近期评级变动\n"
                    for idx, row in recommendations.tail(5).iterrows():
                        date = idx
                        grade = row.get('To Grade', row.get('ToGrade', 'N/A'))
                        firm = row.get('Firm', row.get('firm', 'N/A'))
                        rec_text += f"- **{date}**: {firm} → {grade}\n"
                    sentiment_parts.append(rec_text)
            except Exception as e:
                logger.warning(f"获取港股评级变动失败: {e}")
            
            sentiment_parts.append("\n---\n*数据来源: yfinance*\n")
            return "\n".join(sentiment_parts)
            
        except Exception as e:
            logger.warning(f"获取港股情绪数据失败: {e}")
            return f"暂无港股情绪数据（获取失败: {str(e)}）"
    
    async def _get_us_sentiment(self, ticker: str, trade_date: str, market_info: Dict) -> str:
        """获取美股情绪数据"""
        if not self.yfinance_available:
            return "yfinance未安装，无法获取美股情绪数据"
        
        try:
            import yfinance as yf
            
            stock, info = await self._get_yfinance_info(yf, ticker, timeout=30)
            
            # 从分析评级获取情绪
            recommendations = await self._run_blocking(
                lambda: getattr(stock, 'recommendations', None),
                timeout=30,
            )
            
            sentiment_text = f"""## 美股情绪数据

**股票代码**: {ticker}
**日期**: {trade_date}

### 分析师情绪
| 指标 | 数值 |
|------|------|
| 分析师评级 | {info.get('recommendationKey', 'N/A')} |
| 目标价 | ${info.get('targetMeanPrice', 'N/A')} |
| 买入评级数 | {info.get('numberOfAnalystOpinions', 'N/A')} |

### 情绪评分（1-10分）
| 方向 | 评分 | 说明 |
|------|------|------|
| 买入情绪 | 5-7分 | 中性偏正面 |
| 持有情绪 | 4-6分 | 中性 |
| 卖出情绪 | 2-4分 | 中性偏负面 |

注：美股情绪数据受新闻、社交媒体影响较大，建议结合多个数据源判断。
"""
            
            if recommendations is not None and not recommendations.empty:
                sentiment_text += "\n### 近期评级变动\n"
                for idx, row in recommendations.tail(5).iterrows():
                    date = idx
                    grade = row.get('ToGrade', 'N/A')
                    sentiment_text += f"- **{date}**: {grade}\n"
            
            return sentiment_text
            
        except ImportError:
            return "yfinance未安装"
        except Exception as e:
            return f"获取美股情绪失败: {str(e)}"

    # ==================== ETF 数据获取方法 ====================

    async def _get_etf_market_data(self, ticker: str, trade_date: str, market_info: Dict) -> str:
        """获取ETF市场数据（fund_etf_hist_em + fund_etf_spot_em）"""
        if not self.akshare_available:
            return "akshare未安装，无法获取ETF数据"

        from .utils.stock_utils import StockUtils
        import asyncio

        try:
            import akshare as ak
            import pandas as pd

            code = StockUtils.strip_market_prefix(ticker)

            end_date = datetime.strptime(trade_date, '%Y-%m-%d')
            start_date = end_date - timedelta(days=30)

            # === 1. 历史K线 ===
            hist_text = ""
            df = None
            last_error = None
            for attempt in range(3):
                try:
                    logger.info(f"尝试获取ETF数据 (尝试 {attempt + 1}/3): {code}")
                    df = await self._run_akshare(
                        ak.fund_etf_hist_em,
                        symbol=code,
                        period="daily",
                        start_date=start_date.strftime('%Y%m%d'),
                        end_date=end_date.strftime('%Y%m%d'),
                        adjust="qfq",
                        timeout=30,
                    )
                    if df is not None and not df.empty:
                        break
                except asyncio.TimeoutError:
                    last_error = "获取数据超时"
                    logger.warning(f"ETF尝试 {attempt + 1} 超时")
                except Exception as e:
                    last_error = str(e)
                    logger.warning(f"ETF尝试 {attempt + 1} 失败: {e}")
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)

            use_tencent = False  # 标记是否使用了腾讯源

            if df is not None and not df.empty:
                latest = df.iloc[-1]
                prev = df.iloc[-2] if len(df) > 1 else latest
                price_change = latest['收盘'] - prev['收盘']
                pct_change = (price_change / prev['收盘'] * 100) if prev['收盘'] != 0 else 0

                hist_text = f"""### 近期行情
| 日期 | 开盘 | 收盘 | 最高 | 最低 | 成交量 | 成交额 | 涨跌幅 |
|------|------|------|------|------|--------|--------|--------|
| {latest['日期']} | {latest['开盘']} | {latest['收盘']} | {latest['最高']} | {latest['最低']} | {latest['成交量']} | {latest['成交额']} | {pct_change:.2f}% |

### 最近5个交易日
"""
                for idx, row in df.tail(5).iterrows():
                    _pct = row.get('涨跌幅', None)
                    if _pct is None:
                        _pct_prev = df.iloc[idx - 1]['收盘'] if idx > 0 else latest['收盘']
                        _pct = (row['收盘'] - _pct_prev) / _pct_prev * 100 if _pct_prev else 0
                    _pct_str = f"{_pct:+.2f}" if isinstance(_pct, (int, float)) else str(_pct)
                    hist_text += f"- **{row['日期']}**: 收{row['收盘']} ({_pct_str}%)\n"
            else:
                # akshare 失败，尝试腾讯接口降级
                logger.info(f"ETF akshare K线失败，尝试腾讯接口降级: {code}")
                try:
                    df = await self._run_blocking(self._tencent_kline, code, 30)
                    if df is not None and not df.empty:
                        use_tencent = True
                        latest = df.iloc[-1]
                        prev = df.iloc[-2] if len(df) > 1 else latest
                        price_change = latest['收盘'] - prev['收盘']
                        pct_change = (price_change / prev['收盘'] * 100) if prev['收盘'] != 0 else 0

                        hist_text = f"""### 近期行情（腾讯财经）
| 日期 | 开盘 | 收盘 | 最高 | 最低 | 成交量 | 涨跌幅 |
|------|------|------|------|------|--------|--------|
| {latest['日期']} | {latest['开盘']} | {latest['收盘']} | {latest['最高']} | {latest['最低']} | {latest['成交量']} | {pct_change:.2f}% |

### 最近5个交易日
"""
                        for idx, row in df.tail(5).iterrows():
                            _prev = df.iloc[idx - 1]['收盘'] if idx > 0 else latest['收盘']
                            _pct = (row['收盘'] - _prev) / _prev * 100 if _prev else 0
                            hist_text += f"- **{row['日期']}**: 收{row['收盘']} ({_pct:+.2f}%)\n"
                    else:
                        hist_text = f"### 历史行情\n无法获取ETF {code} 的历史数据（akshare + 腾讯接口均失败）。原因: {last_error or '未知错误'}\n"
                except Exception as e:
                    logger.warning(f"ETF腾讯K线降级也失败: {e}")
                    hist_text = f"### 历史行情\n无法获取ETF {code} 的历史数据（akshare + 腾讯接口均失败）。原因: {last_error or '未知错误'}\n"

            # === 2. 实时行情 ===
            realtime_text = ""
            try:
                spot_df = await self._run_akshare(ak.fund_etf_spot_em, timeout=15)
                spot_result = spot_df[spot_df['代码'] == code]
                if not spot_result.empty:
                    s = spot_result.iloc[0]
                    realtime_text = f"""
### 实时行情
| 指标 | 数值 |
|------|------|
| 名称 | {s.get('名称', 'N/A')} |
| 最新价 | {s.get('最新价', 'N/A')} |
| 涨跌额 | {s.get('涨跌额', 'N/A')} |
| 涨跌幅 | {s.get('涨跌幅', 'N/A')}% |
| 成交量 | {s.get('成交量', 'N/A')} |
| 成交额 | {s.get('成交额', 'N/A')} |
| 最高 | {s.get('最高', 'N/A')} |
| 最低 | {s.get('最低', 'N/A')} |
| 今开 | {s.get('今开', 'N/A')} |
| 昨收 | {s.get('昨收', 'N/A')} |
| 换手率 | {s.get('换手率', 'N/A')} |
| IOPV实时估值 | {s.get('IOPV实时估值', 'N/A')} |
| 基金折价率 | {s.get('基金折价率', 'N/A')} |
| 总市值 | {s.get('总市值', 'N/A')} |
| 流通市值 | {s.get('流通市值', 'N/A')} |
"""
                else:
                    # akshare 有返回但没找到该代码，尝试腾讯
                    raise Exception("fund_etf_spot_em 未找到该ETF")
            except Exception as e:
                logger.warning(f"获取ETF实时行情失败: {type(e).__name__}: {repr(e)}")
                # 腾讯接口降级
                try:
                    rt = await self._run_blocking(self._tencent_realtime, code)
                    if rt:
                        use_tencent = True
                        realtime_text = f"""
### 实时行情（腾讯财经）
| 指标 | 数值 |
|------|------|
| 名称 | {rt.get('名称', 'N/A')} |
| 最新价 | {rt.get('最新价', 'N/A')} |
| 涨跌额 | {rt.get('涨跌额', 'N/A')} |
| 涨跌幅 | {rt.get('涨跌幅', 'N/A')}% |
| 成交量 | {rt.get('成交量', 'N/A')} |
| 成交额 | {rt.get('成交额', 'N/A')} |
| 最高 | {rt.get('最高', 'N/A')} |
| 最低 | {rt.get('最低', 'N/A')} |
| 今开 | {rt.get('今开', 'N/A')} |
| 昨收 | {rt.get('昨收', 'N/A')} |
| 总市值 | {rt.get('总市值', 'N/A')} |
| 流通市值 | {rt.get('流通市值', 'N/A')} |
"""
                except Exception as e2:
                    logger.warning(f"ETF腾讯实时行情降级也失败: {e2}")

            source_tag = "腾讯财经" if use_tencent else "东方财富"
            return f"""## ETF市场数据

**基金代码**: {code}
**交易日期**: {trade_date}
**市场**: {market_info['market_name']}
**交易所**: {market_info['exchange']}
**货币**: {market_info['currency_name']}（{market_info['currency_symbol']}）

{hist_text}
{realtime_text}
---
*数据来源: akshare（{source_tag}）*
"""
        except ImportError:
            return "akshare未安装，请运行: pip install akshare"
        except Exception as e:
            return f"获取ETF市场数据失败: {str(e)}"

    async def _get_etf_fundamentals(self, ticker: str, trade_date: str, market_info: Dict) -> str:
        """获取ETF基本面数据（基金信息 + NAV + 持仓）"""
        from .utils.stock_utils import StockUtils

        result_parts = []
        code = StockUtils.strip_market_prefix(ticker)
        result_parts.append(f"""## ETF基本面数据

**基金代码**: {code}
**分析日期**: {trade_date}

⚠️ **注意**: ETF（交易所交易基金）无传统公司财务指标（PE/PB/ROE），以下提供基金特有指标。
""")

        # === 1. 雪球基金基本信息 ===
        if self.akshare_available:
            try:
                import akshare as ak
                info_df = await self._run_akshare(
                    ak.fund_individual_basic_info_xq,
                    symbol=code,
                    timeout=30,
                )
                if info_df is not None and not info_df.empty:
                    info_dict = dict(zip(info_df.iloc[:, 0], info_df.iloc[:, 1]))
                    fund_text = "### 基金基本信息（雪球）\n| 指标 | 数值 |\n|------|------|\n"
                    key_fields = [
                        '基金代码', '基金名称', '基金全称', '基金类型',
                        '成立时间', '最新规模', '基金公司', '基金经理',
                        '托管银行', '基金评级', '业绩比较基准', '投资策略', '投资目标',
                    ]
                    for key in key_fields:
                        val = info_dict.get(key, '')
                        if val and str(val) != 'nan':
                            fund_text += f"| {key} | {val} |\n"
                    # 输出其他字段
                    for item, value in info_dict.items():
                        if item not in key_fields and value and str(value) != 'nan':
                            fund_text += f"| {item} | {value} |\n"
                    result_parts.append(fund_text)
                else:
                    result_parts.append("### 基金基本信息\n暂无基金基本信息数据\n")
            except Exception as e:
                logger.warning(f"获取ETF雪球基金信息失败: {e}")
                result_parts.append(f"### 基金基本信息\n获取失败: {str(e)}\n")

            # === 2. ETF实时指标（折溢价、规模等） ===
            spot_ok = False
            try:
                import akshare as ak
                spot_df = await self._run_akshare(ak.fund_etf_spot_em, timeout=15)
                spot_result = spot_df[spot_df['代码'] == code]
                if not spot_result.empty:
                    spot_ok = True
                    s = spot_result.iloc[0]
                    metrics_text = """### ETF关键指标（实时）
| 指标 | 数值 |
|------|------|
"""
                    etf_metrics = [
                        ('名称', '名称'),
                        ('最新价', '最新价'),
                        ('IOPV实时估值', 'IOPV实时估值'),
                        ('基金折价率', '基金折价率'),
                        ('总市值', '总市值'),
                        ('流通市值', '流通市值'),
                        ('最新份额', '最新份额'),
                        ('换手率', '换手率'),
                        ('量比', '量比'),
                        ('主力净流入-净额', '主力净流入-净额'),
                        ('主力净流入-净占比', '主力净流入-净占比'),
                    ]
                    for display, col in etf_metrics:
                        val = s.get(col, 'N/A')
                        if val is not None and str(val) != 'nan':
                            metrics_text += f"| {display} | {val} |\n"
                    result_parts.append(metrics_text)
            except Exception as e:
                logger.warning(f"获取ETF实时指标失败: {e}")

            if not spot_ok:
                # akshare 实时指标失败，用腾讯接口降级
                try:
                    rt = await self._run_blocking(self._tencent_realtime, code)
                    if rt:
                        metrics_text = """### ETF关键指标（实时·腾讯财经）
| 指标 | 数值 |
|------|------|
"""
                        tencent_metrics = [
                            ('名称', '名称'),
                            ('最新价', '最新价'),
                            ('总市值', '总市值'),
                            ('流通市值', '流通市值'),
                        ]
                        for display, key in tencent_metrics:
                            val = rt.get(key, 'N/A')
                            if val is not None and str(val) != 'nan':
                                metrics_text += f"| {display} | {val} |\n"
                        result_parts.append(metrics_text)
                except Exception as e2:
                    logger.warning(f"ETF基本面腾讯降级也失败: {e2}")

            # === 3. NAV历史（近10个交易日） ===
            try:
                import akshare as ak
                end_date = datetime.strptime(trade_date, '%Y-%m-%d')
                start_date = end_date - timedelta(days=30)
                nav_df = await self._run_akshare(
                    ak.fund_etf_fund_info_em,
                    fund=code,
                    start_date=start_date.strftime('%Y%m%d'),
                    end_date=end_date.strftime('%Y%m%d'),
                    timeout=30,
                )
                if nav_df is not None and not nav_df.empty:
                    nav_text = "### 净值历史（最近）\n| 日期 | 单位净值 | 累计净值 | 增长率 |\n|------|---------|---------|--------|\n"
                    for idx, row in nav_df.tail(10).iterrows():
                        date_val = row.get('净值日期', row.iloc[0] if len(row) > 0 else 'N/A')
                        nav = row.get('单位净值', row.iloc[1] if len(row) > 1 else 'N/A')
                        acc_nav = row.get('累计净值', row.iloc[2] if len(row) > 2 else 'N/A')
                        growth = row.get('日增长率', row.iloc[3] if len(row) > 3 else 'N/A')
                        nav_text += f"| {date_val} | {nav} | {acc_nav} | {growth} |\n"
                    result_parts.append(nav_text)
            except Exception as e:
                logger.warning(f"获取ETF NAV历史失败: {e}")

            result_parts.append("\n*数据来源: akshare（东方财富 + 雪球）*\n")
        else:
            result_parts.append("\nakshare未安装，无法获取ETF基本面数据\n")

        return "\n".join(result_parts)

    async def _get_etf_news(self, ticker: str, trade_date: str, market_info: Dict) -> str:
        """获取ETF新闻数据（复用A股新闻接口，如失败则提供ETF概要）"""
        from .utils.stock_utils import StockUtils

        code = StockUtils.strip_market_prefix(ticker)

        # 尝试用 stock_news_em（部分ETF代码也能查到新闻）
        if self.akshare_available:
            try:
                import akshare as ak
                news_df = await self._run_akshare(ak.stock_news_em, symbol=code, timeout=30)

                if news_df is not None and not news_df.empty:
                    news_text = f"## ETF新闻数据\n\n**基金代码**: {code}\n**日期**: {trade_date}\n\n### 近期新闻\n"
                    for idx, row in news_df.head(10).iterrows():
                        news_time = row.get('发布时间', 'N/A')
                        news_title = row.get('新闻标题', 'N/A')
                        news_text += f"- **{news_time}**: {news_title}\n"
                    return news_text
            except Exception as e:
                logger.warning(f"获取ETF新闻失败(尝试stock_news_em): {e}")

        # 备选：尝试获取跟踪指数的新闻背景
        stock_name = StockUtils.get_stock_name(ticker)
        return f"""## ETF新闻数据

**基金代码**: {code}
**基金名称**: {stock_name}
**日期**: {trade_date}

### 基金概要
本基金为ETF（交易所交易基金），新闻面请关注：
1. **跟踪指数变动**: 关注该ETF跟踪的标的指数走势和政策变化
2. **行业/板块政策**: 影响ETF持仓行业的重大政策调整
3. **折溢价变化**: 二级市场交易价格与IOPV的偏离程度
4. **资金流向**: ETF份额变动反映机构资金动向
5. **成分股调整**: 指数成分股定期调整对ETF的影响

---
*数据来源: ETF基金无直接新闻接口，以上为投资关注要点*
"""

    async def _get_etf_sentiment(self, ticker: str, trade_date: str, market_info: Dict) -> str:
        """获取ETF情绪数据（资金流向 + 折溢价 + 主力动向）"""
        from .utils.stock_utils import StockUtils

        code = StockUtils.strip_market_prefix(ticker)
        result_parts = [f"""## ETF情绪数据

**基金代码**: {code}
**日期**: {trade_date}
"""]

        if self.akshare_available:
            sentiment_ok = False
            try:
                import akshare as ak

                # ETF实时行情中的情绪指标
                spot_df = await self._run_akshare(ak.fund_etf_spot_em, timeout=15)
                spot_result = spot_df[spot_df['代码'] == code]
                if not spot_result.empty:
                    sentiment_ok = True
                    s = spot_result.iloc[0]
                    sentiment_table = """### ETF情绪指标
| 指标 | 数值 | 解读 |
|------|------|------|
"""
                    # 折价率 → 情绪方向
                    discount = s.get('基金折价率', 'N/A')
                    if isinstance(discount, (int, float)):
                        direction = "溢价交易，市场情绪偏热" if discount > 0 else "折价交易，市场情绪偏冷"
                        sentiment_table += f"| 基金折价率 | {discount}% | {direction} |\n"
                    else:
                        sentiment_table += f"| 基金折价率 | {discount} | 暂无数据 |\n"

                    # 主力净流入
                    net_inflow = s.get('主力净流入-净额', 'N/A')
                    net_pct = s.get('主力净流入-净占比', 'N/A')
                    if isinstance(net_inflow, (int, float)):
                        direction = "资金流入，看多" if net_inflow > 0 else "资金流出，看空"
                        sentiment_table += f"| 主力净流入 | {net_inflow} | {direction} |\n"
                    else:
                        sentiment_table += f"| 主力净流入 | {net_inflow} | - |\n"
                    sentiment_table += f"| 主力净流入占比 | {net_pct}% | - |\n"

                    # 量比
                    vol_ratio = s.get('量比', 'N/A')
                    if isinstance(vol_ratio, (int, float)):
                        vol_desc = "放量" if vol_ratio > 1.5 else ("缩量" if vol_ratio < 0.7 else "正常")
                        sentiment_table += f"| 量比 | {vol_ratio} | {vol_desc} |\n"

                    # 换手率
                    turnover = s.get('换手率', 'N/A')
                    sentiment_table += f"| 换手率 | {turnover}% | 交易活跃度 |\n"

                    result_parts.append(sentiment_table)

                    result_parts.append("""
### 情绪分析说明
- **折价率**: ETF市价低于净值(折价)表示情绪偏冷，高于净值(溢价)表示情绪偏热
- **主力净流入**: 正值表示大资金买入，负值表示大资金卖出
- **量比**: >1.5表示放量，<0.7表示缩量，反映市场参与度变化
- **换手率**: 反映ETF二级市场交易活跃程度
""")
                else:
                    result_parts.append("### ETF情绪指标\n暂无实时行情数据\n")
            except Exception as e:
                logger.warning(f"获取ETF情绪数据失败: {e}")
                result_parts.append(f"### ETF情绪指标\n获取失败: {str(e)}\n")

            if not sentiment_ok:
                # akshare 情绪指标失败，用腾讯接口降级
                try:
                    rt = await self._run_blocking(self._tencent_realtime, code)
                    if rt:
                        sentiment_table = """### ETF情绪指标（腾讯财经）
| 指标 | 数值 | 解读 |
|------|------|------|
"""
                        # 涨跌幅 → 情绪方向
                        chg_pct = rt.get('涨跌幅')
                        if isinstance(chg_pct, (int, float)):
                            direction = "上涨，市场情绪偏多" if chg_pct > 0 else "下跌，市场情绪偏空"
                            sentiment_table += f"| 涨跌幅 | {chg_pct}% | {direction} |\n"

                        # 成交额
                        amount = rt.get('成交额')
                        sentiment_table += f"| 成交额 | {amount} | 反映交易活跃度 |\n"

                        # 总市值
                        mkt_cap = rt.get('总市值')
                        sentiment_table += f"| 总市值 | {mkt_cap} | 基金规模 |\n"

                        result_parts.append(sentiment_table)
                        result_parts.append("""
### 情绪分析说明
- **涨跌幅**: 正值偏多，负值偏空
- **成交额**: 反映市场参与度
- 注: 腾讯接口不提供折溢价、主力净流入等ETF特有指标，以上为有限数据
""")
                except Exception as e2:
                    logger.warning(f"ETF情绪腾讯降级也失败: {e2}")

            # 资金流向历史
            try:
                import akshare as ak
                flow_df = await self._run_akshare(
                    ak.stock_individual_fund_flow,
                    stock=code,
                    market="sh" if code.startswith(('51', '56', '58')) else "sz",
                    timeout=30,
                )
                if flow_df is not None and not flow_df.empty:
                    flow_text = "### 资金流向（近5日）\n"
                    for idx, row in flow_df.tail(5).iterrows():
                        date = row.get('日期', 'N/A')
                        net = row.get('今日主力净流入-净额', 'N/A')
                        net_pct = row.get('今日主力净流入-净占比', 'N/A')
                        flow_text += f"- **{date}**: 主力净流入 {net} ({net_pct}%)\n"
                    result_parts.append(flow_text)
            except Exception as e:
                logger.warning(f"获取ETF资金流向失败: {e}")

            result_parts.append("\n---\n*数据来源: akshare（东方财富）*\n")
        else:
            result_parts.append("\nakshare未安装，无法获取ETF情绪数据\n")

        return "\n".join(result_parts)

    async def fetch_all_data(self, ticker: str, trade_date: str) -> Dict:
        """
        一次性并发获取所有信息源数据，并检查数据完整性。

        Returns:
            dict: {
                'success': bool,              # 是否所有必要数据都获取成功
                'market_data': str,           # 市场数据
                'fundamentals_data': str,      # 基本面数据
                'news_data': str,              # 新闻数据
                'sentiment_data': str,         # 情绪数据
                'missing_sources': list,       # 缺失的信息源列表
                'error_details': dict,         # 各信息源的缺失原因 {source: reason}
            }
        """
        import asyncio

        # 并发获取所有数据
        market_task = self.get_market_data(ticker, trade_date)
        fundamentals_task = self.get_fundamentals(ticker, trade_date)
        news_task = self.get_news(ticker, trade_date)
        sentiment_task = self.get_sentiment(ticker, trade_date)

        results = await asyncio.gather(
            market_task, fundamentals_task, news_task, sentiment_task,
            return_exceptions=True
        )

        market_data = results[0] if not isinstance(results[0], Exception) else f"获取失败: {results[0]}"
        fundamentals_data = results[1] if not isinstance(results[1], Exception) else f"获取失败: {results[1]}"
        news_data = results[2] if not isinstance(results[2], Exception) else f"获取失败: {results[2]}"
        sentiment_data = results[3] if not isinstance(results[3], Exception) else f"获取失败: {results[3]}"

        # 数据完整性校验
        missing_sources = []
        error_details = {}

        # 市场数据校验
        market_ok = self._check_data_valid(market_data, '市场数据')
        if not market_ok['valid']:
            missing_sources.append('市场数据')
            error_details['市场数据'] = market_ok['reason']

        # 基本面数据校验
        fundamentals_ok = self._check_data_valid(fundamentals_data, '基本面数据')
        if not fundamentals_ok['valid']:
            missing_sources.append('基本面数据')
            error_details['基本面数据'] = fundamentals_ok['reason']

        # 新闻数据校验
        news_ok = self._check_data_valid(news_data, '新闻数据')
        if not news_ok['valid']:
            missing_sources.append('新闻数据')
            error_details['新闻数据'] = news_ok['reason']

        # 情绪数据校验（非强制，仅警告）
        sentiment_ok = self._check_data_valid(sentiment_data, '情绪数据')
        if not sentiment_ok['valid']:
            error_details['情绪数据'] = sentiment_ok['reason']

        # 判断是否数据齐全：市场数据和基本面数据是必须的
        success = len(missing_sources) == 0

        return {
            'success': success,
            'market_data': market_data,
            'fundamentals_data': fundamentals_data,
            'news_data': news_data,
            'sentiment_data': sentiment_data,
            'missing_sources': missing_sources,
            'error_details': error_details,
        }

    def _check_data_valid(self, data: str, source_name: str) -> Dict[str, bool | str]:
        """
        检查数据是否有效（非空、非失败信息）。

        Args:
            data: 数据文本
            source_name: 数据源名称（用于日志）

        Returns:
            {'valid': bool, 'reason': str}
        """
        if not data or not data.strip():
            return {'valid': False, 'reason': '数据为空'}

        data_stripped = data.strip()
        data_first_line = data_stripped.split('\n')[0].strip()

        # 通用失败关键词（对所有长度都检测）
        failure_keywords = [
            '获取失败', '未安装', '无法获取',
            'akshare和yfinance均不可用',
            '需要付费数据源',
            '数据失败',     # 匹配 "获取市场数据失败"、"获取基本面数据失败" 等
            'Connection aborted',  # 网络连接中断
            'RemoteDisconnected',  # 远程断连
            'Too Many Requests',   # 频率限制
        ]

        for kw in failure_keywords:
            if kw in data_stripped:
                return {'valid': False, 'reason': data_first_line}

        first_line_failure_keywords = [
            '不可用',
        ]
        for kw in first_line_failure_keywords:
            if kw in data_first_line:
                return {'valid': False, 'reason': data_first_line}

        # 长文本失败模式检测：Markdown 格式的错误页面
        long_failure_patterns = [
            r'暂无.+的行情数据',  # 港股无数据时的返回（如 "暂无港股0700的行情数据"）
        ]
        import re
        for pattern in long_failure_patterns:
            if re.search(pattern, data_stripped):
                return {'valid': False, 'reason': data_first_line}

        if source_name == '基本面数据':
            metric_keywords = [
                '市盈率', '市净率', '市销率', 'ROE', '净资产收益率',
                '每股收益', 'EPS', '净利润', '收入', '营业总收入',
                '毛利率', '净利率', '市值', '企业价值', '资产负债率',
                '经营现金流', '流动比率', '速动比率',
                '基金规模', '总资产', '净值', '费用率', '收益率',
                '基金类型', '基金分类', '基金公司',
            ]
            invalid_values = ('N/A', '暂无', '未知', '无法获取', '获取失败', '未安装')
            valid_metric_count = 0
            for line in data_stripped.splitlines():
                line_stripped = line.strip()
                if not line_stripped.startswith('|') or line_stripped.startswith('|------'):
                    continue
                if not any(keyword in line_stripped for keyword in metric_keywords):
                    continue
                cells = [cell.strip() for cell in line_stripped.strip('|').split('|')]
                if len(cells) < 2:
                    continue
                value = cells[-1]
                if value and not any(marker in value for marker in invalid_values):
                    valid_metric_count += 1

            if valid_metric_count < 2:
                return {'valid': False, 'reason': '基本面有效财务指标不足'}

        return {'valid': True, 'reason': ''}


# REVIEW-NOTE: 原全局单例 get_data_fetcher() 已删除（从未被调用，各模块按需创建 DataFetcher 实例）
