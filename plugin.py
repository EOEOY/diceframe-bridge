"""MaiBot plugin that bridges chat commands to a DiceFrame web service."""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from urllib.parse import quote, urlencode, urlparse, urlunparse

import aiohttp

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase


class PluginSectionConfig(PluginConfigBase):
    """Plugin switch."""

    __ui_label__: ClassVar[str] = "插件"
    __ui_icon__: ClassVar[str] = "package"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(default=False, description="是否启用 DiceFrame 桥接")
    config_version: str = Field(default="0.1.0", description="配置版本")


class DiceFrameServiceConfig(PluginConfigBase):
    """DiceFrame HTTP service settings."""

    __ui_label__: ClassVar[str] = "DiceFrame 服务"
    __ui_icon__: ClassVar[str] = "network"
    __ui_order__: ClassVar[int] = 1

    base_url: str = Field(default="http://127.0.0.1:18000", description="DiceFrame Web 服务地址")
    bot_token: str = Field(
        default="",
        description="DiceFrame Bot API Token；在 DiceFrame 设置 → Bot API 中显示并复制",
        json_schema_extra={"x-widget": "password", "x-icon": "key"},
    )
    public_base_url: str = Field(default="", description="可选，发给玩家的网页入口地址")
    request_timeout_sec: float = Field(default=120, description="HTTP 请求超时时间")
    shared_core_path: str = Field(default="", description="可选，DiceFrame 源码根目录；填写后优先复用 src.bots.bridge_core")


class CommandConfig(PluginConfigBase):
    """Command parsing settings."""

    __ui_label__: ClassVar[str] = "命令"
    __ui_icon__: ClassVar[str] = "terminal"
    __ui_order__: ClassVar[int] = 2

    prefixes: list[str] = Field(default_factory=lambda: ["/df", "/diceframe", "跑团"], description="命令前缀")
    trigger_mode: str = Field(default="prefix_only", description="触发模式：prefix_only / mention_bare / prefix_or_mention / bare")
    allow_mentioned_bare_commands: bool = Field(default=False, description="兼容旧用法：允许 @MaiBot 后发送裸 DiceFrame 指令")
    require_mention_for_bare_commands: bool = Field(default=True, description="旧兼容项：裸指令是否必须来自 @/提到 MaiBot 的消息")
    command_dedup_window_sec: float = Field(default=3, description="同一聊天流同一用户重复命令忽略窗口")
    max_reply_chars: int = Field(default=1800, description="单条回复最大字符数")
    advance_allowed_users: list[str] = Field(default_factory=list, description="除 GM 外允许推进的 scoped user id")


class DiceFrameBridgeConfig(PluginConfigBase):
    """DiceFrame Bridge config."""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    diceframe: DiceFrameServiceConfig = Field(default_factory=DiceFrameServiceConfig)
    commands: CommandConfig = Field(default_factory=CommandConfig)


class DiceFrameHTTPError(RuntimeError):
    """Raised when DiceFrame returns an error payload."""


class DiceFrameClient:
    """Small async client for DiceFrame's Bot-facing HTTP API."""

    def __init__(self, base_url: str, bot_token: str, timeout_sec: float) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.bot_token = str(bot_token or "").strip()
        self.timeout_sec = max(1.0, float(timeout_sec or 120))
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def bind_game(self, game_key: str, bind_token: str) -> dict[str, Any]:
        return await self._request("POST", "/api/bot/bind-game", json={"game_key": game_key, "bind_token": bind_token})

    async def list_games(self) -> dict[str, Any]:
        return await self._request("GET", "/api/games")

    async def ping(self) -> dict[str, Any]:
        return await self._request("GET", "/api/bot/ping")

    async def detail(self, game_key: str, actor: str = "") -> dict[str, Any]:
        return await self._request("GET", f"/api/games/{quote(game_key, safe='')}", actor=actor)

    async def characters(self, game_key: str, actor: str = "") -> dict[str, Any]:
        return await self._request("GET", f"/api/games/{quote(game_key, safe='')}/characters", actor=actor)

    async def action(self, game_key: str, actor: str, text: str, *, confirm: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text, "confirm": confirm, "source": "maibot"}
        if confirm:
            body["server_roll"] = True
        return await self._request(
            "POST",
            f"/api/games/{quote(game_key, safe='')}/action",
            actor=actor,
            json=body,
        )

    async def advance(self, game_key: str, actor: str, *, force: bool = True) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/games/{quote(game_key, safe='')}/advance",
            actor=actor,
            json={"force": bool(force)},
        )

    async def set_away(self, game_key: str, actor: str, user_id: str, away: bool) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/games/{quote(game_key, safe='')}/players/{quote(user_id, safe='')}/away",
            actor=actor,
            json={"away": bool(away)},
        )

    async def private_log(self, game_key: str, actor: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/games/{quote(game_key, safe='')}/private-log", actor=actor)

    async def map(self, game_key: str, actor: str = "") -> dict[str, Any]:
        return await self._request("GET", f"/api/games/{quote(game_key, safe='')}/map", actor=actor)

    async def public_config(self) -> dict[str, Any]:
        return await self._request("GET", "/api/config", auth=False)

    async def resolve_payment(self, game_key: str, actor: str, payment_id: str, accepted: bool) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/games/{quote(game_key, safe='')}/payments/{quote(payment_id, safe='')}",
            actor=actor,
            json={"accepted": bool(accepted)},
        )

    async def _request(self, method: str, path: str, *, actor: str = "", auth: bool = True, **kwargs: Any) -> dict[str, Any]:
        if not self.base_url:
            raise DiceFrameHTTPError("未配置 DiceFrame 服务地址")
        if auth and not self.bot_token:
            raise DiceFrameHTTPError("未配置 DiceFrame Bot API Token；请到 DiceFrame 设置 → Bot API 复制")
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
            self._session = aiohttp.ClientSession(timeout=timeout)

        headers = dict(kwargs.pop("headers", {}) or {})
        if auth:
            headers["X-Bot-Token"] = self.bot_token
        if actor:
            headers["X-Bot-Actor"] = actor

        async with self._session.request(method, self.base_url + path, headers=headers, **kwargs) as response:
            try:
                data = await response.json(content_type=None)
            except Exception as exc:
                text = await response.text()
                raise DiceFrameHTTPError(f"DiceFrame 返回了非 JSON 响应：HTTP {response.status} {text[:120]}") from exc
            if response.status >= 400:
                error = data.get("error") or data.get("message") or f"HTTP {response.status}"
                raise DiceFrameHTTPError(str(error))
            if isinstance(data, dict) and data.get("ok") is False:
                raise DiceFrameHTTPError(str(data.get("error") or data.get("narration") or "DiceFrame 请求失败"))
            return data if isinstance(data, dict) else {"data": data}


