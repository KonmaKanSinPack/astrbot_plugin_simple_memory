# simple-memory

一个帮助 AstrBot 将对话整理成「核心 / 长期 / 中期记忆」的插件。插件会：

- 维护 `memory_store_{uid}.json`，保存结构化记忆。
- 在请求 LLM 时自动注入当前记忆快照。
- 生成记忆更新提示词，并接收模型返回的 JSON 进行增删改。
- 提供 `update_one_memory` 工具，允许模型按条更新记忆。

## 功能概览

- `on_llm_request`：自动把当前记忆加入系统提示词。
- `/mem gen [extra_prompt] [--full]`：生成并应用一次记忆更新。
- `/mem check`：查看上次记忆更新的原始内容。
- `/mem rebuild`：基于备份记忆重构当前记忆。
- `/mem help`：查看命令说明。
- `/mem apply <JSON>`：手动应用一段 JSON 更新（支持代码块）。

## 命令说明

1. `/mem gen`
根据最新对话生成记忆更新并立即应用。

2. `/mem gen 你的额外要求 --full`
- `extra_prompt`：临时附加到记忆生成提示词前。
- `--full`：基于完整会话历史重建记忆，而不是仅最近对话。

3. `/mem check`
查看上一轮 `/mem gen` 的返回内容。

4. `/mem apply <payload>`
手动应用 JSON 更新。`payload` 支持：
- 纯 JSON 文本。
- ` ```json ... ``` ` 代码块。
- 包含说明文字的混合文本（插件会自动提取第一段有效 JSON）。

## LLM 工具：`update_one_memory`

插件暴露了 `update_one_memory` 工具用于按条修改记忆，参数如下：

- `memory_type`：`core_memory | long_term | medium_term`
- `action_type`：`upsert | delete`
- `id`：记忆 ID（必填）
- `content`：记忆内容（`upsert` 时必填）
- `category`：`profile | preference | task | fact`
- `importance`：1-5
- `expires_at`：`YYYY-MM-DD` 或空字符串

当前版本增加了参数校验：
- 非法 `memory_type` 会直接返回错误。
- 缺少 `id` 会直接返回错误。
- `upsert` 缺少 `content` 会直接返回错误。

## JSON 结构约定

```json
{
	"summary": {
		"core_memory_highlights": "<核心变化概述>",
		"long_term_highlights": "<长期变化概述>",
		"medium_term_highlights": "<中期变化概述>"
	},
	"core_memory": {
		"upsert": [
			{
				"id": "可留空，由插件生成",
				"content": "记忆内容",
				"category": "profile|preference|task|fact",
				"importance": 1,
				"expires_at": "2026-12-31"
			}
		],
		"delete": ["id-to-delete"]
	},
	"long_term": {
		"upsert": [],
		"delete": []
	},
	"medium_term": {
		"upsert": [],
		"delete": []
	}
}
```

若无需更新，保持 `upsert`、`delete` 为空数组，并在 `summary` 中说明原因。

## 记忆文件

- 文件名：`memory_store_{uid}.json`
- 路径：`get_astrbot_data_path()` 返回目录下
- 手动删除后会在下次加载时按默认结构自动重建

## 支持

[AstrBot 帮助文档](https://astrbot.app)
