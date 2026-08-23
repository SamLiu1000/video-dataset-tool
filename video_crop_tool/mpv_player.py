"""python-mpv(libmpv) 取帧引擎（--wid 内嵌渲染），替代 PyAV 的 VideoReader。

设计：
- 单 mpv 实例（真实桌面 vo=gpu + wid 内嵌到 _VideoWidget 原生窗口；offscreen
  自动化回退 vo=null）：所有播放/拖动/步进取帧走同一 mpv 渲染流 —— mpv
  内部的 demux 预读线程、seek 合并窗口（0.3s）、hr-seek 精确落点都是现成
  的，对应"mpv 拖动预览流畅分析"报告。
- 静止帧 / 悬停缩略图用 ffmpeg 子进程抽帧（独立进程，不碰 mpv 播放器，
  精确且稳定；辅助 mpv 实例在部分环境会与主播放器冲突导致播放无反应）。
- 渲染：mpv 自己创建 GL 上下文渲染到容器原生窗口（--wid），不经过 Qt 的
  GL 上下文。早期用 render API（MpvRenderContext → Qt GL 上下文）实测
  libmpv 在 Qt 上下文里偶发纹理创建失败（INVALID_ENUM）导致花屏，且与
  呈现方式（blit/shader）无关；--wid 规避该问题（画面实测正常）。
- 线程安全：libmpv API 线程安全，本类方法带 RLock，由后台 worker 线程
  串行调用；mpv 渲染在其内部线程，UI 更新只经 Qt 信号。
"""
from __future__ import annotations

import os
import sys
import threading
import time

import numpy as np
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from .i18n import tr

_HERE = os.path.dirname(os.path.abspath(__file__))


def _ensure_libmpv() -> None:
    """libmpv-2.dll 搜索：绿色版 _MEIPASS 根 → 项目内 libmpv/ → PATH 已有则跳过。

    python-mpv 在 import 时就要找到 dll（模块级 loadlib），必须在 import 前
    把所在目录加进 PATH。
    """
    if os.environ.get("VCT_MPV_DLL"):
        d = os.environ["VCT_MPV_DLL"]
        if os.path.isdir(d):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            return
    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            # 绿色版：add-binary 把 libmpv-2.dll 放在 _MEIPASS 根
            candidates.append(meipass)
            candidates.append(os.path.join(meipass, "video_crop_tool", "libmpv"))
    candidates.append(os.path.join(_HERE, "libmpv"))
    # 项目根 libmpv/（mpv_player.py 位于 video_crop_tool/ 子目录，实际 dll 在上一级 libmpv/）
    candidates.append(os.path.join(os.path.dirname(_HERE), "libmpv"))
    candidates.append(os.path.dirname(os.environ.get("MPV_HOME", "")))
    for cand in candidates:
        if cand and os.path.exists(os.path.join(cand, "libmpv-2.dll")):
            os.environ["PATH"] = cand + os.pathsep + os.environ.get("PATH", "")
            return


_ensure_libmpv()
import mpv  # noqa: E402


