"""用真实的 AstrBot 加载插件并跑完整的 Agent 流程，检查记账结果；未安装 AstrBot 时自动跳过：

    pip install AstrBot pytest
    python -m pytest tests

每个场景在子进程里运行（tests/astrbot_driver.py），因为 AstrBot 在导入时就固定了数据目录。
"""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

if importlib.util.find_spec("astrbot") is None:
    pytest.skip("未安装 AstrBot", allow_module_level=True)

ROOT = Path(__file__).resolve().parent.parent
DRIVER = Path(__file__).resolve().parent / "astrbot_driver.py"
PLUGIN_FILES = ["main.py", "capture.py", "balance.py", "report.py", "usage_store.py",
                "metadata.yaml", "_conf_schema.json"]

# 另一个插件：提供一个直接发送结果、返回 None 的工具，以及一个会 stop_event() 的 on_llm_response 钩子
HELPER_MAIN = '''
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


class Helper(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.llm_tool(name="send_picture")
    async def send_picture(self, event: AstrMessageEvent):
        """发送一张图片"""
        return None

    @filter.on_llm_response()
    async def block(self, event: AstrMessageEvent, resp):
        if event.get_extra("helper_stop"):
            event.stop_event()
'''
HELPER_META = "name: aaa_helper\ndesc: test helper\nversion: v0.0.1\nauthor: test\nrepo: https://example.com\n"

WHOLE_RUN = [620, 500, 110]


def run_scenario(tmp_path: Path, scenario: str) -> dict:
    plugins = tmp_path / "data" / "plugins"
    target = plugins / "astrbot_plugin_token_usage"
    target.mkdir(parents=True)
    for name in PLUGIN_FILES:
        shutil.copy(ROOT / name, target / name)
    helper = plugins / "aaa_helper"
    helper.mkdir()
    (helper / "main.py").write_text(HELPER_MAIN, encoding="utf-8")
    (helper / "metadata.yaml").write_text(HELPER_META, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(DRIVER), str(tmp_path), scenario],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
        env={**__import__("os").environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def booked(result) -> list[int]:
    rows = result["rows"]
    return [sum(r[i] for r in rows) for i in (3, 4, 5)]


@pytest.mark.parametrize("scenario", ["tool_then_answer", "tool_then_answer_stream"])
def test_whole_run_including_tool_step(tmp_path, scenario):
    result = run_scenario(tmp_path, scenario)
    assert result["stats"] == WHOLE_RUN
    assert result["rows"] == [["qq:GroupMessage:g1", "deepseek-chat", 1, 620, 500, 110]]
    # 工具返回给 LLM 的文字：调用工具前的那次请求已计入；标明群聊会话是全群合计；余额失败不影响 token 数据
    report = result["tool_results"][0]
    assert "本群会话（全群成员合计，无法区分到个人）今日消耗：530 tokens" in report
    assert "查询失败（测试中不联网）" in report


def test_tool_that_sends_directly_still_books(tmp_path):
    # 工具返回 None 时 AstrBot 直接结束本轮，不会触发 on_llm_response
    result = run_scenario(tmp_path, "direct_tool")
    assert result["stats"] == [500, 0, 30]
    assert booked(result) == [500, 0, 30]
    assert result["rows"][0][2] == 1


def test_error_after_tool_step_keeps_finished_steps(tmp_path):
    result = run_scenario(tmp_path, "fail_after_tool")
    assert booked(result) == [500, 0, 30] == result["stats"]


def test_other_plugin_stopping_event_does_not_skip_booking(tmp_path):
    result = run_scenario(tmp_path, "stopped_by_other_plugin")
    assert booked(result) == WHOLE_RUN == result["stats"]
