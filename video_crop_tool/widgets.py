"""自定义 UI 组件：预览区（裁剪框/参考线）、时间轴、侧边参数面板、播放速度控件。

交互设计对齐 HTML 原型：
- 拖动框内移动裁剪区、拖动边角调整大小、空格播放/暂停、方向键逐帧、I/O 设置入出点
- 时间轴双滑块选取片段，可整体拖动片段
"""
from __future__ import annotations

import logging
import math

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QImage,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QWheelEvent,
    QWindow,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import style
from .core import fmt_time, human_size
from .i18n import tr

log = logging.getLogger(__name__)


def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(10, 8, 10, 10)
    lay.setSpacing(6)
    if title:
        lbl = QLabel(title)
        lbl.setObjectName("cardTitle")
        lay.addWidget(lbl)
    return frame, lay


# ---------------------------------------------------------------------------
# 预览区：视频 + 可拖拽裁剪框 + 参考线
# ---------------------------------------------------------------------------
class _MpvHostWindow(QWindow):
    """mpv --wid 渲染目标（原生窗口）。把鼠标/滚轮事件转发给宿主 PreviewWidget。

    原生子窗口会在 Windows 命中测试里截获本应到达 PreviewWidget 的鼠标
    事件（Qt 合成层收不到），裁剪框拖拽/滚轮缩放全靠这里显式转发。坐标加
    上宿主容器偏移后重发，与原生 QWidget 版本的事件坐标一致。
    """

    def __init__(self, host: "_VideoWidget") -> None:
        super().__init__()
        self.host = host
        try:
            self.setMouseTracking(True)   # 悬停光标/手柄高亮需要无按键的 move
        except AttributeError:
            pass

    def _forward(self, ev, etype) -> None:
        pw = self.host.parent()
        if pw is None:
            return
        pos = ev.position() + QPointF(self.host.pos())
        mouse = QMouseEvent(etype, pos, ev.globalPosition(),
                            ev.button(), ev.buttons(), ev.modifiers())
        QApplication.sendEvent(pw, mouse)

    def mousePressEvent(self, e) -> None:
        self._forward(e, QEvent.Type.MouseButtonPress)

    def mouseMoveEvent(self, e) -> None:
        self._forward(e, QEvent.Type.MouseMove)

    def mouseReleaseEvent(self, e) -> None:
        self._forward(e, QEvent.Type.MouseButtonRelease)

    def mouseDoubleClickEvent(self, e) -> None:
        self._forward(e, QEvent.Type.MouseButtonDblClick)

    def wheelEvent(self, e) -> None:
        pw = self.host.parent()
        if pw is None:
            return
        pos = e.position() + QPointF(self.host.pos())
        wheel = QWheelEvent(pos, e.globalPosition(), e.pixelDelta(), e.angleDelta(),
                            e.buttons(), e.modifiers(), e.phase(), e.inverted())
        QApplication.sendEvent(pw, wheel)


