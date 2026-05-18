"""
文本处理通用工具函数
这些函数被多个服务使用，保留在 shared-python 中
"""
import importlib
import re
import warnings
from collections.abc import Iterable
from typing import List, Optional, Protocol, cast

from syntok.tokenizer import Tokenizer as _SyntokTokenizer

from shared.utils.chunk_refs import CHUNK_REF_PATTERN

try:
    from blingfire import text_to_words as _blingfire_text_to_words
except (ImportError, OSError):  # blingfire ships a native lib; fall back to syntok when unusable.
    _blingfire_text_to_words = None


class _JiebaModule(Protocol):
    def lcut(self, sentence: str) -> list[str]: ...
    def cut(self, sentence: str) -> list[str]: ...


warnings.filterwarnings(
    "ignore",
    message=r"pkg_resources is deprecated as an API\..*",
    category=UserWarning,
    module=r"jieba\._compat",
)
_jieba = cast(_JiebaModule, importlib.import_module("jieba"))


def remove_duplicates_orderkept(input_list: List) -> List:
    """
    移除重复项但保持顺序
    
    Args:
        input_list: 输入列表
    
    Returns:
        去重后的列表
    """
    seen = set()
    output_list = []
    for item in input_list:
        if item not in seen:
            output_list.append(item)
            seen.add(item)
    return output_list


# Single regex for Chinese chars, English words, and number groups
_CN_EN_NUM_RE = re.compile(r'[\u4e00-\u9fff]|[A-Za-z]+|\d+(?:\.\d+)?')
_CJK_CHAR_RE = re.compile(r'[\u4e00-\u9fff]')

def count_cn_en(text: str) -> int:
    """统计中英文单词和数字的数量（单次正则扫描）"""
    if not text:
        return 0
    return len(_CN_EN_NUM_RE.findall(str(text)))


def truncate_content_preview(
    text: str,
    head: int = 200,
    tail: int = 50,
) -> str:
    """Token-aware content preview truncation.

    Uses the same token definition as ``count_cn_en``:
    - each Chinese character = 1 token
    - each run of English letters = 1 token  (never split mid-word)
    - each number group = 1 token

    Produces: ``<head tokens>...<tail tokens>`` when the token count
    exceeds ``head + tail``, otherwise returns text unchanged.

    Args:
        text: Source text (will be whitespace-normalized first).
        head: Max tokens to keep from the start (default 200).
        tail: Max tokens to keep from the end (default 20, 0 = no tail).

    Returns:
        Truncated preview string, or original text if within budget.
    """
    if not text:
        return ""
    # Normalize whitespace (collapse newlines / multiple spaces)
    normalized = " ".join(str(text).split())
    matches = list(_CN_EN_NUM_RE.finditer(normalized))
    total = len(matches)
    if total <= head + tail:
        return normalized
    # Cut after last head-th token (character boundary, never mid-word)
    cut_start = matches[head - 1].end() if head > 0 else 0
    # Tail starts at (total - tail)-th token
    cut_end = matches[total - tail].start() if tail > 0 else len(normalized)
    if cut_start >= cut_end:
        return normalized
    return normalized[:cut_start] + "..." + normalized[cut_end:]


def _is_meaningful_token(token: str) -> bool:
    """Check if a token is worth keeping: has useful characters and isn't pure noise."""
    if not _CN_EN_NUM_RE.search(token):
        return False
    # Filter single-character tokens: '共','年','月','1','9','m' etc.
    # Multi-char English words like 'PPO' or Chinese words like '施工' are kept.
    if len(token) == 1:
        return False
    return True


def _has_cjk_char(text: str) -> bool:
    return bool(_CJK_CHAR_RE.search(text))


def _is_cjk_char(char: str) -> bool:
    return '\u4e00' <= char <= '\u9fff'


# Lazy-loaded default stopwords (module-level cache)
_DEFAULT_STOPWORDS: Optional[frozenset] = None

def _get_default_stopwords() -> frozenset:
    """Load default stopwords on first call, then return cached frozenset."""
    global _DEFAULT_STOPWORDS
    if _DEFAULT_STOPWORDS is None:
        from shared.core.constants.stopwords import DEFAULT_STOPWORDS
        _DEFAULT_STOPWORDS = DEFAULT_STOPWORDS
    return _DEFAULT_STOPWORDS


_RETRIEVAL_ZH_STOPWORDS: Optional[frozenset[str]] = None
_RETRIEVAL_EN_STOPWORDS: Optional[frozenset[str]] = None


def _get_retrieval_stopwords() -> tuple[frozenset[str], frozenset[str]]:
    global _RETRIEVAL_ZH_STOPWORDS, _RETRIEVAL_EN_STOPWORDS
    if _RETRIEVAL_ZH_STOPWORDS is None or _RETRIEVAL_EN_STOPWORDS is None:
        zh_words: set[str] = set()
        en_words: set[str] = set()
        for word in _get_default_stopwords():
            token = str(word or "").strip()
            if not token:
                continue
            if _has_cjk_char(token):
                zh_words.add(token)
            elif re.search(r"[A-Za-z]", token):
                en_words.add(token.lower())
        _RETRIEVAL_ZH_STOPWORDS = frozenset(zh_words)
        _RETRIEVAL_EN_STOPWORDS = frozenset(en_words)
    return _RETRIEVAL_ZH_STOPWORDS, _RETRIEVAL_EN_STOPWORDS


