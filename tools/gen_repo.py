#!/usr/bin/env python3
"""
gen_repo.py - 生成 Sileo / Cydia 软件源索引

不依赖 dpkg-deb / ar / xz：纯 Python 解析 .deb，Windows 上可直接跑。

用法:
    python tools/gen_repo.py            # 扫描 debs/ 重建索引
    python tools/gen_repo.py --demo     # 额外生成一个演示 deb（用于链路验证）
"""

import argparse
import bz2
import gzip
import hashlib
import io
import json
import lzma
import os
import struct
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import zstandard  # 可选，用于 control.tar.zst
except ImportError:
    zstandard = None

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# 关键：Windows 上必须用字节写入，避免 LF 被转成 CRLF
# Path.write_text() 会把 \n 写成 \r\n，导致 Release 里的校验和与线上字节不符
# --------------------------------------------------------------------------
def write_text_lf(path, text):
    Path(path).write_bytes(text.encode("utf-8"))


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def md5(data):
    return hashlib.md5(data).hexdigest()


def load_config():
    cfg = {
        "origin": "vv-ios",
        "label": "vv-ios",
        "name": "vv-ios",
        "description": "vv-ios 的越狱插件源 · Dopamine / palera1n rootless",
        "maintainer": "vv-ios <vv-ios@users.noreply.github.com>",
        # 改地址时务必同步这里：repo.json 会覆盖，但默认值错了会在无 repo.json 的环境出错
        "url": "https://vv-ios.github.io/",
        "icon": "CydiaIcon.png",
        "accent": "#5b8cff",
        "component": "main",
        "codename": "ios",
        "suite": "stable",
        "version": "1.0",
        "architectures": ["iphoneos-arm64", "iphoneos-arm"],
        "featured": [],
    }
    cfg_path = ROOT / "repo.json"
    if cfg_path.exists():
        cfg.update(json.loads(cfg_path.read_text(encoding="utf-8")))
    return cfg


# --------------------------------------------------------------------------
# .deb 解析（ar 归档）
# --------------------------------------------------------------------------
def iter_ar_members(data):
    """遍历 ar 归档成员，返回 (name, body)。"""
    if data[:8] != b"!<arch>\n":
        raise ValueError("不是合法的 ar 归档（.deb）")
    off = 8
    while off + 60 <= len(data):
        hdr = data[off:off + 60]
        name = hdr[0:16].decode("ascii", "replace").strip()
        try:
            size = int(hdr[48:58].decode("ascii").strip())
        except ValueError:
            break
        body = data[off + 60:off + 60 + size]
        off += 60 + size + (size % 2)
        yield name, body


def decompress_control(blob):
    """control.tar 可能是 gz / xz / lzma / zst / 裸 tar。"""
    if blob[:2] == b"\x1f\x8b":
        return gzip.decompress(blob)
    if blob[:6] == b"\xfd7zXZ\x00":
        return lzma.decompress(blob)
    if blob[:5] == b"\x5d\x00\x00\x80\x00":  # lzma_alone
        return lzma.decompress(blob, format=lzma.FORMAT_ALONE)
    if blob[:4] == b"\x28\xb5\x2f\xfd":  # zstd
        if zstandard is None:
            raise RuntimeError("control.tar.zst 需要 zstandard 模块：pip install zstandard")
        return zstandard.ZstdDecompressor().decompress(blob)
    return blob  # 裸 tar


def parse_control(text):
    """RFC822 解析：续行以空格开头，要拼回同一字段。"""
    fields = {}
    key = None
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and key:
            fields[key] += "\n" + line[1:]
        elif ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            fields[key] = val.strip()
    return fields


