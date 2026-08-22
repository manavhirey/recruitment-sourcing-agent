import hashlib
import unicodedata
from urllib.parse import unquote, urlsplit, urlunsplit


def normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
    return normalized or None


def normalize_profile_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not hostname:
        return None
    host = hostname.casefold()
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = "/" + "/".join(
        segment for segment in unquote(parsed.path).split("/") if segment
    )
    if path == "/":
        path = ""
    return urlunsplit(("https", netloc, path, "", ""))


def observed_value_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
