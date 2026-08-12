"""DiceFrame Bridge — language and presentation helpers.

纯函数集合：语言判定、本地化文本、检定结果展示与命令意图识别。
从原 `plugin.py` 拆出，只依赖标准库，便于单独测试。
"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_LANGUAGE = "zh-CN"


def normalize_language(value: object) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    if text in {"en", "en-us", "en-gb", "english"}:
        return "en"
    return DEFAULT_LANGUAGE


def is_english(value: object) -> bool:
    return normalize_language(value) == "en"


def localized_text(language: object, zh: str, en: str, **values: object) -> str:
    return (en if is_english(language) else zh).format(**values)


def format_check_result(check: dict[str, Any], language: object = DEFAULT_LANGUAGE) -> str:
    """Render a server-resolved check without asking the player to roll again."""
    english = is_english(language)
    actor = str(check.get("actor_name") or check.get("actor_uid") or ("Character" if english else "角色"))
    label = str(check.get("label") or ("Check" if english else "检定"))
    verdict_raw = str(check.get("verdict") or "")
    verdict_map = {
        "大成功": "Critical Success",
        "极难成功": "Extreme Success",
        "困难成功": "Hard Success",
        "普通成功": "Regular Success",
        "成功": "Success",
        "失败": "Failure",
        "大失败": "Critical Failure",
    }
    verdict = verdict_map.get(verdict_raw, verdict_raw) if english else verdict_raw
    dice = str(check.get("dice") or "d20")
    if dice == "d100":
        math = f"d100={check.get('roll')} vs {check.get('threshold')}%"
    else:
        modifier = int(check.get("modifier", 0) or 0)
        if check.get("opponent_name"):
            opponent_modifier = int(check.get("opponent_modifier", 0) or 0)
            math = (
                f"d20={check.get('roll')}({modifier:+d})={check.get('total')} vs "
                f"{check.get('opponent_name')} d20={check.get('opponent_roll')}"
                f"({opponent_modifier:+d})={check.get('opponent_total')}"
            )
        else:
            math = f"d20={check.get('roll')}({modifier:+d})={check.get('total')} vs DC {check.get('dc')}"
    return f"🎲 {actor} · {label} {math} → {verdict}"


def infer_command_language(text: object, fallback: str = DEFAULT_LANGUAGE) -> str:
    """Infer language only from explicit command words, not story actions."""
    value = re.sub(r"\s+", " ", str(text or "").strip().lower())
    first = value.split(" ", 1)[0] if value else ""
    if first in {
        "help", "bind", "unbind", "join", "invite", "status", "recap", "summary",
        "map", "roll", "advance", "next", "pay", "payment", "payments", "confirm",
        "reject", "sense", "perception", "log", "away", "return", "back", "ping",
        "character", "create",
        "luck", "spend", "no",
    } or value in {"ai character", "ai character generation"}:
        return "en"
    return normalize_language(fallback)


def payment_decision(text: object) -> bool | None:
    normalized = re.sub(r"\s+", "", str(text or "").strip().lower())
    if normalized.startswith((
        "确认支付", "同意支付", "确认付款", "同意付款",
        "confirmpay", "confirmpayment", "acceptpay", "acceptpayment",
    )):
        return True
    if normalized.startswith((
        "拒绝支付", "取消支付", "拒绝付款", "取消付款",
        "rejectpay", "rejectpayment", "declinepay", "declinepayment",
    )):
        return False
    return None


def luck_decision(text: object) -> bool | None:
    normalized = re.sub(r"\s+", "", str(text or "").strip().lower())
    if normalized in {
        "幸运", "用幸运", "使用幸运", "消耗幸运", "花幸运",
        "luck", "useluck", "spendluck",
    } or normalized.startswith(("用幸运", "使用幸运", "消耗幸运", "花幸运", "useluck", "spendluck")):
        return True
    if normalized in {
        "不用幸运", "不使用幸运", "保留失败", "接受失败", "放弃幸运",
        "noluck", "declineluck", "keepfailure", "acceptfailure",
    } or normalized.startswith(("不用幸运", "不使用幸运", "保留失败", "接受失败", "放弃幸运", "declineluck", "keepfailure")):
        return False
    return None


def decision_index(text: object) -> int:
    match = re.search(r"(\d+)", str(text or ""))
    return max(1, int(match.group(1))) if match else 1


def localized_error(error: object, language: object) -> str:
    text = str(error or "").strip()
    if not is_english(language):
        return text
    known = {
        "未配置 DiceFrame 服务地址": "DiceFrame service URL is not configured.",
        "未配置 DiceFrame Bot API Token；请到 DiceFrame 设置 → Bot API 复制": (
            "DiceFrame Bot API Token is not configured. Copy it from DiceFrame Settings → Bot API."
        ),
        "游戏不存在": "Game not found.",
        "绑定凭证无效或已使用，请由 GM 在网页重新生成一次性绑定命令": (
            "The binding token is invalid or has already been used. The GM must generate a new one-time binding command."
        ),
        "Bot 服务未授权": "Bot service authentication failed.",
        "DiceFrame 请求失败": "DiceFrame request failed.",
    }
    if text in known:
        return known[text]
    prefix = "DiceFrame 返回了非 JSON 响应："
    if text.startswith(prefix):
        return "DiceFrame returned a non-JSON response: " + text[len(prefix):]
    return text
