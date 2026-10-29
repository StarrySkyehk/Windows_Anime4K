# 第三方组件说明

## 1. Anime4K 着色器（shaders/）

- 来源：https://github.com/bloc97/Anime4K （v4.0.1）
- 许可：MIT License
- 用途：`shaders/*.glsl` 为官方着色器原文件；`shaders/chain_*.glsl` 为按官方预设（Mode A Fast / HQ）拼接的着色器链，
  通过 ffmpeg 的 libplacebo `custom_shader_path` 加载。
- 版权归原作者（bloc97 等）所有，本仓库仅按 MIT 条款再分发。

## 2. ffmpeg（不在本仓库内）

- 本项目通过 `scripts/fetch_tools.ps1` 下载 BtbN 的 Windows 构建：
  https://github.com/BtbN/FFmpeg-Builds
- ffmpeg 采用 GPL/LGPL 许可（构建版本为 GPL），二进制不随本仓库提供。
- 需要 libplacebo 滤镜（Vulkan）与 x264/x265/NVENC 编码器。

## 3. Python 依赖

- 主程序仅使用标准库（tkinter 为 Python 自带）；拖拽功能可选安装 `tkinterdnd2`（MIT）。
