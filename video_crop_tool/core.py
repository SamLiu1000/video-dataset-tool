"""核心引擎层：裁切任务模型、FFmpeg 子进程执行、工具函数。

对应方案文档的关键原则：
- 裁切/编码绝不在 UI 主线程执行 —— 全部走 FFmpegWorker(QThread)
- 实时进度 —— 解析 FFmpeg -progress 输出
- 视频预览由 mpv 原生渲染（见 mpv_player.py），本模块不再承担取帧职责
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass

import av
from PySide6.QtCore import QObject, QThread, Signal

from .i18n import tr

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv", ".mpg", ".mpeg", ".ts", ".3gp"}

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def fmt_time(sec: float, with_ms: bool = True) -> str:
    """秒 -> HH:MM:SS.mm（与 HTML 原型的显示风格一致）"""
    if sec is None or sec < 0:
        sec = 0.0
    ms = int(round((sec - int(sec)) * 100))
    h = int(sec) // 3600
    m = (int(sec) % 3600) // 60
    s = int(sec) % 60
    if with_ms:
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def human_size(nbytes: float) -> str:
    nbytes = max(0.0, nbytes)
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024 or unit == "GB":
            return f"{nbytes:.1f}{unit}" if unit != "B" else f"{int(nbytes)}B"
        nbytes /= 1024.0
    return f"{nbytes:.1f}GB"


def estimate_size_bytes(duration: float, w: int, h: int, fps: float, bit_factor: float = 0.05) -> float:
    """粗略估算 H.264 输出体积：原始 RGB 字节数 × 经验压缩系数（≈0.05）"""
    return max(0.0, duration) * max(1, fps) * max(1, w) * max(1, h) * 3 * bit_factor


def detect_nvenc() -> bool:
    """检测 FFmpeg 是否支持 NVENC 硬件编码（结果缓存）"""
    if detect_nvenc._cached is not None:
        return detect_nvenc._cached
    ok = False
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
            creationflags=_CREATE_NO_WINDOW,
        ).stdout
        ok = bool(re.search(r"h264_nvenc|hevc_nvenc", out))
    except Exception:
        ok = False
    detect_nvenc._cached = ok
    return ok


detect_nvenc._cached = None


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def find_cjpeg() -> str | None:
    """定位系统 cjpeg（libjpeg-turbo 命令行）：PATH 优先，其次常见安装目录"""
    p = shutil.which("cjpeg")
    if p:
        return p
    for cand in (
        os.path.join(os.environ.get("ProgramFiles", ""), "libjpeg-turbo", "bin", "cjpeg.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "libjpeg-turbo", "bin", "cjpeg.exe"),
        r"C:\libjpeg-turbo\bin\cjpeg.exe",
    ):
        if cand and os.path.exists(cand):
            return cand
    return None


def ensure_even(n: int) -> int:
    return int(n) & ~1


def source_video_bitrate(path: str) -> int:
    """源视频的视频流码率（bps）；容器未记录时用 文件大小/时长 估算。返回 0 表示未知。"""
    try:
        with av.open(path) as c:
            vs = next((s for s in c.streams if s.type == "video"), None)
            if vs is not None and vs.bit_rate:
                return int(vs.bit_rate)
            dur = c.duration / 1_000_000.0 if c.duration else 0.0
    except Exception:
        dur = 0.0
    if dur > 0:
        return int(os.path.getsize(path) * 8 / dur)
    return 0


def _is_hdr(path: str) -> bool:
    """检测源视频是否 HDR（PQ/HLG）。HDR 源导出时自动转 SDR，非 HDR 保持原样。

    依据 video stream 的 color_transfer（转码传递特性）：
    smpte2084=PQ、arib-std-b67=HLG。判断失败/无标注一律按 SDR 处理（不套滤镜）。
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30,
            creationflags=_CREATE_NO_WINDOW).stdout.strip()
        return out in ("smpte2084", "arib-std-b67")
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 裁切任务模型
# ---------------------------------------------------------------------------
@dataclass
class CropJob:
    source: str
    out_dir: str
    crop_x: int
    crop_y: int
    crop_w: int
    crop_h: int
    in_point: float
    out_point: float
    fps: float
    out_w: int
    out_h: int
    keep_scale: bool = True
    keep_audio: bool = False
    audio_track: int = -1      # 要导出的音轨序号(0 起的音频流); -1 = 默认/第一条
    audio_gain: float = 1.0    # 音频增益(1.0=100%; 支持 >1 放大到 600%)
    hw: bool = True
    hdr_to_sdr: bool = False   # HDR(PQ/HLG) 源 → 导出时转 SDR（否则画面偏暗偏灰）
    source_bitrate: int = 0    # 源视频码率（bps）；>0 时导出视频片段按它封顶，防止体积暴涨
    label: str = ""
    kind: str = "segment"  # 'segment' 视频片段 | 'frame' 单帧截图 | 'audio' 声音
    filename: str = ""
    img_format: str = "png"   # 图片导出格式 'png' | 'jpg'
    jpg_quality: int = 90     # jpg 质量 1..100
    sar: float = 1.0          # 源像素宽高比(PAR)；!=1.0 为变形源，导出时还原到显示宽高比

    def __post_init__(self) -> None:
        # yuv420p 要求宽高与起点全为偶数（向下取偶不会越界）；UI 层保持用户输入原值，
        # 偶数化统一在此兜底
        self.crop_w = ensure_even(max(2, self.crop_w))
        self.crop_h = ensure_even(max(2, self.crop_h))
        self.crop_x = ensure_even(max(0, self.crop_x))
        self.crop_y = ensure_even(max(0, self.crop_y))
        self.out_w = max(2, self.out_w)
        self.out_h = max(2, self.out_h)

    def duration(self) -> float:
        return max(0.001, self.out_point - self.in_point)

    def output_size(self) -> tuple[int, int]:
        if self.keep_scale:
            # 变形源按显示宽高比还原（否则导出看起来被拉伸）
            if abs(self.sar - 1.0) > 1e-3:
                return (ensure_even(int(round(self.crop_w * self.sar))), ensure_even(self.crop_h))
            return (self.crop_w, self.crop_h)
        return (self.out_w, self.out_h)

    def estimate_bytes(self) -> float:
        w, h = self.output_size()
        if self.kind == "frame":
            return w * h * 3
        return estimate_size_bytes(self.duration(), w, h, self.fps)

    # -- FFmpeg 命令 -----------------------------------------------------
    # HDR(PQ/HLG) → SDR 滤镜链（libzimg 的 zscale + tonemap，ffmpeg 6.0 官方构建自带）：
    #   EOTF 转线性(npl=100 以 SDR 白为基准) → Reinhard 色调映射 → BT.709 色域
    #   → gamma 2.4 编码。实测校准：zimg 的 t=bt709 是纯 gamma 2.4。
    # peak 必须显式固定为 10 与预览端 _chain_tone_map 一致：ffmpeg 的 tonemap
    # 默认会读源文件 mastering 元数据取峰值（如 1000），那会让导出色调曲线与
    # 预览不同（预览=导出不一致）；固定 peak=10 后两者始终一致。
    # 末尾按导出类型追加：视频/JPG 需 yuv420p；PNG/PPM 保留全分辨率全色度（不
    # 下采样，否则 PNG 体积从 16bit 全分辨率掉到 8bit 4:2:0 且画质受损）。
    _HDR_TO_SDR_VF = ("zscale=t=linear:npl=100,format=gbrpf32le,"
                      "tonemap=reinhard:desat=0:peak=10,zscale=p=bt709")

    def build_cmd(self, out_path: str, jpg_via: str = "mjpeg") -> list[str]:
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
        if self.kind == "audio":
            # 单独导出声音：默认 mp3，可选指定音轨 + 音量增益(0~600%)
            cmd += ["-ss", f"{self.in_point:.3f}", "-t", f"{self.duration():.3f}"]
            cmd += ["-i", self.source]
            if self.audio_track >= 0:
                cmd += ["-map", f"0:a:{self.audio_track}"]
            cmd += ["-vn", "-c:a", "libmp3lame", "-q:a", "2", "-map_metadata", "-1"]
            if self.audio_gain != 1.0:
                cmd += ["-af", f"volume={self.audio_gain:.3f}"]
            cmd += [out_path]
            return cmd
        vf = f"crop={self.crop_w}:{self.crop_h}:{self.crop_x}:{self.crop_y}"
        if abs(self.sar - 1.0) > 1e-3:
            # 变形源(PAR≠1)：把裁剪内容还原到显示宽高比，否则预览(16:9)与导出
            # (4:3 像素)不一致、图片/视频看起来被拉伸。
            dw = ensure_even(int(round(self.crop_w * self.sar)))
            dh = ensure_even(self.crop_h)
            vf += f",scale={dw}:{dh},setsar=1"
        if self.kind == "segment":
            vf += f",fps={self.fps}"
        if not self.keep_scale:
            # 保持尺寸缩放：把裁切内容缩放到锁定的导出尺寸（视频与图片通用）
            vf += f",scale={self.out_w}:{self.out_h}"
        if self.hdr_to_sdr:
            vf += "," + self._HDR_TO_SDR_VF
            if self.kind == "segment" or (self.kind == "frame" and self.img_format == "jpg"
                                          and jpg_via == "mjpeg"):
                # 视频编码器(H.264/mjpeg)需要 yuv420p limited
                vf += ",zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
            else:
                # PNG / PPM(经 cjpeg)：全分辨率全色度，full range
                vf += ",zscale=t=bt709:m=bt709:r=pc"
        # -ss/-t 放在 -i 之前（input seeking）：直接跳到入点附近的关键帧，
        # 避免从 0 逐帧解码到入点（否则长视频导出短片段会慢到像卡死）
        if self.kind == "segment":
            cmd += ["-progress", "pipe:1", "-nostats"]
            cmd += ["-ss", f"{self.in_point:.3f}", "-t", f"{self.duration():.3f}"]
        else:
            cmd += ["-ss", f"{self.in_point:.3f}"]
        cmd += ["-i", self.source]
        if self.kind == "segment" and self.keep_audio and self.audio_track >= 0:
            # 指定音轨：显式 map 视频 + 该音轨（一旦出现 -map，自动选择即失效，
            # 必须把视频也 map 上，否则只剩音轨丢画面）
            cmd += ["-map", "0:v:0", "-map", f"0:a:{self.audio_track}"]
        cmd += ["-vf", vf]
        if self.kind == "segment":
            if self.hw and detect_nvenc():
                cmd += ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "19", "-pix_fmt", "yuv420p"]
            else:
                cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
            if self.source_bitrate > 0:
                # CRF/CQ 是无上限的质量模式：重编码复杂画面（颗粒/噪点）时码率可能
                # 远超源文件（20G 源导出 15 分钟暴涨到 32G）。按源视频码率封顶后，
                # 输出用与源相同的比特预算，画质与源相当，体积不再暴涨。
                cmd += ["-maxrate", str(self.source_bitrate),
                        "-bufsize", str(2 * self.source_bitrate)]
            if self.keep_audio:
                cmd += ["-c:a", "aac", "-b:a", "192k"]
                if self.audio_gain != 1.0:
                    cmd += ["-af", f"volume={self.audio_gain:.3f}"]
            else:
                cmd += ["-an"]
            cmd += ["-movflags", "+faststart"]
        else:
            # 图片导出：jpg（带质量）或 png
            cmd += ["-frames:v", "1"]
            if self.img_format == "jpg":
                if jpg_via == "ppm":
                    # cjpeg (libjpeg-turbo) 流程：ffmpeg 只抽帧出 PPM（无损中间格式），
                    # 质量由后续 cjpeg -quality 0-100 控制（与 UI 百分比一致）
                    cmd += ["-c:v", "ppm"]
                else:
                    # mjpeg 的 qscale 范围是 1~31（1 最好），不是 UI 的 1~100；
                    # 直接传 90 会被 ffmpeg clamp 到 31（最差）→ 导出图特别模糊
                    q = max(1, min(31, round(1 + (100 - self.jpg_quality) * 30 / 99)))
                    cmd += ["-c:v", "mjpeg", "-q:v", f"{q}"]
            else:
                cmd += ["-c:v", "png"]
        cmd += [out_path]
        return cmd


