"""Local AnyDoc conversion. Uploaded documents are data, never instructions."""

import hashlib
from importlib.metadata import version
from pathlib import PurePosixPath
import uuid

import anydoc

MAX_FILES = 50
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_BATCH_BYTES = 50 * 1024 * 1024
MAX_TEXT_CHARS = 100_000
ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx", ".rtf"}
PARSER_VERSION = "anydoc-" + version("firecrawl-anydoc")


class ResumeParseError(ValueError):
    pass


def safe_filename(filename: str | None) -> str:
    return PurePosixPath((filename or "resume").replace("\\", "/")).name[:200]


def parse_resume(data: bytes, filename: str, mime_type: str | None = None) -> dict:
    filename = safe_filename(filename)
    if PurePosixPath(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ResumeParseError("仅支持 PDF、DOC、DOCX 和 RTF 简历。")
    if not data:
        raise ResumeParseError("文件为空，请重新选择。")
    if len(data) > MAX_FILE_BYTES:
        raise ResumeParseError("单份简历不能超过 10 MB。")
    try:
        detected = anydoc.format_from_bytes(data)
        if detected not in {"pdf", "doc", "docx", "rtf"}:
            raise ResumeParseError("文件内容不是支持的简历格式，请检查文件。")
        # No hosted OCR: conversion stays local and requires no API key.
        text = anydoc.to_markdown_bytes(data).strip()
    except anydoc.NeedsOcrError:
        raise ResumeParseError("包含扫描或图片页面，需要 OCR；请改用可选中文字的文件。") from None
    except anydoc.EncryptedError:
        raise ResumeParseError("文件有密码保护，请解锁后重新上传。") from None
    except (anydoc.ConvertError, ValueError) as exc:
        if isinstance(exc, ResumeParseError):
            raise
        raise ResumeParseError("文件无法解析，请检查是否损坏或改用其他格式。") from None
    if len(text) < 20:
        raise ResumeParseError("提取的文字过少，请检查文件或使用 OCR。")
    if len(text) > MAX_TEXT_CHARS:
        raise ResumeParseError("提取的文字超过 10 万字，请拆分文件。")
    return {
        "id": str(uuid.uuid4()), "name": PurePosixPath(filename).stem,
        "resume_text": text, "resume_hash": hashlib.sha256(text.encode()).hexdigest(),
        "source_file": filename, "original_filename": filename,
        "mime_type": mime_type, "file_size": len(data),
        "parse_status": "COMPLETED", "parser_version": PARSER_VERSION,
    }
