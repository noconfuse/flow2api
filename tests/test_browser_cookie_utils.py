from src.services.browser_cookie_utils import (
    extract_session_token_from_cookie_payload,
    parse_browser_cookie_payload,
)


def test_parse_netscape_cookie_payload():
    raw_cookie = """# Netscape HTTP Cookie File
.google.com\tTRUE\t/\tTRUE\t1790000000\tSID\tsid-value
#HttpOnly_accounts.google.com\tFALSE\t/\tTRUE\t1790000000\t__Host-GAPS\tgaps-value
"""
    cookies = parse_browser_cookie_payload(raw_cookie)

    assert len(cookies) == 2
    assert cookies[0]["name"] == "SID"
    assert cookies[0]["domain"] == ".google.com"
    assert cookies[0]["secure"] is True
    assert cookies[1]["name"] == "__Host-GAPS"
    assert cookies[1]["httpOnly"] is True
    assert cookies[1]["url"] == "https://labs.google/"
    assert cookies[1]["path"] == "/"


def test_extract_session_token_from_netscape_cookie_payload():
    raw_cookie = """# Netscape HTTP Cookie File
labs.google\tFALSE\t/\tTRUE\t1790000000\t__Secure-next-auth.session-token\tfresh-session-token
"""

    assert extract_session_token_from_cookie_payload(raw_cookie) == "fresh-session-token"
