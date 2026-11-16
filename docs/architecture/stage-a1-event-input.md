# Stage A1：共享 Event 输入构建

基于架构文档提交 `704d1f6`，工作分支 `refactor/event-input-validation`。仅重构输入，不实施 A2、SQLite 或新 API。

## 接口与边界

`event_input.py` 提供：

- `parse_event_due(date_text, time_text)`：GUI 文本解析，保留原 strptime/空白处理。
- `build_event_candidate(values, current=None, identifier=None, context=None, creating=None)`：新建或局部 Patch 的脱离原对象候选。
- `EventInputError(field, message, clarification=False)`：业务错误，GUI显示现有对话框，AI映射现有ChatError。

values 使用已有 Chat 字段名 date/time，不增加 HTTP 字段；只允许明确业务输入。current=None创建使用现有 Event 默认属性、无时间23:59占位和无分钟提醒；GUI仍显式提交控件选择的默认值。current存在时深复制，缺字段不覆盖；null仅允许time/category_id/reminder。ID、created_at、snooze_until不开放为Patch字段。

context仅使用分类查询、duration_for_range与event_dates，不保存、不刷新、不记录历史。分类/日期业务上下文仍由Store提供；不是已经完成Repository抽象。calendar_core的quick factory以局部import调用，避免模块加载循环；不把文件读取归一化改成严格新输入校验。

## 入口复用

EventEditor.save仍读取控件，保留标题/日期/持续长度的原错误展示和输入解析，候选构建改走共享函数。parse_event_due旧导出保留。Store.make_quick_event复用构建器。Gateway._patch仅去掉category_basis等AI元数据、保留既有创建容错，再调用共享函数并映射错误；LLM语义、Tool Schema、HTTP、确认和执行政策不变。

全部提交仍走EventCommandHistory，未改preview/commit、批量、Undo/Redo、持久化与回滚。Habit没有改动。

## 有意保留的兼容差异

- 未来API草案提议inherit与color并传拒绝；现有GUI在inherit时也提交色块，故A1保留“分类颜色优先”，不采用草案新限制。
- 无分类显式inherit在共享验证器拒绝；AI新建时模型误补inherit的既有容错留在AI适配器，修改仍要求澄清。
- AI Schema保留标题300/备注2000限制，GUI以前没有相同长度限制，本轮不擅自给GUI加新限制；共享层检查文本类型/标题非空。
- 无时间使用23:59+has_time=False，不改磁盘表示；日期-only Patch保留旧时间秒数和offset，time=null才清时间与分钟提醒。
- end_date沿用现有有效工作日计数与最终有效日校验；持续事项和DDL含义不变。

## 验证

正常权限基线915/915通过（29.823秒），修改后完整927/927通过（25.220秒）。专项12项包含实际EventEditor.save回调与Gateway候选对照、三态Patch、其它字段逐项保留、颜色策略、无时间DDL、官方节假日/调休/用户请假上下文、预览零写入、命令Undo/Redo。

沙箱内Chat专项135项出现9个errors，观察到loopback权限与备份硬链接权限错误；正常权限完整套件全部通过，未为环境限制修改备份/HTTP逻辑。基线历史915项的42个沙箱错误未逐条保留完整分类日志，不能声称每个都证明为同一原因。

Windows Chat GUI smoke：四主题×Compact/Global共8组，实际192DPI，Mock LLM、本机HTTP、Modal/Search/Undo通过。自动GUI不等于人工逐像素外观验收；本轮没有真实DeepSeek、正式EXE构建或多显示器DPI切换验收。

`tools/check_interactions.py`真实Tk检查亦通过：四主题Compact/Flow/Timeline、move/resize/cancel、原编辑器保存、Undo、搜索单实例/Modal、筛选后临时定位均正常，实际192DPI。

后续A2可围绕现有EventCommandHistory增加薄Application入口及类型化结果；不要以本次抽取为理由立即更换Store/事务或扩大Habit改造。
