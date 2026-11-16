"""Short-lived, owned native migration dialogs; no calendar services yet."""
import tkinter as tk
from tkinter import messagebox, ttk, filedialog


class StartupDialogs:
    def __enter__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        return self

    def confirm(self):
        return messagebox.askyesno('桌面月历 · 存储升级',
            '将升级已有 JSON 日历；空目录将创建新日历。\n\n将先保留并校验原件保护副本，再验证并转换为 SQLite。'
            '\n迁移期间需要独占数据目录；成功后仅使用 SQLite，不再采纳旧版 JSON 的修改。'
            '\n取消则继续使用 JSON。\n\n是否开始？', parent=self.root)

    def notify(self, state):
        if state == 'migrating':
            self.root.title('桌面月历 · 隔离存储迁移')
            ttk.Label(self.root, text='正在保护原件、验证数据并激活 SQLite…\n请勿关闭进程。', padding=20).pack()
            self.root.deiconify()
            self.root.update_idletasks()
        if state == 'activated':
            messagebox.showinfo('桌面月历 · 隔离存储迁移',
                '迁移与完整性验证完成，原 JSON 和保护副本已保留。\n即将使用 SQLite 打开日历。', parent=self.root)

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.root is not None:
            self.root.destroy()
            self.root = None


def offline_recovery(options):
    from calendar_sqlite_backup import read_container
    from calendar_storage_lifecycle import digest
    from calendar_disaster_recovery import disaster_restore
    with StartupDialogs() as dialogs:
        path = filedialog.askopenfilename(parent=dialogs.root, title='离线灾难恢复：选择备份',
            filetypes=(('月历备份', '*.dcalbackup'),))
        if not path:
            return False
        fingerprint = digest(path)
        manifest, _, _ = read_container(path)
        if not messagebox.askyesno('桌面月历 · 离线灾难恢复',
            f"备份时间：{manifest['created_at']}\n将原样归档数据库和 WAL/SHM 后恢复。"
            '\n备份之后的变更可能丢失；旧待执行操作和撤销历史失效。'
            '\n备份可能遗忘已发提醒，继续即接受可能再次提醒。是否恢复？', parent=dialogs.root):
            return False
        disaster_restore(options.manager(), path, authorize_replacement=True,
            reminder_replay_reviewed=True, expected_backup_hash=fingerprint)
        messagebox.showinfo('桌面月历', '恢复验证完成，原件已归档。即将启动恢复后的日历。', parent=dialogs.root)
        return True
