# Windows 托盘与应用生命周期（开发中 Phase 1A）

## 行为

- 托盘菜单：显示桌面月历、打开全局视图、隐藏到托盘、退出桌面月历。
- 双击图标恢复隐藏前的 Compact / Global；“打开全局视图”才明确切换视图。
- 更多菜单新增“隐藏到系统托盘”。托盘不可用时禁用；关闭入口再次尝试初始化失败时保留窗口。
- 沿用原有 Compact 横线 / 窗口关闭隐藏行为；Global 横线仍最小化到任务栏。`LifecycleState.close_hides` 是未来关闭策略的会话接口，本轮不新增配置或改变默认习惯。
- 隐藏/恢复不保存日历、不重建界面、不清空命令历史；退出使用最后可见的几何快照，避免用隐藏窗口尺寸覆盖设置。
- 编辑器、分类管理、更新及原生数据对话框打开时，先完成/关闭当前操作再隐藏或退出，避免丢失未保存输入。搜索可由退出流程正常关闭。

## 结构

- `tray_icon.py`：原生 Shell_NotifyIconW、一个隐藏 Win32 helper HWND、一个阻塞 GetMessage 消息线程，复用 ICO，无新依赖、无 Tk Root、无通知业务。
- 所有 Win32 句柄和窗口参数显式使用 pointer-wide ctypes 原型；私有 DLL 缓存不改写现有窗口模块签名。
- `app_lifecycle.py`：Tk 主线程消费命令队列，处理隐藏、恢复、失败回退及退出。普通恢复不使用永久 TOPMOST；最终恢复用户保存的窗口层级。
- 主队列每 250ms 有界消费，合并连续重复命令；隐藏时每 2 秒只读检查一次 Shell 图标。托盘线程不调用任何 Tk 方法；更新工作线程也通过相同队列投递 UI 结果。
- TaskbarCreated 重新注册同一 helper HWND / icon ID；创建失败、消息线程死亡或静默图标丢失时保留/恢复窗口。
- 退出先完成正常保存，更新交接不重复保存；保存失败保持运行。随后停止搜索/托盘、取消本应用 Tcl 回调、丢弃晚到命令、释放实例守卫并销毁根窗口。

## 验证命令及边界

```powershell
python -m unittest discover -s tests -q
python tools/check_system_tray.py
python tools/check_system_tray.py --exe build/v0100-tray-qa/dist/DesktopCalendar/DesktopCalendar.exe
```

测试使用隔离数据目录。Shell 图标通过真实 Shell_NotifyIconGetRect 验证存在/移除；菜单命令与双击使用投递到真实 helper HWND 的原生回调消息，不冒充物理鼠标点击。

Explorer 测试是删除本应用的图标并发送 TaskbarCreated 的协议回归，不强行结束用户 Explorer。完整 Explorer 进程重启、通知区域物理鼠标操作及多块真实 DPI 显示器验收仍需人工补充。现有 Windows 桌面原生为 192 DPI；其他 DPI 测试只模拟布局，不修改系统显示设置。

QA EXE 使用正常 app.py、原有资源和 PerMonitorV2 manifest 构建到忽略的 build 子目录。它不是新 Release，版本号保持 v0.9.3，不替换正式发布附件。
