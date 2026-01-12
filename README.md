# simple-memory

一个帮助 AstrBot 将对话整理成「长期 / 短期记忆」的插件。插件会：

- 维护 `memory_store.json`，保存结构化记忆与元信息。
- 输出提示词，指导大模型根据对话编写记忆增删改 JSON。
- 接收模型 JSON，自动更新本地记忆并给出报告。

## 使用方法
1. 使用`/memory gen`自动根据最新对话内容生成并存储记忆；添加参数`/memory gen --full`可根据全部对话内容生成记忆
2. 在 AstrBot 中启用插件后发送 `/memory help` 查看指令说明。
3. 使用`/memory check`查看上次更新的内容（增删改）
4. 新增配置文件，现在可以自己自定义记忆生成prompt了

## TODO
1. 使用 `/memory prompt <对话记录>` 生成一段可直接投喂给大模型的 Prompt，里面包含当前记忆快照与要求的 JSON 格式。（未实装）
2. 等待大模型返回更新 JSON 后，使用 `/memory apply <JSON 或 ```json 代码块>`，插件会完成新增 / 更新 / 删除并汇总结果。（未实装）

## JSON 结构约定

```json
{
	"summary": {
		"long_term_highlights": "<长期变化概述>",
		"short_term_highlights": "<短期变化概述>"
	},
	"long_term": {
		"upsert": [
			{
				"id": "可留空，由插件生成",
				"content": "记忆内容",
				"category": "profile|preference|task|fact",
				"importance": 1,
				"expires_at": "2025-12-31"
			}
		],
		"delete": ["lt-123"]
	},
	"medium_term: { 同上 }"
	"short_term: { 同上 }"
}
```

若无需更新，保持 `upsert`、`delete` 为空数组即可。

## 记忆文件

- 默认存储在插件根目录的 `memory_store.json`。
- `metadata.last_update` 会记录最近一次 apply 时间，方便审计。
- 可直接删除该文件以重置所有记忆，插件会在下次运行时重新生成。

## 支持

[AstrBot 帮助文档](https://astrbot.app)
