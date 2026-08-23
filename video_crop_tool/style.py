"""深色主题 QSS —— 统一设计令牌 + Apple 专业剪辑工具视觉语言。

设计原则：
- 近黑分层背景（页面最深，逐层微亮），层级靠亮度差不靠边框堆砌
- 系统蓝 accent，主操作按钮实心、次级按钮描边，禁用花哨装饰
- 大圆角（8-10px）、充足内边距、克制的 hover 反馈
- 所有间距/圆角/高度/字号集中在下方"设计令牌"区，改视觉风格只动这里
- 性能注意：不使用 `*` 通配选择器、不设置全局透明背景（避免强制软件合成）
"""

# ---- 调色板：Apple dark 档位 ----
BG = "#0e0f13"            # 页面背景（近黑）
SURFACE_1 = "#16181e"     # 卡片/工作区
SURFACE_2 = "#1e2129"     # 按钮/面板
SURFACE_3 = "#262a34"     # 输入框
HOVER = "#2c313d"
BORDER = "#2a2e38"
BORDER_STRONG = "#333845"
TEXT = "#f0f2f7"
TEXT_SECONDARY = "#a8aebb"
TEXT_MUTED = "#6a707c"
ACCENT = "#0a84ff"        # Apple 系统蓝
ACCENT_BG = "rgba(10,132,255,0.16)"
ACCENT_HOVER = "#3d9dff"
SUCCESS = "#30d158"       # Apple 系统绿（主按钮用）
DANGER = "#ff453a"
DANGER_BG = "rgba(255,69,58,0.14)"
NEW_ITEM_BG = "#550A84FF"   # 新导入视频高亮底色（带alpha，Qt QColor 认 #AARRGGBB）
NEW_ITEM_BORDER = "#0a84ff" # 新导入视频高亮描边（accent，由 delegate 绘制，不受 item QSS 影响）
MONO_FONT = "Consolas"

# ---- 设计令牌：间距 / 圆角 / 控件高度 / 字号 ----
SPACING = "8px"               # 布局默认间距
RADIUS_L = "10px"             # 面板/卡片圆角
RADIUS_M = "8px"              # 按钮/列表项圆角
RADIUS_S = "6px"              # 输入控件圆角
CTRL_H = "20px"               # 按钮基准高度
INPUT_H = "18px"              # 输入控件基准高度
FONT_BASE = "11px"            # 全局控件字号
FONT_SMALL = "10px"           # 紧凑区（参数设置栏）字号
PAD_BTN = "6px 12px"          # 按钮内边距
PAD_INPUT = "3px 14px 3px 7px"  # 输入控件内边距（右侧留给步进/下拉箭头）


def _write_check_icon(path: str, color: str = "#0a84ff") -> None:
    """生成对勾 PNG 供 QSS 引用（QCheckBox 勾选态图标，默认 accent 色）"""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
    pm = QPixmap(30, 30)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(Qt.NoBrush)
    pen = QPen(QColor(color), 4)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.drawLine(QRectF(7, 7, 16, 16).topLeft() + QPointF(0, 8), QPointF(13, 22))
    p.drawLine(QPointF(13, 22), QPointF(24, 8))
    p.end()
    pm.save(path, "PNG")


def _write_chevron_icon(path: str, up: bool) -> None:
    """QSpinBox 上下调节按钮的 V 形箭头图标（比默认方块三角更精致）"""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
    pm = QPixmap(18, 18)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(TEXT_SECONDARY), 2)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    r = QRectF(4.5, 4.5, 9, 9)
    if up:
        p.drawLine(QPointF(r.left(), r.bottom() - 2), QPointF(r.center().x(), r.top() + 2))
        p.drawLine(QPointF(r.center().x(), r.top() + 2), QPointF(r.right(), r.bottom() - 2))
    else:
        p.drawLine(QPointF(r.left(), r.top() + 2), QPointF(r.center().x(), r.bottom() - 2))
        p.drawLine(QPointF(r.center().x(), r.bottom() - 2), QPointF(r.right(), r.top() + 2))
    p.end()
    pm.save(path, "PNG")


