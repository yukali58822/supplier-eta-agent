from __future__ import annotations

import hashlib
import html as html_lib
import re
from dataclasses import dataclass
from datetime import date, datetime
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from zoneinfo import ZoneInfo

MAX_BODY = 20_000


class InvalidEmail(ValueError):
    pass


@dataclass(frozen=True)
class ParsedEmail:
    message_id: str
    sender: str
    subject: str
    sent_at: datetime
    body: str
    quoted: str = ""
    attachments: tuple[str, ...] = ()

    @property
    def local_date(self) -> date:
        return self.sent_at.astimezone(ZoneInfo("Asia/Taipei")).date()


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", "", html)
    html = re.sub(r"(?i)</t[dh]>", "  ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>|<hr[^>]*>", "\n", html)
    text = html_lib.unescape(re.sub(r"<[^>]+>", "", html)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


_HTML_QUOTE = re.compile(r'(?i)<blockquote\b|class=["\']?gmail_quote|id=["\']?(divRplyFwdMsg|appendonsend)'
                         r'|class=["\']?moz-cite-prefix|border-top:\s*solid\s*#E1E1E1')


def _split_html(html: str) -> tuple[str, str]:
    m = _HTML_QUOTE.search(html)
    if not m:
        return html, ""
    start = html.rfind("<", 0, m.start() + 1)
    return html[:start], html[start:]


_SEPARATOR = re.compile(r"(?i)^-{2,}\s*(original message|forwarded message|原始郵件|原始邮件|轉寄的郵件|转发的邮件"
                        r"|轉寄郵件|轉寄訊息)\s*-{2,}$")
_HEADER_FROM = re.compile(r"(?i)^\**(from|寄件者|寄件人|發件人|发件人)\**\s*[:：]")
_HEADER_NEXT = re.compile(r"(?i)^\**(sent|date|to|subject|寄件日期|傳送時間|日期|發送時間|发送时间|收件者|收件人|主旨|主題)"
                          r"\**\s*[:：]")
_WROTE = re.compile(r"(?i)(wrote|寫道|写道)\s*[:：]?\s*$")


def _quote_start(lines: list[str]) -> int | None:
    for i, raw in enumerate(lines):
        ln = raw.strip()
        if not ln:
            continue
        if ln.startswith(">") or _SEPARATOR.match(ln):
            return i
        if re.fullmatch(r"_{10,}|-{10,}", ln) and any(_HEADER_FROM.match(x.strip()) for x in lines[i + 1:i + 4]):
            return i
        if _HEADER_FROM.match(ln) and any(_HEADER_NEXT.match(x.strip()) for x in lines[i + 1:i + 5]):
            return i
        if _WROTE.search(ln):
            if i > 0 and re.match(r"(?i)^on\s", lines[i - 1].strip()) and not re.match(r"(?i)^on\s", ln):
                return i - 1
            return i
    return None


def split_quoted(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    i = _quote_start(lines)
    if i is None:
        return text.strip(), ""
    return "\n".join(lines[:i]).strip(), "\n".join(lines[i:]).strip()


_CHARSET_FIX = {"big5": "cp950", "x-big5": "cp950", "big5-hkscs": "big5hkscs", "gb2312": "gb18030", "gbk": "gb18030"}


def _decode(part) -> str:
    cs = (part.get_content_charset() or "").lower()
    if cs in _CHARSET_FIX:
        data = part.get_payload(decode=True) or b""
        return data.decode(_CHARSET_FIX[cs], errors="replace")
    return part.get_content()


def _attachments(msg) -> tuple[str, ...]:
    names = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = part.get_content_disposition()
        name = part.get_filename()
        if disp == "attachment" or (name and disp is None and part.get_content_maintype() != "image"):
            names.append(name or part.get_content_type())
    return tuple(names)


def parse_eml(raw: bytes) -> ParsedEmail:
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as e:  # noqa: BLE001
        raise InvalidEmail("無法解析信件") from e
    sender = parseaddr(msg.get("From", ""))[1].lower()
    if not sender or "@" not in sender:
        raise InvalidEmail("缺少寄件者")
    date_hdr = msg.get("Date")
    if not date_hdr:
        raise InvalidEmail("缺少信件日期（無法換算相對日期）")
    try:
        sent = parsedate_to_datetime(str(date_hdr))
    except (TypeError, ValueError) as e:
        raise InvalidEmail("信件日期格式錯誤") from e
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=ZoneInfo("Asia/Taipei"))

    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        raise InvalidEmail("沒有可讀的內文")
    text = _decode(part)
    quoted = ""
    if part.get_content_type() == "text/html":
        new_html, quoted_html = _split_html(text)
        text, quoted = _html_to_text(new_html), _html_to_text(quoted_html)
    body, more = split_quoted(text)
    quoted = "\n\n".join(x for x in (more, quoted) if x)
    if not body and not quoted:
        raise InvalidEmail("內文為空")
    body, quoted = body[:MAX_BODY], quoted[:MAX_BODY]

    mid = (msg.get("Message-ID") or "").strip().strip("<>")
    if not mid:
        mid = "sha256-" + hashlib.sha256(raw).hexdigest()[:32]
    return ParsedEmail(message_id=mid, sender=sender, subject=str(msg.get("Subject", "")),
                       sent_at=sent, body=body, quoted=quoted, attachments=_attachments(msg))
