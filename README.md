# 盛世豪莱尔抽奖机器人

Telegram 群管理基础版：群抽奖、入群欢迎、自动回复、违禁词、禁言和群组统计。

## 群内命令

```text
/newdraw 奖品 | 获奖人数 | YYYY-MM-DD HH:MM
/redraw 抽奖ID
/welcome on 或 /welcome off
/setwelcome 欢迎 {name}
/reply add 关键词 | 回复内容
/reply del 关键词
/banword add 违禁词
/banword del 违禁词
/mute 10m （回复成员消息后使用）
/stats
```

示例：

```text
/newdraw 100 USDT 奖励 | 3 | 2026-10-21 20:00
```

时间以 `Asia/Dubai`（迪拜时间）为准。

## Railway 环境变量

```text
BOT_TOKEN=从BotFather获取的Token
TIMEZONE=Asia/Dubai
DATABASE_PATH=/data/lucky_draw.sqlite3
```

请在 Railway 为服务添加 Volume，并把挂载路径设为 `/data`，以保留抽奖记录。

为使自动回复和违禁词生效，请在 BotFather 的 Bot Settings → Group Privacy 中关闭群隐私模式；同时把机器人设为群管理员并授予删除消息、限制成员权限。
