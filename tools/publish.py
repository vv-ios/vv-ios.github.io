#!/usr/bin/env python3
"""
publish.py - 一键发布：重建索引 → commit → push，失败则自动改走 GitHub Contents API。

为什么要这个脚本：国内经常出现 github.com:443 不可达（git push 报
Connection was reset），但 api.github.com 一直正常。此时用 Contents API
逐文件上传即可发布成功。

用法:
    python tools/publish.py                 # 自动 commit message
    python tools/publish.py -m "add tweak"  # 指定提交信息
    python tools/publish.py --api-only      # 直接走 API，不尝试 push
"""

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "vv-ios/vv-ios.github.io"   # <owner>/<repo>，改地址时务必同步这里
BRANCH = "main"

# 索引与元数据文件，发布时一起提交
INDEX_FILES = [
    "Packages", "Packages.gz", "Packages.bz2",
    "Release", "packages.json", "repo-data.js",
]


def run(cmd, check=True, capture=True):
    r = subprocess.run(
        cmd, cwd=ROOT, shell=isinstance(cmd, str),
        capture_output=capture, text=True,
        encoding="utf-8", errors="replace",
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"命令失败: {cmd}\n{r.stdout}\n{r.stderr}")
    return r


def git_blob_sha(content: bytes) -> str:
    """本地自算 git blob sha1，用于和 Contents API 返回的 sha 比对。

    不能用 git status 判断变化：API 提交会让远端前进而本地毫不知情，
    此时工作区干净，git status 会误报 nothing to publish。
    """
    import hashlib
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def remote_sha(relpath: str):
    """取远端某文件的 blob sha；不存在返回 None。"""
    r = run(["gh", "api", f"repos/{REPO}/contents/{relpath}?ref={BRANCH}"], check=False)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout).get("sha")
    except Exception:
        return None


def changed_files():
    """比对远端 blob sha，找出真正需要发布/删除的文件。"""
    out = []
    # 索引文件：按内容比对
    for name in INDEX_FILES:
        p = ROOT / name
        if not p.exists():
            continue
        local = git_blob_sha(p.read_bytes())
        if local != remote_sha(name):
            out.append((name, p))
    # debs：比对新增的
    debs = ROOT / "debs"
    if debs.exists():
        for p in sorted(debs.glob("*.deb")):
            rel = f"debs/{p.name}"
            if git_blob_sha(p.read_bytes()) != remote_sha(rel):
                out.append((rel, p))
    return out


def upload_via_api(relpath: str, path: Path, message: str):
    """用 stdin 传 JSON —— base64 后的 deb 常超 Windows 32KB 命令行上限。"""
    sha = remote_sha(relpath)
    payload = {
        "message": message,
        "content": base64.b64encode(path.read_bytes()).decode("ascii"),
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha
    r = subprocess.run(
        ["gh", "api", "-X", "PUT", f"repos/{REPO}/contents/{relpath}", "--input", "-"],
        cwd=ROOT, input=json.dumps(payload), capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        raise RuntimeError(f"API 上传失败 {relpath}:\n{r.stderr}")
    print(f"  ✓ API 已更新 {relpath}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--message", default="chore: publish repo update")
    ap.add_argument("--api-only", action="store_true", help="跳过 git push，直接用 API")
    args = ap.parse_args()

    print("==> 重建索引")
    run([sys.executable, "tools/gen_repo.py"], capture=False)

    files = changed_files()
    if not files:
        print("没有需要发布的变化。")
        return
    print(f"==> 检测到 {len(files)} 个文件有变化:")
    for rel, _ in files:
        print(f"    {rel}")

    if not args.api_only:
        print("==> 尝试 git 提交并推送")
        # 推送前先对齐远端，避免 API 提交造成的分叉
        run(["git", "fetch", "origin", BRANCH], check=False)
        run(["git", "rebase", f"origin/{BRANCH}"], check=False)
        run(["git", "add", "-A"], check=False)
        r = run(["git", "commit", "-m", args.message], check=False)
        pushed = False
        if r.returncode == 0:
            pr = run(["git", "push", "origin", BRANCH], check=False)
            if pr.returncode == 0:
                print("  ✓ git push 成功")
                pushed = True
            else:
                # (fetch first) 说明分叉已产生，立刻转 API 路径，不要反复重试 push
                print("  ! git push 失败，转为 API 路径")
        else:
            print("  ! 无需提交（可能已由索引重建产生相同内容）")

        if pushed:
            return

    print("==> 通过 GitHub Contents API 发布")
    for rel, path in files:
        upload_via_api(rel, path, args.message)
    print("完成。")


if __name__ == "__main__":
    main()
