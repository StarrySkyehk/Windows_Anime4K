"""
Anime4K 离线超分工具（Windows 10/11）

把视频文件拖进来 / 选择文件，自动用 Anime4K（GLSL 着色器）做超分辨率，导出新的 mp4。

管线：ffmpeg + libplacebo 滤镜（Vulkan）+ Anime4K v4 GLSL 链
      → 需要显卡驱动支持 Vulkan；如果初始化失败会自动回退到 Lanczos 放大 + 锐化。
      → 视频编码：libx264（CPU，默认）、libx265、或 NVENC（N 卡硬件编码）。

也支持命令行模式（便于排查/批量）：
    python "Anime4K超分工具.py" --cli 输入视频 [--outdir 输出目录] [--preset efficiency|quality]
                                 [--height 1080|1440|2160|0] [--encoder auto|x264|x265|nvenc]
"""

import os
import re
import sys
import json
import time
import shutil
import queue
import threading
import subprocess
import traceback

APP_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(APP_DIR, "tools")
SHADERS_DIR = os.path.join(APP_DIR, "shaders")

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".flv", ".ts", ".m2ts", ".webm",
    ".wmv", ".mpg", ".mpeg", ".m4v", ".rmvb", ".rm", ".3gp", ".vob", ".ogv",
}

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

PRESETS = {
    "efficiency": {
        "label": "效率档（推荐，速度约 1.5x 实时）",
        "upscale": "chain_efficiency.glsl",
        "noresize": "chain_efficiency_noresize.glsl",
    },
    "quality": {
        "label": "质量档（慢，线条更强，约 1x 实时）",
        "upscale": "chain_quality.glsl",
        "noresize": "chain_quality_noresize.glsl",
    },
}

HEIGHTS = {
    1080: "1080p（推荐，AR 眼镜原生分辨率）",
    1440: "1440p（2K，更慢）",
    2160: "2160p（4K，非常慢）",
    0: "保持原分辨率（只做 Anime4K 修复/锐化）",
}

ENCODERS = {
    "auto": "自动（有 N 卡就用 NVENC，否则 CPU x264）",
    "x264": "CPU H.264（兼容性最好）",
    "x265": "CPU H.265（更小，更慢）",
    "nvenc": "N 卡 NVENC（最快，需要 NVIDIA 显卡）",
}


# --------------------------------------------------------------------------- #
# 工具查找 / 探测
# --------------------------------------------------------------------------- #

def find_tool(name: str) -> str:
    local = os.path.join(TOOLS_DIR, name + (".exe" if os.name == "nt" else ""))
    if os.path.isfile(local):
        return local
    found = shutil.which(name)
    if found:
        return found
    return ""


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")


def run_capture(args, timeout=30):
    """运行命令并返回 (returncode, stdout, stderr)；失败返回 (-1, '', 错误)。"""
    try:
        proc = subprocess.run(
            args,
            cwd=APP_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except Exception as exc:  # noqa: BLE001 - 工具探测失败不应崩溃
        return -1, "", str(exc)


_NVENC_WORKS = None


def has_encoder(name: str) -> bool:
    if not FFMPEG:
        return False
    code, out, _ = run_capture([FFMPEG, "-hide_banner", "-encoders"], timeout=20)
    return code == 0 and name in out


def nvenc_works() -> bool:
    """真正跑一次 NVENC 小测试：列在 -encoders 里不代表驱动能用（远程桌面/旧驱动会失败）。"""
    global _NVENC_WORKS
    if _NVENC_WORKS is not None:
        return _NVENC_WORKS
    if not has_encoder("h264_nvenc"):
        _NVENC_WORKS = False
        return False
    code, out, err = run_capture(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "nullsrc=s=320x240:d=0.3",
            "-c:v", "h264_nvenc", "-f", "null", "-",
        ],
        timeout=30,
    )
    _NVENC_WORKS = code == 0
    if not _NVENC_WORKS:
        msg = (err or out or "").strip().splitlines()
        short = msg[-1] if msg else "未知原因"
        print("[Anime4K] NVENC 不可用，改用 CPU x264：" + short)
    return _NVENC_WORKS


