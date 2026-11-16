"""Short-lived, owned native migration dialogs; no calendar services yet."""
import tkinter as tk
from tkinter import messagebox, ttk


class StartupDialogs:
    def __enter__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        return self

    def confirm(self):
        return messagebox.askyesno('桌面月历 · 隔离存储迁移',
            '检测到旧版 JSON 日历。\n\n将先保留并校验原件保护副本，再验证并转换为 SQLite。'
            '\n迁移期间需要独占此隔离目录；成功后仅使用 SQLite。'
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
        self.root.destroy()