class BridgeStore:
    """Persistent mapping between MaiBot streams/users and DiceFrame games."""

    def __init__(self, path: Path, recent_limit: int = 500) -> None:
        self.path = path
        self.recent_limit = recent_limit
        self._lock = asyncio.Lock()
        self.groups: dict[str, dict[str, Any]] = {}
        self.players: dict[str, dict[str, str]] = {}
        self.recent_commands: dict[str, float] = {}

    async def load(self) -> None:
        if not self.path.exists():
            return
        async with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                return
            self.groups = data.get("groups", {}) if isinstance(data.get("groups"), dict) else {}
            self.players = data.get("players", {}) if isinstance(data.get("players"), dict) else {}
            recent = data.get("recent_commands", {})
            if isinstance(recent, dict):
                self.recent_commands = {
                    str(key): float(value)
                    for key, value in recent.items()
                    if isinstance(value, (int, float))
                }

    async def bind_group(self, stream_id: str, game_key: str, gm_platform_id: str, gm_uid: str, roster: list[dict[str, Any]]) -> None:
        async with self._lock:
            self.groups[stream_id] = {
                "game_key": game_key,
                "gm_platform_id": gm_platform_id,
                "gm_uid": gm_uid,
                "roster": roster,
                "world_name": "",
            }
            self.players[self.player_key(stream_id, gm_platform_id)] = {"game_key": game_key, "user_id": gm_uid}
            self._persist_locked()

    async def unbind_group(self, stream_id: str) -> None:
        async with self._lock:
            group = self.groups.pop(stream_id, None)
            game_key = str((group or {}).get("game_key") or "")
            if game_key:
                self.players = {
                    key: value
                    for key, value in self.players.items()
                    if not key.startswith(stream_id + ":") or value.get("game_key") != game_key
                }
            self._persist_locked()

    def group(self, stream_id: str) -> dict[str, Any] | None:
        return self.groups.get(stream_id)

    async def update_roster(self, stream_id: str, roster: list[dict[str, Any]]) -> None:
        async with self._lock:
            group = self.groups.get(stream_id)
            if not group:
                return
            group["roster"] = roster
            self._persist_locked()

    def player(self, stream_id: str, platform_user_id: str) -> dict[str, str] | None:
        return self.players.get(self.player_key(stream_id, platform_user_id))

    async def bind_player(self, stream_id: str, platform_user_id: str, user_id: str) -> bool:
        async with self._lock:
            group = self.groups.get(stream_id)
            if not group:
                return False
            game_key = str(group.get("game_key") or "")
            for key, mapping in self.players.items():
                if key != self.player_key(stream_id, platform_user_id) and mapping.get("game_key") == game_key and mapping.get("user_id") == user_id:
                    return False
            self.players[self.player_key(stream_id, platform_user_id)] = {"game_key": game_key, "user_id": user_id}
            self._persist_locked()
            return True

    async def remember_command(self, signature: str, window_sec: float) -> bool:
        signature = str(signature or "").strip()
        if not signature or window_sec <= 0:
            return True
        now = time.time()
        cutoff = now - window_sec
        async with self._lock:
            self.recent_commands = {key: ts for key, ts in self.recent_commands.items() if ts >= cutoff}
            if signature in self.recent_commands:
                return False
            self.recent_commands[signature] = now
            if len(self.recent_commands) > self.recent_limit:
                newest = sorted(self.recent_commands.items(), key=lambda item: item[1])[-self.recent_limit :]
                self.recent_commands = dict(newest)
            self._persist_locked()
            return True

    @staticmethod
    def player_key(stream_id: str, platform_user_id: str) -> str:
        return f"{stream_id}:{platform_user_id}"

    def _persist_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        data = {"groups": self.groups, "players": self.players, "recent_commands": self.recent_commands}
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)


