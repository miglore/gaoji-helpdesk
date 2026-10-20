#!/usr/bin/env python3
"""KF5 帮助台纯 API 自动接单常驻进程。

零浏览器、零 Playwright：每 POLL_SECONDS 秒轮询一次“本人待接单”列表，
发现工单后直接用 ``POST /api/v1/ticket_comment/receive.json`` 完成接单，
单张工单耗时约 2 秒。

所有凭据与运行参数都通过环境变量注入（见 README 的「配置」章节），
源码中不包含任何账号、密码或内网地址。
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import requests

# ── 配置（全部可用环境变量覆盖）───────────────────────────────────────────────
BASE_URL = os.getenv("GAOJI_BASE_URL", "https://helpdesk.example.com").rstrip("/")
PROVIDER_ID = os.getenv("GAOJI_PROVIDER_ID", "").strip()
USER_ID = int(os.getenv("GAOJI_USER_ID", "0") or 0)   # 0 = 登录后自动探测
USERNAME = os.getenv("GAOJI_USERNAME", "").strip()
PASSWORD = os.getenv("GAOJI_PASSWORD", "")

POLL_SECONDS = int(os.getenv("GAOJI_POLL_SECONDS", "5"))

# 只在「我的待接单」里状态为 new/assigned 的工单上动作
ALLOWED_STATUSES = {"new", "assigned"}

# 运行窗口，默认每天 07:30–22:00（窗口外休眠到下一天）
def _hhmm(text: str) -> int:
    """'07:30' → 450（分钟数）。"""
    hour, minute = text.split(":")
    return int(hour) * 60 + int(minute)


WINDOW_START_MIN = _hhmm(os.getenv("GAOJI_WINDOW_START", "07:30"))
WINDOW_END_MIN = _hhmm(os.getenv("GAOJI_WINDOW_END", "22:00"))

LOG_FILE = Path(os.getenv("GAOJI_LOG_FILE") or (Path(__file__).resolve().parent / "accept.log"))

# 接单时自动回复给门店的文案（如需改动，改这里，然后重启服务）
AUTO_REPLY = os.getenv(
    "GAOJI_AUTO_REPLY",
    "您好，请描述一下具体情况并附上截图，我们尽快为您处理；"
    "如需远程协助，请提供远程工具识别码和验证码。感谢您的配合！",
)


def log(message: str) -> None:
    """同时写文件与 stdout（stdout 会被 systemd 收进 journal）。"""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def in_window(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    minutes = now.hour * 60 + now.minute
    return WINDOW_START_MIN <= minutes <= WINDOW_END_MIN


def seconds_until_window(now: datetime | None = None) -> int:
    """返回距离下一个运行窗口开始的秒数；已在窗口内返回 0。"""
    now = now or datetime.now()
    start = now.replace(hour=WINDOW_START_MIN // 60, minute=WINDOW_START_MIN % 60,
                        second=0, microsecond=0)
    end = now.replace(hour=WINDOW_END_MIN // 60, minute=WINDOW_END_MIN % 60,
                      second=0, microsecond=0)
    if now < start:
        return max(1, int((start - now).total_seconds()))
    if now > end:
        return max(1, int((start + timedelta(days=1) - now).total_seconds()))
    return 0


def notification(ticket: dict) -> str:
    """接单通知文案：``HH:MM-客户名-工单主题``（一行一条，供微信/QQ 推送）。"""
    customer = (ticket.get("requester") or {}).get("name") or ticket.get("requester.name") or "未知客户"
    subject = str(ticket.get("subject") or "无主题").strip()
    return f"{datetime.now():%H:%M}-{customer}-{subject}"


def build_filter() -> str:
    """KF5 view_tickets 过滤器。

    关键：``engineer.id`` 用 ``${current_engineer_id}`` 占位，服务端会替换成
    当前登录工程师 —— 这样只会看到「我的待接单」，绝不会看到公共抢单池。
    """
    return (
        '{"$and":['
        '{"$or":[{"$eq":{"status":"new"}},{"$eq":{"status":"assigned"}}]},'
        '{"$eq":{"engineer.id":${current_engineer_id}}}'
        ']}'
    )


class KF5:
    """帮助台 API 客户端（一个 requests.Session 贯穿登录态）。"""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        })
        self.token = ""
        self.user_id = USER_ID   # 0 表示尚未探测，登录后自动获取

    # ── 认证 ─────────────────────────────────────────────────────────────────
    def login(self) -> None:
        """POST /v3/login → 跟随 /v3/login-success → 从 Cookie 取 user_token。"""
        r = self.session.post(
            f"{BASE_URL}/v3/login",
            data={
                "username": USERNAME, "password": PASSWORD,
                "remember_me": "true", "captcha": "",
                "provider.id": PROVIDER_ID, "type": "account",
            },
            timeout=20,
        )
        r.raise_for_status()
        self.session.get(f"{BASE_URL}/v3/login-success", timeout=15).raise_for_status()
        self.token = self.session.cookies.get("user_token", "")
        if not self.token:
            raise RuntimeError("login returned no user_token（账号/密码/provider 是否正确？）")

    def detect_user_id(self) -> int:
        """登录后自动获取本人的 user_id（接单请求里的 ``user.id``）。

        做法：用 ``${current_engineer_id}`` 占位符查一条「我处理过」的工单，
        再读该工单详情里的 ``engineer.user.id`` —— 这个值就是接单需要的 user_id。
        完全没有历史工单时返回 0（此时需手动设置 GAOJI_USER_ID）。
        """
        for status in ("open", "new", "assigned", "processing", "closed"):
            flt = ('{"$and":['
                   f'{{"$eq":{{"status":"{status}"}}}},'
                   '{"$eq":{"engineer.id":${current_engineer_id}}}]}')
            try:
                r = self.session.get(
                    f"{BASE_URL}/api/v1/view_tickets",
                    params=self._params(_count="1", _page="1", _sort="id", _order="desc",
                                        _fields="id,status", _filter=flt),
                    timeout=20,
                )
                r.raise_for_status()
                tickets = (r.json().get("result") or {}).get("tickets") or []
            except Exception:
                continue
            if not tickets:
                continue
            try:
                d = self.session.get(f"{BASE_URL}/api/v1/tickets/{tickets[0]['id']}",
                                     params=self._params(), timeout=20)
                d.raise_for_status()
                engineer = (d.json().get("result") or {}).get("engineer") or {}
                uid = int((engineer.get("user") or {}).get("id") or 0)
                if uid:
                    return uid
            except Exception:
                continue
        return 0

    def _params(self, **extra) -> dict:
        return {"provider_id": PROVIDER_ID, "_token": self.token, **extra}

    # ── 查待接单 ─────────────────────────────────────────────────────────────
    def pending(self) -> list[dict]:
        r = self.session.get(
            f"{BASE_URL}/api/v1/view_tickets",
            params=self._params(
                _count="20", _page="1", _sort="createdAt", _order="desc",
                # 注意：_fields 里不能出现 engineer 或裸 requester，否则返回 0 条
                _fields="id,status,no,subject,requester.name",
                _filter=build_filter(),
            ),
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        return (data.get("result") or {}).get("tickets") or []

    # ── 接单 ─────────────────────────────────────────────────────────────────
    def accept(self, ticket_id: int, ticket_no: str) -> bool:
        payload = {
            "ticket": {"id": ticket_id},
            "attachments": [],
            "content": AUTO_REPLY,
            "open": True,
            "user": {"id": self.user_id or USER_ID},
            "uId": uuid.uuid4().hex,   # 任意唯一串即可
        }
        r = self.session.post(
            f"{BASE_URL}/api/v1/ticket_comment/receive.json",
            params=self._params(),
            json=payload,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") != 0 or not (data.get("result") or {}).get("success"):
            log(f"accept #{ticket_no}: API returned {data}")
            return False
        return True

    # ── 回读状态（验证真的接上了）─────────────────────────────────────────────
    def status(self, ticket_id: int) -> str:
        r = self.session.get(
            f"{BASE_URL}/api/v1/tickets/{ticket_id}",
            params=self._params(),
            timeout=20,
        )
        r.raise_for_status()
        return (r.json().get("result") or {}).get("status", "")


def _require_config() -> None:
    """只强制要求登录必需的项；USER_ID 未设置时会在登录后自动探测。"""
    missing = [n for n, v in (("GAOJI_USERNAME", USERNAME), ("GAOJI_PASSWORD", PASSWORD),
                              ("GAOJI_PROVIDER_ID", PROVIDER_ID)) if not v]
    if missing:
        log(f"配置缺失: {', '.join(missing)} —— 请设置环境变量后重启服务（见 README）")
        sys.exit(78)   # EX_CONFIG


def main() -> int:
    _require_config()
    client = KF5()
    while True:
        wait = seconds_until_window()
        if wait:
            log(f"window closed; sleeping {wait}s")
            time.sleep(wait)
        try:
            client.login()
            if not (client.user_id or USER_ID):
                uid = client.detect_user_id()
                if not uid:
                    log("无法自动获取 user_id：请在 secrets.env 设置 GAOJI_USER_ID"
                        "（或重新运行 ./setup.sh）")
                    sys.exit(78)
                client.user_id = uid
                log(f"已自动获取 user_id={uid}")
            seen_ids: set[int] = set()
            log("pure-api acceptor started")
            while in_window():
                tickets = client.pending()
                for ticket in tickets:
                    tid = int(ticket["id"])
                    tno = ticket["no"]
                    if tid in seen_ids:
                        continue
                    seen_ids.add(tid)

                    log(f"accepting #{tno} {str(ticket.get('subject') or '')[:60]}")
                    if not client.accept(tid, tno):
                        log(f"accept #{tno}: API call failed")
                        continue

                    # 回读验证：状态真的离开 new/assigned 才算接单成功
                    new_status = ""
                    for _ in range(8):
                        new_status = client.status(tid)
                        if new_status not in ALLOWED_STATUSES:
                            break
                        time.sleep(1)

                    if new_status in ALLOWED_STATUSES:
                        log(f"accept #{tno}: status stayed {new_status}")
                    else:
                        log(f"accepted #{tno}: {ticket['status']} -> {new_status}")
                        # 需要接单推送时取消下一行注释（格式 HH:MM-客户名-主题）
                        # print(notification(ticket), flush=True)

                time.sleep(POLL_SECONDS)
            log("window ended; waiting for next window")
            seen_ids.clear()
        except Exception as exc:               # 网络抖动/会话过期 → 重建客户端重试
            log(f"acceptor error: {type(exc).__name__}: {exc}")
            client = KF5()
            time.sleep(15)


if __name__ == "__main__":
    sys.exit(main())
