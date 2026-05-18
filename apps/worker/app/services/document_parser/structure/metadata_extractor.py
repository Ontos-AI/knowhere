# pyright: reportMissingImports=false
"""
Metadata Extractor for Layout Parser (MD-First + layout.json span height)
---
Starts from MD headings (lines with #), finds their span heights in layout.json,
then computes relative size rankings.

Key insight: layout.json span height is the SINGLE-LINE height, more accurate than
content_list.json block height which may span multiple lines.

Flow:
1. Scan MD for all # headings
2. Build occurrence map from layout.json (including table html content)
3. Match each heading by occurrence order to get span height
4. Rank headings by height (largest = rank 1)
"""

import json
import logging
import os
import re
from collections import defaultdict
from typing import Dict, List, Tuple

from bs4 import BeautifulSoup

try:
    from app.utils.logger import logger
except ImportError:
    logger = logging.getLogger(__name__)


def normalize_content(text: str) -> str:
    """Normalize text content for matching"""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    text = text.lower()
    return text


_FULL_LINE_MD_BOLD_RE = re.compile(r"^(\*{2,3}|_{2,3})(.+)\1\s*$")


def strip_md_heading_markers(line: str) -> str:
    """Strip leading and trailing markdown heading markers from a line."""
    if not line:
        return line

    line = line.strip()
    line = re.sub(r"^#+\s*", "", line)
    # Require whitespace before trailing # so we do not mangle content like "C#".
    line = re.sub(r"\s+#+\s*$", "", line)
    return line.strip()


def detect_and_strip_md_bold(line_clean: str) -> tuple:
    """Detect and strip markdown full-line bold markers.

    Criteria for "full-line bold":
    - line_clean (with '#' prefix stripped) is entirely wrapped by **...**, __...__, or ***...***
    - Partial bolding (e.g. "- **Object Detection:** Anchor-free") is not counted.

    Args:
        line_clean: Line text with '#' prefix stripped

    Returns:
        (stripped_text, is_full_bold)
        - stripped_text: If full-line bold, returns plain text without markers; otherwise returns as-is
        - is_full_bold: Whether it is a full-line bold
    """
    if not line_clean:
        return line_clean, False

    # Match bold/bold-italic markers wrapping the entire line
    # Supports **text**, __text__, ***text***, ___text___
    m = _FULL_LINE_MD_BOLD_RE.match(line_clean)
    if m and m.group(1) in ("**", "***", "__", "___"):
        return m.group(2).strip(), True

    return line_clean, False


def clean_md_text_for_llm(line: str) -> str:
    """Strip markdown formatting markers so LLM sees semantic text only."""
    if not line:
        return line

    line = strip_md_heading_markers(line)
    line, _ = detect_and_strip_md_bold(line)
    return line.strip()


def extract_md_headings(md_lines: List[str]) -> List[dict]:
    """Extract all headings (lines starting with #) from MD

    Tracks ALL text occurrences (including non-# lines) to correctly
    match the order in layout.json.

    Returns:
        List of {line_no, text, text_key, hash_level, layout_occurrence}
    """
    occurrence_counter = defaultdict(int)
    headings = []

    for i, line in enumerate(md_lines):
        line_stripped = line.strip()

        # Skip empty lines
        if not line_stripped:
            continue

        # Check if this is a # heading
        is_heading = line_stripped.startswith("#") and not line_stripped.startswith(
            "#x"
        )

        # For table lines, extract all text content and increment occurrences
        if "<table>" in line_stripped or "<td>" in line_stripped:
            try:
                soup = BeautifulSoup(line_stripped, "html.parser")
                for td in soup.find_all("td"):
                    text = td.get_text().strip()
                    if text:
                        text_key = normalize_content(text)
                        occurrence_counter[text_key] += 1
            except Exception:
                pass
            continue

        if is_heading:
            hash_match = re.match(r"^(#+)\s*(.+)$", line_stripped)
            if hash_match:
                hash_level = len(hash_match.group(1))
                text = hash_match.group(2).strip()
                text_key = normalize_content(text)

                # Increment occurrence counter
                occurrence_counter[text_key] += 1

                headings.append(
                    {
                        "line_no": i,
                        "text": text,
                        "text_key": text_key,
                        "hash_level": hash_level,
                        "layout_occurrence": occurrence_counter[text_key],
                    }
                )
        else:
            # Not a # heading, but still track occurrence for layout matching
            if line_stripped:
                text_key = normalize_content(line_stripped)
                occurrence_counter[text_key] += 1

    return headings


