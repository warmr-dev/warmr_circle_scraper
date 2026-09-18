"""Circle's TipTap rich text as plain text -- one flattener for every reader.

A post's identity in storage is a hash of its text, so the public reader, the
member feed and the cookie (member API) reader have to turn the same post into
the same string. They used to carry three copies of this walk that joined
nodes differently ("Hiring : ..." vs "Hiring: ..."), so one post read by two
readers became two rows (review of 2026-09-19).
"""

from __future__ import annotations

# Block nodes end a line, so paragraphs and list items don't run together.
_BLOCKS = frozenset({"paragraph", "heading", "listItem", "blockquote", "codeBlock"})


def tiptap_text(node) -> str:
    """Flatten a TipTap/ProseMirror node (or a post's tiptap_body) to text.

    Text nodes are concatenated as they are -- they carry their own spacing.
    A post's tiptap_body wraps the document ({"body": doc,
    "circle_ios_fallback_text": ..., ...}), so the walk steps into "body".
    Mentions ("@Name") and links to posts or events ("#Title") are leaf nodes
    without a text child; their visible text is circle_ios_fallback_text.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(tiptap_text(child) for child in node)
    if not isinstance(node, dict):
        return ""
    if "type" not in node and isinstance(node.get("body"), dict):
        return tiptap_text(node["body"])
    out: list[str] = []
    if node.get("type") == "text" and isinstance(node.get("text"), str):
        out.append(node["text"])
    elif node.get("circle_ios_fallback_text") and not node.get("content"):
        out.append(str(node["circle_ios_fallback_text"]))
    elif node.get("type") == "hardBreak":
        out.append("\n")
    for child in node.get("content") or []:
        out.append(tiptap_text(child))
    if node.get("type") in _BLOCKS:
        out.append("\n")
    return "".join(out)


def tiptap_plain(node) -> str:
    """tiptap_text on one line, the way every reader stores a post body.

    Whitespace only: TipTap text nodes are plain text, and strip_html would
    delete anything between a literal "<" and ">" ("<5 years ... ->").
    """
    return " ".join(tiptap_text(node).split())
