"""
tests/test_client_ip.py — client_ip._client_ip() の優先順位・不正値フォールバックの検証

検証方針:
  Flask の test_request_context でヘッダ・REMOTE_ADDR を差し替え、
  優先順位（CF-Connecting-IP → True-Client-IP → remote_addr）と、
  不正な形式のヘッダ値は次点にフォールバックすることを確認する。
  X-Forwarded-For は採用しない（偽装可能なため）ことも担保する。
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask

from client_ip import _client_ip, _valid_ip_or_none, _MAX_IP_LEN

_app = Flask(__name__)


def test_cf_connecting_ip_has_highest_priority():
    with _app.test_request_context(
        headers={"CF-Connecting-IP": "1.2.3.4", "True-Client-IP": "5.6.7.8"},
        environ_base={"REMOTE_ADDR": "9.9.9.9"},
    ):
        assert _client_ip() == "1.2.3.4"


def test_true_client_ip_is_second_priority():
    with _app.test_request_context(
        headers={"True-Client-IP": "5.6.7.8"},
        environ_base={"REMOTE_ADDR": "9.9.9.9"},
    ):
        assert _client_ip() == "5.6.7.8"


def test_falls_back_to_remote_addr_when_no_headers():
    with _app.test_request_context(environ_base={"REMOTE_ADDR": "10.0.0.1"}):
        assert _client_ip() == "10.0.0.1"


def test_invalid_cf_header_falls_back_to_true_client_ip():
    """CF-Connecting-IPがIPアドレスとしてパース不能なら次点にフォールバックする"""
    with _app.test_request_context(
        headers={"CF-Connecting-IP": "not-an-ip", "True-Client-IP": "5.6.7.8"},
        environ_base={"REMOTE_ADDR": "9.9.9.9"},
    ):
        assert _client_ip() == "5.6.7.8"


def test_invalid_all_headers_falls_back_to_remote_addr():
    with _app.test_request_context(
        headers={"CF-Connecting-IP": "garbage", "True-Client-IP": "also-garbage"},
        environ_base={"REMOTE_ADDR": "10.0.0.1"},
    ):
        assert _client_ip() == "10.0.0.1"


def test_invalid_remote_addr_returns_unknown():
    with _app.test_request_context(environ_base={"REMOTE_ADDR": ""}):
        assert _client_ip() == "unknown"


def test_x_forwarded_for_is_not_used():
    """XFFは偽装可能なため採用しない。XFFのみ指定してもremote_addrが使われる。"""
    with _app.test_request_context(
        headers={"X-Forwarded-For": "1.1.1.1, 2.2.2.2"},
        environ_base={"REMOTE_ADDR": "10.0.0.1"},
    ):
        assert _client_ip() == "10.0.0.1"


def test_valid_ip_or_none_rejects_non_ip_string():
    assert _valid_ip_or_none("not-an-ip") is None
    assert _valid_ip_or_none("") is None
    assert _valid_ip_or_none("   ") is None


def test_valid_ip_or_none_accepts_ipv4_and_ipv6():
    assert _valid_ip_or_none("203.0.113.5") == "203.0.113.5"
    assert _valid_ip_or_none("2001:db8::1") == "2001:db8::1"


def test_valid_ip_or_none_truncates_to_45_chars():
    """検証済みの値がIPv6最大長(45文字)を超えないよう、念のため切り詰める防御を確認する。
    ipaddress.ip_address() を通れば45文字を超えることは通常無いため、境界値を
    直接検証するためにパース処理自体をモックする。"""
    long_value = "1" * 100
    with patch("client_ip.ipaddress.ip_address", return_value=None):
        result = _valid_ip_or_none(long_value)
    assert result is not None
    assert len(result) == _MAX_IP_LEN