def view_icon(mode: str) -> "QIcon":
    """文件列表视图拟物 SVG 图标：name=列表(蓝) / large=大图册(紫)。

    拟物风格：线性渐变卡面（上浅下深）+ 偏移半透明圆角阴影（QtSvg 不支持
    filter，用叠层近似）+ 顶部高光条，两种模式沿用蓝/紫主题色。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon, QPainter, QPixmap
    from PySide6.QtSvg import QSvgRenderer

    # 每张"卡"的渐变（id 需全局唯一，用 mode 区分）
    def card(x, y, w, h, gid, c1, c2):
        shadow = f'<rect x="{x + 0.6}" y="{y + 1.4}" width="{w}" height="{h}" rx="2.5" fill="#000000" opacity="0.28"/>'
        body = (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="2.5" '
                f'fill="url(#{gid})" stroke="#ffffff" stroke-opacity="0.25" stroke-width="0.5"/>')
        gloss = (f'<rect x="{x + 1}" y="{y + 1}" width="{w - 2}" height="{max(1, h * 0.32)}" '
                 f'rx="1.5" fill="#ffffff" opacity="0.30"/>')
        return shadow + body + gloss

    if mode == "name":
        c1, c2 = "#6db8ff", "#1f7ff0"
        cards = [(3, 3.2, 14, 3.4), (3, 8.3, 14, 3.4), (3, 13.4, 14, 3.4)]
    else:  # large：2×2
        c1, c2 = "#c9b6ff", "#7c5cf0"
        cards = [(3, 3, 6, 6), (11, 3, 6, 6), (3, 11, 6, 6), (11, 11, 6, 6)]
    body = "".join(card(x, y, w, h, f"g{mode}_{i}", c1, c2) for i, (x, y, w, h) in enumerate(cards))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20">'
           f'<defs>'
           f'<linearGradient id="g{mode}_0" x1="0" y1="0" x2="0" y2="1">'
           f'<stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient>'
           f'<linearGradient id="g{mode}_1" x1="0" y1="0" x2="0" y2="1">'
           f'<stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient>'
           f'<linearGradient id="g{mode}_2" x1="0" y1="0" x2="0" y2="1">'
           f'<stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient>'
           f'<linearGradient id="g{mode}_3" x1="0" y1="0" x2="0" y2="1">'
           f'<stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient>'
           f'</defs>{body}</svg>')
    renderer = QSvgRenderer(bytes(svg, encoding="utf-8"))
    pm = QPixmap(20, 20)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    renderer.render(p)
    p.end()
    return QIcon(pm)


def step_icon(direction: str, color: str = "#f0f2f7") -> "QIcon":
    """帧步进 SVG 图标：prev=|◀（竖线+左三角），next=▶|（右三角+竖线）。

    替代 ⏮/⏭ emoji 字形（跨平台字体不一致会显示成色块），矢量绘制。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon, QPainter, QPixmap
    from PySide6.QtSvg import QSvgRenderer

    if direction == "prev":
        body = (f'<rect x="3.2" y="5" width="2.6" height="10" rx="1.2" fill="{color}"/>'
                f'<path d="M 6.8 10 L 17 4.5 L 17 15.5 Z" fill="{color}"/>')
    else:
        body = (f'<path d="M 3 4.5 L 13.2 10 L 3 15.5 Z" fill="{color}"/>'
                f'<rect x="14.2" y="5" width="2.6" height="10" rx="1.2" fill="{color}"/>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" '
           f'viewBox="0 0 20 20">{body}</svg>')
    renderer = QSvgRenderer(bytes(svg, encoding="utf-8"))
    pm = QPixmap(20, 20)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    renderer.render(p)
    p.end()
    return QIcon(pm)