# ---------------------------------------------------------------------------
# 任务结果摘要（仅给 UI 回显导出信息，不落盘）
# ---------------------------------------------------------------------------
def make_job_entry(job: CropJob, out_path: str) -> dict:
    return {
        "source": job.source,
        "output": out_path,
        "output_basename": os.path.basename(out_path),
    }


def unique_output_path(out_dir: str, stem: str, ext: str = ".mp4") -> str:
    p = os.path.join(out_dir, stem + ext)
    n = 1
    while os.path.exists(p):
        p = os.path.join(out_dir, f"{stem}_{n}{ext}")
        n += 1
    return p


def sanitize_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "clip"


# ---------------------------------------------------------------------------
# FFmpeg 子进程 Worker（QThread，绝不在 UI 主线程执行）
# ---------------------------------------------------------------------------
class FFmpegWorker(QThread):
    progress = Signal(int, int, float)      # job_index, total, percent(0..1)
    job_done = Signal(int, int, dict)       # job_index, total, 结果摘要(source/output/basename)
    job_error = Signal(int, int, str)
    all_done = Signal(int)
    status = Signal(str)

    def __init__(self, jobs: list[CropJob], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.jobs = jobs
        self._cancel = False
        self._proc: subprocess.Popen | None = None

    def cancel(self) -> None:
        self._cancel = True
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                pass

    def run(self) -> None:
        total = len(self.jobs)
        for i, job in enumerate(self.jobs):
            if self._cancel:
                break
            try:
                out_path = self._run_job(i, total, job)
                self.job_done.emit(i, total, make_job_entry(job, out_path))
            except Exception as exc:
                self.job_error.emit(i, total, str(exc))
        self.all_done.emit(total)

    def _run_job(self, i: int, total: int, job: CropJob) -> str:
        if not ffmpeg_available():
            raise RuntimeError(tr("未找到 ffmpeg，请先安装并加入 PATH"))
        stem = job.filename or sanitize_name(os.path.splitext(os.path.basename(job.source))[0])
        if job.kind == "segment":
            stem = f"{stem}_{int(round(job.in_point * 1000))}-{int(round(job.out_point * 1000))}"
            ext = ".mp4"
        elif job.kind == "audio":
            stem = f"{stem}_{int(round(job.in_point * 1000))}-{int(round(job.out_point * 1000))}"
            ext = ".mp3"
        else:
            if job.out_point > 0:
                # 图片+视频同导出：与视频段同名的图片（仅扩展名不同）
                stem = f"{stem}_{int(round(job.in_point * 1000))}-{int(round(job.out_point * 1000))}"
            else:
                stem = f"{stem}_frame_{int(round(job.in_point * 1000))}"
            ext = ".jpg" if job.img_format == "jpg" else ".png"
        out_path = unique_output_path(job.out_dir, stem, ext)
        # 视频片段：读源码率用于导出封顶（防体积暴涨；同一源批量导出只读一次）
        if job.kind == "segment" and job.source_bitrate <= 0:
            job.source_bitrate = source_video_bitrate(job.source)
        # HDR 源自动转 SDR（默认启用，不再依赖用户勾选）：源是 PQ/HLG 才套滤镜，
        # 非 HDR 源保持原样，避免硬套滤镜导致导出失败
        if job.kind in ("segment", "frame"):
            job.hdr_to_sdr = _is_hdr(job.source)
        # JPG：优先走系统 cjpeg (libjpeg-turbo) —— ffmpeg 抽帧出 PPM 无损中间图，
        # cjpeg -quality 0-100 直接对应 UI 质量百分比；无 cjpeg 时回退 ffmpeg mjpeg
        cjpeg_path = find_cjpeg() if job.img_format == "jpg" else None
        use_cjpeg = cjpeg_path is not None
        if use_cjpeg:
            tmp_ppm = unique_output_path(job.out_dir, stem, ".ppm")
            cmd = job.build_cmd(tmp_ppm, jpg_via="ppm")
        else:
            cmd = job.build_cmd(out_path)
        self.status.emit(tr("[{}/{}] 正在处理 {}").format(i + 1, total, os.path.basename(job.source)))
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_CREATE_NO_WINDOW,
        )
        in_sec, dur = job.in_point, job.duration()
        rc = self._wait_progress(self._proc, i, total, in_sec, dur)
        if self._cancel:
            raise RuntimeError(tr("已取消"))
        if rc != 0:
            err = (self._proc.stderr.read() if self._proc.stderr else "") or tr("FFmpeg 返回错误")
            raise RuntimeError(err.strip()[-500:])
        if use_cjpeg:
            # cjpeg -quality：0-100 与 UI 质量百分比一致
            cj = subprocess.run(
                [cjpeg_path, "-quality", str(max(1, min(100, job.jpg_quality))),
                 "-outfile", out_path, tmp_ppm],
                capture_output=True, text=True,
                creationflags=_CREATE_NO_WINDOW)
            try:
                os.remove(tmp_ppm)
            except OSError:
                pass
            if cj.returncode != 0:
                raise RuntimeError((cj.stderr or tr("cjpeg 转换失败")).strip()[-500:])
        return out_path

    def _wait_progress(self, proc, i: int, total: int, in_sec: float, dur: float) -> int:
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            if "=" in line:
                k, v = line.strip().split("=", 1)
                if k == "out_time_ms":
                    try:
                        sec = int(v) / 1_000_000.0
                        pct = (sec - in_sec) / dur
                        self.progress.emit(i, total, max(0.0, min(1.0, pct)))
                    except ValueError:
                        pass
                elif k == "progress" and v == "end":
                    self.progress.emit(i, total, 1.0)
            if self._cancel and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
        return proc.wait()


