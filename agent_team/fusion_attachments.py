"""
fusion_attachments.py — accept, scan, parse, and encrypt files a signed-in user attaches
to a Fusion conversation (Fusion 2.0 R1: document understanding).

The pipeline REUSES the report product's building blocks: file_tools.parse_file turns a
PDF / Excel / CSV / Word / text file into model-ready text, security.looks_executable
rejects spoofed binaries, and crypto encrypts both the raw file and the extracted text at
rest. The DB rows are owned by metrics_store; this module owns the disk + parse side and
the context block injected into a chat turn.
"""
import hashlib
import mimetypes
import os
import re

from . import config, crypto, metrics_store, security
from .file_tools import parse_file

# Strip path separators + control chars; keep CJK (secure_filename would drop it entirely).
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


class AttachmentError(ValueError):
    """A user-facing rejection (unsupported type, too big, looks executable)."""


def _safe_name(filename, fallback="file"):
    base = os.path.basename((filename or "").replace("\\", "/")).strip()
    base = _UNSAFE.sub("_", base).strip(" .")
    return base or fallback


def _attachments_root():
    return os.path.join(metrics_store.DATA_DIR, "fusion_attachments")


def accept_attachment(account_id, conversation_id, filename, fileobj):
    """Validate, store (encrypted), and parse one attachment for a conversation. Returns the
    attachment's public dict, or raises AttachmentError with a Chinese reason the route
    surfaces as a 400."""
    name = _safe_name(filename)
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    if ext not in config.FUSION_ATTACHMENT_EXTS:
        raise AttachmentError(f"不支持的文件类型 “.{ext}”。可上传 PDF / Excel / CSV / Word / 文本。")

    raw = fileobj.read()
    if not raw:
        raise AttachmentError("文件为空。")
    if security.looks_executable(raw[:16]):
        raise AttachmentError(f"文件 “{name}” 看起来是可执行程序，已拒绝。")
    cap = config.fusion_attachment_max_bytes()
    if len(raw) > cap:
        raise AttachmentError(f"文件 “{name}” 超过 {cap // (1024 * 1024)} MB 上限。")

    content_hash = hashlib.sha256(raw).hexdigest()
    folder = os.path.join(_attachments_root(), str(account_id), str(conversation_id))
    os.makedirs(folder, exist_ok=True)
    stored_path = os.path.join(folder, f"{content_hash[:24]}.{ext or 'bin'}")
    with open(stored_path, "wb") as fh:
        fh.write(raw)

    # Parse the PLAINTEXT on disk first (server mode → unrestricted), THEN encrypt it at
    # rest so a leaked disk/backup never exposes the original.
    parsed = parse_file(stored_path)
    crypto.encrypt_file_in_place(stored_path)   # no-op when FERNET unset

    return metrics_store.create_fusion_attachment(
        conversation_id, account_id, name,
        media_type=mimetypes.guess_type(name)[0], kind="document",
        file_size=len(raw), content_hash=content_hash,
        char_count=parsed.get("chars", 0), stored_path=stored_path,
        parsed_text=parsed.get("text", ""))


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
