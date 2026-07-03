"""报告工具：Markdown → PDF 转换 & 结论提取。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from datetime import datetime

import markdown


# ── 内嵌字体路径 ─────────────────────────────────────────────────────
_FONT_DIR = Path(__file__).resolve().parent.parent / "fonts"


def _font_path(filename: str) -> str:
    """返回字体文件的 file:// URI；文件不存在则返回空字符串（weasyprint 会忽略空 url）。"""
    p = _FONT_DIR / filename
    return p.as_uri() if p.exists() else ""


# ── 结论提取 ──────────────────────────────────────────────────────────

def extract_conclusion(report: str) -> str:
    """
    从完整分析报告中提取最终建议/结论部分。

    策略（按优先级）：
    1. 提取「风险评估」章节的全部内容（从标题到报告元数据之前）
    2. 提取「多空辩论综合」章节的全部内容
    3. 回退：取报告最后几个实质段落

    会自动排除报告元数据（生成时间、免责声明）。
    """
    if not report or report.startswith("❌"):
        return report or "报告生成失败"

    def _strip_footer(text: str) -> str:
        """去除文本末尾的元数据行。"""
        text = re.sub(
            r'\n\*{0,2}报告生成时间\*{0,2}.*$', '', text, flags=re.DOTALL,
        )
        text = re.sub(
            r'\n\*{0,2}本报告由AI自动生成.*$', '', text, flags=re.DOTALL,
        )
        text = re.sub(r'\n*---\s*$', '', text)
        return text.strip()

    def _extract_section(text: str, keyword: str) -> str | None:
        """
        提取包含 keyword 的 ## 章节的全部内容。
        章节边界 = 从 ## 标题行到「报告生成时间」之间的所有文字。
        不使用 ## 子标题作为边界，因为 LLM 会在章节内部使用 ##。
        """
        # 找所有包含 keyword 的 ## 标题行（取最后一个，因为风险评估通常是最后一节）
        matches = list(re.finditer(
            r'^## [^\n]*' + re.escape(keyword) + r'[^\n]*$',
            text, re.MULTILINE,
        ))
        if not matches:
            return None
        m = matches[-1]  # 取最后一个匹配
        heading = m.group(0).strip()
        body_start = m.end()

        # 章节结束 = 「报告生成时间」或文本末尾
        meta_match = re.search(
            r'\n\*{0,2}报告生成时间', text[body_start:],
        )
        if meta_match:
            body_end = body_start + meta_match.start()
        else:
            body_end = len(text)

        body = _strip_footer(text[body_start:body_end])
        if not body or len(body) < 30:
            return None

        clean_heading = re.sub(r'^##\s*', '', heading)
        return f"**{clean_heading}**\n\n{body}"

    # ── 策略 1：风险评估章节 ──
    result = _extract_section(report, '风险评估')
    if result:
        return result

    # ── 策略 2：多空辩论综合章节 ──
    result = _extract_section(report, '辩论综合')
    if result:
        return result

    # ── 策略 3：回退，取报告最后几个实质段落 ──
    cleaned = _strip_footer(report)
    paragraphs = [
        p.strip() for p in cleaned.split('\n\n')
        if p.strip() and p.strip() != '---'
        and not re.match(r'^#{1,3}\s', p.strip())
    ]
    if paragraphs:
        return '\n\n'.join(paragraphs[-5:])

    return report


def check_pdf_available() -> tuple[bool, str]:
    """
    检查 PDF 生成所需的依赖是否可用。

    Returns:
        (可用与否, 原因说明)
    """
    try:
        from weasyprint import HTML  # noqa: F401
    except (ImportError, OSError) as e:
        return False, f"缺少 PDF 依赖: {e}"

    # 检查内嵌字体文件是否存在（Linux 服务器通常无 CJK 系统字体）
    regular = _FONT_DIR / "NotoSansSC-Regular.ttf"
    bold = _FONT_DIR / "NotoSansSC-Bold.ttf"
    if not regular.exists() or not bold.exists():
        missing = [p.name for p in (regular, bold) if not p.exists()]
        return False, f"缺少字体文件: {', '.join(missing)}"

    return True, ""


# ── Emoji → 文字标签映射（PDF 用） ──────────────────────────────────