# ---------------------------------------------------------------------------
# 修复 MKV/MP4 缺 Cues：字节级 remux（-c copy），生成带完整索引的新文件
# ---------------------------------------------------------------------------
def build_remux_cmd(src: str, dst: str) -> list[str]:
    """补 Cues：-c copy 重写容器（不重编码，画质码率不变），生成完整索引。

    -map 0 保留全部流；-movflags +faststart 仅对 MP4 输出有意义。
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats",
           "-i", src, "-map", "0", "-map_metadata", "0", "-c", "copy"]
    if dst.lower().endswith(".mp4"):
        cmd += ["-movflags", "+faststart"]
    cmd += [dst]
    return cmd


class RemuxWorker(QThread):
    """后台补 Cues（ffmpeg -c copy），逐行解析 -progress 报真实进度。

    进度 = 容器已写出的时间戳 out_time_us / 视频时长。只复制字节，不解码。
    """
    progress = Signal(float)   # 0..1
    done = Signal(str)         # 输出文件路径
    error = Signal(str)

    def __init__(self, src: str, dst: str, duration: float = 0.0,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.src = src
        self.dst = dst
        self._duration = duration
        self._proc: subprocess.Popen | None = None
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                pass

    def _video_duration(self) -> float:
        if self._duration > 0:
            return self._duration
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", self.src],
                capture_output=True, text=True, timeout=30,
                creationflags=_CREATE_NO_WINDOW).stdout.strip()
            return float(out or 0)
        except Exception:
            return 0.0

    def run(self) -> None:
        dur = self._video_duration()
        cmd = build_remux_cmd(self.src, self.dst)
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=_CREATE_NO_WINDOW)
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k == "out_time_us":
                try:
                    sec = int(v) / 1_000_000.0
                    if dur > 0:
                        self.progress.emit(max(0.0, min(1.0, sec / dur)))
                except ValueError:
                    pass
            elif k == "progress" and v == "end":
                self.progress.emit(1.0)
        rc = self._proc.wait()
        if self._cancel:
            self.error.emit(tr("已取消"))
            return
        if rc != 0:
            err = (self._proc.stderr.read() if self._proc.stderr else "") or tr("ffmpeg 补 Cues 失败")
            self.error.emit(err.strip()[-300:])
            return
        self.done.emit(self.dst)
