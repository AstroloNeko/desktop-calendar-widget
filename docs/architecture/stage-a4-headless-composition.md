# Stage A4：无窗口运行时组装

基线 `d6ff0b1`，997 项测试；分支 `refactor/headless-calendar-composition`。
只做组装与平台边界，不迁移存储、不调整 Agent 策略、不进入 B1。

## 旧路径与实际拆分

原 calendar_chat_ui.ChatRuntime 自行构造配置、Journal、Gateway、Provider、
ChatService 和 HTTP；Gateway 文件末尾还定义 TkDispatcher。
业务服务本身已经可以测试，但产品组装依赖 CalendarApp。

```text
Desktop ChatRuntime（窗口、jobs、callback、Modal/退出策略）
    -> CalendarRuntime（共用组装）
    -> CalendarChatService -> Gateway -> TkDispatcher -> Tk owner

HeadlessRuntime（显式 data_file，无 Root）
    -> 同一个 CalendarRuntime / ChatService / Gateway
    -> SerialExecutor -> 单个 calendar-business 线程
    -> 同一个 Store / EventCommandHistory / A2 / A3
```

tk_dispatcher.py 原样保留桌面分派行为，旧 Gateway 导出保持兼容。
没有修改 app.py、托盘、窗口样式、Modal 或 DPI 代码。

## 公开入口与所有权

`HeadlessRuntime(data_file, provider=..., protector=..., configuration=...,
journal=..., clock=..., notification_backend=..., timeout=10)`，支持 with。
必须显式提供数据路径，不隐式选择用户日历。构造过程在业务线程创建 Store
及唯一历史栈。该 Runtime 不自动启动 HTTP、后台提醒循环或永久服务。

- `events`：A2 方法的串行 facade，含预览、提交、批量。
- `mutations`：A3 方法的串行 facade，Habit/Category/日期状态。
- `query(arguments)`、`snapshot()`：脱离 Store 的读取结果。
- `undo()` / `redo()`：原共享混合历史。
- `service`：现有 CalendarChatService，同一工具和确认策略。
- `apply_http()`：仅明确配置启用后启动原 LocalChatHTTP。
- `reminder_tick(now)`：仅显式注入 backend 后可调用；无新增调度循环。
- `stop(timeout=10)`：可重复关闭。

facade 深复制输入与结果，避免返回 canonical 可变对象；查询、预览、提交均
在执行器内。A3 的候选 date_states 临时覆盖不会与其他业务任务交错。
Store 仍是唯一业务状态来源。Python 私有成员不是安全沙箱；受信任的组装代码
仍能访问内部 Store，未来 B1 应进一步收紧仓储接口，调用者不得绕开 facade。

SerialExecutor 只执行短业务任务。LLM 网络等待和 ChatJournal 编排在调用线程
或 HTTP handler，不持有业务执行器等待网络。嵌套业务 dispatch 同线程直接执行。
Future 将异常返回原调用者：排队超时成功 cancel 后不会执行；已开始的超时
返回 execution_uncertain，不重试。stop 拒绝新任务、取消排队任务，等待正在
保存的任务完成。超时不强杀线程、不报告已经关闭。

## 共用业务与兼容

CalendarRuntime 允许传入现有 Store/history/dispatch、刷新端口、writable gate、
Provider、Journal 和 clock。桌面传入 app.event_commands，不创建第二套历史。
Headless 只创建一个 history，A2、A3、Gateway 和 Undo 全部共享。

Habit 规则/历史、分类删除颜色冻结、日期范围一次保存、MutationUncertainError、
失败回滚保持不变。load_error 阻断后续业务写入。分类成功失效历史、Habit
删除/打卡不新增 Undo 的语义不变。JSON Schema 9、备份格式、目录、ChatJournal
schema、Prompt、工具注册、两级批量删除、HTTP 路由均未改。

clock 继续注入现有 ChatService 的过期/审计时钟；不是全项目替换时间来源。
日历既有 local date.today/datetime 语义仍保留，业务 API 的具体日期可显式提供。

## 设备能力

