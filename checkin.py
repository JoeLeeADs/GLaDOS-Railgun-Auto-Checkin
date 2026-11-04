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
  - 积分兑换阈值保护（总积分够才兑，不浪费）+ 邮箱白名单（EXCHANGE_EMAILS 只兑指定账号）
  - 签到失败自动重试（网络/HTTP/未知失败各重试一次；鉴权失败不重试）
  - 定时兜底：一天可触发多次，当天已签到则静默跳过推送，不重复刷屏
  - 网络重试 + 退出码区分「业务失败」与「通知失败」
  - LifeLoom 上报（双写）：原有通知不变，另把本轮全量结构化结果 POST 给 LifeLoom 签到聚合页

仅依赖 requests，可直接跑在 GitHub Actions。
"""
import os
import re
import sys
import json
import time
import random
import hashlib
import hmac
import base64
import urllib.parse
import logging
from datetime import datetime, timedelta
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

VERSION = "v1.5.0"

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

# ---- LifeLoom 上报（家庭签到聚合页，双写出口）----
# 地址走环境变量 LIFELOOM_HOOK_URL，不写死在代码里；未配置则整段逻辑自动跳过。
LIFELOOM_MAX_BYTES = 32 * 1024   # 接口请求体上限 32KB，超了会 413
LIFELOOM_MAX_RETRY = 2           # 失败后最多再重试 2 次（总尝试 3 次）
LIFELOOM_RETRY_DELAY = 30        # 每次重试前等待秒数（≥30s，且是上限为 1 分钟的温和频率）
REPORT_SOURCE = "glados-railgun-checkin"

# 平台 status 接口字段名各版本不统一，按候选名（大小写不敏感）尽力抽取；抽不到就不给该字段
# 到期时间：只认「确实是时间」的值（布尔/普通数字会被丢弃，避免把 expired:false 当成日期）
PLAN_EXPIRE_KEYS = (
    "expiredat", "expireat", "expiretime", "expires", "expiredate", "expired",
    "endtime", "planexpire", "vipexpireat", "vip_expire_at", "expire_at",
)
# 连续签到天数：实测 status 接口给的 `days` 更像「套餐总天数」而非连续签到，
# 误映射会把错数据灌给下游，故不收录 `days`；平台没给就整个字段不给。
STREAK_KEYS = (
    "checkincount", "continuousdays", "continuouscheckindays", "checkinstreak",
    "streak", "continuous_count", "checkindays",
)
# 推算到期日用的系统日期字段（平台只给 leftDays 时，用它加天数得出一个大致的到期日）
SYSTEM_DATE_KEYS = ("system_date", "systemdate", "date", "today")


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


# ===================== LifeLoom 上报 =====================
# 双写出口：原有 8 路通知完全不动，另把「本轮全量结构化结果」POST 给 LifeLoom。
# 硬约束：上报任何环节异常都在本段内吞掉，绝不影响签到主流程与退出码。

def now_iso() -> str:
    """当前时间 ISO8601（带时区偏移；脚本已强制 Asia/Shanghai，故为 +08:00）。"""
    try:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001
        return time.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def platform_of(domain: str) -> str:
    """按域名判断平台：railgun.* → Railgun，其余 → GLaDOS（同一套 API，仅品牌域名不同）。"""
    return "Railgun" if (domain or "").startswith("railgun") else "GLaDOS"


def _pick_field(data: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    """在 dict 中按候选键名（大小写不敏感）取第一个非空值，取不到返回 None。"""
    if not isinstance(data, dict):
        return None
    lowered = {str(k).lower(): v for k, v in data.items()}
    for key in keys:
        val = lowered.get(key)
        if val not in (None, "", [], {}):
            return val
    return None


def _as_time_string(val: Any) -> Optional[str]:
    """把值规整成时间字符串；明显不是时间的（布尔/短数字/纯文本）返回 None。

    平台可能返回 `expired: false` 这类布尔标志，绝不能当成到期时间，故做类型校验。
    """
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        ts = float(val)
        if ts > 1e12:          # 毫秒时间戳
            ts /= 1000
        if 1e9 <= ts <= 4e9:   # 2001~2096 年之间才认为可信
            try:
                return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
            except Exception:  # noqa: BLE001
                return None
        return None
    s = str(val).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{13}", s) or re.fullmatch(r"\d{10}", s):
        return _as_time_string(int(s))
    # 形如 2026-12-04 / 2026-12-04T00:00:00Z / 2026/12/04 的才认，其余（如 "false"）丢弃
    if re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", s):
        return s
    return None


def _pick_time_field(data: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[str]:
    """按候选键名取第一个「确实是时间」的值。"""
    if not isinstance(data, dict):
        return None
    lowered = {str(k).lower(): v for k, v in data.items()}
    for key in keys:
        parsed = _as_time_string(lowered.get(key))
        if parsed:
            return parsed
    return None


def _estimate_expire(data: Dict[str, Any], left_days: Optional[int]) -> Optional[str]:
    """由 status 的 system_date + leftDays 推算到期日（日期粒度，仅作参考）。

    平台通常只给「剩余天数」不给「到期时间」，这个字段就是把它换算成一个便于展示的日期。
    """
    if left_days is None:
        return None
    sys_date = _pick_field(data, SYSTEM_DATE_KEYS)
    if not sys_date:
        return None
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", str(sys_date).strip())
    if not m:
        return None
    try:
        base = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return (base + timedelta(days=int(left_days))).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _clean_platform_status(data: Dict[str, Any]) -> Dict[str, Any]:
    """原样透传平台 status 接口的 data，仅剔除疑似凭据字段。

    原则是「宁可多给」：不做无谓裁剪，字段取舍交给 LifeLoom 侧。
    """
    if not isinstance(data, dict):
        return {}
    blocked = ("token", "cookie", "secret", "password", "passwd", "session", "authorization")
    return {k: v for k, v in data.items() if not any(b in str(k).lower() for b in blocked)}


def _prune_empty(obj: Any) -> Any:
    """递归删掉 None / 空串 / 空容器：不存在的字段整个不给，也不拿 0 或 null 占位。"""
    if isinstance(obj, dict):
        return {k: _prune_empty(v) for k, v in obj.items()
                if v is not None and v != "" and v != [] and v != {}}
    if isinstance(obj, list):
        return [_prune_empty(x) for x in obj]
    return obj


def _trigger_name() -> str:
    """本次运行来自哪里：schedule / manual / local。"""
    event = (os.getenv("GITHUB_EVENT_NAME") or "").strip()
    if event == "workflow_dispatch":
        return "manual"
    if event == "schedule":
        return "schedule"
    return event or "local"


def build_lifeloom_payload(results: List[Dict[str, Any]], summary: Dict[str, int],
                           notify_title: str, notify_text: str,
                           reported_at: Optional[str] = None) -> Dict[str, Any]:
    """把一轮签到结果组装成上报体（字段尽量给全，取不到的留空由 _prune_empty 剔除）。"""
    accounts: List[Dict[str, Any]] = []
    for acc in results:
        status_data = acc.get("status_data") or {}
        left_days = acc.get("remaining_days_int")
        item: Dict[str, Any] = {
            "index": acc.get("index"),
            # 账号标识：优先原始邮箱（稳定唯一）；拿不到邮箱时才没有该字段
            "account": acc.get("email_raw") or None,
            "account_masked": acc.get("email") if acc.get("email") != "unknown" else None,
            "platform": platform_of(acc.get("domain", "")),
            "domain": None if acc.get("domain") in (None, "", "-") else acc.get("domain"),
            "checkin_result": {"ok": "success", "repeat": "already_checked_in",
                               "fail": "failed"}.get(acc.get("result"), "unknown"),
            "checkin_code": acc.get("code"),
            "checkin_message": acc.get("message") or None,
            "checked_at": acc.get("checked_at") or None,
            "total_points": acc.get("total_points_int"),
            "remaining_days": left_days,
            # 到期时间：平台真给了时间就用它；只给剩余天数时，用 system_date + leftDays 推算一个日期
            "plan_expires_at": _pick_time_field(status_data, PLAN_EXPIRE_KEYS),
            "plan_expires_estimated": _estimate_expire(status_data, left_days),
            "streak_days": _pick_field(status_data, STREAK_KEYS),
            "attempts": acc.get("attempts"),
            "exchange": acc.get("exchange_info"),
        }
        # 本次获得积分：成功时 0 也是真实值；「已签到」且解析不到数字时不填（不拿 0 占位）
        earned = acc.get("earned")
        if acc.get("result") == "ok" and earned is not None:
            item["earned_points"] = earned
        elif acc.get("result") == "repeat" and earned:
            item["earned_points"] = earned
        cleaned = _clean_platform_status(status_data)
        if cleaned:
            item["platform_status"] = cleaned
        accounts.append(item)

    payload = {
        "source": REPORT_SOURCE,
        "script_version": VERSION,
        "reported_at": reported_at or now_iso(),
        "timezone": "Asia/Shanghai",
        "trigger": _trigger_name(),
        "summary": {
            "accounts": len(results),
            "success": summary.get("ok", 0),
            "already_checked_in": summary.get("repeat", 0),
            "failed": summary.get("fail", 0),
        },
        "notification_title": notify_title,
        "notification_text": notify_text,
        "accounts": accounts,
    }
    return _prune_empty(payload)


def _encode_payload(payload: Dict[str, Any]) -> bytes:
    """紧凑序列化（中文不转义），字节数即实际上行体积。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _fit_payload(payload: Dict[str, Any]) -> Optional[bytes]:
    """把请求体压到 32KB 以内：先丢可再生的文本，再丢平台原始状态，最后才截断账号。"""
    body = _encode_payload(payload)
    if len(body) <= LIFELOOM_MAX_BYTES:
        return body

    if payload.pop("notification_text", None) is not None:
        body = _encode_payload(payload)
        logger.warning("LifeLoom 请求体超过 32KB，已丢弃 notification_text（现 %d 字节）", len(body))

    if len(body) > LIFELOOM_MAX_BYTES:
        for acc in payload.get("accounts", []):
            acc.pop("platform_status", None)
        body = _encode_payload(payload)
        logger.warning("LifeLoom 请求体仍超限，已丢弃 platform_status（现 %d 字节）", len(body))

    omitted = 0
    while len(body) > LIFELOOM_MAX_BYTES and len(payload.get("accounts", [])) > 1:
        payload["accounts"].pop()
        omitted += 1
        body = _encode_payload(payload)
    if omitted:
        payload["truncated"] = True
        payload["accounts_omitted"] = omitted
        body = _encode_payload(payload)
        logger.warning("LifeLoom 请求体仍超限，已省略 %d 个账号（保留前 %d 个）",
                       omitted, len(payload.get("accounts", [])))

    if len(body) > LIFELOOM_MAX_BYTES:
        logger.error("LifeLoom 请求体 %d 字节，压不到 32KB 以内，放弃本次上报", len(body))
        return None
    return body


