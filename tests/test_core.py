"""不依赖 AstrBot 的单元测试：python -m pytest tests"""

import asyncio
import importlib
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

balance = importlib.import_module(f"{ROOT.name}.balance")
capture = importlib.import_module(f"{ROOT.name}.capture")
report = importlib.import_module(f"{ROOT.name}.report")
usage_store = importlib.import_module(f"{ROOT.name}.usage_store")
Usage = usage_store.Usage


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- usage_store


def test_store_accumulates_per_session_and_day(tmp_path):
    store = usage_store.UsageStore(tmp_path / "usage.db")

    async def scenario():
        await store.add("2026-10-24", "qq:GroupMessage:1", "deepseek-chat", Usage(1, 100, 50, 20))
        await store.add("2026-10-24", "qq:GroupMessage:1", "deepseek-chat", Usage(1, 10, 0, 5))
        await store.add("2026-10-24", "qq:GroupMessage:1", "deepseek-reasoner", Usage(1, 1, 1, 1))
        await store.add("2026-10-24", "qq:FriendMessage:2", "deepseek-chat", Usage(1, 1000, 0, 0))
        await store.add("2026-10-23", "qq:GroupMessage:1", "deepseek-chat", Usage(1, 9999, 0, 0))
        session = await store.session_usage("2026-10-24", "qq:GroupMessage:1")
        total, sessions = await store.day_usage("2026-10-24")
        models = await store.day_models("2026-10-24")
        top = await store.top_sessions("2026-10-24", 1)
        empty = await store.session_usage("2026-10-24", "unknown")
        return session, total, sessions, models, top, empty

    session, total, sessions, models, top, empty = run(scenario())
    store.close()

    assert session == Usage(3, 111, 51, 26)
    assert session.input == 162 and session.total == 188
    assert total == Usage(4, 1111, 51, 26) and sessions == 2
    assert [m for m, _ in models] == ["deepseek-chat", "deepseek-reasoner"]
    assert top == [("qq:FriendMessage:2", Usage(1, 1000, 0, 0))]
    assert empty == Usage() and empty.total == 0


def test_store_prune_keeps_recent_days(tmp_path):
    store = usage_store.UsageStore(tmp_path / "usage.db")

    async def scenario():
        for day in ("2026-10-20", "2026-10-22", "2026-10-24"):
            await store.add(day, "s", "m", Usage(1, 1, 0, 0))
        removed = await store.prune("2026-10-24", 3)
        kept = [await store.day_usage(d) for d in ("2026-10-20", "2026-10-22", "2026-10-24")]
        forever = await store.prune("2026-10-24", 0)
        return removed, kept, forever

    removed, kept, forever = run(scenario())
    store.close()
    assert removed == 1
    assert [u.total for u, _ in kept] == [0, 1, 1]
    assert forever == 0


# ---------------------------------------------------------------- capture


def test_usage_from_astrbot_token_usage():
    tu = SimpleNamespace(input_other=70, input_cached=30, output=12)
    assert capture.usage_from(tu) == Usage(1, 70, 30, 12)
    assert capture.usage_from(SimpleNamespace(input_other=0, input_cached=0, output=0)) is None
    assert capture.usage_from(None) is None
    assert capture.usage_from(SimpleNamespace(foo=1)) is None


def test_usage_from_openai_and_deepseek_styles():
    openai_style = SimpleNamespace(
        prompt_tokens=100, completion_tokens=8,
        prompt_tokens_details=SimpleNamespace(cached_tokens=64),
    )
    assert capture.usage_from(openai_style) == Usage(1, 36, 64, 8)
    deepseek_dict = {"prompt_tokens": 100, "completion_tokens": 8, "prompt_cache_hit_tokens": 40}
    assert capture.usage_from(deepseek_dict) == Usage(1, 60, 40, 8)
    broken = {"prompt_tokens": 10, "completion_tokens": None, "prompt_cache_hit_tokens": 99}
    assert capture.usage_from(broken) == Usage(1, 0, 10, 0)


def step(i, c, o):
    return SimpleNamespace(input_other=i, input_cached=c, output=o)


