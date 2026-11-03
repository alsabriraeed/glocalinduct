from .normalize import Block, NormalizedText, normalize_text, build_normalized_text, matching_text
from .extractors import extract_file, extract_text, ExtractionResult
from .pairing import discover_pairs, parse_pair_filename, load_metadata, load_xlsx_pairs, PairPaths
from .grouping import group_documents, jaccard_shingles
from .windows import build_windows, Window, count_tokens
from .splits import split_groups, SplitManifest

__all__ = [
    'Block', 'NormalizedText', 'normalize_text', 'build_normalized_text', 'matching_text',
    'extract_file', 'extract_text', 'ExtractionResult', 'discover_pairs', 'parse_pair_filename',
    'load_metadata', 'load_xlsx_pairs', 'PairPaths', 'group_documents', 'jaccard_shingles', 'build_windows', 'Window',
    'count_tokens', 'split_groups', 'SplitManifest',
]
