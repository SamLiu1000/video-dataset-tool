"""主窗口：工具栏、预览/时间轴/侧栏布局、右侧文件列表、播放控制、批量导出队列。"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QEvent, QRectF, QSize, QTime, Qt, QThread, QTimer, Signal, QUrl
from PySide6.QtGui import QBrush, QColor, QDoubleValidator, QDragEnterEvent, QDropEvent, QDesktopServices, QFont, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QSplitter,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTimeEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import style
from .core import (
    VIDEO_EXTS,
    CropJob,
    FFmpegWorker,
    RemuxWorker,
    detect_nvenc,
    ensure_even,
    estimate_size_bytes,
    ffmpeg_available,
    fmt_time,
    human_size,
    sanitize_name,
)
from .mpv_player import MpvWorker, ffmpeg_frame
from . import settings
from .widgets import LeftPanel, PreviewWidget, SpeedWidget, TimelineWidget
from . import i18n
from .i18n import tr

# ---------------------------------------------------------------------------
# 运行时日志：写本地 video_crop_tool.log（与程序同目录），便于排查问题
# ---------------------------------------------------------------------------
_LOG_PATH = (os.path.join(os.path.dirname(sys.executable), "video_crop_tool.log")
             if getattr(sys, "frozen", False)
             else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "video_crop_tool.log"))
logging.basicConfig(
    filename=_LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("vct")


@dataclass
class _VideoMeta:
    """当前视频元数据（由 MpvWorker.loaded 回传，属性访问兼容旧 reader 用法）。"""
    path: str = ""
    width: int = 0
    height: int = 0
    fps: float = 30.0
    duration: float = 0.0
    frame_count: int = 0
    has_audio: bool = False
    video_bitrate: int = 0
    dar: float = 0.0     # 显示宽高比（含 PAR），供裁切/导出还原变形源
    sar: float = 1.0     # 像素宽高比(PAR)；变形源 !=1.0，导出时还原到显示宽高比


# 列表项数据角色：标记"新导入"项，由 _NameWrapDelegate 绘制高亮（QSS 会忽略 setBackground）
NEW_ITEM_ROLE = Qt.UserRole + 1


class MainWindow(QWidget):
    # 入点/出点小容器预览：后台 ffmpeg 抽帧（低清），seq 丢弃过期结果
    range_in_ready = Signal(int, object)    # seq, QImage（入点帧）
    range_out_ready = Signal(int, object)   # seq, QImage（出点帧）

    def __init__(self) -> None:
        super().__init__()
        # 语言：启动即从设置读取并设好（切换语言后重启生效，重启时重读）
        i18n.set_lang(str(settings.get("lang", "zh")))
        self.setObjectName("mainWindow")
        self.setWindowTitle(tr("视频裁切工具 — AI 训练数据准备"))
        self.setAcceptDrops(True)
        self.resize(1440, 900)

        self.reader: _VideoMeta | None = None
        self.current_path: str = ""
        self.files: list[str] = []
        self.file_jobs: dict[str, dict] = {}   # path -> 该文件的裁切参数（跨文件保留）
        self.file_icons: dict[str, QIcon] = {}
        self._file_items: dict[str, QListWidgetItem] = {}
        self.worker: FFmpegWorker | None = None
        self._worker_queue: list[CropJob] = []
        self._remux: RemuxWorker | None = None   # 补 Cues 的后台线程

        self._frame_idx = 0
        self._playing = False
        self._frac = 0.0
        self._last_tick_time = 0.0
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(33)
        # 精确定时器：Windows 默认粗定时器（CoarseTimer）实际约 47ms 才触发一次，
        # 会把 30fps 播放拖成 ~21fps 的慢放
        self._play_timer.setTimerType(Qt.PreciseTimer)
        self._play_timer.timeout.connect(self._on_tick)
        # 长按 A/D 持续步进（等同慢速播放）：按下步进一帧，长按 300ms 后才开始连续步进
        self._step_timer = QTimer(self)
        self._step_timer.setInterval(70)
        self._step_timer.timeout.connect(self._step_tick)
        self._step_hold_timer = QTimer(self)
        self._step_hold_timer.setSingleShot(True)
        self._step_hold_timer.setInterval(300)
        self._step_hold_timer.timeout.connect(self._step_hold_expired)
        self._step_dir = 1   # 长按方向（+1/-1），供 _step_tick 使用
        # 应用级按键拦截：A/D 在任何焦点下生效（输入控件除外）。
        # 不能只挂在 MainWindow：QListWidget 的 type-ahead 会把 A/D 当字母跳转吞掉、
        # 事件到不了窗口级 filter —— 应用级 filter 在事件到任何控件前先拦截。
        QApplication.instance().installEventFilter(self)

        # 预览音频由 mpv 原生播放（音画同步 + 音量 af=volume 增益 + aid 音轨切换）。
        # 音频相关状态（导出增益/预览增益/音轨）在此初始化。
        self._audio_gain = 1.0          # 导出音频增益（1.0=100%，随「导出音频增益」控件 0~600% 变动）
        self._preview_gain = 1.0        # 预览音量增益（1.0=100%，随音量滑块 0~600% 变动，af=volume）
        self._audio_track_ordinal = -1  # 当前选择的音轨（0 起音频流）；-1=默认
        self._audio_tracks: list[dict] = []  # 当前源视频的音频流元数据（ffprobe）
        # 入点/出点小容器预览：后台 ffmpeg 抽帧（低清），seq 丢弃过期结果
        self._range_seq = 0
        self._range_req = None      # (path, in_sec, out_sec, seq)
        self._range_busy = False
        self.range_in_ready.connect(self._set_in_preview)
        self.range_out_ready.connect(self._set_out_preview)

        # 文件名模板：每个源视频的片段序号（# 数字 / % 字母 共用）
        self._seg_counters: dict[str, int] = {}
        self._name_tpl_custom = False

        self._icon_queue: queue.Queue = queue.Queue()
        self._icon_thread = _IconWorker(self._icon_queue, self)
        self._icon_thread.icon_ready.connect(self._on_icon)
        self._icon_thread.setPriority(QThread.LowPriority)
        self._icon_thread.start()

        # 拖动裁剪框/时间轴时防抖刷新裁切信息，避免高频布局重排
        self._info_timer = QTimer(self)
        self._info_timer.setSingleShot(True)
        self._info_timer.setInterval(100)
        self._info_timer.timeout.connect(self._refresh_info)

        # 拖动时间轴：seek 异步发出（mpv 合并），画面由 33ms 节拍截图驱动
        self._seek_timer = QTimer(self)
        self._seek_timer.setSingleShot(True)
        self._seek_timer.setInterval(150)
        self._seek_timer.timeout.connect(self._seek_finalize)
        self._seek_target_sec = 0.0
        # 拖动节拍：拖动中按"预览帧精度"粒度用 ffmpeg 抽帧显示。
        # 不用 mpv screenshot：vo=gpu 暂停态下 VO 渲染落点帧是异步的，
        # 33ms 节拍截图几乎全截到旧帧（画面几秒才变一次）。
        self._scrub_timer = QTimer(self)
        self._scrub_timer.setInterval(33)
        self._scrub_timer.setTimerType(Qt.PreciseTimer)
        self._scrub_timer.timeout.connect(self._scrub_tick)

        # mpv 渲染后台线程（单实例）：mpv 直接渲染到 PreviewWidget 的视频层
        # （可见窗口，播放/拖动/步进走 mpv 原生渲染管线，零截图零搬运）。
        # 渲染目标（preview._video_layer）在 _build_ui 里创建后，再赋给 worker。
        self._mpv_queue: queue.Queue = queue.Queue()
        self._mpv_thread = MpvWorker(self._mpv_queue, self, host=None)
        self._mpv_thread.loaded.connect(self._on_mpv_loaded)
        self._mpv_thread.load_failed.connect(self._on_mpv_load_failed)
        self._mpv_thread.frame_ready.connect(self._on_mpv_frame)
        self._mpv_thread.setPriority(QThread.LowPriority)
        self._mpv_thread.start()
        self._meta: dict = {}          # 当前视频元数据（来自 MpvWorker.loaded）
        self._settle_seq = 0           # 请求序号：新动作递增，旧结果据此作废
        self._scrub_seq = 0            # 拖动/取帧请求序号：UI 只接受最新序号结果

        self._output_dir = ""   # 保存位置（导出目录），会持久化到 JSON 设置
        self._view_mode = "large"
        self._view_labels = {"name": tr("名称"), "large": tr("大图")}
        # 视图切换按钮/菜单图标：直接加载 assets 下两张 SVG（代码加载，不手改 SVG）。
        # 绿色版（PyInstaller 冻结后）资源会被打进 _MEIPASS，路径要跟着切换。
        if getattr(sys, "frozen", False):
            _asset_dir = os.path.join(sys._MEIPASS, "video_crop_tool", "assets")
        else:
            _asset_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
        self._view_icon_large = QIcon(os.path.join(_asset_dir, "icon_large.svg"))
        self._view_icon_name = QIcon(os.path.join(_asset_dir, "icon_name.svg"))
        # 窗口标题栏/任务栏图标（绿色版资源也在 _MEIPASS 下，随 assets 一起打包）
        self.setWindowIcon(QIcon(os.path.join(_asset_dir, "app.ico")))

        self._build_ui()
        # 从设置恢复上次的保存位置并显示在顶部
        try:
            self._output_dir = str(settings.get(self._OUTDIR_KEY, "") or "")
        except Exception:
            self._output_dir = ""
        self._update_save_path_label()
        self._restore_panel_state()
        self._set_view_mode(self._view_mode)   # 同步视图按钮图标与菜单勾选

        # 硬件加速：检测到 NVENC 则默认开启，否则禁用提示
        if detect_nvenc():
            self.panel.hw_chk.setChecked(True)
        else:
            self.panel.hw_chk.setEnabled(False)
            self.panel.hw_chk.setToolTip(tr("未检测到 NVENC 硬件编码器，将使用 CPU 编码"))

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        # 顶部工具栏（单独一栏）：独立卡片背景，按钮无边框（去除按钮间的竖条分割线）
        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        toolbar.setFixedHeight(40)
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(8, 0, 8, 0)
        bar.setSpacing(4)
        self.import_btn = self._btn(tr("导入文件夹"))
        self.import_files_btn = self._btn(tr("导入文件"))
        self.import_files_btn.setToolTip(tr("添加一个或多个视频文件"))
        self.outdir_btn = self._btn(tr("设置保存位置"))
        self.fix_cues_btn = self._btn(tr("修复索引"))
        self.fix_cues_btn.setToolTip(
            tr("修复视频缺失的 Cues 索引（补 Cues）：选源文件与输出位置，"
               "生成带完整索引的新文件，之后打开即可全片快速定位"))
        self.prev_btn = self._btn(tr("◀ 上一段"))
        self.next_btn = self._btn(tr("下一段 ▶"))
        self.export_btn = QPushButton(tr("✂ 导出"))
        self.export_btn.setObjectName("primaryBtn")
        self.export_btn.setToolTip(tr("按右侧“导出选项”的当前模式导出（视频/图片/音频）"))
        for b in (self.import_btn, self.import_files_btn, self.outdir_btn, self.fix_cues_btn,
                  self.prev_btn, self.next_btn):
            b.setFixedHeight(30)
            b.setObjectName("toolBtn")   # 普通工具栏按钮：扁平无边框
        bar.addWidget(self.import_btn)
        bar.addWidget(self.import_files_btn)
        bar.addWidget(self.outdir_btn)
        bar.addWidget(self.fix_cues_btn)
        bar.addWidget(self.prev_btn)
        bar.addWidget(self.next_btn)
        bar.addStretch(1)
        # 当前保存位置：并入顶部工具栏（导出按钮左侧），不再单独占一行
        self.save_path_label = QLabel()
        self.save_path_label.setObjectName("savePathLabel")
        self.save_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.save_path_label.setToolTip("")
        self.save_path_label.setMaximumWidth(360)
        self.open_dir_btn = QPushButton(tr("打开目录"))
        self.open_dir_btn.setObjectName("toolBtn")
        self.open_dir_btn.setCursor(Qt.PointingHandCursor)
        self.open_dir_btn.setToolTip(tr("在系统文件管理器中打开当前保存位置"))
        self.open_dir_btn.setFixedHeight(30)
        self.open_dir_btn.clicked.connect(self.open_output_dir)
        bar.addWidget(self.save_path_label)
        bar.addWidget(self.open_dir_btn)
        bar.addWidget(self.export_btn)
        # 中英切换按钮：点击保存语言并重启生效（文案显示目标语言）
        self.lang_btn = self._btn(self._lang_label())
        self.lang_btn.setObjectName("toolBtn")
        self.lang_btn.setFixedHeight(30)
        self.lang_btn.setToolTip(tr("切换界面语言（中 / EN）"))
        self.lang_btn.clicked.connect(self._toggle_lang)
        bar.addWidget(self.lang_btn)
        root.addWidget(toolbar)

        # 中部：预览+时间轴 | 文件列表 | 参数设置（参数面板移到了最右）
        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(6)

        self.panel = LeftPanel()   # 参数设置面板：在 splitter 末尾 addWidget 落到最右侧

        center = QWidget()
        center.setObjectName("centerPanel")
        c_lay = QVBoxLayout(center)
        c_lay.setContentsMargins(10, 10, 10, 10)
        c_lay.setSpacing(8)

        # 预览区工具条（参考线按钮 + 分辨率）
        pbar = QHBoxLayout()
        self.v_guide_btn = self._btn(tr("│ 竖参考线"))
        self.h_guide_btn = self._btn(tr("─ 横参考线"))
        self.clear_guide_btn = self._btn(tr("清除参考线"))
        self.src_label = QLabel(tr("未载入"))
        self.src_label.setObjectName("mono")
        pbar.addWidget(self.v_guide_btn)
        pbar.addWidget(self.h_guide_btn)
        pbar.addWidget(self.clear_guide_btn)
        # 预览缩放重置按钮（滚轮放大预览后可一键还原）
        self.preview_reset_zoom_btn = self._btn(tr("重置缩放"))
        self.preview_reset_zoom_btn.setToolTip(tr("重置预览画面缩放"))
        pbar.addWidget(self.preview_reset_zoom_btn)
        pbar.addStretch(1)
        pbar.addWidget(self.src_label)
        c_lay.addLayout(pbar)

        self.preview = PreviewWidget()
        c_lay.addWidget(self.preview, 1)
        # mpv 渲染宿主 = 预览的 _VideoWidget（--wid 原生窗口容器）。worker 只
        # 用它做平台判定（offscreen 回退 vo=null）；wid 句柄在 open_file 里
        # 取 native_handle() 传入加载任务。
        self._mpv_thread._host = self.preview._video_layer

        # 播放控制行
        trow = QHBoxLayout()
        self.prev_frame_btn = self._btn("")
        self.prev_frame_btn.setIcon(style.step_icon("prev"))
        self.prev_frame_btn.setIconSize(QSize(16, 16))
        self.prev_frame_btn.setToolTip(tr("上一步 (←/A)"))
        # 步进秒数输入框：留空=1帧(最小步进)；填数字(可含小数)=按秒跳转
        self.step_input = QLineEdit()
        self.step_input.setFixedWidth(80)
        self.step_input.setValidator(QDoubleValidator(0.0, 99999.0, 5, self.step_input))
        self.step_input.setToolTip(tr("步进秒数：留空=最小步进1帧；填数字(可含小数)=前进/后退按秒跳转"))
        self.next_frame_btn = self._btn("")
        self.next_frame_btn.setIcon(style.step_icon("next"))
        self.next_frame_btn.setIconSize(QSize(16, 16))
        self.next_frame_btn.setToolTip(tr("下一步 (→/D)"))
        self.play_btn = self._btn(tr("▶ 播放"))
        self.play_btn.setCheckable(True)
        self.play_btn.setToolTip(tr("播放/暂停 (空格)"))
        self.frame_label = QLabel(tr("00:00:00.00 · 帧 0"))
        self.frame_label.setObjectName("mono")
        self.speed = SpeedWidget()
        # 音量：名字 + 喇叭图标 + 滑块 + 数值（拖动实时生效，最大 600%）
        self.volume_name = QLabel(tr("音量"))
        self.volume_name.setObjectName("fieldLbl")
        self.volume_icon = QLabel("🔊")
        self.volume_icon.setToolTip(tr("音量"))
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 600)
        self.volume_slider.setValue(100)
        self.volume_slider.setFixedWidth(90)
        self.volume_slider.setToolTip(tr("预览音量（100%=原音量，最大 600%）"))
        self.volume_slider.valueChanged.connect(self._on_volume)
        self.volume_value = QLabel("100%")
        self.volume_value.setObjectName("mono")
        self.volume_value.setFixedWidth(46)
        self.volume_value.setToolTip(tr("预览音量（100%=原音量，最大 600%）"))
        trow.addWidget(self.prev_frame_btn)
        trow.addWidget(self.step_input)
        trow.addWidget(self.next_frame_btn)
        trow.addWidget(self.play_btn)
        # 定位选区：缩放窗口到裁切选区（放在重置缩放上方）
        self.timeline_fit_btn = self._btn(tr("定位选区"))
        self.timeline_fit_btn.setToolTip(tr("缩放时间轴窗口到当前裁切选区"))
        trow.addWidget(self.timeline_fit_btn)
        # 时间轴缩放重置按钮（滚轮放大时间轴后可一键还原）
        self.timeline_reset_zoom_btn = self._btn(tr("重置缩放"))
        self.timeline_reset_zoom_btn.setToolTip(tr("重置时间轴缩放"))
        trow.addWidget(self.timeline_reset_zoom_btn)
        trow.addWidget(self.frame_label)
        # 画面→入点/出点：把当前播放头帧设为入/出点，免去拖裁切条手柄（紧凑小按钮）
        self.to_in_btn = self._btn(tr("到入点"))
        self.to_in_btn.setFont(QFont("Microsoft YaHei UI", 8))
        self.to_in_btn.setToolTip(tr("把当前帧设为入点"))
        self.to_out_btn = self._btn(tr("到出点"))
        self.to_out_btn.setFont(QFont("Microsoft YaHei UI", 8))
        self.to_out_btn.setToolTip(tr("把当前帧设为出点"))
        trow.addWidget(self.to_in_btn)
        trow.addWidget(self.to_out_btn)
        trow.addStretch(1)
        trow.addWidget(self.volume_name)
        trow.addWidget(self.volume_icon)
        trow.addWidget(self.volume_slider)
        trow.addWidget(self.volume_value)
        trow.addSpacing(16)   # 音量与速度之间留空隙，避免误认
        trow.addWidget(self.speed)   # 「速度」名字 + 滑块 + 数值
        # 播放控制行包一层容器：QSS #toolRow 让该行所有控件字号缩小 50%
        tool_row_host = QWidget()
        tool_row_host.setObjectName("toolRow")
        tool_row_host.setLayout(trow)
        c_lay.addWidget(tool_row_host)

        self.timeline = TimelineWidget()
        c_lay.addWidget(self.timeline)

        # 入点/出点小容器：分别显示选区首帧/尾帧（低清，后台 ffmpeg 抽帧）
        tinfo = QHBoxLayout()
        tinfo.setSpacing(6)
        self.in_preview = QLabel()
        self.in_preview.setFixedSize(96, 54)
        self.in_preview.setAlignment(Qt.AlignCenter)
        self.in_preview.setObjectName("posPreview")
        self.in_preview.setToolTip(tr("入点画面（选区首帧）"))
        self.in_preview.setText("—")
        tinfo.addWidget(self.in_preview)
        in_lbl = QLabel(tr("入点"))
        in_lbl.setObjectName("fieldLbl")
        self.in_edit = QTimeEdit()
        self.in_edit.setDisplayFormat("HH:mm:ss.zzz")
        self.in_edit.setMinimumWidth(80)
        self.in_edit.setToolTip(tr("手动输入入点（时分秒），点 ✓ 生效"))
        self.in_apply = QPushButton("✓")
        self.in_apply.setFixedWidth(38)
        self.in_apply.setToolTip(tr("应用入点"))
        out_lbl = QLabel(tr("出点"))
        out_lbl.setObjectName("fieldLbl")
        self.out_preview = QLabel()
        self.out_preview.setFixedSize(96, 54)
        self.out_preview.setAlignment(Qt.AlignCenter)
        self.out_preview.setObjectName("posPreview")
        self.out_preview.setToolTip(tr("出点画面（选区尾帧）"))
        self.out_preview.setText("—")
        self.out_edit = QTimeEdit()
        self.out_edit.setDisplayFormat("HH:mm:ss.zzz")
        self.out_edit.setMinimumWidth(80)
        self.out_edit.setToolTip(tr("手动输入出点（时分秒），点 ✓ 生效"))
        self.out_apply = QPushButton("✓")
        self.out_apply.setFixedWidth(38)
        self.out_apply.setToolTip(tr("应用出点"))
        self.dur_label = QLabel(tr("时长 --"))
        self.dur_label.setObjectName("mono")
        for lbl in (in_lbl, out_lbl):
            lbl.setObjectName("fieldLbl")
        tinfo.addWidget(in_lbl)
        tinfo.addWidget(self.in_edit)
        tinfo.addWidget(self.in_apply)
        tinfo.addStretch(1)
        tinfo.addWidget(self.dur_label)
        tinfo.addStretch(1)
        tinfo.addWidget(self.out_preview)
        tinfo.addWidget(out_lbl)
        tinfo.addWidget(self.out_edit)
        tinfo.addWidget(self.out_apply)
        c_lay.addLayout(tinfo)

        # 文件名模板栏（预览条下方的矩形功能条）
        nbar = QHBoxLayout()
        nbar.setSpacing(6)
        n_lbl = QLabel(tr("文件名"))
        n_lbl.setObjectName("fieldLbl")
        self.name_tpl = QLineEdit()
        self.name_tpl.setPlaceholderText(tr("导出文件名模板（自动填充当前视频名）"))
        self.name_tpl.setToolTip(tr("#=序号(1,2,3…)  $=日期(精确到分钟)  %=字母(a,b,…,z,aa…)；不含#/%时为 模板名_clip序号"))
        self.hash_btn = self._btn("#")
        self.hash_btn.setToolTip(tr("插入序号 #：同一视频第 1、2、3… 段"))
        self.hash_btn.setFixedWidth(34)
        self.dollar_btn = self._btn("$")
        self.dollar_btn.setToolTip(tr("插入日期 $：精确到分钟"))
        self.dollar_btn.setFixedWidth(34)
        self.pct_btn = self._btn("%")
        self.pct_btn.setToolTip(tr("插入字母 %：a,b,…,z,aa,ab…（逻辑同 #）"))
        self.pct_btn.setFixedWidth(34)
        nbar.addWidget(n_lbl)
        nbar.addWidget(self.name_tpl, 1)
        nbar.addWidget(self.hash_btn)
        nbar.addWidget(self.dollar_btn)
        nbar.addWidget(self.pct_btn)
        c_lay.addLayout(nbar)

        split.addWidget(center)

        # 右侧文件列表：固定宽度，与其他栏高度对齐，支持 名称/大图 视图
        right = QWidget()
        right.setObjectName("rightPanel")
        right.setFixedWidth(232)
        r_lay = QVBoxLayout(right)
        r_lay.setContentsMargins(10, 10, 10, 10)
        r_lay.setSpacing(6)
        r_head = QHBoxLayout()
        r_head.setSpacing(6)
        r_title = QLabel(tr("文件列表"))
        r_title.setObjectName("cardTitle")
        self.file_count = QLabel("0")
        self.file_count.setObjectName("fileCountBadge")
        self.file_count.setAlignment(Qt.AlignCenter)
        self.file_count.setFixedHeight(20)
        r_head.addWidget(r_title)
        r_head.addWidget(self.file_count)
        r_head.addStretch(1)
        # 视图切换：彩色图标按钮，点击循环切换 名称→大图；右键弹菜单二选一
        self.view_btn = QToolButton()
        self.view_btn.setObjectName("viewSwitchBtn")
        self.view_btn.setCheckable(False)
        self.view_btn.setFixedSize(24, 22)
        self.view_btn.setIconSize(QSize(14, 14))
        self.view_btn.clicked.connect(self._cycle_view_mode)
        self.view_btn.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view_menu = QMenu(self)
        self._view_actions: dict[str, object] = {}
        for mode, label in (("name", tr("名称")), ("large", tr("大图"))):
            act = self.view_menu.addAction(self._view_icon(mode), label)
            act.setCheckable(True)
            act.setData(mode)
            act.triggered.connect(lambda _=False, m=mode: self._set_view_mode(m))
            self._view_actions[mode] = act
        self.view_btn.setMenu(None)   # 不挂菜单（避免右下角箭头）；右键菜单走 customContextMenuRequested
        self.view_btn.setToolTip(tr("切换文件列表视图（名称 / 大图）· 左键循环切换，右键菜单"))
        self.view_btn.customContextMenuRequested.connect(
            lambda _p: self.view_menu.exec(self.view_btn.mapToGlobal(self.view_btn.rect().bottomLeft())))
        r_head.addWidget(self.view_btn)
        r_lay.addLayout(r_head)
        self.file_list = QListWidget()
        self.file_list.setViewMode(QListWidget.IconMode)
        self.file_list.setIconSize(QSize(110, 62))
        self.file_list.setResizeMode(QListWidget.Adjust)
        self.file_list.setMovement(QListWidget.Static)
        self.file_list.setSpacing(4)
        self.file_list.setUniformItemSizes(True)
        # 视口 resize 时按新宽度重算图标 item 尺寸，保持整行铺满居中
        self.file_list.viewport().installEventFilter(self)
        self.file_list.itemSelectionChanged.connect(self._on_file_selected)
        # 名称视图：文件名自动换行（代理常驻，wrap_mode 由 _set_view_mode 切换）
        self._name_delegate = _NameWrapDelegate(self.file_list)
        self.file_list.setItemDelegate(self._name_delegate)
        r_lay.addWidget(self.file_list, 1)
        split.addWidget(right)

        # 参数设置面板放到文件列表右侧（splitter 顺序决定视觉顺序）
        split.addWidget(self.panel)

        split.setStretchFactor(0, 1)   # 预览+时间轴：弹性伸缩
        split.setStretchFactor(1, 0)   # 文件列表：固定宽
        split.setStretchFactor(2, 0)   # 参数设置：固定宽
        split.setSizes([940, 232, 248])
        root.addWidget(split, 1)

        # 底部：状态/进度合并为一行；常驻显示（不自动隐藏），避免界面布局跳动
        self.bottom_row = QWidget()
        self.bottom_row.setVisible(True)
        prog_row = QHBoxLayout(self.bottom_row)
        prog_row.setContentsMargins(2, 0, 2, 0)
        prog_row.setSpacing(8)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        self.progress.setFixedHeight(8)
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("hint")
        prog_row.addWidget(self.progress_label, 1)
        prog_row.addWidget(self.progress, 2)
        root.addWidget(self.bottom_row)

        self._msg_timer = QTimer(self)   # 状态文字自动淡出，随后整行收起
        self._msg_timer.setSingleShot(True)
        self._msg_timer.timeout.connect(self._hide_bottom_if_idle)
        self._loading_timer = QTimer(self)   # 打开视频时的等待秒数刷新
        self._loading_timer.setInterval(200)
        self._loading_timer.timeout.connect(self._on_loading_tick)
        self._load_start = 0.0

        # ---- 信号连接 ----
        self.import_btn.clicked.connect(self.import_folder)
        self.import_files_btn.clicked.connect(self.import_files)
        self.outdir_btn.clicked.connect(self.choose_output_dir)
        self.fix_cues_btn.clicked.connect(self._fix_missing_cues)
        self.prev_btn.clicked.connect(lambda: self._nav_file(-1))
        self.next_btn.clicked.connect(lambda: self._nav_file(1))
        self.export_btn.clicked.connect(self.export_now)

        self.v_guide_btn.clicked.connect(lambda: self.preview.add_guide("v"))
        self.h_guide_btn.clicked.connect(lambda: self.preview.add_guide("h"))
        self.clear_guide_btn.clicked.connect(self.preview.clear_guides)

        self.prev_frame_btn.clicked.connect(lambda: self._button_step(-1))
        self.next_frame_btn.clicked.connect(lambda: self._button_step(1))
        self.play_btn.clicked.connect(self.toggle_play)
        self.to_in_btn.clicked.connect(self._set_in_to_playhead)
        self.to_out_btn.clicked.connect(self._set_out_to_playhead)
        self.speed.speed_changed.connect(self._on_speed)
        self.preview_reset_zoom_btn.clicked.connect(self.preview.reset_zoom)
        self.timeline_fit_btn.clicked.connect(self.timeline.fit_selection)
        self.timeline_reset_zoom_btn.clicked.connect(self.timeline.reset_zoom)

        self.panel.cw.valueChanged.connect(self._on_size_input)
        self.panel.ch.valueChanged.connect(self._on_size_input)
        # 面板 W/H → 预览锁定导出尺寸（保持尺寸缩放时拖拽角标显示它）
        self.panel.cw.valueChanged.connect(lambda v: self.preview.set_export_size(v, self.panel.ch.value()))
        self.panel.ch.valueChanged.connect(lambda v: self.preview.set_export_size(self.panel.cw.value(), v))
        # 代码层 set_sizes 会 block spinbox 信号，导出尺寸（角标/保持尺寸缩放的宽高比）由此同步
        self.panel.sizes_changed.connect(self.preview.set_export_size)
        self.panel.swap_btn.clicked.connect(self._swap_wh)
        self.panel.audio_track_combo.currentIndexChanged.connect(self._on_audio_track_changed)
        self.panel.audio_gain_spin.valueChanged.connect(self._on_export_gain)
        self.panel.keep_ratio_chk.toggled.connect(self.preview.set_keep_scale)
        self.panel.keep_size_chk.toggled.connect(self.preview.set_keep_size)
        self.panel.fps.valueChanged.connect(self._refresh_info)
        self.panel.duration.valueChanged.connect(self._on_duration_input)
        self.panel.keep_ratio_chk.toggled.connect(self._refresh_info)
        self.panel.keep_size_chk.toggled.connect(self._refresh_info)
        self.panel.fps_chip_requested.connect(self._fps_to_source)
        self.panel.add_preset_btn.clicked.connect(self._add_preset)

        self.preview.crop_changed.connect(self._on_crop_changed)
        self.preview.frame_step.connect(self._step_frame)
        self.preview.play_toggle.connect(self.toggle_play)
        self.preview.set_in.connect(self._set_in)
        self.preview.set_out.connect(self._set_out)

        self.timeline.range_changed.connect(self._on_range_changed)
        self.timeline.seek_requested.connect(self._seek)
        self.timeline.play_toggle.connect(self.toggle_play)
        self.in_apply.clicked.connect(self._apply_in_edit)
        self.out_apply.clicked.connect(self._apply_out_edit)
        self.hash_btn.clicked.connect(lambda: self._insert_token("#"))
        self.dollar_btn.clicked.connect(lambda: self._insert_token("$"))
        self.pct_btn.clicked.connect(lambda: self._insert_token("%"))
        self.name_tpl.textEdited.connect(lambda _t: setattr(self, "_name_tpl_custom", True))

        # ---- 窗口级快捷键 ----
        # 之前快捷键只在 PreviewWidget 获得焦点时生效，点过列表/时间轴后空格就失灵了
        from PySide6.QtGui import QShortcut
        self._sc = [
            ("Space", lambda: self.toggle_play()),
            ("Left", lambda: self._shortcut_step(-1)),
            ("Right", lambda: self._shortcut_step(1)),
            ("Ctrl+Left", lambda: self._step_frame(-10)),
            ("Ctrl+Right", lambda: self._step_frame(10)),
            ("I", lambda: self._set_in()),
            ("O", lambda: self._set_out()),
        ]
        for key, fn in self._sc:
            QShortcut(QKeySequence(key), self, activated=fn)

        self._init_presets()

    @staticmethod
    def _btn(text: str) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        return b

    def _msg(self, text: str, msec: int = 5000) -> None:
        """状态文字显示在底部进度行左侧，超时后整行收起"""
        self.progress_label.setText(text)
        self.bottom_row.setVisible(True)
        self._msg_timer.start(msec)

    def _hide_bottom_if_idle(self) -> None:
        """空闲时收起进度条、清空状态文字；底部行保持常驻（不隐藏）。"""
        if not (self.worker and self.worker.isRunning()):
            self.progress_label.setText("")
            self.progress.setVisible(False)

    # ------------------------------------------------------------------
    # 预设尺寸（持久化到 JSON，重启保留）
    # ------------------------------------------------------------------
    _PRESET_KEY = "presets"   # 设置键：以 "WxH" 字符串存列表

    _LANG_KEY = "lang"   # 设置键：语言 "zh"/"en"
    _OUTDIR_KEY = "out_dir"   # 设置键：保存位置（输出目录）

    def _lang_label(self) -> str:
        """语言按钮显示目标语言：当前中文显示 EN，英文显示 中。"""
        return "中" if i18n.LANG == "en" else "EN"

    def _toggle_lang(self) -> None:
        """切换语言：保存偏好并重启程序，重启后按新语言重建界面。"""
        new = "en" if i18n.LANG != "en" else "zh"
        i18n.set_lang(new)
        settings.set(self._LANG_KEY, new)
        self._relaunch()

    def _relaunch(self) -> None:
        """重启本程序（绿色版用 exe，开发模式用 python -m video_crop_tool），再退出当前进程。"""
        if getattr(sys, "frozen", False):
            args = [sys.executable] + sys.argv[1:]
        else:
            args = [sys.executable, "-m", "video_crop_tool"] + sys.argv[1:]
        try:
            subprocess.Popen(args)
        except Exception:
            pass
        self.close()   # 触发 closeEvent 优雅停线程
        QApplication.instance().quit()

    def _load_saved_presets(self) -> list[tuple[int, int]]:
        try:
            raw = settings.get(self._PRESET_KEY, [])
            out = []
            for s in (raw if isinstance(raw, list) else [raw]):
                if isinstance(s, str) and "x" in s:
                    w, h = s.split("x")
                    out.append((int(w), int(h)))
            return out
        except Exception:
            return []

    def _save_presets(self) -> None:
        try:
            settings.set(self._PRESET_KEY, [f"{w}x{h}" for w, h in self._presets])
        except Exception:
            pass

    def _init_presets(self) -> None:
        builtin = [(480, 360), (512, 512), (640, 360), (854, 480)]
        saved = self._load_saved_presets()
        self._presets = builtin + [p for p in saved if p not in builtin]
        self._rebuild_presets()

    def _rebuild_presets(self) -> None:
        grid = self.panel.preset_grid
        while grid.count():
            item = grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for i, (w, h) in enumerate(self._presets):
            b = QPushButton(f"{w}×{h}")
            b.setMaximumHeight(30)
            b.setToolTip(tr("点击应用 {}×{}，右键删除").format(w, h))
            b.clicked.connect(lambda _=False, w=w, h=h: self._apply_preset(w, h))
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda _p, w=w, h=h: self._remove_preset(w, h))
            grid.addWidget(b, i // 2, i % 2)

    def _apply_preset(self, w: int, h: int) -> None:
        if self.reader:
            # 先更新面板宽高/导出尺寸，再设剪裁框 → 框按预设比例贴合，且面板同步跟随
            self.panel.set_sizes(w, h)
            self.preview.set_crop_size(w, h)

    def _add_preset(self) -> None:
        w, h = self.panel.cw.value(), self.panel.ch.value()
        if (w, h) not in self._presets:
            self._presets.append((w, h))
            self._save_presets()
            self._rebuild_presets()

    def _remove_preset(self, w: int, h: int) -> None:
        if (w, h) in self._presets and (w, h) not in ((480, 360), (512, 512), (640, 360), (854, 480)):
            self._presets.remove((w, h))
            self._save_presets()
            self._rebuild_presets()

    # ------------------------------------------------------------------
    # 导入与文件列表
    # ------------------------------------------------------------------
    def import_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, tr("选择视频文件夹"))
        if d:
            self.add_files_from_dir(d)

    def import_files(self) -> None:
        """添加一个或多个视频文件（可跨文件夹多选）。"""
        exts = " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))
        files, _ = QFileDialog.getOpenFileNames(
            self, tr("选择视频文件"), "",
            f"{tr('视频文件 ({})').format(exts)};;{tr('所有文件 (*)')}")
        added = 0
        self._bulk_importing = True
        self.file_list.setUpdatesEnabled(False)
        try:
            for p in files:
                if self.add_file(p):
                    added += 1
        finally:
            self.file_list.setUpdatesEnabled(True)
            self.file_list.viewport().update()
            self._bulk_importing = False
        if files and added == 0:
            self._msg(tr("所选文件已存在或无法添加"), 4000)
        elif added:
            self._msg(tr("已添加 {} 个视频").format(added), 4000)

    def add_files_from_dir(self, folder: str) -> None:
        """递归扫描文件夹（含子文件夹）下的视频文件。"""
        added = 0
        # 批量导入：关掉列表后台刷新（逐条 addItem 会触发大量重排/重绘），
        # 并抑制每文件的底栏状态提示，结束后一次刷新 + 汇总提示。
        self._bulk_importing = True
        self.file_list.setUpdatesEnabled(False)
        try:
            for root, dirs, names in os.walk(folder):
                dirs.sort()   # 子文件夹按名排序，结果稳定
                for name in sorted(names):
                    if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                        if self.add_file(os.path.join(root, name)):
                            added += 1
        finally:
            self.file_list.setUpdatesEnabled(True)
            self.file_list.viewport().update()
            self._bulk_importing = False
        if added == 0:
            QMessageBox.information(self, tr("提示"), tr("该文件夹及子文件夹下没有找到视频文件"))
        else:
            self._msg(tr("已添加 {} 个视频").format(added), 4000)

    def add_file(self, path: str) -> bool:
        path = os.path.abspath(path)
        if path in self.files:
            return False
        self.files.append(path)
        self.file_jobs.setdefault(path, {})
        item = QListWidgetItem()
        item.setData(Qt.UserRole, path)
        item.setToolTip(path)
        if self._view_mode == "name":
            item.setText(os.path.basename(path))
            item.setTextAlignment(Qt.AlignCenter)
            # 行高交给 _NameWrapDelegate 按换行后的行数计算，这里不写死 sizeHint
            item.setSizeHint(QSize(-1, -1))
        else:
            w, h = self._icon_item_size()
            item.setSizeHint(QSize(w, h))
        self.file_list.addItem(item)
        # 新导入提示：未被选中前高亮，选中后清除（见 _on_file_selected）
        item.setData(NEW_ITEM_ROLE, True)   # 新导入提示：delegate 绘制高亮，选中后清除
        self._file_items[path] = item
        self.file_count.setText(str(len(self.files)))
        # 后台抓取首帧缩略图
        self._icon_queue.put(path)
        # 导入反馈：状态栏提示 + 日志（批量导入不逐条弹状态，避免底栏/布局反复刷新）
        if not getattr(self, "_bulk_importing", False):
            self._msg(tr("已添加：{}").format(os.path.basename(path)), 3000)
        log.info("导入视频: %s", path)
        return True

    def _on_file_selected(self) -> None:
        """选中变化：清掉"刚导入"高亮（恢复正常选中态）。

        不再直接打开文件——部分 Qt 平台悬停列表项会触发 selectionChanged，
        据此 open_file 会导致"悬停就切换预览"。打开只由真实鼠标点击
        （eventFilter 的 MouseButtonRelease）和 _nav_file 触发。
        """
        for it in self.file_list.selectedItems():
            it.setData(NEW_ITEM_ROLE, None)   # 清掉"刚导入"高亮（恢复正常选中态）

    def _nav_file(self, delta: int) -> None:
        if not self.files:
            return
        idx = self.files.index(self.current_path) if self.current_path in self.files else 0
        idx = (idx + delta) % len(self.files)
        self.file_list.setCurrentRow(idx)
        self.open_file(self.files[idx])

    def _on_icon(self, path: str, icon: QIcon) -> None:
        self.file_icons[path] = icon
        item = self._file_items.get(path)
        if item is not None:
            self._apply_item_icon(item, path)

    # ------------------------------------------------------------------
    # 打开视频（mpv 后台线程异步加载）
    # ------------------------------------------------------------------
    def open_file(self, path: str) -> bool:
        log.info("打开视频: %s", path)
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, tr("提示"), tr("正在批量裁切，请稍候或先停止"))
            return False
        self._save_current_job()
        self.current_path = path
        self._update_save_path_label()   # 未显式设置保存位置时，默认路径随源视频变化
        self._frame_idx = 0
        self._frac = 0.0
        self._playing = False
        self._playing = False
        self.play_btn.setChecked(False)
        self._settle_seq += 1   # 切换文件：作废旧请求
        # 文件名模板自动填充当前视频名（用户自定义后不再覆盖）
        if not self._name_tpl_custom:
            self.name_tpl.setText(os.path.splitext(os.path.basename(path))[0])
        # 异步加载：元数据与首帧由 _on_mpv_loaded 回调后填充 UI
        # --wid 内嵌：把视频容器窗口的原生句柄传给 mpv（worker 里创建实例用）
        self._mpv_queue.put(("load", path, self.preview._video_layer.native_handle()))
        # 加载指示：显示已等待秒数（mpv 就绪后由 _on_mpv_loaded 收起）。
        # 原生预览没有可测的转码百分比，用"已等待 X 秒"让用户知道还要多久
        self._load_start = time.time()
        self._msg_timer.stop()
        self.progress.setRange(0, 0)   # 忙态（不确定进度）
        self.progress.setVisible(True)
        self.bottom_row.setVisible(True)
        self.progress_label.setText(tr("正在加载预览 0s"))
        self._loading_timer.start()
        if not ffmpeg_available():
            self._msg(tr("警告：未检测到 ffmpeg，裁切导出将不可用"), 8000)
        return True

    def _on_loading_tick(self) -> None:
        """打开视频时每 200ms 刷新"已等待秒数"（mpv 就绪即停止）。"""
        self.progress_label.setText(tr("正在加载预览 {:.1f}s").format(time.time() - self._load_start))

    def _on_mpv_loaded(self, path: str, meta: dict) -> None:
        """mpv 加载完成：填充元数据与 UI（主线程）。"""
        if path != self.current_path:
            return   # 期间已切换文件，忽略过期加载
        self._clear_loading()
        # 把 worker 的播放器绑定到预览的 mpv 渲染层（--wid 窗口），后续
        # set_reader 用它的 dar 给裁剪构图视图对齐显示比例
        self.preview._video_layer.set_player(self._mpv_thread._player)
        self.reader = _VideoMeta(**meta)
        r = self.reader
        self._frame_idx = 0
        # 首帧静止画面（源分辨率原图）
        self._scrub_seq += 1
        self._mpv_queue.put(("settle", 0, self._scrub_seq))
        self._populate_audio_tracks(path)   # 枚举音轨，填充下拉（预览+导出映射）
        # 载入完成后重发当前预览音量增益与音轨（玩家此时才就绪，之前若在玩家
        # 未就绪时调过会丢）
        self._mpv_queue.put(("gain", self._preview_gain))
        self._mpv_queue.put(("aid", self._audio_track_ordinal))
        # 恢复该文件的裁切参数（人性化：跨文件保留各自设置）
        job = self.file_jobs.get(path, {})
        self.timeline.set_duration(r.duration, min_span_sec=1.0 / r.fps)
        self.preview.set_reader(r)
        self.preview.set_export_size(self.panel.cw.value(), self.panel.ch.value())
        # 裁剪尺寸可任意输入（不限视频宽高）；超出画面由 set_crop_size 等比缩小适配
        self.panel.cw.setRange(2, 99999)
        self.panel.ch.setRange(2, 99999)
        in_p = job.get("in", 0.0)
        out_p = job.get("out", r.duration)
        self.timeline.set_range(in_p, out_p)
        self._request_range_preview()   # 载入后显示选区首帧/尾帧
        if job.get("crop"):
            self.preview.set_crop(QRectF(*job["crop"]))
        else:
            self.preview.set_crop(QRectF(0, 0, r.width, r.height))
        if job.get("fps"):
            self.panel.fps.setValue(job["fps"])
        self.src_label.setText(f"{r.width}x{r.height}  {fmt_time(r.duration, False)}")
        self.file_count.setText(str(len(self.files)))
        self._refresh_info()
        self._update_timeline_labels()
        self._update_frame_label()

    # -- 音轨：预览切换 + 导出映射 --------------------------------------------
    def _probe_audio_tracks(self, path: str) -> list[dict]:
        """ffprobe 列出所有音频流（index/codec/channels/language），顺序即音轨序号。"""
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=index,codec_name,channels,channel_layout,sample_rate:stream_tags=language",
                 "-of", "json", path],
                capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            return json.loads(out).get("streams", [])
        except Exception:
            return []

    def _populate_audio_tracks(self, path: str) -> None:
        """枚举音轨填充下拉：同时驱动预览(QMediaPlayer)切换与导出(ffmpeg -map)映射。

        下拉第 0 项 = 自动(默认音轨，不 map)；之后每项对应一条音频流(序号 = 下拉索引-1)。
        """
        self._audio_track_ordinal = -1
        combo = self.panel.audio_track_combo
        combo.blockSignals(True)
        combo.clear()
        tracks = self._probe_audio_tracks(path)
        self._audio_tracks = tracks
        if not tracks:
            combo.setEnabled(False)
            self.panel.audio_only_chk.setChecked(False)
            self.panel.audio_only_chk.setEnabled(False)
            combo.blockSignals(False)
            return
        combo.setEnabled(True)
        self.panel.audio_only_chk.setEnabled(True)
        combo.addItem(tr("自动 (默认)"))
        for i, t in enumerate(tracks):
            label = tr("音轨 {}").format(i + 1)
            if t.get("codec_name"):
                label += f" · {t['codec_name']}"
            ch = t.get("channels")
            if ch:
                label += f" · {ch}ch"
            lang = (t.get("tags") or {}).get("language", "und")
            if lang and lang != "und":
                label += f" · {lang}"
            combo.addItem(label)
        combo.setCurrentIndex(0)   # 默认：自动（不限定音轨）
        combo.blockSignals(False)

    def _on_audio_track_changed(self, idx: int) -> None:
        """用户切换音轨：更新导出映射序号 + 预览声音(mpv aid)。idx==0 表示自动(默认)。"""
        if idx <= 0:
            self._audio_track_ordinal = -1   # 自动：不限定音轨
        else:
            ordn = idx - 1
            if ordn >= len(self._audio_tracks):
                ordn = -1
            self._audio_track_ordinal = ordn
        self._mpv_queue.put(("aid", self._audio_track_ordinal))

    def _on_mpv_load_failed(self, path: str, err: str) -> None:
        # 用状态栏提示而非模态弹窗：弹窗会阻塞主线程事件循环（播放/预览
        # 全停），且导入多文件时连续失败会"疯狂弹窗"
        if path == self.current_path:
            self.reader = None
            self._clear_loading()
            log.error("打开失败: %s — %s", path, err)
            self._msg(tr("无法打开 {}：{}").format(os.path.basename(path), err[:80]), 8000)

    def _clear_loading(self) -> None:
        """收起打开视频时的忙态加载指示（还原进度条为 0..1000 数值模式）。

        加载成功/失败都要收起进度条并清空文字；底部行常驻，不隐藏。
        """
        self._loading_timer.stop()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        self.progress_label.setText("")

    def _save_current_job(self) -> None:
        if not self.reader or not self.current_path:
            return
        self.file_jobs[self.current_path] = {
            "crop": (int(self.preview.crop_rect().x()), int(self.preview.crop_rect().y()),
                     int(self.preview.crop_rect().width()), int(self.preview.crop_rect().height())),
            "in": self._current_in(),
            "out": self._current_out(),
            "fps": self.panel.fps.value(),
        }

    # ------------------------------------------------------------------
    # 播放控制
    # ------------------------------------------------------------------
    def _current_in(self) -> float:
        return self.timeline._in_pct * self.reader.duration if self.reader else 0.0

    def _current_out(self) -> float:
        return self.timeline._out_pct * self.reader.duration if self.reader else 0.0

    def toggle_play(self) -> None:
        if not self.reader:
            return
        self._playing = not self._playing
        log.info("播放状态: %s", "播放" if self._playing else "暂停")
        self.play_btn.setChecked(self._playing)
        self.play_btn.setText(tr("⏸ 暂停") if self._playing else tr("▶ 播放"))
        if self._playing:
            # 只播放裁切条选中的内容：起始位置若在选区外，先跳到入点
            in_f = int(self._current_in() * self.reader.fps)
            out_f = int(self._current_out() * self.reader.fps)
            if not (in_f <= self._frame_idx <= out_f):
                self._frame_idx = in_f
            # mpv 播放（视频+音频原生同步，ab-loop 选区循环）从当前帧起播
            self._mpv_queue.put(("loop", self._current_in(), self._current_out()))
            self._mpv_queue.put(("play", self.speed.speed()))
            self._play_timer.start()
        else:
            self._play_timer.stop()
            self._mpv_queue.put(("pause",))
            # 暂停后画面静止：请求 mpv 截图当前帧（源分辨率原图）
            self._request_settle()

    def _on_tick(self) -> None:
        """播放定时器（33ms）：帧号/进度直接读 mpv time_pos；画面与声音由 mpv 原生渲染。"""
        if not self.reader or not self._playing:
            return
        # 帧号/进度直接读 mpv time_pos（画面/声音 mpv 渲染，A/V 原生同步）
        try:
            tp = self._mpv_thread._player.time_pos()
            idx = max(0, min(int(tp * self.reader.fps), self.reader.frame_count - 1))
            if idx != self._frame_idx:
                self._frame_idx = idx
                self._update_frame_label()
                if self.reader.duration > 0:
                    self.timeline.set_playhead(idx / self.reader.fps)
        except Exception:
            pass

    def _request_settle(self) -> None:
        """请求后台出当前帧的精确帧（拖动结束 / 步进 / 暂停共用）。

        mpv 截图当前帧（源分辨率原图），经 frame_ready 回传显示。
        """
        if not self.reader:
            return
        self._scrub_seq += 1   # 新请求序号：旧结果作废
        self._mpv_queue.put(("settle", self._frame_idx, self._scrub_seq))

    def _manual_step(self, delta: int) -> None:
        """手动步进（按钮/A/D 键）：若正在播放先自动暂停，再步进一帧。

        "点击步进自动暂停"——用户对帧的精确定位发生在暂停态。
        """
        if self._playing:
            self.toggle_play()   # 暂停
        self._step_frame(delta)

    def _parse_step_seconds(self) -> float | None:
        """读步进输入框：留空/非法/<=0 → None(按1帧)；否则返回秒数。"""
        txt = self.step_input.text().strip()
        if not txt:
            return None
        try:
            v = float(txt)
        except ValueError:
            return None
        return v if v > 0 else None

    def _button_step(self, direction: int) -> None:
        """前进/后退步进按钮：输入框填了秒数 → 按秒跳转；留空 → 逐帧步进。
        两者都先暂停(与手动步进一致)；跳转复用 _seek(精确落点+settle定版)。
        """
        if not self.reader:
            return
        secs = self._parse_step_seconds()
        if secs is None:
            self._manual_step(direction)
        else:
            cur = self._frame_idx / self.reader.fps
            dur = self.reader.duration
            target = cur + direction * secs
            if dur > 0:
                target = max(0.0, min(dur, target))
            self._seek(target)

    def _set_in_to_playhead(self) -> None:
        """画面到入点：把当前播放头帧设为入点，出点保持不变(必要时被最小跨距钳制)。"""
        if not self.reader:
            return
        in_sec = self._frame_idx / self.reader.fps
        self.timeline.set_range(in_sec, self._current_out())
        self._on_range_changed(in_sec, self._current_out())

    def _set_out_to_playhead(self) -> None:
        """画面到出点：把当前播放头帧设为出点，入点保持不变(必要时被最小跨距钳制)。"""
        if not self.reader:
            return
        out_sec = self._frame_idx / self.reader.fps
        self.timeline.set_range(self._current_in(), out_sec)
        self._on_range_changed(self._current_in(), out_sec)

    def _step_motion(self, direction: int) -> bool:
        """步进的"动效"：帧步进或按秒跳，更新帧号/进度/画面。返回是否为秒跳(True=秒)。"""
        if not self.reader:
            return False
        if self._playing:
            self.toggle_play()   # 暂停，与手动步进一致
        secs = self._parse_step_seconds()
        if secs is None:
            self._step_frame(direction)
            return False
        fps = self.reader.fps
        n = self.reader.frame_count
        target = max(0, min(n - 1, self._frame_idx + direction * round(secs * fps)))
        self._frame_idx = target
        self._update_frame_label()
        if self.reader.duration > 0:
            self.timeline.set_playhead(target / fps)
        self._mpv_queue.put(("jogseek", target / fps))
        return True

    def _shortcut_step(self, direction: int) -> None:
        """快捷键(A/D/←→)单次步进：填秒→秒跳，留空→逐帧；秒跳后 settle 定版。"""
        if self._step_motion(direction):
            self._request_settle()

    def _repeat_step(self, direction: int) -> None:
        """长按连发的步进动效：不逐拍 settle（松开时统一定版），避免截图过密卡顿。"""
        self._step_motion(direction)

    def _step_tick(self) -> None:
        """长按 A/D 的连续步进定时器（帧步进/秒跳都支持连发）"""
        self._repeat_step(self._step_dir)

    def _step_hold(self, delta: int) -> None:
        """A/D 首次按下：暂停后按设置步进一帧/秒，并启动长按检测。

        长按超过 300ms 后开始连续步进（帧=逐帧，秒=按秒连发）。
        """
        self._shortcut_step(delta)
        self._step_dir = delta
        self._step_hold_timer.start()

    def _step_hold_expired(self) -> None:
        """长按检测到期：进入持续步进模式"""
        self._step_timer.start()

    def _step_frame(self, delta: int) -> None:
        """步进 delta 帧（mpv frame-step 精确）；播放中由 _on_tick 驱动。"""
        if not self.reader:
            return
        n = self.reader.frame_count
        fps = self.reader.fps
        if self._playing:
            # 播放限定在裁切条选区 [in, out] 内循环（指针在裁切条中即进度条）
            in_f = max(0, int(self._current_in() * fps))
            out_f = min(n - 1, max(in_f, int(self._current_out() * fps)))
            span = max(1, out_f - in_f + 1)
            f = self._frame_idx + delta
            if f > out_f:
                f = in_f + (f - in_f) % span
            elif f < in_f:
                f = out_f - (in_f - f) % span
            self._frame_idx = f
            if self.reader.duration > 0:
                self.timeline.set_playhead(self._frame_idx / fps, follow=True)
            self._update_frame_label()
            return
        self._frame_idx = max(0, min(n - 1, self._frame_idx + delta))
        self._scrub_seq += 1
        # 传相对 delta：worker 用 mpv 的 frame-step 从当前解码位置续解，
        # 避免每次绝对 exact seek 顺解几百帧导致长按步进卡顿
        self._mpv_queue.put(("step", delta, self._scrub_seq))
        self._update_frame_label()
        if self.reader.duration > 0:
            self.timeline.set_playhead(self._frame_idx / fps)

    def _drain_step(self) -> None:
        """松手即停：从队列移除所有未处理的 step(步进)请求，其余任务放回。

        长按步进时每 70ms 入队一个 step；松手只停定时器，队列里堆积的残余 step
        会被 worker 继续执行、导致松手后连走几帧。这里把它们清掉，只让已在途
        的那一帧收尾。
        """
        kept = []
        while True:
            try:
                it = self._mpv_queue.get_nowait()
            except queue.Empty:
                break
            # 清掉残留的 step(帧步进) 与 jogseek(按秒跳)，避免松手后连走几帧/几跳
            if it is None or it[0] not in ("step", "jogseek"):
                kept.append(it)
        for it in kept:
            self._mpv_queue.put(it)

    def _on_mpv_frame(self, seq: int, idx: int, img) -> None:
        """mpv settle/step 回传（设置裁剪构图视图的画面帧；视频由 mpv 渲染）。

        过期结果丢弃：settle/step 低频，严格 seq 校验即可。
        """
        if img is None or self.reader is None:
            return
        if seq != self._scrub_seq:
            return   # 过期请求的结果（stale response）：丢弃
        self._frame_idx = idx
        self._update_frame_label()
        if self.reader.duration > 0:
            self.timeline.set_playhead(idx / self.reader.fps)
        self.preview.set_image(img)

    def _request_range_preview(self) -> None:
        """更新入点/出点小容器：后台 ffmpeg 抽选区首帧/尾帧（低清，coalescing）。"""
        if not self.reader or not self.current_path:
            return
        self._range_seq += 1
        self._range_req = (self.current_path, self._current_in(),
                           self._current_out(), self._range_seq)
        if not self._range_busy:
            self._range_busy = True
            threading.Thread(target=self._do_range_preview, daemon=True).start()

    def _do_range_preview(self) -> None:
        try:
            while self._range_req is not None:
                path, in_sec, out_sec, seq = self._range_req
                self._range_req = None
                in_img = ffmpeg_frame(path, in_sec, max_w=192, max_h=108)
                if in_img is not None:
                    self.range_in_ready.emit(seq, in_img)
                out_img = ffmpeg_frame(path, out_sec, max_w=192, max_h=108)
                if out_img is not None:
                    self.range_out_ready.emit(seq, out_img)
        finally:
            self._range_busy = False

    def _set_in_preview(self, seq: int, img) -> None:
        if seq != self._range_seq:
            return   # 过期结果丢弃
        try:
            pm = QPixmap.fromImage(img).scaled(
                self.in_preview.width(), self.in_preview.height(),
                Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.in_preview.setPixmap(pm)
        except Exception:
            pass

    def _set_out_preview(self, seq: int, img) -> None:
        if seq != self._range_seq:
            return
        try:
            pm = QPixmap.fromImage(img).scaled(
                self.out_preview.width(), self.out_preview.height(),
                Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.out_preview.setPixmap(pm)
        except Exception:
            pass

    def _seek(self, sec: float, scrubbing: bool = False) -> None:
        log.debug("拖动到 %.3fs", sec)
        """拖动/点击时间轴的跳转 —— mpv 原生机制。

        mpv 直接渲染到可见窗口：seek 命令异步发出（mpv 内部 0.3s 合并窗口），
        VO 渲染循环落点帧即上屏（旧帧保留，拖动跟手），不截图不搬运。
        主线程只更新进度条/帧号（画面与进度解耦）。松手后 settle 精确落点。
        """
        if not self.reader:
            return
        # 播放中开始拖动：停 UI 播放状态（音频/定时器）
        if self._playing and not self._seek_timer.isActive():
            self._playing = False
            self.play_btn.setChecked(False)
            self.play_btn.setText(tr("▶ 播放"))
            self._play_timer.stop()
            self._mpv_queue.put(("pause",))
        self._seek_target_sec = sec
        fps = self.reader.fps
        idx = max(0, min(int(sec * fps), self.reader.frame_count - 1))
        # 主线程零解码：只更新进度条/帧号（画面由 mpv 渲染）
        self._frame_idx = idx
        self._update_frame_label()
        if self.reader.duration > 0:
            self.timeline.set_playhead(idx / fps)
        self._scrub_seq += 1
        # 统一用 exact：拖拽画面精确落到指针处，松手时帧不动，不再"停画面又跳一下"。
        # 拖拽中 worker 的 _take_latest 丢弃排队中的旧 seek、只落最新一条，保住实时跟手。
        # scrubbing 参数保留用于承接信号语义（连续 move），此处不再区分精度。
        precision = "exact"
        self._mpv_queue.put(("seek", sec, precision))
        if scrubbing:
            # 拖拽中：取消任何挂起的 settle 定时器。鼠标事件会被系统合并，
            # 快速单向拖拽时相邻 move 可能间隔 >150ms，若放任定时器在此刻触发，
            # 会用「拖拽前端已过时的位置」对 mpv 做一次 exact 校准——画面先被
            # 拖回去(往后窜)再被下一个 move 拖向前(往前窜)。
            self._seek_timer.stop()
        else:
            # 点击/松手：派发精确 seek 后，触发一次 settle 定版（帧号/播放头/音频）。
            self._seek_timer.start()

    def _scrub_tick(self) -> None:
        """拖动画面由 mpv 直接渲染（seek 落点即上屏），无需节拍抽帧。"""
        self._scrub_timer.stop()

    def _seek_finalize(self) -> None:
        """停止拖动后的 Settling：精确帧由 worker 出（与拖动同一条截图流，
        天然串行，无需额外等待）。立即定版帧号/playhead/音频位置。"""
        if not self.reader:
            return
        self._scrub_timer.stop()
        sec = self._seek_target_sec
        self._frame_idx = max(0, min(int(sec * self.reader.fps),
                                     self.reader.frame_count - 1))
        self._update_frame_label()
        if self.reader.duration > 0:
            self.timeline.set_playhead(self._frame_idx / self.reader.fps)
        self._request_settle()

    # ------------------------------------------------------------------
    # 修复缺失 Cues 索引（手动触发：选源/选输出，后台 remux 显示真实进度）
    # ------------------------------------------------------------------
    def _fix_missing_cues(self) -> None:
        if self._remux is not None and self._remux.isRunning():
            QMessageBox.information(self, tr("提示"), tr("正在修复中，请稍候"))
            return
        src, _ = QFileDialog.getOpenFileName(
            self, tr("选择需要修复 Cues 的视频"),
            self.current_path or os.getcwd(),
            tr("视频 (*.mkv *.mp4 *.mov);;所有文件 (*)"))
        if not src:
            return
        stem, ext = os.path.splitext(os.path.basename(src))
        default = os.path.join(os.path.dirname(src), f"{stem}_fixed{ext}")
        dst, _ = QFileDialog.getSaveFileName(
            self, tr("选择修复后的保存位置"), default,
            tr("视频 (*.mkv *.mp4);;所有文件 (*)"))
        if not dst:
            return
        if os.path.abspath(dst) == os.path.abspath(src):
            QMessageBox.information(self, tr("提示"), tr("输出不能覆盖原文件，请换一个保存位置"))
            return
        self._remux = RemuxWorker(src, dst, parent=self)
        self._remux.progress.connect(self._on_remux_progress)
        self._remux.done.connect(self._on_remux_done)
        self._remux.error.connect(self._on_remux_error)
        # 底部行显示修复进度（真实百分比）
        self._msg_timer.stop()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.bottom_row.setVisible(True)
        self.progress_label.setText(tr("正在修复视频索引 Cues…  {}").format(os.path.basename(src)))
        self._remux.start()

    def _on_remux_progress(self, pct: float) -> None:
        if self._remux is not None:
            self.progress.setValue(int(pct * 1000))

    def _on_remux_done(self, dst: str) -> None:
        self.progress.setVisible(False)
        self._msg(tr("修复完成：{}").format(os.path.basename(dst)), 8000)

    def _on_remux_error(self, err: str) -> None:
        self.progress.setVisible(False)
        self._msg(tr("修复失败：{}").format(err), 8000)

    def _on_speed(self, s: float) -> None:
        # 变速：mpv 播放速度（音画同步变速）
        self._mpv_queue.put(("speed", s))

    def _update_frame_label(self) -> None:
        if self.reader:
            sec = self._frame_idx / self.reader.fps
            self.frame_label.setText(tr("{} · 帧 {}").format(fmt_time(sec), self._frame_idx))

    def _set_in(self) -> None:
        if not self.reader:
            return
        in_sec = self._frame_idx / self.reader.fps
        out_sec = self._current_out()
        if out_sec <= in_sec:
            out_sec = self.reader.duration
        self.timeline.set_range(in_sec, out_sec)
        self._on_range_changed(in_sec, out_sec)

    def _set_out(self) -> None:
        if not self.reader:
            return
        out_sec = self._frame_idx / self.reader.fps
        in_sec = self._current_in()
        if out_sec <= in_sec:
            in_sec = 0.0
        self.timeline.set_range(in_sec, out_sec)
        self._on_range_changed(in_sec, out_sec)

    # ------------------------------------------------------------------
    # 裁剪框 / 尺寸联动
    # ------------------------------------------------------------------
    def _on_crop_changed(self, x: int, y: int, w: int, h: int) -> None:
        # 手动输入宽/高期间：面板保持用户输入值，不回写（否则输入的大尺寸会被
        # 缩小后的框值冲掉，看起来“受限于画面尺寸”）。仅鼠标拖动时回写真实框尺寸。
        if (self.reader and not self.panel.keep_size_chk.isChecked()
                and not self.preview._editing_size):
            # 保持尺寸缩放：面板宽高是锁定的导出尺寸，不能随框变化回写
            self.panel.set_sizes(w, h)
        self._info_timer.start()  # 防抖：拖动过程中不逐事件重算信息面板

    def _on_size_input(self) -> None:
        """宽/高输入始终生效：按裁切框中心设定新尺寸（保持尺寸与否都允许显式缩放）"""
        if self.reader:
            self.preview.set_crop_size(self.panel.cw.value(), self.panel.ch.value())

    def _swap_wh(self) -> None:
        """交换宽高：尺寸面板 W↔H 字面对换（1440×1080 ↔ 1080×1440）。

        尺寸(面板/导出)=字面对换值，保持不变形不缩；画面上框按新尺寸比例
        等比缩小适应放进画面（不超出）；导出把框截到的内容填充到尺寸。
        """
        if not self.reader:
            return
        w, h = self.panel.cw.value(), self.panel.ch.value()   # 当前尺寸
        self.panel.cw.setRange(2, 99999)   # 允许超源的纵向/横向分辨率
        self.panel.ch.setRange(2, 99999)
        self.panel.set_sizes(h, w)          # 尺寸 W↔H（导出尺寸同步更新）
        self.preview.set_crop_size(h, w)    # 框按新尺寸比例适应放进画面

    def _on_duration_input(self) -> None:
        if not self.reader:
            return
        dur = self.panel.duration.value()
        in_sec = self._current_in()
        self.timeline.set_range(in_sec, in_sec + dur)
        self._on_range_changed(in_sec, in_sec + dur)

    def _on_range_changed(self, in_sec: float, out_sec: float) -> None:
        self.panel.duration.blockSignals(True)
        self.panel.duration.setValue(out_sec - in_sec)
        self.panel.duration.blockSignals(False)
        self._update_timeline_labels()
        self._info_timer.start()  # 防抖：时间轴拖动中不逐事件重算
        self._request_range_preview()   # 刷新入点/出点小容器（首帧/尾帧）

    def _update_timeline_labels(self) -> None:
        if not self.reader:
            return
        in_sec, out_sec = self._current_in(), self._current_out()
        self.in_edit.blockSignals(True)
        self.out_edit.blockSignals(True)
        self.in_edit.setTime(QTime(0, 0).addMSecs(int(round(in_sec * 1000))))
        self.out_edit.setTime(QTime(0, 0).addMSecs(int(round(out_sec * 1000))))
        self.in_edit.blockSignals(False)
        self.out_edit.blockSignals(False)
        self.dur_label.setText(tr("时长 {:.1f} 秒").format(out_sec - in_sec))

    # -- 入点 / 出点手动输入 ----------------------------------------------
    def _apply_in_edit(self) -> None:
        if not self.reader:
            return
        t = self.in_edit.time()
        in_sec = t.msecsSinceStartOfDay() / 1000.0   # 支持毫秒（HH:mm:ss.zzz）
        in_sec = max(0.0, min(in_sec, self.reader.duration))
        out_sec = self._current_out()
        if out_sec <= in_sec:
            out_sec = min(in_sec + 1.0 / self.reader.fps, self.reader.duration)   # 最少 1 帧
        self.timeline.set_range(in_sec, out_sec)
        self._on_range_changed(in_sec, out_sec)
        self._seek(in_sec)

    def _apply_out_edit(self) -> None:
        if not self.reader:
            return
        t = self.out_edit.time()
        out_sec = t.msecsSinceStartOfDay() / 1000.0   # 支持毫秒（HH:mm:ss.zzz）
        out_sec = max(0.0, min(out_sec, self.reader.duration))
        in_sec = self._current_in()
        if out_sec <= in_sec:
            in_sec = max(0.0, out_sec - 1.0 / self.reader.fps)   # 最少 1 帧
        self.timeline.set_range(in_sec, out_sec)
        self._on_range_changed(in_sec, out_sec)
        self._seek(in_sec)

    # -- 文件名模板 ---------------------------------------------------------
    def _template(self) -> str:
        return self.name_tpl.text().strip()

    def _insert_token(self, token: str) -> None:
        self.name_tpl.insert(token)
        self._name_tpl_custom = True
        self.name_tpl.setFocus()

    @staticmethod
    def _num_to_letters(n: int) -> str:
        """1->a, 2->b, …, 26->z, 27->aa, 28->ab（超过 26 往后叠加）"""
        s = ""
        while n > 0:
            n, r = divmod(n - 1, 26)
            s = chr(ord("a") + r) + s
        return s

    def _expand_filename(self, tpl: str, source: str, seg_no: int) -> str:
        """展开文件名模板：#=序号  $=日期(到分钟)  %=字母；未自定义时用源文件名"""
        base = sanitize_name(os.path.splitext(os.path.basename(source))[0])
        tpl = tpl or base
        tpl = tpl.replace("{name}", base)
        tpl = re.sub(r"#+", lambda m: str(seg_no).zfill(len(m.group(0))) if len(m.group(0)) > 1
                     else str(seg_no), tpl)
        tpl = tpl.replace("$", datetime.now().strftime("%Y%m%d-%H%M"))
        tpl = re.sub(r"%+", lambda _m: self._num_to_letters(seg_no), tpl)
        return sanitize_name(tpl)

    def _export_name(self, tpl: str, source: str, seg_no: int) -> str:
        """导出文件名主干（不含扩展名，也不附加帧范围后缀）。

        模板含计数插入符（# 序号 / % 字母）时按模板展开，如「视频名#」→「视频名1」；
        否则为「模板名_clip序号」，如「视频名_clip1」「视频名_clip2」。
        """
        if "#" in tpl or "%" in tpl:
            return self._expand_filename(tpl, source, seg_no)
        base = self._expand_filename(tpl, source, seg_no)
        return f"{base}_clip{seg_no}"

    # -- 文件列表视图模式 ----------------------------------------------------
    def _cycle_view_mode(self) -> None:
        """view_btn 左键点击：在 名称→大图 间循环切换"""
        order = ("name", "large")
        idx = (order.index(self._view_mode) + 1) % len(order)
        self._set_view_mode(order[idx])

    def _view_icon(self, mode: str) -> QIcon:
        """视图切换图标：大图视图 = icon_large.svg，名称视图 = icon_name.svg。"""
        return self._view_icon_large if mode != "name" else self._view_icon_name

    def _set_view_mode(self, mode: str) -> None:
        self._view_mode = mode
        self.view_btn.setIcon(self._view_icon(mode))
        for m, act in self._view_actions.items():
            act.setChecked(m == mode)
        self.view_btn.setToolTip(tr("切换文件列表视图（当前：{}）").format(self._view_labels[mode]))
        if mode == "name":
            self._name_delegate.wrap_mode = True
            self.file_list.setViewMode(QListWidget.ListMode)
            self.file_list.setIconSize(QSize(18, 18))
            self.file_list.setSpacing(1)
            for i in range(self.file_list.count()):
                it = self.file_list.item(i)
                it.setText(os.path.basename(it.data(Qt.UserRole)))
                it.setTextAlignment(Qt.AlignCenter)
                it.setIcon(self.file_icons.get(it.data(Qt.UserRole), QIcon()))
                it.setSizeHint(QSize(-1, -1))   # 清除固定尺寸，行高由 delegate 按换行行数计算
        else:
            self._name_delegate.wrap_mode = False
            self.file_list.setViewMode(QListWidget.IconMode)
            self.file_list.setResizeMode(QListWidget.Adjust)
            self.file_list.setSpacing(8)
            w, h = self._icon_item_size()
            # iconSize == cell == sizeHint：避免 QIcon 按 iconSize 二次等比缩放缩略图
            self.file_list.setIconSize(QSize(w, h))
            for i in range(self.file_list.count()):
                it = self.file_list.item(i)
                it.setText("")   # 图标模式紧凑：只显示缩略图
                self._apply_item_icon(it, it.data(Qt.UserRole))
            # 关键修复：切回大图后 QListView 不会自动重算 verticalScrollBar 的
            # singleStep，仍停留在列表模式的值（常为 1）。滚轮每格按 singleStep 滚，
            # 于是几乎原地不动、滚动异常缓慢。按图标行高 + 间距显式恢复即可。
            self.file_list.verticalScrollBar().setSingleStep(h + self.file_list.spacing())
        self.file_list.setUniformItemSizes(mode != "name")

    ICON_PAD = 6          # 缩略图在 cell 内的内边距（与高亮框之间留呼吸区，边框完整可见）
    ICON_SIDE = 5         # cell 距视口左右边缘的边距（选中框不顶到边框被圆角裁切）
    ICON_PAINT_DY = 2     # IconMode 绘制 item 图标时固定从矩形顶部向下偏移 2px，画布内容需上移补偿
    _LONG_EXPORT_GUARD_SEC = 5.0   # 防止误操作：默认禁止导出超过该时长的视频片段（面板可取消勾选）

    def _icon_item_size(self) -> tuple[int, int]:
        """大图 item 尺寸：宽度按视口铺满整行（网格整体居中），预留左右边距"""
        w, h = 132, 74
        vw = max(1, self.file_list.viewport().width())
        cols = max(1, vw // (w + 8))
        cell_w = (vw - 2 * self.ICON_SIDE - 8 * (cols - 1)) // cols
        return max(w, cell_w), h

    def _recalc_icon_items(self) -> None:
        if self._view_mode == "name" or not self.file_list:
            return
        w, h = self._icon_item_size()
        self.file_list.setIconSize(QSize(w, h))   # resize 后 iconSize 同步，避免二次缩放
        for i in range(self.file_list.count()):
            it = self.file_list.item(i)
            it.setSizeHint(QSize(w, h))
            self._apply_item_icon(it, it.data(Qt.UserRole))

    def _apply_item_icon(self, item: QListWidgetItem, path: str) -> None:
        icon = self.file_icons.get(path)
        if self._view_mode == "name":
            item.setIcon(icon or QIcon())
            return
        # 大图模式：无论缩略图有没有加载，都先写入统一占位尺寸。否则切回大图时
        # 没缩略图的项会残留列表模式留下的 (-1,-1) 尺寸，导致 IconMode 网格整体
        # 塌陷成极小的格子、挤成一排（滚动异常）。setUniformItemSizes(True) 依赖
        # 等尺寸退化，这里保证每一项都是 (w,h)。
        w, h = self._icon_item_size()
        item.setSizeHint(QSize(w, h))
        if icon is None:
            item.setIcon(QIcon())
            return
        pad = self.ICON_PAD
        content_w, content_h = max(2, w - 2 * pad), max(2, h - 2 * pad)
        # 等比缩放完整显示（不裁切内容），在格子内居中
        # 注意：IconMode 会把 item 图标从矩形顶部往下 ICON_PAINT_DY 处绘制
        #（QSS 边框无关，实测固定偏移），内容需上移同样距离才在高亮框内视觉居中
        src = icon.pixmap(110, 62)
        pm = src.scaled(content_w, content_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        canvas = QPixmap(w, h)
        canvas.fill(Qt.transparent)
        p = QPainter(canvas)
        p.drawPixmap(pad + (content_w - pm.width()) // 2,
                     pad + (content_h - pm.height()) // 2 - self.ICON_PAINT_DY, pm)
        p.end()
        item.setIcon(QIcon(canvas))

    def eventFilter(self, obj, ev) -> bool:
        # 点击任意文本输入控件（文件名/宽高/帧率/时长/入出点/下拉框等）以外的位置
        # → 让当前聚焦的输入控件失焦/退出编辑，避免还要专门点预览框。步进框同样适用。
        if ev.type() == QEvent.MouseButtonPress:
            fw = QApplication.focusWidget()
            if fw is not None and isinstance(fw, (QLineEdit, QSpinBox, QDoubleSpinBox,
                                                  QTimeEdit, QComboBox)):
                if obj is not fw and not fw.isAncestorOf(obj):
                    # 忽略输入控件自身的弹层（下拉框/日历等独立子窗口）的点击，避免误失焦
                    if not (isinstance(obj, QWidget) and obj.isWindow()):
                        fw.clearFocus()
        # 文件列表视口尺寸变化时按新宽度重算图标 item 宽度，保持整行铺满居中
        # （构造早期 file_list 尚未创建，需判空）
        fl = getattr(self, "file_list", None)
        if fl is not None and obj is fl.viewport() and ev.type() == QEvent.Resize:
            self._recalc_icon_items()
        # 文件列表真实鼠标点击（MouseButtonRelease）→ 打开该视频。
        # 不依赖 itemSelectionChanged：部分 Qt 平台悬停列表项会触发选中
        # （未点击），据此打开会导致"悬停就切换预览/时间轴"。
        if fl is not None and obj is fl.viewport() \
                and ev.type() == QEvent.MouseButtonRelease and ev.button() == Qt.LeftButton:
            item = fl.itemAt(ev.position().toPoint())
            if item is not None:
                path = item.data(Qt.UserRole)
                if path and path != self.current_path:
                    log.info("点击打开: %s", path)
                    self.open_file(path)
                return False   # 放行给 QListWidget 正常处理选中
        # A/D 快捷键全局生效（任何焦点下，输入控件除外）：按一次步进一帧，
        # 长按 300ms 后持续步进（等同播放）。返回 True 表示事件已处理。
        if ev.type() == QEvent.KeyPress and ev.key() in (Qt.Key_A, Qt.Key_D) and not ev.isAutoRepeat():
            fw = self.focusWidget()
            if isinstance(fw, (QLineEdit, QSpinBox, QDoubleSpinBox, QTimeEdit, QComboBox)):
                return False   # 输入控件：不拦截，让用户打字
            self._step_hold(-1 if ev.key() == Qt.Key_A else 1)
            return True
        if ev.type() == QEvent.KeyRelease and ev.key() in (Qt.Key_A, Qt.Key_D) and not ev.isAutoRepeat():
            was_repeating = self._step_timer.isActive()   # 是否进入过长按连发
            self._step_hold_timer.stop()
            self._step_timer.stop()
            # 松手即停：清掉队列里所有未处理的步进/秒跳请求，避免松手后连走几帧/几跳
            self._drain_step()
            # 秒跳长按松手：统一 settle 定版到最终位置（连发时未逐拍定版）
            if was_repeating and self._parse_step_seconds() is not None:
                self._request_settle()
            return True
        return super().eventFilter(obj, ev)

    def _on_volume(self, v: int) -> None:
        """预览音量滑块(0~600%) -> mpv af=volume 滤镜（可超 100%，不受 Qt 限制）。"""
        self._preview_gain = v / 100.0
        self.volume_value.setText(f"{v}%")
        self._mpv_queue.put(("gain", self._preview_gain))

    def _on_export_gain(self, v: int) -> None:
        """导出音频增益(0~600%)：写入 _audio_gain，仅作用到导出(-af volume)，不影响预览音量。"""
        self._audio_gain = v / 100.0

    def _fps_to_source(self) -> None:
        if self.reader:
            self.panel.fps.setValue(self.reader.fps)

    # ------------------------------------------------------------------
    # 裁切信息
    # ------------------------------------------------------------------
    def _refresh_info(self) -> None:
        if not self.reader:
            self.panel.set_info([tr("尚未载入视频")])
            return
        r = self.preview.crop_rect()
        crop_w, crop_h = int(r.width()), int(r.height())
        # 导出尺寸：勾选"保持尺寸缩放"→ 锁定面板 W/H（框内容缩放输出）；否则按框实际尺寸
        if self.panel.keep_size_chk.isChecked():
            out_w, out_h = self.panel.cw.value(), self.panel.ch.value()
        else:
            out_w, out_h = crop_w, crop_h
        fps = self.panel.fps.value()
        dur = max(0.0, self._current_out() - self._current_in())
        # 导出已按源码率封顶（-maxrate 源码率）：估算 = 源码率 × 时长，比固定 5% 系数准
        if self.reader and self.reader.video_bitrate > 0:
            est = self.reader.video_bitrate * dur / 8.0
        else:
            est = estimate_size_bytes(dur, out_w, out_h, fps)
        in_sec, out_sec = self._current_in(), self._current_out()
        lines = [
            tr("分辨率 {}×{}").format(self.reader.width, self.reader.height),
            tr("裁剪区 {}×{}").format(crop_w, crop_h),
            tr("输出尺寸 {}×{}").format(out_w, out_h),
            tr("帧率 {:.2f}fps").format(fps),
            tr("时间 {}–{}").format(fmt_time(in_sec), fmt_time(out_sec)),
            tr("时长 {:.1f} 秒").format(dur),
            tr("预计大小 ≈{}").format(human_size(est)),
        ]
        self.panel.set_info(lines)

    # ------------------------------------------------------------------
    # 导出（全部走 FFmpegWorker，UI 不阻塞）
    # ------------------------------------------------------------------
    def _ensure_output_dir(self) -> bool:
        if self._output_dir:
            os.makedirs(self._output_dir, exist_ok=True)
            return True
        if self.current_path:
            default = os.path.join(os.path.dirname(self.current_path), "crops")
        else:
            default = os.path.join(os.getcwd(), "crops")
        self._output_dir = default
        os.makedirs(self._output_dir, exist_ok=True)
        self._update_save_path_label()
        self._msg(tr("保存位置：{}").format(self._output_dir))
        return True

    def choose_output_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, tr("选择保存位置"), self._output_dir or os.getcwd())
        if d:
            self._output_dir = d
            self._save_outdir()
            self._update_save_path_label()
            self._msg(tr("保存位置：{}").format(d), 5000)

    def _save_outdir(self) -> None:
        try:
            settings.set(self._OUTDIR_KEY, self._output_dir)
        except Exception:
            pass

    def _effective_output_dir(self) -> str:
        """实际会被使用的保存位置：显式设置则用之，否则按当前视频/工作目录算默认值。"""
        if self._output_dir:
            return self._output_dir
        if self.current_path:
            return os.path.join(os.path.dirname(self.current_path), "crops")
        return os.path.join(os.getcwd(), "crops")

    def _update_save_path_label(self) -> None:
        p = self._effective_output_dir()
        self.save_path_label.setText(tr("当前保存位置：{}").format(p))
        self.save_path_label.setToolTip(p)

    def open_output_dir(self) -> None:
        """在系统文件管理器中打开当前保存位置（目录不存在则先创建）。"""
        p = self._effective_output_dir()
        try:
            os.makedirs(p, exist_ok=True)
        except OSError as e:
            self._msg(tr("无法打开目录：{}").format(e), 6000)
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(p)):
            self._msg(tr("无法打开目录：{}").format(p), 6000)

    # -- 面板参数持久化:帧率/导出时长/导出选项(存到 JSON) -------------------
    # hw_chk 由硬件检测(NVENC)驱动,不在此持久化;cw/ch 与裁切框联动,也不在此存。
    _PANEL_KEY = "panel"

    def _panel_state(self) -> dict:
        p = self.panel
        return {
            "fps": float(p.fps.value()),
            "duration": float(p.duration.value()),
            "chk_video": p.chk_video.isChecked(),
            "chk_image": p.chk_image.isChecked(),
            "img_format": p.img_format.currentText(),
            "jpg_q": int(p.jpg_q.value()),
            "audio_only": p.audio_only_chk.isChecked(),
            "audio_gain": int(p.audio_gain_spin.value()),
            "keep_audio": p.keep_audio_chk.isChecked(),
            "keep_ratio": p.keep_ratio_chk.isChecked(),
            "keep_size": p.keep_size_chk.isChecked(),
            "long_guard": p.long_guard_chk.isChecked(),
        }

    def _save_panel_state(self) -> None:
        try:
            settings.set(self._PANEL_KEY, self._panel_state())
        except Exception:
            pass

    def _restore_panel_state(self) -> None:
        try:
            d = settings.get(self._PANEL_KEY, {}) or {}
        except Exception:
            return
        if not isinstance(d, dict):
            return
        p = self.panel
        if "fps" in d:
            p.fps.setValue(float(d["fps"]))
        if "duration" in d:
            p.duration.setValue(float(d["duration"]))
        if "chk_video" in d:
            p.chk_video.setChecked(bool(d["chk_video"]))
        if "chk_image" in d:
            p.chk_image.setChecked(bool(d["chk_image"]))
        if "img_format" in d:
            i = p.img_format.findText(str(d["img_format"]))
            if i >= 0:
                p.img_format.setCurrentIndex(i)
        if "jpg_q" in d:
            p.jpg_q.setValue(int(d["jpg_q"]))
        if "audio_only" in d:
            p.audio_only_chk.setChecked(bool(d["audio_only"]))
        if "audio_gain" in d:
            p.audio_gain_spin.setValue(int(d["audio_gain"]))
        if "keep_audio" in d:
            p.keep_audio_chk.setChecked(bool(d["keep_audio"]))
        if "keep_ratio" in d:
            p.keep_ratio_chk.setChecked(bool(d["keep_ratio"]))
        if "keep_size" in d:
            p.keep_size_chk.setChecked(bool(d["keep_size"]))
        if "long_guard" in d:
            p.long_guard_chk.setChecked(bool(d["long_guard"]))

    def _source_sar(self) -> float:
        """源像素宽高比(PAR)。变形源(如 1440x1080 @PAR 4:3)时 !=1.0，
        导出管线据此把裁剪内容还原到显示宽高比，避免被拉伸。"""
        r = self.reader
        if not r or not getattr(r, "width", 0):
            return 1.0
        sar = getattr(r, "sar", 0.0) or 0.0
        if sar > 0 and abs(sar - 1.0) > 1e-3:
            return sar
        dar = getattr(r, "dar", 0.0) or 0.0
        if dar > 0 and r.height > 0:
            return dar * r.height / r.width
        return 1.0

    def _segment_job(self, path: str) -> CropJob:
        job = self.file_jobs.setdefault(path, {})
        if path == self.current_path and self.reader:
            r = self.preview.crop_rect()
            job["crop"] = (int(r.x()), int(r.y()), int(r.width()), int(r.height()))
            job["in"] = self._current_in()
            job["out"] = self._current_out()
            job["fps"] = self.panel.fps.value()
        crop = job.get("crop", (0, 0, 640, 480))
        in_p = job.get("in", 0.0)
        out_p = job.get("out", max(in_p + 1.0, in_p))
        return CropJob(
            source=path,
            out_dir=self._output_dir,
            crop_x=crop[0], crop_y=crop[1], crop_w=crop[2], crop_h=crop[3],
            in_point=in_p, out_point=out_p,
            fps=job.get("fps", 30.0),
            out_w=self.panel.cw.value(), out_h=self.panel.ch.value(),
            # 保持尺寸缩放：导出分辨率锁定面板 W/H（否则按框实际尺寸导出）
            keep_scale=not self.panel.keep_size_chk.isChecked(),
            keep_audio=self.panel.keep_audio_chk.isChecked(),
            audio_track=self._audio_track_ordinal,
            audio_gain=self._audio_gain,
            hw=self.panel.hw_chk.isChecked(),
            label=job.get("label", ""),
            kind="segment",
            sar=self._source_sar(),
        )

    def _frame_job(self, path: str, at_sec: float) -> CropJob:
        if path == self.current_path and self.reader:
            r = self.preview.crop_rect()
            crop = (int(r.x()), int(r.y()), int(r.width()), int(r.height()))
        else:
            crop = self.file_jobs.get(path, {}).get("crop", (0, 0, 640, 480))
        return CropJob(
            source=path, out_dir=self._output_dir,
            crop_x=crop[0], crop_y=crop[1], crop_w=crop[2], crop_h=crop[3],
            in_point=at_sec, out_point=0.0,
            fps=30.0,
            # 保持尺寸缩放：图片同样锁定为面板宽高（缩放输出），否则按框尺寸出图
            out_w=self.panel.cw.value() if self.panel.keep_size_chk.isChecked() else 0,
            out_h=self.panel.ch.value() if self.panel.keep_size_chk.isChecked() else 0,
            keep_scale=not self.panel.keep_size_chk.isChecked(),
            keep_audio=False, hw=False, label="", kind="frame",
            img_format=self.panel.image_format(), jpg_quality=self.panel.jpg_q.value(),
            sar=self._source_sar(),
        )

    def _audio_job(self, path: str) -> CropJob:
        job = self.file_jobs.get(path, {})
        crop = job.get("crop", (0, 0, 640, 480))
        in_p = job.get("in", 0.0)
        out_p = job.get("out", max(in_p + 1.0, in_p))
        return CropJob(
            source=path, out_dir=self._output_dir,
            crop_x=crop[0], crop_y=crop[1], crop_w=crop[2], crop_h=crop[3],
            in_point=in_p, out_point=out_p,
            fps=30.0, out_w=0, out_h=0, keep_scale=True,
            keep_audio=False, audio_track=self._audio_track_ordinal,
            audio_gain=self._audio_gain, hw=False, label="", kind="audio",
        )

    def _jobs_for(self, path: str) -> list[CropJob]:
        """按右侧导出选项生成该文件的任务：视频 / 图片 / 声音，可任意勾选、可全不选。"""
        want_video = self.panel.wants_video()
        want_image = self.panel.wants_image()
        want_audio = self.panel.wants_audio()
        # 同一视频每剪切一次序号 +1：#=1,2,3…  %=a,b,c…
        self._seg_counters[path] = self._seg_counters.get(path, 0) + 1
        seg_no = self._seg_counters[path]
        fname = self._export_name(self._template(), path, seg_no)
        jobs: list[CropJob] = []
        seg_job = None
        if want_video:
            seg_job = self._segment_job(path)
            seg_job.filename = fname
            jobs.append(seg_job)
        if want_image:
            if want_video and seg_job is not None:
                # 视频+图片：图片取视频段的第一帧（入点帧），与视频画面一致
                in_sec = seg_job.in_point
            elif path == self.current_path and self.reader:
                # 单独图片：导出当前预览帧（与"导出当前帧"一致）
                in_sec = self._frame_idx / self.reader.fps
            else:
                in_sec = self.file_jobs.get(path, {}).get("in", 0.0)
            j = self._frame_job(path, in_sec)
            j.filename = fname
            if want_video and seg_job is not None:
                # 图片与视频同名：图片沿用视频段的 in/out 标记（仅扩展名不同）
                j.out_point = seg_job.out_point
            jobs.append(j)
        if want_audio:
            j = self._audio_job(path)
            j.filename = fname
            jobs.append(j)
        return jobs

    def export_now(self) -> None:
        """“导出”按钮：按右侧导出勾选项（视频/图片/声音，可任意组合、可全不选）。

        只勾了“导出图片”（且未勾声音）→ 导出当前预览帧；其余组合
        （含视频 / 含声音）→ 走 _jobs_for 统一分发。全不选则提示。
        """
        if not self.reader:
            QMessageBox.information(self, tr("提示"), tr("请先导入并打开一个视频"))
            return
        if not self.panel.has_export_selection():
            QMessageBox.information(self, tr("提示"), tr("请至少勾选一种导出内容（视频/图片/声音）"))
            return
        if self.panel.wants_image() and not self.panel.wants_video() and not self.panel.wants_audio():
            self.export_frame()
        else:
            self.export_current()

    def _check_segment_duration(self, jobs: list[CropJob]) -> bool:
        """导出前检查：勾选了“超过 5 秒禁止导出”时，任何超长视频片段都阻止导出。

        防止误操作（默认开启）：误点导出长片段会白等很久还占磁盘。
        返回 False 表示已被阻止（已弹窗说明）。
        """
        if not getattr(self.panel, "long_guard_chk", None) or not self.panel.long_guard_chk.isChecked():
            return True
        for j in jobs:
            # 允许恰好 5 秒：加 1ms 容差，避免时间轴百分比换算产生的浮点越界(如 5.0001s)误拦
            if j.kind == "segment" and j.duration() > self._LONG_EXPORT_GUARD_SEC + 0.001:
                QMessageBox.warning(
                    self, tr("已阻止导出"),
                    tr("当前选区时长 {:.1f}s，超过 {:.0f}s 上限，已取消导出。\n"
                       "（确需导出长片段时，请取消勾选“超过 5 秒禁止导出”）")
                    .format(j.duration(), self._LONG_EXPORT_GUARD_SEC))
                return False
        return True

    def export_current(self) -> None:
        if not self.reader:
            QMessageBox.information(self, tr("提示"), tr("请先导入并打开一个视频"))
            return
        if not self._ensure_output_dir():
            return
        self._save_current_job()
        jobs = self._jobs_for(self.current_path)
        if not self._check_segment_duration(jobs):
            return
        self._start_queue(jobs)

    def export_frame(self) -> None:
        """把当前预览帧按裁剪区域导出为图片（jpg/png，按导出选项的格式与质量）"""
        if not self.reader:
            return
        if not self._ensure_output_dir():
            return
        # 拖动未松手时先定版当前拖动位置，保证导出的是预览正在显示的那一帧
        if self._seek_timer.isActive():
            self._seek_finalize()
        self._seg_counters[self.current_path] = self._seg_counters.get(self.current_path, 0) + 1
        seg_no = self._seg_counters[self.current_path]
        j = self._frame_job(self.current_path, self._frame_idx / self.reader.fps)
        j.filename = self._export_name(self._template(), self.current_path, seg_no)
        self._start_queue([j])

    def export_all(self) -> None:
        if not self.files:
            QMessageBox.information(self, tr("提示"), tr("文件列表为空，请先导入视频"))
            return
        if not self._ensure_output_dir():
            return
        self._save_current_job()
        jobs: list[CropJob] = []
        for p in self.files:
            jobs += self._jobs_for(p)
        if not self._check_segment_duration(jobs):
            return
        self._start_queue(jobs)

    def _start_queue(self, jobs: list[CropJob]) -> None:
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, tr("提示"), tr("已有任务在运行，请先停止"))
            return
        self._worker_queue = jobs
        self.worker = FFmpegWorker(jobs, self)
        self.worker.progress.connect(self._on_progress)
        self.worker.job_done.connect(self._on_job_done)
        self.worker.job_error.connect(self._on_job_error)
        self.worker.all_done.connect(self._on_all_done)
        self.worker.status.connect(self._on_worker_status)
        self.progress.setRange(0, 1000)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.bottom_row.setVisible(True)
        self._msg_timer.stop()
        self.progress_label.setText(tr("准备中…"))
        self._set_exporting(True)
        self.worker.start()

    def _set_exporting(self, on: bool) -> None:
        for b in (self.import_btn, self.outdir_btn, self.export_btn):
            b.setEnabled(not on)

    def _on_progress(self, idx: int, total: int, pct: float) -> None:
        overall = (idx + pct) / max(1, total)
        self.progress.setValue(int(overall * 1000))
        self.progress_label.setText(tr("正在裁切 {}/{}").format(idx + 1, total))

    def _on_worker_status(self, msg: str) -> None:
        self._msg(msg)

    def _on_job_done(self, idx: int, total: int, entry: dict) -> None:
        self._msg(tr("已导出：{}").format(entry['output']), 6000)
        # 标记文件列表
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if item.data(Qt.UserRole) == entry["source"]:
                item.setText(f"✓ {entry['output_basename']}")
                break

    def _on_job_error(self, idx: int, total: int, err: str) -> None:
        self._msg(tr("裁切失败：{}").format(err[:120]), 8000)

    def _on_all_done(self, total: int) -> None:
        self._set_exporting(False)
        self.progress.setVisible(False)
        self._msg(tr("全部完成（{} 个任务）").format(total), 8000)

    # ------------------------------------------------------------------
    # 拖放导入
    # ------------------------------------------------------------------
    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent) -> None:
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if os.path.isfile(p) and os.path.splitext(p)[1].lower() in VIDEO_EXTS:
                self.add_file(p)
            elif os.path.isdir(p):
                self.add_files_from_dir(p)

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------
    def closeEvent(self, _e) -> None:
        self._save_panel_state()   # 退出时保存面板参数（帧率/时长/导出选项）
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(3000)
        if self._remux is not None and self._remux.isRunning():
            self._remux.cancel()
            self._remux.wait(3000)
        # 停掉定时器，避免关闭时残留
        self._play_timer.stop()
        self._scrub_timer.stop()
        self._seek_timer.stop()
        self._loading_timer.stop()
        # 优雅停止后台线程（MpvWorker 退出时会显式 close 主播放器，
        # 释放 mpv 资源，不再靠 GC 拖慢关闭）
        self._mpv_queue.put(None)
        self._icon_queue.put(None)
        self._mpv_thread.wait(3000)
        self._icon_thread.wait(2000)