def bgr0_to_qimage(data: bytes, stride: int, h: int) -> QImage:
    """screenshot-raw 的 bgr0 字节 → QImage（Format_RGB32，零颜色重排）。

    bgr0 内存字节序 = B,G,R,X（小端 0xAARRGGBB 存储），与 Qt Format_RGB32
    完全一致，直接包 buffer 即正确显示。
    """
    arr = np.frombuffer(data, dtype=np.uint8)
    if arr.size >= stride * h:
        arr = arr[: stride * h]
    img = QImage(arr.data, stride // 4, h, stride, QImage.Format_RGB32)
    return img.copy()


def ffmpeg_frame(path: str, sec: float, max_w: int = 0, max_h: int = 0) -> QImage | None:
    """ffmpeg 子进程抽帧（独立进程，不碰 mpv 播放器）。

    用于静止帧/悬停缩略图：精确（与 ffmpeg 基准差异 0.0）、稳定，且不
    影响主播放器的播放状态。耗时 ~100-150ms，低频取帧可接受。失败返回 None。
    """
    import subprocess
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-threads", "1", "-ss", f"{sec:.3f}", "-i", path, "-frames:v", "1"]
    if max_w or max_h:
        cmd += ["-vf", f"scale='min({max_w or 99999},iw)':-2"]
    cmd += ["-f", "image2pipe", "-vcodec", "png", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30,
                           creationflags=0x08000000 if os.name == "nt" else 0)
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    img = QImage()
    if img.loadFromData(r.stdout, "PNG"):
        return img
    return None


def _pil_resize(img, max_w: int, max_h: int) -> "Image":
    from PIL import Image
    w, h = img.size
    scale = min(1.0, max_w / max(1, w), max_h / max(1, h))
    if scale >= 1.0:
        return img
    return img.resize((max(2, int(w * scale)), max(2, int(h * scale))),
                      Image.BILINEAR)


def _parse_sar(raw) -> float:
    """像素宽高比(PAR)：兼容 mpv 返回的浮点或 'a:b' / 'a/b' 字符串(如 4:3 / 4/3)。
    解析失败或非正方形像素时返回 1.0。"""
    if raw is None:
        return 1.0
    try:
        v = float(raw)
        return v if v > 0 else 1.0
    except (TypeError, ValueError):
        pass
    s = str(raw).strip()
    for sep in (":", "/"):
        if sep in s:
            a, b = s.split(sep, 1)
            try:
                num, den = float(a), float(b)
            except (TypeError, ValueError):
                continue
            return (num / den) if den else 1.0
    return 1.0


class MvpPlayer:
    """mpv 取帧播放引擎（--wid 内嵌渲染）。

    帧号语义：frame_idx 由 UI 维护；mpv 只负责 seek(sec=idx/fps,
    precision='exact')（hr-seek 精确到目标帧）与 frame-step。
    真实桌面：vo=gpu + wid 内嵌到 _VideoWidget 的原生窗口，mpv 自己管理
    GL 上下文渲染（播放/拖动/seek 落点即时上屏）；offscreen（自动化）
    回退 vo=null，暂停态精确取帧走 ffmpeg_frame。
    """

    def __init__(self) -> None:
        self.path = ""
        self._current_path = ""
        self.width = 0
        self.height = 0
        self.dar = 0.0          # 显示宽高比（含像素比 PAR），裁剪构图视图对齐用
        self.sar = 1.0          # 像素宽高比(PAR)；变形源(≠1)导出时还原到显示宽高比
        self.fps = 30.0
        self.duration = 0.0
        self.frame_count = 0
        self.has_audio = False
        self.video_bitrate = 0
        self._mpv: "mpv.MPV | None" = None
        self._lock = threading.RLock()
        self._error = ""
        self._frame_idx = 0
        self._playing = False

    # ------------------------------------------------------------------
    # 打开 / 关闭
    # ------------------------------------------------------------------
    @staticmethod
    def _new_mpv(render: bool = False, wid: int | None = None) -> "mpv.MPV":
        if render and wid:
            # --wid 内嵌：mpv 自己创建 GL 上下文渲染到给定原生窗口。早期
            # render API（vo=libmpv + MpvRenderContext → Qt GL 上下文）实测
            # libmpv 在 Qt 上下文里偶发纹理创建失败（INVALID_ENUM）导致
            # 花屏，且与呈现方式无关；--wid 让 mpv 自管上下文，规避该问题。
            # gpu-api=opengl：Windows 上默认 d3d11 后端渲染到 Qt 子窗口会
            # 因合成问题黑屏（VO 有帧但窗口不显示）；opengl(wgl) 直绘窗口
            # 显示正常。
            return mpv.MPV(
                vo="gpu",
                wid=wid,
                gpu_api="opengl",
                hwdec="auto",
                keep_open="yes",
                # 禁用 mpv 自己的鼠标/OSC：默认 mpv 会在底部显示屏幕控制条(OSC)、
                # 把左键拖拽当“拖拽寻道”。嵌入到 Qt 后会与裁剪框拖拽抢鼠标事件，
                # 尤其在画面边缘/底部 OSC 区域出现“撞到空气墙”停住。
                # 工具用 Python 命令 seek，不依赖 mpv 的鼠标/OSC/OSD。
                osc="no",
                osd_level=0,
                input_default_bindings="no",   # 禁用 mpv 默认鼠标/键盘绑定(如左键拖拽寻道)
                demuxer_max_bytes="1GiB",
                demuxer_readahead_secs=60,
                cache=True,   # 本地文件默认 --cache=auto 不启用 RAM 缓存,需显式开才能吃到上面配置
            )
        # 回退：offscreen（自动化测试）无窗口 → vo=null 慢但能跑
        return mpv.MPV(
            vo="null",
            audio=False,
            hwdec="auto",
            keep_open="yes",
        )

    def _display_aspect(self) -> float:
        """视频显示宽高比（含像素比 PAR）。

        存储尺寸可能 ≠ 显示比例（如 720x406 + SAR 406:405 → 显示 16:9）。
        裁剪构图视图的帧区域按此比例适配，否则框与画面错位。
        取不到就退回存储比例。
        """
        try:
            params = self._mpv.video_params or {}
            if float(params.get("aspect") or 0) > 0:
                return float(params["aspect"])
            w = float(params.get("w") or self.width)
            h = float(params.get("h") or self.height)
            par = float(params.get("par") or 1.0)
            if w > 0 and h > 0 and par > 0:
                return w / h * par
        except Exception:
            pass
        if self.width > 0 and self.height > 0:
            return self.width / self.height
        return 1.0

    def load(self, path: str, render: bool = False,
             wid: int | None = None) -> None:
        with self._lock:
            self.close()
            self.path = str(path)
            self._current_path = self.path
            self._error = ""
            self._mpv = self._new_mpv(render, wid)
        try:
            with self._lock:
                self._mpv.loadfile(self.path)
            # loadfile 异步：先等元数据（duration），再等首帧就绪（time-pos 可用）。
            # 轮询只在读属性时短持锁，等待期间不阻塞 GUI 的渲染调用。
            end = time.time() + 8
            while time.time() < end:
                with self._lock:
                    try:
                        if self._mpv.duration:
                            break
                    except Exception:
                        pass
                time.sleep(0.02)
            if not (self._mpv.duration or 0):
                raise RuntimeError(tr("视频加载超时或无法解析"))
            end = time.time() + 5
            while time.time() < end:
                with self._lock:
                    try:
                        if self._mpv.time_pos is not None:
                            break
                    except Exception:
                        pass
                time.sleep(0.02)
            with self._lock:
                self.duration = float(self._mpv.duration or 0.0)
                self.width = int(self._mpv.width or 0)
                self.height = int(self._mpv.height or 0)
                self.dar = self._display_aspect()
                # 像素宽高比(PAR)：直接从 mpv video_params 取，变形源(如 1440x1080 @PAR 4:3)
                # 时 !=1；导出管线据此把裁剪内容还原到显示宽高比，避免被拉伸
                self.sar = _parse_sar((self._mpv.video_params or {}).get("par"))
                try:
                    fps = float(self._mpv.estimated_frame_count / max(0.001, self.duration)) \
                        if self.duration and self._mpv.estimated_frame_count else 30.0
                    if not (5.0 <= fps <= 240.0):
                        fps = 30.0
                except Exception:
                    fps = 30.0
                self.fps = fps
                self.frame_count = int(self._mpv.estimated_frame_count or 0) \
                    or int(self.duration * self.fps + 0.5)
                self.has_audio = bool(self._mpv.audio_params is not None) \
                    if hasattr(self._mpv, "audio_params") else True
                # 暂停并停在首帧
                self._mpv.pause = True
                self._frame_idx = 0
                self._mpv.seek(0.0, reference="absolute", precision="exact")
            self._wait_seek(0.0, timeout=5.0)
        except Exception as exc:
            self._error = tr("无法打开视频：{}").format(exc)
            try:
                if self._mpv is not None:
                    self._mpv.terminate()
            except Exception:
                pass
            self._mpv = None
            raise

    def close(self) -> None:
        with self._lock:
            if self._mpv is not None:
                try:
                    self._mpv.terminate()
                except Exception:
                    pass
                self._mpv = None
            self._playing = False

    # ------------------------------------------------------------------
    # 帧号 / 时间
    # ------------------------------------------------------------------
    def time_pos(self) -> float:
        with self._lock:
            if self._mpv is None:
                return 0.0
            try:
                return float(self._mpv.time_pos or 0.0)
            except Exception:
                return 0.0

    def frame_number(self) -> int:
        with self._lock:
            if self._mpv is None:
                return 0
            try:
                # estimated-frame-number 是估算值且帧步进后滞后 1 帧；
                # time-pos×fps 更实时（hr-seek 已精确对齐到目标帧 pts）
                tp = float(self._mpv.time_pos or 0.0)
                n = int(round(tp * self.fps))
                return max(0, min(n, self.frame_count - 1))
            except Exception:
                return self._frame_idx

    def _wait_seek(self, target: float, timeout: float = 2.0) -> bool:
        """等 mpv seek 完成：time-pos 到达目标附近（hr-seek exact 落点）。

        mpv 的 seek 是异步队列化的（内部 0.3s 合并窗口），命令立即返回；
        画面真正落到目标帧需等 seek 完成。轮询 time-pos 与目标误差
        ≤1/4 帧间隔，并连续两次读数稳定。
        """
        tol = max(0.004, 1.0 / max(1.0, self.fps) / 4)
        last_tp = None
        end = time.time() + timeout
        while time.time() < end:
            try:
                tp = float(self._mpv.time_pos or 0.0)
                if abs(tp - target) <= tol and (last_tp is None or abs(tp - last_tp) <= tol):
                    return True
                last_tp = tp
            except Exception:
                pass
            time.sleep(0.004)
        return False

    # ------------------------------------------------------------------
    # 取帧
    # ------------------------------------------------------------------
    def screenshot(self, fullres: bool = False,
                   max_w: int = 1920, max_h: int = 1080) -> QImage | None:
        """截当前 VO 帧：screenshot-raw 全分辨率 bgr0 → QImage（零拷贝）。

        播放态可靠；暂停态 vo=null 不渲染 VO，可能截到旧缓冲（拖动画面
        跟随但滞后，松手后由 ffmpeg 静止帧精确）。
        """
        with self._lock:
            if self._mpv is None:
                return None
            try:
                r = self._mpv.command("screenshot-raw")
            except Exception:
                return None
            if not r or r.get("format") != "bgr0":
                return None
            stride = int(r["stride"])
            h = int(r["h"])
            if fullres:
                return bgr0_to_qimage(r["data"], stride, h)
            if stride // 4 > max_w or h > max_h:
                try:
                    from PIL import Image
                    # bgr0 的 A 通道为 0（全透明）：必须先转 RGB 再缩放，
                    # 否则 PIL 对透明像素 resize 会把 RGB 归零（整图全黑）
                    pil = Image.frombytes("RGBA", (stride // 4, h),
                                          bytes(r["data"]), "raw", "BGRA").convert("RGB")
                    pil = _pil_resize(pil, max_w, max_h)
                    arr = np.ascontiguousarray(pil)
                    return QImage(arr.data, arr.shape[1], arr.shape[0],
                                  arr.shape[1] * 3, QImage.Format_RGB888).copy()
                except Exception:
                    pass
            return bgr0_to_qimage(r["data"], stride, h)

    def _reliable_screenshot(self, sec: float, fullres: bool,
                             max_w: int, max_h: int) -> QImage | None:
        """暂停态可靠取帧：seek 到目标前 + 自然播放到目标（渲染循环运行）→ 截图。

        vo=null 暂停态不渲染 VO，screenshot-raw 只能截旧缓冲（滞后一拍）；
        播放态渲染循环运行，截图可靠（实测与 ffmpeg 同帧差异即渲染管线
        固有差异）。耗时 ~150ms（含 0.15s 播放），用于独立实例的低频精确
        取帧（缩略图）。主播放器不用此路径（播放/拖动会受 seek 干扰）。
        """
        pre = max(0.0, sec - 0.15)
        try:
            self._mpv.seek(pre, reference="absolute", precision="exact")
            self._wait_seek(pre)
            self._mpv.pause = False
            end = time.time() + 1.5
            while time.time() < end and (self._mpv.time_pos or 0) < sec - 0.03:
                time.sleep(0.003)
            img = self.screenshot(fullres=fullres, max_w=max_w, max_h=max_h)
            try:
                self._mpv.pause = True
            except Exception:
                pass
            try:
                self._mpv.seek(sec, reference="absolute", precision="exact")
                self._wait_seek(sec)
            except Exception:
                pass
            return img
        except Exception:
            try:
                self._mpv.pause = True
            except Exception:
                pass
            return None

    def frame_at(self, idx: int, fullres: bool = False,
                 max_w: int = 1920, max_h: int = 1080) -> QImage | None:
        """取第 idx 帧（独立实例用，可靠渲染精确到目标帧），线程安全。"""
        with self._lock:
            if self._mpv is None or self.frame_count <= 0:
                return None
            idx = max(0, min(int(idx), self.frame_count - 1))
            self._frame_idx = idx
            return self._reliable_screenshot(idx / self.fps, fullres, max_w, max_h)

    def step(self, delta: int) -> int:
        """逐帧步进：统一走 seek exact（hr-seek 精确到目标帧，可可靠等待）。

        frame-step 在 seek 刚完成后会被 mpv 吞掉（时序不可控），改用 seek。
        """
        return self.goto(self._frame_idx + delta)

    def goto(self, idx: int) -> int:
        """seek 到指定帧号（UI 传入的目标帧），返回实际帧号。

        render 模式不等待 seek 完成：画面由 poll 定时器驱动渲染（mpv seek
        落点帧就绪后即上屏），同步等待反而会阻塞 worker（4K seek 后
        time_pos 就绪慢，等待 2s 超时会让后续 play/seek 命令排队）。
        """
        with self._lock:
            if self._mpv is None or self.frame_count <= 0:
                return self._frame_idx
            try:
                sec = max(0.0, min(idx / self.fps, self.duration))
                self._mpv.seek(sec, reference="absolute", precision="exact")
                self._frame_idx = self.frame_number()
                return self._frame_idx
            except Exception:
                return self._frame_idx

    def step_frame(self, delta: int) -> int:
        """mpv 逐帧命令前进/后退 delta 帧（从当前解码位置续解，流畅）。

        与 goto(absolute exact seek) 不同：frame-step 只在当前播放位置附近解
        下一帧，长按步进不会每次从关键帧顺解几百帧。返回实际帧号。
        """
        with self._lock:
            if self._mpv is None:
                return self._frame_idx
            try:
                if delta > 0:
                    for _ in range(delta):
                        self._mpv.command("frame-step")
                elif delta < 0:
                    for _ in range(-delta):
                        self._mpv.command("frame-back-step")
                # mpv 帧号(由 time_pos 反映)更新有一拍异步延迟(offscreen 更明显)，
                # 轮询等落到目标；超时兜底读实际帧号，避免帧标签/播放头滞后
                target = max(0, min(self.frame_count - 1, self._frame_idx + delta))
                sec_t = target / max(1.0, self.fps)
                end = time.time() + 0.3
                tol = 1.0 / max(2.0, self.fps) / 2
                while time.time() < end:
                    try:
                        tp = float(self._mpv.time_pos or 0.0)
                        if abs(tp - sec_t) <= tol:
                            break
                    except Exception:
                        pass
                    time.sleep(0.008)
                self._frame_idx = max(0, min(self.frame_number(), self.frame_count - 1))
                return self._frame_idx
            except Exception:
                return self._frame_idx

    # ------------------------------------------------------------------
    # 播放控制（mpv 原生播放，音画同步由 mpv 负责）
    # ------------------------------------------------------------------
    def play(self, speed: float = 1.0) -> None:
        with self._lock:
            if self._mpv is None:
                return
            try:
                self._mpv.pause = False   # 先解除暂停（关键，独立 try）
                self._playing = True
            except Exception:
                return
            try:
                self._mpv.speed = max(0.1, min(8.0, speed))   # mpv 属性名是 speed
            except Exception:
                pass

    def pause(self) -> None:
        with self._lock:
            if self._mpv is None:
                return
            try:
                self._mpv.pause = True
                self._playing = False
            except Exception:
                pass

    def set_loop(self, in_sec: float, out_sec: float) -> None:
        with self._lock:
            if self._mpv is None:
                return
            try:
                self._mpv.command("set", "ab-loop-a", str(in_sec))
                self._mpv.command("set", "ab-loop-b", str(out_sec))
            except Exception:
                pass

    def clear_loop(self) -> None:
        with self._lock:
            if self._mpv is None:
                return
            try:
                self._mpv.command("set", "ab-loop-a", "no")
                self._mpv.command("set", "ab-loop-b", "no")
            except Exception:
                pass

    def set_audio_gain(self, gain: float) -> None:
        """预览音频增益：用 af volume 滤镜（不受 100% 限制，可放大到 600%）。"""
        with self._lock:
            if self._mpv is None:
                return
            try:
                if gain is None or abs(gain - 1.0) < 1e-6:
                    self._mpv.af = ""
                else:
                    self._mpv.af = f"volume={max(0.0, gain):.3f}"
            except Exception:
                pass

    def _audio_track_aid(self, ordinal: int) -> int | None:
        """第 ordinal 条音频流的 mpv aid（0 起）；-1 或越界 → None（让 mpv 用默认）。"""
        try:
            tl = self._mpv.track_list or []
            audio = [t for t in tl if isinstance(t, dict) and t.get("type") == "audio"]
            if not audio:
                return None
            if ordinal is None or ordinal < 0:
                return audio[0].get("id")
            if ordinal < len(audio):
                return audio[ordinal].get("id")
            return None
        except Exception:
            return None

    def set_audio_track(self, ordinal: int) -> None:
        """预览音轨切到第 ordinal 条音频流（0 起；-1=默认）。"""
        with self._lock:
            if self._mpv is None:
                return
            try:
                aid = self._audio_track_aid(ordinal)
                if aid is not None:
                    self._mpv["aid"] = aid
            except Exception:
                pass

    def is_playing(self) -> bool:
        with self._lock:
            return self._playing


class MpvWorker(QThread):
    """单 mpv 实例的后台线程：所有取帧/播放控制串行执行，UI 主线程零阻塞。

    任务 (kind, ...)：
      ('load', path)            → loaded(path, meta_dict) / load_failed
      ('step', delta, seq)      → frame_ready(seq, idx, img)
      ('seek', sec)             → 只发 seek 命令（异步，画面由 tick 节拍驱动）
      ('tick', seq)             → frame_ready(seq, idx, img)  # 播放/拖动节拍截图
      ('settle', idx, seq)      → frame_ready(seq, idx, img)  # ffmpeg 静止帧
      ('play', speed) / ('pause',) / ('speed', s) /
      ('loop', in, out) / ('clearloop',)
    seq 为请求序号：主线程递增，过期结果由主线程按 seq 丢弃（stale response）。
    """
    loaded = Signal(str, object)            # path, meta dict
    frame_ready = Signal(int, int, object)  # seq, frame_idx, QImage
    load_failed = Signal(str, str)          # path, error

    def __init__(self, q, parent=None, host=None) -> None:
        super().__init__(parent)
        self._q = q
        self._host = host   # 预览的视频层（--wid 原生窗口容器）：仅用于平台判定（offscreen 回退）
        self._player: MvpPlayer | None = None

    def _use_render(self) -> bool:
        """--wid 内嵌判定：真实桌面（非 offscreen）→ vo=gpu + wid。

        offscreen（自动化测试）无窗口，必须回退 vo=null（走 screenshot/ffmpeg 取帧）。
        """
        if self._host is None:
            return False
        try:
            from PySide6.QtGui import QGuiApplication
            return QGuiApplication.platformName() != "offscreen"
        except Exception:
            return False

    def _take_latest(self, kind_prefix: str, item, drop_on_other: bool = False):
        """合并队列：取最新一条同类任务（拖动 seek / tick 防堆积）。

        drop_on_other=True（seek 用）：队列里出现非 seek 任务（settle/tick/
        step，即拖动已结束）时，当前 seek 作废返回 None，新任务留在队列
        优先处理 —— 否则拖动残留的 seek 会阻塞播放/静止帧。
        """
        while True:
            try:
                newer = self._q.get_nowait()
            except Exception:
                break
            if newer is None:
                return None
            if newer[0] == kind_prefix:
                item = newer
            elif drop_on_other:
                self._q.put(newer)
                return None
            else:
                self._q.put(newer)
                break
        return item

    def _screenshot(self) -> "QImage | None":
        """取当前帧（源分辨率原图）。

        仅 offscreen（vo=null）回退模式使用；render 模式画面由 mpv 直接渲染
        到视频窗口，静止帧截图供裁剪框以外的低频用途。
        """
        return self._player.screenshot(fullres=True)

    def run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            kind = item[0]
            try:
                if kind == "load":
                    path = item[1]
                    wid = item[2] if len(item) > 2 else None
                    try:
                        # 复用同一个 MvpPlayer：load() 内部先 close() 终止旧 mpv
                        # 再创建新实例。若每次 load 都新建 MvpPlayer，旧 mpv 实例
                        # 还没 terminate 就创建新实例，会残留窗口/占用渲染上下文。
                        if self._player is None:
                            self._player = MvpPlayer()
                        p = self._player
                        # 真实桌面 + 有容器窗口：vo=gpu + wid 内嵌（mpv 自管 GL
                        # 上下文渲染到容器原生窗口）；offscreen（自动化测试无窗口）
                        # 回退 vo=null。
                        p.load(path, render=self._use_render(), wid=wid)
                    except Exception as exc:
                        # load() 已 close 旧实例并置 _mpv=None，这里清掉引用，
                        # 下次 load 重新创建；失败必须上报（被外层 except 吞掉用户只能干等）
                        self._player = None
                        self.load_failed.emit(path, str(exc))
                        continue
                    meta = {
                        "path": path,
                        "width": p.width, "height": p.height,
                        "fps": p.fps, "duration": p.duration,
                        "frame_count": p.frame_count,
                        "has_audio": p.has_audio,
                        "video_bitrate": p.video_bitrate,
                        "dar": p.dar, "sar": p.sar,
                    }
                    self.loaded.emit(path, meta)
                elif kind == "step":
                    # delta 是相对步进量：用 mpv 逐帧命令从当前解码位置续解，
                    # 不用绝对 exact seek（跨关键帧要顺解几百帧，长按步进会卡）。
                    delta, seq = item[1], item[2]
                    if self._player is None:
                        continue
                    idx = self._player.step_frame(delta)
                    img = self._screenshot()
                    if img is not None:
                        self.frame_ready.emit(seq, idx, img)
                elif kind == "seek":
                    item = self._take_latest("seek", item, drop_on_other=True)
                    if item is None:
                        continue   # 队列里已有新任务（拖动结束）：seek 作废
                    sec = item[1]
                    # 统一精确落点（主线程已不再区分拖拽/点击，均为 exact）；
                    # 拖拽期间 _take_latest 丢弃排队中的旧 seek，只落最新一条。
                    precision = item[2] if len(item) > 2 else "exact"
                    if self._player is None:
                        continue
                    try:
                        self._player._mpv.seek(sec, reference="absolute",
                                               precision=precision)
                    except Exception:
                        pass
                elif kind == "jogseek":
                    # 快捷键(A/D/←→)按秒跳转：轻量 seek，画面由 mpv VO 渲染。
                    # 秒模式是单次跳(无长按连续)，故不需 _take_latest 合并。
                    sec = item[1]
                    if self._player is None:
                        continue
                    try:
                        self._player._mpv.seek(sec, reference="absolute",
                                               precision="exact")
                    except Exception:
                        pass
                elif kind == "settle":
                    # 静止帧：seek 精确到目标后截图上送。render 模式画面由
                    # mpv 直接渲染到视频窗口，但裁剪构图视图需要 CPU 帧；
                    # 目标帧通常就在当前位置附近，seek 落点很快（超时兜底）。
                    idx, seq = item[1], item[2]
                    if self._player is None:
                        continue
                    sec = idx / max(1.0, self._player.fps)
                    try:
                        self._player._mpv.seek(sec, reference="absolute",
                                               precision="exact")
                    except Exception:
                        pass
                    self._player._wait_seek(sec, timeout=1.0)
                    img = self._screenshot()
                    if img is not None:
                        self.frame_ready.emit(seq, idx, img)
                elif kind == "play":
                    if self._player is not None:
                        self._player.play(item[1])
                elif kind == "speed":
                    if self._player is not None:
                        try:
                            self._player._mpv.speed = max(0.1, min(8.0, item[1]))
                        except Exception:
                            pass
                elif kind == "pause":
                    if self._player is not None:
                        self._player.pause()
                elif kind == "loop":
                    if self._player is not None:
                        self._player.set_loop(item[1], item[2])
                elif kind == "clearloop":
                    if self._player is not None:
                        self._player.clear_loop()
                elif kind == "gain":
                    if self._player is not None:
                        self._player.set_audio_gain(item[1])   # 预览音量增益(af=volume)
                elif kind == "aid":
                    if self._player is not None:
                        self._player.set_audio_track(item[1])  # 预览音轨(序号)
            except Exception:
                pass
        # 退出循环（收到 None 关闭信号）：释放主播放器。
        # mpv terminate 靠 Python GC 清理很慢（表现为"点击关闭等很久"），
        # 这里显式 close 让退出立刻完成。
        if self._player is not None:
            try:
                self._player.close()
            except Exception:
                pass
            self._player = None