_EMOJI_REPLACEMENTS: list[tuple[re.Pattern, str]] = [
    # 常用符号 emoji
    (re.compile(r'⚠️|⚠'), '[!]'),
    (re.compile(r'⚔️|⚔'), '[vs]'),
    (re.compile(r'📊'), '[图表]'),
    (re.compile(r'📈'), '[↑]'),
    (re.compile(r'📉'), '[↓]'),
    (re.compile(r'📋'), '[清单]'),
    (re.compile(r'📰'), '[新闻]'),
    (re.compile(r'🔍'), '[分析]'),
    (re.compile(r'🎯'), '[目标]'),
    (re.compile(r'💰'), '[资金]'),
    (re.compile(r'💭'), '[观点]'),
    (re.compile(r'🛡️|🛡'), '[防护]'),
    (re.compile(r'⚡'), '[!]'),
    (re.compile(r'🔥'), '[!]'),
    # 动物（多空用）
    (re.compile(r'🐂'), '[看涨]'),
    (re.compile(r'🐻'), '[看跌]'),
    # 彩色圆点（风险等级）
    (re.compile(r'🔴'), '●'),
    (re.compile(r'🟡'), '◐'),
    (re.compile(r'🟢'), '○'),
    (re.compile(r'🔵'), '◆'),
    # 其他常见 emoji
    (re.compile(r'✅'), '[Y]'),
    (re.compile(r'❌'), '[N]'),
    (re.compile(r'❗'), '!'),
    (re.compile(r'📌'), '[注]'),
    (re.compile(r'💡'), '[提示]'),
    (re.compile(r'🔑'), '[关键]'),
    (re.compile(r'🚀'), '[↑↑]'),
    (re.compile(r'💀'), '[×]'),
    (re.compile(r'🏆'), '[★]'),
    (re.compile(r'📢'), '[公告]'),
    (re.compile(r'🤖'), '[AI]'),
    (re.compile(r'📝'), '[报告]'),
    (re.compile(r'🧠'), '[思考]'),
    (re.compile(r'💎'), '[+]'),
    (re.compile(r'⚖️|⚖'), '[权衡]'),
    (re.compile(r'🤝'), '[合作]'),
    (re.compile(r'🌏'), '[全球]'),
    # 滑稽/交易相关
    (re.compile(r'💸'), '[亏损]'),
    (re.compile(r'🏦'), '[银行]'),
    (re.compile(r'📅'), '[日期]'),
    (re.compile(r'🗂️|🗂'), '[归档]'),
]


def _replace_emojis_for_pdf(text: str) -> str:
    """将 Markdown 文本中的 emoji 替换为文字标签，确保 PDF 可正常显示。"""
    for pattern, replacement in _EMOJI_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    # 兜底：移除剩余 emoji 和符号（覆盖完整 Unicode emoji 范围）
    # U+2600-U+27BF: Miscellaneous Symbols & Dingbats
    # U+1F000-U+1FFFF: Emoticons, Symbols, Transport, etc.
    # U+FE00-U+FE0F: Variation Selectors
    # U+200D: Zero Width Joiner (used in compound emoji)
    text = re.sub(r'[☀-➿\U0001F000-\U0001FFFF\U0000FE00-\U0000FE0F\U0000200D]', '', text)
    return text


_TOP_LEVEL_KEYWORDS = (
    '市场技术面', '基本面', '新闻面', '市场情绪',
    '综合结论', '多空辩论', '风险评估', '风险提示', '投资建议',
)


def _normalize_heading_levels(md_text: str) -> str:
    """将 LLM 误用的 ## 标题降级为 ###，仅保留已知顶级章节的 ##。

    规则：
    - # (h1) 保留不变（报告标题）
    - ## 中匹配到 _TOP_LEVEL_KEYWORDS 的保留
    - 其余 ## 降级为 ###
    - ### 及以下保留不变
    """
    lines = md_text.split('\n')
    result: list[str] = []
    for line in lines:
        if line.startswith('## ') and not line.startswith('### '):
            if any(kw in line for kw in _TOP_LEVEL_KEYWORDS):
                result.append(line)
            else:
                result.append('#' + line)
        else:
            result.append(line)
    return '\n'.join(result)


# ── Markdown → PDF ───────────────────────────────────────────────────