def read_deb(deb_path):
    """返回 (control_dict, deb_bytes, data_uncompressed_size)"""
    raw = Path(deb_path).read_bytes()
    control_blob = None
    data_size = 0
    for name, body in iter_ar_members(raw):
        base = name.rstrip("/")
        if base.startswith("control.tar"):
            control_blob = body
        elif base.startswith("data.tar"):
            data_size = len(decompress_control(body))
    if control_blob is None:
        raise ValueError(f"{deb_path}: 找不到 control.tar")

    tar_bytes = decompress_control(control_blob)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        ctrl_text = None
        for m in tf.getmembers():
            if m.name.lstrip("./") == "control":
                ctrl_text = tf.extractfile(m).read().decode("utf-8", "replace")
                break
    if ctrl_text is None:
        raise ValueError(f"{deb_path}: control.tar 里没有 control 文件")
    return parse_control(ctrl_text), raw, data_size


# --------------------------------------------------------------------------
# 索引条目
# --------------------------------------------------------------------------
FIELD_ORDER = [
    "Package", "Name", "Version", "Architecture", "Description", "Section",
    "Depends", "Pre-Depends", "Conflicts", "Replaces", "Provides",
    "Maintainer", "Author", "Icon", "Sileodepiction", "Depiction",
    "Filename", "Size", "MD5sum", "SHA256", "Installed-Size",
]


