import json
from astrbot.api.provider import ProviderRequest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from astrbot.api.event import MessageChain
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from openai import AsyncOpenAI
import json_repair
from astrbot.api.star import StarTools
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    UserMessageSegment,
    TextPart,
)
from astrbot.api import AstrBotConfig
import os
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_state() -> Dict[str, Any]:
    now = _utc_now()
    return {
        "core_memory": [],
        "long_term": [],
        "medium_term": [],
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
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.last_update: Dict[str, str] = {}
    # async def initialize(self):
    #     """插件初始化时确保记忆文件存在。"""
    #     _ = self.store.load()

    @filter.on_llm_request()
    async def add_mem_prompt(self, event: AstrMessageEvent, req: ProviderRequest, *_, **__):
        """在发送给大模型的请求中添加记忆提示词。"""
        uid = event.unified_msg_origin
        mem_file_path = StarTools.get_data_dir() / f"memory_store_{uid}.json"
        state = MemoryStore(mem_file_path).load()
        memory_snapshot = json.dumps(state, ensure_ascii=False, indent=2)

        mem_prompt = (
            "\n\n[Memory Info]\n"
            "You have access to the following memory information: core memory, long-term, medium-term, and short-term memories. Use this context when generating responses to maintain consistency and coherence across interactions.\n"
            f"{memory_snapshot}\n"
            "Adjust your responses based on this memory information to ensure they align with your existing knowledge.\n"
        )

        req.system_prompt += f"\n{mem_prompt}"
        # logger.info(f"当前的系统提示词:{req.system_prompt}")

    @filter.command_group("mem")
    def mem(self, t):
        pass
    
    @mem.command("check")
    async def check(self, event: AstrMessageEvent):
        uid = event.unified_msg_origin
        if self.last_update.get(uid) is None:
            await self.context.send_message(uid,MessageChain().message("尚未进行过记忆更新。"))
        else:
            await self.context.send_message(uid,MessageChain().message(f"上次更新内容:\n{self.last_update[uid]}"))
        event.stop_event()

    @mem.command("gen")
    async def gen(self, event: AstrMessageEvent, extra_prompr: str="", use_full: str = ""):
        """生成记忆提示词或应用模型返回的记忆更新。"""

        # use_full = kwargs.get("use_full") if "use_full" in kwargs else (args[0] if args else "")
        mem_result = await self.send_prompt(event, extra_prompt=extra_prompr, full=(str(use_full).strip() == "--full"))
        self.last_update[event.unified_msg_origin] = mem_result
        
        handle_result = self._handle_apply(event, mem_result)
        logger.info(f"应用记忆结果:{handle_result}")
        message_chain = MessageChain().message(handle_result)
        await self.context.send_message(event.unified_msg_origin,message_chain)
        event.stop_event()
    
    @mem.command("help")
    async def help(self, event: AstrMessageEvent):
        yield event.plain_result(self._usage_manual())
        return
    
    @mem.command("rebuild")
    async def mem_rebuild(self, event):
        uid = event.unified_msg_origin
        # mem_file_path = StarTools.get_data_dir() / f"memory_store_{uid}.json"
        mem_path = StarTools.get_data_dir() / f"memory_store_{uid}.json"
        pre_mem_path = StarTools.get_data_dir() / f"memory_store_{uid}_pre.json"
        try:
            os.rename(mem_path, pre_mem_path)
            os.remove(mem_path)
        except Exception as e:
            logger.info(f"发生错误:{e}")
            event.stop_event()

        pre_mem = MemoryStore(pre_mem_path)
        state = pre_mem.load()
        await self.gen(event, extra_prompt=f"这是你之前的记忆，根据这些记忆重构现在的记忆:{state}")

        

    @mem.command("apply")
    async def apply(self, event: AstrMessageEvent):
        """生成记忆提示词或应用模型返回的记忆更新。"""
        raw_message = (event.message_str or "").strip()
        subcommand, payload = self._parse_arguments(raw_message)
            
        result = self._handle_apply(event, payload)
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
            "1. /mem gen 生成给大模型使用的长中短期记忆。使用--full参数可使用全部对话历史。\n"
            "2. /mem check  应用大模型返回的记忆更新结果。\n"
            "建议流程: /mem gen -> 让大模型总结并应用记忆 -> /mem check 查看结果。"
        )

    async def send_prompt(self, event, extra_prompt="", full=False):
        uid = event.unified_msg_origin
        provider_id = await self.context.get_current_chat_provider_id(uid)
        logger.info(f"uid:{uid}")

        #获取会话历史
        conv_mgr = self.context.conversation_manager
        curr_cid = await conv_mgr.get_curr_conversation_id(uid)
        conversation = await conv_mgr.get_conversation(uid, curr_cid)  # Conversation
        history = json.loads(conversation.history) if conversation and conversation.history else []

        #获取人格
        system_prompt = await self.get_persona_system_prompt(uid)

        mem_prompt = self._handle_prompt(event, history, full)
        if extra_prompt != "":
            mem_prompt = extra_prompt + "\n" + mem_prompt

        #发送信息到llm
        sys_msg = f"{system_prompt}"
        provider = self.context.get_using_provider()
        llm_resp = await provider.text_chat(
                prompt=mem_prompt,
                session_id=None,
                contexts=history,
                image_urls=[],
                func_tool=None,
                system_prompt=sys_msg,
            )
        # await conv_mgr.add_message_pair(
        #     cid=curr_cid,
        #     user_message=user_msg,
        #     assistant_message=AssistantMessageSegment(
        #         content=[TextPart(text=llm_resp.completion_text)]
        #     ),
        # )
        return llm_resp.completion_text

    def _handle_prompt(self, event: AstrMessageEvent, history: str, full=False) -> str:
        conversation = history
        # if not conversation:
        #     return "请在 prompt 子命令后附带对话文本，例如 /memory prompt 最近的对话内容。"

        uid = event.unified_msg_origin
        mem_file_path = StarTools.get_data_dir() / f"memory_store_{uid}.json"
        if not mem_file_path.exists() or full:
            task_prompt = "please refresh long-term/medium-term/short-term memory based on the entire conversation.\n"
        else:
            task_prompt = "please refresh long-term/medium-term/short-term memory based on the latest conversation.\n"
        state = MemoryStore(mem_file_path).load()
        logger.info("创建记忆提示词，操作者: %s", uid)
        
        memory_snapshot = json.dumps(state, ensure_ascii=False, indent=2)
        cur_mem_prompt = "your current memories are shown below, make sure that new memory doesn't exist in current memory and delete redunant/outmoded memories everytime.\n" \
        "**[current memories]**\n"
        f"{memory_snapshot}"
        template = (
            task_prompt +
            cur_mem_prompt + 
            self.config.mem_prompt+
            "output JSON with the following sections (each is required and serves a distinct purpose):\n"
            "- summary: concise highlights of any changes across memories.\n"
            "- core_memory: enduring identity/profile/preferences/facts; anchor for consistency and rarely changes.\n"
            "- long_term: durable knowledge/goals worth keeping across many sessions; update cautiously.\n"
            "- medium_term: active themes/tasks spanning recent sessions that aid continuity.\n"
            "- short_term: freshest context from the latest exchanges; can be pruned frequently.\n\n"
            "JSON Format:\n"
            "{\n"
            "  \"summary\": {\n"
            "    \"core_memory_highlights\": \"<概述核心记忆变更>\",\n"
            "    \"long_term_highlights\": \"<概述长期变更>\",\n"
            "    \"medium_term_highlights\": \"<概述中期变更>\",\n"
            "    \"short_term_highlights\": \"<概述短期变更>\"\n"
            "  },\n"
            "  \"core_memory\": {\n"
            "    \"upsert\": [{\n"
            "      \"id\": \"沿用或由系统生成\",\n"
            "      \"content\": \"记忆文本\",\n"
            "      \"category\": \"profile|preference|task|fact\",\n"
            "      \"importance\": 1-5,\n"
            "      \"expires_at\": \"YYYY-MM-DD 或留空\"\n"
            "    }],\n"
            "    \"delete\": [\"要删除的 id\"]\n"
            "  },\n"
            "  \"long_term\": { 与 core_memory 相同结构 },\n"
            "  \"medium_term\": { 与 core_memory 相同结构 },\n"
            "  \"short_term\": { 与 core_memory 相同结构 }\n"
            "}\n\n"
            "若无需操作，请返回空的 upsert/delete 并说明理由。"
        )
        # logger.info(f"记忆提示词内容:{template}")
        return template

    def _handle_apply(self, event, payload_text: str) -> str:
        payload_text = payload_text.strip()
        if not payload_text:
            return "请提供大模型返回的 JSON 内容。"

        json_text = self._extract_json_block(payload_text)
        if json_text is None:
            return "未能解析 JSON，请直接粘贴模型输出或 ```json ``` 代码块。"

        try:
            # json_repair.loads() returns parsed object directly (dict/list)
            operations = json_repair.loads(json_text)
            logger.info("JSON parsed successfully: %s", operations)
        except Exception as e:
            logger.warning("JSON repair failed, fallback to standard parser: %s", e)
            try:
                operations = json.loads(json_text.strip())
            except json.JSONDecodeError as exc:
                return f"JSON parsing failed: {exc}"

        uid = event.unified_msg_origin
        mem_file_path = StarTools.get_data_dir() / f"memory_store_{uid}.json"
        store = MemoryStore(mem_file_path)
        state = store.load()

        report = self._apply_operations(state, operations)
        store.save(state)
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

        
        core_result = self._upsert_and_delete(
            state.setdefault("core_memory", []), operations.get("core_memory", {}), True, now
        )
        lt_result = self._upsert_and_delete(
            state.setdefault("long_term", []), operations.get("long_term", {}), True, now
        )
        mt_result = self._upsert_and_delete(
            state.setdefault("medium_term", []), operations.get("medium_term", {}), True, now
        )
        st_result = self._upsert_and_delete(
            state.setdefault("short_term", []), operations.get("short_term", {}), False, now
        )

        summary_block = operations.get("summary")
        if isinstance(summary_block, dict):
            state.setdefault("metadata", {}).setdefault("summary", {}).update(summary_block)

        state.setdefault("metadata", {})["last_update"] = now

        report_lines.append(self._format_report_line("核心记忆", core_result))
        report_lines.append(self._format_report_line("长期", lt_result))
        report_lines.append(self._format_report_line("中期", mt_result))
        report_lines.append(self._format_report_line("短期", st_result))

        if isinstance(summary_block, dict) and summary_block:
            core_high = summary_block.get("core_memory_highlights", "无")
            lt_high = summary_block.get("long_term_highlights", "无")
            mt_high = summary_block.get("medium_term_highlights", "无")
            st_high = summary_block.get("short_term_highlights", "无")
            report_lines.append("概述:\n- 核心: " + core_high + "\n- 长期: " + lt_high + "\n- 中期: " + mt_high + "\n- 短期: " + st_high)

        # report_lines.append(f"记忆文件位置: {state.path}")

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

    async def get_persona_system_prompt(self, session: str) -> str:
        """获取人格系统提示词

        Args:
            session: 会话ID

        Returns:
            人格系统提示词
        """
        base_system_prompt = ""
        try:
            # 尝试获取当前会话的人格设置
            uid = session  # session 就是 unified_msg_origin
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(
                uid
            )

            # 获取默认人格设置
            default_persona_obj = self.context.provider_manager.selected_default_persona

            if curr_cid:
                conversation = await self.context.conversation_manager.get_conversation(
                    uid, curr_cid
                )

                if (
                    conversation
                    and conversation.persona_id
                    and conversation.persona_id != "[%None]"
                ):
                    # 有指定人格，尝试获取人格的系统提示词
                    personas = self.context.provider_manager.personas
                    if personas:
                        for persona in personas:
                            if (
                                hasattr(persona, "name")
                                and persona.name == conversation.persona_id
                            ):
                                base_system_prompt = getattr(persona, "prompt", "")
                                
                                break

            # 如果没有获取到人格提示词，尝试使用默认人格
            if (
                not base_system_prompt
                and default_persona_obj
                and default_persona_obj.get("prompt")
            ):
                base_system_prompt = default_persona_obj["prompt"]
                

        except Exception as e:
            logger.warning(f"获取人格系统提示词失败: {e}")

        return base_system_prompt

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