def report_to_lifeloom(results: List[Dict[str, Any]], summary: Dict[str, int],
                       notify_title: str, notify_text: str) -> bool:
    """把本轮结果上报给 LifeLoom，返回是否成功（失败只记日志，绝不影响签到主流程）。

    响应约定（NestJS POST，成功是 201，没有 200）：
      201 收下了 / 413 太大（不重试）/ 429 发太快（按 Retry-After 或 30s 后再试）/ 其他 4xx 不重试
    """
    url = (os.getenv("LIFELOOM_HOOK_URL") or "").strip()
    if not url:
        logger.info("未配置 LIFELOOM_HOOK_URL，跳过 LifeLoom 上报")
        return False
    if not url.lower().startswith(("http://", "https://")):
        logger.warning("LIFELOOM_HOOK_URL 格式异常（需 http/https 开头），跳过 LifeLoom 上报")
        return False

    try:
        payload = build_lifeloom_payload(results, summary, notify_title, notify_text)
        body = _fit_payload(payload)
    except Exception as e:  # noqa: BLE001
        logger.warning("LifeLoom 上报数据组装异常（不影响签到主流程）: %s", e)
        return False
    if body is None:
        return False

    account_count = len(payload.get("accounts", []))
    logger.info("LifeLoom 上报开始：%d 个账号 / %d 字节", account_count, len(body))
    # 字段库存（只打键名，不打值）：便于日后核对「哪些可选字段本次真的带上了」
    field_names = sorted({k for acc in payload.get("accounts", []) for k in acc})
    status_keys = sorted({k for acc in payload.get("accounts", [])
                          for k in (acc.get("platform_status") or {})})
    logger.info("LifeLoom 字段: %s", ", ".join(field_names))
    logger.info("LifeLoom 平台状态字段: %s", ", ".join(status_keys) or "（无）")

    attempts = LIFELOOM_MAX_RETRY + 1
    for attempt in range(1, attempts + 1):
        wait = LIFELOOM_RETRY_DELAY
        try:
            resp = requests.post(url, data=body,
                                 headers={"Content-Type": "application/json"},
                                 timeout=TIMEOUT)
        except Exception as e:  # noqa: BLE001
            logger.warning("LifeLoom 上报异常（第 %d/%d 次）: %s", attempt, attempts, e)
        else:
            code = resp.status_code
            if code == 201:
                logger.info("LifeLoom 上报成功：HTTP 201，%d 个账号 / %d 字节，上报时间 %s",
                            account_count, len(body), payload.get("reported_at"))
                return True
            if code == 413:
                logger.error("LifeLoom 拒收：HTTP 413 请求体过大（%d 字节），不再重试", len(body))
                return False
            if code == 429:
                retry_after = (resp.headers.get("Retry-After") or "").strip()
                if retry_after.isdigit():
                    wait = max(LIFELOOM_RETRY_DELAY, int(retry_after))
                logger.warning("LifeLoom 限流：HTTP 429，%d 秒后重试", wait)
            elif 400 <= code < 500:
                detail = (resp.text or "")[:200]
                logger.error("LifeLoom 拒收：HTTP %d %s（4xx 不重试）", code, detail)
                return False
            else:
                logger.warning("LifeLoom 上报失败：HTTP %d %s（第 %d/%d 次）",
                               code, (resp.text or "")[:200], attempt, attempts)
        if attempt < attempts:
            time.sleep(wait)

    logger.error("LifeLoom 上报失败：已尝试 %d 次后放弃（不影响签到主流程）", attempts)
    return False


