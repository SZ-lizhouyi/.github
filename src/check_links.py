#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档链接健康检查工具。

用途
----
扫描仓库内的 Markdown / YAML 文件，检查其中的链接是否有效。它是针对
OpenHUTB/.github 这一类「以文档为主要产出」的仓库设计的：本仓库的正文里
有大量指向同级文档、图片以及组织下其他仓库的链接，一旦目标改名或页面被
删，链接就会静默失效 —— 读者点开才会发现，维护者很难靠人工巡检发现。
本工具把这些检查放到本地和 CI 里，在 PR 阶段就拦住。

检查项
------
1. 相对路径链接：目标文件在仓库内是否存在（含图片、视频等资源）。
2. 页内锚点：`#xxx` 指向的标题锚点在当前页面中是否存在。
3. 跨文件锚点：`other.md#xxx` 同时校验文件和锚点。
4. 未加 `mailto:` 的裸邮箱地址（会被当成相对路径）。
5. 仓库地址（可选，需联网）：`github.com/OpenHUTB/<repo>` 形式的链接，
   该仓库在组织下是否真实存在。默认关闭。

设计取舍
--------
- 只依赖标准库，不需要 pip 安装任何东西，CI 里几乎零成本。
- 默认不联网：外部链接的可用性受网络环境影响，容易产生误报，因此把
  网络检查做成可选的 `--check-github` 开关。
- 支持通过 `--allow` 放行已知问题：有些链接是**故意**暂时失效的（例如
  指向尚未补写的页面 / 章节），全站长期红着会让大家习惯性忽略告警，反而
  失去意义。放行条目应当写明原因，并在问题修好后删掉，让检查逐步收紧。

用法
----
    # 基本检查（离线）
    python src/check_links.py

    # 同时联网校验 GitHub 仓库地址
    python src/check_links.py --check-github

    # 放行已知问题（见下方「放行条目语法」）
    python src/check_links.py --allow "docs/simulator.md" \
                              "docs/ask_question.md#声明"

    # 在 CI 中，发现任何问题即以非零码退出
    python src/check_links.py --strict

放行条目语法
------------
    docs/simulator.md                  放行「指向该文件」的链接
    docs/ask_question.md#声明          只放行该文件里的这一个页内锚点
    docs/ask_question.md               放行该文件内所有页内锚点

退出码
------
    0  未发现问题（被 --allow 放行的除外）
    1  发现失效链接（仅在 --strict 下）
