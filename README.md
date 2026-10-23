# 盛世豪莱尔积分抽奖机器人 V2

V2 保留原有抽奖、欢迎词、自动回复、违禁词、禁言和统计，并新增积分体系。

## 新增功能

- 每日签到积分 + 成员正常发言积分自动累计；可设置发言计分间隔，减少刷分。
- 本周、本月、总积分排行榜。
- 积分门槛抽奖、自动开奖、重抽、抽奖历史。
- “我的积分”、排行榜、当前抽奖等 Telegram 按钮入口。
- 兼容 V1 的 SQLite 数据库，升级不会清空原来的抽奖记录。

## 命令

成员：

```text
/checkin
/points
/rank
/rank month
/rank total
```

管理员：

```text
/newdraw 奖品 | 获奖人数 | YYYY-MM-DD HH:MM | 最低积分
/redraw 抽奖ID
/pointsrule 每次积分 | 间隔秒数
/addpoints 分数 原因      （回复成员消息后使用）
/welcome on 或 /welcome off
/setwelcome 欢迎 {name}
/reply add 关键词 | 回复内容
/reply del 关键词
/banword add 违禁词
/banword del 违禁词
/mute 10m                （回复成员消息后使用）
/stats
```

示例：

```text
/newdraw 100 USDT 奖励 | 3 | 2026-10-25 20:00 | 20
/pointsrule 1 | 60
```

时间按 `Asia/Dubai`（迪拜时间）处理。

## 部署变量

```text
BOT_TOKEN=从BotFather获取的Token
TIMEZONE=Asia/Dubai
DATABASE_PATH=/root/shengshi-lucky-draw-bot/lucky_draw.sqlite3
```

升级前先备份服务器上的 `lucky_draw.sqlite3`。要让积分、自动回复、违禁词生效，需要在 BotFather 关闭群隐私模式；使用自动删除和禁言时，需要为机器人开启“删除消息”和“限制成员”权限。
