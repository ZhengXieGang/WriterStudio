"""把 PyInstaller 产物组装成 AppImage（Linux 零安装分发格式）。

PyInstaller 的 Linux 目录版仍链接构建机的系统库（libGL/X11/字体配置…），
在没装这些库的发行版上起不来——AppImage 把它们全部随身携带：

    python scripts/make_appimage.py [--dist dist/WriterStudio] [--out dist]
        [--appimagetool PATH]

组装 AppDir（AppRun/.desktop/图标 + ldd 闭包收集的全部非 glibc 依赖）；
给出 appimagetool 时直接产出 ``WriterStudio-<arch>.AppImage``，否则留下
AppDir 并提示手工命令（CI 上下载 appimagetool 后调用即可）。
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

# glibc 核心族永远不打包——必须用目标机内核/加载器配套的版本
_BLACKLIST = re.compile(
    r"^(ld-linux|libc\.|libm\.|libpthread|libdl|librt\.|libresolv|libutil|"
    r"libanl|libnsl|libcrypt|libBrokenLocale|libSegFault|libpcprofile)")

_DESKTOP = """[Desktop Entry]
Type=Application
Name=WriterStudio
Comment=ESP32 GRBL 写字机软件：多字体、手写扰动、Markdown/LaTeX
Exec=WriterStudio
Icon=writerstudio
Categories=Graphics;Office;
Terminal=false
"""

_APPRUN = """#!/bin/sh
HERE=$(dirname "$(readlink -f "$0")")
export LD_LIBRARY_PATH="$HERE/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# 透传：AppImage --mcp [参数] == 直接运行包内的 AI 排版桥（MCP stdio 服务），
# 供 AI 应用的 mcpServers 配置把本 AppImage 当作 command 使用
if [ "$1" = "--mcp" ]; then
    shift
    exec "$HERE/opt/WriterStudio/WriterStudioMCP" "$@"
fi
exec "$HERE/opt/WriterStudio/WriterStudio" "$@"
"""


def _is_elf(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def _ldd_system_deps(binary: Path) -> tuple[set[Path], set[str]]:
    """返回 binary 直接依赖的系统库路径集合 + 缺失库名集合。"""
    proc = subprocess.run(["ldd", str(binary)], capture_output=True, text=True)
    libs, missing = set(), set()
    for line in proc.stdout.splitlines():
        m = re.search(r"=> (/\S+) \(", line)
        if m:
            libs.add(Path(m.group(1)))
            continue
        if "not found" in line:
            missing.add(line.strip().split()[0])
            continue
        m = re.match(r"(/\S+) \(", line)          # 无 "=>" 的直接路径（ld-linux）
        if m:
            libs.add(Path(m.group(1)))
    return libs, missing


def collect_libs(tree: Path, libdir: Path) -> list[Path]:
    """ldd 闭包：树内全部 ELF 依赖的系统库（非 glibc 族）拷进 libdir。

    缺失依赖只警告不阻断——Qt 的可选图片/媒体插件 dlopen 弱依赖
    （如 JPEG-XR 的 libjxrglue），构建机上没有时该插件运行时跳过，
    不影响应用；真正的完整性由 AppRun 冒烟验证兜底。
    """
    queue = [p for p in tree.rglob("*") if p.is_file() and _is_elf(p)]
    seen: set[Path] = set()
    copied: list[Path] = []
    missing_all: dict[str, set[str]] = {}
    while queue:
        f = queue.pop()
        deps, missing = _ldd_system_deps(f)
        if missing:
            try:
                where = str(f.relative_to(tree))
            except ValueError:
                where = f.name                     # 新收进 usr/lib 的库
            missing_all[where] = missing
        for lib in sorted(deps):
            if _BLACKLIST.match(lib.name):
                continue
            if lib in seen:
                continue
            seen.add(lib)
            dest = libdir / lib.name
            if not dest.exists():
                shutil.copy2(lib, dest)
                copied.append(dest)
            queue.append(dest)                      # 收集库自己的依赖
    if missing_all:
        print("警告（可选插件弱依赖，运行时自动跳过）：")
        for f, ms in missing_all.items():
            print(f"  {f}: {sorted(ms)}")
    return copied


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default="dist/WriterStudio")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--appimagetool", default="appimagetool")
    args = ap.parse_args()

    dist = Path(args.dist).resolve()
    out = Path(args.out).resolve()
    appdir = out / "AppDir"
    if appdir.exists():
        shutil.rmtree(appdir)

    app_tree = appdir / "opt" / "WriterStudio"
    libdir = appdir / "usr" / "lib"
    libdir.mkdir(parents=True)
    shutil.copytree(dist, app_tree, symlinks=True)

    copied = collect_libs(app_tree, libdir)
    print(f"收进包里的系统依赖库：{len(copied)} 个")

    icon = Path("resources/icon.png").resolve()
    shutil.copy2(icon, appdir / ".DirIcon")
    shutil.copy2(icon, appdir / "writerstudio.png")
    (appdir / "writerstudio.desktop").write_text(_DESKTOP, encoding="utf-8")
    apprun = appdir / "AppRun"
    apprun.write_text(_APPRUN, encoding="utf-8")
    apprun.chmod(0o755)

    name = f"WriterStudio-{platform.machine()}.AppImage"
    tool = os.environ.get("APPIMAGETOOL", args.appimagetool)
    cmd = [tool]
    if os.environ.get("APPIMAGE_EXTRACT_AND_RUN") == "1":
        cmd.append("--appimage-extract-and-run")
    cmd += [str(appdir), str(out / name)]
    try:
        subprocess.run(cmd, check=True)
        print(f"完成：{out / name}")
    except (OSError, subprocess.CalledProcessError):
        print(f"appimagetool 不可用——AppDir 已就绪：{appdir}\n"
              f"手工出包：{tool} {appdir} {out / name}")


if __name__ == "__main__":
    main()