"""

import argparse
import os
import posixpath
import re
import sys
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------
# 已知需要放行的目标。
# 每条都应当注明原因，问题修好后请删掉，让检查恢复严格。
# 例：
#     "docs/simulator.md",              # 页面尚未补写，见 issue #xx
#     "docs/ask_question.md#声明",      # 章节内容缺失，见 issue #xx
# --------------------------------------------------------------------------
ALLOWLIST = set()

MD_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HTML_LINK = re.compile(r'(?:src|href)\s*=\s*(["\'])([^"\']+)\1')
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.M)
EXPLICIT_ID = re.compile(r"""id\s*=\s*['"]([^'"]+)['"]""")
GITHUB_REPO = re.compile(r"https?://github\.com/OpenHUTB/([A-Za-z0-9._-]+)")

SKIP_DIRS = {".git", "site", "node_modules", "__pycache__", ".venv"}


def repo_files(root):
    """返回仓库内所有文件的相对路径（POSIX 风格）。"""
    out = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out.add(rel)
    return out


def slugify(text):
    """复刻 mkdocs 在「保留中文」配置下的锚点生成规则。

    注意：mkdocs 的**默认** slugify 会先做 NFKD 归一化再丢弃所有非 ASCII
    字符，中文标题会变成空串，锚点退化为 _1、_2 这类占位符。本项目已通过
    mkdocs.yml 里 `toc.slugify` 指向 pymdownx.slugs.slugify 修正为保留中文，
    所以这里按修正后的规则实现：

        标题文本 -> 去掉内联 HTML -> 转小写 -> 丢弃标点 -> 空格转连字符
    """
    text = re.sub(r"<[^>]+>", "", text)          # 去掉内联 HTML
    text = text.strip().lower()
    # 保留：字母、数字、下划线、中文、空格、连字符
    text = re.sub(r"[^\w\u4e00-\u9fff\s-]", "", text)
    text = re.sub(r"[\s]+", "-", text)
    return text.strip("-")


def anchors_of(path):
    """抽取一个 Markdown 文件里所有可用的锚点。"""
    ids = set()
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ids

    for m in EXPLICIT_ID.finditer(text):
        ids.add(m.group(1))
    for m in HEADING.finditer(text):
        ids.add(slugify(m.group(2)))
    return ids


def iter_links(path):
    """逐个产出文件中的链接。"""
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().split("\n")
    except OSError:
        return
    for lineno, line in enumerate(lines, 1):
        for m in MD_LINK.finditer(line):
            yield lineno, m.group(1)
        for m in HTML_LINK.finditer(line):
            yield lineno, m.group(2)


def is_external(url):
    return url.startswith(("http://", "https://", "mailto:", "ftp://"))


def allowed_file(rel_url):
    """判断「指向某个文件」的链接是否被放行。"""
    return rel_url in ALLOWLIST or rel_url.lstrip("./") in ALLOWLIST


def allowed_anchor(src_rel, url):
    """判断页内锚点 / 跨文件锚点是否被放行。

    放行条目可以写成 `文件#锚点` 精确放行一条，也可以只写 `文件` 放行该
    文件里所有的页内锚点。
    """
    if f"{src_rel}{url}" in ALLOWLIST or src_rel in ALLOWLIST:
        return True
    # 指向自己所在文件的写法，例如 `./xxx.md#abc`
    if url.startswith("#") and src_rel.split("/")[-1] in ALLOWLIST:
        return True
    return False


def check(root, allow_github):
    files = repo_files(root)
    problems = []
    gh_cache = {}
    all_anchors = {}

    targets = sorted(f for f in files if f.endswith((".md", ".yml", ".yaml")))

    for rel in targets:
        full = os.path.join(root, rel.replace("/", os.sep))
        for lineno, raw in iter_links(full):
            url = raw.strip()
            if not url:
                continue

            # --- 纯页内锚点 ---
            if url.startswith("#"):
                if len(url) <= 1:
                    continue
                anc = url[1:]
                if allowed_anchor(rel, url):
                    continue
                if anc not in all_anchors.setdefault(rel, anchors_of(full)):
                    problems.append((rel, lineno, url, "页内锚点不存在"))
                continue

            # --- 未加 mailto: 的裸邮箱 ---
            if (
                "@" in url
                and not is_external(url)
                and "/" not in url
                and "." in url
            ):
                problems.append((rel, lineno, url, "应为 mailto: 链接"))
                continue

            # --- 外部链接 ---
            if is_external(url):
                if allow_github:
                    for repo in GITHUB_REPO.findall(url):
                        if repo not in gh_cache:
                            gh_cache[repo] = github_repo_exists(repo)
                        if not gh_cache[repo]:
                            problems.append(
                                (rel, lineno, url, f"仓库 OpenHUTB/{repo} 不存在")
                            )
                continue

            path_part, _, frag = url.partition("#")
            if not path_part:
                continue

            decoded = urllib.parse.unquote(path_part)
            resolved = posixpath.normpath(
                posixpath.join(posixpath.dirname(rel), decoded)
            )
            if resolved not in files:
                if not allowed_file(resolved) and not allowed_file(url):
                    problems.append((rel, lineno, url, f"目标文件不存在：{resolved}"))
                continue

            if frag and resolved.endswith(".md"):
                if allowed_anchor(rel, "#" + frag) or allowed_file(resolved):
                    continue
                if frag not in all_anchors.setdefault(
                    resolved,
                    anchors_of(os.path.join(root, resolved.replace("/", os.sep))),
                ):
                    problems.append((rel, lineno, url, "跨文件锚点不存在"))

    return problems


def github_repo_exists(name):
    url = f"https://api.github.com/repos/OpenHUTB/{name}"
    req = urllib.request.Request(url, headers={"User-Agent": "openhutb-link-check"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        return e.code != 404
    except Exception:
        # 限流、网络问题等不判定为失效，避免误报
        return True


def main():
    ap = argparse.ArgumentParser(description="文档链接健康检查")
    ap.add_argument("--root", default=".", help="仓库根目录（默认当前目录）")
    ap.add_argument(
        "--allow",
        nargs="*",
        default=[],
        help="放行的目标，见文件头部「放行条目语法」",
    )
    ap.add_argument(
        "--check-github",
        action="store_true",
        help="联网校验 github.com/OpenHUTB/<repo> 是否存在",
    )
    ap.add_argument(
        "--strict", action="store_true", help="发现问题时以非零码退出（CI 用）"
    )
    args = ap.parse_args()

    ALLOWLIST.update(args.allow)

    problems = check(os.path.abspath(args.root), args.check_github)

    if not problems:
        print("链接检查通过，未发现问题。")
        return 0

    print(f"发现 {len(problems)} 处问题：\n")
    seen = set()
    for rel, lineno, url, why in problems:
        key = (rel, url, why)
        if key in seen:
            continue
        seen.add(key)
        print(f"  {rel}:{lineno}")
        print(f"      链接 -> {url}")
        print(f"      原因 -> {why}")
    print(f"\n合计 {len(seen)} 处（去重后）")

    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