class FakeRunner:
    """模拟 AstrBot ToolLoopAgentRunner：stats 累计各步用量，钩子在 runner 的方法里被直接 await"""

    def __init__(self, event=None, model="deepseek-chat"):
        self.stats = SimpleNamespace(token_usage=step(0, 0, 0), current_context_tokens=0)
        self.provider = SimpleNamespace(get_model=lambda: model)
        self.run_context = SimpleNamespace(context=SimpleNamespace(event=event))
        self.final_llm_resp = None

    def add_step(self, usage):
        tu = self.stats.token_usage
        tu.input_other += usage.input_other
        tu.input_cached += usage.input_cached
        tu.output += usage.output
        self.stats.current_context_tokens = usage.input_other + usage.input_cached

    async def on_tool_start(self, hook):
        return await call_event_hook(hook)

    async def complete(self, resp, hook):
        self.final_llm_resp = resp
        return await call_event_hook(hook)


async def call_event_hook(hook):
    return await hook()


def test_find_agent_runner_by_event_and_by_resp():
    event, other_event, resp = object(), object(), object()
    runner = FakeRunner(event)

    async def lookup(**kw):
        return capture.find_agent_runner(**kw)

    assert run(runner.on_tool_start(lambda: lookup(event=event))) is runner
    assert run(runner.on_tool_start(lambda: lookup(event=other_event))) is None
    assert run(runner.complete(resp, lambda: lookup(resp=resp))) is runner
    assert run(lookup(event=event)) is None  # 不在 runner 里调用时找不到


def test_find_agent_runner_prefers_innermost_and_requires_stats():
    event = object()
    outer, inner = FakeRunner(event), FakeRunner(event)

    async def nested():
        return await inner.on_tool_start(lambda: lookup())

    async def lookup():
        return capture.find_agent_runner(event=event)

    assert run(outer.on_tool_start(nested)) is inner
    no_stats = FakeRunner(event)
    del no_stats.stats  # 旧版 AstrBot 没有 stats：退回到只记最后一步
    assert run(no_stats.on_tool_start(lookup)) is None


def test_booker_books_each_runner_increment_once():
    booker = capture.RunBooker()
    runner = FakeRunner()
    assert booker.take_delta(runner) is None  # 还没有用量

    runner.add_step(step(500, 0, 30))  # 第一步：调用工具
    assert booker.take_delta(runner) == Usage(1, 500, 0, 30)
    assert booker.take_delta(runner) is None  # 同一步的第二个工具不重复记账

    runner.add_step(step(120, 500, 80))  # 第二步：最终回复
    assert booker.take_delta(runner) == Usage(0, 120, 500, 80)

    runner.stats = SimpleNamespace(token_usage=step(10, 0, 1))  # runner.reset() 换了新的 stats
    assert booker.take_delta(runner) == Usage(1, 10, 0, 1)

    other = FakeRunner()
    other.add_step(step(7, 0, 0))
    assert booker.take_delta(other) == Usage(1, 7, 0, 0)


def test_booker_adds_final_step_missing_from_stats():
    booker = capture.RunBooker()
    runner = FakeRunner()
    runner.add_step(step(500, 0, 30))
    assert booker.take_delta(runner) == Usage(1, 500, 0, 30)

    # skills_like 模式下重新请求得到的最终回复没有累加进 stats
    final = Usage(1, 200, 400, 50)
    assert capture.final_missing_from_stats(runner, final)
    assert booker.take_delta(runner, final) == Usage(0, 200, 400, 50)

    runner.add_step(step(1, 2, 3))
    assert not capture.final_missing_from_stats(runner, Usage(1, 1, 2, 3))
    assert not capture.final_missing_from_stats(runner, None)
    del runner.stats.current_context_tokens  # 旧版没有该字段时视为已累加
    assert not capture.final_missing_from_stats(runner, final)


def test_final_usage_and_model_fallbacks():
    raw = SimpleNamespace(
        model="deepseek-chat",
        usage=SimpleNamespace(prompt_tokens=50, completion_tokens=5, prompt_tokens_details=None),
    )
    assert capture.final_usage(SimpleNamespace(raw_completion=raw)) == Usage(1, 50, 0, 5)
    assert capture.final_usage(SimpleNamespace(usage=step(1, 2, 3), raw_completion=raw)) == Usage(1, 1, 2, 3)
    assert capture.final_usage(SimpleNamespace()) is None
    assert capture.raw_model(SimpleNamespace(raw_completion=raw)) == "deepseek-chat"
    assert capture.raw_model(SimpleNamespace(raw_completion=None)) == ""
    assert capture.runner_model(FakeRunner(model="deepseek-reasoner")) == "deepseek-reasoner"
    assert capture.runner_model(SimpleNamespace()) == ""


