# 下载并解压 ffmpeg（含 libplacebo / Vulkan / x264 / x265 / NVENC）
#
# 用法（在仓库根目录）：
#   powershell -ExecutionPolicy Bypass -File .\scripts\fetch_tools.ps1
#
# 只从压缩包里提取 tools\ffmpeg.exe 与 tools\ffprobe.exe，避免把 200MB 的完整包塞进仓库。

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$tools = Join-Path $root 'tools'
New-Item -ItemType Directory -Force -Path $tools | Out-Null

$zip = Join-Path $env:TEMP 'ffmpeg-master-latest-win64-gpl.zip'
$url = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip'

if (-not (Test-Path $zip)) {
    Write-Host "下载 ffmpeg: $url"
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
} else {
    Write-Host "已存在缓存: $zip"
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::OpenRead($zip)
$found = @()
foreach ($entry in $archive.Entries) {
    $name = $entry.Name
    if ($name -eq 'ffmpeg.exe' -or $name -eq 'ffprobe.exe') {
        $target = Join-Path $tools $name
        Write-Host "解压 $name -> $target"
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
        $found += $name
    }
}
$archive.Dispose()

if ($found.Count -eq 0) {
    throw "没有在压缩包里找到 ffmpeg.exe / ffprobe.exe，请检查下载地址。"
}

Write-Host ''
Write-Host '完成：tools\ffmpeg.exe、tools\ffprobe.exe'
Write-Host '验证 libplacebo 是否可用：'
Write-Host '  .\tools\ffmpeg.exe -hide_banner -filters | findstr libplacebo'
