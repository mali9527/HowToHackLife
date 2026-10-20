#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 马力及贡献者
"""检查公开页面的格式、引用与内部信息；不核实事实，不评文风。许可见 LICENSE。"""
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from urllib.parse import unquote

RULES = [
    ("来源编号", r"S\d{4}"),
    ("断言号", r"断言\s*\d+"),
    ("内部编号", r"(?<![A-Za-z0-9])(?:SOC1|SOC2|LIF1|LIF2|AGE|CAR|CON|DEV|EDU|FAM|FIN|HOU|LAB|LAW|MOV|PLT|TAX|TEL|TRV)-\d{3}(?!\d)"),
    ("内部用语", r"本表|本题|本页|原页|旧页|本轮|本次"),
    ("核对口吻", r"refs|拆表|核表|另核|待拆|没核(?:到)?|反例|已核到|先核(?!对|验|实)"),
    ("内部路径", r"(?:questions|sources|packs|pipeline|checks|build|qa|research|spec|goals|过程)/[^\s，。；“”]*"),
    ("取件备注", r"textutil|cmux|命令行|脚本壳|浏览器读取|需要浏览器|未登录|抽文|域代码|未逐省读|未取到|正文\.txt|(?<!llms)\.txt\b|原件\.[A-Za-z]+|取件|\.py\b|pdftotext|下载[^，。]*后抽取|抽取"),
    ("内部字段", r"sensitive|author_viewed|事实表|读者稿|代理\.yaml|过程/|选题红线|红线|黄区|监工"),
    ("内部流程", r"复核(?:报告|补丁|会话|记录|阶段|流程|者|意见|\.md|\.yaml)|(?:核表|核补丁|会话)复核|复核[-－]"),
    ("模型名", r"(?i)(?<![a-z])(claude|codex|gpt|opus|sonnet|astra|fable|gemini)(?![a-z])"),
]
PROCESS_OK = r"材料(进行)?复核|再复核|重新复核|复核材料|复核结果|申请复核|复核完毕|复核申请|认定复核|复核程序"
UNCHECKED_LABEL = "没核到"
RULES = [(kind, re.compile(pattern)) for kind, pattern in RULES]
PROCESS_OK = re.compile(PROCESS_OK)
NUMBER_PATH_KINDS = {"来源编号", "断言号", "内部编号", "内部路径"}
PAGE_PATH = re.compile(r"题库/[^/]+/([0-9]{4})\.md")
CITATION = re.compile(r"\[([0-9]{1,3})\]")
SOURCE_LINE = re.compile(r"^\[([0-9]+)\]\s+\S")
LINK = re.compile(r'\]\((<[^>\n]+>|(?:\\.|[^()\\\n]|\([^()\n]*\))*)\)')


def changed_files(root):
    """只读本地差异；不取远端、不依赖凭证，不能确定范围时返回空值以全库查。"""
    event = os.environ.get("GITHUB_EVENT_PATH")
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request" or not event:
        return None
    try:
        request = json.loads(Path(event).read_text(encoding="utf-8"))["pull_request"]
        base, head = request["base"]["sha"], request["head"]["sha"]
        if not all(isinstance(s, str) and re.fullmatch(r"[0-9a-fA-F]{40,64}", s)
                   for s in (base, head)):
            return None
        result = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--no-textconv", "--name-only",
             "--diff-filter=ACMRT", "--no-renames", "-z", base + "..." + head, "--"],
            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            check=False)
        if result.returncode:
            return None
        names = result.stdout.decode("utf-8").split("\0")
        names = [name for name in names if name]
        if any(PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
               for name in names):
            return None
        return names
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return None


def file_kind(name):
    if name in ("README.md", "题库/README.md") or re.fullmatch(r"题库/[^/]+/README\.md", name):
        return "索引"
    if name.startswith("题库/") and name.endswith(".md"):
        return "题页"
    if name in ("参考文献总表.md", "Hot100.md") or name.startswith("专题/"):
        return "索引"
    return None


def check_links(root, name, lines):
    root = root.resolve()
    errors = []
    for ln, line in enumerate(lines, 1):
        for match in LINK.finditer(line):
            target = match.group(1).strip()
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            else:
                target = re.sub(r'\s+[\"\x27].*[\"\x27]$', '', target)
            if target.lower().startswith(("http", "#", "mailto:")):
                continue
            local = unquote(target.split("#", 1)[0])
            local = re.sub(r"\\([() ])", r"\1", local)
            path = (root / name).parent / local
            try:
                path.resolve().relative_to(root)
                valid = bool(local) and not Path(local).is_absolute() and path.is_file()
            except (OSError, ValueError):
                valid = False
            if not valid:
                errors.append((ln, "链接失效", target))
    return errors