# ---------------------------------------------------------------------------
# 文件列表 item 代理：名称视图下文件名按列宽自动换行
# QListView 的 item 文本只画单行（TextSingleLine，换行符会被忽略），所以换行
# 只能在代理里完成。代理常驻视图（不能换回 None：实测 setItemDelegate(None)
# 会让列表布局失效、visualItemRect 变 -1x-1），用 wrap_mode 开关两种行为：
# - 关闭（大图模式）：尺寸/绘制全部走默认 QStyledItemDelegate 路径，
#   背景/悬停/选中框/缩略图仍由 QSS ::item + 默认绘制完成；
# - 开启（名称模式）：行高按换行行数计算；paint 先让样式画好背景边框，
#   再自己画图标和换行文本（避免样式把单行文本也画出来）。
# ---------------------------------------------------------------------------
class _NameWrapDelegate(QStyledItemDelegate):
    ICON_W = 18      # 名称视图行首缩略图尺寸（与 name 视图 iconSize 一致）
    ICON_GAP = 6     # 图标与文本间距
    PAD_V = 6        # 文本区上下内边距（加大行高）
    MIN_H = 32       # 最小行高（短名/空名也有足够高度）
    _CACHE_MAX = 2048
    # 断行分隔符：在空白 / _ . - / ( ) [ ] 处断，避免把单词/文件名从中间截断
    _BREAK_RE = re.compile(r'(\s+|[._\-/\\()\[\]])')

    def __init__(self, view: QListWidget) -> None:
        super().__init__(view)
        self._view = view
        self.wrap_mode = False
        self._cache: dict[tuple, list[str]] = {}

    def _wrap(self, text: str, width: int, fm) -> list[str]:
        key = (text, width, fm.height())
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        lines: list[str] = []
        for para in text.split("\n"):
            atoms = [a for a in self._BREAK_RE.split(para) if a]   # 词元 + 分隔符
            cur = ""
            for a in atoms:
                # 当前行塞不下这个词元：先断行
                if cur and fm.horizontalAdvance(cur + a) > width:
                    lines.append(cur)
                    cur = ""
                if fm.horizontalAdvance(a) > width:
                    # 单个超长词元（无分隔符可断）：回退逐字符断
                    for ch in a:
                        if cur and fm.horizontalAdvance(cur + ch) > width:
                            lines.append(cur)
                            cur = ch
                        else:
                            cur += ch
                else:
                    cur += a
            if cur:
                lines.append(cur)
        if not lines:
            lines = [""]
        if len(self._cache) >= self._CACHE_MAX:
            self._cache.clear()
        self._cache[key] = lines
        return lines

    def sizeHint(self, option, index) -> QSize:
        if self.wrap_mode and index.data(Qt.DisplayRole):
            width = max(40, self._view.viewport().width() - self.ICON_W - self.ICON_GAP - 4)
            fm = option.fontMetrics
            lines = self._wrap(index.data(Qt.DisplayRole), width, fm)
            # 宽度 0：ListMode 行由视图撑满整列，避免出现横向滚动条
            return QSize(0, max(self.MIN_H, len(lines) * fm.height() + 2 * self.PAD_V))
        explicit = index.data(Qt.SizeHintRole)
        if explicit is not None and explicit.isValid():
            return explicit
        return super().sizeHint(option, index)

    def paint(self, painter, option, index) -> None:
        rect = option.rect
        is_new = bool(index.data(NEW_ITEM_ROLE))
        if is_new:
            # 高亮底色：画在内容下面（QSS 会忽略 setBackground，故在此画）
            painter.save()
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(style.NEW_ITEM_BG))
            painter.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
            painter.restore()
        if self.wrap_mode and index.data(Qt.DisplayRole):
            opt = QStyleOptionViewItem(option)
            self.initStyleOption(opt, index)
            opt.text = ""
            opt.icon = QIcon()
            self._view.style().drawControl(QStyle.CE_ItemViewItem, opt, painter, self._view)

            icon = index.data(Qt.DecorationRole)
            if icon and not icon.isNull():
                ipm = icon.pixmap(self.ICON_W, self.ICON_W)
                painter.drawPixmap(rect.left() + 2, rect.top() + (rect.height() - ipm.height()) // 2, ipm)
            text = index.data(Qt.DisplayRole) or ""
            tleft = rect.left() + self.ICON_W + self.ICON_GAP
            tw = max(10, rect.width() - (tleft - rect.left()) - 4)
            painter.setPen(QColor(style.TEXT if opt.state & QStyle.State_Selected else style.TEXT_SECONDARY))
            painter.drawText(QRectF(tleft, rect.top(), tw, rect.height()),
                             Qt.AlignCenter | Qt.TextWordWrap, text)
        else:
            super().paint(painter, option, index)
        if is_new:
            # 高亮描边：accent 画最上层，保证任何模式下都可见
            painter.save()
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(style.NEW_ITEM_BORDER), 2))
            painter.drawRoundedRect(QRectF(rect).adjusted(1, 1, -1, -1), 8, 8)
            painter.restore()


