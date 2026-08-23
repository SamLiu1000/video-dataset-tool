"""程序入口：python -m video_crop_tool [视频文件夹]"""
from __future__ import annotations

import os
import sys


def main() -> int:
    # 自包含：无论绿色版还是开发模式，都把内置的 ffmpeg / libmpv 目录加进 PATH，
    # 使 python-mpv 与 subprocess/shutil.which 不依赖系统环境。
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            paths = [os.path.join(meipass, "bin"), meipass]
        else:
            paths = []
    else:
        pkg_dir = os.path.dirname(os.path.abspath(__file__))   # .../video_crop_tool
        proj_dir = os.path.dirname(pkg_dir)                    # 项目根
        paths = [os.path.join(proj_dir, "ffmpeg"), os.path.join(proj_dir, "libmpv")]
    if paths:
        os.environ["PATH"] = os.pathsep.join(paths) + os.pathsep + os.environ.get("PATH", "")

    # 必须先于 QApplication 创建设置高分屏适配
    import tempfile
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from .main_window import MainWindow
    from . import style

    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 10))

    # 勾选/箭头图标需在 QApplication 之后生成（QPixmap 依赖 GUI 实例），
    # QSS 用 url() 引用；路径必须用正斜杠，Windows 反斜杠会被 QSS 当转义
    tmp = tempfile.gettempdir().replace("\\", "/")
    icon_path = os.path.join(tmp, "vct_check.png").replace("\\", "/")
    up_path = os.path.join(tmp, "vct_spin_up.png").replace("\\", "/")
    down_path = os.path.join(tmp, "vct_spin_down.png").replace("\\", "/")
    style._write_check_icon(icon_path)
    style._write_chevron_icon(up_path, up=True)
    style._write_chevron_icon(down_path, up=False)
    app.setStyleSheet(style.build_qss(icon_path, up_path, down_path))

    win = MainWindow()
    win.show()

    # 命令行参数：可附带一个视频文件夹
    if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
        win.add_files_from_dir(os.path.abspath(sys.argv[1]))

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
