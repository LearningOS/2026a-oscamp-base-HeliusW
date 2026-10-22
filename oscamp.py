#!/usr/bin/env python3
"""Run OS Camp on a verified Linux copy of the saved Mac sources."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time


CONTAINER = "oscamp-base-2026a"
WORKSPACE = "/opt/oscamp-live/workspace"
TARGET = "/opt/oscamp-live/target"
ROOT_FILES = {"Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "exercises.toml"}
SOURCE_DIRS = {".cargo", "cli", "exercises", "src"}
EXCLUDED = {"target", ".git", ".cph", "__pycache__", ".DS_Store"}
RISCV_PACKAGES = {"stack_coroutine", "green_threads"}

# The receiver reads bytes from stdin, never from the Mac bind mount.
RECEIVER = r'''
import base64, fcntl, hashlib, json, os, pathlib, sys, tempfile

root = pathlib.Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
payload = json.load(sys.stdin)

def destination(name):
    relative = pathlib.PurePosixPath(name)
    if relative.is_absolute() or not relative.parts or any(
        part in (".", "..") for part in relative.parts
    ):
        raise ValueError("Invalid relative source path")
    path = root.joinpath(*relative.parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Source path escapes workspace")
    return path

with (root / ".oscamp-sync.lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    manifest_path = root / ".oscamp-sources.json"
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    changed = deleted = 0
    for name, entry in payload.items():
        content = base64.b64decode(entry["content"], validate=True)
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise ValueError("Invalid source checksum: " + name)
        path = destination(name)
        if path.exists() and path.read_bytes() == content:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".oscamp-save-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(content)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        changed += 1
    for name in previous.keys() - payload.keys():
        path = destination(name)
        if path.is_file():
            path.unlink()
            deleted += 1
    manifest = {name: entry["sha256"] for name, entry in payload.items()}
    for name, expected in manifest.items():
        if hashlib.sha256(destination(name).read_bytes()).hexdigest() != expected:
            raise ValueError("Source verification failed: " + name)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    print(json.dumps({"files": len(manifest), "changed": changed,
                      "deleted": deleted, "manifest": manifest}))
'''


def saved_sources(root):
    """Read contents, including changes whose timestamps were preserved."""
    sources = {}
    for name in sorted(ROOT_FILES):
        path = root / name
        if path.is_file() and not path.is_symlink():
            sources[name] = path.read_bytes()
    for name in sorted(SOURCE_DIRS):
        directory = root / name
        if not directory.is_dir() or directory.is_symlink():
            continue
        for current, dirs, files in os.walk(directory, followlinks=False):
            parent = Path(current)
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDED
                             and not (parent / d).is_symlink())
            for filename in sorted(files):
                path = parent / filename
                if filename in EXCLUDED or filename.endswith(("~", ".swp", ".swo")):
                    continue
                if path.is_symlink():
                    raise RuntimeError("源码目录含符号链接，请先确认其目标：" + str(path))
                sources[path.relative_to(root).as_posix()] = path.read_bytes()
    return sources


def signature(sources):
    return {name: hashlib.sha256(content).hexdigest()
            for name, content in sources.items()}


def stable_sources(root):
    previous = None
    for _ in range(50):
        try:
            current = saved_sources(root)
        except FileNotFoundError:
            previous = None
            time.sleep(0.2)
            continue
        if previous is not None and current == previous:
            return current
        previous = current
        time.sleep(0.2)
    raise RuntimeError("文件持续变化，无法取得稳定的保存内容；请稍后重试。")


class SourceSync:
    def __init__(self, root, container=CONTAINER, workspace=WORKSPACE, target=TARGET):
        self.root = Path(root).resolve()
        self.container = container
        self.workspace = workspace
        self.target = target
        self.applied = None
        self.stop = threading.Event()
        self.error = None
        self.thread = None

    def sync(self, sources=None):
        if sources is None:
            sources = stable_sources(self.root)
        payload = {name: {"sha256": hashlib.sha256(content).hexdigest(),
                          "content": base64.b64encode(content).decode("ascii")}
                   for name, content in sources.items()}
        result = subprocess.run(
            ["docker", "exec", "-i", self.container, "python3", "-c",
             RECEIVER, self.workspace],
            input=json.dumps(payload).encode(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=45,
        )
        if result.returncode:
            raise RuntimeError("源码同步失败：" + result.stderr.decode(errors="replace"))
        report = json.loads(result.stdout)
        expected = signature(sources)
        if report["manifest"] != expected:
            raise RuntimeError("容器源码校验不一致，已停止继续使用旧源码。")
        self.applied = expected
        return report

    def start(self):
        def monitor():
            previous = None
            while not self.stop.wait(0.3):
                try:
                    current = saved_sources(self.root)
                    digest = signature(current)
                    # Two matching scans debounce the editor's save sequence.
                    if digest == previous and digest != self.applied:
                        self.sync(current)
                    previous = digest
                except FileNotFoundError:
                    previous = None
                except Exception as error:
                    self.error = error
                    return
        self.thread = threading.Thread(target=monitor, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=46)

    def command(self, arguments, interactive=False):
        command = ["docker", "exec"]
        if interactive:
            command += ["-it"]
        command += ["-w", self.workspace, "-e", "CARGO_TARGET_DIR=" + self.target,
                    self.container] + arguments
        return command


def ensure_container():
    if not shutil.which("docker"):
        raise RuntimeError("没有找到 docker，请先安装 Docker Desktop。")
    result = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                            capture_output=True, timeout=5)
    if result.returncode:
        app = Path("/Applications/Docker.app")
        if sys.platform != "darwin" or not app.exists():
            raise RuntimeError("Docker 后台未运行，请先启动 Docker。")
        subprocess.run(["open", str(app)], check=True)
        print("正在等待 Docker Desktop 启动……", flush=True)
        for _ in range(30):
            result = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                                    capture_output=True, timeout=5)
            if result.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Docker 启动尚未完成，请检查 Docker Desktop 后重试。")
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", CONTAINER],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode:
        raise RuntimeError("找不到练习容器 " + CONTAINER + "，请先完成课程环境配置。")
    if result.stdout.strip() != "true":
        subprocess.run(["docker", "start", CONTAINER], check=True)


def watch(session):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("watch 需要交互终端；单题验证请使用：python3 oscamp.py test 包名")
    import termios
    terminal_state = termios.tcgetattr(sys.stdin.fileno())
    process = None
    try:
        session.start()
        process = subprocess.Popen(session.command(
            ["cargo", "run", "-p", "oscamp-cli", "--", "watch"], interactive=True))
        while process.poll() is None:
            if session.error:
                raise RuntimeError(str(session.error))
            time.sleep(0.1)
        return process.returncode
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        session.close()
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, terminal_state)


def main():
    parser = argparse.ArgumentParser(description="OS Camp：保存后同步校验，在 Linux 本地运行")
    parser.add_argument("command", nargs="?", default="watch", choices=("watch", "test", "sync"))
    parser.add_argument("packages", nargs="*", help="test 时要验证的 package 名称")
    args = parser.parse_args()
    if args.command == "test" and not args.packages:
        parser.error("请指定 package，例如：python3 oscamp.py test atomic_ordering")
    if args.command != "test" and args.packages:
        parser.error("只有 test 接受 package 参数")
    ensure_container()
    root = Path(__file__).resolve().parent
    session = SourceSync(root)
    report = session.sync()
    print("源码校验通过：{} 个文件。".format(report["files"]), flush=True)
    if args.command == "sync":
        return 0
    if args.command == "watch":
        return watch(session)
    for package in args.packages:
        command = ["cargo", "test", "-p", package]
        if package in RISCV_PACKAGES:
            command += ["--target", "riscv64gc-unknown-linux-gnu"]
        result = subprocess.run(session.command(command))
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (RuntimeError, subprocess.SubprocessError, OSError) as error:
        print("OS Camp：" + str(error), file=sys.stderr)
        sys.exit(1)