def _build_report_html(body_html: str, regular_font_uri: str, bold_font_uri: str) -> str:
    """组装完整的报告 HTML 文档（避免 str.format 与内容中的花括号冲突）。"""
    return (
        '<!DOCTYPE html>\n'
        '<html lang="zh-CN">\n'
        '<head>\n'
        '<meta charset="utf-8">\n'
        '<style>\n'
        f'  @font-face {{\n'
        f'    font-family: "Noto Sans SC";\n'
        f'    src: local("Noto Sans SC"), local("Noto Sans CJK SC"),\n'
        f'         url("{regular_font_uri}") format("truetype");\n'
        f'    font-weight: normal;\n'
        f'    font-style: normal;\n'
        f'  }}\n'
        f'  @font-face {{\n'
        f'    font-family: "Noto Sans SC";\n'
        f'    src: local("Noto Sans SC Bold"), local("Noto Sans CJK SC Bold"),\n'
        f'         url("{bold_font_uri}") format("truetype");\n'
        f'    font-weight: bold;\n'
        f'    font-style: normal;\n'
        f'  }}\n'
        '  @page {\n'
        '    size: A4;\n'
        '    margin: 2cm 2.5cm;\n'
        '  }\n'
        '  body {\n'
        '    font-family: "Noto Sans SC", "Noto Sans CJK SC", "PingFang SC",\n'
        '                 "Microsoft YaHei", "WenQuanYi Micro Hei", sans-serif;\n'
        '    font-size: 13px;\n'
        '    line-height: 1.7;\n'
        '    color: #333;\n'
        '  }\n'
        '  h1 {\n'
        '    font-size: 22px;\n'
        '    border-bottom: 2px solid #1a73e8;\n'
        '    padding-bottom: 8px;\n'
        '    color: #1a73e8;\n'
        '  }\n'
        '  h2 {\n'
        '    font-size: 17px;\n'
        '    margin-top: 24px;\n'
        '    color: #333;\n'
        '    border-left: 4px solid #1a73e8;\n'
        '    padding-left: 10px;\n'
        '  }\n'
        '  h3 {\n'
        '    font-size: 15px;\n'
        '    color: #555;\n'
        '  }\n'
        '  table {\n'
        '    border-collapse: collapse;\n'
        '    width: 100%;\n'
        '    margin: 10px 0;\n'
        '    page-break-inside: avoid;\n'
        '  }\n'
        '  th, td {\n'
        '    border: 1px solid #ddd;\n'
        '    padding: 6px 10px;\n'
        '    text-align: left;\n'
        '    font-size: 12px;\n'
        '    overflow-wrap: break-word;\n'
        '    word-wrap: break-word;\n'
        '  }\n'
        '  th {\n'
        '    background-color: #f5f7fa;\n'
        '  }\n'
        '  blockquote {\n'
        '    border-left: 4px solid #ddd;\n'
        '    margin: 10px 0;\n'
        '    padding: 8px 16px;\n'
        '    color: #666;\n'
        '  }\n'
        '  strong {\n'
        '    color: #222;\n'
        '  }\n'
        '  em {\n'
        '    color: #555;\n'
        '  }\n'
        '  hr {\n'
        '    border: none;\n'
        '    border-top: 1px solid #e0e0e0;\n'
        '    margin: 20px 0;\n'
        '  }\n'
        '  code {\n'
        '    background: #f5f5f5;\n'
        '    padding: 2px 5px;\n'
        '    border-radius: 3px;\n'
        '    font-size: 12px;\n'
        '  }\n'
        '  .footer {\n'
        '    text-align: center;\n'
        '    color: #999;\n'
        '    font-size: 11px;\n'
        '    margin-top: 30px;\n'
        '    border-top: 1px solid #e0e0e0;\n'
        '    padding-top: 10px;\n'
        '  }\n'
        '  tr {\n'
        '    page-break-inside: avoid;\n'
        '  }\n'
        '  h1, h2, h3 {\n'
        '    page-break-after: avoid;\n'
        '  }\n'
        '  h2 {\n'
        '    page-break-before: auto;\n'
        '  }\n'
        '</style>\n'
        '</head>\n'
        '<body>\n'
        + body_html +
        '\n</body>\n'
        '</html>'
    )


