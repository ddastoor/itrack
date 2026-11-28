"""Rich-text Markdown/HTML normalization, strict XSS sanitization, validation, and CLI rendering."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Any

from markdown_it import MarkdownIt

from itrack_core.models import ValidationError

ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "p",
        "br",
        "hr",
        "div",
        "span",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "s",
        "del",
        "strike",
        "code",
        "pre",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "ul",
        "ol",
        "li",
        "a",
        "img",
    }
)

_VOID_TAGS: frozenset[str] = frozenset({"br", "hr", "img"})
_DROP_CONTENT_TAGS: frozenset[str] = frozenset(
    {"script", "style", "iframe", "object", "embed", "svg", "math", "noscript", "template"}
)
_BLOCK_TAGS: frozenset[str] = frozenset(
    {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "li", "pre", "hr"}
)
_CTRL_WS_RE = re.compile(r"[\x00-\x20]+")
_ASSET_PATH_RE = re.compile(r"^/api/assets/[A-Za-z0-9._-]+$")
_DATA_IMG_RE = re.compile(
    r"^data:image/(?:png|jpeg|jpg|gif|webp|bmp|svg\+xml);base64,[A-Za-z0-9+/=\s]+$",
    re.IGNORECASE,
)
_SAFE_CLASS_RE = re.compile(r"^[A-Za-z0-9 _-]+$")
_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_HTML_HINT_RE = re.compile(r"<\s*/?\s*[a-zA-Z][a-zA-Z0-9-]*\b[^>]*>")

_MD_ENGINE = MarkdownIt("commonmark", {"html": True, "breaks": True}).enable("strikethrough").disable("code")


def _is_safe_link_href(raw_href: str) -> str | None:
    cleaned = raw_href.strip()
    if not cleaned:
        return None
    normalized = _CTRL_WS_RE.sub("", cleaned).lower()
    if normalized.startswith(("http://", "https://", "mailto:")):
        return cleaned
    return None


def _is_safe_img_src(raw_src: str) -> str | None:
    cleaned = raw_src.strip()
    if not cleaned:
        return None
    normalized = _CTRL_WS_RE.sub("", cleaned)
    low = normalized.lower()
    if low.startswith(("http://", "https://")):
        return cleaned
    if _ASSET_PATH_RE.match(normalized):
        return normalized
    if _DATA_IMG_RE.match(normalized):
        return normalized
    return None


class _SanitizingHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._drop_stack: list[str] = []
        self._open_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        if self._drop_stack:
            if tag_l not in _VOID_TAGS:
                self._drop_stack.append(tag_l)
            return
        if tag_l in _DROP_CONTENT_TAGS:
            self._drop_stack.append(tag_l)
            return
        if tag_l not in ALLOWED_TAGS:
            return

        attr_map: dict[str, str] = {}
        for k, v in attrs:
            if k is None:
                continue
            kl = k.lower().strip()
            if kl.startswith("on") or kl in ("style", "srcdoc", "formaction"):
                continue
            attr_map[kl] = v if v is not None else ""

        if tag_l == "img":
            safe_src = _is_safe_img_src(attr_map.get("src", ""))
            if not safe_src:
                return
            alt_val = attr_map.get("alt", "")
            attrs_out = [
                f'src="{html.escape(safe_src, quote=True)}"',
                f'alt="{html.escape(alt_val, quote=True)}"',
            ]
            asset_id = attr_map.get("data-asset-id", "").strip()
            if asset_id and _ASSET_ID_RE.match(asset_id):
                attrs_out.append(f'data-asset-id="{html.escape(asset_id, quote=True)}"')
            self.parts.append(f"<img {' '.join(attrs_out)} />")
            return

        if tag_l in _VOID_TAGS:
            self.parts.append(f"<{tag_l} />")
            return

        rendered_attrs: list[str] = []
        if tag_l == "a":
            safe_href = _is_safe_link_href(attr_map.get("href", ""))
            if safe_href:
                rendered_attrs.append(f'href="{html.escape(safe_href, quote=True)}"')
            title = attr_map.get("title", "").strip()
            if title:
                rendered_attrs.append(f'title="{html.escape(title, quote=True)}"')
            if attr_map.get("target", "").strip() == "_blank" and safe_href:
                rendered_attrs.append('target="_blank"')
                rendered_attrs.append('rel="noopener noreferrer"')
        elif tag_l in ("code", "pre"):
            cls = attr_map.get("class", "").strip()
            if cls and _SAFE_CLASS_RE.match(cls):
                rendered_attrs.append(f'class="{html.escape(cls, quote=True)}"')

        attr_str = (" " + " ".join(rendered_attrs)) if rendered_attrs else ""
        self.parts.append(f"<{tag_l}{attr_str}>")
        self._open_tags.append(tag_l)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        tag_l = tag.lower()
        if not self._drop_stack and tag_l in ALLOWED_TAGS and tag_l not in _VOID_TAGS:
            if self._open_tags and self._open_tags[-1] == tag_l:
                self._open_tags.pop()
                self.parts.append(f"</{tag_l}>")

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if self._drop_stack:
            for idx in range(len(self._drop_stack) - 1, -1, -1):
                if self._drop_stack[idx] == tag_l:
                    del self._drop_stack[idx:]
                    break
            return
        if tag_l not in ALLOWED_TAGS or tag_l in _VOID_TAGS:
            return
        for idx in range(len(self._open_tags) - 1, -1, -1):
            if self._open_tags[idx] == tag_l:
                while len(self._open_tags) > idx:
                    closing = self._open_tags.pop()
                    self.parts.append(f"</{closing}>")
                break

    def handle_data(self, data: str) -> None:
        if self._drop_stack:
            return
        self.parts.append(html.escape(data, quote=False))

    def handle_comment(self, data: str) -> None:
        return

    def finalize(self) -> str:
        while self._open_tags:
            closing = self._open_tags.pop()
            self.parts.append(f"</{closing}>")
        return "".join(self.parts).strip()


def sanitize_rich_html(raw_html: str) -> str:
    """Strictly sanitize HTML against XSS while preserving rich-text formatting, links, and safe images."""
    if not raw_html:
        return ""
    parser = _SanitizingHTMLParser()
    parser.feed(raw_html)
    parser.close()
    return parser.finalize()


def _looks_like_full_html(raw: str) -> bool:
    stripped = raw.lstrip()
    if stripped.startswith(("<p>", "<p ", "<div>", "<div ", "<h1", "<h2", "<h3", "<h4", "<h5", "<h6", "<ul>", "<ol>", "<blockquote>", "<pre>")):
        return True
    return False


def markdown_or_html_to_safe_html(raw: str) -> str:
    """Convert Markdown (with inline HTML support) or HTML to canonical sanitized HTML."""
    if not raw or not isinstance(raw, str):
        return ""
    if _looks_like_full_html(raw):
        return sanitize_rich_html(raw)
    rendered = _MD_ENGINE.render(raw)
    return sanitize_rich_html(rendered)


class _TextAndImageExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.images: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs if k}
        if tag_l == "br":
            self.chunks.append("\n")
        elif tag_l in _BLOCK_TAGS:
            if self.chunks and not self.chunks[-1].endswith((" ", "\n")):
                self.chunks.append("\n")
        elif tag_l == "img":
            src = attr_map.get("src", "").strip()
            alt = attr_map.get("alt", "")
            asset_id = attr_map.get("data-asset-id", "").strip() or None
            if src.startswith("/api/assets/"):
                kind = "asset"
                asset_id = asset_id or src[len("/api/assets/") :]
            elif src.lower().startswith("data:image/"):
                kind = "data_uri"
            else:
                kind = "url"
            self.images.append(
                {
                    "src": src,
                    "alt": alt,
                    "kind": kind,
                    "asset_id": asset_id,
                }
            )

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _BLOCK_TAGS and self.chunks and not self.chunks[-1].endswith((" ", "\n")):
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        self.chunks.append(data)


def extract_visible_text_and_images(sanitized_html: str) -> tuple[str, list[dict[str, Any]]]:
    """Extract normalized visible plain text and image metadata list from sanitized HTML."""
    parser = _TextAndImageExtractor()
    parser.feed(sanitized_html or "")
    parser.close()
    raw_text = "".join(parser.chunks).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in raw_text.splitlines()]
    visible_text = "\n".join(line for line in lines if line).strip()
    return visible_text, parser.images


def has_meaningful_rich_content(sanitized_html: str) -> bool:
    """Return True iff visible plain text is non-empty or at least one valid image is present."""
    visible_text, images = extract_visible_text_and_images(sanitized_html)
    return bool(visible_text.replace("\xa0", " ").strip() or len(images) > 0)


def validate_mandatory_rich_text(
    raw: str | None,
    field_label: str = "Description",
) -> tuple[str, str, list[dict[str, Any]]]:
    """Normalize, sanitize, and validate non-empty rich text; return (html, search_text, images)."""
    if raw is None or not isinstance(raw, str):
        raise ValidationError(f"{field_label} is required and cannot be empty.")
    sanitized = markdown_or_html_to_safe_html(raw)
    if not has_meaningful_rich_content(sanitized):
        raise ValidationError(
            f"{field_label} cannot be empty, whitespace-only, or empty markup without text or images."
        )
    visible_text, images = extract_visible_text_and_images(sanitized)
    alt_parts = [img["alt"].strip() for img in images if img.get("alt", "").strip()]
    search_text = " ".join(part for part in [visible_text, *alt_parts] if part).strip()
    return sanitized, search_text, images


class _CliRichRenderer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._list_stack: list[tuple[str, int]] = []
        self._in_pre = False
        self._link_href: str | None = None
        self._in_blockquote = False

    def _ensure_blank_line(self) -> None:
        joined = "".join(self.out)
        if not joined:
            return
        if not joined.endswith("\n\n"):
            if joined.endswith("\n"):
                self.out.append("\n")
            else:
                self.out.append("\n\n")

    def _ensure_newline(self) -> None:
        joined = "".join(self.out)
        if joined and not joined.endswith("\n"):
            self.out.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs if k}
        if tag_l in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._ensure_blank_line()
            level = int(tag_l[1])
            self.out.append("#" * level + " ")
        elif tag_l in ("p", "div"):
            if self._list_stack or self._in_blockquote:
                return
            self._ensure_blank_line()
        elif tag_l == "br":
            self.out.append("\n")
        elif tag_l == "hr":
            self._ensure_blank_line()
            self.out.append("---\n\n")
        elif tag_l in ("strong", "b"):
            self.out.append("**")
        elif tag_l in ("em", "i"):
            self.out.append("*")
        elif tag_l == "u":
            self.out.append("__")
        elif tag_l in ("s", "del", "strike"):
            self.out.append("~~")
        elif tag_l == "pre":
            self._ensure_blank_line()
            self._in_pre = True
            self.out.append("```\n")
        elif tag_l == "code":
            if not self._in_pre:
                self.out.append("`")
        elif tag_l == "blockquote":
            self._ensure_blank_line()
            self._in_blockquote = True
            self.out.append("> ")
        elif tag_l == "ul":
            self._ensure_newline()
            self._list_stack.append(("ul", 0))
        elif tag_l == "ol":
            self._ensure_newline()
            self._list_stack.append(("ol", 0))
        elif tag_l == "li":
            self._ensure_newline()
            indent = "  " * max(0, len(self._list_stack) - 1)
            if self._list_stack:
                kind, count = self._list_stack[-1]
                count += 1
                self._list_stack[-1] = (kind, count)
                prefix = f"{count}. " if kind == "ol" else "- "
            else:
                prefix = "- "
            self.out.append(f"{indent}{prefix}")
        elif tag_l == "a":
            self._link_href = attr_map.get("href", "").strip() or None
        elif tag_l == "img":
            src = attr_map.get("src", "").strip()
            alt = attr_map.get("alt", "").strip()
            if alt:
                self.out.append(f"[Image: {alt} ({src})]")
            else:
                self.out.append(f"[Image: ({src})]")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if tag_l in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "div"):
            if not self._list_stack and not self._in_blockquote:
                self._ensure_blank_line()
        elif tag_l in ("strong", "b"):
            self.out.append("**")
        elif tag_l in ("em", "i"):
            self.out.append("*")
        elif tag_l == "u":
            self.out.append("__")
        elif tag_l in ("s", "del", "strike"):
            self.out.append("~~")
        elif tag_l == "pre":
            if self._in_pre:
                self._ensure_newline()
                self.out.append("```\n\n")
                self._in_pre = False
        elif tag_l == "code":
            if not self._in_pre:
                self.out.append("`")
        elif tag_l == "blockquote":
            self._in_blockquote = False
            self._ensure_blank_line()
        elif tag_l in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            self._ensure_newline()
        elif tag_l == "li":
            self._ensure_newline()
        elif tag_l == "a":
            if self._link_href:
                self.out.append(f" <{self._link_href}>")
            self._link_href = None

    def handle_data(self, data: str) -> None:
        if self._in_pre:
            self.out.append(data)
        elif self._in_blockquote:
            self.out.append(data.replace("\n", "\n> "))
        else:
            self.out.append(data)


def render_rich_html_for_cli(sanitized_html: str) -> str:
    """Render sanitized rich HTML into legible ANSI-free CLI text preserving links and images."""
    if not sanitized_html:
        return ""
    parser = _CliRichRenderer()
    parser.feed(sanitized_html)
    parser.close()
    text = "".join(parser.out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
