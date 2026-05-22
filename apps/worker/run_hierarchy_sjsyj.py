"""
针对 MinerU 解析输出的 MD 文件，运行 pred_titles 完整流程
输出 preds_3_llm_base.csv 和 preds_5_final_output.csv 到同目录
"""

import os
import sys
import json
import shutil

# ── 配置 ──
MODEL_NAME = "qwen3.5-27b"
ENABLE_THINKING = False

# 目标 MD 文件
TARGET_MD = (
    "/Users/wuchengke/Desktop/temp/ontos/parse_comparison/"
    "sjsyj_mineru_notable/SJSYJ-SC-2024 企业制度汇编（上册）/txt/"
    "SJSYJ-SC-2024 企业制度汇编（上册）.md"
)
OUTPUT_DIR = os.path.dirname(TARGET_MD)

# ── 工程路径注入 ──
WORKER_DIR = "/Users/wuchengke/Desktop/knowhere/knowhereapi-main/apps/worker"
SHARED_DIR = "/Users/wuchengke/Desktop/knowhere/knowhereapi-main/packages/shared-python"
sys.path.insert(0, SHARED_DIR)
sys.path.insert(0, WORKER_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(WORKER_DIR, ".env"))
os.environ["LOCAL_DEBUG"] = "1"

import openai
from loguru import logger

# ── Qwen thinking mode 关闭 patch ──
_original_create = openai.resources.chat.completions.Completions.create

def patched_create(self, *args, **kwargs):
    kwargs.setdefault("extra_body", {})
    kwargs["extra_body"]["enable_thinking"] = False
    return _original_create(self, *args, **kwargs)

openai.resources.chat.completions.Completions.create = patched_create
logger.info("🚫 Thinking mode 已关闭 (enable_thinking=false)")

from app.services.document_parser.structure.layout_parser import pred_titles
from app.services.document_parser.structure.toc_parser import detect_tocs_in_texts
from app.services.document_parser.formats.html.parser import merge_html_tables


def main():
    logger.info("=" * 60)
    logger.info(f"🚀 SJSYJ hierarchy 检测 — 模型: {MODEL_NAME}")
    logger.info(f"   输入: {TARGET_MD}")
    logger.info(f"   输出: {OUTPUT_DIR}")
    logger.info("=" * 60)

    # 1. 加载 MD
    with open(TARGET_MD, "r", encoding="utf-8") as f:
        md_lines = f.read().splitlines()
    md_lines = [line.strip() for line in md_lines if line.strip()]
    md_lines = merge_html_tables(md_lines)
    logger.info(f"📄 MD 加载完毕: {len(md_lines)} 行")

    # 2. TOC 检测
    toc_json_path = os.path.join(OUTPUT_DIR, "toc_hierarchies.json")
    toc_hierarchies = None
    if os.path.exists(toc_json_path):
        os.remove(toc_json_path)
        logger.info("🗑️  已删除旧 toc_hierarchies.json")

    logger.info("🔍 检测 TOC...")
    toc_hierarchies, md_lines = detect_tocs_in_texts(md_lines, model_name=MODEL_NAME)
    toc_hierarchies = toc_hierarchies or []
    if toc_hierarchies:
        with open(toc_json_path, "w", encoding="utf-8") as f:
            json.dump(toc_hierarchies, f, ensure_ascii=False, indent=2)
        logger.info(f"✅ TOC 检测完毕: {len(toc_hierarchies)} 个区域 → {toc_json_path}")
    else:
        logger.info("   未检测到 TOC")

    # 3. 运行 pred_titles（完整流程，输出 CSV）
    logger.info(f"🧠 运行 pred_titles (model={MODEL_NAME}, smart_parse=True)...")
    heading_preds = pred_titles(
        infos=md_lines,
        doc_type="md",
        toc_hierarchies=toc_hierarchies or [],
        prompt_limt=4000,
        enable_regx=True,
        smart_parse=True,
        model_name=MODEL_NAME,
        output_dir=OUTPUT_DIR,      # ← CSV 保存到这里
        layout_json_path=None,
    )

    if heading_preds.empty:
        logger.warning("⚠️ 没有检测到任何有效标题")
        return

    valid = heading_preds[heading_preds["level"] > 0]
    logger.info(f"✅ 完成! 有效标题 {len(valid)} 个 / 总行 {len(heading_preds)}")
    logger.info(f"   层级分布:\n{heading_preds['level'].value_counts().sort_index().to_string()}")

    # 汇报生成的 CSV
    for csv_name in ["preds_3_llm_base.csv", "preds_4_llm_final.csv", "preds_5_final_output.csv"]:
        p = os.path.join(OUTPUT_DIR, csv_name)
        if os.path.exists(p):
            size_kb = os.path.getsize(p) / 1024
            logger.info(f"   📄 {csv_name} → {p} ({size_kb:.0f} KB)")

    logger.info("=" * 60)
    logger.info("🎉 Done!")


if __name__ == "__main__":
    main()
