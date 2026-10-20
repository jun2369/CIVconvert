"""Named value transforms that a customer profile can apply to a template column."""

from __future__ import annotations

import re
from typing import Any, Callable

# Chinese/CJK characters, CJK punctuation, kana and half/full-width forms.
_CJK_CLASS = (
    "⺀-⿟"  # CJK radicals, Kangxi radicals
    "　-〿"  # CJK symbols and punctuation  、。「」【】《》
    "぀-ヿ"  # hiragana, katakana
    "㄀-ㄯㆠ-ㆿ"  # bopomofo
    "㈀-㏿"  # enclosed CJK letters, CJK compatibility
    "㐀-䶿"  # CJK extension A
    "一-鿿"  # CJK unified ideographs
    "豈-﫿"  # CJK compatibility ideographs
    "︰-﹏"  # CJK compatibility forms
    "＀-￯"  # half/full-width forms (full-width ASCII is converted to ASCII first)
    "\U00020000-\U0003134f"  # CJK extensions B-G
)
_CJK = re.compile(f"[{_CJK_CLASS}]")
_CJK_RUN = re.compile(f"[{_CJK_CLASS}]+")
# Full-width ASCII （ＡＢＣ１２３） -> ASCII, ideographic space -> space
_FULL_WIDTH = {cp: cp - 0xFEE0 for cp in range(0xFF01, 0xFF5F)} | {0x3000: 0x20}
_HAS_ALNUM = re.compile(r"[A-Za-z0-9]")
_EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]|\{\s*\}")
_SPACES = re.compile(r"\s+")
_TRAILING_JUNK = " \t-–—_/\\|,;:+&([{<"
_LEADING_JUNK = " \t-–—_/\\|,;:+&)]}>"


def has_cjk(text: str | None) -> bool:
    return bool(text) and _CJK.search(text) is not None


def strip_chinese(text: str, mode: str = "keep_leading_english") -> str:
    """Remove the Chinese part of a product description.

    keep_leading_english (default): everything from the first Chinese character on is
        dropped – "Pivot Stand 转向支架" -> "Pivot Stand". If the text *starts* with
        Chinese, the Chinese runs are removed instead – "快拍脚架 Selfie Stick" -> "Selfie Stick".
    remove_all: every Chinese run is removed and English after it is kept –
        "Pivot Stand 转向支架 Black" -> "Pivot Stand Black".
    """
    if mode not in ("keep_leading_english", "remove_all"):
        raise ValueError(f"strip_chinese: unknown mode '{mode}'")
    if not text:
        return text
    s = text.translate(_FULL_WIDTH)
    first = _CJK.search(s)
    if first is None:
        return s
    if mode == "keep_leading_english":
        head = s[: first.start()]
        if _HAS_ALNUM.search(head):
            return _tidy(head)
    return _tidy(_CJK_RUN.sub(" ", s))


def _tidy(s: str) -> str:
    s = _SPACES.sub(" ", _EMPTY_BRACKETS.sub(" ", s))
    s = s.rstrip(_TRAILING_JUNK).lstrip(_LEADING_JUNK)
    for opening, closing in ("()", "[]"):
        missing = s.count(opening) - s.count(closing)
        if missing > 0:  # "Memory Card (128G 内存卡)" -> "Memory Card (128G)"
            s += closing * missing
    return s


def _strip(value: str) -> str:
    return value.strip()


def _upper(value: str) -> str:
    return value.upper()


def _remove_spaces(value: str) -> str:
    return _SPACES.sub("", value)


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", value)


def _regex_replace(value: str, pattern: str, replace: str = "") -> str:
    return re.sub(pattern, replace, value)


TRANSFORMS: dict[str, Callable[..., str]] = {
    "strip_chinese": strip_chinese,
    "strip": _strip,
    "upper": _upper,
    "remove_spaces": _remove_spaces,
    "digits_only": _digits_only,
    "regex_replace": _regex_replace,
}


def parse_spec(spec: Any) -> tuple[str, dict[str, Any]]:
    """'strip_chinese' or {name: strip_chinese, mode: remove_all} -> (name, options)."""
    if isinstance(spec, str):
        return spec, {}
    if isinstance(spec, dict) and isinstance(spec.get("name"), str):
        return spec["name"], {k: v for k, v in spec.items() if k != "name"}
    raise ValueError(f"invalid transform {spec!r} – use a name or {{name: ..., option: ...}}")


def apply_transforms(value: str, specs: list[Any]) -> str:
    for spec in specs:
        name, options = parse_spec(spec)
        value = TRANSFORMS[name](value, **options)
    return value
