"""Extract donation details from an email and route it to a streamer."""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from html.parser import HTMLParser

from .config import ParsingConfig, Streamer

_FLAGS = re.IGNORECASE | re.MULTILINE


@dataclass
class ParsedDonation:
    subject: str
    text: str
    amount: Decimal | None
    currency: str
    donor: str
    message: str
    streamer_hint: str | None  # text where the streamer keyword was searched


# --------------------------------------------------------------------------
# Email -> text
# --------------------------------------------------------------------------
class _HTMLText(HTMLParser):
    BLOCK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table", "td"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    p = _HTMLText()
    p.feed(markup)
    text = html.unescape("".join(p.parts)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def email_text(msg: EmailMessage) -> str:
    """Return the readable body of an email (plain text preferred)."""
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    try:
        content = body.get_content()
    except (LookupError, UnicodeDecodeError):
        payload = body.get_payload(decode=True) or b""
        content = payload.decode("utf-8", errors="replace")
    if body.get_content_subtype() == "html":
        return html_to_text(content)
    return content.replace("\r\n", "\n").strip()


# --------------------------------------------------------------------------
# Text -> donation
# --------------------------------------------------------------------------
def _extract(pattern: str | None, text: str) -> str | None:
    if not pattern:
        return None
    m = re.search(pattern, text, _FLAGS)
    if not m:
        return None
    if "value" in m.groupdict():
        val = m.group("value")
    elif m.groups():
        val = m.group(1)
    else:
        val = m.group(0)
    return val.strip() if val is not None else None


def parse_amount(raw: str | None) -> Decimal | None:
    """Parse Swiss/European amounts: "1'000.50", "1 000,50", "25.-", "CHF 10"."""
    if not raw:
        return None
    s = re.sub(r"[^\d.,'’]", "", raw.replace(".-", "").replace(",-", ""))
    s = s.replace("'", "").replace("’", "")
    if not s:
        return None
    if "," in s and "." in s:
        # Whichever comes last is the decimal separator.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) in (1, 2) else s.replace(",", "")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        value = Decimal(s.strip("."))
    except InvalidOperation:
        return None
    return value.quantize(Decimal("0.01")) if value > 0 else None


def parse_donation(subject: str, text: str, rules: ParsingConfig) -> ParsedDonation:
    full = f"{subject}\n{text}"
    donor = _extract(rules.donor, full) or rules.default_donor
    hint = _extract(rules.streamer, full) if rules.streamer else full
    return ParsedDonation(
        subject=subject,
        text=text,
        amount=parse_amount(_extract(rules.amount, full)),
        currency=rules.currency,
        donor=donor,
        message=_extract(rules.message, full) or "",
        streamer_hint=hint,
    )


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def find_streamer(hint: str | None, streamers: list[Streamer]) -> Streamer | None:
    """Return the streamer whose keyword appears earliest in ``hint``.

    Matching is case- and accent-insensitive and on whole words, so the
    keyword "max" matches "Pour Max !" but not "maximum".
    """
    if not hint:
        return None
    text = _norm(hint)
    best: tuple[int, int, Streamer] | None = None
    for st in streamers:
        for kw in st.keywords or [st.slug]:
            kw_n = _norm(kw).strip()
            if not kw_n:
                continue
            m = re.search(rf"(?<!\w){re.escape(kw_n)}(?!\w)", text)
            if m:
                # earliest position wins; on a tie the longest keyword wins
                key = (m.start(), -len(kw_n), st)
                if best is None or key[:2] < best[:2]:
                    best = key
    return best[2] if best else None
