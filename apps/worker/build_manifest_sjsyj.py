"""
从 preds_5_final_output.csv 构建 HIERARCHY 树，
输出 manifest.json 并与原始 MD 的 # 标题对比
"""

import json
import os
import re
import pandas as pd
from collections import OrderedDict

CSV_PATH = (
    "/Users/wuchengke/Desktop/temp/ontos/parse_comparison/"
    "sjsyj_mineru_notable/SJSYJ-SC-2024 企业制度汇编（上册）/txt/"
    "preds_5_final_output.csv"
)
MD_PATH = (
    "/Users/wuchengke/Desktop/temp/ontos/parse_comparison/"
    "sjsyj_mineru_notable/SJSYJ-SC-2024 企业制度汇编（上册）/txt/"
    "SJSYJ-SC-2024 企业制度汇编（上册）.md"
)
OUT_DIR = os.path.dirname(CSV_PATH)
SOURCE_FILE = "SJSYJ-SC-2024 企业制度汇编（上册）.pdf"


def clean_heading(h: str) -> str:
    """去掉 Markdown # 前缀和前后空格"""
    return re.sub(r"^#+\s*", "", str(h)).strip()


def build_hierarchy(df: pd.DataFrame) -> dict:
    """从有层级的行构建嵌套字典树"""
    root = OrderedDict()
    root["Root"] = {}
    stack = []  # [(level, dict)]

    valid = df[df["level"].astype(str).str.match(r"^[1-9]")].copy()
    valid["level"] = valid["level"].astype(int)

    for _, row in valid.iterrows():
        title = clean_heading(str(row["heading"]))
        level = int(row["level"])

        # 弹栈到当前父节点
        while stack and stack[-1][0] >= level:
            stack.pop()

        parent = stack[-1][1] if stack else root
        # 处理重名
        key, suffix = title, 2
        while key in parent:
            key = f"{title} ({suffix})"
            suffix += 1
        parent[key] = OrderedDict()
        stack.append((level, parent[key]))

    return root


def extract_md_headings(md_path: str) -> list[dict]:
    """从 MD 文件提取所有 # 标题"""
    headings = []
    with open(md_path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            m = re.match(r"^(#{1,6})\s+(.+)", line.rstrip())
            if m:
                level = len(m.group(1))
                text = m.group(2).strip()
                headings.append({"lineno": lineno, "level": level, "text": text})
    return headings


def compare_with_md(hierarchy: dict, md_headings: list[dict]):
    """对比 hierarchy 树中的标题 vs MD 中的 # 标题"""
    def flatten(d, prefix="", result=None):
        if result is None:
            result = []
        for k, v in d.items():
            result.append(k)
            if isinstance(v, dict) and v:
                flatten(v, prefix + k + "/", result)
        return result

    tree_titles = set(flatten(hierarchy))
    tree_titles.discard("Root")

    md_texts = set(h["text"] for h in md_headings)

    in_tree_not_md = tree_titles - md_texts
    in_md_not_tree = md_texts - tree_titles
    both = tree_titles & md_texts

    print(f"\n{'='*60}")
    print(f"📊 标题对比")
    print(f"{'='*60}")
    print(f"  MD 中 # 标题数:      {len(md_texts)}")
    print(f"  Hierarchy 树标题数:  {len(tree_titles)}")
    print(f"  完全匹配:            {len(both)}")
    print(f"  仅在 Hierarchy 中:   {len(in_tree_not_md)}")
    print(f"  仅在 MD 中:          {len(in_md_not_tree)}")

    if in_tree_not_md:
        print(f"\n⚠️  Hierarchy 有但 MD # 里没有（LLM 可能误识别）:")
        for t in sorted(in_tree_not_md)[:15]:
            print(f"    - {t[:80]}")

    if in_md_not_tree:
        print(f"\n⚠️  MD # 有但 Hierarchy 没有（可能被降级为正文）:")
        for t in sorted(in_md_not_tree)[:15]:
            print(f"    - {t[:80]}")

    return {"matched": len(both), "only_tree": len(in_tree_not_md), "only_md": len(in_md_not_tree)}


def main():
    print(f"📄 读取: {CSV_PATH}")
    df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
    print(f"   总行数: {len(df)}")

    # 构建树
    hierarchy = build_hierarchy(df)
    top_level = [k for k in hierarchy if k != "Root"]
    print(f"   顶层章节数: {len(top_level)}")

    # 输出 manifest.json
    manifest = {
        "version": "2.0",
        "job_id": SOURCE_FILE,
        "source_file_name": SOURCE_FILE,
        "processing_date": "2026-05-21T04:46:35Z",
        "statistics": {
            "total_chunks": int((df["level"] == -1).sum()),
            "heading_count": int((df["level"].astype(str).str.match(r"^[1-9]")).sum()),
        },
        "HIERARCHY": hierarchy,
    }

    out_path = os.path.join(OUT_DIR, "manifest.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    size_kb = os.path.getsize(out_path) / 1024
    print(f"\n✅ 已保存: {out_path} ({size_kb:.0f} KB)")

    # 预览顶层结构
    print(f"\n📋 顶层章节（前 20）:")
    for i, k in enumerate(top_level[:20], 1):
        children = hierarchy[k]
        child_count = len(children) if isinstance(children, dict) else 0
        print(f"  L1 [{i:2d}] {k[:60]}{'...' if len(k)>60 else ''} → {child_count} 子节点")

    # 与 MD 对比
    print(f"\n📄 读取 MD: {MD_PATH}")
    md_headings = extract_md_headings(MD_PATH)
    print(f"   MD # 标题数: {len(md_headings)}")
    compare_with_md(hierarchy, md_headings)


if __name__ == "__main__":
    main()
