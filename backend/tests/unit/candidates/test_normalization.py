from app.candidates.normalization import normalize_profile_url, normalize_text


def test_profile_url_normalization_removes_tracking() -> None:
    assert (
        normalize_profile_url("https://www.linkedin.com/in/priya/?trk=search")
        == "https://www.linkedin.com/in/priya"
    )


def test_profile_url_normalization_canonicalizes_host_and_fragment() -> None:
    assert (
        normalize_profile_url("HTTP://WWW.LINKEDIN.COM:80/in/Priya-Sharma//#about")
        == "https://www.linkedin.com/in/Priya-Sharma"
    )


def test_profile_url_normalization_rejects_malformed_port() -> None:
    assert normalize_profile_url("https://www.linkedin.com:notaport/in/priya") is None


def test_text_normalization_is_unicode_and_whitespace_stable() -> None:
    assert normalize_text("  PRIYA\u00a0  Sharma  ") == "priya sharma"