def test_usage_arithmetic_clamps_at_zero():
    assert Usage(1, 5, 5, 5) + Usage(0, 1, 2, 3) == Usage(1, 6, 7, 8)
    assert Usage(1, 5, 1, 5) - Usage(0, 1, 2, 3) == Usage(1, 4, 0, 2)


# ---------------------------------------------------------------- report


@pytest.mark.parametrize("text, offset", [
    ("UTC+8", timedelta(hours=8)),
    ("+08:00", timedelta(hours=8)),
    ("GMT-5", timedelta(hours=-5)),
    ("utc+5:30", timedelta(hours=5, minutes=30)),
    ("UTC", timedelta(0)),
])
def test_parse_timezone_offsets(text, offset):
    tz = report.parse_timezone(text)
    assert tz.utcoffset(None) == offset


def test_parse_timezone_empty_and_invalid():
    assert report.parse_timezone("") is None
    assert report.parse_timezone(None) is None
    with pytest.raises(ValueError):
        report.parse_timezone("UTC+20")
    with pytest.raises(ValueError):
        report.parse_timezone("Mars/Olympus")


def test_build_report_lines():
    text = report.build_report(
        day="2026-10-24",
        session=Usage(2, 1000, 500, 234),
        total=Usage(9, 20000, 10000, 3456),
        sessions=3,
        balance="¥110.00（充值 100.00，赠金 10.00），可正常调用",
        models=[("deepseek-chat", Usage(8, 1, 1, 1)), ("deepseek-reasoner", Usage(1, 1, 0, 0))],
    )
    assert "当前会话今日消耗：1,734 tokens（输入 1,500，其中缓存命中 500；输出 234；共 2 轮对话）" in text
    assert "今日全部会话总消耗：33,456 tokens" in text and "涉及 3 个会话" in text
    assert "- deepseek-reasoner：1 tokens" in text
    assert "DeepSeek 余额：¥110.00" in text
    assert "回答要求" not in text  # /token 给人看的输出不带给 LLM 的说明

    empty = report.build_report(day="2026-10-24", session=Usage(), total=Usage(), sessions=0, balance="x")
    assert empty.count("暂无消耗记录") == 2 and "涉及" not in empty

    for_llm = report.build_report(
        day="2026-10-24", session=Usage(), total=Usage(), sessions=0, balance="x", note=report.LLM_NOTE,
    )
    assert for_llm.endswith(report.LLM_NOTE)
    assert "¥ 表示人民币" in report.LLM_NOTE and "$ 表示美元" in report.LLM_NOTE


def test_build_top_report():
    assert "暂无" in report.build_top_report("2026-10-24", [])
    text = report.build_top_report("2026-10-24", [("qq:GroupMessage:1", Usage(3, 10, 0, 5))])
    assert "1. qq:GroupMessage:1：15 tokens（3 轮）" in text


# ---------------------------------------------------------------- balance

BALANCE_JSON = {
    "is_available": True,
    "balance_infos": [
        {"currency": "CNY", "total_balance": "110.00", "granted_balance": "10.00", "topped_up_balance": "100.00"}
    ],
}


def test_fetch_balance_success_and_cache():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json=BALANCE_JSON)

    client = balance.DeepSeekBalanceClient(transport=httpx.MockTransport(handler))

    async def scenario():
        b1 = await client.fetch("sk-test", base_url="api.deepseek.com/v1/", cache_seconds=60)
        b2 = await client.fetch("sk-test", base_url="https://api.deepseek.com", cache_seconds=60)
        b3 = await client.fetch("sk-test", cache_seconds=0)
        return b1, b2, b3

    b1, b2, b3 = run(scenario())
    assert len(seen) == 2  # 第二次命中缓存
    assert str(seen[0].url) == "https://api.deepseek.com/user/balance"
    assert seen[0].headers["Authorization"] == "Bearer sk-test"
    assert b1 is b2 and b3.is_available
    assert balance.format_balance(b1) == "人民币 ¥110.00（充值 ¥100.00，赠金 ¥10.00），可正常调用"


@pytest.mark.parametrize("response, message", [
    (httpx.Response(401, json={"error": {"message": "invalid"}}), "无效"),
    (httpx.Response(500, text="oops"), "HTTP 500"),
    (httpx.Response(200, text="not json"), "不是 JSON"),
    (httpx.Response(200, json={"foo": 1}), "无法识别"),
])
def test_fetch_balance_errors(response, message):
    client = balance.DeepSeekBalanceClient(transport=httpx.MockTransport(lambda r: response))
    with pytest.raises(balance.BalanceError, match=message):
        run(client.fetch("sk-test"))


