# Token 用量统计（astrbot_plugin_token_usage）

AstrBot 插件：按会话记录每天消耗的 token，并查询 DeepSeek 账户余额。插件给 LLM 注册了一个工具，用户在聊天里直接问「今天用了多少 token」「余额还剩多少」，Bot 就能如实回答。

## 功能

- **按会话记账**：把每轮对话消耗的输入 token（区分缓存命中）和输出 token 记到「日期 × 会话 × 模型」的账本里。
- **工具调用也算进去**：一轮对话里 LLM 调用工具时会发出多次请求，插件记录的是主 Agent 这一轮所有请求的累计用量，不只是最后一次。工具直接发送结果（比如画图、点歌）而不再回复时，这一轮也会照常记账。
- **DeepSeek 余额**：调用 DeepSeek 官方 `/user/balance` 接口，显示总余额、充值余额和赠金余额。人民币和美元余额分开列出，每个金额都带货币符号（¥ / $），并要求 LLM 回答时保留符号、不把两种币种相加或换算。
- **LLM 工具 `query_token_usage`**：返回当前会话今日消耗、今日所有会话总消耗和 DeepSeek 余额，由 LLM 用自己的口吻转述。
- **指令**：
  - `/token`：查看当前会话今日用量、今日总用量（按模型拆分）和余额
  - `/token top`：查看今日消耗最多的 10 个会话。仅 AstrBot 管理员可用，并且只能在私聊中使用，因为结果会列出其他会话的 ID（含私聊用户号）

## 安装

把整个文件夹放进 AstrBot 的 `data/plugins/` 目录后重启，或在 WebUI 的插件市场里通过仓库地址安装。依赖只有 `httpx`，AstrBot 本身已自带。

要求 AstrBot ≥ 4.12.2。

## 配置

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| 时区 `timezone` | 用来划分「今天」。支持 `UTC+8`、`+08:00` 或 `Asia/Shanghai`（Windows 需安装 tzdata）；留空使用服务器本地时间 | `UTC+8` |
| 保留天数 `retention_days` | 超过天数的记录自动清理，`0` 为永久保留 | `90` |
| 仅管理员可查询余额 `balance_admin_only` | 开启后，非管理员问余额时 Bot 不会透露 | 关 |
| DeepSeek API Key `deepseek.api_key` | 查询余额用的 Key | 空 |
| 自动读取提供商 Key `deepseek.auto_detect_key` | Key 留空时，使用 AstrBot 里 API 地址包含 `api.deepseek.com` 的提供商的 Key。只在下面的 API 地址是官方地址时生效 | 开 |
| API 地址 `deepseek.base_url` | 末尾的 `/v1` 会自动去掉；改成其他地址时需要手动填写 API Key | `https://api.deepseek.com` |
| 代理 `deepseek.proxy_url` | 例如 `http://127.0.0.1:7890`；留空使用环境变量中的代理 | 空 |
| 超时 / 缓存 | 请求超时秒数（3～30）；缓存时间内重复查询直接使用上次结果。查询失败后 30 秒内不会重复请求 | `15` / `60` |

如果你已经在 AstrBot 里配置了 DeepSeek 官方提供商，通常不需要任何配置就能直接使用。

## 使用示例

> 用户：今天我们聊天花了多少 token？余额还够吗？
>
> Bot：今天咱们这个会话用了 18,342 个 token，大部分是输入（其中 1 万多命中了缓存，比较省钱）；今天所有会话加起来一共 52,107 个 token。DeepSeek 账户还剩 ¥108.73，放心用~

## 统计口径

- **会话**按 `unified_msg_origin` 区分：默认同一个群是一个会话（全群成员合计，不区分个人），每个私聊各是一个会话；开启 AstrBot 的「隔离对话」后，群里每位成员各是一个会话。工具返回给 LLM 的文字里会注明这一点，避免 Bot 把全群的用量说成提问者个人的。
- **一轮对话**指 AstrBot 主 Agent 的一次运行（从收到消息到 Bot 给出最终回复）。插件在每次调用工具前和这一轮结束时，各记一次新增的用量，所以：
  - 中途出错时，出错前已经完成的请求会计入；
  - 在对话中调用工具查询时，这一轮里查询之前的请求已经计入，Bot 读完结果后生成回复的那次请求要等这一轮结束才计入。
- **以下请求不计入**，所以统计值可能低于 DeepSeek 账单：
  - SubAgent（`transfer_to_*` 交接工具）内部的请求；
  - 工具调用模式为 skills_like（两阶段）时，AstrBot 为补全工具参数额外发出的请求；
  - AstrBot 自身的辅助请求：上下文超长时的 LLM 压缩摘要、图片转述（含引用图片）、WebChat 标题生成，以及返回空内容或被内容过滤后自动重试的请求；
  - 其他插件直接调用模型提供商发出的请求；
  - 使用 Dify / Coze / 阿里云百炼 / DeerFlow 等第三方 Agent 执行器时的请求（它们不返回用量）；
  - 在某个配置文件中停用了本插件的会话。
- 统计所有模型提供商的消耗，不只 DeepSeek；按模型拆分时，以发出请求时正在使用的提供商为准。余额只支持 DeepSeek 官方接口，第三方中转服务一般没有余额接口。
- 整轮累计用量是从 AstrBot Agent Runner 的内部统计里读取的。如果将来 AstrBot 改了内部实现、读不到这份统计，插件会退回到只记录每轮最后一次请求的用量（工具调用时会少算一些），不影响正常聊天。

## 数据存储

用量记录保存在 `data/plugin_data/astrbot_plugin_token_usage/usage.db`（SQLite），每个「日期 × 会话 × 模型」一行，体积很小。

## 开发与测试

核心逻辑（账本、用量提取、余额查询、报告文本）不依赖 AstrBot，可以单独测试：

```bash
pip install httpx pytest
python -m pytest tests
```

装了 AstrBot 时（`pip install AstrBot`），`tests/test_astrbot_integration.py` 会用真实的 Agent Runner 加载插件、跑完整的工具调用流程，检查记账结果；没装时自动跳过。