def build_entry(ctrl, raw, data_size, deb_relpath):
    """把 control 字段整理成索引条目（方括号顺序输出）。"""
    fields = dict(ctrl)

    # 这几项必须重算并覆盖，避免与 control 里手写的值重复
    for k in ("Filename", "Size", "MD5sum", "SHA256"):
        fields.pop(k, None)

    fields["Filename"] = deb_relpath
    fields["Size"] = str(len(raw))
    fields["MD5sum"] = md5(raw)
    fields["SHA256"] = sha256(raw)

    # Installed-Size 单位 KiB；control 里手写过就保留，绝不重复写
    if "Installed-Size" not in fields:
        fields["Installed-Size"] = str(max(1, data_size // 1024)) if data_size else "1"

    lines = []
    seen = set()
    for key in FIELD_ORDER:
        if key in fields:
            val = fields.pop(key)
            seen.add(key)
            if key == "Description":
                # 描述多行时，续行需要缩进一个空格
                parts = val.split("\n")
                lines.append(f"{key}: {parts[0]}")
                for p in parts[1:]:
                    lines.append(f" {p.strip()}")
            else:
                lines.append(f"{key}: {val}")
    # 剩下未列入 FIELD_ORDER 的字段，按字母序补在后面
    for key in sorted(fields):
        lines.append(f"{key}: {fields[key]}")
    return "\n".join(lines)


def make_demo_deb():
    """生成一个最小可用的演示 deb（纯 Python，不需要 dpkg-deb）。"""
    control = (
        "Package: com.vvios.hello\n"
        "Name: vv-ios Hello\n"
        "Version: 1.0.0\n"
        "Architecture: iphoneos-arm64\n"
        "Description: vv-ios 源链路验证包\n"
        " 装得上就说明源没问题，验证完可以删掉。\n"
        "Section: Tweaks\n"
        "Maintainer: vv-ios <vv-ios@users.noreply.github.com>\n"
        "Author: vv-ios\n"
    )

    def tar_bytes(files):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for name, content in files:
                data = content.encode("utf-8")
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mtime = 0
                info.mode = 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                tf.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    control_tar = gzip.compress(tar_bytes([("./control", control)]), 9, mtime=0)
    data_tar = gzip.compress(
        tar_bytes([
            ("./var/mobile/Library/vv-ios-hello.txt", "hello from vv-ios\n"),
        ]),
        9, mtime=0,
    )

    def ar_member(name, body):
        hdr = f"{name:<16}{0:<12}{0:<6}{0:<6}{0o100644:<8}{len(body):<10}`\n".encode("ascii")
        pad = b"\n" if len(body) % 2 else b""
        return hdr + body + pad

    out = b"!<arch>\n"
    out += ar_member("debian-binary", b"2.0\n")
    out += ar_member("control.tar.gz", control_tar)
    out += ar_member("data.tar.gz", data_tar)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="生成演示 deb")
    args = ap.parse_args()

    cfg = load_config()
    debs_dir = ROOT / "debs"
    debs_dir.mkdir(exist_ok=True)

    if args.demo:
        demo_name = "com.vvios.hello_1.0.0_iphoneos-arm64.deb"
        (debs_dir / demo_name).write_bytes(make_demo_deb())
        print(f"生成演示包: debs/{demo_name}")

    # 必须是相对源根目录的路径，以 ./ 开头
    deb_files = sorted(p for p in debs_dir.glob("*.deb"))
    entries = []
    for p in deb_files:
        ctrl, raw, data_size = read_deb(p)
        rel = f"./debs/{p.name}"
        entries.append(build_entry(ctrl, raw, data_size, rel))
        print(f"  索引 {p.name}: {ctrl.get('Package')} {ctrl.get('Version')}")

    packages_text = "\n\n".join(entries)
    if packages_text:
        packages_text += "\n"
    raw_index = packages_text.encode("utf-8")

    # mtime=0 保证确定性输出，否则每次生成的字节都不同
    gz = gzip.compress(raw_index, 9, mtime=0)
    bz = bz2.compress(raw_index, 9)

    write_text_lf(ROOT / "Packages", packages_text)
    (ROOT / "Packages.gz").write_bytes(gz)
    (ROOT / "Packages.bz2").write_bytes(bz)

    # Release —— 校验和必须基于磁盘上真实字节
    files = [("Packages", raw_index), ("Packages.gz", gz), ("Packages.bz2", bz)]
    md5_lines = "\n".join(f" {md5(b)} {len(b)} {n}" for n, b in files)
    sha_lines = "\n".join(f" {sha256(b)} {len(b)} {n}" for n, b in files)
    date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

    release = (
        f"Origin: {cfg['origin']}\n"
        f"Label: {cfg['label']}\n"
        f"Suite: {cfg['suite']}\n"
        f"Version: {cfg['version']}\n"
        f"Codename: {cfg['codename']}\n"
        f"Architectures: {' '.join(cfg['architectures'])}\n"
        f"Components: {cfg['component']}\n"
        f"Description: {cfg['description']}\n"
        f"Date: {date}\n"
        f"MD5Sum:\n{md5_lines}\n"
        f"SHA256:\n{sha_lines}\n"
    )
    write_text_lf(ROOT / "Release", release)

    # 落地页数据：浏览器解不了 .gz，所以另存 JSON
    pkg_list = []
    for p in deb_files:
        ctrl, raw, data_size = read_deb(p)
        pkg_list.append({
            "package": ctrl.get("Package", ""),
            "name": ctrl.get("Name", ctrl.get("Package", "")),
            "version": ctrl.get("Version", ""),
            "architecture": ctrl.get("Architecture", ""),
            "description": ctrl.get("Description", "").split("\n")[0],
            "section": ctrl.get("Section", ""),
            "author": ctrl.get("Author", ctrl.get("Maintainer", "")),
            "size": len(raw),
            "filename": f"./debs/{p.name}",
            "sha256": sha256(raw),
        })
    payload = {
        "origin": cfg["origin"],
        "label": cfg["label"],
        "name": cfg["name"],
        "description": cfg["description"],
        "maintainer": cfg["maintainer"],
        "url": cfg["url"],
        "icon": cfg["icon"],
        "accent": cfg["accent"],
        "architectures": cfg["architectures"],
        "packages": pkg_list,
        "generated": int(time.time()),
    }
    write_text_lf(ROOT / "packages.json", json.dumps(payload, ensure_ascii=False, indent=2))

    # file:// 也能读的数据（fetch 会被 CORS 拦死）
    js = "window.SILEO_REPO = " + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n"
    write_text_lf(ROOT / "repo-data.js", js)

    print(f"\n索引完成：{len(deb_files)} 个包")
    print(f"  Packages      {len(raw_index)} B  md5={md5(raw_index)}")
    print(f"  Packages.gz   {len(gz)} B  md5={md5(gz)}")
    print(f"  Packages.bz2  {len(bz)} B  md5={md5(bz)}")


if __name__ == "__main__":
    main()
