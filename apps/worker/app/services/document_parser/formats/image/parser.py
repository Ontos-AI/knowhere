# pyright: reportArgumentType=false, reportCallIssue=false, reportOptionalSubscript=false
import base64
import hashlib
import io
import os
import re
import threading
from pathlib import Path

import pandas as pd
from app.services.document_parser.tables.dataframe_helpers import process_dup_paths_df
from app.services.document_parser.support.identifiers import gen_str_codes, get_str_time
from app.services.document_parser.support.parser_rows import (
    PARSER_ROW_COLUMNS,
    ParsedRow,
    ParsedRowsBuilder,
)
from loguru import logger
from PIL import Image

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import (
    ImageParsingException,
    LLMServiceException,
)
from shared.core.exceptions.knowhere_exception import KnowhereException
from shared.services.ai.prompt_service import build_prompt
from shared.services.ai.response_process_service import eval_response
from shared.utils.chunk_refs import build_chunk_ref
from app.services.common.file_loading import is_remote, load_file_bytes
from app.services.common.file_utils import path_handle
from shared.services.ai.summary.engine import summarize, transcribe
from shared.services.ai.llm_overrides import get_vision_client
from shared.services.ai.openai_compatible_client_sync import OpenAICompatibleClientSync

MD_IMAGE_PATTERN = r"!\[[^\]]*?\]\((.*?\.(?:png|jpe?g|gif))\)"
g_img_lock = threading.Lock()


def perceptual_hash(data: bytes) -> str:
    """Compute a normalized pixel-data hash for image dedup.

    Word/PDF may embed the same visual image with different compression
    or metadata, making raw-byte SHA256 differ.  This function decodes
    the image, converts to RGBA, and hashes the raw pixel buffer so
    that visually-identical images always produce the same digest.

    Falls back to raw-bytes hash when PIL cannot decode the data.
    """
    try:
        img = Image.open(io.BytesIO(data))
        pixels = img.convert("RGBA").tobytes()
        return hashlib.sha256(pixels).hexdigest()
    except Exception:
        return hashlib.sha256(data).hexdigest()



def _get_vision_client() -> OpenAICompatibleClientSync:
    """Create OpenAI-compatible client for vision models, auto-routing by IMAGE_MODEL name."""
    image_model = settings.IMAGE_MODEL or "qwen3.6-flash"
    client, _ = get_vision_client(requested_model=image_model)
    return client


def image_bytes_to_base64(img_data: bytes, ext: str) -> str:
    mime_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(ext, "application/octet-stream")
    b64_data = base64.b64encode(img_data).decode("utf-8")
    return f"data:{mime_type};base64,{b64_data}"


def local_image_to_data_url(path, cut=True, min_size=None, max_size=None):
    from shared.core.constants import ProcessingConstants

    if min_size is None:
        min_size = ProcessingConstants.IMG_MIN_SIZE
    if max_size is None:
        max_size = ProcessingConstants.IMG_MAX_SIZE
    if not path.exists():
        logger.warning(f"Image path not found: {path}")
        return None

    if cut:
        file_size = path.stat().st_size  # Bytes.
        if file_size < min_size:  # Smaller than 10 KB.
            logger.debug(f"Skipping {path} (too small: {file_size / 1024:.1f} KB)")
            return None
        if file_size >= max_size:  # Larger than 5 MB.
            logger.debug(
                f"Skipping {path} (too large: {file_size / 1024 / 1024:.1f} MB)"
            )
            return None

    with open(path, "rb") as f:
        img_data_base64 = image_bytes_to_base64(f.read(), path.suffix.lower())
    return img_data_base64


def process_img_path4read(paths_, image_root_dir, cut):
    urls = []
    for path_ in paths_:
        if not is_remote(path_):
            resolved_image_root = Path(image_root_dir).resolve()
            url_ = local_image_to_data_url(resolved_image_root / path_, cut)
            if url_ is not None:
                urls.append(url_)
        else:
            urls.append(path_)
    return urls


