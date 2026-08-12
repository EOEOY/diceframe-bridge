from __future__ import annotations

import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


if "maibot_sdk" not in sys.modules:
    sdk = types.ModuleType("maibot_sdk")

    def command(*_args, **_kwargs):
        pattern = _kwargs.get("pattern")
        if pattern:
            re.compile(pattern)

        def decorate(func):
            func._command_pattern = pattern
            return func
        return decorate

    def field(*, default=None, default_factory=None, **_kwargs):
        return default_factory() if default_factory is not None else default

    class MaiBotPlugin:
        pass

    class PluginConfigBase:
        pass

    sdk.Command = command
    sdk.Field = field
    sdk.MaiBotPlugin = MaiBotPlugin
    sdk.PluginConfigBase = PluginConfigBase
    sys.modules["maibot_sdk"] = sdk


from plugin import (  # noqa: E402
    BridgeStore,
    DiceFrameBridgePlugin,
    DiceFrameHTTPError,
    format_check_result,
    normalize_language,
)


class FakeClient:
    def __init__(self):
        self.payment_resolutions = []
        self.luck_resolutions = []
        self.extension_calls = []
        self.pending_luck = []

    async def bind_game(self, game_key: str, bind_token: str) -> dict:
        self.bind_call = (game_key, bind_token)
        return {
            "ok": True,
            "game_key": game_key,
            "gm_uid": "gm-1",
            "world_name": "The Long Night",
            "language": "en",
            "players": [
                {"user_id": "gm-1", "character_name": "Game Master"},
                {"user_id": "player-1", "character_name": "Erin"},
            ],
        }

    async def characters(self, _game_key: str, _actor: str) -> dict:
        return {
            "players": [{
                "user_id": "player-1",
                "character_name": "Erin",
                "character_sheet": {
                    "hp": 8,
                    "max_hp": 10,
                    "gold": 4,
                    "attributes": {"dex": 14},
                    "skills": [{"name": "Stealth", "value": 45}],
                },
            }],
        }

    async def detail(self, _game_key: str, _actor: str) -> dict:
        return {
            "gm_uid": "gm-1",
            "pending_payments": [{
                "id": "pay-1",
                "uid": "player-1",
                "amount": 5,
                "reason": "Healing potion",
                "round": 3,
                "status": "pending",
            }],
            "pending_luck_decisions": list(self.pending_luck),
        }

    async def resolve_payment(self, game_key: str, actor: str, payment_id: str, accepted: bool) -> dict:
        self.payment_resolutions.append((game_key, actor, payment_id, accepted))
        return {"ok": True, "accepted": accepted}

    async def resolve_luck(self, game_key: str, actor: str, check_id: str, *, spend: bool) -> dict:
        self.luck_resolutions.append((game_key, actor, check_id, spend))
        self.pending_luck = []
        return {"ok": True, "advanced": False, "pending_luck_decisions": []}

    async def ping(self) -> dict:
        return {"ok": True, "bridge_extensions": {"protocol_version": 1}}

    async def apply_bridge_extensions(self, stage: str, payload: dict) -> dict:
        self.extension_calls.append((stage, dict(payload)))
        if stage == "before_message" and payload.get("text") == "plugin":
            return {
                "ok": True,
                "handled": True,
                "payload": payload,
                "outputs": [{"type": "text", "text": "handled by extension"}],
            }
        if stage == "after_result":
            changed = dict(payload)
            changed["text"] = "changed by extension"
            return {"ok": True, "handled": False, "payload": changed, "outputs": []}
        return {"ok": True, "handled": False, "payload": payload, "outputs": []}


def make_plugin(store: BridgeStore) -> DiceFrameBridgePlugin:
    bridge = DiceFrameBridgePlugin()
    bridge._store = store
    bridge._client = FakeClient()
    bridge.config = SimpleNamespace(
        diceframe=SimpleNamespace(
            base_url="http://127.0.0.1:18000",
            bot_token="test-token",
            public_base_url="https://table.example",
            request_timeout_sec=30,
        ),
        commands=SimpleNamespace(
            prefixes=["/df", "/diceframe", "跑团"],
            advance_allowed_users=[],
            max_reply_chars=1800,
        ),
    )
    return bridge


