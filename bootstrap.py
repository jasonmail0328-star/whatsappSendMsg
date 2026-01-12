#!/usr/bin/env python3
"""
bootstrap.py - 在运行程序前自动安装依赖并（可选）创建并切换到项目 .venv

行为：
- 检查所需包（requirements.txt，如果存在则优先使用）
- 若当前未激活 venv 则创建 .venv（项目根/.venv）
- 在目标 python 环境中安装 requirements.txt 或缺失包
- 运行 playwright 安装二进制： python -m playwright install chromium
- 安装完成后，自动使用目标 python 重新 exec 当前脚本（通过 env BOOTSTRAPPED 避免循环）
"""
import os
import sys
import subprocess
from pathlib import Path
import shutil
import re
import importlib.util

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
    # Print a readable command and then run
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

def _safe_parse_req_line(line: str) -> str:
    """
    从 requirements.txt 的一行中提取“包名”（去掉版本、extras、环境标记）
    例如：
      - "package==1.2.3" -> "package"
      - "package[extra]>=1.0; python_version<'3.9'" -> "package"
      - "git+https://..." -> returns whole token (pip will handle it)
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return ""
    # remove inline environment markers starting with ';'
    line = line.split(";", 1)[0].strip()
    # If it's a VCS or URL, return as-is so pip can install it
    if line.startswith(("git+", "http://", "https://", "ssh://", "file:")):
        return line
    # split on version specifiers or extras: <,>,= or '['
    m = re.split(r"[<>=\[\s]", line, 1)
    name = m[0].strip()
    return name

def pip_install_with_python(python_bin: str, requirements: Path = None, packages=None):
    if requirements and requirements.exists():
        print("Installing from requirements:", requirements)
        cmd = [python_bin, "-m", "pip", "install", "--upgrade", "pip"]
        run(cmd)
        cmd = [python_bin, "-m", "pip", "install", "-r", str(requirements)]
        run(cmd)
    else:
        # 安装指定包列表（packages 可包含 VCS/URL 形式）
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

def _module_available(module_name: str) -> bool:
    """
    使用 importlib.util.find_spec 来检测模块是否可用（不会执行模块顶层代码）
    """
    try:
        return importlib.util.find_spec(module_name) is not None
    except Exception:
        return False

def ensure_requirements():
    """
    返回 tuple(installed_something: bool, target_python: str)
    如果需要创建 venv，则在 .venv 中安装并返回 venv python 路径（并自动 exec into it）。
    """
    # 如果已经在被标记为重启后的环境，直接返回 (False, sys.executable)
    if os.environ.get(BOOT_ENV_VAR) == "1":
        return False, sys.executable

    # Build list of requirement tokens (package names or VCS/URL entries)
    if REQUIREMENTS_FILE.exists():
        raw_lines = []
        with open(REQUIREMENTS_FILE, "r", encoding="utf-8") as f:
            for l in f:
                s = l.strip()
                if not s or s.startswith("#"):
                    continue
                raw_lines.append(s)
        parsed_req_names = []
        for l in raw_lines:
            name = _safe_parse_req_line(l)
            if name:
                parsed_req_names.append(name)
    else:
        parsed_req_names = list(CORE_PACKAGES.values())

    # Quick availability check using module names; if a requirement is a URL/VCS, skip import check
    missing_for_install = []
    for token in parsed_req_names:
        if token.startswith(("git+", "http://", "https://", "ssh://", "file:")):
            # cannot check importably; include for install
            missing_for_install.append(token)
            continue
        # token might be package name; try some heuristics to find module: prefer token itself
        module_name = token.split("[", 1)[0]  # remove extras
        module_name = module_name.replace("-", "_")  # common mapping
        if not _module_available(module_name):
            # try without underscores (some packages differ)
            alt = module_name.replace("_", "-")
            if not _module_available(alt):
                missing_for_install.append(token)

    if not missing_for_install:
        # all good
        print("All required packages appear available.")
        return False, sys.executable

    print("Detected missing/needed installs:", missing_for_install)

    # Decide install target: if currently not in venv, create project .venv (or use existing .venv)
    target_python = sys.executable
    created_venv = False
    # If not currently in a venv, prefer using project .venv (create if missing)
    if not in_venv():
        # if .venv exists and looks valid, reuse it
        venv_python = python_executable_for_venv(VENV_DIR)
        if VENV_DIR.exists() and Path(venv_python).exists():
            print(".venv exists; using project .venv python:", venv_python)
            target_python = venv_python
            created_venv = False
        else:
            print("Not in a virtual environment. Creating project .venv at", VENV_DIR)
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
        # If a requirements file exists, install from it (pip will handle versions & VCS entries)
        if REQUIREMENTS_FILE.exists():
            pip_install_with_python(target_python, requirements=REQUIREMENTS_FILE)
        else:
            # install the missing tokens (could be package names or VCS urls)
            pip_install_with_python(target_python, packages=missing_for_install)
    except subprocess.CalledProcessError as e:
        print("pip install failed:", e)
        raise

    # Install playwright browsers (only if playwright is among requirements or if not present)
    try:
        # If playwright not installed previously, ensure playwright install; otherwise still safe to run
        ensure_playwright_browsers(target_python)
    except Exception as e:
        print("playwright browser install failed:", e)
        raise

    # Re-exec logic: if we created a venv, exec into its python; otherwise restart current python once
    os.environ[BOOT_ENV_VAR] = "1"
    if created_venv:
        if not Path(target_python).exists():
            print("Expected venv python not found at", target_python)
            print("Please check .venv creation or run: python -m venv .venv")
            raise RuntimeError("venv python not found")
        print("Dependencies installed into .venv. Re-launching using .venv python...")
        try:
            os.execv(target_python, [target_python] + sys.argv)
        except Exception as e:
            print("Failed to exec into .venv python:", e)
            raise
    else:
        # We installed into the current interpreter environment; restart once to pick up new packages
        print("Dependencies installed into current Python environment. Restarting script...")
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            print("Failed to restart with sys.executable:", e)
            raise

def main():
    try:
        ensure_requirements()
    except Exception as e:
        print("自动安装依赖失败：", e)
        print("请手动检查 Python / pip 环境，或查看上面的错误信息并重试。")
        sys.exit(1)

if __name__ == "__main__":
    main()