def ask_image(
    client: OpenAICompatibleClientSync,
    image_root_dir,
    paths_,
    title_text="",
    task="summary-images",
    query="",
    max_tokens=None,
    size_cut=True,
):
    from shared.core.constants import ProcessingConstants

    if max_tokens is None:
        max_tokens = ProcessingConstants.IMG_MAX_TOKENS

    # Filter unsupported formats (sxjg logic)
    valid_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    valid_paths = []
    for p in paths_:
        ext = os.path.splitext(p)[-1].lower()
        if ext in valid_exts:
            valid_paths.append(p)
        else:
            logger.debug(f"Skipping unsupported image format: {p}")

    if not valid_paths:
        return None

    urls_ = process_img_path4read(valid_paths, image_root_dir, size_cut)

    if task in ("summary-images", "atlas-page-info"):
        image_model = settings.IMAGE_MODEL or "qwen3.6-flash"
    else:  # OCR and image type classification use higher-capability models
        image_model = settings.IMAGE_MODEL_MAX or "qwen3.6-flash"

    if len(urls_) > 0:
        client, image_model = get_vision_client(requested_model=image_model)
        prompt, temperature, top_p, max_tokens = build_prompt(
            task=task, texts=title_text, query=query, paras={"max_tokens": max_tokens}
        )
        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            }
        ]
        for url_ in urls_:
            url_header = {"type": "image_url", "image_url": {"url": url_}}
            messages[0]["content"].append(url_header)
        resp = ""
        try:
            resp = client.chat_completion(
                messages=messages,
                model=image_model,
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
                usage_task=f"parser.image.{task}",
            )
            logger.debug(f"Image understanding response: {resp}")
            # Only parse as JSON for tasks that return structured data
            if task in ("judge-image-type",):
                resp = eval_response(resp)
            else:
                # Text-output tasks: normalize "null" string → Python None
                if isinstance(resp, str) and resp.strip().lower() in ("null", "none"):
                    resp = None
            return resp
        except Exception as e:
            logger.error(f"Failed to understand image content: {e}\nResponse: {resp}")
            raise LLMServiceException(
                internal_message=f"Understanding image content failed: {str(e)}",
                provider="openai_image",
                original_exception=e,
            )
    else:
        return None


def detect_summary_img_md(line, last_context, image_root_dir):
    """Collect markdown image refs with a placeholder summary.

    The actual per-image summary is produced later by the deferred task path
    (``ImageDeferredSummaryTask`` → unified summary engine), so here we only emit
    a positional placeholder derived from the surrounding context.
    """
    imgs = []
    img_paths = re.findall(MD_IMAGE_PATTERN, line, flags=re.IGNORECASE)
    for i, ip in enumerate(img_paths):
        image_summary = last_context + str(i)
        imgs.append((ip, None, image_summary))
    return imgs


