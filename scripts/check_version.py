"""核对项目版本号一致性：pyproject.toml、writerstudio/__init__.py 与可选 Git 标签。

用法：
    python scripts/check_version.py                # 核对 pyproject.toml 与 __version__
    python scripts/check_version.py --tag v0.3.2   # 额外核对发布标签（去掉前缀 v 后一致）
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read_pyproject_version(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib
        return str(tomllib.loads(text)["project"]["version"])
    except ModuleNotFoundError:
        # 兼容 Python 3.10（无标准库 tomllib）
        in_project = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                in_project = stripped == "[project]"
                continue
            if in_project:
                m = re.match(r'^version\s*=\s*["\']([^"\']+)["\']', stripped)
                if m:
                    return m.group(1)
        raise RuntimeError(f"未能从 {path} 解析 [project].version")


def _read_init_version(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    if isinstance(node.value, ast.Constant) and isinstance(
                            node.value.value, str):
                        return node.value.value
    raise RuntimeError(f"未能从 {path} 解析 __version__")


def _normalize_prerelease(ver: str) -> str:
    """兼容 SemVer 标签（如 0.4.0-rc.1）与 PEP 440 版本号（0.4.0rc1）。"""
    s = ver.strip()
    if s.startswith("v"):
        s = s[1:]
    s = re.sub(r"[-.]?(alpha|a)[-.]?(\d+)$", r"a\2", s)
    s = re.sub(r"[-.]?(beta|b)[-.]?(\d+)$", r"b\2", s)
    s = re.sub(r"[-.]?rc[-.]?(\d+)$", r"rc\1", s)
    return s


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="核对项目版本号一致性")
    parser.add_argument("--tag", default=None,
                        help="发布标签（如 v0.3.2）；提供时强制要求与源码版本一致")
    args = parser.parse_args(argv)

    proj_ver = _read_pyproject_version(ROOT / "pyproject.toml")
    init_ver = _read_init_version(ROOT / "writerstudio" / "__init__.py")

    errors: list[str] = []
    if proj_ver != init_ver:
        errors.append(
            f"pyproject.toml 版本 ({proj_ver}) 与 "
            f"writerstudio/__init__.py __version__ ({init_ver}) 不一致"
        )
    if args.tag:
        tag_norm = _normalize_prerelease(args.tag)
        if _normalize_prerelease(proj_ver) != tag_norm:
            errors.append(
                f"Git 标签 ({args.tag}) 与 "
                f"pyproject.toml 版本 ({proj_ver}) 不一致"
            )
        if _normalize_prerelease(init_ver) != tag_norm:
            errors.append(
                f"Git 标签 ({args.tag}) 与 "
                f"writerstudio/__init__.py __version__ ({init_ver}) 不一致"
            )

    if errors:
        for msg in errors:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 1

    suffix = f", tag={args.tag}" if args.tag else ""
    print(f"version check ok: {proj_ver}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