def build_layout_height_map(layout_json_path: str) -> Dict[str, List[dict]]:
    """Build mapping from normalized text to all occurrences with SPAN heights

    Extracts from:
    - para_blocks/discarded_blocks: spans with bbox
    - table blocks: text from html content

    Returns:
        Dict mapping text_key -> [
            {height: 67, page_idx: 9, type: 'title'},
            {height: 34, page_idx: 3, type: 'text'},
            ...
        ]
    """
    try:
        with open(layout_json_path, "r", encoding="utf-8") as f:
            layout_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to load layout.json: {e}")
        return {}

    if not layout_data or "pdf_info" not in layout_data:
        return {}

    height_map = defaultdict(list)

    def process_block(block, page_idx):
        """Recursively process a block and its nested blocks"""
        block_type = block.get("type", "unknown")

        # First, process any nested blocks
        for nested_block in block.get("blocks", []):
            process_block(nested_block, page_idx)

        # Process lines > spans
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                span_type = span.get("type", "")

                # Check if span contains table html
                if span_type == "table" or span.get("html"):
                    html_content = span.get("html", "")
                    if html_content:
                        try:
                            soup = BeautifulSoup(html_content, "html.parser")
                            for td in soup.find_all("td"):
                                text = td.get_text().strip()
                                if text:
                                    text_key = normalize_content(text)
                                    height_map[text_key].append(
                                        {
                                            "height": 0,
                                            "page_idx": page_idx,
                                            "type": "table",
                                        }
                                    )
                        except Exception:
                            pass
                    continue

                # Regular text span
                content = span.get("content", "")
                if not content:
                    continue

                text_key = normalize_content(content)
                span_bbox = span.get("bbox", [0, 0, 0, 0])
                span_height = span_bbox[3] - span_bbox[1] if len(span_bbox) >= 4 else 0

                height_map[text_key].append(
                    {
                        "height": span_height,
                        "page_idx": page_idx,
                        "type": block_type,
                    }
                )

    for page in layout_data["pdf_info"]:
        page_idx = page.get("page_idx", 0)

        for block_list_key in ["para_blocks", "discarded_blocks"]:
            for block in page.get(block_list_key, []):
                process_block(block, page_idx)

    return dict(height_map)


