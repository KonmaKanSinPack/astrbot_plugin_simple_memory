import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from openai import AsyncOpenAI
# MEMORY_FILE = Path(__file__).with_name("memory_store.json")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_state() -> Dict[str, Any]:
    now = _utc_now()
    return {
        "long_term": [],
        "short_term": [],
        "metadata": {
            "version": 1,
            "created_at": now,
            "last_update": now,
            "summary": {},
        },
    }


class MemoryStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            state = _default_state()
            self.save(state)
            return state
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - 防止文件损坏导致崩溃
            logger.error("读取记忆文件失败，将使用默认结构: %s", exc)
            state = _default_state()
            self.save(state)
            return state

    def save(self, state: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )


@dataclass
class UpsertResult:
    added: int = 0
    updated: int = 0
    deleted: int = 0


@register("simple_memory", "兔子", "为大模型提供结构化记忆提示词", "1.0.0")
class SimpleMemoryPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # self.store = MemoryStore(MEMORY_FILE)
        self.context = context

    async def initialize(self):
        """插件初始化时确保记忆文件存在。"""
        _ = self.store.load()

    @filter.command_group("mem")
    def mem(self, t):
        pass

    @mem.command("")
    def default(self, event: AstrMessageEvent):
        """生成记忆提示词或应用模型返回的记忆更新。"""
        user_name = event.get_sender_name()
        
        raw_message = (event.message_str or "").strip()
        subcommand, payload = self._parse_arguments(raw_message)

        prompt = self._handle_prompt(payload, event)
        yield event.plain_result(prompt)
    
    @mem.command("help")
    async def help(self, event: AstrMessageEvent):
        yield event.plain_result(self._usage_manual())
        return

    @mem.command("apply")
    async def apply(self, event: AstrMessageEvent):
        """生成记忆提示词或应用模型返回的记忆更新。"""
        raw_message = (event.message_str or "").strip()
        subcommand, payload = self._parse_arguments(raw_message)
            
        result = self._handle_apply(payload)
        yield event.plain_result(result)
        return

    def _parse_arguments(self, message: str) -> Tuple[str, str]:
        #TODO: 这个不是我要的效果，估计废弃
        normalized = message.lstrip("/").strip()
        if normalized.lower().startswith("memory"):
            normalized = normalized[6:].strip()

        if not normalized:
            return "help", ""

        parts = normalized.split(maxsplit=1)
        head = parts[0].lower()
        tail = parts[1].strip() if len(parts) > 1 else ""

        if head in {"prompt", "p"}:
            return "prompt", tail
        if head in {"apply", "a"}:
            return "apply", tail

        return "prompt", normalized

    def _usage_manual(self) -> str:
        return (
            "记忆指令使用方式:\n"
            "1. /memory prompt <对话记录> 生成给大模型使用的提示词。\n"
            "2. /memory apply <JSON> 应用大模型返回的记忆更新结果。\n"
            "建议流程: prompt -> 将提示词贴给大模型 -> 把模型 JSON 回复交给 apply。"
        )

    def _handle_prompt(self, conversation: str, event: AstrMessageEvent) -> str:
        conversation = conversation.strip()
        if not conversation:
            return "请在 prompt 子命令后附带对话文本，例如 /memory prompt 最近的对话内容。"

        user_name = event.get_sender_name()
        mem_file_path = Path(__file__).with_name(f"memory_store_{user_name}.json")
        state = MemoryStore(mem_file_path).load()
        logger.info("创建记忆提示词，操作者: %s", event.get_sender_name())

        memory_snapshot = json.dumps(state, ensure_ascii=False, indent=2)
        template = (
            "你是一名 AstrBot 的记忆管理员，任务是基于最新对话刷新长期/短期记忆。\n"
            "请阅读以下内容:\n\n"
            "[对话记录]\n"
            f"{conversation}\n\n"
            "[现有记忆 JSON]\n"
            f"{memory_snapshot}\n\n"
            "[你的目标]\n"
            "1. 判断需要新增、更新或删除的记忆点。\n"
            "2. 长期记忆用于稳定画像/长久事实；短期记忆用于阶段性任务和暂存信息。\n"
            "3. 控制记忆数量，删除过期或冲突内容。\n"
            "4. 输出 JSON，字段如下: summary、long_term、short_term。\n\n"
            "JSON 字段格式:\n"
            "{\n"
            "  \"summary\": {\n"
            "    \"long_term_highlights\": \"<概述长期变更>\",\n"
            "    \"short_term_highlights\": \"<概述短期变更>\"\n"
            "  },\n"
            "  \"long_term\": {\n"
            "    \"upsert\": [{\n"
            "      \"id\": \"沿用或留空由系统生成\",\n"
            "      \"content\": \"记忆文本\",\n"
            "      \"category\": \"profile|preference|task|fact\",\n"
            "      \"importance\": 1-5,\n"
            "      \"expires_at\": \"YYYY-MM-DD 或留空\"\n"
            "    }],\n"
            "    \"delete\": [\"要删除的 id\"]\n"
            "  },\n"
            "  \"short_term\": { 与 long_term 相同结构 }\n"
            "}\n\n"
            "若无需操作，请返回空的 upsert/delete 并说明理由。"
        )

        return (
            "以下为可直接投喂给大模型的 Prompt:\n"
            "----------------------------------------\n"
            f"{template}\n"
            "----------------------------------------"
        )

    def _handle_apply(self, payload_text: str) -> str:
        payload_text = payload_text.strip()
        if not payload_text:
            return "请提供大模型返回的 JSON 内容。"

        json_text = self._extract_json_block(payload_text)
        if json_text is None:
            return "未能解析 JSON，请直接粘贴模型输出或 ```json ``` 代码块。"

        try:
            operations = json.loads(json_text)
        except json.JSONDecodeError as exc:
            return f"JSON 解析失败: {exc}"

        state = self.store.load()
        report = self._apply_operations(state, operations)
        self.store.save(state)
        return report

    def _extract_json_block(self, text: str) -> Optional[str]:
        stripped = text.strip()
        if not stripped:
            return None
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if len(lines) >= 3 and lines[-1].startswith("```"):
                return "\n".join(lines[1:-1]).strip()
            if stripped.startswith("```json"):
                return "\n".join(lines[1:-1]).strip()
            return None
        if stripped[0] in "[{" and stripped[-1] in "]}":
            return stripped
        return None

    def _apply_operations(self, state: Dict[str, Any], operations: Dict[str, Any]) -> str:
        now = _utc_now()
        report_lines: List[str] = []

        lt_result = self._upsert_and_delete(
            state.setdefault("long_term", []), operations.get("long_term", {}), True, now
        )
        st_result = self._upsert_and_delete(
            state.setdefault("short_term", []), operations.get("short_term", {}), False, now
        )

        summary_block = operations.get("summary")
        if isinstance(summary_block, dict):
            state.setdefault("metadata", {}).setdefault("summary", {}).update(summary_block)

        state.setdefault("metadata", {})["last_update"] = now

        report_lines.append(self._format_report_line("长期", lt_result))
        report_lines.append(self._format_report_line("短期", st_result))

        if isinstance(summary_block, dict) and summary_block:
            lt_high = summary_block.get("long_term_highlights", "无")
            st_high = summary_block.get("short_term_highlights", "无")
            report_lines.append("概述:\n- 长期: " + lt_high + "\n- 短期: " + st_high)

        report_lines.append(f"记忆文件位置: {self.store.path}")

        return "记忆已更新:\n" + "\n".join(report_lines)

    def _upsert_and_delete(
        self,
        bucket: List[Dict[str, Any]],
        operations: Dict[str, Any],
        is_long_term: bool,
        timestamp: str,
    ) -> UpsertResult:
        result = UpsertResult()
        index = {item.get("id"): item for item in bucket if item.get("id")}

        upserts = operations.get("upsert") or []
        if not isinstance(upserts, list):
            upserts = []

        for raw_entry in upserts:
            if not isinstance(raw_entry, dict):
                continue
            content = (raw_entry.get("content") or "").strip()
            if not content:
                continue

            entry = raw_entry.copy()
            entry["content"] = content
            entry["updated_at"] = timestamp
            entry.setdefault("category", "fact" if is_long_term else "task")
            entry.setdefault("importance", 3)

            entry_id = entry.get("id") or self._generate_entry_id(is_long_term)
            entry["id"] = entry_id

            if entry_id in index:
                entry.setdefault("created_at", index[entry_id].get("created_at", timestamp))
                index[entry_id].update(entry)
                result.updated += 1
            else:
                entry.setdefault("created_at", timestamp)
                index[entry_id] = entry
                result.added += 1

        deletes = operations.get("delete") or []
        if not isinstance(deletes, list):
            deletes = []

        for entry_id in deletes:
            if entry_id in index and entry_id:
                del index[entry_id]
                result.deleted += 1

        bucket.clear()
        bucket.extend(index.values())
        return result

    async def get_all_conversation(self, event: AstrMessageEvent) -> str:
        uid = event.unified_msg_origin
        conv_mgr = self.context.conversation_manager
        curr_cid = await conv_mgr.get_curr_conversation_id(uid)
        conversation = await conv_mgr.get_conversation(uid, curr_cid)  # Conversation
        return conversation.history

    def _generate_entry_id(self, is_long_term: bool) -> str:
        prefix = "lt" if is_long_term else "st"
        return f"{prefix}-{int(datetime.now(timezone.utc).timestamp())}"

    def _format_report_line(self, label: str, result: UpsertResult) -> str:
        return f"- {label}: 新增 {result.added} 条，更新 {result.updated} 条，删除 {result.deleted} 条"

    async def terminate(self):
        """插件销毁时无需特殊处理。"""