# Pre-clean: strip chunk reference markers before tokenization
_CHUNK_MARKER_RE = re.compile(
    rf'{CHUNK_REF_PATTERN}|image-\d+|table-\d+',
    re.IGNORECASE,
)


def _normalize_retrieval_token(token: str) -> str:
    token = str(token or "").strip()
    if not token:
        return ""
    return token.lower() if re.search(r"[A-Za-z]", token) else token


def _split_mixed_language_segments(text: str) -> list[tuple[str, bool]]:
    segments: list[tuple[str, bool]] = []
    current: list[str] = []
    current_is_cjk: Optional[bool] = None

    for char in text:
        char_is_cjk = _is_cjk_char(char)
        if current_is_cjk is None or char_is_cjk == current_is_cjk:
            current.append(char)
            current_is_cjk = char_is_cjk
            continue
        segments.append(("".join(current), current_is_cjk))
        current = [char]
        current_is_cjk = char_is_cjk

    if current:
        segments.append(("".join(current), bool(current_is_cjk)))
    return segments


def _tokenize_english_segment(text: str) -> list[str]:
    if not text.strip():
        return []
    if _blingfire_text_to_words is not None:
        return [str(token) for token in str(_blingfire_text_to_words(text)).split()]
    return [str(token.value) for token in _SyntokTokenizer().tokenize(text)]


def _tokenize_cjk_segment(text: str) -> list[str]:
    if not text.strip():
        return []
    try:
        return list(_jieba.lcut(text))
    except AttributeError:
        return list(_jieba.cut(text))


def _resolve_retrieval_stopwords(
    stopwords: Optional[Iterable[str]],
) -> tuple[Optional[set[str]], Optional[set[str]]]:
    if stopwords is None:
        zh_stopwords, en_stopwords = _get_retrieval_stopwords()
        return set(zh_stopwords), set(en_stopwords)
    normalized = [str(word or "").strip() for word in stopwords if str(word or "").strip()]
    if not normalized:
        return None, None
    zh_words = {word for word in normalized if _has_cjk_char(word)}
    en_words = {word.lower() for word in normalized if re.search(r"[A-Za-z]", word)}
    return (zh_words or None), (en_words or None)


def tokenize_for_retrieval(
    text: str,
    *,
    stopwords: Optional[Iterable[str]] = None,
    dedupe: bool = False,
    min_token_length: int = 2,
) -> list[str]:
    """Tokenize mixed-language retrieval text with jieba + English tokenizer.

    Chinese spans are segmented with jieba. Non-Chinese spans are segmented with
    blingfire when available. Tokens are normalized to lowercase for English,
    filtered for minimum length / useful characters, and optionally deduplicated.
    """
    content = _CHUNK_MARKER_RE.sub('', str(text or ""))
    zh_stopwords, en_stopwords = _resolve_retrieval_stopwords(stopwords)
    tokens: list[str] = []

    for segment, is_cjk in _split_mixed_language_segments(content):
        raw_tokens = _tokenize_cjk_segment(segment) if is_cjk else _tokenize_english_segment(segment)
        for raw_token in raw_tokens:
            token = _normalize_retrieval_token(raw_token)
            if not token or not _is_meaningful_token(token):
                continue
            if len(token) < min_token_length:
                continue
            if _has_cjk_char(token):
                if zh_stopwords and token in zh_stopwords:
                    continue
            elif en_stopwords and token in en_stopwords:
                continue
            tokens.append(token)

    if dedupe:
        tokens = remove_duplicates_orderkept(tokens)
    return tokens


def tokenize_contents_for_retrieval(
    contents: List[str],
    *,
    stopwords: Optional[Iterable[str]] = None,
    link_char: str = ';',
    dedupe: bool = False,
    min_token_length: int = 2,
) -> List[str]:
    """Batch wrapper around `tokenize_for_retrieval()`."""
    return [
        link_char.join(
            tokenize_for_retrieval(
                content,
                stopwords=stopwords,
                dedupe=dedupe,
                min_token_length=min_token_length,
            )
        )
        for content in contents
    ]

def tokenize2stw_remove(contents: List[str], stopwords: Optional[List[str]] = None, link_char: str = ';') -> List[str]:
    """
    Uses jieba for tokenization which handles both Chinese and English text natively:
    - Chinese: jieba word segmentation
    - English: space-delimited word splitting (e.g. "deep learning" → ["deep", "learning"])
    - Mixed: both handled correctly in one pass

    Args:
        stopwords: None → use built-in baidu stopwords (default);
                   []   → no stopword filtering;
                   [custom list] → use provided stopwords.
    """
    # Resolve stopwords: None → default, [] → skip, list → convert to set
    if stopwords is None:
        sw_set = _get_default_stopwords()
    elif stopwords:
        sw_set = set(stopwords)
    else:
        sw_set = None

    res_contents = []
    for content in contents:
        # Pre-clean: remove IMAGE_/TABLE_ markers and reference labels
        content = _CHUNK_MARKER_RE.sub('', content)
        try:
            raw_tokens = _jieba.lcut(content)
        except AttributeError:
            raw_tokens = list(_jieba.cut(content))
        # Filter: keep only tokens with meaningful characters (Chinese/English/numbers)
        tokens = [t for t in raw_tokens if _is_meaningful_token(t)]
        # Remove stopwords
        if sw_set:
            tokens = [w for w in tokens if w not in sw_set]
        # Deduplicate while preserving order
        tokens = remove_duplicates_orderkept(tokens)
        res_contents.append(link_char.join(tokens))
    return res_contents
