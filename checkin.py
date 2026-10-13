#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GLaDOS / Railgun 自动签到（合体版）
====================================
整合 Devilstore(Glados-Railgun-checkin) 与 MZRidaz(GLaDOS-Auto-Checkin) 的优点：

  - 多服务自适应：内置 GLaDOS / Railgun 等多个候选域名，每个账号首次探测到可用域名即锁定复用
  - 设备平台自适应：code 4 device-mismatch 时自动按 loginDevice 切换 UA 重试
  - 8 路推送：PushDeer / Server酱 / Telegram / PushPlus / 钉钉 / 飞书 / 企业微信 / 云湖
  - Cookie 前缀无关校验 + 归一化 + 隐私脱敏（邮箱/Cookie 自动打码）
  - 积分兑换阈值保护（总积分够才兑，不浪费）
  - 签到失败自动重试（网络/HTTP/未知失败各重试一次；鉴权失败不重试）
  - 定时兜底：一天可触发多次，当天已签到则静默跳过推送，不重复刷屏
  - 网络重试 + 退出码区分「业务失败」与「通知失败」

仅依赖 requests，可直接跑在 GitHub Actions。
"""
import os
import re
import sys
import time
import random
import hashlib
import hmac
import base64
import urllib.parse
import logging
from typing import Any, Dict, List, Optional, Tuple, Callable

import requests

# ---- 统一时区为北京时间 ----
# GitHub Actions runner 默认 UTC；若不设置，日志时间会与「实际收到通知的北京时间」差 8 小时，
# 看起来像"跑错时间"。此处强制 TZ=Asia/Shanghai，使日志时间与通知时间一致。
os.environ.setdefault("TZ", "Asia/Shanghai")
if hasattr(time, "tzset"):
    try:
        time.tzset()
    except Exception:
        pass

VERSION = "v1.3.1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("Checkin")

# ===================== 配置常量 =====================
CHECKIN_PATH = "/api/user/checkin"
STATUS_PATH = "/api/user/status"
POINTS_PATH = "/api/user/points"
EXCHANGE_PATH = "/api/user/exchange"

TIMEOUT = (10, 30)          # (连接超时, 读取超时)
MIN_DELAY = 1.0            # 账号间随机延迟下限（秒）
MAX_DELAY = 2.0            # 账号间随机延迟上限（秒）
CONTENT_MAX_LENGTH = 3000  # 推送汇总内容统一长度上限
COOKIE_MASK_LENGTH = 10    # Cookie 脱敏前后保留字符数
COOKIE_MIN_LENGTH = 24     # Cookie 低于此长度整体脱敏

# 签到失败自动重试：总尝试次数 = 1(原始) + (MAX-1) 次重试
# 仅对「可恢复失败」（网络错误 / HTTP 错误 / 未知业务失败）重试；鉴权失败重试无意义，不重试
CHECKIN_MAX_ATTEMPTS = 2
CHECKIN_RETRY_DELAY = 5    # 重试前等待秒数

# 候选域名（自适应探测；可用 DOMAINS 环境变量覆盖，逗号分隔）
DEFAULT_DOMAINS = [
    "railgun.info", "glados.cloud", "glados.space",
    "glados.one", "railgun.space",
]

# 各平台合法 User-Agent（仅平台 token 关键，版本号无关）
PLATFORM_UA = {
    "Windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "macOS": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Linux": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "iPhone": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "Android": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
}

REPEAT_KEYWORDS = ("repeat", "already", "重复", "已签到", "签到过", "请勿")
AUTH_FAIL_KEYWORDS = ("没有权限", "权限不足", "未登录", "登录已失效", "unauthorized", "forbidden", "invalid token")

# Cookie 会话字段「结构」正则：任意前缀 + sess / sess.sig（前缀改名零改动适配）
_SESS_KEY_RE = re.compile(r"^(?P<prefix>[A-Za-z0-9_.-]+):sess$")
_SIG_KEY_RE = re.compile(r"^(?P<prefix>[A-Za-z0-9_.-]+):sess\.sig$")

COOKIE_FORMAT_HINT = (
    "期望 Cookie 格式（前缀任意，sess 与 sess.sig 必须成对）:\n"
    "  gld:sess=xxxxxx; gld:sess.sig=yyyyyy   （现网签发）\n"
    "  koa:sess=xxxxxx; koa:sess.sig=yyyyyy   （旧版签发）\n"
    "获取：登录站点 → F12 → Application/Cookies → 复制完整 Cookie 值"
)

# 积分兑换计划：消耗 points 积分兑换 days 天会员；仅显式配置才执行，默认不兑换
EXCHANGE_PLANS = {
    "plan100": {"points": 100, "days": 10},
    "plan200": {"points": 200, "days": 30},
    "plan500": {"points": 500, "days": 100},
}

TELEGRAM_MAX_LENGTH = 4000
TELEGRAM_TRUNCATE_LENGTH = 3990


# ===================== 工具函数 =====================
def safe_json(resp: requests.Response) -> Dict[str, Any]:
    """安全解析 JSON 响应（非关键路径，失败返回空字典）。"""
    try:
        return resp.json()
    except (ValueError, requests.exceptions.JSONDecodeError):
        return {}


def safe_int_str(val: Any, default: str = "-") -> str:
    """安全转整数字符串，失败返回默认值。"""
    try:
        return str(int(val))
    except (TypeError, ValueError):
        try:
            return str(int(float(val)))
        except (TypeError, ValueError):
            return default


def mask_email(email: str) -> str:
    """邮箱脱敏：保留前两位与最后一位，中间用 *** 替代。"""
    if not email or email == "unknown" or "@" not in email:
        return email
    try:
        name, domain = email.rsplit("@", 1)
        if not name:
            return email
        masked_name = "***" if len(name) <= 3 else f"{name[:2]}***{name[-1]}"
        return f"{masked_name}@{domain}"
    except Exception:
        return email


def mask_cookie(cookie: str) -> str:
    """Cookie 脱敏（只显示前后各 10 个字符）。"""
    if not cookie or len(cookie) <= COOKIE_MIN_LENGTH:
        return "***"
    return f"{cookie[:COOKIE_MASK_LENGTH]}...{cookie[-COOKIE_MASK_LENGTH:]}"


def _escape_markdown(text: str) -> str:
    """转义 Markdown 特殊字符，防止外部文本破坏推送格式。"""
    if not text:
        return text
    for ch in ("\\", "`", "*", "_", "#", "[", "]"):
        text = text.replace(ch, f"\\{ch}")
    return text


def parse_earned_points(raw: Any) -> int:
    """解析本次签到获得的积分（来源按可靠性排序，适配 GLaDOS 不同版本的返回结构）。

    1) 响应字段：顶层 ``points`` / ``data.points``（Devilstore 版实现即依赖此字段）
    2) 响应文案：英文 "got N points" / 中文 "获得 N 点"（MZRidaz 版实现依赖此文案）

    注意：**不区分「签到成功 / 已签到」** —— GLaDOS 在「已签到」时同样会在响应里
    带上今日已获得的积分（如「已经签到成功，获得 1 点」），这里一并解析出来。
    """
    if not isinstance(raw, dict):
        return 0
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    for val in (raw.get("points"), data.get("points")):
        try:
            if val is not None and str(val).strip() != "":
                return int(float(val))
        except (TypeError, ValueError):
            continue
    msg = str(raw.get("message") or "")
    m = re.search(r"got\s+(\d+)\s+points?", msg, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"获得\s*(\d+)\s*点", msg)
    return int(m.group(1)) if m else 0


def normalize_cookie(cookie: str) -> str:
    """归一化用户粘贴的 Cookie：去首尾空白/引号/"Cookie:" 头名，不改写前缀。"""
    c = (cookie or "").strip()
    while len(c) >= 2 and c[0] == c[-1] and c[0] in ('"', "'"):
        c = c[1:-1].strip()
    if c[:7].lower() == "cookie:":
        c = c[7:].strip()
    return c


def parse_session_prefixes(cookie: str) -> Dict[str, Dict[str, bool]]:
    """解析 Cookie 中的会话字段，返回 {前缀: {"sess": bool, "sig": bool}}。"""
    found: Dict[str, Dict[str, bool]] = {}
    for part in (cookie or "").split(";"):
        key = part.strip().split("=", 1)[0].strip()
        if not key:
            continue
        m_sig = _SIG_KEY_RE.match(key)
        if m_sig:
            found.setdefault(m_sig.group("prefix"), {}).setdefault("sig", True)
            continue
        m_sess = _SESS_KEY_RE.match(key)
        if m_sess:
            found.setdefault(m_sess.group("prefix"), {}).setdefault("sess", True)
    return found


def extract_session_prefix(cookie: str) -> Optional[str]:
    """返回 Cookie 中「sess 与 sess.sig 成对」的会话前缀；无则 None。"""
    for prefix, flags in parse_session_prefixes(cookie).items():
        if flags.get("sess") and flags.get("sig"):
            return prefix
    return None


def validate_cookie(cookie: str) -> Tuple[bool, str]:
    """验证 Cookie 是否为合法 GLaDOS/Railgun 会话（结构校验，前缀无关）。"""
    cookie = normalize_cookie(cookie)
    if not cookie:
        return False, f"Cookie 为空。{COOKIE_FORMAT_HINT}"
    groups = parse_session_prefixes(cookie)
    complete, incomplete = [], []
    for prefix, flags in groups.items():
        (complete if (flags.get("sess") and flags.get("sig")) else incomplete).append(prefix)
    if complete:
        return True, ""
    actual = ", ".join(sorted(groups.keys())) or "（未解析到任何 :sess / :sess.sig 字段）"
    if incomplete:
        return False, f"Cookie 会话字段不成对：前缀 {incomplete} 缺少 sess 或 sess.sig；实际键名: {actual}。{COOKIE_FORMAT_HINT}"
    return False, f"Cookie 缺少 <前缀>:sess 与 <前缀>:sess.sig 字段；实际键名: {actual}。{COOKIE_FORMAT_HINT}"


def is_auth_fail(message: str) -> bool:
    """消息是否属于「鉴权失败」（Cookie 失效类，重试无意义）。"""
    low = (message or "").lower()
    return any(kw in low for kw in AUTH_FAIL_KEYWORDS)


def classify_checkin(code: Any, message: str) -> str:
    """判断签到结果: ok / repeat / fail。GLaDOS: code=0 成功, code=1 已签到。"""
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = -2
    if code == 0:
        return "ok"
    if code == 1:
        return "repeat"
    msg = (message or "").lower()
    if re.search(r"got\s+\d+\s+points?", msg) or re.search(r"获得\s*\d+\s*点", message or ""):
        return "ok"
    if any(kw in msg for kw in REPEAT_KEYWORDS):
        return "repeat"
    return "fail"


# ===================== 推送函数 =====================
def _push_request(
    name: str,
    url: str,
    *,
    json_payload: Optional[Dict[str, Any]] = None,
    data_payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    success_check: Callable[[Dict[str, Any], requests.Response], bool],
    fail_msg_keys: Tuple[str, ...] = ("message",),
) -> bool:
    """通用推送请求函数，返回是否推送成功。"""
    try:
        if json_payload is not None:
            r = requests.post(url, json=json_payload, headers=headers, timeout=TIMEOUT)
        else:
            r = requests.post(url, data=data_payload, headers=headers, timeout=TIMEOUT)
        if not r.ok:
            logger.warning("%s 推送失败: HTTP %d", name, r.status_code)
            return False
        resp = safe_json(r)
        if success_check(resp, r):
            logger.info("%s 推送成功", name)
            return True
        fail_msg = r.text
        for key in fail_msg_keys:
            if resp.get(key):
                fail_msg = resp[key]
                break
        if fail_msg and len(fail_msg) > 200:
            fail_msg = fail_msg[:200] + "...(已截断)"
        logger.warning("%s 推送失败: %s", name, fail_msg)
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("%s 推送异常: %s", name, e)
        return False


def push_deer(key: str, title: str, content: str) -> bool:
    if not key:
        return False
    return _push_request(
        "PushDeer",
        "https://api2.pushdeer.com/message/push",
        json_payload={"pushkey": key, "text": f"{title}\n\n{content}", "type": "text"},
        success_check=lambda resp, r: r.ok and resp.get("code") == 0,
        fail_msg_keys=("message",),
    )


def push_serverchan(key: str, title: str, content: str) -> bool:
    if not key:
        return False
    return _push_request(
        "Server酱",
        f"https://sctapi.ftqq.com/{key}.send",
        data_payload={"title": title, "desp": content},
        success_check=lambda resp, r: r.ok and resp.get("code") == 0,
        fail_msg_keys=("message",),
    )


def push_telegram(bot_token: str, chat_id: str, title: str, content: str) -> bool:
    if not bot_token or not chat_id:
        return False
    text = f"{title}\n\n{content}"
    if len(text) > TELEGRAM_MAX_LENGTH:
        text = text[:TELEGRAM_TRUNCATE_LENGTH] + "\n..."
    return _push_request(
        "Telegram",
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json_payload={"chat_id": chat_id, "text": text},
        success_check=lambda resp, r: r.ok and resp.get("ok"),
        fail_msg_keys=("description",),
    )


def push_pushplus(token: str, title: str, content: str) -> bool:
    if not token:
        return False
    # PushPlus 用 html 模板时 \n 不会换行，必须转成 <br>，否则手机上所有内容挤成一行
    html = (content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\n", "<br>"))
    return _push_request(
        "PushPlus",
        "https://www.pushplus.plus/send",
        json_payload={"token": token, "title": title, "content": html, "template": "html"},
        success_check=lambda resp, r: r.ok and resp.get("code") == 200,
        fail_msg_keys=("msg",),
    )


def push_dingtalk(webhook_url: str, title: str, content: str) -> bool:
    if not webhook_url:
        return False
    secret = os.getenv("DINGTALK_SECRET", "")
    if secret:
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        separator = "&" if "?" in webhook_url else "?"
        webhook_url = f"{webhook_url}{separator}timestamp={timestamp}&sign={sign}"
    else:
        logger.warning("DINGTALK_WEBHOOK 已配置，但 DINGTALK_SECRET 缺失：将发送无签名请求（若机器人启用了加签校验会失败）")

    return _push_request(
        "钉钉机器人",
        webhook_url,
        json_payload={
            "msgtype": "markdown",
            "markdown": {"title": _escape_markdown(title), "text": f"### {_escape_markdown(title)}\n\n{_escape_markdown(content)}"},
        },
        headers={"Content-Type": "application/json"},
        success_check=lambda resp, r: r.ok and resp.get("errcode") == 0,
        fail_msg_keys=("errmsg",),
    )


def push_feishu(webhook_url: str, title: str, content: str) -> bool:
    if not webhook_url:
        return False
    data: Dict[str, Any] = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": _escape_markdown(title)}, "template": "blue"},
            "elements": [{"tag": "markdown", "content": _escape_markdown(content)}],
        },
    }
    secret = os.getenv("FEISHU_SECRET", "")
    if secret:
        timestamp = str(round(time.time()))
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256).digest()
        sign = base64.b64encode(hmac_code).decode("utf-8")
        data["timestamp"] = timestamp
        data["sign"] = sign
    else:
        logger.warning("FEISHU_WEBHOOK 已配置，但 FEISHU_SECRET 缺失：将发送无签名请求（若机器人启用了加签校验会失败）")

    return _push_request(
        "飞书机器人",
        webhook_url,
        json_payload=data,
        headers={"Content-Type": "application/json"},
        success_check=lambda resp, r: r.ok and resp.get("code") == 0,
        fail_msg_keys=("msg",),
    )


def push_wecom_bot(webhook_url: str, title: str, content: str) -> bool:
    if not webhook_url:
        return False
    return _push_request(
        "企业微信机器人",
        webhook_url,
        json_payload={
            "msgtype": "markdown",
            "markdown": {"content": f"### {_escape_markdown(title)}\n\n{_escape_markdown(content)}"},
        },
        headers={"Content-Type": "application/json"},
        success_check=lambda resp, r: r.ok and resp.get("errcode") == 0,
        fail_msg_keys=("errmsg",),
    )


def push_yunhu(token: str, recv_id: str, title: str, content: str) -> bool:
    if not token or not recv_id:
        return False
    recv_type = os.getenv("YUNHU_RECV_TYPE", "group")
    if recv_type not in ("group", "private"):
        logger.warning("YUNHU_RECV_TYPE 值 '%s' 非法，应为 'group' 或 'private'，使用默认值 'group'", recv_type)
        recv_type = "group"
    return _push_request(
        "云湖机器人",
        "https://chat-go.jwzhd.com/open-apis/v1/bot/send-message",
        json_payload={
            "token": token,
            "recvId": recv_id,
            "recvType": recv_type,
            "contentType": 1,
            "content": f"**{title}**\n\n{content}",
        },
        headers={"Content-Type": "application/json"},
        success_check=lambda resp, r: r.ok and resp.get("code") == 1,
        fail_msg_keys=("msg", "message"),
    )


# 推送渠道配置（数据驱动，便于扩展）： (渠道名, 触发所需的 env 变量列表, 推送调用闭包)
PUSH_CHANNELS: List[Tuple[str, List[str], Callable[[str, str], bool]]] = [
    ("PushDeer", ["SENDKEY"], lambda t, c: push_deer(os.getenv("SENDKEY", ""), t, c)),
    ("Server酱", ["SERVERCHAN_KEY"], lambda t, c: push_serverchan(os.getenv("SERVERCHAN_KEY", ""), t, c)),
    ("Telegram", ["TG_BOT_TOKEN", "TG_CHAT_ID"], lambda t, c: push_telegram(os.getenv("TG_BOT_TOKEN", ""), os.getenv("TG_CHAT_ID", ""), t, c)),
    ("PushPlus", ["PUSHPLUS_TOKEN"], lambda t, c: push_pushplus(os.getenv("PUSHPLUS_TOKEN", ""), t, c)),
    ("钉钉机器人", ["DINGTALK_WEBHOOK"], lambda t, c: push_dingtalk(os.getenv("DINGTALK_WEBHOOK", ""), t, c)),
    ("飞书机器人", ["FEISHU_WEBHOOK"], lambda t, c: push_feishu(os.getenv("FEISHU_WEBHOOK", ""), t, c)),
    ("企业微信机器人", ["WECOM_BOT_WEBHOOK"], lambda t, c: push_wecom_bot(os.getenv("WECOM_BOT_WEBHOOK", ""), t, c)),
    ("云湖机器人", ["YUNHU_TOKEN", "YUNHU_RECV_ID"], lambda t, c: push_yunhu(os.getenv("YUNHU_TOKEN", ""), os.getenv("YUNHU_RECV_ID", ""), t, c)),
]


def push_all(title: str, content: str) -> Tuple[int, int]:
    """推送到所有已配置渠道，返回 (成功数, 已配置数)。"""
    results: List[Tuple[str, bool]] = []
    for name, env_vars, fn in PUSH_CHANNELS:
        if all(os.getenv(v, "").strip() for v in env_vars):
            try:
                ok_push = fn(title, content)
            except Exception as e:  # noqa: BLE001
                logger.warning("%s 推送异常: %s", name, e)
                ok_push = False
            results.append((name, bool(ok_push)))

    configured = [n for n, _ in results]
    success = sum(1 for _, ok_push in results if ok_push)
    if not configured:
        logger.warning("未配置任何推送服务，请在 Secrets 中设置至少一种推送渠道")
    else:
        logger.info("已推送至: %s（成功 %d/%d）", ", ".join(configured), success, len(configured))
    return success, len(configured)


# ===================== 签到客户端 =====================
class CheckinClient:
    """单个会话内完成：域名探测锁定 → 签到 → 状态 → 积分 → 兑换。"""

    def __init__(self, domains: List[str], exchange_plan: Optional[str]):
        self.domains = domains
        self.exchange_plan = exchange_plan
        self.session = requests.Session()
        self.ua_cache: Dict[str, str] = {}

    def _headers(self, domain: str, cookie: str, ua: Optional[str] = None) -> Dict[str, str]:
        return {
            "origin": f"https://{domain}",
            "referer": f"https://{domain}/console/checkin",
            "user-agent": ua or self.ua_cache.get(domain) or PLATFORM_UA["Windows"],
            "cookie": cookie,
        }

    def _get_json(self, domain: str, path: str, cookie: str, ua: Optional[str] = None) -> Optional[Dict]:
        try:
            r = self.session.get(f"https://{domain}{path}", headers=self._headers(domain, cookie, ua), timeout=TIMEOUT)
            if not r.ok:
                return None
            return r.json()
        except Exception:
            return None

    def _resolve_domain(self, cookie: str) -> Optional[str]:
        """探测首个可用域名：status 返回已登录态即锁定。"""
        for domain in self.domains:
            raw = self._get_json(domain, STATUS_PATH, cookie)
            if raw is None:
                continue
            code = raw.get("code")
            data = raw.get("data") or {}
            msg = (raw.get("message") or "").lower()
            # 鉴权失败说明该域名不认这个 cookie —— 跳过，试下一个候选
            if any(kw in msg for kw in AUTH_FAIL_KEYWORDS):
                continue
            if code == 0 or "leftDays" in data:
                logger.info("账号会话前缀 %s，域名锁定: %s", extract_session_prefix(cookie), domain)
                return domain
        return None

    def _do_checkin(self, domain: str, cookie: str) -> Dict:
        """执行签到，含设备平台自适应重试（code 4 → 切 UA 重试）。"""
        url = f"https://{domain}{CHECKIN_PATH}"
        start_ua = self.ua_cache.get(domain) or PLATFORM_UA["Windows"]
        raw: Optional[Dict] = None
        for _ in range(2):
            h = self._headers(domain, cookie, start_ua)
            try:
                r = self.session.post(url, headers=h, data={"token": domain}, timeout=TIMEOUT)
                if not r.ok:
                    return {"code": -2, "message": f"HTTP {r.status_code}"}
                raw = r.json()
            except Exception as e:
                return {"code": -2, "message": f"网络错误: {e}"}
            code = raw.get("code")
            if code == 4 and raw.get("reason") == "device-mismatch":
                ld = raw.get("loginDevice")
                if ld in PLATFORM_UA and PLATFORM_UA[ld] != start_ua:
                    start_ua = PLATFORM_UA[ld]
                    self.ua_cache[domain] = start_ua
                    logger.warning("设备平台不匹配(loginDevice=%s)，切换 UA 重试", ld)
                    continue
            break
        return raw or {"code": -2, "message": "无响应"}

    def run_account(self, cookie: str, index: int) -> Dict:
        result = {
            "index": index, "email": "unknown", "domain": "-",
            "status": "", "result": "fail",
            "earned": None,
            "attempts": 1,
            "total_points": "-", "remaining_days": "-", "exchange": "-",
        }

        # 0. Cookie 格式预校验
        ok, err = validate_cookie(cookie)
        if not ok:
            result["status"] = f"❌ Cookie格式异常({err})"
            return result

        # 1. 域名自适应探测
        domain = self._resolve_domain(cookie)
        if not domain:
            result["status"] = "❌ 鉴权失败 → 请重新获取 Cookie 或检查 DOMAINS"
            logger.warning("账号 %d 所有候选域名均鉴权失败: %s", index, mask_cookie(cookie))
            return result
        result["domain"] = domain

        # 2. 签到（失败自动重试一次；鉴权失败重试无意义，直接跳过重试）
        raw = self._do_checkin(domain, cookie)
        code = raw.get("code", -2)
        message = raw.get("message", "")
        cls = classify_checkin(code, message)
        attempt = 1
        while (cls == "fail" and not is_auth_fail(message)
               and attempt < CHECKIN_MAX_ATTEMPTS):
            attempt += 1
            logger.warning("账号 %d 签到失败(code=%s, message=%s)，%d 秒后重试（第 %d/%d 次）",
                           index, code, message, CHECKIN_RETRY_DELAY, attempt, CHECKIN_MAX_ATTEMPTS)
            time.sleep(CHECKIN_RETRY_DELAY)
            raw = self._do_checkin(domain, cookie)
            code = raw.get("code", -2)
            message = raw.get("message", "")
            cls = classify_checkin(code, message)

        earned = parse_earned_points(raw)
        result["earned"] = earned
        result["attempts"] = attempt
        # 记录签到原始响应：便于排查「积分未显示 / 接口文案变动」类问题（message 无隐私字段）
        logger.info("账号 %d 签到响应: code=%s, points=%s, message=%s（共尝试 %d 次）",
                    index, code, raw.get("points"), message, attempt)
        retry_tag = "（重试后成功）" if (attempt > 1 and cls == "ok") else ""
        if cls == "ok":
            result["status"] = f"✅ 成功{retry_tag}"
            result["result"] = "ok"
        elif cls == "repeat":
            result["status"] = "🔄 已签到"
            result["result"] = "repeat"
        else:
            if is_auth_fail(message):
                result["status"] = f"❌ 鉴权失败({message}) → 请重新获取 Cookie"
            elif attempt > 1:
                result["status"] = f"❌ 失败({message}) → 已重试 {attempt} 次仍失败"
            else:
                result["status"] = f"❌ 失败({message})"
            result["result"] = "fail"

        # 3. 状态（剩余天数 / 邮箱）
        try:
            s = self._get_json(domain, STATUS_PATH, cookie)
            if s:
                data = s.get("data") or {}
                result["email"] = mask_email(data.get("email", "unknown"))
                if data.get("leftDays") is not None:
                    result["remaining_days"] = f"{safe_int_str(data['leftDays'])} 天"
        except Exception as e:
            logger.warning("账号 %d 状态查询失败: %s", index, e)

        # 4. 积分
        total_int: Optional[int] = None
        try:
            p = self._get_json(domain, POINTS_PATH, cookie)
            if p:
                pts = p.get("points")
                if pts is None:
                    pts = (p.get("data") or {}).get("points")
                if pts is not None:
                    result["total_points"] = f"{safe_int_str(pts)} 积分"
                    try:
                        total_int = int(float(pts))
                    except (TypeError, ValueError):
                        total_int = None
        except Exception as e:
            logger.warning("账号 %d 积分查询失败: %s", index, e)

        # 5. 兑换（仅配置了且积分达标才执行）
        if self.exchange_plan and self.exchange_plan in EXCHANGE_PLANS:
            req_pts = EXCHANGE_PLANS[self.exchange_plan]["points"]
            days_gain = EXCHANGE_PLANS[self.exchange_plan]["days"]
            if total_int is None:
                result["exchange"] = "⚠️ 兑换跳过(积分查询失败)"
            elif total_int < req_pts:
                result["exchange"] = f"⏭️ 积分不足({total_int}/{req_pts})"
            else:
                try:
                    r = self.session.post(
                        f"https://{domain}{EXCHANGE_PATH}",
                        headers=self._headers(domain, cookie),
                        data={"planType": self.exchange_plan},
                        timeout=TIMEOUT,
                    )
                    ex = r.json() if r.ok else {}
                    if ex.get("code") == 0:
                        result["exchange"] = f"🎁 兑换成功(+{days_gain}天)"
                    else:
                        result["exchange"] = f"⚠️ 兑换失败({ex.get('message', '')})"
                except Exception as e:
                    result["exchange"] = f"⚠️ 兑换异常({type(e).__name__})"

        return result


# ===================== 主流程 =====================
def main() -> int:
    raw_cookies = os.getenv("COOKIES") or os.getenv("GLADOS_COOKIES") or ""
    cookies = [c for c in (normalize_cookie(x) for x in re.split(r"\|\|\||[&\n]", raw_cookies)) if c]

    domains = [d.strip() for d in (os.getenv("DOMAINS") or "").split(",") if d.strip()] or list(DEFAULT_DOMAINS)

    raw_plan = (os.getenv("EXCHANGE_PLAN") or os.getenv("GLADOS_EXCHANGE_PLAN") or "").strip()
    exchange_plan = raw_plan if raw_plan in EXCHANGE_PLANS else None
    if raw_plan and not exchange_plan:
        logger.warning("EXCHANGE_PLAN 值 '%s' 无效，可选: %s；本次跳过兑换", raw_plan, "/".join(EXCHANGE_PLANS.keys()))

    if not cookies:
        push_all("GLaDOS 签到", "❌ 未检测到 COOKIES / GLADOS_COOKIES，请配置 GitHub Secrets")
        return 1

    logger.info("%s 检测到 %d 个账号，候选域名: %s", VERSION, len(cookies), ", ".join(domains))
    client = CheckinClient(domains, exchange_plan)
    ok = fail = repeat = 0
    results: List[Dict[str, Any]] = []

    for idx, cookie in enumerate(cookies, 1):
        acc = client.run_account(cookie, idx)
        results.append(acc)
        if acc["result"] == "ok":
            ok += 1
        elif acc["result"] == "repeat":
            repeat += 1
        else:
            fail += 1
        if idx < len(cookies):
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    # 组装「手机友好」排版：每个账号独立成块、逐行展示，避免长行在窄屏上换行挤成一团
    sep = "─" * 16
    lines: List[str] = [
        f"🕘 {time.strftime('%Y-%m-%d %H:%M')}",
        f"✅ 成功 {ok} · 🔄 已签到 {repeat} · ❌ 失败 {fail}",
    ]
    for acc in results:
        lines.append(sep)
        lines.append(f"{acc['index']}. {acc['email']}")
        status = acc["status"]
        earned = acc.get("earned")
        # 「今日签到获得的积分」：
        #   - 签到成功 → 显示本轮实际获得的 +N
        #   - 已签到   → 接口只回 points=0（不带当日积分），故明确标注「今日已领」，避免误解为今天没得分
        if acc["result"] == "ok":
            status += f" · 今日 +{earned if earned is not None else 0} 积分"
        elif acc["result"] == "repeat":
            status += f" · 今日 +{earned} 积分" if earned else " · 今日已领"
        lines.append(f"   {status} · 剩余 {acc['remaining_days']} · 总计 {acc['total_points']}")
        tail = f"   🌐 {acc['domain']}"
        if acc.get("exchange") and acc["exchange"] != "-":
            tail += f" · {acc['exchange']}"
        lines.append(tail)

    title = f"📋 签到完成 ✅{ok} 🔄{repeat} ❌{fail}"
    content = "\n".join(lines)
    if len(content) > CONTENT_MAX_LENGTH:
        content = content[:CONTENT_MAX_LENGTH] + "\n…(已截断)"

    logger.info("%s", "=" * 50)
    logger.info("%s", content)
    logger.info("%s", "=" * 50)

    # 通知抑制：定时任务一天会触发多次（主运行 + 兜底），若本轮所有账号均为「已签到」，
    # 说明当天签到早已完成、本轮没有任何新事件 —— 跳过推送，避免重复通知刷屏。
    # 手动触发时 workflow 会注入 NOTIFY_ALWAYS=true，此时始终推送（便于预览/排查）。
    notify_always = os.getenv("NOTIFY_ALWAYS", "").strip().lower() in ("1", "true", "yes", "on")
    noop_run = (ok == 0 and fail == 0 and repeat > 0)
    if notify_always:
        logger.info("已启用强制推送（NOTIFY_ALWAYS=true）：本轮始终推送通知")
    if noop_run and not notify_always:
        logger.info("本轮无新签到事件（%d 个账号均已签到），按策略跳过推送；如需强制推送请设置 NOTIFY_ALWAYS=true", repeat)
        pushed_ok, pushed_cfg = 0, 0
    else:
        pushed_ok, pushed_cfg = push_all(title, content)

    # 区分「业务失败」与「通知失败」：全部账号失败 → 退出码 1（CI 标红）
    if ok == 0 and repeat == 0 and len(cookies) > 0:
        if pushed_cfg > 0 and pushed_ok == 0:
            logger.error("⚠️ 全部 %d 个账号签到失败，且已配置推送渠道但全部发送失败", len(cookies))
        return 1
    if pushed_cfg > 0 and pushed_ok == 0:
        logger.warning("⚠️ 已配置推送渠道但全部发送失败（不影响运行结果）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