def probe_video(path: str):
    """返回 dict：width, height, duration（秒，可能为 None）。"""
    if not FFPROBE:
        return None
    code, out, _ = run_capture(
        [
            FFPROBE, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json",
            path,
        ],
        timeout=30,
    )
    if code != 0:
        return None
    try:
        data = json.loads(out)
        streams = data.get("streams") or []
        if not streams:
            return None
        stream = streams[0]
        duration = data.get("format", {}).get("duration")
        return {
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0),
            "duration": float(duration) if duration not in (None, "N/A") else None,
        }
    except Exception:  # noqa: BLE001
        return None


def target_size(width: int, height: int, target_height: int):
    """按目标高度等比缩放（宽高取偶数），超宽视频限制到 3840 宽。"""
    width = max(2, width)
    height = max(2, height)
    if target_height <= 0:
        return width, height
    scale = target_height / float(height)
    tw = int(round(width * scale / 2.0)) * 2
    th = int(round(target_height / 2.0)) * 2
    if tw > 3840:
        tw = 3840
        th = int(round(height * (tw / float(width)) / 2.0)) * 2
    return max(2, tw), max(2, th)


# --------------------------------------------------------------------------- #
# 处理管线
# --------------------------------------------------------------------------- #

class ProcessResult:
    def __init__(self):
        self.ok = False
        self.output = ""
        self.message = ""
        self.used_fallback = False
        self.cancelled = False


def build_encoder_args(encoder: str):
    if encoder == "auto":
        encoder = "nvenc" if nvenc_works() else "x264"
    if encoder == "nvenc" and not nvenc_works():
        encoder = "x264"
    if encoder == "nvenc":
        return "h264_nvenc", ["-preset", "p5", "-cq", "20", "-b:v", "0"]
    if encoder == "x265":
        return "libx265", ["-preset", "medium", "-crf", "20", "-tag:v", "hvc1"]
    return "libx264", ["-preset", "fast", "-crf", "18"]


def build_filter(preset: str, no_resize: bool, width: int, height: int) -> str:
    """构造 libplacebo 滤镜串。

    注意：custom_shader_path 必须是**相对于 APP_DIR 的路径**，ffmpeg 的滤镜参数解析器
    处理不了 Windows 绝对路径里的盘符冒号（实测会失败），所以我们固定 cwd=APP_DIR。
    """
    chain = PRESETS[preset]["noresize" if no_resize else "upscale"]
    shader_rel = "shaders/" + chain
    if no_resize:
        return f"libplacebo=custom_shader_path={shader_rel},format=yuv420p"
    return (
        f"libplacebo=w={width}:h={height}:custom_shader_path={shader_rel}"
        f",format=yuv420p"
    )


def build_fallback_filter(no_resize: bool, width: int, height: int) -> str:
    """Vulkan 不可用时的兜底：Lanczos 放大 + 轻度锐化。"""
    if no_resize:
        return "unsharp=5:5:0.8:5:5:0.0,format=yuv420p"
    return (
        f"scale={width}:{height}:flags=lanczos,"
        f"unsharp=5:5:1.0:5:5:0.0,format=yuv420p"
    )


def build_output_path(src: str, out_dir: str, target_height: int) -> str:
    stem, _ = os.path.splitext(os.path.basename(src))
    suffix = f"{target_height}p" if target_height > 0 else "native"
    candidate = os.path.join(out_dir, f"{stem}_Anime4K_{suffix}.mp4")
    if not os.path.exists(candidate):
        return candidate
    index = 2
    while True:
        candidate = os.path.join(out_dir, f"{stem}_Anime4K_{suffix}_{index}.mp4")
        if not os.path.exists(candidate):
            return candidate
        index += 1