# ===================== 签到客户端 =====================
class CheckinClient:
    """单个会话内完成：域名探测锁定 → 签到 → 状态 → 积分 → 兑换。"""

    def __init__(self, domains: List[str], exchange_plan: Optional[str],
                 exchange_emails: Optional[set] = None):
        self.domains = domains
        self.exchange_plan = exchange_plan
        # 兑换邮箱白名单（小写）：非空时只对名单内的账号兑换，其余账号一律不兑
        self.exchange_emails = exchange_emails or set()
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
            "index": index, "email": "unknown", "email_raw": "", "domain": "-",
            "status": "", "result": "fail",
            "earned": None,
            "attempts": 1,
            # 以下字段供 LifeLoom 上报使用（结构化原始值，通知排版不受影响）
            "code": None, "message": "", "checked_at": "",
            "status_data": {}, "exchange_info": None,
            "total_points": "-", "remaining_days": "-", "exchange": "-",
            "total_points_int": None, "remaining_days_int": None,
        }

        # 0. Cookie 格式预校验
        ok, err = validate_cookie(cookie)
        if not ok:
            result["status"] = f"❌ Cookie格式异常({err})"
            result["message"] = err
            return result

        # 1. 域名自适应探测
        domain = self._resolve_domain(cookie)
        if not domain:
            result["status"] = "❌ 鉴权失败 → 请重新获取 Cookie 或检查 DOMAINS"
            result["message"] = "所有候选域名均鉴权失败（Cookie 失效或不适用于这些域名）"
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
        # 结构化留档（上报用）：原始 code / message / 完成时间
        result["code"] = code
        result["message"] = str(message or "")
        result["checked_at"] = now_iso()
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
                if isinstance(data, dict):
                    result["status_data"] = data
                # 保留原始邮箱（仅内部使用，用于兑换白名单匹配；通知里只输出脱敏值）
                raw_email = (data.get("email") or "").strip()
                result["email_raw"] = raw_email
                result["email"] = mask_email(raw_email or "unknown")
                if data.get("leftDays") is not None:
                    result["remaining_days"] = f"{safe_int_str(data['leftDays'])} 天"
                    try:
                        result["remaining_days_int"] = int(float(data["leftDays"]))
                    except (TypeError, ValueError):
                        pass
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
                    result["total_points_int"] = total_int
        except Exception as e:
            logger.warning("账号 %d 积分查询失败: %s", index, e)

        # 5. 兑换（仅配置了且积分达标才执行；配置了邮箱白名单时，只兑名单内的账号）
        if self.exchange_plan and self.exchange_plan in EXCHANGE_PLANS:
            # 白名单保护：未取到邮箱（状态查询失败）时一律视为「不在名单内」，宁可不兑也不误兑
            target_email = (result["email_raw"] or "").strip().lower()
            if self.exchange_emails and target_email not in self.exchange_emails:
                logger.info("账号 %d(%s) 不在兑换白名单内，跳过兑换",
                            index, result["email"] or "unknown")
                result["exchange"] = "-"
                result["exchange_info"] = {"status": "skipped", "reason": "not_in_whitelist"}
            else:
                req_pts = EXCHANGE_PLANS[self.exchange_plan]["points"]
                days_gain = EXCHANGE_PLANS[self.exchange_plan]["days"]
                if total_int is None:
                    result["exchange"] = "⚠️ 兑换跳过(积分查询失败)"
                    result["exchange_info"] = {"status": "skipped", "reason": "points_query_failed",
                                               "plan": self.exchange_plan,
                                               "cost_points": req_pts, "gained_days": days_gain}
                elif total_int < req_pts:
                    result["exchange"] = f"⏭️ 积分不足({total_int}/{req_pts})"
                    result["exchange_info"] = {"status": "insufficient_points",
                                               "plan": self.exchange_plan,
                                               "cost_points": req_pts, "gained_days": days_gain,
                                               "available_points": total_int}
                else:
                    days_before = result["remaining_days"]
                    days_before_int = result["remaining_days_int"]
                    try:
                        r = self.session.post(
                            f"https://{domain}{EXCHANGE_PATH}",
                            headers=self._headers(domain, cookie),
                            data={"planType": self.exchange_plan},
                            timeout=TIMEOUT,
                        )
                        ex = r.json() if r.ok else {}
                        if ex.get("code") == 0:
                            # 兑换成功后重查一次状态，确认天数真的到账（而不是只看接口说了"成功"）
                            days_after = "-"
                            days_after_int: Optional[int] = None
                            try:
                                s2 = self._get_json(domain, STATUS_PATH, cookie)
                                if s2:
                                    d2 = s2.get("data") or {}
                                    if d2.get("leftDays") is not None:
                                        days_after = f"{safe_int_str(d2['leftDays'])} 天"
                                        result["remaining_days"] = days_after
                                        try:
                                            days_after_int = int(float(d2["leftDays"]))
                                            result["remaining_days_int"] = days_after_int
                                        except (TypeError, ValueError):
                                            days_after_int = None
                            except Exception:
                                pass
                            # 积分已被扣减，一并刷新，避免通知里仍显示兑换前的总额
                            try:
                                p2 = self._get_json(domain, POINTS_PATH, cookie)
                                if p2:
                                    pts2 = p2.get("points")
                                    if pts2 is None:
                                        pts2 = (p2.get("data") or {}).get("points")
                                    if pts2 is not None:
                                        result["total_points"] = f"{safe_int_str(pts2)} 积分"
                                        try:
                                            result["total_points_int"] = int(float(pts2))
                                        except (TypeError, ValueError):
                                            pass
                            except Exception:
                                pass
                            result["exchange"] = (f"🎁 兑换成功(+{days_gain}天/-{req_pts}分)"
                                                  f" 剩余 {days_before}→{days_after}")
                            result["exchange_info"] = {
                                "status": "success", "plan": self.exchange_plan,
                                "cost_points": req_pts, "gained_days": days_gain,
                                "remaining_before": days_before_int,
                                "remaining_after": days_after_int,
                                "total_points_after": result["total_points_int"],
                            }
                            logger.info("账号 %d 兑换成功：%s → %s（消耗 %d 积分）",
                                        index, days_before, days_after, req_pts)
                        else:
                            result["exchange"] = f"⚠️ 兑换失败({ex.get('message', '')})"
                            result["exchange_info"] = {"status": "failed", "plan": self.exchange_plan,
                                                       "cost_points": req_pts, "gained_days": days_gain,
                                                       "code": ex.get("code"),
                                                       "message": ex.get("message")}
                            logger.warning("账号 %d 兑换失败: code=%s, message=%s",
                                           index, ex.get("code"), ex.get("message"))
                    except Exception as e:
                        # 兑换是非幂等操作，异常不重试（避免重复扣积分），仅如实报告
                        result["exchange"] = f"⚠️ 兑换异常({type(e).__name__})"
                        result["exchange_info"] = {"status": "error", "plan": self.exchange_plan,
                                                   "cost_points": req_pts, "gained_days": days_gain,
                                                   "error": type(e).__name__}
                        logger.warning("账号 %d 兑换请求异常（不重试，避免重复扣分）: %s", index, e)

        return result


