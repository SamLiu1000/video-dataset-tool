"""轻量中英切换：tr() 在中文时返回原文，英文时查 _EN 表。

使用说明：
- 启动时在构建 UI 前调用一次 set_lang()（从 QSettings 读），此后线程只读。
- 界面/信号里的用户可见字符串统一用 tr("...") 包裹；含插值的用 tr("模板{}").format(...)。
- 未收录到 _EN 的字符串在英文模式下仍返回原文，便于漏翻时优雅降级。
"""
from __future__ import annotations

LANG = "zh"  # "zh" 或 "en"


def set_lang(lang: str) -> None:
    global LANG
    LANG = "en" if lang == "en" else "zh"


def tr(text: str) -> str:
    if LANG != "en":
        return text
    return _EN.get(text, text)


# 中文 → 英文（键为源码里的中文原文，值用英文）
_EN = {
    "视频裁切工具 — AI 训练数据准备": "Video Crop Tool — AI Training Data Prep",
    "导入文件夹": "Import Folder",
    "导入文件": "Import Files",
    "添加一个或多个视频文件": "Add one or more video files",
    "设置保存位置": "Set Output Folder",
    "修复索引": "Repair Index",
    "修复视频缺失的 Cues 索引（补 Cues）：选源文件与输出位置，生成带完整索引的新文件，之后打开即可全片快速定位":
        "Repair missing Cues index: choose a source file and output location to generate a "
        "fully-indexed copy of the video, then open it to seek the whole clip quickly",
    "◀ 上一段": "◀ Prev Clip",
    "下一段 ▶": "Next Clip ▶",
    "✂ 导出": "✂ Export",
    "按右侧“导出选项”的当前模式导出（视频/图片/音频）":
        "Export using the mode selected in the Export Options on the right (video / image / audio)",
    "切换界面语言（中 / EN）": "Switch interface language (CN / EN)",
    "│ 竖参考线": "│ V-Guide",
    "─ 横参考线": "─ H-Guide",
    "清除参考线": "Clear Guides",
    "未载入": "No video",
    "重置缩放": "Reset Zoom",
    "重置预览画面缩放": "Reset preview zoom",
    "上一帧 (←/A)": "Prev frame (←/A)",
    "▶ 播放": "▶ Play",
    "⏸ 暂停": "⏸ Pause",
    "播放/暂停 (空格)": "Play/Pause (Space)",
    "下一帧 (→/D)": "Next frame (→/D)",
    "00:00:00.00 · 帧 0": "00:00:00.00 · Frame 0",
    "音量": "Volume",
    "预览音量（100%=原音量，最大 600%）": "Preview volume (100% = original, up to 600%)",
    "定位选区": "Fit Selection",
    "缩放时间轴窗口到当前裁切选区": "Zoom timeline window to the current crop selection",
    "重置时间轴缩放": "Reset timeline zoom",
    "入点画面（选区首帧）": "In-point frame (first frame of selection)",
    "入点": "In",
    "手动输入入点（时分秒），点 ✓ 生效": "Type in-point (H:M:S), click ✓ to apply",
    "应用入点": "Apply in-point",
    "出点": "Out",
    "出点画面（选区尾帧）": "Out-point frame (last frame of selection)",
    "手动输入出点（时分秒），点 ✓ 生效": "Type out-point (H:M:S), click ✓ to apply",
    "应用出点": "Apply out-point",
    "时长 --": "Duration --",
    "文件名": "File Name",
    "导出文件名模板（自动填充当前视频名）": "Export filename template (auto-filled with current video name)",
    "#=序号(1,2,3…)  $=日期(精确到分钟)  %=字母(a,b,…,z,aa…)":
        "#=number(1,2,3…)  $=date(to minute)  %=letter(a,b,…,z,aa…)",
    "插入序号 #：同一视频第 1、2、3… 段": "Insert number #: 1st, 2nd, 3rd… segment of the same video",
    "插入日期 $：精确到分钟": "Insert date $: precise to the minute",
    "插入字母 %：a,b,…,z,aa,ab…（逻辑同 #）": "Insert letter %: a,b,…,z,aa,ab… (same logic as #)",
    "文件列表": "File List",
    "切换文件列表视图（名称 / 大图）· 左键循环切换，右键菜单":
        "Switch file-list view (Name / Thumbnail) · left-click cycles, right-click for menu",
    "切换文件列表视图（当前：{}）": "Switch file-list view (current: {})",
    "名称": "Name",
    "大图": "Thumbnail",
    "未检测到 NVENC 硬件编码器，将使用 CPU 编码":
        "NVENC hardware encoder not detected; CPU encoding will be used",
    "选择视频文件夹": "Select video folder",
    "选择视频文件": "Select video files",
    "视频文件 ({})": "Video files ({})",
    "所有文件 (*)": "All files (*)",
    "所选文件已存在或无法添加": "The selected file already exists or cannot be added",
    "已添加 {} 个视频": "Added {} video(s)",
    "已添加：{}": "Added: {}",
    "提示": "Notice",
    "该文件夹及子文件夹下没有找到视频文件": "No video files found in this folder or its subfolders",
    "正在批量裁切，请稍候或先停止": "A batch crop is running; please wait or stop it first",
    "正在加载预览 0s": "Loading preview 0s",
    "正在加载预览 {:.1f}s": "Loading preview {:.1f}s",
    "警告：未检测到 ffmpeg，裁切导出将不可用":
        "Warning: ffmpeg not detected; crop export will be unavailable",
    "自动 (默认)": "Auto (default)",
    "音轨 {}": "Track {}",
    "无法打开 {}：{}": "Cannot open {}: {}",
    "无法打开视频：{}": "Cannot open video: {}",
    "正在修复中，请稍候": "Repairing in progress, please wait",
    "选择需要修复 Cues 的视频": "Select video to repair Cues",
    "视频 (*.mkv *.mp4 *.mov);;所有文件 (*)":
        "Video (*.mkv *.mp4 *.mov);;All files (*)",
    "选择修复后的保存位置": "Select output location after repair",
    "视频 (*.mkv *.mp4);;所有文件 (*)": "Video (*.mkv *.mp4);;All files (*)",
    "输出不能覆盖原文件，请换一个保存位置":
        "Output cannot overwrite the source file; choose a different output location",
    "正在修复视频索引 Cues…  {}": "Repairing video index Cues…  {}",
    "修复完成：{}": "Repair done: {}",
    "修复失败：{}": "Repair failed: {}",
    "{} · 帧 {}": "{} · Frame {}",
    "时长 {:.1f} 秒": "Duration {:.1f}s",
    "保存位置：{}": "Output folder: {}",
    "当前保存位置：{}": "Current save location: {}",
    "当前保存位置：未设置（将保存到源视频目录下的 crops）":
        "Current save location: not set (will save to a 'crops' folder beside the source)",
    "选择保存位置": "Select output folder",
    "请先导入并打开一个视频": "Please import and open a video first",
    "请至少勾选一种导出内容（视频/图片/声音）":
        "Please check at least one export type (video / image / audio)",
    "已阻止导出": "Export blocked",
    "当前选区时长 {:.1f}s，超过 {:.0f}s 上限，已取消导出。\n"
    "（确需导出长片段时，请取消勾选“超过 5 秒禁止导出”）":
        "The selected range is {:.1f}s, over the {:.0f}s limit; export cancelled.\n"
        "(To export longer clips, uncheck \u201cBlock exports over 5 seconds\u201d)",
    "文件列表为空，请先导入视频": "File list is empty; please import videos first",
    "已有任务在运行，请先停止": "A task is already running; please stop it first",
    "准备中…": "Preparing…",
    "正在裁切 {}/{}": "Cropping {}/{}",
    "已导出：{}": "Exported: {}",
    "裁切失败：{}": "Crop failed: {}",
    "全部完成（{} 个任务）": "All done ({} task(s))",
    "尚未载入视频": "No video loaded",
    "分辨率 {}×{}": "Resolution {}×{}",
    "裁剪区 {}×{}": "Crop {}×{}",
    "输出尺寸 {}×{}": "Output {}×{}",
    "帧率 {:.2f}fps": "Frame rate {:.2f}fps",
    "时间 {}–{}": "Time {}–{}",
    "预计大小 ≈{}": "Est. size ≈{}",
    "点击应用 {}×{}，右键删除": "Click to apply {}×{}, right-click to delete",
    "裁剪尺寸": "Crop Size",
    "宽": "W",
    "高": "H",
    "交换宽高（面板数值互换，裁切框等比适应画面）":
        "Swap width/height (swap the panel values; crop box scales to fit the frame)",
    "保持宽高比": "Keep Aspect Ratio",
    "保持尺寸缩放": "Keep Size Scale",
    "勾选：拖动手柄等比缩放（宽高比不变），导出按框的实际尺寸；\n取消：拖动可自由拉伸":
        "Checked: drag handles scale proportionally (aspect ratio locked); export at the box's actual size.\n"
        "Unchecked: drag to stretch freely",
    "勾选：拖动手柄等比缩放调整构图，但导出分辨率锁定为上方宽高设定值\n"
    "（框内内容缩放输出，可能放大变糊）":
        "Checked: drag handles to resize the composition, but export resolution is locked to the W/H "
        "values above (content interpolates, may blur when upscaled)",
    "预设尺寸": "Presets",
    "将当前尺寸存为预设": "Save current size as a preset",
    "帧率": "Frame Rate",
    "原帧率": "Original FPS",
    "导出时长": "Export Duration",
    "裁切信息": "Crop Info",
    "导出选项": "Export Options",
    "导出视频": "Export Video",
    "导出 mp4 · H264 视频片段": "Export mp4 · H264 video clip",
    "导出图片": "Export Image",
    "导出图片（jpg/png）": "Export image (jpg/png)",
    "质量": "Quality",
    "JPG 压缩质量（仅 JPG 格式生效；PNG 无损无需设置）":
        "JPG compression quality (only affects JPG; PNG is lossless)",
    "音轨": "Audio Track",
    "选择要预览/导出的音轨": "Select audio track to preview/export",
    "单独导出声音 (mp3)": "Export Audio Only (mp3)",
    "同时导出选区内的声音为 mp3": "Also export the audio inside the selection as mp3",
    "导出音频增益": "Audio Gain",
    "导出声音的音量增益：100%=原音量，最大可放大到 600%":
        "Export audio gain: 100% = original volume, up to 600% boost",
    "视频保留音频": "Keep Video Audio",
    "导出视频片段时是否保留原音轨": "Keep the original audio track when exporting a video clip",
    "硬件加速（NVENC）": "Hardware Acceleration (NVENC)",
    "超过 5 秒禁止导出": "Block exports over 5 seconds",
    "防止误操作（默认开启）：视频片段选区时长超过 5 秒时阻止导出，\n"
    "避免误点导出长片段白等半天。确需导出长片段时取消勾选即可。":
        "Prevents accidental exports (on by default): blocks exporting a video clip whose "
        "selection is longer than 5 seconds, so you don't wait on a long clip by mistake. "
        "Uncheck it to export longer clips.",
    "操作提示": "Help",
    "拖动框内 · 移动裁剪区\n拖动边角 · 调整大小\n空白处拖拽 · 新建裁剪框\n参考线 · 阻挡裁切框\n"
    "双击画面 · 恢复全画面\n空格 · 播放/暂停\n←/→ · 逐帧移动（Ctrl±10帧）\n"
    "I / O · 设置入点/出点\n滚轮 · 缩放预览 · 中键 · 平移":
        "Drag inside · move crop\nDrag corners · resize\nDrag empty area · new crop box\n"
        "Guide lines · block the crop box\nDouble-click frame · full frame\nSpace · play/pause\n"
        "←/→ · step frame (Ctrl±10)\nI / O · set in/out point\nWheel · zoom · middle · pan",
    "速度": "Speed",
    "播放速度(0.1x~8.0x)：拖动/滚轮调节，双击复位 1.0x":
        "Playback speed (0.1x–8.0x): drag/wheel to adjust, double-click resets to 1.0x",
    "时间轴": "Timeline",
    "未找到 ffmpeg，请先安装并加入 PATH":
        "ffmpeg not found; please install it and add it to PATH",
    "FFmpeg 返回错误": "FFmpeg returned an error",
    "cjpeg 转换失败": "cjpeg conversion failed",
    "已取消": "Cancelled",
    "ffmpeg 补 Cues 失败": "Failed to add Cues via ffmpeg",
    "[{}/{}] 正在处理 {}": "[{}/{}] Processing {}",
    "视频加载超时或无法解析": "Video timed out or could not be parsed",
    "导入文件夹后将在此预览视频\n拖动鼠标即可框选裁剪区域":
        "Import a folder to preview the video here\nDrag the mouse to draw a crop area",
}
