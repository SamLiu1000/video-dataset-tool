# -*- mode: python ; coding: utf-8 -*-
"""video-dataset-tool 绿色便携版 PyInstaller spec。

onedir 布局（PyInstaller 6.x）：
  dist/VideoCropTool/VideoCropTool.exe
  dist/VideoCropTool/_internal/          <- sys._MEIPASS
    libmpv-2.dll                          （add-binary 到 _MEIPASS 根）
    bin/ffmpeg.exe, bin/ffprobe.exe       （add-binary 到 _MEIPASS/bin）
    video_crop_tool/assets/*.svg          （add-data 到 _MEIPASS/video_crop_tool/assets）

代码侧配套（见 main.py / mpv_player.py / main_window.py）：
  - main() 冻结后把 _MEIPASS 与 _MEIPASS/bin 加入 PATH；
  - _ensure_libmpv 也会把 _MEIPASS 作为候选目录；
  - assets 路径冻结后用 sys._MEIPASS/video_crop_tool/assets。
"""
import os
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = os.path.abspath(SPECPATH) if 'SPECPATH' in globals() else os.path.abspath(os.getcwd())

# 打包进 _MEIPASS 的第三方二进制/数据
# collect_all('av') -> (datas, binaries, hiddenimports)：av 是带 C 扩展的包，需一并收集
av_datas, av_bins, av_hidden = collect_all('av')

a = Analysis(
    ['app_entry.py'],
    pathex=[ROOT],
    binaries=av_bins + [
        (os.path.join(ROOT, 'libmpv', 'libmpv-2.dll'), '.'),
        (os.path.join(ROOT, 'ffmpeg', 'ffmpeg.exe'), 'bin'),
        (os.path.join(ROOT, 'ffmpeg', 'ffprobe.exe'), 'bin'),
    ],
    datas=av_datas + collect_data_files('PIL') + [
        (os.path.join(ROOT, 'video_crop_tool', 'assets'), 'video_crop_tool/assets'),
    ],
    hiddenimports=av_hidden + collect_submodules('PIL') + ['mpv'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['colour', 'colour_science', 'matplotlib', 'scipy', 'tkinter'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VideoCropTool',
    icon=os.path.join(ROOT, 'video_crop_tool', 'assets', 'app.ico'),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 绿色版：无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='VideoCropTool',
)
