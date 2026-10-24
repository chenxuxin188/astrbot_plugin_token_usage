"""时区解析与用量报告文本（不依赖 AstrBot，便于单独测试）"""

import re
from datetime import timedelta, timezone, tzinfo

from .usage_store import Usage

# 只附在给 LLM 的工具结果里：DeepSeek 的人民币和美元余额分开计
LLM_NOTE = "（回答要求：提到余额时每个金额都要带上货币符号，¥ 表示人民币，$ 表示美元；人民币和美元余额分开说明，不要相加或换算。）"

_OFFSET_RE = re.compile(r"(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", re.IGNORECASE)


def parse_timezone(name: str | None) -> tzinfo | None:
    """解析 "UTC+8" / "+08:00" / "Asia/Shanghai" 等写法；留空返回 None（使用服务器本地时间）。

    Windows 上没装 tzdata 时 IANA 时区名不可用，偏移量写法不受影响。无法解析时抛出 ValueError。
    """
    text = str(name or "").strip()
    if not text:
        return None
    if text.upper() in ("UTC", "GMT", "Z"):
        return timezone.utc
    m = _OFFSET_RE.fullmatch(text)
    if m:
        sign, hours, minutes = m.group(1), int(m.group(2)), int(m.group(3) or 0)
        if hours > 14 or minutes >= 60:
            raise ValueError(f"时区偏移超出范围：{text}")
        delta = timedelta(hours=hours, minutes=minutes)
        return timezone(-delta if sign == "-" else delta, text)
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(text)
    except Exception as e:
        raise ValueError(f"无法识别的时区：{text}") from e


def format_usage(usage: Usage) -> str:
    if not usage.total:
        return "暂无消耗记录"
    return (
        f"{usage.total:,} tokens（输入 {usage.input:,}，其中缓存命中 {usage.input_cached:,}；"
        f"输出 {usage.output:,}；共 {usage.rounds:,} 轮对话）"
    )


def build_report(
    *,
    day: str,
    session: Usage,
    total: Usage,
    sessions: int,
    balance: str,
    session_label: str = "当前会话",
    models: list[tuple[str, Usage]] | None = None,
    note: str = "",
) -> str:
    lines = [
        f"Token 用量统计（{day}，截至本次查询）",
        f"· {session_label}今日消耗：{format_usage(session)}",
        f"· 今日全部会话总消耗：{format_usage(total)}"
        + (f"，涉及 {sessions} 个会话" if sessions else ""),
    ]
    if models and len(models) > 1:
        for model, usage in models:
            lines.append(f"    - {model or '未知模型'}：{usage.total:,} tokens")
    lines.append(f"· DeepSeek 余额：{balance}")
    if note:
        lines.append(note)
    return "\n".join(lines)


def session_label(*, is_group: bool, isolated: bool) -> str:
    """群聊默认整群共用一个会话，必须让 LLM 知道这不是提问者个人的用量"""
    if not is_group:
        return "当前会话"
    if isolated:
        return "你在本群的独立会话"
    return "本群会话（全群成员合计，无法区分到个人）"


def build_top_report(day: str, rows: list[tuple[str, Usage]]) -> str:
    if not rows:
        return f"{day} 暂无 token 消耗记录。"
    lines = [f"{day} 消耗最多的会话："]
    for i, (umo, usage) in enumerate(rows, 1):
        lines.append(f"{i}. {umo}：{usage.total:,} tokens（{usage.rounds:,} 轮）")
    return "\n".join(lines)
