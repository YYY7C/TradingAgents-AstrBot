"""AstrBot 金融助手插件入口。"""

import os
import re

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api import logger, AstrBotConfig
from astrbot.api.star import Context, Star

from .trading_graph_langgraph import TradingGraphLangGraph as FinancialAssistantGraph
from .data_fetcher import DataFetcher
from .llm_client import OpenAICompatibleLLM
from .utils.stock_utils import StockUtils
from .utils.report_utils import extract_conclusion, save_report_pdf, save_report_md, save_report_txt, check_pdf_available


class TradingAssistantPlugin(Star):
    """金融助手插件"""
    
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        logger.info("TradingAssistantPlugin 初始化中...")
        self.config = config or {}
        
        # 从插件配置读取API配置
        self.api_key = None
        self.api_base = None
        self.model = None
        self.reasoning = None
        self.llm = None

        # 检查 PDF 依赖可用性
        self._pdf_available, self._pdf_unavailable_reason = check_pdf_available()
        self._export_txt = False

        # 读取 PDF 导出开关
        self.export_pdf = self.config.get('export_pdf', True)

        if not self._pdf_available:
            if self.export_pdf:
                # PDF 不可用但用户开启了 PDF 导出 → 降级为 TXT
                self._export_txt = True
                logger.warning(
                    f"weasyprint 系统依赖缺失，PDF 导出不可用"
                    f"（{self._pdf_unavailable_reason}），"
                    f"已自动降级为 TXT 文本导出。"
                    f"请安装系统依赖（libglib2.0-0, libpango, libcairo2 等）"
                    f"或在插件配置中关闭 export_pdf。"
                )
            self.export_pdf = False

        if not self.config.get('export_pdf', True) and not self._export_txt:
            logger.info("export_pdf 已关闭，报告将以 Markdown 分模块逐段发送。")

        # 初始化LLM配置
        self._init_llm_config()
    
    def _init_llm_config(self):
        """初始化LLM配置"""
        try:
            self.api_key = self.config.get('api_key') or os.environ.get('TRADING_ASSISTANT_API_KEY', '')
            self.api_base = self.config.get('api_base') or os.environ.get('TRADING_ASSISTANT_API_BASE', 'https://open.bigmodel.cn/api/paas/v4')
            self.model = self.config.get('model') or os.environ.get('TRADING_ASSISTANT_MODEL', 'glm-4-flash')
            self.reasoning = self.config.get('reasoning')
            # REVIEW-NOTE: timeout_seconds 未做类型验证，依赖框架配置校验（实际风险极低）
            self.timeout_seconds = int(self.config.get('timeout_seconds', 120))
            
            if self.api_key:
                logger.info(f"LLM配置已加载，模型: {self.model}, 超时: {self.timeout_seconds}s")
                self.llm = OpenAICompatibleLLM(
                    api_key=self.api_key,
                    api_base=self.api_base,
                    model=self.model,
                    timeout_seconds=self.timeout_seconds,
                    reasoning=self.reasoning,
                )
            else:
                logger.warning("未配置 LLM API Key，部分功能可能不可用")
        except Exception as e:
            logger.error(f"初始化LLM配置失败: {e}")
    
    @staticmethod
    def split_report_by_sections(report: str) -> list[str]:
        """将 Markdown 报告按 ## 标题拆分为多个段落，用于分模块发送（防截断）。

        拆分结果示例：
          [标题+元信息, 市场技术面分析, 基本面分析, 新闻面分析, 多空辩论综合, 风险评估]
        """
        # 按「独占一行的 ## 」拆分，每段保留自身标题
        parts = re.split(r'\n(?=## )', report)

        sections: list[str] = []
        for part in parts:
            text = part.strip()
            if not text:
                continue
            # 跳过末尾的生成时间 / 免责声明（不以 # 开头且不是第一段）
            if sections and not text.startswith('#'):
                continue
            sections.append(text)

        return sections

    async def _get_llm(self) -> OpenAICompatibleLLM:
        """获取或创建LLM实例"""
        if self.llm is None:
            self._init_llm_config()
        
        if self.llm is None:
            raise ValueError("LLM未配置，请在插件配置中填写 api_key 或设置对应环境变量")
        
        return self.llm

    # REVIEW-NOTE: _extract_command_arg 当前实现功能正确且可读性好，removeprefix 简化为风格偏好，暂不修改
    @staticmethod
    def _extract_command_arg(message_str: str, command_names: list[str]) -> str:
        cleaned = message_str.strip()
        for command_name in command_names:
            for prefix in (f'/{command_name}', command_name):
                if cleaned.startswith(prefix):
                    rest = cleaned[len(prefix):]
                    if not rest or rest[0] in (' ', '\t'):
                        return rest.strip()
        return cleaned

    async def _resolve_stock_name(self, raw_input: str) -> str | None:
        """
        将用户输入（股票名称/关键词）解析为标准股票代码。

        解析策略（优先级从高到低）：
        1. **本地查找**：通过 akshare 的 A 股全市场列表精确/模糊匹配（仅限 A 股）
        2. **LLM 解析**：调用 LLM 解析港股/美股名称或其他无法本地匹配的输入

        Args:
            raw_input: 用户原始输入（如"厦门港务"、"苹果"、"腾讯"）

        Returns:
            解析出的标准股票代码；解析失败时返回 None
        """
        # ---- 第一步：本地查找（akshare A股列表，精确+模糊匹配） ----
        try:
            local_result = StockUtils.resolve_stock_name(raw_input)
            if local_result:
                logger.info(f"本地解析股票名称: '{raw_input}' → '{local_result}'")
                return local_result
        except Exception as e:
            logger.warning(f"本地股票名称解析异常，将回退到 LLM: {type(e).__name__}: {repr(e)}")

        # ---- 第二步：LLM 解析（覆盖港股/美股名称等本地无法处理的场景） ----
        llm = await self._get_llm()

        prompt = (
            "你是一个股票代码查询助手。用户会输入一个股票名称或关键词，"
            "你需要返回对应的**标准股票代码**。\n\n"
            "返回格式要求：\n"
            "- A股返回6位纯数字代码，例如：平安银行→000001，厦门港务→000905\n"
            "- 港股返回数字.HK格式，例如：腾讯控股→0700.HK，美团→3690.HK\n"
            "- 美股返回大写字母代码，例如：苹果→AAPL，特斯拉→TSLA\n\n"
            "重要规则：\n"
            "1. 只返回一个最匹配的股票代码，不要有任何多余文字、解释或标点\n"
            "2. 如果输入同时有A股和其他市场，优先返回A股代码\n"
            "3. 如果输入不是股票名称（例如是普通词语、指令等），直接回复 UNKNOWN\n"
            "4. 如果完全无法识别对应的股票，直接回复 UNKNOWN\n\n"
            f"用户输入：{raw_input}"
        )

        try:
            result = await llm.ask(prompt)
            result = result.strip().strip('`').strip()

            if result.upper() == 'UNKNOWN' or not result:
                logger.warning(f"LLM无法解析股票名称 '{raw_input}' → 无法识别")
                return None

            logger.info(f"LLM解析股票名称: '{raw_input}' → '{result}'")
            return result
        except Exception as e:
            logger.error(f"LLM解析股票名称失败: {type(e).__name__}: {repr(e)}")
            return None

    @staticmethod
    def _needs_ticker_resolution(ticker: str) -> bool:
        """判断用户输入是否需要 LLM 解析（即不是有效的股票代码格式）"""
        return not StockUtils.is_valid_stock_code(ticker)

    async def _run_stock_analysis(self, ticker: str, event: AstrMessageEvent = None,
                                    quick_mode: bool = False) -> str:
        """执行股票分析，支持实时进度推送
        
        Args:
            ticker: 标准股票代码
            event: 消息事件（用于进度推送）
            quick_mode: 快速分析模式（跳过多空辩论），默认为 False
        """
        llm = await self._get_llm()
        data_fetcher = DataFetcher()

        # 定义进度回调函数
        async def progress_callback(stage: str, detail: str):
            logger.info(f"[进度] {stage}: {detail}")
            if event:
                try:
                    await event.send(event.plain_result(detail))
                except Exception as e:
                    logger.warning(f"发送进度消息失败: {e}")

        graph = FinancialAssistantGraph(llm, data_fetcher, progress_callback=progress_callback)

        mode_label = "（快速分析）" if quick_mode else ""
        logger.info(f"开始分析股票{mode_label}: {ticker}")
        report = await graph.analyze(ticker, quick_mode=quick_mode)
        
        # 检查是否为数据不完整的错误报告
        if report.startswith("❌"):
            logger.warning(f"分析因数据不完整而终止: {ticker}")
        
        return report

    async def _send_file_via_onebot_upload(
        self,
        event: AstrMessageEvent,
        file_path: str,
        file_name: str,
    ) -> bool:
        """使用 OneBot 上传接口发送文件，避免 File 消息段在 NapCat 中显示 0 B。"""
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            logger.warning(
                "OneBot 文件上传入口不可用，将回退到 File 组件发送: "
                f"event_type={type(event).__name__}, platform={event.get_platform_name()}, "
                f"has_bot={bot is not None}, has_call_action={hasattr(bot, 'call_action') if bot is not None else False}"
            )
            return False

        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        logger.info(
            f"OneBot 文件上传入口已就绪: event_type={type(event).__name__}, "
            f"group_id={group_id}, sender_id={sender_id}, file={file_path}, size={file_size}"
        )
        try:
            if group_id:
                logger.info(f"使用 OneBot upload_group_file 发送报告: group_id={group_id}, file={file_path}")
                await bot.call_action(
                    "upload_group_file",
                    group_id=int(group_id),
                    file=file_path,
                    name=file_name,
                )
            else:
                logger.info(f"使用 OneBot upload_private_file 发送报告: user_id={sender_id}, file={file_path}")
                await bot.call_action(
                    "upload_private_file",
                    user_id=int(sender_id),
                    file=file_path,
                    name=file_name,
                )
            return True
        except Exception as e:
            logger.error(f"OneBot 文件上传失败，将回退到 File 组件发送: {type(e).__name__}: {repr(e)}", exc_info=True)
            return False
    
    async def _do_stock_analysis(self, ticker_input: str, event: AstrMessageEvent,
                                    quick_mode: bool = False) -> MessageEventResult:
        """公共股票分析逻辑，供各命令处理器复用。
        
        Args:
            ticker_input: 用户输入的股票代码或名称
            event: 消息事件
            quick_mode: 快速分析模式（跳过多空辩论），默认为 False
        """
        ticker = ticker_input

        try:
            # 如果输入不是有效的股票代码格式（如中文股票名称），尝试解析为代码
            if self._needs_ticker_resolution(ticker):
                resolved = await self._resolve_stock_name(ticker)
                if resolved is None:
                    yield event.plain_result(
                        f"❌ 无法识别「{ticker}」对应的股票。\n"
                        "请输入有效的股票代码或名称，例如：\n"
                        "- A股：000001 或 平安银行\n"
                        "- 港股：0700.HK 或 腾讯控股\n"
                        "- 美股：AAPL 或 苹果"
                    )
                    return
                if event:
                    await event.send(event.plain_result(f"🔍 已识别「{ticker}」→ {resolved}"))
                ticker = resolved

            report = await self._run_stock_analysis(ticker, event, quick_mode=quick_mode)

            # 如果是错误报告，直接返回文字
            if report.startswith("❌"):
                yield event.plain_result(report)
                return

            # 提取结论部分用于文字发送
            conclusion = extract_conclusion(report)

            if self.export_pdf or self._export_txt:
                # ── 文件附件模式 ──
                import astrbot.api.message_components as Comp

                if self._export_txt:
                    # PDF 不可用，降级为 TXT
                    file_path = save_report_txt(report, ticker)
                    file_label = "TXT"
                elif self._pdf_available:
                    try:
                        file_path = save_report_pdf(report, ticker)
                        file_label = "PDF"
                    except Exception as pdf_err:
                        logger.warning(f"PDF 首次生成失败，尝试重新生成: {type(pdf_err).__name__}: {repr(pdf_err)}")
                        try:
                            file_path = save_report_pdf(report, ticker)
                            file_label = "PDF"
                        except Exception as retry_err:
                            logger.error(f"PDF 重新生成仍失败: {type(retry_err).__name__}: {repr(retry_err)}")
                            yield event.plain_result("PDF 报告生成失败，已停止发送长报告。请查看 AstrBot 日志定位 weasyprint 或文件写入问题。")
                            return
                else:
                    yield event.plain_result("当前环境 PDF 组件不可用，已停止发送长报告。请启用 PDF 或 TXT 导出。")
                    return

                file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
                logger.info(f"报告附件准备发送: {file_path}, size={file_size}, label={file_label}")
                if file_label == "PDF" and file_size <= 0:
                    logger.warning(f"PDF 附件大小异常，重新生成后再发送: {file_path}")
                    try:
                        file_path = save_report_pdf(report, ticker)
                        file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
                        logger.info(f"PDF 附件重新生成完成: {file_path}, size={file_size}")
                    except Exception as retry_err:
                        logger.error(f"PDF 附件重新生成失败: {type(retry_err).__name__}: {repr(retry_err)}")
                        yield event.plain_result("PDF 报告文件大小异常，重新生成失败，已停止发送长报告。")
                        return
                    if file_size <= 0:
                        logger.error(f"PDF 附件重新生成后仍为空: {file_path}")
                        yield event.plain_result("PDF 报告文件重新生成后仍为空，已停止发送长报告。")
                        return

                plain_text = f"📊 {ticker} 分析结论：\n\n{conclusion}\n\n---\n📄 完整报告见附件（{file_label}）。"
                if file_label == "PDF":
                    sent_by_upload = await self._send_file_via_onebot_upload(
                        event,
                        file_path,
                        os.path.basename(file_path),
                    )
                    if sent_by_upload:
                        yield event.plain_result(plain_text)
                        return

                chain = [
                    Comp.Plain(plain_text),
                    Comp.File(file=file_path, name=os.path.basename(file_path)),
                ]
                yield event.chain_result(chain)
            else:
                # ── Markdown 分模块模式：逐段发送，防截断 ──
                yield event.plain_result(f"📊 {ticker} 分析结论：\n\n{conclusion}")
                sections = self.split_report_by_sections(report)
                for section in sections:
                    yield event.plain_result(section)
        except ValueError as e:
            logger.error(f"配置错误: {e}", exc_info=True)
            yield event.plain_result("分析服务配置异常，请联系管理员检查插件设置。")
        except Exception as e:
            logger.error(f"股票分析失败: {e}", exc_info=True)
            yield event.plain_result("分析服务暂时不可用，请稍后重试。如果问题持续，请联系管理员。")

    @filter.command("股票分析")
    async def analyze_stock(self, event: AstrMessageEvent) -> MessageEventResult:
        """
        股票分析命令
        用法: /股票分析 <股票代码或名称>
        例如: /股票分析 000001 或 /股票分析 平安银行
        """
        message_str = self._extract_command_arg(event.message_str, ["股票分析"])

        if not message_str:
            yield event.plain_result("请提供股票代码或名称，例如：/股票分析 000001")
            return

        async for result in self._do_stock_analysis(message_str, event):
            yield result
    
    @filter.command("股票")
    async def stock_command(self, event: AstrMessageEvent) -> MessageEventResult:
        """
        股票查询命令（快捷命令）
        用法: /股票 <代码或名称>
        """
        message_str = self._extract_command_arg(event.message_str, ["股票"])

        if not message_str:
            yield event.plain_result("请提供股票代码或名称，例如：/股票 000001")
            return

        async for result in self._do_stock_analysis(message_str, event):
            yield result

    @filter.command("快速分析")
    async def quick_analyze(self, event: AstrMessageEvent) -> MessageEventResult:
        """
        快速分析命令（跳过多空辩论）
        用法: /快速分析 <股票代码或名称>
        例如: /快速分析 000001 或 /快速分析 平安银行
        """
        message_str = self._extract_command_arg(event.message_str, ["快速分析"])

        if not message_str:
            yield event.plain_result("请提供股票代码或名称，例如：/快速分析 000001")
            return

        async for result in self._do_stock_analysis(message_str, event, quick_mode=True):
            yield result

    @filter.command("年报")
    async def stock_report(self, event: AstrMessageEvent) -> MessageEventResult:
        """
        股票报告命令
        用法: /年报 <股票代码>
        """
        message_str = self._extract_command_arg(event.message_str, ["年报"])

        if not message_str:
            yield event.plain_result("请提供股票代码，例如：/年报 000001")
            return

        async for result in self._do_stock_analysis(message_str, event):
            yield result
    
    @filter.command("查股")
    async def lookup_stock(self, event: AstrMessageEvent) -> MessageEventResult:
        """
        股票信息查询
        用法: /查股 <代码>
        """
        message_str = self._extract_command_arg(event.message_str, ["查股"])
        
        if not message_str:
            yield event.plain_result("请提供股票代码，例如：/查股 000001")
            return
        
        ticker = message_str
        
        try:
            # 如果输入不是有效的股票代码格式，尝试本地+LLM解析
            if self._needs_ticker_resolution(ticker):
                resolved = await self._resolve_stock_name(ticker)
                if resolved is None:
                    yield event.plain_result(
                        f"❌ 无法识别「{ticker}」对应的股票。\n"
                        "请输入有效的股票代码或名称，例如：/查股 000001"
                    )
                    return
                if event:
                    await event.send(event.plain_result(f"🔍 已识别「{ticker}」→ {resolved}"))
                ticker = resolved

            # 获取市场信息
            market_info = StockUtils.get_market_info(ticker)

            # 检查是否解析失败（无法识别的市场）
            if market_info.get('resolution_failed'):
                yield event.plain_result(f"❌ {market_info['resolution_message']}")
                return

            normalized = market_info['normalized_ticker']
            stock_name = StockUtils.get_stock_name(ticker)
            
            # 格式化输出
            info_text = f"""**股票信息**

股票名称: {stock_name}
股票代码: {normalized}
所属市场: {market_info['market_name']}
交易所: {market_info['exchange']}
计价货币: {market_info['currency_name']}（{market_info['currency_symbol']}）
"""
            
            yield event.plain_result(info_text)
            
        except Exception as e:
            logger.error(f"股票查询失败: {e}", exc_info=True)
            yield event.plain_result("查询服务暂时不可用，请稍后重试。")
    
    @filter.command("帮助")
    async def show_help(self, event: AstrMessageEvent) -> MessageEventResult:
        """
        显示帮助信息
        """
        help_text = """**TradingAgents 金融助手 使用帮助**

支持以下命令：

1. **股票分析** - 生成完整的股票分析报告（含多空辩论）
   命令: /股票分析 <股票代码或名称>
   示例: /股票分析 000001
         /股票分析 平安银行
         /股票分析 AAPL

2. **快速分析** - 快速生成分析报告（跳过多空辩论，速度更快）
   命令: /快速分析 <股票代码或名称>
   示例: /快速分析 000001
         /快速分析 平安银行
         /快速分析 AAPL

3. **快捷分析** - 快速股票查询（等同于 /股票分析）
   命令: /股票 <代码或名称>
   示例: /股票 000001

4. **年报** - 查看股票年报
   命令: /年报 <股票代码>
   示例: /年报 000001

5. **股票信息** - 查询股票基本信息
   命令: /查股 <代码>
   示例: /查股 000001

4. **市场支持**:
   - A股 (如: 000001, 600000, 平安银行)
   - 港股 (如: 0700.HK, 腾讯)
   - 美股 (如: AAPL, TSLA, NVDA)

**注意事项**:
- 首次使用可能需要配置API Key
- 分析结果仅供参考，不构成投资建议
"""
        yield event.plain_result(help_text)
    
    async def terminate(self):
        """插件卸载时调用"""
        # 关闭 LLM 客户端连接
        if self.llm is not None and hasattr(self.llm, 'close'):
            await self.llm.close()
        logger.info("TradingAssistantPlugin 卸载中...")