def parse_image(
    image_path,
    filename=None,
    output_dir=None,
    baseurl="",
    base_llm_paras=None,
    auto_rename=True,
    relative_root=None,
):
    split_char = settings.SPLIT_CHAR or "/"
    time_stamp = get_str_time()
    os.makedirs(output_dir, exist_ok=True)
    img_dir = os.path.join(output_dir, "images")
    os.makedirs(img_dir, exist_ok=True)

    try:
        # Store standalone image uploads under images/ so downstream packaging
        # can collect them with the same convention as document-extracted images.
        relative_source_path = f"images/{filename}"
        img_path = os.path.join(img_dir, filename)
        img_bytes = load_file_bytes(image_path, file_url=baseurl)
        img_obj = Image.open(io.BytesIO(img_bytes))
        img_obj.save(img_path)

        # Early exit: skip images smaller than 10KB
        from shared.core.constants import ProcessingConstants

        saved_size = os.path.getsize(img_path)
        if saved_size < ProcessingConstants.IMG_MIN_SIZE:
            logger.debug(
                f"Skipping image {filename} (too small: {saved_size / 1024:.1f} KB)"
            )
            os.remove(img_path)
            return pd.DataFrame(columns=list(PARSER_ROW_COLUMNS))

        # Extract image content
        client = _get_vision_client()
        abs_image_path = os.path.join(output_dir, relative_source_path)

        ## Classify the image so text-heavy scans go through OCR (§4.2) and
        ## everything else through the asset summary contract (§4.1).
        img_context = f"{filename}\n{base_llm_paras['frag_desc']}"
        type_resp = ask_image(
            client,
            output_dir,
            paths_=[relative_source_path],
            title_text=img_context,
            task="judge-image-type",
            size_cut=False,
        )
        is_text_image = bool(
            isinstance(type_resp, dict) and type_resp.get("answer") == "text"
        )

        if not base_llm_paras["summary_image"]:
            img_title = None
            image_summary = filename
            image_content = filename
        elif is_text_image:
            # Text scan: transcribe the body (→ content) and summarize for title.
            transcribed = transcribe(
                image_paths=[abs_image_path],
                max_tokens=ProcessingConstants.IMG_OCR_MAX_TOKENS,
                usage_task="parser.image.transcribe",
            )
            image_content = transcribed or filename
            asset = summarize(
                mode="asset",
                image_paths=[abs_image_path],
                text=filename,
                summary_len=ProcessingConstants.IMG_MAX_TOKENS,
                usage_task="parser.image.summary",
            )
            img_title = asset.title or None
            image_summary = asset.summary or image_content
        else:
            # Figure/chart/diagram: asset summary provides title + summary, and
            # the summary doubles as the chunk content.
            asset = summarize(
                mode="asset",
                image_paths=[abs_image_path],
                text=img_context,
                summary_len=ProcessingConstants.IMG_MAX_TOKENS,
                usage_task="parser.image.summary",
            )
            img_title = asset.title or None
            image_summary = asset.summary or filename
            image_content = asset.summary or filename

        # 2. Decide whether to rename based on image title and filename
        img_name = path_handle((img_title or image_summary)[:20], mode="clean_single")
        img_suffix = os.path.splitext(img_path)[-1]
        img_stem, inferred_suffix = os.path.splitext(img_name)
        if inferred_suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            img_name = img_stem or img_name
        if auto_rename:
            target_img_path = os.path.join(img_dir, f"{img_name}{img_suffix}")
            if os.path.exists(img_path):
                if img_path != target_img_path:
                    os.rename(img_path, target_img_path)
                # Store the relative filename for path construction
                final_img_name = f"{img_name}{img_suffix}"
            else:
                logger.warning(
                    f"Image file missing before rename, keeping original name: {filename}"
                )
                final_img_name = filename
        else:
            final_img_name = filename
    except KnowhereException:
        raise
    except Exception as e:
        logger.error(f"Failed to save image: {e}...")
        raise ImageParsingException(
            user_message="Failed to process the image file",
            reason="IMAGE_STORAGE_FAILED",
            internal_message=f"Storage error: {str(e)}",
            original_exception=e,
        )

    # Deterministic know_id: use image binary hash
    temp_uid = gen_str_codes(hashlib.sha256(img_bytes).hexdigest())
    # Use relative path with relative_root prefix
    relative_img_path = (
        f"{relative_root}{split_char}{final_img_name}"
        if relative_root
        else final_img_name
    )
    img_ref = build_chunk_ref(relative_img_path)
    img_bottom_content = f"{img_ref}\nImage Content:\n{image_content}"
    rows_builder = ParsedRowsBuilder()
    rows_builder.append(
        ParsedRow(
            content=img_bottom_content,
            path=relative_img_path,
            type="image",
            summary=image_summary,
            know_id=temp_uid,
            addtime=time_stamp,
        )
    )

    img_df = rows_builder.to_dataframe()
    img_df = process_dup_paths_df(img_df)

    return img_df
