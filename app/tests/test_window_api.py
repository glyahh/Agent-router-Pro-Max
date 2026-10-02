"""测试 main.py 中的窗口 API（拖拽、缩放、移动、最小尺寸限制、最大化判断）。"""

import sys
import unittest
import ctypes
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent.parent
APP_DIR = ROOT / 'app'
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import main


class TestWindowApi(unittest.TestCase):
    def setUp(self):
        self.shell = MagicMock()
        self.shell.window = MagicMock()
        self.api = main.make_window_api(self.shell)

    def test_api_methods_exposed(self):
        """确保前端所需的全部接口均已暴露且可调用。"""
        required = [
            'minimize',
            'is_maximized',
            'toggle_maximize',
            'drag',
            'start_resize',
            'drag_start',
            'drag_move',
            'drag_end',
            'resize_start',
            'resize_move',
            'resize_end',
            'move_by',
            'resize_by',
            'close',
        ]
        for name in required:
            self.assertTrue(hasattr(self.api, name), f"缺少接口: {name}")
            self.assertTrue(callable(getattr(self.api, name)), f"接口不可调用: {name}")

    def test_minimize_and_close(self):
        self.api.minimize()
        self.shell.window.minimize.assert_called_once()

        self.shell.request_close.return_value = True
        self.api.close()
        self.shell.destroy_window.assert_called_once()

    def test_toggle_maximize(self):
        with patch.object(self.api, 'is_maximized', return_value=True):
            self.api.toggle_maximize()
            self.shell.window.restore.assert_called_once()

        self.shell.window.reset_mock()
        with patch.object(self.api, 'is_maximized', return_value=False):
            self.api.toggle_maximize()
            self.shell.window.maximize.assert_called_once()

    def test_drag_start_move_end(self):
        """测试物理光标绝对坐标追踪拖拽流程。"""
        with patch.object(main, '_own_hwnd', return_value=12345):
            mock_user32 = MagicMock()
            def fake_get_rect(hwnd, ref):
                ref._obj.left = 200
                ref._obj.top = 100
                ref._obj.right = 1100
                ref._obj.bottom = 720
                return 1
            mock_user32.GetWindowRect.side_effect = fake_get_rect

            cursor_pos = [300, 150]
            def fake_cursor(ref):
                ref._obj.x = cursor_pos[0]
                ref._obj.y = cursor_pos[1]
                return 1
            mock_user32.GetCursorPos.side_effect = fake_cursor

            with patch.object(ctypes.windll, 'user32', mock_user32):
                ok = self.api.drag_start()
                self.assertTrue(ok)
                self.assertTrue(self.api._is_dragging)

                # 光标向右下移动 (50, 40)
                cursor_pos[0] = 350
                cursor_pos[1] = 190
                self.api.drag_move()

                mock_user32.SetWindowPos.assert_called_once()
                args = mock_user32.SetWindowPos.call_args[0]
                self.assertEqual(args[2], 250)  # 200 + 50
                self.assertEqual(args[3], 140)  # 100 + 40

                self.api.drag_end()
                self.assertFalse(self.api._is_dragging)

    def test_resize_start_move_end_with_min_bounds(self):
        """测试八方向物理光标绝对坐标缩放及边界保护。"""
        with patch.object(main, '_own_hwnd', return_value=12345):
            mock_user32 = MagicMock()
            def fake_get_rect(hwnd, ref):
                ref._obj.left = 100
                ref._obj.top = 100
                ref._obj.right = 1000  # width = 900
                ref._obj.bottom = 720  # height = 620
                return 1
            mock_user32.GetWindowRect.side_effect = fake_get_rect

            cursor_pos = [1000, 720]
            def fake_cursor(ref):
                ref._obj.x = cursor_pos[0]
                ref._obj.y = cursor_pos[1]
                return 1
            mock_user32.GetCursorPos.side_effect = fake_cursor

            with patch.object(ctypes.windll, 'user32', mock_user32):
                # 1. 向右下缩放
                ok = self.api.resize_start('bottom-right')
                self.assertTrue(ok)
                self.assertTrue(self.api._is_resizing)

                cursor_pos[0] = 1060  # dx = +60
                cursor_pos[1] = 760   # dy = +40
                self.api.resize_move()

                args = mock_user32.SetWindowPos.call_args[0]
                self.assertEqual(args[2], 100)
                self.assertEqual(args[3], 100)
                self.assertEqual(args[4], 960)  # 900 + 60
                self.assertEqual(args[5], 660)  # 620 + 40
                self.api.resize_end()

                # 2. 向左缩小超出最小限制（min_w=900）
                mock_user32.SetWindowPos.reset_mock()
                cursor_pos[0] = 100
                cursor_pos[1] = 100
                self.api.resize_start('left')
                cursor_pos[0] = 300  # dx = +200（向右压缩窗口）
                self.api.resize_move()

                args = mock_user32.SetWindowPos.call_args[0]
                self.assertEqual(args[2], 100)  # 保持
                self.assertEqual(args[4], 900)  # 保护在 min_w

    def test_move_by(self):
        """测试 move_by 正确调用 Win32 SetWindowPos 计算坐标。"""
        with patch.object(main, '_own_hwnd', return_value=12345):
            mock_user32 = MagicMock()
            def fake_get_rect(hwnd, ref):
                ref._obj.left = 100
                ref._obj.top = 150
                ref._obj.right = 1000
                ref._obj.bottom = 750
                return 1
            mock_user32.GetWindowRect.side_effect = fake_get_rect

            with patch.object(ctypes.windll, 'user32', mock_user32):
                self.api.move_by(25, -15)
                mock_user32.SetWindowPos.assert_called_once()
                args = mock_user32.SetWindowPos.call_args[0]
                self.assertEqual(args[2], 125)  # 100 + 25
                self.assertEqual(args[3], 135)  # 150 - 15

    def test_resize_by_right_bottom(self):
        """测试向右下缩放。"""
        with patch.object(main, '_own_hwnd', return_value=12345):
            mock_user32 = MagicMock()
            def fake_get_rect(hwnd, ref):
                ref._obj.left = 100
                ref._obj.top = 100
                ref._obj.right = 1000  # width = 900
                ref._obj.bottom = 720  # height = 620
                return 1
            mock_user32.GetWindowRect.side_effect = fake_get_rect

            with patch.object(ctypes.windll, 'user32', mock_user32):
                self.api.resize_by('right', 50, 0)
                mock_user32.SetWindowPos.assert_called_once()
                args = mock_user32.SetWindowPos.call_args[0]
                self.assertEqual(args[2], 100)
                self.assertEqual(args[3], 100)
                self.assertEqual(args[4], 950)
                self.assertEqual(args[5], 620)

    def test_resize_by_left_top_with_min_bounds(self):
        """测试向左上缩放且不能小于最小宽度和高度。"""
        with patch.object(main, '_own_hwnd', return_value=12345):
            mock_user32 = MagicMock()
            def fake_get_rect(hwnd, ref):
                ref._obj.left = 100
                ref._obj.top = 100
                ref._obj.right = 1000  # width = 900
                ref._obj.bottom = 720  # height = 620
                return 1
            mock_user32.GetWindowRect.side_effect = fake_get_rect

            with patch.object(ctypes.windll, 'user32', mock_user32):
                # 试图缩小 200px，但 min_w 是 900，所以宽度应保持 900，left 保持 100
                self.api.resize_by('left', 200, 0)
                mock_user32.SetWindowPos.assert_called_once()
                args = mock_user32.SetWindowPos.call_args[0]
                self.assertEqual(args[2], 100)
                self.assertEqual(args[4], 900)

    def test_drag_and_start_resize_do_not_crash(self):
        """测试在无真实句柄或抛异常时不会崩溃。"""
        with patch.object(main, '_own_hwnd', return_value=None):
            self.api.drag()
            self.api.start_resize(2)
            self.api.drag_start()
            self.api.drag_move()
            self.api.drag_end()
            self.api.resize_start('top')
            self.api.resize_move()
            self.api.resize_end()
            self.api.move_by(10, 10)
            self.api.resize_by('right', 10, 10)


if __name__ == '__main__':
    unittest.main()
