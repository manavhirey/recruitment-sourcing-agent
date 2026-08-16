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
    parsed = urlsplit(value.strip())
    if not parsed.hostname:
        return None
    host = parsed.hostname.casefold()
    port = parsed.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = "/" + "/".join(
        segment for segment in unquote(parsed.path).split("/") if segment
    )
    if path == "/":
        path = ""
    return urlunsplit(("https", netloc, path, "", ""))


def observed_value_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