# ===================== 主流程 =====================
def main() -> int:
    raw_cookies = os.getenv("COOKIES") or os.getenv("GLADOS_COOKIES") or ""
    cookies = [c for c in (normalize_cookie(x) for x in re.split(r"\|\|\||[&\n]", raw_cookies)) if c]

    domains = [d.strip() for d in (os.getenv("DOMAINS") or "").split(",") if d.strip()] or list(DEFAULT_DOMAINS)

    raw_plan = (os.getenv("EXCHANGE_PLAN") or os.getenv("GLADOS_EXCHANGE_PLAN") or "").strip()
    if raw_plan.lower() in ("none", "no", "-"):
        raw_plan = ""   # 手动触发时选择「不兑换」的占位值
    exchange_plan = raw_plan if raw_plan in EXCHANGE_PLANS else None
    if raw_plan and not exchange_plan:
        logger.warning("EXCHANGE_PLAN 值 '%s' 无效，可选: %s；本次跳过兑换", raw_plan, "/".join(EXCHANGE_PLANS.keys()))

    # 兑换邮箱白名单：非空时只对名单内账号兑换（逗号/分号/空白分隔，大小写不敏感）
    raw_emails = (os.getenv("EXCHANGE_EMAILS") or "").strip()
    exchange_emails = {e.strip().lower() for e in re.split(r"[,;，；\s]+", raw_emails) if e.strip()}
    if exchange_emails:
        logger.info("兑换白名单已启用：%s（名单外账号一律不兑换）", ", ".join(sorted(exchange_emails)))

    if not cookies:
        push_all("GLaDOS 签到", "❌ 未检测到 COOKIES / GLADOS_COOKIES，请配置 GitHub Secrets")
        return 1

    logger.info("%s 检测到 %d 个账号，候选域名: %s", VERSION, len(cookies), ", ".join(domains))
    client = CheckinClient(domains, exchange_plan, exchange_emails)
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
    suppressed = bool(noop_run and not notify_always)
    if suppressed:
        logger.info("本轮无新签到事件（%d 个账号均已签到），按策略跳过推送；如需强制推送请设置 NOTIFY_ALWAYS=true", repeat)
        pushed_ok, pushed_cfg = 0, 0
    else:
        pushed_ok, pushed_cfg = push_all(title, content)

    # LifeLoom 上报（双写出口）：与通知同频 —— 被抑制的「无新事件」轮次一并跳过，
    # 避免同一天给聚合页重复灌同一条数据；手动触发（always）时必定上报。
    # 整段 try/except 包住：LifeLoom 挂了绝不影响签到主流程与退出码。
    if suppressed:
        logger.info("本轮通知被抑制，LifeLoom 上报同步跳过")
    else:
        try:
            report_to_lifeloom(results, {"ok": ok, "repeat": repeat, "fail": fail}, title, content)
        except Exception as e:  # noqa: BLE001
            logger.warning("LifeLoom 上报异常（不影响签到主流程）: %s", e)

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