# ---------------------------------------------------------------------------
# 后台线程：文件首帧缩略图（QThread + 信号，跨线程安全更新 UI）
# 各自独立 PyAV 容器，不跨线程访问；LowPriority 避免与 UI 主线程抢 CPU
# ---------------------------------------------------------------------------
class _IconWorker(QThread):
    icon_ready = Signal(str, object)   # path, QIcon

    def __init__(self, q: queue.Queue, parent=None) -> None:
        super().__init__(parent)
        self._q = q

    def run(self) -> None:
        while True:
            path = self._q.get()
            if path is None:
                break
            try:
                # ffmpeg 子进程抽 45% 处单帧（快、独立、不碰主播放器）。
                # 不做 mpv 兜底：批量导入时逐文件开一个 MvpPlayer（每个都是
                # 一个完整 libmpv 实例，加载/取帧/解帧很重）会拖垮整机，导致
                # 滚动卡顿。ffmpeg 失败就留空（显示默认图标）。
                img = self._ffmpeg_thumb(path)
                if img is not None:
                    pm = QPixmap.fromImage(img).scaled(110, 62, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    self.icon_ready.emit(path, QIcon(pm))
            except Exception:
                pass

    @staticmethod
    def _ffmpeg_thumb(path: str):
        """ffmpeg 抓 45% 处单帧 → QImage；失败返回 None。

        单进程方案：ffmpeg 直接输出 JPEG（image2pipe），QImage.fromData
        解析 —— 免去 ffprobe 探测宽高（少一次子进程启动）。
        """
        import shutil as sh
        import subprocess as sp
        from PySide6.QtGui import QImage
        exe = sh.which("ffmpeg")
        if not exe:
            return None

        def grab(at: float):
            try:
                r = sp.run([exe, "-hide_banner", "-nostdin", "-loglevel", "error",
                            "-threads", "1", "-ss", f"{at:.3f}", "-i", path,
                            "-frames:v", "1",
                            "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                           capture_output=True, timeout=20,
                           creationflags=0x08000000 if os.name == "nt" else 0)
            except Exception:
                return None
            if not r.stdout:
                return None
            img = QImage.fromData(r.stdout, "JPG")
            return img if not img.isNull() else None

        dur = _probe_duration(path)
        at = dur * 0.45 if dur and dur > 0 else 0.0
        return grab(at) or grab(0.0)


def _probe_duration(path: str) -> float:
    """ffprobe 读时长（秒），失败返回 0。"""
    import subprocess as sp
    try:
        r = sp.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "csv=p=0", path], capture_output=True, text=True, timeout=10,
                   creationflags=0x08000000 if os.name == "nt" else 0)
        return float(r.stdout.strip())
    except Exception:
        return 0.0
