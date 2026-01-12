#!/usr/bin/env python3
"""
bootstrap.py - 在运行程序前自动安装依赖并（可选）创建并切换到项目 .venv

行为：
- 检查所需包（requirements.txt，如果存在则优先使用）
- 若当前未激活 venv 则创建 .venv（项目根/.venv）
- 在目标 python 环境中安装 requirements.txt���或缺失包）
- 运行 playwright 安装二进制： python -m playwright install chromium
- 安装完成后，自动使用目标 python 重新 exec 当前脚本（通过 env BOOTSTRAPPED 避免循环）
"""
import os
import sys
import subprocess
from pathlib import Path
import shutil

PROJECT_ROOT = Path.cwd()
VENV_DIR = PROJECT_ROOT / ".venv"
REQUIREMENTS_FILE = PROJECT_ROOT / "requirements.txt"
BOOT_ENV_VAR = "BOOTSTRAPPED"

# 要检查的核心包（modules 名称 -> pip 名称），仅作备份，当 requirements.txt 缺失时使用
CORE_PACKAGES = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "playwright": "playwright",
    "pyperclip": "pyperclip",
    "aiofiles": "aiofiles",
    "aiohttp": "aiohttp",
    "jinja2": "jinja2",
}

def run(cmd, env=None, check=True):
    print(">>>", " ".join(cmd))
    return subprocess.run(cmd, env=env, check=check)

def in_venv():
    # 若 running interpreter 的 sys.prefix 与 sys.base_prefix 不同，说明在 venv
    return (hasattr(sys, 'real_prefix') or sys.prefix != getattr(sys, "base_prefix", sys.prefix))

def python_executable_for_venv(venv_path: Path) -> str:
    if os.name == "nt":
        return str(venv_path / "Scripts" / "python.exe")
    else:
        return str(venv_path / "bin" / "python")

def pip_install_with_python(python_bin: str, requirements: Path = None, packages=None):
    if requirements and requirements.exists():
        cmd = [python_bin, "-m", "pip", "install", "--upgrade", "pip"]
        run(cmd)
        cmd = [python_bin, "-m", "pip", "install", "-r", str(requirements)]
        run(cmd)
    else:
        # 安装指定包列表
        if not packages:
            return
        cmd = [python_bin, "-m", "pip", "install", "--upgrade", "pip"]
        run(cmd)
        cmd = [python_bin, "-m", "pip", "install"] + packages
        run(cmd)

def ensure_playwright_browsers(python_bin: str):
    # install chromium via playwright
    try:
        cmd = [python_bin, "-m", "playwright", "install", "chromium"]
        run(cmd)
    except subprocess.CalledProcessError as e:
        print("playwright browser install failed:", e)
        raise

def ensure_requirements():
    """
    返回 tuple(installed_something: bool, target_python: str)
    如果需要创建 venv，则在 .venv 中安装并返回 venv python 路径（并自动 exec into it）。
    """
    # 如果已经在被标记为重启后的环境，直接返回 (False, sys.executable)
    if os.environ.get(BOOT_ENV_VAR) == "1":
        return False, sys.executable

    # 检查是否满足 requirements by attempting imports (fast check)
    missing = []
    # If requirements.txt available, prefer to use that for installation
    if REQUIREMENTS_FILE.exists():
        # We'll still do a quick import-check for speed; but we'll install from requirements if any missing
        with open(REQUIREMENTS_FILE, "r", encoding="utf-8") as f:
            reqs = [line.strip().split("==")[0].split(">=")[0].split()[0] for line in f if line.strip() and not line.strip().startswith("#")]
    else:
        reqs = list(CORE_PACKAGES.values())

    # simple import-based check:
    for pkg in reqs:
        modname = pkg
        # some packages like uvicorn package name is same as module
        try:
            __import__(modname)
        except Exception:
            missing.append(pkg)

    if not missing:
        # all good
        return False, sys.executable

    print("Detected missing packages:", missing)

    # Decide install target: if currently not in venv, create project .venv and install there
    target_python = sys.executable
    created_venv = False
    if not in_venv():
        print("Not in a virtual environment. Creating project .venv at", VENV_DIR)
        # create venv
        try:
            run([sys.executable, "-m", "venv", str(VENV_DIR)])
            created_venv = True
            target_python = python_executable_for_venv(VENV_DIR)
            print("Created .venv. Python:", target_python)
        except subprocess.CalledProcessError as e:
            print("Failed to create venv:", e)
            raise

    # Install requirements into target_python
    print("Installing requirements into:", target_python)
    try:
        pip_install_with_python(target_python, requirements=REQUIREMENTS_FILE if REQUIREMENTS_FILE.exists() else None, packages=missing)
    except subprocess.CalledProcessError as e:
        print("pip install failed:", e)
        raise

    # Install playwright browsers
    try:
        ensure_playwright_browsers(target_python)
    except Exception as e:
        print("playwright browser install failed:", e)
        raise

    # If we created a venv and target_python differs from current, re-exec into venv python
    if created_venv:
        print("Dependencies installed into .venv. Re-launching using .venv python...")
        # set BOOT_ENV_VAR to avoid loops
        os.environ[BOOT_ENV_VAR] = "1"
        os.execv(target_python, [target_python] + sys.argv)

    # If we didn't create venv but installed into current environment, we can re-exec into same python to pick up imports
    print("Dependencies installed into current Python environment. Restarting script...")
    os.environ[BOOT_ENV_VAR] = "1"
    os.execv(sys.executable, [sys.executable] + sys.argv)

def main():
    try:
        ensure_requirements()
    except Exception as e:
        print("自动安装依赖失败：", e)
        print("请手动检查 Python / pip 环境，或查看上面的错误信息并重试。")
        sys.exit(1)

if __name__ == "__main__":
    main()