def test_fetch_balance_network_error_and_missing_key():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    client = balance.DeepSeekBalanceClient(transport=httpx.MockTransport(handler))
    with pytest.raises(balance.BalanceError, match="网络错误"):
        run(client.fetch("sk-test"))
    with pytest.raises(balance.BalanceError, match="未配置"):
        run(client.fetch(""))


def test_format_balance_unavailable_and_usd():
    b = balance.parse_balance({
        "is_available": False,
        "balance_infos": [{"currency": "USD", "total_balance": "0.00"}],
    })
    assert balance.format_balance(b) == "美元 $0.00（充值 $0，赠金 $0），余额不足，当前无法调用 API"
    assert balance.normalize_proxy("127.0.0.1:7890") == "http://127.0.0.1:7890"


def test_format_balance_keeps_each_currency_separate_with_symbols():
    b = balance.parse_balance({
        "is_available": True,
        "balance_infos": [
            {"currency": "CNY", "total_balance": "108.73", "granted_balance": "0.00", "topped_up_balance": "108.73"},
            {"currency": "USD", "total_balance": "5.20", "granted_balance": "1.20", "topped_up_balance": "4.00"},
            {"currency": "EUR", "total_balance": "1.00", "granted_balance": "0.00", "topped_up_balance": "1.00"},
        ],
    })
    assert balance.format_balance(b) == (
        "人民币 ¥108.73（充值 ¥108.73，赠金 ¥0.00）；"
        "美元 $5.20（充值 $4.00，赠金 $1.20）；"
        "EUR 1.00 EUR（充值 1.00 EUR，赠金 0.00 EUR），可正常调用"
    )
    assert balance.normalize_proxy(" ") is None


def test_fetch_balance_rejects_bad_key_and_bad_config():
    client = balance.DeepSeekBalanceClient()
    for bad_key in ("sk-\u200babc", "sk-abc\n", "sk-ａｂｃ"):
        with pytest.raises(balance.BalanceError, match="全角或不可见"):
            run(client.fetch(bad_key))
    # 全角冒号、非法端口这类配置错误不属于 httpx.HTTPError，也要转成 BalanceError
    with pytest.raises(balance.BalanceError, match="请求参数有误"):
        run(client.fetch("sk-test", proxy="127.0.0.1：7890"))
    with pytest.raises(balance.BalanceError, match="请求参数有误"):
        run(client.fetch("sk-test", base_url="https://api.deepseek.com:abc"))


def test_fetch_balance_caches_failures_and_hides_secrets():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500, text="bad key sk-secret-123")

    client = balance.DeepSeekBalanceClient(transport=httpx.MockTransport(handler))
    with pytest.raises(balance.BalanceError) as first:
        run(client.fetch("sk-secret-123"))
    with pytest.raises(balance.BalanceError) as second:
        run(client.fetch("sk-secret-123"))
    assert len(calls) == 1  # 失败结果被缓存，排队的请求不会再等一次超时
    assert "sk-secret-123" not in str(first.value) and "***" in str(first.value)
    assert second.value is first.value

    def refuse(request):
        raise httpx.ConnectError("connect to Bearer sk-secret-123 failed", request=request)

    client = balance.DeepSeekBalanceClient(transport=httpx.MockTransport(refuse))
    with pytest.raises(balance.BalanceError) as err:
        run(client.fetch("sk-secret-123"))
    assert str(err.value) == "网络错误（ConnectError）"


def test_store_prune_clamps_huge_retention(tmp_path):
    store = usage_store.UsageStore(tmp_path / "usage.db")
    run(store.add("2026-10-24", "s", "m", Usage(1, 1, 0, 0)))
    assert run(store.prune("2026-10-24", 999999)) == 0
    store.close()


def test_session_label():
    assert report.session_label(is_group=False, isolated=False) == "当前会话"
    assert "全群成员合计" in report.session_label(is_group=True, isolated=False)
    assert report.session_label(is_group=True, isolated=True) == "你在本群的独立会话"
    text = report.build_report(
        day="2026-10-24", session=Usage(1, 1, 0, 0), total=Usage(1, 1, 0, 0), sessions=1,
        balance="x", session_label="本群会话（全群成员合计，无法区分到个人）",
    )
    assert "· 本群会话（全群成员合计，无法区分到个人）今日消耗：1 tokens" in text