class DiceFrameBridgePlugin(MaiBotPlugin):
    """Bridge MaiBot commands to DiceFrame."""

    config_model = DiceFrameBridgeConfig

    async def on_load(self) -> None:
        await self._init_runtime()

    async def on_unload(self) -> None:
        client = getattr(self, "_client", None)
        if client is not None:
            await client.close()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del scope
        del config_data
        del version
        client = getattr(self, "_client", None)
        if client is not None:
            await client.close()
        await self._init_runtime()

    async def _init_runtime(self) -> None:
        data_dir = Path(getattr(self.ctx.paths, "data_dir", Path(".")))
        shared = self._load_shared_core()
        if shared is not None:
            self._shared_core = shared
            self._store = shared.JsonBridgeStore(data_dir / "diceframe_bridge_sessions.json")
            await self._store.load()
            self._client = shared.DiceFrameClient(
                self.config.diceframe.base_url,
                self.config.diceframe.bot_token,
                self.config.diceframe.request_timeout_sec,
            )
            self._bridge_service = shared.DiceFrameBridgeService(
                self._client,
                self._store,
                shared.BridgeServiceConfig(
                    trigger=shared.TriggerConfig(
                        prefixes=tuple(self.config.commands.prefixes or ["/df", "/diceframe", "跑团"]),
                        mode=self._trigger_mode(),
                    ),
                    command_dedup_window_sec=self.config.commands.command_dedup_window_sec,
                    max_reply_chars=self.config.commands.max_reply_chars,
                    public_base_url=self.config.diceframe.public_base_url,
                    action_source="maibot",
                    advance_allowed_users={str(item) for item in self.config.commands.advance_allowed_users},
                ),
            )
            return

        self._shared_core = None
        self._bridge_service = None
        self._store = BridgeStore(data_dir / "diceframe_bridge_sessions.json")
        await self._store.load()
        self._client = self._build_client()

    def _load_shared_core(self) -> SimpleNamespace | None:
        core_path = str(self.config.diceframe.shared_core_path or "").strip()
        if core_path and core_path not in sys.path:
            sys.path.insert(0, core_path)
        try:
            client_mod = importlib.import_module("src.bots.bridge_core.client")
            models_mod = importlib.import_module("src.bots.bridge_core.models")
            service_mod = importlib.import_module("src.bots.bridge_core.service")
            store_mod = importlib.import_module("src.bots.bridge_core.store")
            triggers_mod = importlib.import_module("src.bots.bridge_core.triggers")
        except Exception:
            return None
        return SimpleNamespace(
            BridgeInput=models_mod.BridgeInput,
            BridgeServiceConfig=service_mod.BridgeServiceConfig,
            DiceFrameBridgeService=service_mod.DiceFrameBridgeService,
            DiceFrameClient=client_mod.DiceFrameClient,
            JsonBridgeStore=store_mod.JsonBridgeStore,
            TriggerConfig=triggers_mod.TriggerConfig,
        )

    def _trigger_mode(self) -> str:
        mode = str(getattr(self.config.commands, "trigger_mode", "") or "").strip()
        if mode in {"prefix_only", "mention_bare", "prefix_or_mention", "bare"}:
            return mode
        return "mention_bare" if self.config.commands.allow_mentioned_bare_commands else "prefix_only"

    @Command(
        "diceframe",
        description="桥接 DiceFrame 跑团服务。推荐使用 /df、/diceframe 或 跑团 前缀，避免 @MaiBot 触发主聊天回复。",
        pattern=(
            r"(?P<df_command>^(?:"
            r"(?:/df|/diceframe|跑团)(?:\s+.*)?"
            r"|(?:帮助|help|\?|绑定|bind|解绑|unbind|加入|join|邀请|invite|新建角色|车卡|AI车卡|ai车卡|"
            r"前情|recap|地图|map|状态|status|感知|sense|log|支付|pay|确认支付|拒绝支付|rejectpay|"
            r"掷骰|roll|推进|下一轮|advance|next|暂离|away|回来|return|back|行动|做|连接测试|测试连接|ping)(?:\s+.*)?"
            r")$)"
        ),
    )
    async def handle_diceframe(self, stream_id: str = "", platform: str = "", user_id: str = "", matched_groups: dict | None = None, **kwargs: Any):
        if not self.config.plugin.enabled:
            await self.ctx.send.text("DiceFrame Bridge 尚未启用。请先在插件配置里打开 plugin.enabled。", stream_id)
            return False, "插件未启用", True

        if not stream_id:
            return False, "无法获取当前聊天流", True

        raw_text = self._extract_command_text(matched_groups, kwargs)
        platform_user_id = self._scoped_user_id(platform, user_id)
        bridge_service = getattr(self, "_bridge_service", None)
        shared = getattr(self, "_shared_core", None)
        if bridge_service is not None and shared is not None:
            result = await bridge_service.handle(
                shared.BridgeInput(
                    stream_id=stream_id,
                    platform_user_id=platform_user_id,
                    text=raw_text,
                    mentioned_bot=self._message_mentions_bot(kwargs.get("message")),
                    platform=platform,
                    raw_message=kwargs.get("message"),
                )
            )
            if not result.handled:
                return False, "非 DiceFrame 显式前缀命令，已忽略", False
            for reply in result.replies:
                await self.ctx.send.text(reply, stream_id)
            return True, "DiceFrame 命令已处理", True

        explicit_prefix = self._has_explicit_prefix(raw_text)
        if not explicit_prefix and not self.config.commands.allow_mentioned_bare_commands:
            return False, "非 DiceFrame 显式前缀命令，已忽略", False
        if (
            not explicit_prefix
            and self.config.commands.require_mention_for_bare_commands
            and not self._message_mentions_bot(kwargs.get("message"))
        ):
            return False, "非 @DiceFrame 裸指令，已忽略", False
        command_text = self._strip_prefix(raw_text).strip()

        normalized_command = re.sub(r"\s+", " ", command_text)
        signature = f"{stream_id}:{platform_user_id}:{normalized_command}"
        if not await self._store.remember_command(signature, self.config.commands.command_dedup_window_sec):
            return True, "重复命令已忽略", True

        try:
            reply = await self._dispatch_command(command_text, stream_id, platform_user_id)
        except DiceFrameHTTPError as exc:
            reply = f"DiceFrame 请求失败：{exc}"
        except Exception as exc:
            reply = f"DiceFrame Bridge 处理失败：{exc}"

        await self._send_reply(stream_id, reply)
        return True, "DiceFrame 命令已处理", True

    async def _dispatch_command(self, text: str, stream_id: str, platform_user_id: str) -> str:
        if not text or text in {"帮助", "help", "?"}:
            return self._help_text()

        parts = text.split(maxsplit=1)
        verb = parts[0].strip()
        rest = parts[1].strip() if len(parts) > 1 else ""

        aliases = {
            "bind": "绑定",
            "join": "加入",
            "status": "状态",
            "recap": "前情",
            "map": "地图",
            "roll": "掷骰",
            "advance": "推进",
            "next": "推进",
            "下一轮": "推进",
            "pay": "支付",
            "确认支付": "支付",
            "rejectpay": "拒绝支付",
            "log": "感知",
            "sense": "感知",
            "invite": "邀请",
            "away": "暂离",
            "return": "回来",
            "back": "回来",
            "unbind": "解绑",
            "ping": "连接测试",
            "测试连接": "连接测试",
        }
        verb = aliases.get(verb.lower(), verb)

        if verb == "连接测试":
            return await self._handle_ping()
        if verb == "绑定":
            return await self._handle_bind(stream_id, platform_user_id, rest)
        if verb == "解绑":
            return await self._handle_unbind(stream_id)
        if verb == "邀请":
            return await self._handle_invite(stream_id, platform_user_id)
        if verb in {"新建角色", "车卡", "AI车卡", "ai车卡"}:
            return await self._handle_character_guide(stream_id, platform_user_id, ai=verb.lower() == "ai车卡")
        if verb == "加入":
            return await self._handle_join(stream_id, platform_user_id, rest)
        if verb == "状态":
            return await self._handle_status(stream_id, platform_user_id)
        if verb == "前情":
            return await self._handle_recap(stream_id, platform_user_id)
        if verb == "地图":
            return await self._handle_map(stream_id, platform_user_id)
        if verb == "感知":
            return await self._handle_private_log(stream_id, platform_user_id)
        if verb == "掷骰":
            return await self._handle_roll(stream_id, platform_user_id)
        if verb == "推进":
            return await self._handle_advance(stream_id, platform_user_id)
        if verb == "暂离":
            return await self._handle_away(stream_id, platform_user_id, True)
        if verb == "回来":
            return await self._handle_away(stream_id, platform_user_id, False)
        if verb == "支付":
            return await self._handle_payment(stream_id, platform_user_id, rest, accepted=True)
        if verb == "拒绝支付":
            return await self._handle_payment(stream_id, platform_user_id, rest, accepted=False)
        if verb in {"行动", "做"}:
            return await self._handle_action(stream_id, platform_user_id, rest)
        return await self._handle_action(stream_id, platform_user_id, text)

    async def _handle_ping(self) -> str:
        data = await self._client.ping()
        app_name = str(data.get("app_name") or data.get("name") or "DiceFrame")
        return f"{app_name} 连接正常。服务地址：{self.config.diceframe.base_url}"

    async def _handle_bind(self, stream_id: str, platform_user_id: str, args: str) -> str:
        bits = args.split(maxsplit=1)
        if len(bits) != 2:
            return "用法：/df 绑定 <game_key> <一次性凭证>"
        game_key, bind_token = bits[0].strip(), bits[1].strip()
        result = await self._client.bind_game(game_key, bind_token)
        gm_uid = str(result.get("gm_uid") or "")
        players = result.get("players") if isinstance(result.get("players"), list) else []
        await self._store.bind_group(stream_id, game_key, platform_user_id, gm_uid, players)
        world = str(result.get("world_name") or game_key)
        return (
            f"已绑定 DiceFrame 对局《{world}》。\n"
            f"GM 已映射为当前用户。\n"
            f"可认领角色：{self._roster_names(players)}\n"
            "下一步：玩家发送 /df 加入 角色名，然后用 /df 我调查四周 提交行动。"
        )

    async def _handle_unbind(self, stream_id: str) -> str:
        await self._store.unbind_group(stream_id)
        return "当前聊天流已解除 DiceFrame 绑定。"

    async def _handle_invite(self, stream_id: str, platform_user_id: str) -> str:
        _, game_key, actor = self._require_actor_or_group_gm(stream_id, platform_user_id)
        detail = await self._client.detail(game_key, actor)
        world = str(detail.get("world_name") or game_key)
        link = await self._build_join_link(game_key)
        lines = [
            f"DiceFrame《{world}》加入入口：",
            link or "未配置公开网页入口，请在插件配置里设置 diceframe.public_base_url。",
            "",
            "新玩家：先在网页创建或确认角色，再回群发送 @我 加入 角色名。",
        ]
        return "\n".join(lines)

    async def _handle_character_guide(self, stream_id: str, platform_user_id: str, *, ai: bool) -> str:
        _, game_key, actor = self._require_actor_or_group_gm(stream_id, platform_user_id)
        data = await self._client.characters(game_key, actor)
        meta = data.get("rule_meta") if isinstance(data.get("rule_meta"), dict) else {}
        attrs = data.get("rule_attrs") if isinstance(data.get("rule_attrs"), list) else []
        classes = data.get("rule_classes") if isinstance(data.get("rule_classes"), list) else []
        attr_names = [
            str(item.get("display_name") or item.get("name") or item.get("key"))
            for item in attrs
            if isinstance(item, dict)
        ]
        class_names = [str(item.get("name") if isinstance(item, dict) else item).strip() for item in classes]
        class_names = [name for name in class_names if name]
        link = await self._build_join_link(game_key)
        lines = [
            "新建角色 / 车卡：",
            "1. 角色名：你想被怎么称呼",
            "2. 身份/定位：" + ("参考：" + "、".join(class_names[:6]) if class_names else "按世界观填写"),
            "3. 属性：" + ("、".join(attr_names[:8]) if attr_names else "按网页表单填写"),
            "4. 技能：" + str(meta.get("skill_hint") or "按角色定位选择"),
            "5. 背景：1-3 句话说明来历、目标、秘密",
        ]
        if ai:
            lines.append("AI 车卡多轮私聊暂未接入 MaiBot 版；请先用网页入口生成或填写角色。")
        if link:
            lines.append(f"网页建卡入口：{link}")
        lines.append("填完后回群发送：@我 加入 角色名")
        return "\n".join(lines)

    async def _handle_join(self, stream_id: str, platform_user_id: str, name: str) -> str:
        if not name:
            return "用法：/df 加入 <角色名>"
        group, game_key, actor = self._require_group(stream_id)
        roster = await self._refresh_roster(stream_id, group, actor)
        matches = self._match_roster_character(roster, name)
        if len(matches) != 1:
            return f"没有找到唯一匹配的角色。可认领：{self._roster_names(roster)}"
        user_id = str(matches[0].get("user_id") or "")
        if not user_id:
            return "匹配到的角色缺少 user_id，无法认领。"
        ok = await self._store.bind_player(stream_id, platform_user_id, user_id)
        if not ok:
            return "该角色已经被其他群成员认领。"
        character_name = str(matches[0].get("character_name") or name)
        return f"已认领角色：{character_name}。现在可以发送 /df 我调查四周。"

    async def _handle_status(self, stream_id: str, platform_user_id: str) -> str:
        group, game_key, actor = self._require_actor(stream_id, platform_user_id)
        data = await self._client.characters(game_key, actor)
        players = data.get("players") if isinstance(data.get("players"), list) else []
        player = next((item for item in players if str(item.get("user_id") or "") == actor), None)
        if not isinstance(player, dict):
            return "未找到你的角色状态。请先 /df 加入 角色名。"
        return await self._format_status(player, group)

    async def _handle_recap(self, stream_id: str, platform_user_id: str) -> str:
        _, game_key, actor = self._require_actor_or_group_gm(stream_id, platform_user_id)
        detail = await self._client.detail(game_key, actor)
        return self._format_recap(detail)

    async def _handle_map(self, stream_id: str, platform_user_id: str) -> str:
        _, game_key, actor = self._require_actor_or_group_gm(stream_id, platform_user_id)
        data = await self._client.map(game_key, actor)
        return self._format_map(data)

    async def _handle_private_log(self, stream_id: str, platform_user_id: str) -> str:
        _, game_key, actor = self._require_actor(stream_id, platform_user_id)
        data = await self._client.private_log(game_key, actor)
        messages = data.get("messages") if isinstance(data.get("messages"), list) else []
        if not messages:
            return "当前没有你的私密信息。"
        lines = ["感知 / 私密信息："]
        for item in messages[-6:]:
            if isinstance(item, dict):
                lines.append(f"R{item.get('round', '?')}：{str(item.get('text') or '').strip()}")
        return "\n".join(lines)

    async def _handle_action(self, stream_id: str, platform_user_id: str, text: str) -> str:
        if not text:
            group = self._store.group(stream_id)
            return self._bound_help_text(group) if group else self._unbound_text()
        _, game_key, actor = self._require_actor(stream_id, platform_user_id)
        result = await self._client.action(game_key, actor, text)
        if result.get("phase") == "dice":
            return "这次行动需要检定。请发送 /df 掷骰，或重新描述行动覆盖本轮声明。"
        return await self._format_action_response(game_key, actor, result)

    async def _handle_roll(self, stream_id: str, platform_user_id: str) -> str:
        _, game_key, actor = self._require_actor(stream_id, platform_user_id)
        result = await self._client.action(game_key, actor, "", confirm=True)
        return await self._format_action_response(game_key, actor, result)

    async def _handle_advance(self, stream_id: str, platform_user_id: str) -> str:
        group, game_key, actor = self._require_group(stream_id)
        if not self._can_advance(group, platform_user_id):
            return "只有绑定本局的 GM 或配置中的授权用户可以推进。"
        result = await self._client.advance(game_key, actor, force=True)
        return self._format_advance_response(result)

    async def _handle_away(self, stream_id: str, platform_user_id: str, away: bool) -> str:
        _, game_key, actor = self._require_actor(stream_id, platform_user_id)
        result = await self._client.set_away(game_key, actor, actor, away)
        name = str(result.get("character_name") or actor)
        return f"{name} 已{'暂离' if away else '回来'}。"

    async def _handle_payment(self, stream_id: str, platform_user_id: str, arg: str, accepted: bool) -> str:
        _, game_key, actor = self._require_actor(stream_id, platform_user_id)
        payments = await self._pending_payments_for_actor(game_key, actor)
        if not arg:
            if not payments:
                return "当前没有待处理的支付请求。"
            lines = ["待处理支付："]
            for index, payment in enumerate(payments, 1):
                lines.append(self._payment_line(payment, index))
            lines.append("确认：/df 支付 1；拒绝：/df 拒绝支付 1")
            return "\n".join(lines)
        try:
            index = int(arg.strip())
        except ValueError:
            return "支付序号必须是数字，例如 /df 支付 1"
        if index < 1 or index > len(payments):
            return "支付序号不存在。先发送 /df 支付 查看列表。"
        payment = payments[index - 1]
        result = await self._client.resolve_payment(game_key, actor, str(payment.get("id") or ""), accepted)
        if result.get("accepted"):
            return "支付已确认。"
        return "支付已拒绝。"

    def _build_client(self) -> DiceFrameClient:
        cfg = self.config.diceframe
        return DiceFrameClient(cfg.base_url, cfg.bot_token, cfg.request_timeout_sec)

    def _require_group(self, stream_id: str) -> tuple[dict[str, Any], str, str]:
        group = self._store.group(stream_id)
        if not group:
            raise DiceFrameHTTPError(self._unbound_text())
        game_key = str(group.get("game_key") or "")
        gm_uid = str(group.get("gm_uid") or "")
        if not game_key or not gm_uid:
            raise DiceFrameHTTPError("当前绑定信息不完整，请重新绑定。")
        return group, game_key, gm_uid

    def _require_actor(self, stream_id: str, platform_user_id: str) -> tuple[dict[str, Any], str, str]:
        group, game_key, _ = self._require_group(stream_id)
        player = self._store.player(stream_id, platform_user_id)
        if not player:
            raise DiceFrameHTTPError("你还没有认领角色。请先发送 /df 加入 角色名。")
        actor = str(player.get("user_id") or "")
        if not actor:
            raise DiceFrameHTTPError("你的角色映射不完整，请重新 /df 加入 角色名。")
        return group, game_key, actor

    def _require_actor_or_group_gm(self, stream_id: str, platform_user_id: str) -> tuple[dict[str, Any], str, str]:
        group, game_key, gm_uid = self._require_group(stream_id)
        player = self._store.player(stream_id, platform_user_id)
        actor = str((player or {}).get("user_id") or gm_uid)
        return group, game_key, actor

    async def _refresh_roster(self, stream_id: str, group: dict[str, Any], actor: str) -> list[dict[str, Any]]:
        game_key = str(group.get("game_key") or "")
        data = await self._client.characters(game_key, actor)
        players = data.get("players") if isinstance(data.get("players"), list) else []
        roster = [item for item in players if isinstance(item, dict)]
        await self._store.update_roster(stream_id, roster)
        return roster

    async def _pending_payments_for_actor(self, game_key: str, actor: str) -> list[dict[str, Any]]:
        detail = await self._client.detail(game_key, actor)
        payments = detail.get("pending_payments") if isinstance(detail.get("pending_payments"), list) else []
        gm_uid = str(detail.get("gm_uid") or "")
        result = []
        for payment in payments:
            if not isinstance(payment, dict):
                continue
            if actor == gm_uid or str(payment.get("uid") or "") == actor:
                result.append(payment)
        return result

    async def _format_action_response(self, game_key: str, actor: str, result: dict[str, Any]) -> str:
        lines: list[str] = []
        roll = result.get("roll") or {}
        if isinstance(roll, dict) and roll.get("value") is not None:
            lines.append(f"掷骰：{str(roll.get('dice_system') or '').upper()} = {roll.get('value')}")
        narration = str(result.get("narration") or "").strip()
        if narration:
            lines.append(narration)
        if result.get("advanced"):
            try:
                detail = await self._client.detail(game_key, actor)
                recap = detail.get("recap") if isinstance(detail.get("recap"), dict) else {}
                recent = recap.get("recent_rounds") if isinstance(recap.get("recent_rounds"), list) else []
                if recent and isinstance(recent[-1], dict):
                    gm_text = str(recent[-1].get("gm_response") or "").strip()
                    if gm_text and gm_text not in lines:
                        lines = [line for line in lines if line != narration]
                        lines.append(gm_text)
                    changes = recent[-1].get("state_changes") if isinstance(recent[-1].get("state_changes"), list) else []
                    for change in changes[:6]:
                        text = str(change).strip()
                        if text:
                            lines.append(text)
            except Exception:
                pass
        pending = result.get("pending_payments") if isinstance(result.get("pending_payments"), list) else []
        if pending:
            lines.append("有待处理支付，发送 /df 支付 查看。")
        quick_actions = result.get("quick_actions") if isinstance(result.get("quick_actions"), list) else []
        if quick_actions:
            lines.append("可选行动：" + "；".join(str(item) for item in quick_actions[:4]))
        return "\n".join(lines).strip() or "行动已记录。"

    def _format_advance_response(self, result: dict[str, Any]) -> str:
        lines: list[str] = []
        narration = str(result.get("narration") or result.get("message") or "").strip()
        if narration:
            lines.append(narration)
        forced = result.get("forced_waiting") if isinstance(result.get("forced_waiting"), list) else []
        if forced:
            lines.append("已为未行动角色补默认行动：" + "、".join(str(item) for item in forced))
        auto_rolls = result.get("auto_rolls") if isinstance(result.get("auto_rolls"), list) else []
        if auto_rolls:
            roll_text = "、".join(f"{item.get('user_id')}={item.get('value')}" for item in auto_rolls if isinstance(item, dict))
            lines.append("已自动处理待掷骰：" + roll_text)
        pending = result.get("pending_payments") if isinstance(result.get("pending_payments"), list) else []
        if pending:
            lines.append("有待处理支付，发送 /df 支付 查看。")
        quick_actions = result.get("quick_actions") if isinstance(result.get("quick_actions"), list) else []
        if quick_actions:
            lines.append("可选行动：" + "；".join(str(item) for item in quick_actions[:4]))
        return "\n".join(lines).strip() or "推进完成。"

    async def _format_status(self, player: dict[str, Any], group: dict[str, Any]) -> str:
        name = str(player.get("character_name") or player.get("user_id") or "角色")
        sheet = player.get("character_sheet") if isinstance(player.get("character_sheet"), dict) else {}
        lines = [f"{name} 状态"]
        hp = sheet.get("hp")
        max_hp = sheet.get("max_hp")
        if hp is not None or max_hp is not None:
            lines.append(f"HP：{hp}/{max_hp}")
        if sheet.get("gold") is not None:
            lines.append(f"金币：{sheet.get('gold')}")
        attrs = sheet.get("attributes_display") or self._format_attrs(sheet.get("attributes"))
        if attrs:
            lines.append(f"属性：{attrs}")
        skills = self._format_skills(sheet.get("skills"))
        if skills:
            lines.append(f"技能：{skills}")
        status = sheet.get("status")
        if status:
            lines.append(f"状态：{status}")
        game_key = str(group.get("game_key") or "")
        link = await self._build_join_link(game_key, str(player.get("user_id") or ""))
        if link:
            lines.append(f"网页入口：{link}")
        return "\n".join(lines)

    def _format_recap(self, detail: dict[str, Any]) -> str:
        recap = detail.get("recap") if isinstance(detail.get("recap"), dict) else {}
        scene = str(recap.get("current_scene") or detail.get("scene") or "未知场景")
        round_no = recap.get("round_number") or detail.get("round_number") or "?"
        lines = [f"前情提要：第 {round_no} 轮，当前场景「{scene}」。"]
        narrative = str(recap.get("narrative") or "").strip()
        if narrative:
            lines.append(narrative)
        recent = recap.get("recent_rounds") if isinstance(recap.get("recent_rounds"), list) else []
        for item in recent[-3:]:
            if not isinstance(item, dict):
                continue
            gm_text = str(item.get("gm_response") or "").strip()
            if gm_text:
                lines.append(f"R{item.get('round', '?')}：{gm_text}")
        waiting = (detail.get("multiplayer") or {}).get("waiting_players") if isinstance(detail.get("multiplayer"), dict) else []
        if isinstance(waiting, list) and waiting:
            names = [str(item.get("character_name") or item.get("user_id") or "") for item in waiting if isinstance(item, dict)]
            names = [name for name in names if name]
            if names:
                lines.append("现在等待：" + "、".join(names) + " 行动。")
        return "\n".join(lines)

    def _format_map(self, data: dict[str, Any]) -> str:
        locations = data.get("locations") if isinstance(data.get("locations"), list) else []
        current_scene = str(data.get("current_scene") or "").strip()
        if not locations:
            return "暂无地图数据。" + (f"\n当前场景：{current_scene}" if current_scene else "")
        lines = [f"场景地图：{current_scene or '未知'}"]
        for loc in locations[:10]:
            if not isinstance(loc, dict):
                continue
            name = str(loc.get("name") or "").strip()
            if not name:
                continue
            content = re.sub(r"\s+", " ", str(loc.get("content") or "").strip())
            if len(content) > 50:
                content = content[:50] + "..."
            marker = "*" if current_scene and (name == current_scene or name in current_scene or current_scene in name) else "-"
            lines.append(f"{marker} {name}" + (f"：{content}" if content else ""))
        if len(locations) > 10:
            lines.append(f"另有 {len(locations) - 10} 个地点，可在网页地图查看。")
        return "\n".join(lines)

    def _payment_line(self, payment: dict[str, Any], index: int) -> str:
        amount = int(payment.get("amount", 0) or 0)
        reason = str(payment.get("reason") or "GM 建议支付").strip()
        round_no = payment.get("round", "?")
        return f"{index}. R{round_no} {amount} 金币：{reason}"

    def _help_text(self) -> str:
        return (
            "DiceFrame Bridge 帮助\n"
            "推荐不要 @我，避免触发 MaiBot 主聊天回复；请用明确前缀：\n"
            "跑团 绑定 <game_key> <一次性凭证>\n"
            "跑团 邀请 / 新建角色 / 车卡 / AI车卡\n"
            "跑团 加入 <角色名>\n"
            "跑团 <自然语言行动>，或 跑团 行动 <自然语言行动>\n"
            "跑团 掷骰\n"
            "跑团 状态 / 前情 / 地图 / 感知\n"
            "跑团 支付 / 支付 1 / 拒绝支付 1\n"
            "跑团 暂离 / 回来\n"
            "跑团 推进 / 下一轮（GM 或授权用户）\n"
            "跑团 解绑 / 连接测试\n"
            "也可使用 /df 作为前缀，例如 /df 我调查四周。"
        )

    def _bound_help_text(self, group: dict[str, Any] | None) -> str:
        if not group:
            return self._unbound_text()
        roster = group.get("roster") if isinstance(group.get("roster"), list) else []
        return (
            "DiceFrame 群聊指南：\n"
            f"可认领：{self._roster_names(roster)}\n"
            "1. 跑团 加入 角色名\n"
            "2. 跑团 我调查四周\n"
            "3. 需要检定时：跑团 掷骰\n"
            "4. 补信息：跑团 前情、跑团 地图、跑团 状态\n"
            "不要 @我，避免触发 MaiBot 主聊天回复。"
        )

    @staticmethod
    def _unbound_text() -> str:
        return (
            "当前聊天流尚未绑定 DiceFrame 对局。\n"
            "GM 请在 DiceFrame 网页生成一次性 Bot 绑定凭证，然后发送：\n"
            "跑团 绑定 <game_key> <一次性凭证> 或 /df 绑定 <game_key> <一次性凭证>"
        )

    def _can_advance(self, group: dict[str, Any], platform_user_id: str) -> bool:
        if str(group.get("gm_platform_id") or "") == platform_user_id:
            return True
        allowed = {str(item).strip().lower() for item in self.config.commands.advance_allowed_users if str(item).strip()}
        return platform_user_id.lower() in allowed

    async def _send_reply(self, stream_id: str, reply: str) -> None:
        max_chars = max(200, int(self.config.commands.max_reply_chars or 1800))
        text = str(reply or "").strip() or "DiceFrame Bridge 没有返回内容。"
        chunks = [text[i : i + max_chars] for i in range(0, len(text), max_chars)]
        for chunk in chunks[:4]:
            await self.ctx.send.text(chunk, stream_id)

    def _extract_command_text(self, matched_groups: dict | None, kwargs: dict[str, Any]) -> str:
        if isinstance(matched_groups, dict):
            value = str(matched_groups.get("df_command") or "").strip()
            if value:
                return value
        return str(kwargs.get("text") or "").strip()

    def _has_explicit_prefix(self, raw_text: str) -> bool:
        text = str(raw_text or "").strip()
        prefixes = [str(item).strip() for item in self.config.commands.prefixes if str(item).strip()]
        prefixes.extend(["/df", "/diceframe", "跑团"])
        for prefix in sorted(set(prefixes), key=len, reverse=True):
            if text == prefix:
                return True
            if text.startswith(prefix) and (len(text) == len(prefix) or text[len(prefix)].isspace()):
                return True
        return False

    @staticmethod
    def _message_mentions_bot(message: Any) -> bool:
        if not isinstance(message, dict):
            return False
        if message.get("is_at") or message.get("is_mentioned"):
            return True
        info = message.get("message_info")
        if isinstance(info, dict):
            config = info.get("additional_config")
            if isinstance(config, dict) and (config.get("at_bot") or config.get("is_mentioned")):
                return True
        for key in ("message_segment", "raw_message"):
            if DiceFrameBridgePlugin._contains_mention_bot(message.get(key)):
                return True
        return False

    @staticmethod
    def _contains_mention_bot(value: Any) -> bool:
        if isinstance(value, dict):
            value_type = str(value.get("type") or "")
            if value_type in {"mention_bot", "at_bot"}:
                return True
            if value_type == "at":
                data = value.get("data")
                if isinstance(data, dict) and str(data.get("target") or data.get("qq") or "").lower() in {"all", "bot", "self"}:
                    return True
            return any(DiceFrameBridgePlugin._contains_mention_bot(item) for item in value.values())
        if isinstance(value, list):
            return any(DiceFrameBridgePlugin._contains_mention_bot(item) for item in value)
        return False

    def _strip_prefix(self, raw_text: str) -> str:
        text = str(raw_text or "").strip()
        prefixes = sorted((str(item).strip() for item in self.config.commands.prefixes if str(item).strip()), key=len, reverse=True)
        for prefix in prefixes:
            if text == prefix:
                return ""
            if text.startswith(prefix) and (len(text) == len(prefix) or text[len(prefix)].isspace()):
                return text[len(prefix) :].strip()
        for prefix in ("/df", "/diceframe", "跑团"):
            if text.startswith(prefix):
                return text[len(prefix) :].strip()
        return text

    @staticmethod
    def _scoped_user_id(platform: str, user_id: str) -> str:
        platform = str(platform or "unknown").strip().lower() or "unknown"
        user_id = str(user_id or "unknown").strip() or "unknown"
        return f"{platform}:{user_id}"

    @staticmethod
    def _roster_names(roster: list[Any]) -> str:
        names = [
            str(item.get("character_name") or "").strip()
            for item in roster
            if isinstance(item, dict) and str(item.get("character_name") or "").strip()
        ]
        return "、".join(names[:12]) or "暂无角色（请先在网页创建角色）"

    @staticmethod
    def _match_roster_character(roster: list[Any], query: str) -> list[dict[str, Any]]:
        normalized_query = re.sub(r"\s+", "", str(query or ""))
        candidates = [item for item in roster if isinstance(item, dict) and str(item.get("character_name") or "").strip()]
        exact = [
            item
            for item in candidates
            if re.sub(r"\s+", "", str(item.get("character_name") or "")) == normalized_query
        ]
        if exact:
            return exact
        return [
            item
            for item in candidates
            if normalized_query and normalized_query in re.sub(r"\s+", "", str(item.get("character_name") or ""))
        ]

    @staticmethod
    def _format_attrs(attrs: Any) -> str:
        if not isinstance(attrs, dict) or not attrs:
            return ""
        return "、".join(f"{key}:{value}" for key, value in list(attrs.items())[:8])

    @staticmethod
    def _format_skills(skills: Any) -> str:
        if not isinstance(skills, list) or not skills:
            return ""
        names: list[str] = []
        for item in skills[:8]:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                value = item.get("value")
                if name:
                    names.append(f"{name}{f' {value}' if value not in (None, '') else ''}")
            else:
                value = str(item).strip()
                if value:
                    names.append(value)
        return "、".join(names)

    async def _build_join_link(self, game_key: str, user: str = "") -> str:
        if not game_key:
            return ""
        base = str(self.config.diceframe.public_base_url or "").strip()
        if not base:
            try:
                config = await self._client.public_config()
                base = str(config.get("public_base_url") or "").strip()
            except Exception:
                base = ""
        base = base or str(self.config.diceframe.base_url or "").strip()
        if not base:
            return ""
        parsed = urlparse(base)
        if not parsed.scheme:
            parsed = urlparse("http://" + base)
        path = (parsed.path or "").rstrip("/") + "/"
        query = urlencode({"game": game_key, "share": "1", **({"user": user} if user else {})})
        return urlunparse((parsed.scheme, parsed.netloc, path, "", "", f"/join?{query}"))


def create_plugin() -> DiceFrameBridgePlugin:
    """Create plugin instance."""

    return DiceFrameBridgePlugin()
