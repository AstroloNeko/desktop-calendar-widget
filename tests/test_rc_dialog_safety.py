"""No GUI input: regression for the RC file-picker retry incident."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock


@unittest.skipUnless(os.name=='nt','Windows QA driver')
class RcDialogSafetyTests(unittest.TestCase):
    def test_shell_picker_and_missing_file_are_not_automated(self):
        from tools.check_sqlite_rc_exe import dialog_action
        for title,texts in [('离线灾难恢复：选择备份',[]),('桌面月历',['找不到文件'])]:
            with self.assertRaisesRegex(RuntimeError,'manual'):
                dialog_action(title,texts)

    def test_unexpected_dialog_never_defaults_to_ok(self):
        from tools.check_sqlite_rc_exe import dialog_action
        with self.assertRaises(RuntimeError):dialog_action('Other',['Error'])

    def test_busy_confirmation_gets_one_action_only(self):
        from tools import check_sqlite_rc_exe as qa
        title='桌面月历 · 存储升级'
        process=SimpleNamespace(pid=999)
        def descendants(hwnd,callback,param):
            callback(321,0)
        def text(hwnd,buffer,size):buffer.value='是否开始？'
        with patch.object(qa,'windows',return_value=[(123,title,'#32770')]), \
                patch.object(qa.U,'IsWindowVisible',return_value=True), \
                patch.object(qa.U,'EnumChildWindows',side_effect=descendants), \
                patch.object(qa.U,'GetWindowTextW',side_effect=text), \
                patch.object(qa.U,'PostMessageW',return_value=True) as post:
            for _ in range(50):qa.pump_dialogs(process)
            post.assert_called_once_with(123,0x111,6,0)
