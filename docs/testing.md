# 测试入口与验收范围

在仓库根目录执行以下命令。日常默认 L1，依据改动的行为、接口和直接依赖选择测试。

| 级别 | 命令 | 范围 | 桌面影响 |
| --- | --- | --- | --- |
| L1 定向测试 | `py -3 tools/run_tests.py -p test_ui_theme.py -k main_menu` | 主题菜单用例。省略 `-k` 可运行整个文件 | 无 |
| L2 模块回归 | `py -3 tools/run_tests.py -p test_ui_theme.py -p test_ui_draw.py` | 本次涉及的主题和共享绘图库。按需重复 `-p` | 无 |
| L3 完整离线回归 | `py -3 tools/run_tests.py --all` | `tests/` 中的离线用例，适用于发布、重大共享变更或明确要求 | 无 |
| L4 原生控件 | `py -3 tools/run_tests.py --windows --allow-desktop` | `tools/windows_tests/` 中的 Tk 和 ttk 原生行为 | 可见窗口及焦点变化 |
| L4 原生窗口专项 | `py -3 tools/run_tests.py --desktop-script check_frameless_global --allow-desktop -- --layout-only --themes modern` | 明确选择一个现有脚本，`--` 后为原脚本参数 | 布局模式会打开和切换窗口。完整模式还会发送原生输入 |

`--list` 只发现并列出 unittest 测试 ID，例如 `py -3 tools/run_tests.py --all --list` 和 `py -3 tools/run_tests.py --windows --list`。`-p` 接受文件名和 glob，PowerShell 下请给 glob 加引号。`-k` 按用例名过滤。失败、导入错误或无匹配用例均返回非零。使用较小的范围复现失败，报告 Passed、Failed、Skipped、Not run、Manual review required 和 Archived 的实际状态。

离线入口为每次运行建立临时 APPDATA 和 LOCALAPPDATA，并在本进程拦截真实 Tk 根窗口。原生通知、窗口、对话框、文件选择器、鼠标键盘和远程请求相关的离线行为使用 mock 或合成数据，HTTP 集成使用本机回环。测试需关闭自己的数据库、线程、服务、定时器及子进程。`tests/` 仅用于离线发现，运行器提供额外的配置隔离和 Tk 保护。

## Windows 专项验收

根据任务选一个脚本。运行前说明实际影响并取得授权。参数说明可直接查看脚本或其 `--help`。真实热键、鼠标、键盘和通知需要交互式 Windows 会话。保留开发者当前会话时，使用独立测试桌面。使用隔离的 Windows 会话和一次性安装目录验收 EXE、Updater 或 Repair，旧产物可能使用共享单实例互斥量。只清理该次测试拥有的进程和资源。

```powershell
py -3 tools/run_tests.py --desktop-script check_global_hotkey --allow-desktop
py -3 tools/run_tests.py --desktop-script check_native_notifications --allow-desktop -- --report "$env:TEMP\calendar-native-qa.json"
py -3 tools/run_tests.py --desktop-script check_direct_schema8_exe --allow-desktop -- $qaExe "$env:TEMP\calendar-exe-qa.json"
```

`$qaExe` 应预先设置为已存在的 QA 版 `DesktopCalendar.exe` 路径。热键检查会注册热键并发送输入。通知检查需要预先构建原生通知 DLL，会创建并清理本次测试的 Windows 通知注册、开始菜单快捷方式和通知。旧构建、调试或视觉脚本同属专项验收。涉及共享视觉时验证 Modern 和 Slate。FA、Glass 和 Paper 源码保留，离线只验证历史配置和回退。

`tools/manual_sqlite_rc.py` 和 `tools/check_storage_upgrade_button.py --manual-exe` 是需要持续运行测试进程的手工入口。仅在隔离会话中按专项步骤运行，记录脚本输出的测试 PID，结束时正常关闭该测试实例。

### v0.14.0 专项入口

以下命令都需要单独授权。为截图及日志使用独立的 `$qaOutput`，并在一次性 Windows 测试会话运行有关系统资源的场景。

| 入口 | 范围与影响 |
| --- | --- |
| `--desktop-script check_frameless_global --allow-desktop -- --layout-only` | 使用活动主题和注入 DPI 检查真实窗口布局。完整模式另含原生输入 |
| `--desktop-script check_global_sidebar --allow-desktop -- --theme modern --dpi 96` | 用 96 和 192 DPI 分别配合 Modern、Slate 验证右栏开合、快照、快捷添加与贴右抽屉。只操作该次测试窗口 |
| `--desktop-script check_reminder_ui_special --allow-desktop -- --output $qaOutput` | 独立日历与固定时钟。四栏提醒计划、筛选、晨晚简报和 DPI 截图。用本地夹具模拟 QQ 状态 |
| `--desktop-script check_reminder_v0140 --allow-desktop -- --visual-only --output $qaOutput` | 四栏与隐私视觉检查。用 `--lifecycle-only` 验证窗口和托盘生命周期，通知后端为替身。真实八项聚合投递选择 `--send-native` |
| `--desktop-script check_reminder_ef_native --allow-desktop -- --send-native --output $qaOutput` | 隔离 SQLite 和真实 WinRT 提交，可用 `--modes` 限定场景。测试探针只匹配本次短期合成标题。提交证据和屏幕可见证据分别记录 |
| `--desktop-script check_native_reminder_app --allow-desktop -- --output $qaOutput` | 真实 Windows 通知的稍后与事项打开，需要人手点击合成通知 |

所有例行原生界面矩阵使用共享活动主题注册表。原生通知运行前需要原生 DLL。通知脚本与 UI 夹具均使用本次测试档案，QQ 实机验收属于独立 Live 流程。

## 协议与发布候选专项

`pwsh -NoProfile -File tools/check_proactive_schema.ps1` 使用 PowerShell 7.4 及以上的本地 Draft 2020-12 校验器，验证协商、投递和降级载荷。运行时跨字段约束由端云离线回归覆盖。候选包和后续实机门禁参见 [Windows 候选验收手册](v0.14-windows-rc-qa.md)。

## Live 模型与远程验收

这些命令会访问实际服务。先取得专项授权，并为本次运行明确选择独立 QA 凭据、服务端和数据。`$qaModelConfig` 应预先设置为 QA 在当前 Windows 用户下生成的 `configuration.dpapi` 路径，评测使用合成日历和调用预算。微信探针通过 `CALENDAR_SERVER_URL` 使用指定的 QA 服务。带 `--isolated-test-queue` 的操作还需要该服务的 QA Worker 凭据以及专用测试队列，详见 [微信探针](wechat-b2-probe.md)。

```powershell
py -3 -m tools.calendar_agent_eval --mode live --config $qaModelConfig --cases a01-create-tomorrow --max-calls 6
py -3 tools/wechat_b2_probe.py
py -3 tools/wechat_b2_probe.py --isolated-test-queue
```