class _VideoWidget(QWidget):
    """mpv --wid 渲染容器：普通 QWidget，mpv 直接渲染到本 widget 的原生窗口。

    worker 加载视频时把本 widget 的 winId() 传给 mpv（--wid 内嵌），mpv
    直接渲染到该原生窗口；叠在上面的裁剪框层是普通 QWidget，Qt 合成。

    说明：早期版本用 render API（MpvRenderContext → QOpenGLWidget FBO），
    实测 libmpv 在 Qt 的 GL 上下文里偶发纹理创建失败（INVALID_ENUM）导致
    花屏，且与呈现方式无关（blit/shader 对照均干净）。--wid 让 mpv 自己
    管理 GL 上下文渲染到原生窗口，规避该问题（实测画面正常）。
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)   # 鼠标穿透：裁剪框交互在 PreviewWidget
        # --wid 内嵌：必须用 QWindow（原生窗口，不经过 Qt 的 QWidget 合成）。
        # 实测 mpv 渲染到"Qt 合成树里的子 QWidget"时，Qt 的 backing store
        # 会盖住 mpv 的原生渲染（黑屏）；QWindow 直接是原生子窗口，mpv 的
        # GPU 内容由 DWM 合成显示，画面正常。
        self._win = _MpvHostWindow(self)
        self._win.setFlags(Qt.FramelessWindowHint)
        self._container = QWidget.createWindowContainer(self._win, self)
        self._player = None   # worker 的 MvpPlayer（loaded 后由 main_window 注入）

    def resizeEvent(self, _e) -> None:
        """QWindow 跟随容器尺寸（mpv 渲染目标尺寸）。"""
        self._container.setGeometry(self.rect())
        self._win.setGeometry(0, 0, self.width(), self.height())
        if not self._win.isVisible():
            self._win.show()

    def set_player(self, player) -> None:
        """worker 加载完成后注入 MvpPlayer（保持旧接口）。"""
        self._player = player

    def set_host_cursor(self, shape) -> None:
        """同步鼠标光标到 mpv 原生窗口（独立 HWND，不吃 PreviewWidget 的光标）。"""
        try:
            self._win.setCursor(QCursor(shape))
        except Exception:
            pass

    def native_handle(self) -> int:
        """mpv --wid 需要的原生窗口句柄（QWindow 的原生窗口）。"""
        if not self._win.isVisible():
            self._win.show()
        return int(self._win.winId())

def _win32_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """GetWindowRect 返回窗口屏幕矩形 (x, y, w, h)（物理像素）；失败返回 None。"""
    try:
        import ctypes
        from ctypes import wintypes
        ctypes.windll.user32.GetWindowRect.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
        ctypes.windll.user32.GetWindowRect.restype = ctypes.c_int
        r = wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r)):
            return None
        return r.left, r.top, r.right - r.left, r.bottom - r.top
    except Exception:
        return None


def _clip_child_window(hwnd: int, child_phys: tuple, clip_phys: tuple) -> None:
    """把子窗口的显示区域裁剪到指定矩形（SetWindowRgn，物理像素）。

    缩放时 mpv 窗口比预览视窗大，用它把画面约束在视窗内显示（图片查看器
    逻辑）；区域外的部分既不可见也不接收鼠标。
    """
    try:
        import ctypes
        # 必须声明 argtypes：HRGN/HWND 是 64 位句柄，不声明会被截断成 32 位
        ctypes.windll.user32.SetWindowRgn.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
        ctypes.windll.user32.SetWindowRgn.restype = ctypes.c_int
        ctypes.windll.gdi32.CreateRectRgn.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        ctypes.windll.gdi32.CreateRectRgn.restype = ctypes.c_void_p
        l = max(child_phys[0], clip_phys[0])
        t = max(child_phys[1], clip_phys[1])
        r = min(child_phys[0] + child_phys[2], clip_phys[0] + clip_phys[2])
        b = min(child_phys[1] + child_phys[3], clip_phys[1] + clip_phys[3])
        if r <= l or b <= t:
            region = ctypes.windll.gdi32.CreateRectRgn(0, 0, 0, 0)
        else:
            # 区域用子窗口相对坐标
            region = ctypes.windll.gdi32.CreateRectRgn(
                l - child_phys[0], t - child_phys[1],
                r - child_phys[0], b - child_phys[1])
        # SetWindowRgn 接管 region 的所有权，无需 DeleteObject
        ctypes.windll.user32.SetWindowRgn(ctypes.c_void_p(hwnd), region, True)
    except Exception:
        pass


class CropOverlay(QWidget):
    """视频上方的裁剪框浮层：独立顶层透明窗口（WS_EX_LAYERED）。

    --wid 下 mpv 的原生窗口盖住 Qt 合成内容，裁剪框作为普通子控件画不进
    视频区；本浮层以顶层窗口（WA_TranslucentBackground 分层 + 输入穿透）
    盖在预览的视频区域正上方，用 QPainter 画常规裁剪框（遮罩/框线/手柄/
    参考线/角标）。几何从 PreviewWidget 的视频区域同步；不参与交互
    （点击穿透到 mpv 原生窗口，拖拽仍由 PreviewWidget 处理）。
    """

    def __init__(self, preview: "PreviewWidget") -> None:
        super().__init__(preview.window(), Qt.Window | Qt.FramelessWindowHint
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self._preview = preview
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # 视频区 → 浮层本地的变换（sync_geometry 维护；浮层原点 = 视窗裁剪后的左上角）
        self._ox = 0.0
        self._oy = 0.0
        self._sx = 1.0
        self._sy = 1.0
        self._vr_w = 0.0
        self._vr_h = 0.0
        self.hide()

    # -- 几何同步 ---------------------------------------------------------
    def sync_geometry(self) -> None:
        """把浮层对准预览的视频显示区域，并裁剪到预览视窗内（全局坐标）。

        滚轮放大时视频矩形会超出预览视窗（图片查看器逻辑：放大内容固定在
        视窗内、超出部分裁剪、中键平移查看），浮层只覆盖视窗内的可见部分，
        裁剪框随可见区域裁剪。mpv 窗口超出的画面用 SetWindowRgn 裁剪，
        避免盖住工具栏/时间轴。
        """
        pv = self._preview
        if pv._reader is None:
            self.hide()
            return
        vr = pv._display_rect()
        if vr.isEmpty():
            self.hide()
            return
        dpr = self.screen().devicePixelRatio() if self.screen() else 1.0
        mpv_hwnd = int(pv._video_layer.native_handle())
        mpv_phys = _win32_rect(mpv_hwnd)
        if mpv_phys is None:
            # 非 Windows 或取不到：退回 Qt 坐标映射
            tl = pv.mapToGlobal(vr.topLeft().toPoint())
            vr_global = QRectF(tl, vr.size())
        else:
            # 视频实际显示位置 = mpv 原生窗口的屏幕矩形（物理→逻辑）。
            # 不用 Qt mapToGlobal：对强制原生窗口的控件会偏移
            vr_global = QRectF(mpv_phys[0] / dpr, mpv_phys[1] / dpr,
                               mpv_phys[2] / dpr, mpv_phys[3] / dpr)
        pv_global = QRectF(pv.mapToGlobal(QPoint(0, 0)), pv.size())
        visible = vr_global.intersected(pv_global)
        if visible.isEmpty():
            self.hide()
            return
        # 让裁切框贴到视频边缘时，手柄与框线也能完整画出、不被浮层边界裁掉：
        # 把浮层几何外扩一圈（仍限制在预览视窗内），视频矩形相应向内偏移。
        pad = int(self.HANDLE_CORNER) + 4
        exp = visible.adjusted(-pad, -pad, pad, pad).intersected(pv_global)
        if exp.isEmpty():
            exp = visible
        # 视频矩形在浮层本地坐标下的原点：视频可能向浮层左/上边缘之外
        # 越出（滚轮锚点缩放/平移时），此时原点为负。方向必须是
        # vr_global - visible：src=0（视频左缘）映射到视频的实际左缘。
        self._ox = vr_global.left() - exp.left()
        self._oy = vr_global.top() - exp.top()
        # 用 mpv 实际窗口矩形(取整后)作为视频映射基准，与画面边缘严格对齐，
        # 避免浮点 _display_rect 与整型视频窗口差 1px 造成:贴边拉不满、边缘露细缝
        self._vr_w = vr_global.width()
        self._vr_h = vr_global.height()
        self._sx = vr_global.width() / pv._reader.width
        self._sy = vr_global.height() / pv._reader.height
        geo = QRect(exp.topLeft().toPoint(), exp.size().toSize())
        if self.geometry() != geo:
            self.setGeometry(geo)
        if not self.isVisible():
            self.show()
        # 裁剪 mpv 原生窗口到预览视窗（物理像素，SetWindowRgn）
        if mpv_phys is not None:
            pv_phys = (int(pv_global.left() * dpr), int(pv_global.top() * dpr),
                       int(pv_global.width() * dpr), int(pv_global.height() * dpr))
            _clip_child_window(mpv_hwnd, mpv_phys, pv_phys)
        self.update()

    def _src_to_overlay(self, r: QRectF) -> QRectF:
        """源像素 → 浮层本地坐标（已含视窗裁剪偏移）。"""
        return QRectF(r.left() * self._sx + self._ox,
                      r.top() * self._sy + self._oy,
                      r.width() * self._sx, r.height() * self._sy)

    def _frame_local(self) -> QRectF:
        """完整视频矩形在浮层本地坐标（未裁剪时为 (0,0,W,H)）"""
        return QRectF(self._ox, self._oy, self._vr_w, self._vr_h)

    # -- 绘制（常规裁剪框） ------------------------------------------------
    HANDLE_CORNER = 12.0   # 角手柄边长（px，屏幕恒定）
    HANDLE_EDGE = 9.0      # 边中点手柄边长（px，屏幕恒定）

    def _handle_specs(self, sr: QRectF) -> list[tuple[float, float, float]]:
        return [
            (sr.left(), sr.top(), self.HANDLE_CORNER),
            (sr.right(), sr.top(), self.HANDLE_CORNER),
            (sr.left(), sr.bottom(), self.HANDLE_CORNER),
            (sr.right(), sr.bottom(), self.HANDLE_CORNER),
            (sr.center().x(), sr.top(), self.HANDLE_EDGE),
            (sr.center().x(), sr.bottom(), self.HANDLE_EDGE),
            (sr.left(), sr.center().y(), self.HANDLE_EDGE),
            (sr.right(), sr.center().y(), self.HANDLE_EDGE),
        ]

    def paintEvent(self, _e) -> None:
        pv = self._preview
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        crop = pv.crop_rect()
        fr = self._frame_local()
        if crop.isNull() or fr.width() <= 0 or fr.height() <= 0:
            p.end()
            return
        sr = self._src_to_overlay(crop)
        # 1) 遮罩：裁剪区外压暗（相对完整视频矩形；视窗外的部分被浮层自身裁掉）
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 110))
        for r in (QRectF(fr.left(), fr.top(), fr.width(), sr.top() - fr.top()),
                  QRectF(fr.left(), sr.bottom(), fr.width(), fr.bottom() - sr.bottom()),
                  QRectF(fr.left(), sr.top(), sr.left() - fr.left(), sr.height()),
                  QRectF(sr.right(), sr.top(), fr.right() - sr.right(), sr.height())):
            if r.width() > 0.5 and r.height() > 0.5:
                p.drawRect(r)
        # 2) 框线：accent 圆角矩形边框
        p.setPen(QPen(QColor(style.ACCENT), 2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(sr, 3, 3)
        # 3) 手柄：白色方块 + accent 描边
        for cx, cy, s in self._handle_specs(sr):
            hr = QRectF(cx - s / 2, cy - s / 2, s, s)
            p.setPen(QPen(QColor(style.ACCENT), 1.5))
            p.setBrush(QColor(style.TEXT))
            p.drawRect(hr)
        # 4) 参考线：警示色虚线
        p.setPen(QPen(QColor(style.DANGER), 1.5, Qt.DashLine))
        for orient, frac in pv._guides:
            if orient == "v":
                x = fr.left() + fr.width() * frac
                p.drawLine(QPointF(x, fr.top()), QPointF(x, fr.bottom()))
            else:
                y = fr.top() + fr.height() * frac
                p.drawLine(QPointF(fr.left(), y), QPointF(fr.right(), y))
        # 5) 拖拽中实时尺寸角标（保持尺寸缩放时显示锁定的导出尺寸）
        if pv._drag is not None:
            if pv._keep_size and pv._export_w > 0:
                badge = f"{pv._export_w}×{pv._export_h}"
            else:
                badge = f"{int(crop.width())}×{int(crop.height())}"
            f = QFont(self.font())
            f.setPointSize(9)
            bw, bh = p.fontMetrics().horizontalAdvance(badge) + 14, 22
            bx = min(sr.right() - bw, self.width() - bw)
            by = max(0.0, sr.top() - bh - 4)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 170))
            p.drawRoundedRect(QRectF(bx, by, bw, bh), 4, 4)
            p.setPen(QColor(style.TEXT))
            p.setFont(f)
            p.drawText(QRectF(bx, by, bw, bh), Qt.AlignCenter, badge)
        p.end()

class PreviewWidget(QWidget):
    """预览区：mpv 视频（--wid 原生窗口）+ 裁剪框浮层（CropOverlay）。

    视频只负责显示/滚轮缩放/中键平移，mpv 原生窗口盖住普通 Qt 内容；
    裁剪框画在独立顶层透明浮层上（CropOverlay），与视频区域严格对齐。
    裁剪交互（拖拽/手柄/参考线）仍在 PreviewWidget 处理 —— 鼠标事件由
    mpv 原生窗口显式转发过来。
    """
    crop_changed = Signal(int, int, int, int)   # x, y, w, h（源像素）
    frame_step = Signal(int)                    # +1 / -1 / ±10
    play_toggle = Signal()
    set_in = Signal()
    set_out = Signal()

    MIN_CROP = 2       # 源像素最小裁剪尺寸（与导出层一致）
    NEW_BOX_MIN = 8    # 新建框过小判为误触的阈值（源像素）
    GUIDE_HIT = 7      # 参考线命中半径（像素）
    VIDEO_MARGIN = 10  # 视频画面与控件边缘的边距（px）
    ZOOM_MAX = 8.0     # 滚轮预览最大放大倍率

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._reader = None
        self._crop = QRectF()          # 源像素坐标
        self._guides: list[tuple[str, float]] = []   # ('v'|'h', 0..1)
        self._lock_ratio = False
        self._keep_scale = False       # 保持宽高比（默认不勾选）：拖动手柄自由缩放
        self._keep_size = False        # 保持尺寸缩放：锁定面板 W/H
        self._export_w = 0             # 面板锁定的导出宽/高（保持尺寸缩放角标显示它）
        self._export_h = 0
        self._editing_size = False     # 手动输入宽/高时 True：面板保持输入值，不随框回写
        self._drag: tuple | None = None
        self._drag_box = QRectF()
        self._drag_start = QPointF()
        self._drag_ratio = 1.0
        self._zoom = 1.0               # 预览滚轮缩放倍率
        self._view_ox = 0.0            # 缩放后的平移偏移
        self._view_oy = 0.0
        self._pan_last = QPointF()
        # 注意：不能给预览控件强制 WA_NativeWindow —— 实测会让 mapToGlobal
        # 产生偏移（浮层错位跑偏），且 createWindowContainer 的容器原生父级
        # 始终是顶层窗口，靠原生父级裁剪预览边界也走不通。缩放时画面超出
        # 预览视窗的部分由 CropOverlay.sync_geometry 用 SetWindowRgn 裁剪。
        self._video_layer = _VideoWidget(self)
        self._video_layer.hide()   # 无视频时不显示（未渲染的原生窗口会露出白框）
        self._overlay = CropOverlay(self)
        self.setMinimumSize(480, 270)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        # 主窗口移动/缩放时浮层要跟着走（过滤器在 showEvent 里装到真正的顶层窗口）
        self._overlay.sync_geometry()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        win = self.window()
        if win is not None and win is not self:
            # __init__ 时 widget 尚未入树，window() 返回自身；到 show 时
            # 才是真正的顶层窗口。装错对象会导致窗口拖动时浮层不跟随。
            win.removeEventFilter(self)
            win.installEventFilter(self)
            self._overlay.sync_geometry()

    def eventFilter(self, obj, ev) -> bool:
        if obj is self.window() and ev.type() in (QEvent.Type.Move, QEvent.Type.Resize):
            self._overlay.sync_geometry()
        return False

    # -- 视频渲染层 --------------------------------------------------------
    def _fitted_rect(self) -> QRectF:
        """未缩放的居中 fitted 矩形（_video_rect 的基准）"""
        if not self._reader or not self._reader.width:
            return QRectF()
        w, h = self._reader.width, self._reader.height
        m = self.VIDEO_MARGIN
        vw, vh = max(1, self.width() - 2 * m), max(1, self.height() - 2 * m)
        scale = min(vw / w, vh / h)
        rw, rh = w * scale, h * scale
        return QRectF((self.width() - rw) / 2, (self.height() - rh) / 2, rw, rh)

    def _video_rect(self) -> QRectF:
        """视频画面矩形：基础 fitted 矩形 × 缩放倍率 + 平移偏移"""
        base = self._fitted_rect()
        if base.isEmpty():
            return base
        rw, rh = base.width() * self._zoom, base.height() * self._zoom
        return QRectF(base.left() + self._view_ox, base.top() + self._view_oy, rw, rh)

    def _display_rect(self) -> QRectF:
        """按视频显示宽高比（含 PAR）取整后的显示区域。

        mpv 窗口宽高比与视频显示比例一致时画面正好铺满、内部无黑边，
        裁剪框浮层才能与画面 1:1 对齐（SAR 视频如 720x406→16:9 时修正）。
        """
        vr = self._video_rect()
        if vr.isEmpty() or not self._reader or not self._reader.height:
            return vr
        pl = self._video_layer._player
        dar = getattr(pl, "dar", 0.0) or 0.0
        ar = dar if dar > 0 else (self._reader.width / self._reader.height)
        w, h = vr.width(), vr.height()
        h2 = w / ar
        if h2 <= h + 0.5:      # 以宽为准（不越界）
            h = h2
        else:                  # 以高为准
            w = h * ar
        # 真正取整：让视频窗口、裁切框映射、浮层共用同一整数矩形，
        # 否则浮点 display_rect 与取整后的 mpv 窗口差 1px，贴边时
        # 「尺寸拉不满、边缘露出细缝」。
        w, h = int(round(w)), int(round(h))
        cx, cy = vr.center().x(), vr.center().y()
        return QRectF(int(round(cx - w / 2)), int(round(cy - h / 2)), w, h)

    def _update_video_geometry(self) -> None:
        """让 mpv 渲染层覆盖视频显示区域（fitted，含缩放/平移/DAR 修正）。

        video_layer 只覆盖视频区域，视频外是 PreviewWidget 的 style.BG 背景。
        无视频时隐藏视频层：未渲染内容的原生窗口会露出白色方框。加载时
        native_handle() 会先 show() 再取句柄，隐藏期间句柄失效无影响。
        """
        vr = self._display_rect()
        if not vr.isEmpty():
            self._video_layer.setGeometry(vr.toRect())
            self._video_layer.show()
        else:
            self._video_layer.hide()
        self._overlay.sync_geometry()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._update_video_geometry()

    # -- 预览缩放/平移 -----------------------------------------------------
    def set_zoom(self, zoom: float) -> None:
        self._zoom = max(1.0, min(self.ZOOM_MAX, zoom))
        self._update_video_geometry()
        self.update()

    def reset_zoom(self) -> None:
        self._zoom = 1.0
        self._view_ox = 0.0
        self._view_oy = 0.0
        self._update_video_geometry()
        self.update()

    def wheelEvent(self, e) -> None:
        if self._drag or self._reader is None:
            return
        old_vr = self._video_rect()
        if old_vr.isEmpty():
            return
        pos = e.position()
        # 以鼠标位置为基点缩放：锚点处的内容保持在屏幕原位
        fx = (pos.x() - old_vr.left()) / old_vr.width()
        fy = (pos.y() - old_vr.top()) / old_vr.height()
        new_zoom = self._zoom * (1.1 if e.angleDelta().y() > 0 else 0.9)
        new_zoom = max(1.0, min(self.ZOOM_MAX, new_zoom))
        if new_zoom == self._zoom:
            return
        self._zoom = new_zoom
        new_base = self._fitted_rect()   # 无偏移的基础 fitted 矩形
        new_w = new_base.width() * self._zoom
        new_h = new_base.height() * self._zoom
        self._view_ox = pos.x() - fx * new_w - new_base.left()
        self._view_oy = pos.y() - fy * new_h - new_base.top()
        self._update_video_geometry()
        self.update()

    # -- 状态 -------------------------------------------------------------
    def set_reader(self, reader) -> None:
        self._reader = reader
        self._guides.clear()
        if reader is not None and reader.width and reader.height:
            self._crop = QRectF(0, 0, reader.width, reader.height)
        else:
            self._crop = QRectF()
        self._update_video_geometry()
        self.update()

    def set_image(self, img: QImage | None, smooth: bool = False) -> None:
        # 裁剪框浮层直接盖在 mpv 画面上，不再需要 CPU 帧画面（保留接口兼容）
        pass

    def set_export_size(self, w: int, h: int) -> None:
        self._export_w, self._export_h = int(w), int(h)
        self._overlay.update()

    def set_lock_ratio(self, on: bool) -> None:
        self._lock_ratio = on

    def set_keep_scale(self, on: bool) -> None:
        """保持宽高比：勾选后拖动手柄等比缩放（宽高比不变）"""
        self._keep_scale = on

    def set_keep_size(self, on: bool) -> None:
        """保持尺寸缩放：框贴合导出宽高比（面板 W/H 锁定）"""
        self._keep_size = on
        if on and self._export_w > 0 and self._export_h > 0 and self._reader and not self._crop.isNull():
            self.set_crop(self._crop)

    def set_crop(self, rect: QRectF) -> None:
        # 显示用户输入的原值（含奇数宽高）；yuv420p 的偶数化由导出层兜底
        r = self._clamp_crop(rect)
        if self._keep_size and self._export_w > 0 and self._export_h > 0 and self._reader:
            r = self._fit_aspect(r, self._export_w / self._export_h)
        # round 而非 int：坐标换算浮点尾差（479.9999）直接截断会少 1px
        self._crop = self._clamp_crop(QRectF(int(round(r.x())), int(round(r.y())),
                                             int(round(r.width())), int(round(r.height()))))
        self._overlay.update()   # 浮层实时跟随
        self._emit_crop()

    def set_crop_size(self, w: int, h: int) -> None:
        """按当前裁剪区中心点缩放（编辑 W/H 输入时调用）。

        裁剪尺寸可任意输入：超出画面时按输入宽高比等比缩小到能放进画面，
        保证不超出画面；不超出时按输入尺寸设定。
        """
        if not self._reader:
            return
        src_w, src_h = self._reader.width, self._reader.height
        w, h = max(self.MIN_CROP, w), max(self.MIN_CROP, h)
        # 超出画面 -> 等比缩小（保持输入比例），只缩小不放大
        scale = min(1.0, src_w / w, src_h / h)
        w, h = max(self.MIN_CROP, w * scale), max(self.MIN_CROP, h * scale)
        if self._crop.isNull():
            self._crop = QRectF(0, 0, src_w, src_h)
        cx, cy = self._crop.center().x(), self._crop.center().y()
        self._editing_size = True   # 手动输入：面板保持输入值，禁止 _on_crop_changed 回写
        try:
            self.set_crop(QRectF(cx - w / 2, cy - h / 2, w, h))
        finally:
            self._editing_size = False

    def crop_rect(self) -> QRectF:
        return self._crop

    def dragging(self) -> bool:
        return self._drag is not None

    def add_guide(self, orient: str) -> None:
        if not self._reader:
            return
        fracs = [1 / 3, 1 / 2, 2 / 3]
        same = [g for g in self._guides if g[0] == orient]
        idx = len(same) % len(fracs)
        self._guides.append((orient, fracs[idx]))
        self._overlay.update()

    def clear_guides(self) -> None:
        self._guides.clear()
        self._overlay.update()

    # -- 几何换算 ---------------------------------------------------------
    def _src_to_widget(self, r: QRectF) -> QRectF:
        vr = self._display_rect()
        if vr.isEmpty() or not self._reader.width:
            return QRectF()
        sx = vr.width() / self._reader.width
        sy = vr.height() / self._reader.height
        return QRectF(vr.left() + r.left() * sx, vr.top() + r.top() * sy,
                      r.width() * sx, r.height() * sy)

    def _widget_to_src(self, p: QPointF) -> QPointF:
        vr = self._display_rect()
        if vr.isEmpty() or not self._reader.width:
            return QPointF()
        return QPointF(
            (p.x() - vr.left()) / vr.width() * self._reader.width,
            (p.y() - vr.top()) / vr.height() * self._reader.height,
        )

    def _widget_to_src_rect(self, r: QRectF) -> QRectF:
        tl = self._widget_to_src(r.topLeft())
        br = self._widget_to_src(r.bottomRight())
        return QRectF(tl, br).normalized()

    def _clamp_crop(self, r: QRectF) -> QRectF:
        if not self._reader:
            return r
        src_w, src_h = float(self._reader.width), float(self._reader.height)
        w = max(self.MIN_CROP, min(r.width(), src_w))
        h = max(self.MIN_CROP, min(r.height(), src_h))
        x = max(0.0, min(r.x(), src_w - w))
        y = max(0.0, min(r.y(), src_h - h))
        return QRectF(x, y, w, h)

    def _fit_aspect(self, r: QRectF, ratio: float) -> QRectF:
        """把矩形调整为指定宽高比（保持中心与面积量级，不超出画面）"""
        if ratio <= 0 or not self._reader or r.width() <= 0 or r.height() <= 0:
            return r
        src_w, src_h = float(self._reader.width), float(self._reader.height)
        area = max(1.0, r.width() * r.height())
        w = (area * ratio) ** 0.5
        h = (area / ratio) ** 0.5
        if w > src_w:
            w = src_w
            h = w / ratio
        if h > src_h:
            h = src_h
            w = h * ratio
        w, h = max(self.MIN_CROP, w), max(self.MIN_CROP, h)
        cx, cy = r.center().x(), r.center().y()
        x = max(0.0, min(cx - w / 2, src_w - w))
        y = max(0.0, min(cy - h / 2, src_h - h))
        return QRectF(x, y, w, h)

    def _emit_crop(self) -> None:
        # 拖拽过程中不逐事件发信号（下游刷新 QSpinBox/信息面板是卡顿来源），
        # 松手后统一同步一次；拖拽中的实时反馈走画面上的尺寸角标
        if self._drag is not None:
            return
        r = self._crop
        if not r.isNull():
            self.crop_changed.emit(int(r.x()), int(r.y()), int(r.width()), int(r.height()))

    def _clamp_to_guides(self, b: QRectF, old: QRectF, mode: str = "move") -> QRectF:
        """参考线是裁切框的阻挡墙：裁切框不可越过参考线（move 保尺寸，resize 挡被拖边）"""
        if not self._guides:
            return b
        vr = self._display_rect()
        for orient, frac in self._guides:
            if orient == "v":
                gx = vr.left() + vr.width() * frac
                if mode == "move":
                    if old.right() <= gx and b.right() > gx:
                        b.moveRight(gx)
                    elif old.left() >= gx and b.left() < gx:
                        b.moveLeft(gx)
                else:
                    if b.left() < gx < b.right():
                        if old.center().x() < gx:
                            b.setRight(gx)
                        else:
                            b.setLeft(gx)
            else:
                gy = vr.top() + vr.height() * frac
                if mode == "move":
                    if old.bottom() <= gy and b.bottom() > gy:
                        b.moveBottom(gy)
                    elif old.top() >= gy and b.top() < gy:
                        b.moveTop(gy)
                else:
                    if b.top() < gy < b.bottom():
                        if old.center().y() < gy:
                            b.setBottom(gy)
                        else:
                            b.setTop(gy)
        return b

    # -- 鼠标交互 ---------------------------------------------------------
    def _hit_handle(self, pos: QPointF, sr: QRectF) -> str | None:
        hs = 20   # 命中半径：手柄骑在框角/边上，半径留足，拖动不用精确瞄准
        centers = {
            "tl": sr.topLeft(), "tr": sr.topRight(),
            "bl": sr.bottomLeft(), "br": sr.bottomRight(),
            "t": sr.topLeft() + QPointF(sr.width() / 2, 0),
            "b": sr.topLeft() + QPointF(sr.width() / 2, sr.height()),
            "l": sr.topLeft() + QPointF(0, sr.height() / 2),
            "r": sr.topLeft() + QPointF(sr.width(), sr.height() / 2),
        }
        for name, c in centers.items():
            if abs(pos.x() - c.x()) <= hs and abs(pos.y() - c.y()) <= hs:
                return name
        return None

    def _handle_center(self, handle: str, sr: QRectF) -> QPointF:
        """手柄中心点（位于框边/角上）。缩放拖拽的锚点用这个，而不是鼠标按下位置。"""
        return {
            "tl": sr.topLeft(), "tr": sr.topRight(),
            "bl": sr.bottomLeft(), "br": sr.bottomRight(),
            "t": sr.topLeft() + QPointF(sr.width() / 2, 0),
            "b": sr.topLeft() + QPointF(sr.width() / 2, sr.height()),
            "l": sr.topLeft() + QPointF(0, sr.height() / 2),
            "r": sr.topLeft() + QPointF(sr.width(), sr.height() / 2),
        }[handle]

    def _guide_line(self, i: int, vr: QRectF) -> tuple[str, float]:
        orient, frac = self._guides[i]
        pos = vr.left() + vr.width() * frac if orient == "v" else vr.top() + vr.height() * frac
        return orient, pos

    def _hit_guide(self, pos: QPointF, vr: QRectF) -> int | None:
        for i in range(len(self._guides)):
            orient, p = self._guide_line(i, vr)
            if orient == "v" and vr.top() <= pos.y() <= vr.bottom() and abs(pos.x() - p) <= self.GUIDE_HIT:
                return i
            if orient == "h" and vr.left() <= pos.x() <= vr.right() and abs(pos.y() - p) <= self.GUIDE_HIT:
                return i
        return None

    def _remove_guide_at(self, pos: QPointF) -> bool:
        i = self._hit_guide(pos, self._display_rect())
        if i is not None:
            self._guides.pop(i)
            self._overlay.update()
            return True
        return False

    def _set_cursor(self, shape) -> None:
        """同步光标到本控件与 mpv 原生窗口（两个独立 HWND 都要设）。"""
        self.setCursor(shape)
        self._video_layer.set_host_cursor(shape)

    def mousePressEvent(self, e) -> None:
        if self._reader is None:
            return
        self.setFocus()
        pos = e.position()
        if e.button() == Qt.MiddleButton:
            # 中键：平移缩放后的画面（滚轮缩放后查看细节），任意位置可按下
            self.grabMouse()
            self._drag = ("pan", None)
            self._pan_last = pos
            return
        vr = self._display_rect()
        sr = self._src_to_widget(self._crop)
        handle = self._hit_handle(pos, sr)
        # 锚点优先于区域检查：裁切框贴边时锚点中心在视窗边缘，鼠标在
        # 锚点边缘（视窗外 1-2px）按下也能拖拽
        if handle is None and not vr.contains(pos):
            return
        self.grabMouse()  # 拖动可能超出组件边界，抓取鼠标保证持续响应
        self._drag_start = pos
        guide = self._hit_guide(pos, vr)
        if handle:
            self._drag = ("resize", handle)
            self._drag_box = sr
            # 关键：缩放拖拽锚点用“手柄中心”（在框边上），而非鼠标按下位置。
            # 命中半径 20px 允许在框边外侧按下；若用按下位置做锚点，框边会滞后
            # 鼠标若干像素，拖到画面边缘时留出空隙（空气墙、缩放才闭合）。
            # 取框边交点后，框边严格跟随鼠标，拖到边缘即全幅。
            self._drag_start = self._handle_center(handle, sr)
            # 保持尺寸缩放：等比锁导出宽高比（而非框当前比例）
            if self._keep_size and self._export_w > 0 and self._export_h > 0:
                self._drag_ratio = self._export_w / self._export_h
            else:
                self._drag_ratio = sr.width() / sr.height() if sr.height() else 1.0
        elif e.button() == Qt.RightButton and guide is not None:
            self.releaseMouse()
            self._drag = None
            self._guides.pop(guide)
            self._overlay.update()
            return
        elif guide is not None:
            self._drag = ("guide", guide)
        elif sr.contains(pos):
            # 整幅画面时内部拖动无可移动空间，直接转为框选新区域
            if self._crop.width() >= self._reader.width - 1 and                     self._crop.height() >= self._reader.height - 1:
                self._drag = ("new", None)
            else:
                self._drag = ("move", None)
                self._drag_box = sr
        else:
            self._drag = ("new", None)  # 空白处拖拽直接新建裁剪框

    def mouseMoveEvent(self, e) -> None:
        if not self._drag:
            # 悬停光标（原生窗口是独立 HWND，本控件与 mpv 窗口都要同步）
            if self._reader:
                vr = self._display_rect()
                sr = self._src_to_widget(self._crop)
                h = self._hit_handle(e.position(), sr)
                g = self._hit_guide(e.position(), vr)
                if h in ("tl", "br"):
                    self._set_cursor(Qt.SizeFDiagCursor)
                elif h in ("tr", "bl"):
                    self._set_cursor(Qt.SizeBDiagCursor)
                elif h in ("t", "b"):
                    self._set_cursor(Qt.SizeVerCursor)
                elif h in ("l", "r"):
                    self._set_cursor(Qt.SizeHorCursor)
                elif g is not None:
                    self._set_cursor(Qt.SizeHorCursor if self._guides[g][0] == "v" else Qt.SizeVerCursor)
                elif sr.contains(e.position()):
                    self._set_cursor(Qt.SizeAllCursor)
                else:
                    self._set_cursor(Qt.CrossCursor)
            return
        pos = e.position()
        mode = self._drag[0]
        if mode == "pan":
            # 中键平移缩放后的画面（滚轮缩放后查看细节），不受视频矩形限制
            dx = pos.x() - self._pan_last.x()
            dy = pos.y() - self._pan_last.y()
            self._pan_last = pos
            self._view_ox += dx
            self._view_oy += dy
            self._update_video_geometry()
            return
        vr = self._display_rect()
        if mode == "guide":
            i = self._drag[1]
            if i >= len(self._guides):
                # 参考线已被删除（如双击删除/右键删除后残留的拖拽），作废
                self._drag = None
                return
            orient, _ = self._guides[i]
            if orient == "v":
                frac = (pos.x() - vr.left()) / vr.width()
            else:
                frac = (pos.y() - vr.top()) / vr.height()
            self._guides[i] = (orient, max(0.0, min(1.0, frac)))
            self._overlay.update()   # 参考线拖动实时跟随
            return
        if mode == "new":
            start_src = self._widget_to_src(self._drag_start)
            cur_src = self._widget_to_src(pos)
            rect = QRectF(start_src, cur_src).normalized()
            b = self._src_to_widget(rect)
            b = self._clamp_to_guides(b, QRectF(b.topLeft(), b.topLeft()), "new")
            self.set_crop(self._widget_to_src_rect(b))
            return
        # move / resize（widget 坐标运算；越界收进视频矩形内）
        if not vr.contains(pos) and mode != "move":
            pos = QPointF(min(max(pos.x(), vr.left()), vr.right()),
                         min(max(pos.y(), vr.top()), vr.bottom()))
        dx = pos.x() - self._drag_start.x()
        dy = pos.y() - self._drag_start.y()
        b = QRectF(self._drag_box)
        handle = self._drag[1]
        if mode == "move":
            b.translate(dx, dy)
        else:  # resize（四角 + 四边中点，等比锁宽高比/导出比例）
            locked = self._lock_ratio or self._keep_scale or self._keep_size
            if "r" in handle:
                b.setWidth(max(8, b.width() + dx))
            if "l" in handle:
                old_right = b.right()
                nl = min(b.x() + dx, old_right - 8)
                b.setX(nl)
                b.setWidth(max(8, old_right - nl))
            if "b" in handle:
                b.setHeight(max(8, b.height() + dy))
            if "t" in handle:
                old_bottom = b.bottom()
                nt = min(b.y() + dy, old_bottom - 8)
                b.setY(nt)
                b.setHeight(max(8, old_bottom - nt))
            if locked:
                # 等比缩放必须对边锚定：高度/宽度由拖动主导边决定
                if "r" in handle or "l" in handle:
                    new_h = b.width() / self._drag_ratio
                    if "t" in handle:
                        b.setY(b.bottom() - new_h)   # 下边固定
                    b.setHeight(new_h)
                else:
                    new_w = b.height() * self._drag_ratio
                    if "l" in handle:
                        b.setX(b.right() - new_w)    # 右边固定
                    b.setWidth(new_w)
        b = self._clamp_to_guides(b, QRectF(self._drag_box), mode)
        self.set_crop(self._widget_to_src_rect(b))

    def mouseReleaseEvent(self, _e) -> None:
        was_new = self._drag and self._drag[0] == "new"
        if was_new and self._crop.width() < self.NEW_BOX_MIN:
            # 过小的新框视为误操作，恢复全画面
            if self._reader:
                self._crop = QRectF(0, 0, self._reader.width, self._reader.height)
        self._drag = None
        self.releaseMouse()
        self._emit_crop()   # 拖拽结束，同步面板与信息
        self._overlay.update()   # 去掉拖拽角标，定格裁剪框

    def mouseDoubleClickEvent(self, e) -> None:
        if self._reader:
            # 双击参考线删除该条；双击其余位置恢复全画面
            if self._remove_guide_at(e.position()):
                return
            self.set_crop(QRectF(0, 0, self._reader.width, self._reader.height))

    # -- 绘制 -------------------------------------------------------------
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(style.BG))
        if self._reader is None:
            p.setPen(QColor(style.TEXT_MUTED))
            p.drawText(self.rect(), Qt.AlignCenter,
                       tr("导入文件夹后将在此预览视频\n"
                          "拖动鼠标即可框选裁剪区域"))
        p.end()

    # -- 键盘 -------------------------------------------------------------
    def keyPressEvent(self, e) -> None:
        k = e.key()
        if k == Qt.Key_Space:
            self.play_toggle.emit()
        elif k == Qt.Key_Left:
            self.frame_step.emit(-1 if not (e.modifiers() & Qt.ControlModifier) else -10)
        elif k == Qt.Key_Right:
            self.frame_step.emit(1 if not (e.modifiers() & Qt.ControlModifier) else 10)
        elif k == Qt.Key_Up:
            self.frame_step.emit(10)
        elif k == Qt.Key_Down:
            self.frame_step.emit(-10)
        elif k in (Qt.Key_I, Qt.Key_i):
            self.set_in.emit()
        elif k in (Qt.Key_O, Qt.Key_o):
            self.set_out.emit()
        else:
            super().keyPressEvent(e)

# 播放速度控件：横向拖动或滚轮调节 0.1x ~ 8x
# ---------------------------------------------------------------------------
class SpeedWidget(QWidget):
    """播放速度控件：名字「速度」+ 横向滑块 + 数值（对数刻度 0.1x~8.0x）。

    样式与音量条一致（名字 + 滑块 + 数值）；拖动/滚轮调节，双击复位 1.0x。
    """
    speed_changed = Signal(float)
    MIN_SPEED = 0.1
    MAX_SPEED = 8.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._speed = 1.0
        self._tmin = math.log10(self.MIN_SPEED)
        self._tmax = math.log10(self.MAX_SPEED)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.name_lbl = QLabel(tr("速度"))
        self.name_lbl.setObjectName("fieldLbl")
        lay.addWidget(self.name_lbl)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setFixedWidth(70)
        self.slider.setToolTip(tr("播放速度(0.1x~8.0x)：拖动/滚轮调节，双击复位 1.0x"))
        self.slider.valueChanged.connect(self._on_slider)
        lay.addWidget(self.slider)
        self.value_lbl = QLabel("1.00x")
        self.value_lbl.setObjectName("mono")
        self.value_lbl.setFixedWidth(46)
        lay.addWidget(self.value_lbl)
        self.setFixedHeight(26)
        self.setFixedWidth(self.sizeHint().width())
        self.slider.blockSignals(True)
        self.slider.setValue(self._to_slider(self._speed))
        self.slider.blockSignals(False)

    def _to_slider(self, s: float) -> int:
        s = max(self.MIN_SPEED, min(self.MAX_SPEED, s))
        t = math.log10(s)
        return round((t - self._tmin) / (self._tmax - self._tmin) * 100)

    def _to_speed(self, v: int) -> float:
        t = self._tmin + (v / 100.0) * (self._tmax - self._tmin)
        return 10 ** t

    def _on_slider(self, v: int) -> None:
        self.set_speed(self._to_speed(v))

    def speed(self) -> float:
        return self._speed

    def set_speed(self, s: float) -> None:
        self._speed = max(self.MIN_SPEED, min(self.MAX_SPEED, s))
        self.slider.blockSignals(True)
        self.slider.setValue(self._to_slider(self._speed))
        self.slider.blockSignals(False)
        self.value_lbl.setText(f"{self._speed:.2f}x")
        self.speed_changed.emit(self._speed)

    def mouseDoubleClickEvent(self, _e) -> None:
        self.set_speed(1.0)


# ---------------------------------------------------------------------------
# 时间轴：缩略图条带 + 片段选择 + 播放头
# ---------------------------------------------------------------------------
class TimelineWidget(QWidget):
    range_changed = Signal(float, float)    # in_sec, out_sec
    seek_requested = Signal(float, bool)   # 秒, 是否在拖拽中(连续 move)；否则用精确
    play_toggle = Signal()                  # 点击播放指针：播放/暂停

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._duration = 0.0
        self._min_span_sec = 0.0   # 最小裁切时长（绝对秒），open_file 注入为 1 帧
        self._in_pct = 0.0
        self._out_pct = 1.0
        self._play_pct = 0.0
        # 可见时间窗口（0..1 全时长比例）：缩放后只显示 [vis_start, vis_end]，
        # 长视频的裁切条不再被压缩成一条线。默认全时长。
        self._vis_start = 0.0
        self._vis_end = 1.0
        self._drag: str | None = None       # 'in' | 'out' | 'range' | 'play' | 'pan'
        self._drag_in = 0.0
        self._drag_out = 1.0
        self._press_x = 0.0                 # 按下位置，用于区分“点击跳转”和“拖动平移”
        self.setMinimumHeight(84)
        self.setMouseTracking(True)

    # -- 状态 -------------------------------------------------------------
    def set_duration(self, dur: float, min_span_sec: float | None = None) -> None:
        """设置总时长；min_span_sec 为最小裁切时长（绝对秒，通常 1 帧）。

        旧实现用"全时长比例"做下限（0.1%/0.5%），长视频下会放大成十几秒，
        改成帧级绝对秒数后，用户想裁多短就能裁多短。
        """
        self._duration = max(0.0, dur)
        if min_span_sec is not None:
            self._min_span_sec = max(0.0, min_span_sec)
        self._in_pct = 0.0
        self._out_pct = 1.0
        self._play_pct = 0.0
        self._vis_start = 0.0
        self._vis_end = 1.0
        self.update()

    def _min_span_pct(self) -> float:
        """最小裁切时长（秒）→ 全时长比例；默认 1 帧的绝对秒数"""
        if self._duration <= 0:
            return 0.0
        if self._min_span_sec > 0:
            return min(1.0, self._min_span_sec / self._duration)
        return min(1.0, (1.0 / 30.0) / self._duration)   # 兜底：无注入时按 30fps 一帧

    def set_range(self, in_sec: float, out_sec: float) -> None:
        if self._duration <= 0:
            return
        self._in_pct = max(0.0, min(1.0, in_sec / self._duration))
        self._out_pct = max(self._in_pct + self._min_span_pct(), min(1.0, out_sec / self._duration))
        self.update()

    def set_playhead(self, sec: float, follow: bool = False) -> None:
        """更新播放头；follow=True（播放中）时若指针跑出可见窗口则平移窗口跟随"""
        if self._duration <= 0:
            return
        self._play_pct = max(0.0, min(1.0, sec / self._duration))
        if follow and self._vis_end > self._vis_start:
            span = self._vis_end - self._vis_start
            if self._play_pct < self._vis_start:
                self._vis_start = self._play_pct
                self._vis_end = min(1.0, self._vis_start + span)
                self._vis_start = self._vis_end - span
            elif self._play_pct > self._vis_end:
                self._vis_end = self._play_pct
                self._vis_start = max(0.0, self._vis_end - span)
                self._vis_end = self._vis_start + span
        self.update()

    # -- 缩放 ------------------------------------------------------------
    MIN_VIS_SPAN = 0.005   # 最小可见跨度（0.5% 时长），防止过度放大

    def _vis_span(self) -> float:
        return max(self.MIN_VIS_SPAN, self._vis_end - self._vis_start)

    def zoom_by(self, factor: float, anchor_pct: float) -> None:
        """以 anchor_pct（0..1 全时长比例）为锚点缩放可见窗口。

        factor>1 放大、<1 缩小。锚点处的内容保持在同一屏幕位置。
        """
        if self._duration <= 0:
            return
        anchor_pct = max(0.0, min(1.0, anchor_pct))
        old_span = self._vis_span()
        new_span = max(self.MIN_VIS_SPAN, min(1.0, old_span / factor))
        # 锚点在窗口内的相对位置保持不变
        rel = 0.0 if old_span <= 0 else (anchor_pct - self._vis_start) / old_span
        new_start = anchor_pct - rel * new_span
        # 平移回 [0, 1] 范围
        if new_start < 0:
            new_start = 0.0
        elif new_start + new_span > 1:
            new_start = max(0.0, 1.0 - new_span)
        self._vis_start = new_start
        self._vis_end = new_start + new_span
        self.update()

    def reset_zoom(self) -> None:
        self._vis_start = 0.0
        self._vis_end = 1.0
        self.update()

    def fit_selection(self) -> None:
        """缩放窗口到刚好包住裁切选区 [in, out]"""
        if self._duration <= 0:
            return
        span = max(self.MIN_VIS_SPAN, self._out_pct - self._in_pct)
        if span >= 1.0:
            self.reset_zoom()
            return
        self._vis_start = max(0.0, min(1.0 - span, self._in_pct))
        self._vis_end = self._vis_start + span
        self.update()

    # -- 绘制（扁平化颜色条：纯色轨道 + 纯色选区 + 细手柄 + 细播放指针） ----
    STRIP_TOP = 6        # 颜色条距顶
    RULER_H = 18         # 底部时间刻度高度
    HANDLE_HIT = 14      # 手柄命中半径（像素，左右各算）
    STRIP_MARGIN = 24    # 颜色条左右留边：入/出点手柄拉到最两端时不会被界面裁掉
    HANDLE_W = 4         # 手柄宽度（两个手柄统一，改细便于精确定位）
    PLAY_HIT = 12        # 播放指针命中半径（放大三角后同步加大，方便点击拖动）

    def _strip_rect(self) -> QRectF:
        h = self.height()
        strip_h = max(24, h - self.STRIP_TOP - self.RULER_H - 10)
        return QRectF(self.STRIP_MARGIN, self.STRIP_TOP,
                      self.width() - self.STRIP_MARGIN * 2, strip_h)

    def _x_at(self, pct: float) -> float:
        """全时长比例 -> 像素（含左右留边），受可见窗口 [vis_start, vis_end] 影响"""
        sr = self._strip_rect()
        span = self._vis_span()
        return sr.left() + (pct - self._vis_start) / span * sr.width()

    def _pct_at_x(self, x: float) -> float:
        """像素 -> 全时长比例（含左右留边），受可见窗口 [vis_start, vis_end] 影响"""
        sr = self._strip_rect()
        span = self._vis_span()
        return max(0.0, min(1.0, self._vis_start + (x - sr.left()) / sr.width() * span))

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(style.BG))
        if self._duration <= 0:
            p.setPen(QColor(style.TEXT_MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, tr("时间轴"))
            p.end()
            return
        sr = self._strip_rect()
        in_x = self._x_at(self._in_pct)
        out_x = self._x_at(self._out_pct)

        # 1) 轨道：纯色圆角颜色条，选区之外压暗
        path = QPainterPath()
        path.addRoundedRect(sr, 6, 6)
        p.save()
        p.setClipPath(path)
        p.fillRect(sr, QColor(style.SURFACE_2))
        dim = QColor(6, 8, 12, 150)
        p.fillRect(QRectF(sr.left(), sr.top(), max(0.0, in_x - sr.left()), sr.height()), dim)
        p.fillRect(QRectF(out_x, sr.top(), sr.right() - out_x, sr.height()), dim)
        p.restore()

        # 2) 选区（裁切条主体）：纯色强调色，与轨道同圆角裁剪
        sel = QRectF(in_x, sr.top(), out_x - in_x, sr.height())
        if sel.width() > 2:
            p.save()
            p.setClipPath(path)
            p.fillRect(sel, QColor(style.ACCENT))
            p.restore()

        # 3) 入/出点手柄：统一白色圆条（贴边，可抓面积大），两端样式一致
        for x in (in_x, out_x):
            hb = QRectF(x - self.HANDLE_W / 2, sr.top() - 3, self.HANDLE_W, sr.height() + 6)
            p.setPen(QPen(QColor(0, 0, 0, 70), 1))
            p.setBrush(QColor(style.TEXT))
            p.drawRoundedRect(hb, 4, 4)

        # 4) 播放指针：细竖线 + 放大顶部三角（方便点击拖动），裁切条内的进度条
        px = self._x_at(self._play_pct)
        if sr.left() <= px <= sr.right():
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(style.TEXT))
            tri = QPolygonF([QPointF(px - 8, sr.top() - 9), QPointF(px + 8, sr.top() - 9),
                             QPointF(px, sr.top() + 6)])
            p.drawPolygon(tri)
            # 细竖线（0.5px 半透明，视觉更轻；点击识别区由 PLAY_HIT 保证不变）
            p.fillRect(QRectF(px - 0.25, sr.top() + 2, 0.5, sr.height() - 4), QColor(style.TEXT))

        # 5) 底部时间刻度：显示可见窗口内的绝对时间
        p.setPen(QColor(style.TEXT_MUTED))
        f = QFont(self.font())
        f.setPointSize(8)
        p.setFont(f)
        marks = 5
        for i in range(marks + 1):
            frac = self._vis_start + (self._vis_end - self._vis_start) * i / marks
            x = self._x_at(frac)
            p.drawLine(QPointF(x, h - self.RULER_H + 2), QPointF(x, h - self.RULER_H + 6))
            p.drawText(QRectF(x - 40, h - self.RULER_H + 6, 80, 12), Qt.AlignCenter,
                       fmt_time(frac * self._duration, False))
        p.end()

    # -- 交互：手柄命中区扩大到 ±14px，选区中部拖动整体平移，其余位置拖动即刮擦 --
    def _hit_handle(self, x: float) -> str | None:
        in_x, out_x = self._x_at(self._in_pct), self._x_at(self._out_pct)
        if abs(x - in_x) <= self.HANDLE_HIT:
            return "in"
        if abs(x - out_x) <= self.HANDLE_HIT:
            return "out"
        return None

    def mousePressEvent(self, e) -> None:
        if self._duration <= 0:
            return
        # 中键：平移可见窗口（pan），不参与左键的手柄/刮擦逻辑
        if e.button() == Qt.MiddleButton:
            self._drag = "pan"
            self._pan_start_x = e.position().x()
            self._pan_vis_start = self._vis_start
            return
        x = e.position().x()
        self._drag_start_x = x
        self._press_x = x
        self._drag_in, self._drag_out = self._in_pct, self._out_pct
        hit = self._hit_handle(x)
        if hit:
            # 入点/出点重叠时命中会先返回 in；但重叠时往右拖用户想分出点、往左想分入点。
            # 置为 pending，交给首个 move 的方向决定，避免"重叠时往右拖没反应"。
            in_x, out_x = self._x_at(self._in_pct), self._x_at(self._out_pct)
            overlapping = abs(in_x - out_x) <= self.HANDLE_HIT
            self._drag = "pending" if (hit == "in" and overlapping) else hit
            # 按下即指向该手柄位置，方便精确对齐画面
            self.seek_requested.emit(self._pct_at_x(x) * self._duration, False)
            return
        # 点击播放指针（且指针在选区内）：播放/暂停 —— 指针即进度条
        px = self._x_at(self._play_pct)
        in_x, out_x = self._x_at(self._in_pct), self._x_at(self._out_pct)
        if in_x < px < out_x and abs(x - px) <= self.PLAY_HIT and \
                e.position().y() <= self._strip_rect().bottom() + 6:
            self.play_toggle.emit()
            return
        if in_x <= x <= out_x:
            # 手柄之间的任意位置：按下即拖动整个裁切条平移
            self._drag = "range"
        else:
            self._drag = "play"
            pct = self._pct_at_x(x)
            self.seek_requested.emit(pct * self._duration, False)

    def mouseMoveEvent(self, e) -> None:
        if self._duration <= 0:
            return
        if not self._drag:
            # 悬停光标反馈（悬停预览缩略图已移除：拖动即可预览）
            hit = self._hit_handle(e.position().x())
            self.setCursor(Qt.SizeHorCursor if hit else Qt.PointingHandCursor)
            return
        if self._drag == "pending":
            # 重叠手柄：首个明显位移方向决定分出点(右)还是分入点(左)
            if abs(e.position().x() - self._drag_start_x) >= 3:
                self._drag = "out" if e.position().x() > self._drag_start_x else "in"
            else:
                return
        pct = self._pct_at_x(e.position().x())
        if self._drag == "pan":
            # 中键平移可见窗口：像素位移 × 窗口跨度 / 条带宽 = 比例位移
            sr = self._strip_rect()
            dx = (e.position().x() - self._pan_start_x) / sr.width() * self._vis_span()
            span = self._vis_span()
            self._vis_start = max(0.0, min(1.0 - span, self._pan_vis_start - dx))
            self._vis_end = self._vis_start + span
        elif self._drag == "in":
            self._in_pct = min(pct, self._out_pct - self._min_span_pct())
            self.range_changed.emit(self._in_pct * self._duration, self._out_pct * self._duration)
            self.seek_requested.emit(pct * self._duration, True)   # 拖动手柄实时指向预览帧
        elif self._drag == "out":
            self._out_pct = max(pct, self._in_pct + self._min_span_pct())
            self.range_changed.emit(self._in_pct * self._duration, self._out_pct * self._duration)
            self.seek_requested.emit(pct * self._duration, True)
        elif self._drag == "range":
            sr = self._strip_rect()
            # 像素位移 → 全时长比例位移：乘可见窗口跨度（缩放后拖 1px 对应更小的时间段）
            dx = (e.position().x() - self._drag_start_x) / sr.width() * self._vis_span()
            self._in_pct = self._drag_in + dx
            self._out_pct = self._drag_out + dx
            if self._in_pct < 0:
                self._in_pct = 0
                self._out_pct = self._drag_out - self._drag_in
            elif self._out_pct > 1:
                self._out_pct = 1
                self._in_pct = 1 - (self._drag_out - self._drag_in)
            self.range_changed.emit(self._in_pct * self._duration, self._out_pct * self._duration)
            self.seek_requested.emit(self._in_pct * self._duration, True)   # 整块拖动：预览随入点更新
        elif self._drag == "play":
            self.seek_requested.emit(pct * self._duration, True)
        self.update()

    def mouseReleaseEvent(self, e) -> None:
        if self._drag == "range" and abs(e.position().x() - self._press_x) < 4:
            # 选中区内部单击（未拖动）：跳转预览到点击位置；拖动则平移整条
            self.seek_requested.emit(self._pct_at_x(self._press_x) * self._duration, False)
        self._drag = None

    # -- 缩放交互：滚轮以指针为锚缩放，双击重置，右键适配选区/重置 ----------
    def wheelEvent(self, e) -> None:
        if self._duration <= 0:
            return
        anchor = self._pct_at_x(e.position().x())
        factor = 1.25 if e.angleDelta().y() > 0 else 0.8
        self.zoom_by(factor, anchor)

    def mouseDoubleClickEvent(self, _e) -> None:
        # 双击空白区域：重置缩放（放大后找回全局视图）
        self.reset_zoom()

    def contextMenuEvent(self, e) -> None:
        if self._duration <= 0:
            return
        menu = QMenu(self)
        act_fit = menu.addAction(tr("定位选区"))
        act_reset = menu.addAction(tr("重置缩放"))
        chosen = menu.exec(e.globalPos())
        if chosen == act_fit:
            self.fit_selection()
        elif chosen == act_reset:
            self.reset_zoom()


# ---------------------------------------------------------------------------
# 参数设置面板
# ---------------------------------------------------------------------------
class LeftPanel(QWidget):
    sizes_changed = Signal(int, int)   # 代码层改宽高（set_sizes 会 block 掉 spinbox 信号）

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sidePanel")
        # 自定义 QWidget 子类必须加 WA_StyledBackground，QSS 才会完整绘制
        # 卡片背景+边框（否则只有背景、边框不显示）
        self.setAttribute(Qt.WA_StyledBackground, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(8)

        # --- 裁剪尺寸 ---
        card, c_lay = _card(tr("裁剪尺寸"))
        row = QHBoxLayout()
        row.setSpacing(5)
        self.cw = QSpinBox()
        self.cw.setRange(2, 99999)
        self.cw.setValue(1920)
        self.cw.setMaximumWidth(72)
        self.ch = QSpinBox()
        self.ch.setRange(2, 99999)
        self.ch.setValue(1080)
        self.ch.setMaximumWidth(72)
        w_lbl = QLabel(tr("宽"))
        h_lbl = QLabel(tr("高"))
        for lbl in (w_lbl, h_lbl):
            lbl.setObjectName("fieldLbl")
        row.addWidget(w_lbl)
        row.addWidget(self.cw)
        row.addWidget(h_lbl)
        row.addWidget(self.ch)
        # 交换宽高：放到宽/高数值设置行的末尾
        self.swap_btn = QToolButton()
        self.swap_btn.setText("⇄")
        self.swap_btn.setToolTip(tr("交换宽高（面板数值互换，裁切框等比适应画面）"))
        row.addWidget(self.swap_btn)
        row.addStretch(1)
        c_lay.addLayout(row)
        # 保持宽高比 / 保持尺寸缩放（移到裁剪尺寸卡片；锁定宽高比按钮已移除，功能重复）
        self.keep_ratio_chk = QCheckBox(tr("保持宽高比"))
        self.keep_ratio_chk.setChecked(False)
        self.keep_ratio_chk.setToolTip(
            tr("勾选：拖动手柄等比缩放（宽高比不变），导出按框的实际尺寸；\n"
               "取消：拖动可自由拉伸"))
        c_lay.addWidget(self.keep_ratio_chk)
        self.keep_size_chk = QCheckBox(tr("保持尺寸缩放"))
        self.keep_size_chk.setToolTip(
            tr("勾选：拖动手柄等比缩放调整构图，但导出分辨率锁定为上方宽高设定值\n"
               "（框内内容缩放输出，可能放大变糊）"))
        c_lay.addWidget(self.keep_size_chk)
        # 兼容旧属性名（外部/测试引用）
        self.keep_scale_chk = self.keep_ratio_chk
        lay.addWidget(card)

        # --- 预设尺寸 ---
        card, p_lay = _card(tr("预设尺寸"))
        self.preset_grid = QGridLayout()
        self.preset_grid.setSpacing(5)
        p_lay.addLayout(self.preset_grid)
        add_row = QHBoxLayout()
        self.add_preset_btn = QToolButton()
        self.add_preset_btn.setText("＋")
        self.add_preset_btn.setToolTip(tr("将当前尺寸存为预设"))
        add_row.addWidget(self.add_preset_btn)
        add_row.addStretch(1)
        p_lay.addLayout(add_row)
        lay.addWidget(card)

        # --- 帧率 ---
        card, f_lay = _card(tr("帧率"))
        frow = QHBoxLayout()
        frow.setContentsMargins(0, 0, 0, 0)
        self.fps = QDoubleSpinBox()
        self.fps.setRange(0.5, 240.0)
        self.fps.setDecimals(2)
        self.fps.setValue(30.0)
        self.fps.setSuffix(" fps")
        # 撑满卡片宽度，与下方预设按钮行左右边缘对齐
        self.fps.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.fps.setMinimumHeight(22)
        frow.addWidget(self.fps)
        f_lay.addLayout(frow)
        chips = QGridLayout()
        chips.setSpacing(4)
        # (key, label)：key 用于逻辑判断，label 用于显示（语言切换后原帧率仍稳定识别）
        for i, (key, label) in enumerate((("orig", tr("原帧率")), ("24", "24"), ("25", "25"),
                                          ("30", "30"), ("50", "50"), ("60", "60"))):
            b = QPushButton(label)
            b.setFlat(False)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.setMinimumHeight(22)   # 与 fps 输入框同高，视觉一致
            b.clicked.connect(lambda _=False, k=key: self._fps_chip(k))
            chips.addWidget(b, i // 3, i % 3)
        for c in range(3):
            chips.setColumnStretch(c, 1)   # 三列等宽，按钮行与输入框对齐
        f_lay.addLayout(chips)
        lay.addWidget(card)

        # --- 导出时长 ---
        card, d_lay = _card(tr("导出时长"))
        drow = QHBoxLayout()
        self.duration = QDoubleSpinBox()
        self.duration.setRange(0.1, 86400.0)
        self.duration.setDecimals(1)
        self.duration.setValue(30.0)
        self.duration.setSuffix(" s")
        self.duration.setMaximumWidth(112)
        drow.addWidget(self.duration, 1)
        d_lay.addLayout(drow)
        lay.addWidget(card)

        # --- 裁切信息 ---
        card, i_lay = _card(tr("裁切信息"))
        self.info = QLabel(tr("尚未载入视频"))
        self.info.setObjectName("mono")
        self.info.setWordWrap(True)
        self.info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)  # 允许缩窄换行
        i_lay.addWidget(self.info)
        lay.addWidget(card)

        # --- 导出选项 ---
        card, e_lay = _card(tr("导出选项"))
        # 导出内容：视频/图片/声音改为独立勾选，可全不选（单纯导出声音等）
        self.chk_video = QCheckBox(tr("导出视频"))
        self.chk_video.setToolTip(tr("导出 mp4 · H264 视频片段"))
        self.chk_image = QCheckBox(tr("导出图片"))
        self.chk_image.setToolTip(tr("导出图片（jpg/png）"))
        self.chk_video.setChecked(True)   # 默认导出视频
        e_lay.addWidget(self.chk_video)
        e_lay.addWidget(self.chk_image)
        frow = QHBoxLayout()
        frow.setSpacing(5)
        self.img_format = QComboBox()
        self.img_format.addItems(["PNG", "JPG"])
        self.img_format.setMinimumWidth(64)
        self.img_format.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        frow.addWidget(self.img_format)
        self.jpg_q_label = QLabel(tr("质量"))
        frow.addWidget(self.jpg_q_label)
        self.jpg_q = QSpinBox()
        self.jpg_q.setRange(1, 100)
        self.jpg_q.setValue(90)
        self.jpg_q.setSuffix("%")
        self.jpg_q.setMaximumWidth(60)
        self.jpg_q.setToolTip(tr("JPG 压缩质量（仅 JPG 格式生效；PNG 无损无需设置）"))
        frow.addWidget(self.jpg_q)
        frow.addStretch(1)
        e_lay.addLayout(frow)
        # 音轨选择：预览(mpv) + 导出(ffmpeg -map) 同步切换
        trow = QHBoxLayout()
        trow.setSpacing(5)
        self.audio_track_lbl = QLabel(tr("音轨"))
        self.audio_track_combo = QComboBox()
        self.audio_track_combo.setMinimumWidth(150)
        self.audio_track_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.audio_track_combo.setEnabled(False)   # 载入含音轨的视频后启用
        self.audio_track_combo.setToolTip(tr("选择要预览/导出的音轨"))
        trow.addWidget(self.audio_track_lbl)
        trow.addWidget(self.audio_track_combo, 1)
        e_lay.addLayout(trow)
        self.audio_only_chk = QCheckBox(tr("单独导出声音 (mp3)"))
        self.audio_only_chk.setToolTip(tr("同时导出选区内的声音为 mp3"))
        e_lay.addWidget(self.audio_only_chk)
        # 导出音频增益：独立小控件，只放大导出的声音(0~600%)，不影响预览音量
        grow = QHBoxLayout()
        grow.setSpacing(5)
        self.audio_gain_lbl = QLabel(tr("导出音频增益"))
        self.audio_gain_spin = QSpinBox()
        self.audio_gain_spin.setRange(0, 600)
        self.audio_gain_spin.setValue(100)
        self.audio_gain_spin.setSuffix("%")
        self.audio_gain_spin.setMaximumWidth(70)
        self.audio_gain_spin.setToolTip(tr("导出声音的音量增益：100%=原音量，最大可放大到 600%"))
        grow.addWidget(self.audio_gain_lbl)
        grow.addWidget(self.audio_gain_spin)
        grow.addStretch(1)
        e_lay.addLayout(grow)
        self.keep_audio_chk = QCheckBox(tr("视频保留音频"))
        self.keep_audio_chk.setChecked(True)   # 默认保留原音轨
        self.keep_audio_chk.setToolTip(tr("导出视频片段时是否保留原音轨"))
        e_lay.addWidget(self.keep_audio_chk)
        self.hw_chk = QCheckBox(tr("硬件加速（NVENC）"))
        e_lay.addWidget(self.hw_chk)
        self.long_guard_chk = QCheckBox(tr("超过 5 秒禁止导出"))
        self.long_guard_chk.setChecked(True)
        self.long_guard_chk.setToolTip(
            tr("防止误操作（默认开启）：视频片段选区时长超过 5 秒时阻止导出，\n"
               "避免误点导出长片段白等半天。确需导出长片段时取消勾选即可。"))
        e_lay.addWidget(self.long_guard_chk)
        lay.addWidget(card)

        # --- 操作提示 ---
        card, h_lay = _card(tr("操作提示"))
        hint = QLabel(
            tr("拖动框内 · 移动裁剪区\n"
               "拖动边角 · 调整大小\n"
               "空白处拖拽 · 新建裁剪框\n"
               "参考线 · 阻挡裁切框\n"
               "双击画面 · 恢复全画面\n"
               "空格 · 播放/暂停\n"
               "←/→ · 逐帧移动（Ctrl±10帧）\n"
               "I / O · 设置入点/出点\n"
               "滚轮 · 缩放预览 · 中键 · 平移")
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        h_lay.addWidget(hint)
        lay.addWidget(card)

        lay.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        # 导出联动：仅图片相关时启用格式/质量，JPG 才启用质量
        self.chk_video.toggled.connect(self._on_export_kind_changed)
        self.chk_image.toggled.connect(self._on_export_kind_changed)
        self.img_format.currentIndexChanged.connect(self._on_export_kind_changed)
        self._on_export_kind_changed()

        # 固定宽度按内容自适应：宽度取滚动区内容的最小宽度+边距，否则内容比面板
        # 宽会被裁掉（右侧被遮挡）；固定后拖分割器也不会把面板压缩到内容以下。
        self.setFixedWidth(self.sizeHint().width() + 8)

    def wants_video(self) -> bool:
        return self.chk_video.isChecked()

    def wants_image(self) -> bool:
        return self.chk_image.isChecked()

    def wants_audio(self) -> bool:
        return self.audio_only_chk.isChecked()

    def has_export_selection(self) -> bool:
        return self.wants_video() or self.wants_image() or self.wants_audio()

    def image_format(self) -> str:
        return "jpg" if self.img_format.currentText() == "JPG" else "png"

    def _on_export_kind_changed(self) -> None:
        img = self.wants_image()
        self.img_format.setEnabled(img)
        is_jpg = img and self.img_format.currentText() == "JPG"
        # PNG 无损：质量选择直接隐藏（而非禁用）；仅 JPG 显示
        self.jpg_q_label.setVisible(is_jpg)
        self.jpg_q.setVisible(is_jpg)
        self.jpg_q.setEnabled(is_jpg)

    def _fps_chip(self, key: str) -> None:
        if key == "orig":
            self.fps_chip_requested.emit()
        else:
            self.fps.setValue(float(key))

    fps_chip_requested = Signal()

    def set_info(self, lines: list[str]) -> None:
        self.info.setText("\n".join(lines))

    def set_sizes(self, w: int, h: int) -> None:
        """外部同步（裁剪框变化时）—— 避免信号回环"""
        self.cw.blockSignals(True)
        self.ch.blockSignals(True)
        self.cw.setValue(w)
        self.ch.setValue(h)
        self.cw.blockSignals(False)
        self.ch.blockSignals(False)
        # spinbox 信号被 block，导出尺寸（预览角标/保持尺寸缩放的宽高比）不会跟随，
        # 这里显式通知主窗口同步
        self.sizes_changed.emit(w, h)