def markdown_to_pdf_bytes(md_text: str) -> bytes:
    """
    将 Markdown 文本转换为 PDF 字节。

    使用 markdown 库转为 HTML，再通过 weasyprint 渲染为 PDF。

    Args:
        md_text: Markdown 格式的报告文本

    Returns:
        PDF 文件的二进制内容

    Raises:
        ImportError: 如果缺少依赖库
        RuntimeError: 如果 PDF 生成失败
    """
    try:
        from weasyprint import HTML
    except (ImportError, OSError) as e:
        raise ImportError(
            f"生成 PDF 需要安装 weasyprint及其系统依赖: {e}"
        ) from e

    # 预处理：将 emoji 替换为文字标签，确保 PDF 中可显示
    md_for_pdf = _replace_emojis_for_pdf(md_text)

    # 预处理：规范化标题层级，避免 LLM 误用 ## 导致层级扁平
    md_for_pdf = _normalize_heading_levels(md_for_pdf)

    # Markdown → HTML。部分 AstrBot 运行环境会同时暴露多份 markdown 包，
    # 导致扩展类型校验失败；失败时退回基础 Markdown 渲染，保证 PDF 可生成。
    try:
        body_html = markdown.markdown(
            md_for_pdf,
            extensions=["tables", "fenced_code"],
        )
    except TypeError as e:
        if "markdown.extensions.Extension" not in str(e):
            raise
        body_html = markdown.markdown(md_for_pdf)

    full_html = _build_report_html(
        body_html=body_html,
        regular_font_uri=_font_path("NotoSansSC-Regular.ttf"),
        bold_font_uri=_font_path("NotoSansSC-Bold.ttf"),
    )

    # 渲染为 PDF
    pdf_bytes = HTML(string=full_html).write_pdf()
    return pdf_bytes


def save_report_pdf(md_text: str, ticker: str, output_dir: str | None = None) -> str:
    """
    将 Markdown 报告保存为 PDF 文件。

    Args:
        md_text: Markdown 格式的报告
        ticker: 股票代码（用于文件名）
        output_dir: 输出目录，默认为 data/plugin_data/astrbot_plugin_tradingagents/reports

    Returns:
        保存的 PDF 文件绝对路径
    """
    if output_dir is None:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            base = get_astrbot_data_path()
            # get_astrbot_data_path() 可能返回 str 或 Path，统一用 os.path.join
            output_dir = os.path.join(
                str(base), "plugin_data", "astrbot_plugin_tradingagents", "reports"
            )
        except ImportError:
            output_dir = os.path.join(
                os.path.expanduser("~"), ".astrbot_plugin_tradingagents", "reports"
            )

    os.makedirs(output_dir, exist_ok=True)

    # 生成文件名： ticker_日期_时间.pdf
    now = datetime.now()
    filename = f"{ticker}_{now.strftime('%Y%m%d_%H%M%S')}.pdf"
    filepath = os.path.join(output_dir, filename)

    pdf_bytes = markdown_to_pdf_bytes(md_text)
    if not pdf_bytes:
        raise RuntimeError("PDF 生成结果为空")

    with open(filepath, "wb") as f:
        f.write(pdf_bytes)

    file_size = os.path.getsize(filepath)
    if file_size <= 0:
        raise RuntimeError(f"PDF 文件写入后为空: {filepath}")

    return filepath


def save_report_md(md_text: str, ticker: str, output_dir: str | None = None) -> str:
    """
    将 Markdown 报告保存为 .md 文件（PDF 不可用时的降级方案）。

    Args:
        md_text: Markdown 格式的报告
        ticker: 股票代码（用于文件名）
        output_dir: 输出目录

    Returns:
        保存的 .md 文件绝对路径
    """
    if output_dir is None:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            base = get_astrbot_data_path()
            output_dir = os.path.join(
                str(base), "plugin_data", "astrbot_plugin_tradingagents", "reports"
            )
        except ImportError:
            output_dir = os.path.join(
                os.path.expanduser("~"), ".astrbot_plugin_tradingagents", "reports"
            )

    os.makedirs(output_dir, exist_ok=True)

    now = datetime.now()
    filename = f"{ticker}_{now.strftime('%Y%m%d_%H%M%S')}.md"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(md_text)

    return filepath


def save_report_txt(md_text: str, ticker: str, output_dir: str | None = None) -> str:
    """
    将报告保存为纯文本 .txt 文件（PDF 不可用时的降级方案）。

    Args:
        md_text: Markdown 格式的报告
        ticker: 股票代码（用于文件名）
        output_dir: 输出目录

    Returns:
        保存的 .txt 文件绝对路径
    """
    if output_dir is None:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            base = get_astrbot_data_path()
            output_dir = os.path.join(
                str(base), "plugin_data", "astrbot_plugin_tradingagents", "reports"
            )
        except ImportError:
            output_dir = os.path.join(
                os.path.expanduser("~"), ".astrbot_plugin_tradingagents", "reports"
            )

    os.makedirs(output_dir, exist_ok=True)

    now = datetime.now()
    filename = f"{ticker}_{now.strftime('%Y%m%d_%H%M%S')}.txt"
    filepath = os.path.join(output_dir, filename)

    # 预处理：将 emoji 替换为文字标签，确保纯文本中可正常显示
    txt_text = _replace_emojis_for_pdf(md_text)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(txt_text)

    return filepath