def process_video(
    src: str,
    out_dir: str,
    preset: str = "efficiency",
    target_height: int = 1080,
    encoder: str = "auto",
    on_progress=None,
    should_cancel=None,
    on_log=None,
) -> ProcessResult:
    """处理单个视频。on_progress(percent, speed) / should_cancel() -> bool / on_log(str)。"""
    result = ProcessResult()

    def log(message: str):
        if on_log:
            try:
                on_log(message)
            except Exception:  # noqa: BLE001
                pass

    if not FFMPEG:
        result.message = "找不到 ffmpeg，请确认 tools/ffmpeg.exe 存在。"
        return result
    if not os.path.isfile(src):
        result.message = "输入文件不存在。"
        return result

    info = probe_video(src)
    if not info or not info["width"] or not info["height"]:
        result.message = "无法读取视频信息（文件损坏或不是视频）。"
        return result

    width, height = target_size(info["width"], info["height"], target_height)
    no_resize = target_height <= 0
    output = build_output_path(src, out_dir, target_height)
    codec, codec_args = build_encoder_args(encoder)

    def make_cmd(vf: str, use_vulkan: bool = True):
        device_args = []
        if use_vulkan:
            device_args = ["-init_hw_device", "vulkan=vk", "-filter_hw_device", "vk"]
        return [
            FFMPEG, "-hide_banner", "-nostdin", "-y",
            "-loglevel", "warning",
            "-progress", "pipe:1", "-nostats",
            *device_args,
            "-i", src,
            "-map", "0:v:0", "-map", "0:a?",
            "-vf", vf,
            "-c:v", codec, *codec_args,
            "-c:a", "copy",
            "-movflags", "+faststart",
            output,
        ]

    def run_ffmpeg(vf: str, use_vulkan: bool = True):
        if should_cancel and should_cancel():
            return -2, "", "cancelled", True
        cmd = make_cmd(vf, use_vulkan=use_vulkan)
        log("ffmpeg: " + " ".join(cmd))
        proc = subprocess.Popen(
            cmd,
            cwd=APP_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
        stderr_lines = []
        cancelled = [False]

        def drain_stderr():
            try:
                for line in proc.stderr:  # type: ignore[union-attr]
                    stderr_lines.append(line)
            except Exception:  # noqa: BLE001
                pass

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()

        duration = info["duration"] or 0.0
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                if should_cancel and should_cancel():
                    cancelled[0] = True
                    kill_process_tree(proc.pid)
                    break
                line = line.strip()
                if not line:
                    continue
                if line.startswith("out_time_us="):
                    try:
                        seconds = int(line.split("=", 1)[1]) / 1_000_000.0
                    except ValueError:
                        continue
                    if duration > 0 and on_progress:
                        percent = max(0.0, min(99.9, seconds / duration * 100.0))
                        on_progress(percent, None)
                elif line.startswith("speed="):
                    speed = line.split("=", 1)[1]
                    if on_progress:
                        on_progress(None, speed)
        finally:
            proc.wait()
            stderr_thread.join(timeout=5)
        if cancelled[0]:
            return -2, "", "".join(stderr_lines), True
        return proc.returncode, "", "".join(stderr_lines), False

    vf = build_filter(preset, no_resize, width, height)
    code, _, stderr, cancelled = run_ffmpeg(vf)
    if cancelled:
        result.cancelled = True
        result.message = "已取消"
        return result

    vulkan_failed = any(
        marker in stderr
        for marker in (
            "Failed creating Vulkan device",
            "Failed initializing vulkan device",
            "Failed to create Vulkan",
            "Cannot load libvulkan",
            "No Vulkan",
            "vulkan device",
            "vkGetInstanceProcAddr",
            "libplacebo",
            "Generic error in an external library",
        )
    )
    if code != 0 and vulkan_failed:
        log("Anime4K/Vulkan 初始化失败，自动回退到 Lanczos 放大 + 锐化（无 Anime4K）。")
        result.used_fallback = True
        fallback_vf = build_fallback_filter(no_resize, width, height)
        code, _, stderr, cancelled = run_ffmpeg(fallback_vf, use_vulkan=False)
        if cancelled:
            result.cancelled = True
            result.message = "已取消"
            return result

    nvenc_failed = code != 0 and any(
        marker in stderr
        for marker in ("nvEncodeAPI64.dll", "h264_nvenc", "OpenEncodeSessionEx", "No capable devices")
    )
    if nvenc_failed and codec == "h264_nvenc":
        log("NVENC 启动失败（驱动/远程桌面限制），自动改用 CPU x264 重试…")
        codec, codec_args = build_encoder_args("x264")
        code = subprocess.run(
            make_cmd(vf),
            cwd=APP_DIR, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        ).returncode

    if code != 0 and "audio" in stderr.lower():
        log("音频流无法直接复制，改为重新编码 AAC 再试一次…")
        cmd = make_cmd(vf)
        audio_index = cmd.index("-c:a")
        cmd[audio_index + 1] = "aac"
        cmd[audio_index + 2:audio_index + 2] = ["-b:a", "256k"]
        log("ffmpeg: " + " ".join(cmd))
        code = subprocess.run(
            cmd,
            cwd=APP_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        ).returncode

    if code != 0:
        tail = "\n".join(stderr.strip().splitlines()[-12:])
        result.message = f"处理失败（ffmpeg 退出码 {code}）\n{tail}"
        if os.path.exists(output) and os.path.getsize(output) == 0:
            try:
                os.remove(output)
            except OSError:
                pass
        return result

    result.ok = True
    result.output = output
    result.message = "完成" + ("（Lanczos 兜底，无 Anime4K）" if result.used_fallback else "")
    if on_progress:
        on_progress(100.0, None)
    return result


def kill_process_tree(pid: int):
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# 命令行模式
# --------------------------------------------------------------------------- #

def cli_main(argv):
    import argparse

    parser = argparse.ArgumentParser(description="Anime4K 离线超分工具（命令行模式）")
    parser.add_argument("--cli", action="store_true")
    parser.add_argument("--input", nargs="+", required=True, help="输入视频（可多个）")
    parser.add_argument("--outdir", default="", help="输出目录（默认与源文件同目录）")
    parser.add_argument("--preset", default="efficiency", choices=list(PRESETS.keys()))
    parser.add_argument("--height", type=int, default=1080,
                        help="目标高度：1080/1440/2160，0 = 保持原分辨率（仅修复）")
    parser.add_argument("--encoder", default="auto", choices=list(ENCODERS.keys()))
    args = parser.parse_args(argv)

    failed = 0
    for src in args.input:
        src = os.path.abspath(src)
        out_dir = args.outdir or os.path.dirname(src)
        os.makedirs(out_dir, exist_ok=True)

        def on_progress(percent, speed):
            if percent is not None:
                sys.stdout.write(f"\r{percent:5.1f}%")
                sys.stdout.flush()

        def on_log(message):
            if "[Kazumi]" in message or message.startswith("ffmpeg:"):
                return
            print("\n" + message)

        print(f"\n=== {os.path.basename(src)} ===")
        result = process_video(
            src, out_dir,
            preset=args.preset,
            target_height=args.height,
            encoder=args.encoder,
            on_progress=on_progress,
            on_log=on_log,
        )
        print()
        if result.ok:
            print(f"OK -> {result.output}")
        elif result.cancelled:
            print("已取消")
            failed += 1
        else:
            print("失败: " + result.message)
            failed += 1
    return 0 if failed == 0 else 1


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #

def gui_main():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
        root = TkinterDnD.Tk()
        dnd_available = True
    except Exception:  # noqa: BLE001
        root = tk.Tk()
        dnd_available = False

    root.title("Anime4K 离线超分工具")
    root.geometry("980x680")
    root.minsize(820, 560)

    state = {
        "files": [],          # list[dict]
        "running": False,
        "cancel": False,
        "out_dir": os.path.join(os.path.expanduser("~"), "Desktop", "Anime4K输出"),
        "current_index": None,
    }

    # ---------- 变量 ----------
    out_dir_var = tk.StringVar(value=state["out_dir"])
    preset_var = tk.StringVar(value=PRESETS["efficiency"]["label"])
    height_var = tk.StringVar(value=HEIGHTS[1080])
    encoder_var = tk.StringVar(value=ENCODERS["auto"])
    status_var = tk.StringVar(value="把视频拖进窗口，或点击「添加文件」。")
    overall_var = tk.DoubleVar(value=0.0)

    preset_by_label = {v["label"]: k for k, v in PRESETS.items()}
    height_by_label = {v: k for k, v in HEIGHTS.items()}
    encoder_by_label = {v: k for k, v in ENCODERS.items()}

    log_queue: "queue.Queue[str]" = queue.Queue()
    event_queue: "queue.Queue[dict]" = queue.Queue()

    # ---------- 布局 ----------
    main = ttk.Frame(root, padding=10)
    main.pack(fill=tk.BOTH, expand=True)

    drop_frame = tk.Frame(main, height=84, bg="#f2f6ff", highlightthickness=2,
                          highlightbackground="#8fb3ff")
    drop_frame.pack(fill=tk.X)
    drop_frame.pack_propagate(False)
    drop_label = tk.Label(
        drop_frame,
        text=("把视频文件拖到这里（支持多选）\n或点击下方「添加文件 / 添加文件夹」" if dnd_available
              else "点击下方「添加文件 / 添加文件夹」（当前环境未启用拖拽）"),
        bg="#f2f6ff", fg="#335", justify=tk.CENTER, font=("Microsoft YaHei UI", 10),
    )
    drop_label.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

    def pick_files(_event=None):
        paths = filedialog.askopenfilenames(
            title="选择视频文件",
            filetypes=[("视频文件", " ".join("*" + ext for ext in sorted(VIDEO_EXTS))),
                       ("所有文件", "*.*")],
        )
        add_files(list(paths))

    def pick_folder():
        folder = filedialog.askdirectory(title="选择包含视频的文件夹")
        if not folder:
            return
        found = []
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if os.path.isfile(path) and os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                found.append(path)
        add_files(found)

    drop_frame.bind("<Button-1>", pick_files)
    drop_label.bind("<Button-1>", pick_files)
    if dnd_available:
        def on_drop(event):
            add_files([p for p in root.tk.splitlist(event.data)])
        drop_frame.drop_target_register(DND_FILES)
        drop_label.drop_target_register(DND_FILES)
        drop_frame.dnd_bind("<<Drop>>", on_drop)
        drop_label.dnd_bind("<<Drop>>", on_drop)

    # 选项区
    options = ttk.LabelFrame(main, text="选项", padding=8)
    options.pack(fill=tk.X, pady=(10, 6))

    ttk.Label(options, text="输出目录:").grid(row=0, column=0, sticky=tk.W)
    ttk.Entry(options, textvariable=out_dir_var).grid(row=0, column=1, columnspan=4,
                                                     sticky=tk.EW, padx=6)
    ttk.Button(options, text="浏览", command=lambda: choose_out_dir()).grid(row=0, column=5)
    ttk.Button(options, text="打开", command=lambda: open_folder(out_dir_var.get())).grid(
        row=0, column=6, padx=(4, 0))

    ttk.Label(options, text="质量:").grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
    ttk.Combobox(options, textvariable=preset_var, values=list(preset_by_label.keys()),
                 state="readonly", width=30).grid(row=1, column=1, sticky=tk.W, padx=6, pady=(8, 0))
    ttk.Label(options, text="分辨率:").grid(row=1, column=2, sticky=tk.W, pady=(8, 0))
    ttk.Combobox(options, textvariable=height_var, values=list(height_by_label.keys()),
                 state="readonly", width=28).grid(row=1, column=3, sticky=tk.W, padx=6, pady=(8, 0))
    ttk.Label(options, text="编码:").grid(row=1, column=4, sticky=tk.W, pady=(8, 0))
    ttk.Combobox(options, textvariable=encoder_var, values=list(encoder_by_label.keys()),
                 state="readonly", width=26).grid(row=1, column=5, columnspan=2, sticky=tk.W,
                                                  padx=6, pady=(8, 0))
    options.columnconfigure(1, weight=1)

    # 操作按钮
    buttons = ttk.Frame(main)
    buttons.pack(fill=tk.X, pady=4)
    add_btn = ttk.Button(buttons, text="添加文件", command=pick_files)
    add_btn.pack(side=tk.LEFT)
    add_folder_btn = ttk.Button(buttons, text="添加文件夹", command=pick_folder)
    add_folder_btn.pack(side=tk.LEFT, padx=6)
    remove_btn = ttk.Button(buttons, text="移除选中", command=lambda: remove_selected())
    remove_btn.pack(side=tk.LEFT, padx=6)
    start_btn = ttk.Button(buttons, text="开始处理", command=lambda: start_processing())
    start_btn.pack(side=tk.LEFT, padx=(24, 6))
    cancel_btn = ttk.Button(buttons, text="取消", command=lambda: request_cancel(), state=tk.DISABLED)
    cancel_btn.pack(side=tk.LEFT)

    ttk.Progressbar(buttons, variable=overall_var, maximum=100).pack(
        side=tk.LEFT, fill=tk.X, expand=True, padx=14)

    # 文件列表
    tree_frame = ttk.LabelFrame(main, text="队列", padding=6)
    tree_frame.pack(fill=tk.BOTH, expand=False, pady=6)
    columns = ("file", "status", "progress", "speed")
    tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=6)
    tree.heading("file", text="文件")
    tree.heading("status", text="状态")
    tree.heading("progress", text="进度")
    tree.heading("speed", text="速度")
    tree.column("file", width=520, anchor=tk.W)
    tree.column("status", width=130, anchor=tk.W)
    tree.column("progress", width=80, anchor=tk.CENTER)
    tree.column("speed", width=80, anchor=tk.CENTER)
    tree_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
    tree.configure(yscrollcommand=tree_scroll.set)
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    status_bar = ttk.Label(main, textvariable=status_var, anchor=tk.W)
    status_bar.pack(fill=tk.X, pady=(0, 4))

    # 日志
    log_frame = ttk.LabelFrame(main, text="日志", padding=4)
    log_frame.pack(fill=tk.BOTH, expand=True)
    log_text = tk.Text(log_frame, height=10, wrap=tk.WORD, state=tk.DISABLED,
                       font=("Consolas", 9))
    log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=log_text.yview)
    log_text.configure(yscrollcommand=log_scroll.set)
    log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    # ---------- 日志 / 队列工具 ----------

    def append_log(message: str):
        log_queue.put(message)

    def flush_logs():
        while True:
            try:
                message = log_queue.get_nowait()
            except queue.Empty:
                break
            log_text.configure(state=tk.NORMAL)
            log_text.insert(tk.END, message + "\n")
            log_text.see(tk.END)
            log_text.configure(state=tk.DISABLED)

    def open_folder(path: str):
        if path and os.path.isdir(path):
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            messagebox.showinfo("提示", "输出目录还不存在，处理完成后会自动创建。")

    def choose_out_dir():
        folder = filedialog.askdirectory(title="选择输出目录", initialdir=out_dir_var.get())
        if folder:
            out_dir_var.set(folder)

    def add_files(paths):
        added = 0
        for path in paths:
            if not path:
                continue
            path = os.path.abspath(path.strip().strip('"'))
            if not os.path.isfile(path):
                continue
            if os.path.splitext(path)[1].lower() not in VIDEO_EXTS:
                append_log(f"跳过（不是支持的视频后缀）: {path}")
                continue
            if any(item["path"] == path for item in state["files"]):
                continue
            item = {"path": path, "status": "等待中", "progress": 0.0, "speed": "",
                    "iid": None, "result": None}
            item["iid"] = tree.insert("", tk.END, values=(
                os.path.basename(path), item["status"], "0%", ""))
            state["files"].append(item)
            added += 1
        if added:
            status_var.set(f"已加入 {added} 个文件。共 {len(state['files'])} 个。")

    def remove_selected():
        if state["running"]:
            messagebox.showinfo("提示", "处理中不能移除，请先取消。")
            return
        for iid in tree.selection():
            tree.delete(iid)
            state["files"] = [item for item in state["files"] if item["iid"] != iid]
        status_var.set(f"队列剩余 {len(state['files'])} 个文件。")

    def update_item(item):
        tree.item(item["iid"], values=(
            os.path.basename(item["path"]), item["status"],
            f"{item['progress']:.0f}%", item["speed"]))

    # ---------- 处理流程 ----------

    def start_processing():
        if state["running"]:
            return
        pending = [item for item in state["files"] if item["status"] in ("等待中", "失败")]
        if not pending:
            messagebox.showinfo("提示", "队列里没有待处理的文件。")
            return
        out_dir = out_dir_var.get().strip()
        if not out_dir:
            messagebox.showwarning("提示", "请先选择输出目录。")
            return
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("无法创建输出目录", str(exc))
            return
        state["running"] = True
        state["cancel"] = False
        overall_var.set(0)
        start_btn.configure(state=tk.DISABLED)
        cancel_btn.configure(state=tk.NORMAL)
        add_btn.configure(state=tk.DISABLED)
        add_folder_btn.configure(state=tk.DISABLED)
        remove_btn.configure(state=tk.DISABLED)
        status_var.set("开始处理…")
        threading.Thread(
            target=worker,
            args=(pending, out_dir,
                  preset_by_label[preset_var.get()],
                  height_by_label[height_var.get()],
                  encoder_by_label[encoder_var.get()]),
            daemon=True,
        ).start()

    def worker(items, out_dir, preset, height, encoder):
        total = len(items)
        for index, item in enumerate(items, start=1):
            if state["cancel"]:
                event_queue.put({"type": "log", "message": "已取消剩余任务。"})
                break
            event_queue.put({"type": "item_status", "item": item, "status": "处理中"})
            event_queue.put({"type": "log", "message": f"[{index}/{total}] 开始: {item['path']}"})
            event_queue.put({"type": "log",
                             "message": f"  预设={preset} 目标高度={height} 编码={encoder}"})

            def on_progress(percent, speed, item=item, index=index):
                if percent is not None:
                    item["progress"] = percent
                    overall = ((index - 1) + percent / 100.0) / total * 100.0
                    event_queue.put({"type": "progress", "item": item,
                                     "overall": overall})
                if speed is not None:
                    item["speed"] = speed

            def on_log(message):
                event_queue.put({"type": "log", "message": "  " + message})

            try:
                result = process_video(
                    item["path"], out_dir,
                    preset=preset, target_height=height, encoder=encoder,
                    on_progress=on_progress,
                    should_cancel=lambda: state["cancel"],
                    on_log=on_log,
                )
            except Exception as exc:  # noqa: BLE001
                trace = traceback.format_exc()
                event_queue.put({"type": "log", "message": trace})
                result = ProcessResult()
                result.message = f"异常: {exc}"

            item["result"] = result
            if result.cancelled:
                item["status"] = "已取消"
            elif result.ok:
                item["status"] = "完成"
                item["progress"] = 100.0
                event_queue.put({"type": "log", "message": f"  输出: {result.output}"})
            else:
                item["status"] = "失败"
                event_queue.put({"type": "log", "message": "  失败: " + result.message})
            event_queue.put({"type": "item_done", "item": item})

        event_queue.put({"type": "finished"})

    def request_cancel():
        if not state["running"]:
            return
        state["cancel"] = True
        status_var.set("正在取消…")
        append_log("用户请求取消，正在终止 ffmpeg…")

    def poll_events():
        flush_logs()
        while True:
            try:
                event = event_queue.get_nowait()
            except queue.Empty:
                break
            kind = event.get("type")
            if kind == "log":
                append_log(event["message"])
            elif kind == "item_status":
                item = event["item"]
                item["status"] = event["status"]
                update_item(item)
            elif kind == "progress":
                item = event["item"]
                update_item(item)
                overall_var.set(event.get("overall", 0))
            elif kind == "item_done":
                update_item(event["item"])
            elif kind == "finished":
                state["running"] = False
                state["cancel"] = False
                start_btn.configure(state=tk.NORMAL)
                cancel_btn.configure(state=tk.DISABLED)
                add_btn.configure(state=tk.NORMAL)
                add_folder_btn.configure(state=tk.NORMAL)
                remove_btn.configure(state=tk.NORMAL)
                done = sum(1 for item in state["files"] if item["status"] == "完成")
                failed = sum(1 for item in state["files"] if item["status"] == "失败")
                status_var.set(f"全部结束：完成 {done} 个，失败 {failed} 个。")
                overall_var.set(100 if done else overall_var.get())
                if done and not failed:
                    if messagebox.askyesno("完成", "全部处理完成，打开输出目录吗？"):
                        open_folder(out_dir_var.get())
        root.after(120, poll_events)

    def on_close():
        if state["running"]:
            if not messagebox.askyesno("退出", "还有任务在运行，确定退出吗？"):
                return
            state["cancel"] = True
            time.sleep(0.2)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    append_log("Anime4K 离线超分工具已启动。")
    append_log(f"ffmpeg: {FFMPEG or '未找到'}")
    append_log(f"ffprobe: {FFPROBE or '未找到'}")
    append_log("流程：ffmpeg + libplacebo(Vulkan) + Anime4K v4 GLSL；若无 Vulkan 会自动回退 Lanczos。")
    append_log("提示：源 720p→1080p 效果最明显；1080p 源建议用「保持原分辨率（仅修复）」。")
    if not dnd_available:
        append_log("未安装 tkinterdnd2，拖拽不可用（可用「添加文件」按钮）。")

    root.after(120, poll_events)
    root.mainloop()


if __name__ == "__main__":
    if "--cli" in sys.argv:
        sys.exit(cli_main(sys.argv[1:]))
    try:
        gui_main()
    except Exception:  # noqa: BLE001
        trace = traceback.format_exc()
        log_path = os.path.join(APP_DIR, "启动错误.log")
        try:
            with open(log_path, "w", encoding="utf-8") as handle:
                handle.write(trace)
        except OSError:
            pass
        try:
            import tkinter.messagebox as mb
            mb.showerror("启动失败", trace)
        except Exception:  # noqa: BLE001
            print(trace)
        raise