def build_qss(check_icon: str | None = None,
              spin_up_icon: str | None = None,
              spin_down_icon: str | None = None) -> str:
    """组装 QSS。图标为运行时生成的 PNG 路径（需在 QApplication 之后调用）"""
    check_img = f"image: url({check_icon});" if check_icon else ""
    up_img = f"image: url({spin_up_icon});" if spin_up_icon else ""
    down_img = f"image: url({spin_down_icon});" if spin_down_icon else ""
    return f"""
/* ================= 容器与面板 ================= */
QMainWindow, QDialog, QWidget#mainWindow {{
    background: {BG};
}}
/* 顶部功能栏：独立成栏（卡片背景），按钮扁平无边框 */
QFrame#toolbar {{
    background: {SURFACE_1};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_L};
}}
/* 中部三栏：统一卡片外观，之间由透明分割器露出页面底色形成间隔 */
QWidget#centerPanel, QWidget#sidePanel, QWidget#rightPanel {{
    background: {SURFACE_1};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_L};
}}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover {{ background: transparent; }}
QToolTip {{
    background: {SURFACE_3};
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    padding: 5px 9px;
    border-radius: {RADIUS_S};
}}

/* ================= 卡片（参数分组） ================= */
QFrame#card {{
    background: {SURFACE_1};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_L};
}}
QLabel#cardTitle {{
    color: {TEXT_SECONDARY};
    font-size: {FONT_BASE};
    font-weight: 600;
    letter-spacing: 0.6px;
    border: none;
}}

/* ================= 按钮：次级描边、主操作实心、工具栏扁平 ================= */
QPushButton {{
    background: {SURFACE_2};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_M};
    padding: {PAD_BTN};
    color: {TEXT};
    font-size: {FONT_BASE};
    min-height: {CTRL_H};
}}
QPushButton:hover {{ background: {HOVER}; border-color: #3d4452; }}
QPushButton:pressed {{ background: #23272f; }}
QPushButton:disabled {{ color: {TEXT_MUTED}; background: {SURFACE_1}; border-color: {BORDER}; }}
QPushButton:checked {{
    background: {ACCENT_BG};
    border-color: {ACCENT};
    color: #a8cdfF;
}}
QPushButton#primaryBtn {{
    background: {ACCENT};
    border: none;
    color: #ffffff;
    font-weight: 600;
    padding: 7px 18px;
}}
QPushButton#primaryBtn:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#primaryBtn:pressed {{ background: #0871d9; }}
QPushButton#primaryBtn:disabled {{ background: #1d2a3c; color: #5c6f8c; }}
QPushButton#dangerBtn {{ background: transparent; border: 1px solid {DANGER}; color: {DANGER}; }}
QPushButton#dangerBtn:hover {{ background: {DANGER_BG}; }}
/* 工具栏扁平按钮（去竖条分割线） */
QPushButton#toolBtn {{
    background: transparent;
    border: none;
    border-radius: {RADIUS_M};
    padding: {PAD_BTN};
    color: {TEXT};
    font-size: {FONT_BASE};
}}
QPushButton#toolBtn:hover {{ background: {SURFACE_2}; }}
QPushButton#toolBtn:pressed {{ background: #23272f; }}
QPushButton#toolBtn:disabled {{ color: {TEXT_MUTED}; background: transparent; }}
QToolButton {{
    background: {SURFACE_2};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_M};
    padding: 4px 8px;
    color: {TEXT};
    font-size: {FONT_BASE};
    min-width: 24px;
    min-height: {CTRL_H};
}}
QToolButton:hover {{ background: {HOVER}; }}
QToolButton:checked {{ background: {ACCENT_BG}; border-color: {ACCENT}; color: #a8cdff; }}

/* ================= 菜单 ================= */
QMenu {{
    background: {SURFACE_2};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_M};
    padding: 4px;
    color: {TEXT};
    font-size: {FONT_BASE};
}}
QMenu::item {{
    padding: 5px 22px 5px 10px;
    border-radius: 5px;
    min-width: 90px;
}}
QMenu::item:selected {{ background: {ACCENT}; color: #ffffff; }}
QMenu::item:checked {{ color: {ACCENT_HOVER}; font-weight: 600; }}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 3px 6px;
}}
QMenu::icon {{ padding-left: 4px; }}

/* ================= 输入控件 ================= */
QSpinBox, QDoubleSpinBox, QLineEdit, QTimeEdit, QComboBox {{
    background: {SURFACE_3};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_S};
    padding: {PAD_INPUT};
    color: {TEXT};
    font-size: {FONT_BASE};
    selection-background-color: {ACCENT};
    min-height: {INPUT_H};
}}
QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus, QTimeEdit:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QLineEdit {{ padding: 3px 7px; }}
/* 下拉框：内容自适应宽度，下拉列表用卡片风格 */
QComboBox {{ padding-right: 26px; }}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 22px;
    border: none;
}}
QComboBox::down-arrow {{
    width: 10px; height: 10px; {down_img}
}}
QComboBox QAbstractItemView {{
    background: {SURFACE_2};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_S};
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
    color: {TEXT};
    outline: none;
    padding: 2px;
}}
QComboBox QAbstractItemView::item {{ min-height: 20px; padding: 0 6px; border-radius: 4px; }}
QComboBox QAbstractItemView::item:selected {{ background: {ACCENT}; color: #ffffff; }}
QSpinBox::up-button, QDoubleSpinBox::up-button, QTimeEdit::up-button {{
    subcontrol-position: top right;
    width: 12px; height: 9px; background: transparent; border: none;
    margin: 1px 1px 0 0; border-radius: 4px;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button, QTimeEdit::down-button {{
    subcontrol-position: bottom right;
    width: 12px; height: 9px; background: transparent; border: none;
    margin: 0 1px 1px 0; border-radius: 4px;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover, QTimeEdit::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background: {HOVER}; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    width: 12px; height: 12px; {up_img}
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    width: 12px; height: 12px; {down_img}
}}
QCheckBox {{
    color: {TEXT_SECONDARY};
    spacing: 7px;
    background: transparent;
    font-size: {FONT_BASE};
}}
QCheckBox::indicator {{
    width: 15px; height: 15px; border-radius: 4px;
    border: 1px solid {BORDER_STRONG}; background: {SURFACE_3};
}}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{ background: {SURFACE_3}; border-color: {ACCENT}; {check_img} }}
QRadioButton {{
    color: {TEXT_SECONDARY};
    spacing: 6px;
    background: transparent;
    font-size: {FONT_BASE};
}}
QRadioButton::indicator {{
    width: 14px; height: 14px; border-radius: 7px;
    border: 1px solid {BORDER_STRONG}; background: {SURFACE_3};
}}
QRadioButton::indicator:hover {{ border-color: {ACCENT}; }}
QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

/* ================= 标签 ================= */
QLabel {{
    background: transparent;
    color: {TEXT};
    font-size: {FONT_BASE};
}}
QLabel#mono {{ font-family: {MONO_FONT}; font-size: {FONT_BASE}; color: {TEXT_SECONDARY}; }}
QLabel#hint {{ color: {TEXT_MUTED}; font-size: {FONT_SMALL}; }}
QLabel#fieldLbl {{ color: {TEXT_SECONDARY}; font-size: {FONT_BASE}; background: transparent; }}
QLabel#posPreview {{ background: {SURFACE_2}; border: 1px solid {BORDER}; border-radius: 4px;
    color: {TEXT_MUTED}; font-size: {FONT_SMALL}; }}

/* ================= 参数设置栏：更紧凑，避免内容被裁 ================= */
QWidget#sidePanel QLabel, QWidget#sidePanel QCheckBox, QWidget#sidePanel QRadioButton {{
    font-size: {FONT_BASE};
}}
QWidget#sidePanel QLabel#mono {{ font-size: {FONT_BASE}; }}
QWidget#sidePanel QPushButton {{
    padding: 4px 6px;
    font-size: {FONT_BASE};
    min-height: 18px;
}}
QWidget#sidePanel QSpinBox, QWidget#sidePanel QDoubleSpinBox,
QWidget#sidePanel QComboBox, QWidget#sidePanel QToolButton {{
    padding: 3px 6px;
    min-height: 18px;
    font-size: {FONT_BASE};
}}
/* 下拉框侧栏覆盖：单独留出右侧箭头位（padding-right 26px 不能被上面覆盖掉） */
QWidget#sidePanel QComboBox {{ padding: 3px 7px 3px 6px; }}
/* 禁用态灰显：默认“导出视频”时图片格式下拉是禁用的，需能看出状态 */
QComboBox:disabled {{ color: {TEXT_MUTED}; background: {SURFACE_2}; }}
QSpinBox:disabled, QDoubleSpinBox:disabled, QTimeEdit:disabled, QLineEdit:disabled {{
    color: {TEXT_MUTED};
    background: {SURFACE_2};
}}

/* 文件列表计数：方框背景 */
QLabel#fileCountBadge {{
    background: {SURFACE_2};
    border: 1px solid {BORDER_STRONG};
    border-radius: 8px;
    padding: 1px 8px;
    font-size: {FONT_SMALL};
    color: {TEXT_SECONDARY};
}}

/* 视图切换按钮：隐藏右下角菜单箭头，三态彩色图标 */
QToolButton#viewSwitchBtn {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 2px;
    background: transparent;
}}
QToolButton#viewSwitchBtn:hover {{ background: {HOVER}; }}
QToolButton#viewSwitchBtn::menu-indicator {{ image: none; width: 0; height: 0; }}

/* 文件列表行首的强制缓存按钮：半透明小方块，叠在缩略图/行首 */
/* ================= 列表 ================= */
QListWidget {{
    background: {SURFACE_1};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_L};
    outline: none;
    padding: 4px;
}}
QListWidget::item {{
    border-radius: {RADIUS_M};
    /* padding 必须为 0：Qt 对 QListView::item 的 padding 支持不完整，
       会导致图标相对高亮框位移（偏下）；缩略图与高亮框的间距由 ICON_PAD 控制 */
    padding: 0px;
    color: {TEXT_SECONDARY};
    border: 1px solid transparent;
}}
QListWidget::item:hover {{ background: {SURFACE_2}; border-color: {BORDER}; }}
QListWidget::item:selected {{
    background: {ACCENT_BG};
    border: 1px solid {ACCENT};
    color: {TEXT};
}}

/* ================= 滚动条 ================= */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: {SURFACE_3}; border-radius: 4px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {HOVER}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {SURFACE_3}; border-radius: 4px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: {HOVER}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}

/* ================= 进度条 ================= */
QProgressBar {{
    background: {SURFACE_2};
    border: none;
    border-radius: 4px;
    height: 8px;
    text-align: center;
    font-size: 10px;
    color: transparent;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}

/* ================= 音量/滑块 ================= */
QSlider::groove:horizontal {{
    height: 4px;
    background: {SURFACE_3};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 12px;
    margin: -5px 0;
    border-radius: 6px;
    background: {TEXT};
}}
QSlider::handle:horizontal:hover {{ background: {ACCENT_HOVER}; }}

/* ================= 状态栏 ================= */
QStatusBar {{
    background: {SURFACE_1};
    border-top: 1px solid {BORDER};
    color: {TEXT_MUTED};
    min-height: 22px;
}}
QStatusBar::item {{ border: none; }}
"""
