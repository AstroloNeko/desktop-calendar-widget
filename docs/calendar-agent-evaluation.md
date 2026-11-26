# Calendar Agent 行为评测

评测器通过生产 `HeadlessRuntime` 和 `CalendarChatService` 执行合成会话。每次尝试使用独立的临时 SQLite 日历和 ChatJournal，按案例提供的本地日期与 UTC 偏移量运行。判定分别检查模型提案、宿主交互、业务回执和实际落盘状态。

## 运行

从仓库根目录执行。默认模式是 Mock，适合 CI 和评测器回归。

```powershell
python -m tools.calendar_agent_eval
python -m tools.calendar_agent_eval --mode mock --cases core --repeat 2
py -3 tools/run_tests.py -p test_calendar_agent_eval.py
```

`--cases` 支持 `all`、`core`、类别或逗号分隔的案例 ID。每个案例可运行 1 至 3 次，每次最多选择 40 个案例。Mock 模式使用案例中的固定工具响应。它能检验宿主执行，并可通过篡改 mock 提案验证裁判识别缺失字段、错写和重复写入的能力。

### 备忘录输入评测

独立的 `tools/calendar_agent_eval_memo_cases.json` 包含 20 个备忘录案例和 3 个专项复测变体，固定消息时间为 2026-11-19 10:00（Asia/Shanghai）。原有 40 案和历史报告保留，可复跑原始 C01、C04、D01 作对照。[备忘录产品规则](calendar-agent-memo-input.md)

```powershell
python -m tools.calendar_agent_eval --dataset tools/calendar_agent_eval_memo_cases.json --mode mock
python -m tools.calendar_agent_eval --dataset tools/calendar_agent_eval_memo_cases.json --mode live --config $qaModelConfig --cases core --max-calls 12
python -m tools.calendar_agent_eval --dataset tools/calendar_agent_eval_memo_cases.json --mode live --config $qaModelConfig --cases all --max-calls 64
python -m tools.calendar_agent_eval --mode live --config $qaModelConfig --cases c01-select-second,c04-affirm-draft,d01-non-action --max-calls 12
```

Mock 模式下版本号歧义和抱怨回复仍保留人工复核。CLI 对支持案例的待复核返回非零，查看报告可区分这些结果与测试错误。

### 可靠性修复前基线

提交 `c100516` 保留旧案例、裁判与文档。以下真实原始记录仍位于 `build/calendar-agent-eval/`，每个运行目录有 `cases.jsonl`、`summary.json` 和 `report.md`。表内摘要用于核对原始逐轮轨迹，含模型调用、Pending、回执和 SQLite 差异。

| 运行 | `cases.jsonl` SHA-256 |
|---|---|
| `run-20261119-085552-ac6da762`（冒烟） | `e3dc5b25e082f76d63e73d665f19a32c915707222468492d6b4463dcf0e1cedb` |
| `run-20261119-085836-0fc86cf2`（Memo 完整） | `44e798c21e46683102cada40b6124fbcbace7e96a4f2f4ecc12d7d7da90ae2ef` |
| `run-20261119-085757-36065c9e`（旧案例复测） | `8de80c09d8424ca6c59de1321ebc37fb74d5eab2961db97aa1086a83c3947fe6` |
| `run-20261119-085915-4a4616ec`（跨日补跑） | `4c0896860a497df08e23df791e13a5e607ee780a74c8a0f0abc20cdc05483aee` |

本轮只调整两类预期。DDL 用例仍要求结构化提案及最终 Event 为 `ddl`，标题可省略 `DDL`。C04 和 Memo D19 在首次回复要求显示可执行的待确认预览且零写入，用户认可以后才检查事务与最终成员。历史运行的原始答案与判定留在原报告中。旧 C04 固定评测分日草案，Memo D19 固定评测跨日草案，真实核心复测使用符合最新推荐交互的 D19。

### Live DeepSeek

以下入口会发起计费请求。获得 Live 授权后，将 `$qaModelConfig` 设为当前 Windows 用户下生成的独立 QA `configuration.dpapi` 路径。脚本以只读方式加载该配置，只传输合成案例及其合成日历上下文。评测 Runtime 使用单独的临时配置，HTTP 不启动。

```powershell
python -m tools.calendar_agent_eval --mode live --config $qaModelConfig --cases a01-create-tomorrow,a02-wb-two-days --max-calls 12
```

Live 必须提供 `--config`，评测器只按该路径读取配置。DPAPI 要求使用配置所属的 Windows 用户。若密钥、远程同意或合法 HTTPS 端点不可用，命令会在模型调用前退出。

