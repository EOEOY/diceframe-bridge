# DiceFrame Bridge

[中文](README.md) | English

DiceFrame Bridge connects the current MaiBot chat stream to a DiceFrame tabletop game. It forwards explicit `/df` commands to the DiceFrame HTTP API for game binding, character claims, actions, checks, status, recap, maps, payments, and GM progression.

## Features

- Bind a MaiBot chat stream to a DiceFrame game.
- Claim an existing DiceFrame character.
- Submit natural-language actions and confirm server-side rolls.
- View character status, recap, map, and private character information.
- Review and resolve pending payments.
- Advance rounds as the bound GM or an authorized user.
- Mark a character away or back.
- Follow the bound game's Chinese or English language.
- Use server-installed custom commands, reply hooks, images, and card renderers when DiceFrame exposes the Bot Bridge extension protocol.

## Installation

1. Place this directory under MaiBot's `plugins/` directory.
2. In DiceFrame, open Settings → Bot API and copy the DiceFrame service URL and Bot API Token.
3. In MaiBot plugin settings, enable DiceFrame Bridge and configure `diceframe.base_url` and `diceframe.bot_token`.
4. Send `/df ping` to verify both the service URL and token.

The plugin uses the declared `aiohttp` dependency and requests `send.text` plus `send.image`. It does not read or modify DiceFrame save files directly.

## Bot Bridge Extension Compatibility

The plugin checks `/api/bot/ping` for DiceFrame's Bot Bridge extension protocol. When available, `/df` commands and final replies pass through server-installed `bot-extension` plugins, allowing community extensions to add commands, transform text, or return images and cards. DiceFrame exposes images through a Bot-authenticated asset route; this plugin downloads and sends them through MaiBot's `send.image` capability.

With an older DiceFrame release, the plugin keeps its existing commands and text presentation without requiring configuration changes or rebinding. It also falls back to the built-in behavior when an extension fails.

## Configuration

- `plugin.enabled`: enables the bridge.
- `diceframe.base_url`: internal DiceFrame HTTP address reachable from MaiBot.
- `diceframe.bot_token`: global Bot API Token copied from DiceFrame Settings → Bot API.
- `diceframe.public_base_url`: optional player-facing web address. When empty, the plugin reads DiceFrame's sharing address and finally falls back to `base_url`.
- `diceframe.request_timeout_sec`: HTTP timeout; increase it if story generation needs more time.
- `commands.prefixes`: explicit command prefixes; defaults to `/df`, `/diceframe`, and `跑团`.
- `commands.allow_mentioned_bare_commands`: compatibility option for commands after mentioning MaiBot. Explicit prefixes are recommended to avoid triggering MaiBot's normal chat reply.
- `commands.command_dedup_window_sec`: ignores repeated commands from the same stream and user during this window.
- `commands.max_reply_chars`: maximum text length per reply.
- `commands.advance_allowed_users`: additional scoped user IDs allowed to advance rounds.

When MaiBot runs in Docker, on a NAS, or on another machine, `127.0.0.1` refers to the MaiBot environment. Use a LAN address or container service name that can reach DiceFrame.

## Usage

The GM first generates a one-time Bot binding command on the DiceFrame game page:

```text
/df bind <game_key> <one-time-token>
```

The binding response includes the DiceFrame game's language. The plugin stores it with the chat stream and uses it for help, status, recap, map, payment, and error messages.

Common English commands:

```text
/df help
/df invite
/df create character
/df AI character
/df join Erin
/df status
/df recap
/df map
/df sense
/df roll
/df pay
/df confirm pay 1
/df reject pay 1
/df away
/df back
/df advance
/df unbind
/df I inspect the area
```

Chinese games continue to accept the existing Chinese commands. Bindings created by an older plugin version have no stored language and therefore remain Chinese; rebind an English game once to store its language.

## Notes and Troubleshooting

- Regenerating the DiceFrame Bot API Token invalidates the old token, so update the plugin configuration as well.
- A one-time game binding token expires immediately after successful use.
- “Bot service authentication failed” normally means the Bot API Token is missing, incorrect, or was regenerated.
- If a public player link points to the wrong host, set `diceframe.public_base_url`.
- AI character creation through a multi-step private-message wizard is not implemented in the MaiBot bridge; use DiceFrame's web character creator.
- Persistent bindings and deduplication data are stored under the MaiBot plugin data directory. The plugin closes its HTTP session when unloaded.
