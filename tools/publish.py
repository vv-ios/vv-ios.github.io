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
    r = subprocess.run(
        ["gh", "api", f"repos/{REPO}/contents/{relpath}?ref={BRANCH}"],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
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
    # debs：比对新增/修改的
    debs = ROOT / "debs"
    if debs.exists():
        for p in sorted(debs.glob("*.deb")):
            rel = f"debs/{p.name}"
            if git_blob_sha(p.read_bytes()) != remote_sha(rel):
                out.append((rel, p))
    return out


def removed_debs():
    """找出远端有、但本地 debs/ 已经没有的 .deb（即被删除的包）。

    没有这段逻辑的话，从 debs/ 删掉文件后索引会更新，
    但仓库里的 .deb 还留着，且 publish 不会检测到任何变化。
    """
    r = subprocess.run(
        ["gh", "api", f"repos/{REPO}/contents/debs?ref={BRANCH}"],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        return []  # debs/ 不存在或空仓库
    try:
        items = json.loads(r.stdout)
    except Exception:
        return []

    local = {p.name for p in (ROOT / "debs").glob("*.deb")} if (ROOT / "debs").exists() else set()
    gone = []
    for it in items:
        name = it.get("name", "")
        if name.endswith(".deb") and name not in local:
            gone.append((f"debs/{name}", it.get("sha")))
    return gone


def delete_via_api(relpath: str, sha: str, message: str):
    """删除远端文件。Contents API 的 DELETE 必须带上当前 sha。"""
    payload = {"message": message, "sha": sha, "branch": BRANCH}
    r = subprocess.run(
        ["gh", "api", "-X", "DELETE", f"repos/{REPO}/contents/{relpath}", "--input", "-"],
        cwd=ROOT, input=json.dumps(payload), capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        raise RuntimeError(f"API 删除失败 {relpath}:\n{r.stderr}")
    print(f"  ✓ API 已删除 {relpath}")


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


def ensure_pages_built(wait_seconds: int = 240):
    """等 Pages 构建完成；卡住或 errored 时自动重置。

    实测：连续多次 API 提交（每个文件一次 commit）会让 Pages 构建互相竞争，
    出现连续 `Page build failed` 或长期卡在 `building`。
    解法是按 skill §6.5 —— 重新 PUT 一次 Pages 配置强制重新初始化。
    """
    import time

    deadline = time.time() + wait_seconds
    reset_done = False
    last = None

    while time.time() < deadline:
        r = subprocess.run(
            ["gh", "api", f"repos/{REPO}/pages", "--jq", ".status"],
            cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        status = (r.stdout or "").strip()
        if status != last:
            print(f"    Pages: {status}")
            last = status

        if status == "built":
            return True

        if status == "errored" and not reset_done:
            print("    ! 构建失败，重置 Pages 配置后重试")
            subprocess.run(
                ["gh", "api", "-X", "PUT", f"repos/{REPO}/pages",
                 "-f", "build_type=legacy",
                 "-f", "source[branch]=main", "-f", "source[path]=/"],
                cwd=ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
            time.sleep(3)
            subprocess.run(
                ["gh", "api", "-X", "POST", f"repos/{REPO}/pages/builds"],
                cwd=ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
            reset_done = True

        time.sleep(8)

    print("    ! 等待 Pages 超时，请到仓库 Pages 设置查看")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--message", default="chore: publish repo update")
    ap.add_argument("--api-only", action="store_true", help="跳过 git push，直接用 API")
    ap.add_argument("--no-wait", action="store_true", help="不等 Pages 构建完成")
    args = ap.parse_args()

    print("==> 重建索引")
    run([sys.executable, str(ROOT / "tools" / "gen_repo.py")], capture=False)

    files = changed_files()
    gone = removed_debs()

    if not files and not gone:
        print("没有需要发布的变化。")
        return

    if files:
        print(f"==> 检测到 {len(files)} 个文件有变化:")
        for rel, _ in files:
            print(f"    {rel}")
    if gone:
        print(f"==> 检测到 {len(gone)} 个包已从 debs/ 移除:")
        for rel, _ in gone:
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
            if not args.no_wait:
                print("==> 等待 Pages 构建")
                ensure_pages_built()
            print("完成。")
            return

    print("==> 通过 GitHub Contents API 发布")
    for rel, path in files:
        upload_via_api(rel, path, args.message)
    for rel, sha in gone:
        delete_via_api(rel, sha, args.message)

    if not args.no_wait:
        print("==> 等待 Pages 构建")
        ensure_pages_built()

    print("完成。")
    print(f"    源地址: https://{REPO.split('/')[0]}.github.io/")


if __name__ == "__main__":
    main()