class MetadataContext:
    """Context for computing heading ranks based on MD headings and layout.json span heights"""

    def __init__(self, md_lines: List[str], layout_json_path: str):
        """Initialize with MD content and layout.json path"""
        # 1. Extract all # headings from MD (with occurrence tracking)
        self.md_headings = extract_md_headings(md_lines)

        # 2. Build height map from layout.json (using span height)
        if os.path.exists(layout_json_path):
            self.height_map = build_layout_height_map(layout_json_path)
        else:
            logger.warning(f"layout.json not found: {layout_json_path}")
            self.height_map = {}

        # 3. Match each MD heading to layout.json to get span height
        self._assign_heights_to_headings()

        # 4. Compute size levels using K-means clustering
        self._compute_levels()

        # 5. Build lookup map for get_meta_for_line
        self._build_lookup_map()

        # 6. Track occurrences during processing
        self.occurrence_tracker = defaultdict(int)

    def _assign_heights_to_headings(self):
        """Match each MD heading to layout.json and get span heights"""
        for heading in self.md_headings:
            text_key = heading["text_key"]
            occ = heading.get("layout_occurrence", 1)
            heading["occurrence"] = occ

            # Find height from layout.json
            if text_key in self.height_map:
                occurrences = self.height_map[text_key]
                # Match by occurrence order (1-based)
                occ_idx = occ - 1
                if occ_idx < len(occurrences):
                    heading["height"] = occurrences[occ_idx]["height"]
                else:
                    # Use last known if MD has more occurrences
                    heading["height"] = occurrences[-1]["height"]
            else:
                # Try fuzzy match
                heading["height"] = self._fuzzy_find_height(text_key)

    def _fuzzy_find_height(self, text_key: str) -> int:
        """Try to find height using fuzzy matching"""
        if not text_key or len(text_key) < 10:
            return 0

        for key, occurrences in self.height_map.items():
            if len(key) > 10 and (text_key in key or key in text_key):
                # Return first non-zero height
                for occ in occurrences:
                    if occ["height"] > 0:
                        return occ["height"]

        return 0

    def _compute_levels(self):
        """Compute size levels for all MD headings using fixed percentile thresholds

        Uses percentile-based classification:
        - Level 1: top 20% (largest headings)
        - Level 2: 20%-40%
        - Level 3: 40%-60%
        - Level 4: 60%-80%
        - Level 5: bottom 20% (smallest headings)
        """
        # Filter headings with valid heights (> 0)
        valid_headings = [h for h in self.md_headings if h.get("height", 0) > 0]

        if not valid_headings:
            # No valid headings, set all to level 0
            for heading in self.md_headings:
                heading["size_level"] = 0
            return

        # Get all heights and compute percentile thresholds
        heights = sorted([h["height"] for h in valid_headings], reverse=True)
        n = len(heights)

        if n == 1:
            # Only one heading, assign level 1
            valid_headings[0]["size_level"] = 1
            for heading in self.md_headings:
                if "size_level" not in heading:
                    heading["size_level"] = 0
            return

        # Compute percentile thresholds (20%, 40%, 60%, 80%)
        # Higher height = lower level number (Level 1 is largest)
        def get_percentile(pct):
            idx = int(n * pct / 100)
            return heights[min(idx, n - 1)]

        p20 = get_percentile(20)  # Top 20% threshold
        p40 = get_percentile(40)
        p60 = get_percentile(60)
        p80 = get_percentile(80)

        # Assign levels based on height thresholds
        for heading in valid_headings:
            h = heading["height"]
            if h >= p20:
                heading["size_level"] = 1  # Top 20%
            elif h >= p40:
                heading["size_level"] = 2  # 20%-40%
            elif h >= p60:
                heading["size_level"] = 3  # 40%-60%
            elif h >= p80:
                heading["size_level"] = 4  # 60%-80%
            else:
                heading["size_level"] = 5  # Bottom 20%

        # Headings without valid height get level 0
        for heading in self.md_headings:
            if "size_level" not in heading:
                heading["size_level"] = 0

        # Store threshold info for debugging
        self.percentile_thresholds = {
            "p20": p20,
            "p40": p40,
            "p60": p60,
            "p80": p80,
            "total_valid": n,
        }

    def _build_lookup_map(self):
        """Build lookup map: text_key -> list of (occurrence, level, height)"""
        self.lookup_map = defaultdict(list)
        for heading in self.md_headings:
            self.lookup_map[heading["text_key"]].append(
                {
                    "occurrence": heading["occurrence"],
                    "size_level": heading.get("size_level", 0),
                    "height": heading.get("height", 0),
                    "line_no": heading["line_no"],
                }
            )

    def get_meta_for_line(self, line: str) -> Tuple[int, int]:
        """Get META features for a single MD line

        Returns:
            Tuple of (size_level, total_occurrences)
            - size_level: 1-5 (1=largest heading), 0 if not a # heading
            - total_occurrences: total times this text appears in the document, 0 if not tracked
        """
        if not line:
            return (0, 0)

        # Clean the line
        line_clean = line.strip()
        line_clean = re.sub(r"^#+\s*", "", line_clean)
        line_key = normalize_content(line_clean)

        # Check if this is a tracked heading
        if line_key in self.lookup_map:
            total_occ = len(self.lookup_map[line_key])
            self.occurrence_tracker[line_key] += 1
            current_occ = self.occurrence_tracker[line_key]

            # Find matching occurrence to get correct size_level for this position
            for item in self.lookup_map[line_key]:
                if item["occurrence"] == current_occ:
                    return (item["size_level"], total_occ)

            # If more occurrences in processing than in MD headings
            if self.lookup_map[line_key]:
                last_item = self.lookup_map[line_key][-1]
                return (last_item["size_level"], total_occ)

        return (0, 0)

    def format_meta_suffix(
        self, size_level: int, occurrence: int, is_bold: int = 0
    ) -> str:
        """Format META suffix for reason code"""
        return f" META [{size_level}, {occurrence}, {is_bold}]"

    def get_stats(self) -> dict:
        """Get statistics about the headings"""
        return {
            "total_md_headings": len(self.md_headings),
            "headings_with_height": len(
                [h for h in self.md_headings if h.get("height", 0) > 0]
            ),
            "unique_heading_texts": len(self.lookup_map),
        }


# Convenience function
def format_meta_suffix(size_rank: int, occurrence: int = 0, is_bold: int = 0) -> str:
    """Format META suffix for reason code"""
    return f" META [{size_rank}, {occurrence}, {is_bold}]"
