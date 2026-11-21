"""Finite deterministic redaction for operator-confirmed briefs, not general DLP."""
import re


def sanitize(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("摘要必须是文本")
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)\b(?:api[_-]?key|access[_-]?token|authorization)\s*[:=]\s*(?:sk-)?[A-Za-z0-9._~+/=-]{8,}", "[SECRET_REDACTED]", text)
    text = re.sub(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b", "[SECRET_REDACTED]", text)
    text = re.sub(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])", "[EMAIL_REDACTED]", text)
    text = re.sub(r"(?<!\d)\d{17}[0-9Xx](?!\d)", "[ID_REDACTED]", text)
    text = re.sub(r"(?<!\d)\d{15}(?!\d)", "[ID_REDACTED]", text)
    text = re.sub(r"(?<!\d)\d{16,19}(?!\d)", "[ACCOUNT_REDACTED]", text)
    text = re.sub(r"(?<!\d)(?:\+?86[ -]?)?1[3-9]\d(?:[ -]?\d){8}(?!\d)", "[PHONE_REDACTED]", text)
    return text.strip()
