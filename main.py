from datetime import datetime
from urllib.parse import urlsplit

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.platform import MessageType
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, StarTools

from .balance import BalanceError, DeepSeekBalanceClient, format_balance, normalize_base_url
from .capture import (
    RunBooker,
    final_missing_from_stats,
    final_usage,
    find_agent_runner,
    raw_model,
    runner_model,
)
from .report import LLM_NOTE, build_report, build_top_report, parse_timezone, session_label
from .usage_store import Usage, UsageStore

PLUGIN_NAME = "astrbot_plugin_token_usage"
LOG_PREFIX = "[TokenUsage]"
DEEPSEEK_HOST = "api.deepseek.com"
# 记账钩子只读不改，排在最前：事件被 stop_event() 后 AstrBot 只会执行第一个钩子
HOOK_PRIORITY = 100000


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default if value is None else bool(value)


def _as_float(value, default: float, lo: float | None = None, hi: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if lo is not None:
        result = max(lo, result)
    if hi is not None:
        result = min(hi, result)
    return result


def _as_int(value, default: int, lo: int | None = None) -> int:
    return int(_as_float(value, default, lo))


class TokenUsagePlugin(Star):
    """记录每个会话的 token 消耗并查询 DeepSeek 余额；LLM 可调用 query_token_usage 工具回答用户"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = UsageStore(StarTools.get_data_dir(PLUGIN_NAME) / "usage.db")
        self.balance = DeepSeekBalanceClient()
        self.booker = RunBooker()
        self._pruned_day = ""
        self._tz_warned = False

    async def terminate(self):
        self.store.close()

    # ------------------------------------------------------------------ 配置

    def _section(self, name: str) -> dict:
        value = self.config.get(name)
        return value if isinstance(value, dict) else {}

    def _today(self) -> str:
        try:
            tz = parse_timezone(self.config.get("timezone"))
        except ValueError as e:
            if not self._tz_warned:
                logger.warning(f"{LOG_PREFIX} {e}，改用服务器本地时间")
                self._tz_warned = True
            tz = None
        return datetime.now(tz).date().isoformat()

    def _deepseek_key(self, base_url: str) -> str:
        c = self._section("deepseek")
        key = str(c.get("api_key") or "").strip()
        if key or not _as_bool(c.get("auto_detect_key"), True):
            return key
        # 自动读取的是 AstrBot 里 DeepSeek 官方提供商的 Key，只允许发往官方地址
        if urlsplit(base_url).hostname != DEEPSEEK_HOST:
            return ""
        for provider in self.context.get_all_providers():
            cfg = getattr(provider, "provider_config", None) or {}
            if DEEPSEEK_HOST not in str(cfg.get("api_base") or ""):
                continue
            keys = cfg.get("key") or []
            if isinstance(keys, str):
                keys = [keys]
            for k in keys:
                if str(k).strip():
                    return str(k).strip()
        return ""

    # ------------------------------------------------------------------ 记录

    @filter.on_using_llm_tool(priority=HOOK_PRIORITY)
    async def record_tool_step(self, event: AstrMessageEvent, tool, tool_args):
        """调用工具前先记下本轮已产生的用量：工具直接发送结果或返回空时，本轮会就此结束"""
        try:
            runner = find_agent_runner(event=event)
            if runner is None:
                return
            delta = self.booker.take_delta(runner)
            if delta is not None:
                await self._save(event, delta, runner_model(runner))
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 记录 token 用量失败：{e}")

    @filter.on_llm_response(priority=HOOK_PRIORITY)
    async def record_usage(self, event: AstrMessageEvent, resp: LLMResponse):
        """每轮对话结束时记下本轮剩余的用量"""
        try:
            final = final_usage(resp)
            runner = find_agent_runner(resp=resp, event=event)
            if runner is None:
                # 找不到 runner（第三方 Agent 执行器、或 AstrBot 内部实现有变）时只能记最后一步
                if final is not None:
                    await self._save(event, final, raw_model(resp))
                return
            extra = final if final_missing_from_stats(runner, final) else None
            delta = self.booker.take_delta(runner, extra)
            if delta is not None:
                await self._save(event, delta, runner_model(runner))
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 记录 token 用量失败：{e}")

    async def _save(self, event: AstrMessageEvent, usage: Usage, model: str):
        day = self._today()
        await self.store.add(day, event.unified_msg_origin, model or self._current_model(event), usage)
        if day != self._pruned_day:
            self._pruned_day = day
            try:
                removed = await self.store.prune(day, _as_int(self.config.get("retention_days"), 90))
                if removed:
                    logger.info(f"{LOG_PREFIX} 已清理 {removed} 条过期用量记录")
            except Exception as e:
                logger.warning(f"{LOG_PREFIX} 清理过期用量记录失败：{e}")

    def _current_model(self, event: AstrMessageEvent) -> str:
        try:
            provider = self.context.get_using_provider(event.unified_msg_origin)
            return str(provider.get_model() or "") if provider else ""
        except Exception:
            return ""

    # ------------------------------------------------------------------ 查询

    async def _balance_text(self, event: AstrMessageEvent) -> str:
        if _as_bool(self.config.get("balance_admin_only"), False) and not event.is_admin():
            return "仅管理员可以查询余额"
        c = self._section("deepseek")
        base_url = normalize_base_url(c.get("base_url"))
        api_key = self._deepseek_key(base_url)
        if not api_key:
            return "未配置 DeepSeek API Key，无法查询"
        try:
            balance = await self.balance.fetch(
                api_key,
                base_url=base_url,
                proxy=c.get("proxy_url"),
                timeout=_as_float(c.get("timeout"), 15.0, 3.0, 30.0),
                cache_seconds=_as_float(c.get("cache_seconds"), 60.0, 0.0),
            )
        except BalanceError as e:
            logger.warning(f"{LOG_PREFIX} 查询 DeepSeek 余额失败：{e}")
            return f"查询失败（{e}）"
        except Exception as e:
            # 余额只是附带信息，任何意外都不能让 token 用量一起查不到
            logger.warning(f"{LOG_PREFIX} 查询 DeepSeek 余额出错：{type(e).__name__}")
            return "查询失败（内部错误）"
        return format_balance(balance)

    def _session_label(self, event: AstrMessageEvent) -> str:
        try:
            is_group = event.get_message_type() == MessageType.GROUP_MESSAGE
            isolated = bool(event.get_extra("_session_isolated"))
        except Exception:
            return "当前会话"
        return session_label(is_group=is_group, isolated=isolated)

    async def _report(self, event: AstrMessageEvent, *, for_llm: bool) -> str:
        day = self._today()
        session = await self.store.session_usage(day, event.unified_msg_origin)
        total, sessions = await self.store.day_usage(day)
        models = None if for_llm else await self.store.day_models(day)
        return build_report(
            day=day,
            session=session,
            total=total,
            sessions=sessions,
            balance=await self._balance_text(event),
            session_label=self._session_label(event),
            models=models,
            note=LLM_NOTE if for_llm else "",
        )

    # ------------------------------------------------------------------ LLM 工具与指令

    @filter.llm_tool(name="query_token_usage")
    async def query_token_usage(self, event: AstrMessageEvent):
        """查询 token 消耗和 DeepSeek API 余额。当用户问到 token 用量、今天消耗/花了多少、API 余额还剩多少时调用。返回当前会话今日消耗的 token、今日所有会话总共消耗的 token，以及 DeepSeek 账户余额（人民币和美元分开计）；群聊的会话用量通常是全群成员的合计，不是提问者个人的。请根据用户的问题挑选相关数据，用自然的口吻回答，提到金额时带上货币符号。"""
        return await self._report(event, for_llm=True)

    @filter.command("token")
    async def token_cmd(self, event: AstrMessageEvent, action: str = ""):
        """查看今日 token 用量与 DeepSeek 余额；/token top 查看今日消耗最多的会话（管理员，仅私聊）"""
        if str(action).strip().lower() == "top":
            if not event.is_admin():
                yield event.plain_result("权限不足：仅 AstrBot 管理员可以查看全部会话排行。")
                return
            if event.get_message_type() != MessageType.FRIEND_MESSAGE:
                yield event.plain_result("会话排行会列出其他会话的 ID（含私聊用户号），请私聊 Bot 使用 /token top。")
                return
            day = self._today()
            rows = await self.store.top_sessions(day, 10)
            yield event.plain_result(build_top_report(day, rows)).use_t2i(False)
            return
        yield event.plain_result(await self._report(event, for_llm=False)).use_t2i(False)