AIConfiguration 的既有 protector 注入点继续使用，默认 UserProtection/DPAPI。
正式路径失败不回退明文，配置格式不变。测试用临时、仅内存映射的 opaque
protector：磁盘只写随机句柄，不写明文密钥；不能拿它当生产 SecretStore。
ChatJournal 每次操作独立打开/关闭 SQLite 连接，不持有待关闭长连接。

ReminderEngine 本来已接收 backend 且无 Tk。Headless 可注入 backend，成功
变更/Undo 后 invalidate，显式 tick；桌面继续原 NativeReminderMixin/Windows
适配，不新增通知协议或改变设备投递账本。

desktop_preferences.py 仅保存主题 ID、别名和归一化，不含 token/绘制。
calendar_core 不再导入 ui_theme；ui_theme 重新导出同一归一化函数，保留
modern/aero/paper/frutiger/slate 及旧别名兼容。Event/Category 用户颜色未改变。

## 生命周期与关闭

状态 starting -> running -> stopping -> stopped；初始化失败为 failed。
无重启同一实例入口，重新启动需创建新 Runtime。初始化异常清理执行器。
HTTP 启动失败无遗留监听，可继续使用业务服务并修正配置。

Headless stop 先停止 Chat 新写入及 HTTP 接收，然后有界等待活动 HTTP/模型
请求，最后关闭执行器。LocalChatHTTP 新增活动请求计数与可选 wait_requests，
只影响关闭协调；Token/Host/Origin、127.0.0.1、请求限制全部保留。
超时保持 stopping 并返回错误，调用者在任务结束后可再次 stop。不能声称
在同步网络请求超时之前强行释放了其线程，也不自动重放未知结果。

Desktop 不在 Tk owner 上等待可能等待 TkDispatcher 的模型线程，避免死锁。
继续使用原 bounded daemon jobs，停止后不再入队或执行 UI callback；已在网络
中的请求按原 Provider 上限结束，后续业务写入被 gate 拒绝。此点不是新增
“强制取消网络”能力。托盘隐藏/真正退出行为不变。

业务保存与 UI 刷新仍分开；测试验证刷新报错不会导致重复写入。JSON 与 AI
SQLite 回执依旧不是共同 ACID 事务，不声称 exactly-once。

## 测试证据

- 基线 997/997（27.994 秒）；新增 test_calendar_runtime.py **31 项**。
- 完整 **1028/1028**（29.898 秒）通过；A1/A2/A3、Habit、分类、日期、Chat、备份/窗口回归包含在内。
- 无窗口 Event CRUD/持久化，Habit 周期修改、打卡、历史、重启；混合及批量 Undo。
- Mock LLM 经真实 Registry/Gateway 完成创建、多轮改名、查询、删除 Pending/确认。
- 真正 loopback HTTP 完成会话、消息、创建/删除、操作查询/确认、认证拒绝和关闭。
- 并发 20 次写入均在同一个 owner；预览串行、磁盘失败回滚、不确定提交拒绝后续写入。
- 执行器排队超时、运行中超时、异常传播、重入、停止拒绝、初始化失败无线程遗留。
- 慢模型不占业务线程；关闭超时显式报告，后续停止可完成。
- 独立 Python 子进程启动/关闭临时 HeadlessRuntime，未加载 tkinter/app/ui_theme/
  win_integration；import 无文件、Root、服务器或 Win32 UI 初始化。
- Windows Chat GUI 4主题×2视图 8组，Mock/HTTP/modal_search_undo 通过，errors=[]。
- A3 GUI 8组通过；四主题 Compact/Flow/Timeline、Drag/Resize、编辑器、Undo、
  搜索单实例、Modal 通过；实际192 DPI（200%）。
- Python 138文件 compile、git diff --check 通过。全部使用隔离数据。

没有运行真实模型、正式 EXE、Linux/macOS 真机或跨显示器 DPI 切换测试。
DPAPI 正式运行仍限 Windows；其它平台须显式提供安全 protector，没有生产明文替代。

## B1 推荐切入点

以 runtime facade 和 A2/A3 返回值作为契约，先封装 JSON Repository 的读取快照
和原子 apply，不修改规则/文件格式。重点是收紧 Store 可变对象逃逸与跨进程
冲突边界，不应从此阶段直接开始 SQLite 或远程 CRUD。本轮到 A4 为止。
