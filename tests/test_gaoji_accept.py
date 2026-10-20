"""gaoji_accept.py 的单元测试 —— 不发真实请求。

运行: python -m pytest tests/ -v
"""

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

# gaoji_accept 在导入时读取配置，所以先注入测试值
os.environ.update({
    "GAOJI_USERNAME": "test-user",
    "GAOJI_PASSWORD": "test-pass",
    "GAOJI_PROVIDER_ID": "10001",
    "GAOJI_USER_ID": "10000001",
    "GAOJI_WINDOW_START": "07:30",
    "GAOJI_WINDOW_END": "22:00",
    # 测试日志写到临时目录，避免污染项目里的 accept.log
    "GAOJI_LOG_FILE": str(Path(tempfile.gettempdir()) / "gaoji_test_accept.log"),
})

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gaoji_accept as g  # noqa: E402


# ── 过滤器：绝不能碰到公共抢单池 ──────────────────────────────────────────────
def test_filter_scopes_to_current_engineer():
    f = g.build_filter()
    assert "${current_engineer_id}" in f, "必须用占位符让服务端限定为本人"


def test_filter_only_pending_statuses():
    f = g.build_filter()
    assert '"status":"new"' in f
    assert '"status":"assigned"' in f
    assert '"status":"open"' not in f, "open 是已接单状态，不应出现在查询里"


# ── 运行窗口 ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("hour,minute,expected", [
    (7, 29, False),
    (7, 30, True),
    (12, 0, True),
    (22, 0, True),
    (22, 1, False),
    (3, 0, False),
])
def test_in_window(hour, minute, expected):
    assert g.in_window(datetime(2026, 10, 16, hour, minute)) is expected


def test_seconds_until_window_is_zero_inside():
    assert g.seconds_until_window(datetime(2026, 10, 16, 12, 0)) == 0


def test_seconds_until_window_counts_to_next_open():
    # 06:30 → 07:30 还有 3600 秒
    assert g.seconds_until_window(datetime(2026, 10, 16, 6, 30)) == 3600


def test_seconds_until_window_wraps_to_next_day():
    # 23:00 → 次日 07:30 = 8.5 小时
    assert g.seconds_until_window(datetime(2026, 10, 16, 23, 0)) == 8 * 3600 + 1800


# ── 通知文案：HH:MM-客户名-工单主题 ─────────────────────────────────────────
def test_notification_format():
    ticket = {"subject": "打印机不出小票", "requester": {"name": "张三"}}
    text = g.notification(ticket)
    parts = text.split("-")
    assert len(parts) >= 3
    assert parts[0] == datetime.now().strftime("%H:%M")
    assert parts[1] == "张三"
    assert "打印机不出小票" in text


def test_notification_handles_missing_fields():
    text = g.notification({})
    assert "未知客户" in text and "无主题" in text


# ── 接单请求体 ───────────────────────────────────────────────────────────────
class _FakeResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"status": 0, "result": {"success": True, "ticketCommentId": 1}}


class _FakeSession:
    def __init__(self):
        self.sent = None

    def post(self, url, params=None, json=None, timeout=None, **kw):
        self.sent = {"url": url, "params": params, "json": json}
        return _FakeResp()


def test_accept_payload_shape():
    client = g.KF5()
    client.token = "tok"
    fake = _FakeSession()
    client.session = fake

    assert client.accept(123, "260700001") is True

    assert fake.sent["url"].endswith("/api/v1/ticket_comment/receive.json")
    assert fake.sent["params"]["_token"] == "tok"
    body = fake.sent["json"]
    assert body["ticket"]["id"] == 123
    assert body["open"] is True, "open=true 才会真正接单"
    assert body["user"]["id"] == 10000001
    assert body["content"] == g.AUTO_REPLY, "自动回复文案不可被改动"
    assert body["uId"], "uId 必须非空（任意唯一串）"


def test_accept_detects_api_failure():
    class _Bad(_FakeResp):
        def json(self):
            return {"status": 0, "result": {"success": False}}

    client = g.KF5()
    client.token = "tok"
    fake = _FakeSession()
    fake.post = lambda *a, **k: _Bad()
    client.session = fake
    assert client.accept(1, "x") is False
