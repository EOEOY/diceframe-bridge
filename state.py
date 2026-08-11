"""DiceFrame Bridge — persistent stream/game binding state.

`BridgeStore` 从原 `plugin.py` 拆出：只负责聊天流/用户与 DiceFrame 对局的
持久化映射、角色认领与命令去重，不涉及任何网络或格式化逻辑。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from presenters import DEFAULT_LANGUAGE, normalize_language


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

    async def bind_group(
        self,
        stream_id: str,
        game_key: str,
        gm_platform_id: str,
        gm_uid: str,
        roster: list[dict[str, Any]],
        language: str = DEFAULT_LANGUAGE,
    ) -> None:
        async with self._lock:
            self.groups[stream_id] = {
                "game_key": game_key,
                "gm_platform_id": gm_platform_id,
                "gm_uid": gm_uid,
                "roster": roster,
                "world_name": "",
                "language": normalize_language(language),
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
