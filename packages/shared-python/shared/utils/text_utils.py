"""
文本处理通用工具函数
这些函数被多个服务使用，保留在 shared-python 中
"""
import importlib
import re
import warnings
from collections.abc import Callable, Generator, Iterable, Iterator
from typing import List, Optional, Protocol, cast

from syntok.tokenizer import Tokenizer as _SyntokTokenizer

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


# Fixed-width parts of CHUNK_REF_PATTERN and the inline labels. Searching for
# each opener's suffix with the full lazy regex can rescan the same line once
# per malformed opener. Each resource kind instead shares its next boundary.
_SCAN_CHARACTERS: int = 4096
_MARKER_LOOKAHEAD: int = 8
_MARKER_START_RE: re.Pattern[str] = re.compile(
    r"(?P<image>\[images/)|(?P<table>\[tables/)|(?:image|table)-\d", re.IGNORECASE
)
_MARKER_BOUNDARIES: dict[str, re.Pattern[str]] = {
    "image": re.compile(r"\.(?:png|jpe?g|gif|webp)\]|\n", re.IGNORECASE),
    "table": re.compile(r"\.(?:html|csv)\]|\n", re.IGNORECASE),
}
_LABEL_DIGITS_RE: re.Pattern[str] = re.compile(r"\d+")
_LANGUAGE_RUN_RE: re.Pattern[str] = re.compile(r"[\u4e00-\u9fff]+|[^\u4e00-\u9fff]+")


def _normalize_retrieval_token(token: str) -> str:
    token = str(token or "").strip()
    if not token:
        return ""
    return token.lower() if re.search(r"[A-Za-z]", token) else token


def _find_marker_boundary(text: str, start: int, pattern: re.Pattern[str]) -> Generator[None, None, re.Match[str] | None]:
    position: int = start
    while position < len(text):
        limit: int = min(position + _SCAN_CHARACTERS, len(text))
        match: re.Match[str] | None = pattern.search(text, position, limit + _MARKER_LOOKAHEAD)
        if match is not None and match.start() < limit:
            return match
        position = limit
        yield None
    return None


def _skip_label_digits(text: str, start: int) -> Generator[None, None, int]:
    position: int = start
    while position < len(text):
        limit: int = min(position + _SCAN_CHARACTERS, len(text))
        match: re.Match[str] | None = _LABEL_DIGITS_RE.match(text, position, limit)
        if match is None:
            break
        position = match.end()
        if position < limit:
            break
        yield None
    return position


def _iterate_clean_text_steps(text: str) -> Iterator[str | None]:
    position: int = 0
    checkpoint: int = _SCAN_CHARACTERS
    boundaries: dict[str, re.Match[str] | None] = {}
    while position < len(text):
        if position >= checkpoint:
            yield None
            checkpoint = position + _SCAN_CHARACTERS
        limit: int = min(checkpoint, len(text))
        marker: re.Match[str] | None = _MARKER_START_RE.search(text, position, limit + _MARKER_LOOKAHEAD)
        if marker is None or marker.start() >= limit:
            yield text[position:limit]
            position = limit
            continue
        if marker.start() > position:
            yield text[position:marker.start()]
        position = marker.end()
        kind: str | None = marker.lastgroup
        if kind is None:
            position = yield from _skip_label_digits(text, position)
            continue
        boundary: re.Match[str] | None = boundaries.get(kind)
        if kind not in boundaries or (boundary is not None and boundary.start() < position):
            boundary = yield from _find_marker_boundary(text, position, _MARKER_BOUNDARIES[kind])
        if boundary is not None and boundary.start() == position and text[position] == ".":
            # The lazy path expression requires at least one character before
            # the extension. An immediate suffix may still be part of a path
            # that ends at the next matching suffix on this line.
            boundary = yield from _find_marker_boundary(text, boundary.end(), _MARKER_BOUNDARIES[kind])
        boundaries[kind] = boundary
        if boundary is None or text[boundary.start()] == "\n":
            yield marker.group()
        else:
            position = boundary.end()


def _iterate_retrieval_segment_steps(text: str) -> Iterator[tuple[str, bool] | None]:
    """Stream the legacy language runs and checkpoints between scan windows."""
    parts: list[str] = []
    isCjk: bool | None = None
    for fragment in _iterate_clean_text_steps(str(text or "")):
        if fragment is None:
            yield None
            continue
        for run in _LANGUAGE_RUN_RE.finditer(fragment):
            runIsCjk: bool = _is_cjk_char(fragment[run.start()])
            if isCjk is not None and runIsCjk != isCjk:
                yield "".join(parts), isCjk
                parts.clear()
            parts.append(run.group())
            isCjk = runIsCjk
    if isCjk is not None:
        yield "".join(parts), isCjk


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


def _iterate_retrieval_token_steps(
    text: str,
    *,
    stopwords: Optional[Iterable[str]] = None,
    dedupe: bool = False,
    min_token_length: int = 2,
    validateCjkSpan: Callable[[str], None] | None = None,
) -> Iterator[str | None]:
    """Yield checkpoints around the unchanged authoritative language spans."""
    zh_stopwords, en_stopwords = _resolve_retrieval_stopwords(stopwords)
    seen: set[str] = set()
    for step in _iterate_retrieval_segment_steps(text):
        if step is None:
            yield None
            continue
        segment, is_cjk = step
        if is_cjk and validateCjkSpan is not None:
            validateCjkSpan(segment)
        yield None
        raw_tokens = _tokenize_cjk_segment(segment) if is_cjk else _tokenize_english_segment(segment)
        yield None
        for raw_token in raw_tokens:
            yield None
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
            if dedupe:
                if token in seen:
                    continue
                seen.add(token)
            yield token


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
    return [token for token in _iterate_retrieval_token_steps(text, stopwords=stopwords,
        dedupe=dedupe, min_token_length=min_token_length) if token is not None]


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
        content = "".join(part for part in _iterate_clean_text_steps(content) if part is not None)
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
