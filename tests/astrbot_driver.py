"""由 test_astrbot_integration.py 在子进程中运行：用真实的 PluginManager 加载插件，
再用真实的 ToolLoopAgentRunner + MAIN_AGENT_HOOKS + FunctionToolExecutor 跑一轮对话，
把记账结果以 JSON 打印到最后一行。

用法：python astrbot_driver.py <astrbot_root> <scenario>
"""

import asyncio
import json
import os
import sqlite3
import sys

ROOT, SCENARIO = sys.argv[1], sys.argv[2]
os.environ["ASTRBOT_ROOT"] = ROOT
os.chdir(ROOT)
sys.path.insert(0, ROOT)

STREAMING = SCENARIO.endswith("_stream")
TOOL = "send_picture" if SCENARIO == "direct_tool" else "query_token_usage"


async def main():
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.agent.tool import ToolSet
    from astrbot.core.astr_agent_context import AstrAgentContext
    from astrbot.core.astr_agent_hooks import MAIN_AGENT_HOOKS
    from astrbot.core.astr_agent_run_util import run_agent
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from astrbot.core.config.astrbot_config import AstrBotConfig
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.provider.entities import LLMResponse, ProviderRequest, TokenUsage
    from astrbot.core.provider.provider import Provider
    from astrbot.core.provider.register import llm_tools
    from astrbot.core.star.context import Context
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_manager import PluginManager

    class DeepSeekProvider(Provider):
        def __init__(self):
            super().__init__({"id": "deepseek", "key": ["sk-test"], "api_base": "https://api.deepseek.com/v1"}, {})
            self.calls = 0

        def get_current_key(self):
            return "sk-test"

        def set_key(self, key):
            pass

        async def get_models(self):
            return ["deepseek-chat"]

        def get_model(self):
            return "deepseek-chat"

        async def text_chat(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    role="assistant", tools_call_name=[TOOL], tools_call_args=[{}], tools_call_ids=["c1"],
                    usage=TokenUsage(input_other=500, input_cached=0, output=30),
                )
            if SCENARIO == "fail_after_tool":
                raise RuntimeError("upstream 500")
            return LLMResponse(
                role="assistant", completion_text="done",
                usage=TokenUsage(input_other=120, input_cached=500, output=80),
            )

        async def text_chat_stream(self, **kwargs):
            resp = await self.text_chat(**kwargs)
            resp.is_chunk = True
            yield resp
            resp.is_chunk = False
            yield resp

    provider = DeepSeekProvider()

    class Ctx(Context):
        def __init__(self):
            pass

        def get_all_providers(self):
            return [provider]

        def get_using_provider(self, umo=None):
            return provider

        def get_all_stars(self):
            return list(star_map.values())

        def get_config(self, umo=None):
            return {}

    ctx = Ctx()
    manager = PluginManager(ctx, AstrBotConfig(config_path=os.path.join(ROOT, "data", "cmd_config.json")))
    for name in sorted(os.listdir(os.path.join(ROOT, "data", "plugins"))):
        ok, err = await manager.load(specified_dir_name=name)
        assert ok, f"load {name} failed: {err}"
    md = star_map["data.plugins.astrbot_plugin_token_usage.main"]
    plugin = md.star_cls
    balance_mod = sys.modules["data.plugins.astrbot_plugin_token_usage.balance"]

    async def no_network(*args, **kwargs):
        raise balance_mod.BalanceError("测试中不联网")

    plugin.balance.fetch = no_network

    class Event(AstrMessageEvent):
        async def send(self, message):
            pass

    msg = AstrBotMessage()
    msg.type = MessageType.GROUP_MESSAGE
    msg.self_id = "bot"
    msg.session_id = "g1"
    msg.message_id = "m1"
    msg.sender = MessageMember(user_id="u1", nickname="u")
    msg.message = []
    msg.message_str = "今天用了多少 token"
    msg.raw_message = None
    event = Event("今天用了多少 token", msg, PlatformMetadata(name="aiocqhttp", description="", id="qq"), "g1")
    if SCENARIO == "stopped_by_other_plugin":
        event.set_extra("helper_stop", True)

    runner = ToolLoopAgentRunner()
    await runner.reset(
        provider=provider,
        request=ProviderRequest(prompt="hi", func_tool=ToolSet(tools=[llm_tools.get_func(TOOL)]), contexts=[]),
        run_context=ContextWrapper(context=AstrAgentContext(context=ctx, event=event)),
        tool_executor=FunctionToolExecutor(),
        agent_hooks=MAIN_AGENT_HOOKS,
        streaming=STREAMING,
    )
    async for _ in run_agent(runner, max_step=5, show_tool_use=False):
        pass

    tool_results = [str(m.content) for m in runner.run_context.messages if m.role == "tool"]
    db = sqlite3.connect(os.path.join(ROOT, "data", "plugin_data", "astrbot_plugin_token_usage", "usage.db"))
    rows = db.execute("SELECT umo, model, rounds, input_other, input_cached, output FROM daily_usage").fetchall()
    db.close()
    await manager._terminate_plugin(md)
    stats = runner.stats.token_usage
    print(json.dumps({
        "rows": rows,
        "stats": [stats.input_other, stats.input_cached, stats.output],
        "tool_results": tool_results,
    }, ensure_ascii=False))


asyncio.run(main())