class BridgeI18nTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "sessions.json"
        self.store = BridgeStore(self.path)
        self.bridge = make_plugin(self.store)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_english_bind_persists_language_and_drives_replies(self):
        bound = await self.bridge._dispatch_command(
            "bind game-1 bind-ok",
            "stream-1",
            "discord:gm",
        )

        self.assertIn("Bound to DiceFrame game", bound)
        self.assertEqual(self.store.group("stream-1")["language"], "en")

        help_text = await self.bridge._dispatch_command("help", "stream-1", "discord:player")
        self.assertIn("/df join Character Name", help_text)
        self.assertNotIn("加入角色", help_text)

        joined = await self.bridge._dispatch_command("join Erin", "stream-1", "discord:player")
        self.assertIn("Character claimed: Erin", joined)

        status = await self.bridge._dispatch_command("status", "stream-1", "discord:player")
        self.assertIn("Erin status", status)
        self.assertIn("Gold: 4", status)

        payments = await self.bridge._dispatch_command("pay", "stream-1", "discord:player")
        self.assertIn("Pending payments:", payments)
        self.assertIn("/df confirm pay 1", payments)

        confirmed = await self.bridge._dispatch_command("confirm pay 1", "stream-1", "discord:player")
        self.assertEqual(confirmed, "Payment confirmed.")
        self.assertEqual(
            self.bridge._client.payment_resolutions[-1],
            ("game-1", "player-1", "pay-1", True),
        )

        rejected = await self.bridge._dispatch_command("reject pay 1", "stream-1", "discord:player")
        self.assertEqual(rejected, "Payment rejected.")
        self.assertEqual(
            self.bridge._client.payment_resolutions[-1],
            ("game-1", "player-1", "pay-1", False),
        )

        reloaded = BridgeStore(self.path)
        await reloaded.load()
        self.assertEqual(reloaded.group("stream-1")["language"], "en")

    async def test_legacy_binding_without_language_defaults_to_chinese(self):
        self.path.write_text(json.dumps({
            "groups": {
                "stream-1": {
                    "game_key": "game-1",
                    "gm_platform_id": "qq:1",
                    "gm_uid": "gm-1",
                    "roster": [],
                },
            },
            "players": {},
            "recent_commands": {},
        }), encoding="utf-8")

        await self.store.load()

        self.assertEqual(normalize_language(self.store.group("stream-1").get("language")), "zh-CN")
        self.assertIn("群聊指南", self.bridge._bound_help_text(self.store.group("stream-1")))

    async def test_server_check_result_is_localized_and_roll_command_is_informational(self):
        check = {
            "actor_name": "Erin",
            "label": "Stealth Check",
            "dice": "d20",
            "roll": 12,
            "modifier": 2,
            "total": 14,
            "dc": 12,
            "verdict": "成功",
        }
        self.assertIn("d20=12(+2)=14 vs DC 12 → Success", format_check_result(check, "en"))

        await self.store.bind_group("stream-1", "game-1", "qq:gm", "gm-1", [], language="en")
        await self.store.bind_player("stream-1", "qq:player", "player-1")
        message = await self.bridge._dispatch_command("roll", "stream-1", "qq:player")
        self.assertIn("Manual roll confirmation is no longer required", message)

    async def test_existing_chinese_payment_commands_remain_compatible(self):
        await self.store.bind_group(
            "stream-1",
            "game-1",
            "qq:gm",
            "gm-1",
            [
                {"user_id": "gm-1", "character_name": "主持人"},
                {"user_id": "player-1", "character_name": "艾琳"},
            ],
        )
        await self.store.bind_player("stream-1", "qq:player", "player-1")

        payments = await self.bridge._dispatch_command("支付", "stream-1", "qq:player")
        self.assertIn("待处理支付", payments)
        self.assertIn("/df 支付 1", payments)

        confirmed = await self.bridge._dispatch_command("支付 1", "stream-1", "qq:player")
        self.assertEqual(confirmed, "支付已确认。")

        rejected = await self.bridge._dispatch_command("拒绝支付 1", "stream-1", "qq:player")
        self.assertEqual(rejected, "支付已拒绝。")

    def test_manifest_declares_english_locale(self):
        manifest = json.loads((Path(__file__).parents[1] / "_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "0.2.2")
        self.assertEqual(manifest["capabilities"], ["send.text", "send.image"])
        self.assertEqual(manifest["i18n"]["supported_locales"], ["zh-CN", "en"])

    async def test_deleted_game_error_unbinds_stale_chat(self):
        await self.store.bind_group(
            "stream-1",
            "deleted-game",
            "qq:gm",
            "gm-1",
            [],
            language="zh-CN",
        )

        reply = await self.bridge._http_error_reply(
            DiceFrameHTTPError("游戏不存在", status=404, code="GAME_NOT_FOUND"),
            "stream-1",
            "zh-CN",
        )

        self.assertIn("已自动解除绑定", reply)
        self.assertIsNone(self.store.group("stream-1"))
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["groups"], {})
        self.assertEqual(persisted["players"], {})

    def test_command_pattern_accepts_english_commands_case_insensitively(self):
        pattern = DiceFrameBridgePlugin.handle_diceframe._command_pattern
        self.assertIsNotNone(re.fullmatch(pattern, "/df Help"))
        self.assertIsNotNone(re.fullmatch(pattern, "create character"))
        self.assertIsNotNone(re.fullmatch(pattern, "AI character"))
        self.assertIsNotNone(re.fullmatch(pattern, "CONFIRM PAY 1"))
        self.assertIsNotNone(re.fullmatch(pattern, "NO LUCK"))

    async def test_luck_decision_uses_shared_server_endpoint(self):
        await self.store.bind_group(
            "stream-1",
            "game-1",
            "discord:gm",
            "gm-1",
            [{"user_id": "player-1", "character_name": "Erin"}],
            language="en",
        )
        await self.store.bind_player("stream-1", "discord:player", "player-1")
        self.bridge._client.pending_luck = [{
            "check_id": "check-1",
            "actor_uid": "player-1",
            "actor_name": "Erin",
            "luck_cost": 2,
        }]

        reply = await self.bridge._dispatch_command("luck", "stream-1", "discord:player")

        self.assertIn("Spent 2 Luck", reply)
        self.assertEqual(
            self.bridge._client.luck_resolutions,
            [("game-1", "player-1", "check-1", True)],
        )

    def test_language_inference_is_safe_before_runtime_initialization(self):
        bridge = DiceFrameBridgePlugin()
        self.assertEqual(bridge._message_language("stream-1", "help"), "en")
        self.assertEqual(bridge._message_language("stream-1", "帮助"), "zh-CN")

    async def test_maibot_uses_server_side_bridge_extensions_when_available(self):
        sent = []

        class Send:
            async def text(self, text, stream_id):
                sent.append(("text", stream_id, text))

            async def image(self, image_data, stream_id):
                sent.append(("image", stream_id, image_data))

        class Logger:
            def warning(self, *_args):
                pass

        self.bridge.ctx = SimpleNamespace(send=Send(), logger=Logger())
        self.bridge._bridge_extensions_supported = None

        command = await self.bridge._apply_message_extensions(
            platform="qq",
            stream_id="stream-1",
            platform_user_id="qq:user",
            text="plugin",
            language="en",
        )
        await self.bridge._send_reply("stream-1", "original", "en", platform="qq")

        self.assertTrue(command["handled"])
        self.assertEqual(command["outputs"][0]["text"], "handled by extension")
        self.assertEqual(sent[-1], ("text", "stream-1", "changed by extension"))
        self.assertEqual(
            [stage for stage, _payload in self.bridge._client.extension_calls],
            ["before_message", "after_result", "render"],
        )


if __name__ == "__main__":
    unittest.main()
