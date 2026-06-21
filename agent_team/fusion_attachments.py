"""
fusion_attachments.py — accept, scan, parse, and encrypt files a signed-in user attaches
to a Fusion conversation (Fusion 2.0 R1: document understanding).

The pipeline REUSES the report product's building blocks: file_tools.parse_file turns a
PDF / Excel / CSV / Word / text file into model-ready text, security.looks_executable
rejects spoofed binaries, and crypto encrypts both the raw file and the extracted text at
rest. The DB rows are owned by metrics_store; this module owns the disk + parse side and
the context block injected into a chat turn.
"""
import base64
import hashlib
import mimetypes
import os
import re

from . import config, crypto, metrics_store, security
from .file_tools import parse_file

# Strip path separators + control chars; keep CJK (secure_filename would drop it entirely).
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

# Images are capped tighter than documents — vision APIs (Anthropic ~5 MB) reject big ones.
_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_IMAGE_MAGIC = {b"\x89PNG\r\n\x1a\n": "image/png", b"\xff\xd8\xff": "image/jpeg",
                b"GIF87a": "image/gif", b"GIF89a": "image/gif"}


def _sniff_image(head):
    """The real media type from the leading bytes (so a renamed non-image is rejected)."""
    for magic, mt in _IMAGE_MAGIC.items():
        if head.startswith(magic):
            return mt
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":   # WebP: 'RIFF'....'WEBP'
        return "image/webp"
    return None


class AttachmentError(ValueError):
    """A user-facing rejection (unsupported type, too big, looks executable)."""


def _safe_name(filename, fallback="file"):
    base = os.path.basename((filename or "").replace("\\", "/")).strip()
    base = _UNSAFE.sub("_", base).strip(" .")
    return base or fallback


def _attachments_root():
    return os.path.join(metrics_store.DATA_DIR, "fusion_attachments")


def accept_attachment(account_id, conversation_id, filename, fileobj):
    """Validate, store (encrypted), and parse one attachment for a conversation. A DOCUMENT
    is parsed to text (file_tools); an IMAGE is stored for the vision path (no text). Returns
    the attachment's public dict, or raises AttachmentError with a Chinese reason the route
    surfaces as a 400."""
    name = _safe_name(filename)
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    is_image = ext in config.FUSION_IMAGE_EXTS
    if not is_image and ext not in config.FUSION_ATTACHMENT_EXTS:
        raise AttachmentError(
            f"不支持的文件类型 “.{ext}”。可上传 PDF / Excel / CSV / Word / 文本 / 图片。")

    raw = fileobj.read()
    if not raw:
        raise AttachmentError("文件为空。")
    if security.looks_executable(raw[:16]):
        raise AttachmentError(f"文件 “{name}” 看起来是可执行程序，已拒绝。")

    if is_image:
        media_type = _sniff_image(raw[:16])
        if not media_type:
            raise AttachmentError(f"文件 “{name}” 不是有效的图片。")
        if len(raw) > _IMAGE_MAX_BYTES:
            raise AttachmentError(f"图片 “{name}” 超过 {_IMAGE_MAX_BYTES // (1024 * 1024)} MB 上限。")
    else:
        cap = config.fusion_attachment_max_bytes()
        if len(raw) > cap:
            raise AttachmentError(f"文件 “{name}” 超过 {cap // (1024 * 1024)} MB 上限。")
        media_type = mimetypes.guess_type(name)[0]

    content_hash = hashlib.sha256(raw).hexdigest()
    folder = os.path.join(_attachments_root(), str(account_id), str(conversation_id))
    os.makedirs(folder, exist_ok=True)
    stored_path = os.path.join(folder, f"{content_hash[:24]}.{ext or 'bin'}")
    with open(stored_path, "wb") as fh:
        fh.write(raw)

    # An image carries no parsed text — it rides the vision path as native image input. A
    # document is parsed (server mode → unrestricted) BEFORE we encrypt it at rest.
    parsed = None if is_image else parse_file(stored_path)
    crypto.encrypt_file_in_place(stored_path)   # no-op when FERNET unset

    return metrics_store.create_fusion_attachment(
        conversation_id, account_id, name, media_type=media_type,
        kind=("image" if is_image else "document"), file_size=len(raw),
        content_hash=content_hash,
        char_count=(0 if is_image else parsed.get("chars", 0)),
        stored_path=stored_path,
        parsed_text=(None if is_image else parsed.get("text", "")))


def image_part(att):
    """Build an OpenAI-style image content part (a base64 data URL) from a stored image
    attachment dict (needs stored_path + media_type). Returns None if unreadable. The
    Anthropic adapter converts this to a native Claude image block; the OpenAI-compatible
    adapter (e.g. Qwen-VL via DashScope) forwards it as-is."""
    path = att.get("stored_path")
    if not path or not os.path.isfile(path):
        return None
    try:
        with crypto.materialize_plaintext(path) as plain:
            with open(plain, "rb") as fh:
                data = fh.read()
    except OSError:
        return None
    mt = att.get("media_type") or "image/png"
    b64 = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mt};base64,{b64}"}}


# Bound the COMBINED attachment text fed to the council, so several big files can't blow
# past model context windows (file_tools already caps each doc at 60k chars on its own).
_CONTEXT_CHAR_CAP = 48_000


def build_attachment_context(attachments, max_chars=_CONTEXT_CHAR_CAP):
    """Render fetched attachment dicts (each carrying decrypted `text`) into a context block
    prepended to the user's message, so EVERY council member reads the files. The combined
    size is capped (with a visible truncation notice). Returns '' when nothing has text."""
    blocks, used = [], 0
    for a in (attachments or []):
        text = (a.get("text") or "").strip()
        if not text:
            continue
        head = f"--- 文件：{a.get('filename', 'file')} ---\n"
        budget = max_chars - used
        if budget <= len(head) + 200:
            blocks.append("[更多附件内容因长度限制已省略]")
            break
        body = text[:budget - len(head)]
        if len(body) < len(text):
            body += "\n[... 该文件内容过长，已截断 ...]"
        blocks.append(head + body)
        used += len(head) + len(body)
    if not blocks:
        return ""
    return ("[附件内容 — 用户上传的文件，供你参考]\n\n"
            + "\n\n".join(blocks) + "\n\n[附件结束]")


def prune(older_than_days=30):
    """Hard-delete soft-deleted attachment rows + their encrypted blobs (scheduler job)."""
    removed = 0
    for path in metrics_store.prune_fusion_attachments(older_than_days):
        try:
            if path and os.path.isfile(path):
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed
