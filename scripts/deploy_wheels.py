"""在本机准备与服务器 Linux 容器兼容的 wheel 安装包（离线部署用）。

服务器容器无外网：先用 PROBE_PYTHON 读取容器的 Python 版本、机器架构与
pip 平台标签，本机再按相同标签 pip download（含传递依赖），结果按
requirements 内容与运行时信息哈希缓存，重复发布无需重新下载。
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path


def prepare_wheels(requirements: Path, runtime: dict, cache_root: Path) -> Path:
    """返回包含全部 *.whl 的目录；已缓存时直接复用。"""
    requirements = Path(requirements)
    cache_root = Path(cache_root)
    python = str(runtime.get("python") or "").strip()
    if not python:
        raise RuntimeError("服务器未返回 Python 版本，无法准备安装包")
    major_minor = ".".join(python.split(".")[:2])
    abi = "cp" + major_minor.replace(".", "")
    platforms = [p for p in runtime.get("platforms", []) if isinstance(p, str) and p]
    if not platforms:
        machine = runtime.get("machine") or "x86_64"
        platforms = [f"manylinux2014_{machine}"]
    key = hashlib.sha256(
        (requirements.read_text(encoding="utf-8") + python + "\n".join(platforms)).encode("utf-8")
    ).hexdigest()[:16]
    wheelhouse = cache_root / key
    marker = wheelhouse / ".complete"
    if marker.is_file():
        return wheelhouse
    wheelhouse.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "download", "-r", str(requirements),
           "-d", str(wheelhouse), "--only-binary=:all:",
           "--python-version", major_minor, "--implementation", "cp"]
    # abi3/none 兼容 wheel（如 cryptography）也要能命中。
    for value in (abi, "abi3", "none"):
        cmd += ["--abi", value]
    for platform in platforms:
        cmd += ["--platform", platform]
    subprocess.run(cmd, check=True)
    marker.write_text("ok", encoding="utf-8")
    return wheelhouse
