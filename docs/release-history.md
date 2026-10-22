# 版本与更新日志 / Repair 说明

- 主程序入口：更多 → 版本与更新日志。默认显示已安装版本，可切换随包历史，完全离线。
- 本地来源为 PyInstaller `_MEIPASS/CHANGELOG.md`；源码运行读取项目同名文件。缺失、无法读取或没有当前版本时显示明确暂无记录，不显示其他版本冒充当前版本。
- 自动更新与 Repair 使用目标 GitHub Release 的原始 body，不拼接本地历史，不截取长文本。
- 三个入口共用 `release_notes.py` 的惰性文本转换及 `NotesDocument` 的 Text/Tag 视图。支持标题、列表、段落、行内代码及加粗去标记；不是完整 Markdown/HTML 浏览器，不执行脚本或外链。
- Repair 无需导入主程序或用户日历数据。先预览并确认，再沿用既有 SHA256 下载和安装流程；取消不下载。
- 新包附带程序文件 `app-version.txt` 供 Repair 识别安装版本。旧包缺少元数据时明确显示未知；不会把修复工具版本冒充已安装版本，也不会启动损坏 EXE 来查询。
- 保留修复工具与已知安装版本的降级保护；远端比当前修复工具旧时仍安全停止。网络失败不伪造日志或开始安装。
- 更新日志窗口复用主程序 modal 生命周期；Repair 的子窗口独立持有并释放自己的 grab。
- 数据 Schema 保持 8，备份格式保持 1。Schema 7/8 与更新交接修复见 `update-schema-handoff.md`。

## 验证

`tools/check_release_history.py` 使用隔离 Store、模拟 Release Notes 与独立 Repair Tk 窗口，验证四主题、Compact/Global、置顶恢复、输入/Ctrl+K、滚动末段、版本切换、实际菜单和 96–192 DPI 布局。

脚本报告真实显示器 DPI；其他 DPI 是布局模拟，不代表具有五台物理显示器。Tk 控件事件不是物理鼠标输入验收。正式包另行校验版本、资源、独立 Repair 依赖及 EXE 启动。
