"""从 AstrBot 的 Agent 运行中取出 token 用量（不依赖 AstrBot，便于单独测试）

AstrBot 的 ToolLoopAgentRunner 把一轮对话里每一步 LLM 请求的用量累加在 runner.stats.token_usage。
插件钩子只拿得到 event 和最后一步的响应，所以这里沿调用栈找到正在运行的 runner，
在「调用工具前」和「整轮结束时」各记一次它的累计用量与上次已记部分的差值：
- 工具直接发送结果或返回 None 时，本轮会就此结束且不触发 on_llm_response，工具钩子已经记下了用量；
- 中途出错时，出错前已完成的步骤也已记下。
"""

import sys
import weakref

from .usage_store import Usage


def _int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def usage_from(obj) -> Usage | None:
    """把各种形态的 usage 统一成 Usage（rounds 记为 1），取不到或全为 0 时返回 None。

    兼容：
    - AstrBot 的 TokenUsage（input_other / input_cached / output）
    - OpenAI SDK 的 CompletionUsage 或 dict（prompt_tokens / completion_tokens），
      缓存命中取 prompt_tokens_details.cached_tokens，DeepSeek 另有 prompt_cache_hit_tokens
    """
    if obj is None:
        return None
    get = obj.get if isinstance(obj, dict) else (lambda k, d=None: getattr(obj, k, d))

    if get("input_other") is not None or get("output") is not None:
        usage = Usage(1, _int(get("input_other")), _int(get("input_cached")), _int(get("output")))
        return usage if usage.total else None

    if get("prompt_tokens") is None and get("completion_tokens") is None:
        return None
    prompt = _int(get("prompt_tokens"))
    details = get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = _int(details.get("cached_tokens"))
    else:
        cached = _int(getattr(details, "cached_tokens", 0))
    cached = min(prompt, cached or _int(get("prompt_cache_hit_tokens")))
    usage = Usage(1, prompt - cached, cached, _int(get("completion_tokens")))
    return usage if usage.total else None


def final_usage(resp) -> Usage | None:
    """最后一步响应自身的用量：resp.usage，旧版 AstrBot 只有 resp.raw_completion.usage"""
    return usage_from(getattr(resp, "usage", None)) or usage_from(
        getattr(getattr(resp, "raw_completion", None), "usage", None)
    )


def raw_model(resp) -> str:
    model = getattr(getattr(resp, "raw_completion", None), "model", None)
    return model if isinstance(model, str) else ""


def _is_runner_for(owner, resp, event) -> bool:
    if not hasattr(getattr(owner, "stats", None), "token_usage"):
        return False
    if resp is not None and getattr(owner, "final_llm_resp", None) is resp:
        return True
    if event is None:
        return False
    agent_ctx = getattr(getattr(owner, "run_context", None), "context", None)
    return getattr(agent_ctx, "event", None) is event


def find_agent_runner(*, resp=None, event=None, max_depth: int = 64):
    """沿调用栈找到正在运行、且属于这条消息（或产出 resp）的 Agent Runner。

    AstrBot 的钩子由 runner 在自身方法内直接 await，所以 runner 的栈帧就在调用链上；
    从内向外找，嵌套运行时取最内层的那个。AstrBot 内部实现变化导致找不到时返回 None。
    """
    frame = sys._getframe(1)
    try:
        for _ in range(max_depth):
            if frame is None:
                return None
            owner = frame.f_locals.get("self")
            if owner is not None:
                try:
                    if _is_runner_for(owner, resp, event):
                        return owner
                except Exception:
                    pass
            frame = frame.f_back
        return None
    finally:
        del frame


def runner_model(runner) -> str:
    try:
        return str(runner.provider.get_model() or "")
    except Exception:
        return ""


def final_missing_from_stats(runner, final: Usage | None) -> bool:
    """最后一步的用量是否没有累加进 runner.stats。

    runner 每累加一步就把 stats.current_context_tokens 设为该步的输入量；skills_like 工具模式下
    重新请求得到的最终回复不会被累加，此时两者不相等。旧版没有这个字段时视为已累加。
    """
    if final is None:
        return False
    current = getattr(getattr(runner, "stats", None), "current_context_tokens", None)
    return isinstance(current, int) and current != final.input


class RunBooker:
    """计算每个 runner 自上次记账以来新增的用量，保证同一轮不会重复记账"""

    def __init__(self):
        # runner -> (记账时的 stats 对象, 已记账的累计用量)；runner 被回收后自动清除
        self._booked: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def take_delta(self, runner, extra: Usage | None = None) -> Usage | None:
        """返回尚未记账的部分；本轮第一次记账时 rounds=1，之后为 0。没有新增时返回 None"""
        stats = getattr(runner, "stats", None)
        total = usage_from(getattr(stats, "token_usage", None)) or Usage()
        if extra is not None:
            total = total + extra
        entry = self._booked.get(runner)
        booked = entry[1] if entry is not None and entry[0] is stats else None
        delta = total - booked if booked is not None else total
        if not delta.total:
            return None
        delta.rounds = 0 if booked is not None else 1
        self._booked[runner] = (stats, total)
        return delta
