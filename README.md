# vv-ios 越狱插件源

面向 Dopamine / palera1n（rootless）的 Sileo / Cydia 软件源，托管在 GitHub Pages。

## 源地址

```
https://vv-ios.github.io/
```

Sileo → 软件源 → `+` → 粘贴上面的地址（**必须以 `/` 结尾**）。

## 仓库结构

```
.
├── index.html            # 落地页
├── CydiaIcon.png         # 源图标 90x90
├── repo.json             # 源配置（改这里，别改生成物）
├── Packages              # 索引（Sileo 读）
├── Packages.gz
├── Packages.bz2          # Sileo 优先读这个
├── Release               # 元信息 + MD5Sum/SHA256
├── packages.json         # 落地页数据副本
├── repo-data.js          # 同上，供 file:// 打开时使用
├── debs/                 # 所有 .deb
└── tools/
    ├── gen_repo.py       # 索引生成器（纯 Python，不需要 dpkg-deb）
    ├── publish.py        # 一键发布（push 失败自动改走 Contents API）
    └── build-repo.yml    # 自动构建工作流（需手动放到 .github/workflows/）
```

## 发新包

```bash
# 1. 把编译好的 deb 丢进 debs/
cp ~/my-tweak.deb debs/

# 2. 一键发布（重建索引 + 提交 + 推送）
python tools/publish.py -m "add my-tweak 1.0.0"
```

只要走这一条命令就够了。它会自动重建 `Packages` / `Release` / `packages.json`，
然后优先尝试 `git push`；如果 `github.com:443` 不通，自动改走 Contents API 逐文件上传。

## 本地重建索引

```bash
python tools/gen_repo.py          # 扫描 debs/ 重建
python tools/gen_repo.py --demo   # 额外生成一个验证用 deb
```

## 几个容易踩的坑（已在本仓库处理）

| 坑 | 本仓库的处理 |
|---|---|
| `.gitignore` 忽略掉 `Packages` / `Release` | 已显式 `!Packages` `!Release` 等，索引会正常提交 |
| Windows 把 LF 转成 CRLF 导致校验和失配 | `.gitattributes` 强制 `eol=lf`；脚本用字节写入 |
| `gzip.compress` 的 mtime 导致每次字节都不同 | 固定 `mtime=0`，输出确定性 |
| Pages 的 `build_type=workflow` 但没有可用工作流 | 需确认是 `legacy` |
| 忘了 `.nojekyll` | 已有 |
| 提交动了 `.github/workflows/` 被拒（token 无 `workflow` scope） | 工作流放在 `tools/`，需要时在网页上手动添加 |

## 架构

`Release` 里声明了 `iphoneos-arm64 iphoneos-arm` 两种，rootless 与 rootful 都能用。
debian 包的 `Architecture` 字段必须与实际相符（rootless = `iphoneos-arm64`）。
