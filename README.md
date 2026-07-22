# DiceFrame Bridge

DiceFrame Bridge 是 DiceFrame 的聊天桥接插件，用来把 MaiBot 当前聊天流连接到 DiceFrame 跑团服务。安装后，在聊天里使用 `/df 绑定`、`/df 加入`、`/df 状态`、`/df 掷骰` 等指令，把群聊里的跑团操作转发给 DiceFrame HTTP API。

[DiceFrame](https://github.com/diceframe/diceframe) 是一个 AI 驱动的 TRPG 跑团应用，提供 Web 跑团界面、角色管理、剧情推进、掷骰确认、地图/前情/私密日志等能力。


## 功能

- 绑定当前聊天流到 DiceFrame 对局
- 认领已有角色
- 提交自然语言行动
- 确认系统掷骰
- 查看状态、前情、地图、感知/私密日志
- 处理待支付请求
- GM 推进回合、暂离/回来

## 安装

1. 将本目录放入 MaiBot 的 `plugins/` 目录。
2. 打开 DiceFrame 的“设置 → Bot API”，复制“DiceFrame 服务地址”和“Bot API Token”。
3. 在 MaiBot WebUI 的插件配置里打开“DiceFrame 服务”，填写 `diceframe.base_url` 和 `diceframe.bot_token`；也可以直接编辑本插件的 `config.toml`。
4. 启用插件并发送 `/df 测试连接`。该命令会同时检查网络连通性和 Bot API Token。

## DiceFrame 配置

先启动 DiceFrame Web 服务。插件默认连接：

```text
http://127.0.0.1:18000
```

如果 MaiBot / DiceFrame Bridge 跑在 NAS、Docker 或另一台机器上，`127.0.0.1` 只代表插件所在环境本机；这时需要把 `diceframe.base_url` 改成插件能访问到的 DiceFrame 内网地址，例如 `http://192.168.1.x:18000` 或容器网络里的服务名。

配置项：

- `diceframe.base_url`: MaiBot 插件访问 DiceFrame API 的内部服务地址，可使用内网地址或容器服务名。
- `diceframe.bot_token`: DiceFrame Bot API Token。在 DiceFrame 的“设置 → Bot API”中显示并复制；它不是 NapCat 的 access_token。
- `diceframe.public_base_url`: 可选的玩家网页地址覆盖值。留空时自动读取 DiceFrame“设置 → 分享链接地址”；仅在该设置也为空时才使用 `diceframe.base_url`。
- `diceframe.request_timeout_sec`: DiceFrame HTTP 请求超时时间；剧情生成较慢时可适当增加。
- `diceframe.shared_core_path`: 可选，DiceFrame 源码根目录；仅源码联调时需要，普通安装保持为空。
- `commands.prefixes`: 兜底命令前缀，默认 `/df`、`/diceframe`、`跑团`。
- `commands.trigger_mode`: 推荐保持 `prefix_only`，只处理显式 `/df`、`/diceframe` 或 `跑团` 命令。
- `commands.require_mention_for_bare_commands`: 无前缀裸指令必须来自 @/提到 MaiBot 的消息，默认开启，避免误吞普通聊天。
- `commands.advance_allowed_users`: 额外允许使用“推进/下一轮”的用户 ID。通常留空即可；绑定该局的 DiceFrame GM 会自动允许。若 MaiBot 或上游桥接器已经配置了聊天白名单，一般不需要在这里重复配置。

首次使用DiceFrame Bridge，需要从“设置 → Bot API”复制同一个 Token。重新生成 Token 后，旧值立即失效，必须同步更新 MaiBot 插件配置。

## 使用

GM 先在 DiceFrame 网页里打开当前游戏，生成一次性 Bot 绑定凭证，然后在 MaiBot 所在聊天流发送：

```text
/df 绑定 <game_key> <一次性凭证>
```

玩家认领角色：

```text
/df 加入 艾琳
```

提交行动：

```text
/df 我调查四周
```

常用命令：

```text
/df 帮助
/df 测试连接
/df 邀请
/df 新建角色
/df 车卡
/df AI车卡
/df 状态
/df 前情
/df 地图
/df 感知
/df 掷骰
/df 暂离
/df 回来
/df 支付
/df 支付 1
/df 拒绝支付 1
/df 推进
/df 下一轮
/df 解绑
```

说明：自然语言行动可以直接写成 `/df 我调查四周`。

## 注意

- 本插件只通过 DiceFrame HTTP API 工作，不直接读写 DiceFrame 存档。
- 绑定、玩家映射和去重数据保存在 MaiBot 插件数据目录。
- `127.0.0.1` 永远指 MaiBot 插件所在环境。DiceFrame 在另一台设备或另一个容器时，请使用可达的局域网地址或容器服务名。
- “Bot 服务未授权”表示 `diceframe.bot_token` 缺失、填错或已被重新生成；“未配置 DiceFrame 服务地址”表示 `diceframe.base_url` 为空。