def scan_leaks(lines, numbers_only=False):
    findings = []
    excerpts = False
    for ln, line in enumerate(lines, 1):
        if line.strip() == "<summary>原文摘录</summary>":
            excerpts = True
        elif line.strip() == "</details>" or line.startswith("## "):
            excerpts = False
        quote = (re.fullmatch(r'\[\d+\] (?:.*?：)?“(?P<quote>[^“”]*)”', line)
                 if excerpts else None)
        for kind, pattern in RULES:
            if numbers_only and kind not in NUMBER_PATH_KINDS:
                continue
            for match in pattern.finditer(line):
                in_quote = (quote and quote.start("quote") <= match.start()
                            and match.end() <= quote.end("quote"))
                if kind == "内部用语" and in_quote:
                    continue
                if (kind == "核对口吻" and match.group() == UNCHECKED_LABEL
                        and not in_quote and not line[:match.start()].strip()
                        and line[match.end():].startswith("：")):
                    continue
                if kind == "内部流程" and PROCESS_OK.search(
                        line[max(0, match.start() - 8):match.end() + 2]):
                    continue
                findings.append((ln, kind, line[max(0, match.start() - 12):match.end() + 12]))
    return findings


def check_page(name, lines):
    errors, warnings = [], []

    def fail(ln, rule, message):
        context = lines[ln - 1].strip() if lines and 0 < ln <= len(lines) else "（空文件）"
        errors.append((ln, rule, message + "；" + context))

    path_match = PAGE_PATH.fullmatch(name)
    if not path_match:
        fail(1, "题页路径", "应为题库、领域、四位题号加扩展名的三级路径")
    if not lines or not re.fullmatch(r"# [^\s#].*", lines[0]):
        fail(1, "题目格式", "第一行应为井号、空格和题目")
    headings = {heading: [i for i, line in enumerate(lines) if line.strip() == heading]
                for heading in ("## 解析", "## 要点和依据", "## 依据和出处")}
    for heading, positions in headings.items():
        if len(positions) != 1:
            fail(positions[1] + 1 if len(positions) > 1 else 1,
                 "必需章节", "须有且只有一个“" + heading + "”")
    starts = [positions[0] for positions in headings.values() if positions]
    if starts != sorted(starts):
        fail(starts[0] + 1, "章节顺序", "解析、要点和依据、依据和出处须依次排列")
    answer_end = headings["## 解析"][0] if headings["## 解析"] else len(lines)
    answer = re.search(r"(?m)^\*\*答案\*\*([^\n]*(?:\n.*)*)", "\n".join(lines[:answer_end]))
    if not answer or not answer.group(1).strip():
        fail(1, "答案段", "解析前须有非空的答案段，以双星号包住答案二字")
    footer_ln = next((i for i in range(len(lines), 0, -1) if lines[i - 1].strip()), 1)
    footer = re.search(r"第\s+([0-9]{4})\s+题", lines[footer_ln - 1] if lines else "")
    if not footer:
        fail(footer_ln, "页尾题号", "最后一个非空行须有第 NNNN 题")
    elif path_match and footer.group(1) != path_match.group(1):
        fail(footer_ln, "页尾题号", "题号与文件名不一致，应为 " + path_match.group(1))

    declared, used = {}, []
    section, excerpts = "", False
    for ln, line in enumerate(lines, 1):
        if line.startswith("## "):
            section, excerpts = line.strip(), False
        elif line.strip() == "<summary>原文摘录</summary>":
            excerpts = True
        elif line.strip() == "</details>":
            excerpts = False
        if section == "## 依据和出处" and not excerpts:
            source = SOURCE_LINE.match(line)
            if source:
                declared.setdefault(source.group(1), ln)
        else:
            used.extend((match.group(1), ln) for match in CITATION.finditer(line))
    for number, ln in used:
        if number not in declared:
            fail(ln, "引用缺出处", "引用 [" + number + "] 在依据和出处中没有对应行")
    used_numbers = {number for number, _ in used}
    for number, ln in declared.items():
        if number not in used_numbers:
            warnings.append((ln, "孤立出处（提示）", "[" + number + "] 未在正文、要点或摘录中使用；" + lines[ln - 1]))
    return errors, warnings


def main():
    root = Path(__file__).resolve().parents[2]
    all_names = [path.relative_to(root).as_posix() for directory in ("题库", "专题")
                 for path in (root / directory).rglob("*") if path.is_file()]
    all_names += [name for name in ("README.md", "参考文献总表.md", "Hot100.md")
                  if (root / name).exists()]
    names = changed_files(root)
    if names is None:
        print("未取得改动清单，检查全库。")
        names = all_names
    else:
        print("已取得改动清单，格式与内部信息只检查改动文件；链接检查全库。")
    selected = set(names)
    error_count = warning_count = checked = 0
    for name in sorted(selected | set(all_names)):
        kind = file_kind(name)
        if kind is None:
            continue
        path = root / name
        checked += 1
        try:
            path.resolve().relative_to(root)
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, ValueError):
            print(f"{name}:1: 文件读取: 无法读取仓库内的 UTF-8 文件")
            error_count += 1
            continue
        errors, warnings = check_page(name, lines) if kind == "题页" and name in selected else ([], [])
        if name in selected:
            errors += scan_leaks(lines, numbers_only=(kind != "题页"))
        errors += check_links(root, name, lines)
        for ln, rule, context in errors + warnings:
            print(f"{name}:{ln}: {rule}: {context}")
        error_count += len(errors)
        warning_count += len(warnings)
    print(f"检查完成：{checked} 个文件，{error_count} 个硬错，{warning_count} 个提示。")
    return 1 if error_count else 0


if __name__ == "__main__":
    sys.exit(main())