Live 默认选择 `core`，全次运行共享最多 120 次模型调用的预算。用 `--max-calls` 可以下调预算。每次调用的超时最多 30 秒，并受生产工具循环的剩余时间限制。网络失败、限流和 API 错误由 Provider 报为基础设施问题。现有 Provider 将部分网络错误归并为 `model_unavailable`。

## 案例与预期

数据集位于 `tools/calendar_agent_eval_cases.json`。使用合成记录和稳定的 `case_id`。保留同一个 ID 可以跨版本对比；调整测试语义时用新的数据集摘要标识基线。

| 字段 | 含义 |
|---|---|
| `category`、`support`、`tags` | 选择范围和分组。支持状态为 `supported`、`exploratory` 或 `unsupported` |
| `reference_datetime`、`timezone` | 带明确 UTC 偏移量的本地时间和时区标签。偏移量决定本次解析基准 |
| `initial_calendar` | 合成 Event 列表，含稳定 ID，字段沿用创建工具输入 |
| `turns` | `message`、`select`、`confirm`、`cancel`、`restart` 或 `receipt` |
| `expected_plan` | 按 1 起始的轮次匹配动作，可匹配目标、字段或独立成员。成员检查数量与字段，顺序可变 |
| `expected_interaction` | 每轮预期结果和事务数，可核对错误码、候选、回执或人类可读文案 |
| `expected_final_state` | `create`、`update`、`delete` 与 `writes`。未列入变更的已有 Event 应保持原样 |

- 每个消息轮次的 `mock` 是模型轮次数组。每轮包含一组工具调用，名称为 `name`，参数为 `arguments`。用 `[[]]` 模拟模型返回无工具响应，用 `[]` 表示该轮应由宿主本地处理。
- `when.kind` 指定下一轮发送条件。澄清回复可加 `when.question_any`。遇到其他澄清时会停下脚本并标记待复核。对话评测允许人审这些分歧。
- 用 `save_as` 保存当前操作的 ID 和 revision，后续用 `ref` 引用。旧卡片确认可以直接引用旧版本。选择始终使用返回的候选和 selection revision。
- 修订目标的模拟调用可写 `"$target_revision"`，执行时从真实待修订计划取 revision。重启会停止当前 Runtime 并重新打开同一尝试的临时数据库。
- 对只读、取消或拒绝操作，显式声明 `writes: 0`。同名创建仍按成员数验证。改色看有效颜色，确认后要能关联对应 SQLite 事务和执行回执。
- 新建标题有无害的排版差异时，可在预期成员或新建结果中用 `"title": {"one_of": ["WB实验", "WB 实验"]}` 显式列出等价标题。固定的检索目标与用户指定的精确标题继续逐字核对。
- `also_accept` 可列出零写入的合理替代回复；SQLite 回执与数据变化仍独立核对。`review_reply` 指定需要人工评审的回复，自动裁判保留待复核。`update_created_on` 校验后续修改作用在指定创建轮次的原 ID。单事项修改可按目标条件提案，或使用由该轮创建结果或 `resolved_id` 预置的同一 ID，Patch 和实际写入仍独立核对。备注可用 `{"contains":"科研楼302"}` 核对通知中的明确地点。

## 结果与对比

每次运行写入 `build/calendar-agent-eval/run-.../`。可用 `--output` 改用其他报告目录。

- `summary.json` 记录 Git HEAD、工作区是否有改动、数据集 SHA-256、模式、模型、筛选项、通过率和重跑波动。
- `cases.jsonl` 每行一份尝试。含预期条件、每轮输入、结构化工具调用、Pending、诊断、审计、操作 ID、业务回执与 SQLite 差异。异常会记录失败阶段和错误类型。
- `report.md` 汇总各类结果、分维度判定、非操作误写入及失败案例复跑参数。

判定为 `pass`、`fail`、`needs_review` 或 `infrastructure_error`。待复核不会计作通过。支持范围内的非通过、基础设施错误或任何误写入都会使 CLI 返回非零。探索和明确不支持的案例仍保留发现和证据，供单独审查。

跨版本对比时固定数据集摘要、案例选择、重复次数、模型和模式。生成的 UUID 与耗时可以变化，判断结果以逐成员匹配、目标 ID、回执和写入次数为准。本版不导出模型系统 prompt 或推理文本。API Key、HTTP token 和常见认证字段会脱敏。当前生产 Provider 只提供模型消息，Token 和费用字段保留为 `null`。

真实模型评测需要显式启动。若 Mock 发现生产路径的行为差异，保留逐轮记录和复现案例，再单独评估产品修复。
