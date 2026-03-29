import json
from astrbot.api.provider import ProviderRequest
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from astrbot.api.event import MessageChain
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from openai import AsyncOpenAI
import json_repair
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    UserMessageSegment,
    TextPart,
)
from astrbot.api import AstrBotConfig

import os
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_state() -> Dict[str, Any]:
    now = _utc_now()
    return {
        "core_memory": [],
        "long_term": [],
        "medium_term": [],
        # "metadata": {
        #     "version": 1,
        #     "created_at": now,
        #     "last_update": now,
        #     "summary": {},
        # },
    }


class MemoryStore:
    def __init__(self, path: str):
        self.path = Path(path)

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

class UserRoster:
    def __init__(self: str):
        path = get_astrbot_data_path() + f"user_roster.json"
        self.path = Path(path)
        self.id_dict = self.load()

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            state = {}
            self.save(state)
            return state
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - 防止文件损坏导致崩溃
            logger.error("读取UserRoster文件失败，将使用默认结构: %s", exc)
            state = {}
            self.save(state)
            return state
    
    def save(self, state: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def update(self, k, v=None, delete=False):
        if not delete:  
            self.id_dict[k] = v
            self.save(self.id_dict)
        else:
            if k in self.id_dict:
                del self.id_dict[k]
                self.save(self.id_dict)
        logger.info(f"当前字典：{self.id_dict}")

    def check(self):
        return self.id_dict

@dataclass
class UpsertResult:
    added: int = 0
    updated: int = 0
    deleted: int = 0


@register("simple_memory", "兔子", "为大模型提供结构化记忆提示词", "1.2.0")
class SimpleMemoryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.use_global = self.config.get("use_global", True)
        self.last_update: Dict[str, str] = {}
        self.user_roster = UserRoster()
    # async def initialize(self):
    #     """插件初始化时确保记忆文件存在。"""
    #     _ = self.store.load()

    def process_mem_info(self, mem_snapshot: Dict[str, Any], id_list=["global"]) -> str:
        """将记忆快照转换为字符串格式，供提示词使用。"""
        
        final_mem_info = []
        for mem_type in ["core_memory", "long_term", "medium_term"]:
            filtered_entries = []
            if mem_type not in mem_snapshot:
                continue
            mem_entries = mem_snapshot.get(mem_type, [])
            id_mem = {id_: [] for id_ in id_list}
            for entry in mem_entries:
                if entry.get("subject_id") in id_list:
                    id_mem[entry.get("subject_id")].append(f"- memory_id:{entry.get('memory_id')}, {entry.get('content')})")
            
            filtered_entries.extend(f"<subject_id: {id_}>\n" + "\n".join(entries) + "\n</subject_id>\n" for id_, entries in id_mem.items() if entries)
            final_mem_info.append(f"{mem_type}:\n" + "\n".join(filtered_entries) + "\n")

        if not final_mem_info:
            return "No relevant memories found."
        else:
            return "<Relevant memories>\n" + "\n".join(final_mem_info) + "\n</Relevant memories>"
    

    @filter.on_llm_request()
    async def add_mem_prompt(self, event: AstrMessageEvent, req: ProviderRequest, *_, **__):
        """在发送给大模型的请求中添加记忆提示词。"""
        uid = event.unified_msg_origin
        subject_id = uid.split(":")[-1]
        msg_type = uid.split(":")[-2]
        sender_name = event.get_sender_name()
        if msg_type == "GroupMessage":
            id_list = ["global", subject_id, self.user_roster.id_dict.get(sender_name, "")]
        else:
            id_list = ["global", subject_id]  
            if sender_name not in self.user_roster.id_dict and msg_type != "GroupMessage":
                self.user_roster.update(sender_name, subject_id)

        mem_file_path = get_astrbot_data_path() + f"memory_store_{uid}.json" if not self.use_global else get_astrbot_data_path() + f"memory_store_global.json"  
        state = MemoryStore(mem_file_path).load()
        state.pop("metadata", None)
        core_mem = state.get("core_memory", [])
        # logger.info(f"原始记忆快照_core_memory:{core_mem}")
        core_mem_list = []
        for entry in core_mem:
            if entry.get("content"):
                core_mem_list.append(f"- memory_id:{entry.get('memory_id')}, {entry.get('content')}, subject_id: {entry.get('subject_id')})")
        core_mem_info = "\n".join(core_mem_list)
        state.pop("core_memory", None)
        # memory_snapshot = json.dumps(state, ensure_ascii=False, indent=2)

        # core_mem = self.process_mem_info(core_mem, id_list=id_list)
        memory_snapshot = self.process_mem_info(state, id_list=id_list)
        ori_system_prompt = req.system_prompt or ""
        # logger.info(f"原系统提示词_SimpleMemory:{ori_system_prompt}")

        # 先计算出当前的具体身份信息
        current_user_id = subject_id if msg_type != "GroupMessage" else self.user_roster.id_dict.get(sender_name, "unknown")
        current_group_id = subject_id if msg_type == "GroupMessage" else "None (Private Chat)"
        
        # 组装带有强制约束的 Prompt
        mem_prompt = (
            "\n\n====================\n"
            "### [CURRENT CHAT CONTEXT] ###\n"
            f"- 当前正在对你说话的用户名字 (Sender Name): {sender_name}\n"
            f"- 当前用户的专属 ID (User ID): {current_user_id}\n"
            f"- 当前所在的群组 ID (Group ID): {current_group_id}\n\n"
            
            "### [MEMORY SYSTEM RULES - STRICT] ###\n"
            "1. 你拥有被 <subject_id: xxx> 标记的分类记忆。\n"
            f"2. 极其重要：除了 global 记忆外，你只能将带有 <subject_id: {current_user_id}> 或 <subject_id: {current_group_id}> 的记忆应用到当前用户身上！\n"
            f"3. 绝对禁止将其他用户的记忆（如提到其他 subject_id 的内容）当作当前用户 ({sender_name}) 的经历！如果记忆里的 subject_id 与当前 User ID 不匹配，说明那是别人的事，请保持客观，不要张冠李戴。\n\n"
            
            "### [RETRIEVED MEMORIES] ###\n"
            f"<core_memory>\n{core_mem_info}\n</core_memory>\n"
            f"{memory_snapshot}\n"
            "====================\n"
        )

        req.system_prompt = ori_system_prompt +f"\n{mem_prompt}"
        logger.info(f"当前的系统提示词_SimpleMemory:{req.system_prompt}")

        # req.prompt = f"<core_memory>: {json.dumps(core_mem, ensure_ascii=False)}\n</core_memory>\n" + req.prompt    

    @filter.command_group("mem")
    def mem(self, t):
        pass
    
    @mem.command("check")
    async def check(self, event: AstrMessageEvent):
        '''
        查看上次记忆更新内容
        '''
        uid = event.unified_msg_origin
        if self.last_update.get(uid) is None:
            await self.context.send_message(uid,MessageChain().message("尚未进行过记忆更新。"))
        else:
            await self.context.send_message(uid,MessageChain().message(f"上次更新内容:\n{self.last_update[uid]}"))
        event.stop_event()

    @mem.command("gen")
    async def gen(self, event: AstrMessageEvent, extra_prompt: str="", use_full: str = ""):
        """生成记忆提示词或应用模型返回的记忆更新。
        可以在命令后添加临时加入的extra prompt，还可以附加 --full 参数以使用全部对话历史。
        (e.g. /mem gen 删除无效记忆 --full)
        """

        # use_full = kwargs.get("use_full") if "use_full" in kwargs else (args[0] if args else "")
        mem_result = await self.send_prompt(event, extra_prompt=extra_prompt, full=(str(use_full).strip() == "--full"))
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
        '''
        重构记忆，将当前记忆备份为pre文件，然后根据备份重构记忆
        适用于需要重构记忆的场景
        '''
        uid = event.unified_msg_origin
        mem_path = get_astrbot_data_path() + f"memory_store_{uid}.json" if not self.use_global else get_astrbot_data_path() + f"memory_store_global.json"  
        pre_mem_path = get_astrbot_data_path() + f"memory_store_{uid}_pre.json" if not self.use_global else get_astrbot_data_path() + f"memory_store_global_pre.json"  
        if os.path.exists(pre_mem_path):
            state_pre = MemoryStore(pre_mem_path).load()
        else:
            state_pre = MemoryStore(mem_path).load()
        try:
            os.rename(mem_path, pre_mem_path)
            os.remove(mem_path)
        except Exception as e:
            logger.info(f"发生错误:{e}")

        # pre_mem = MemoryStore(pre_mem_path)
        # state = pre_mem.load()
        await self.gen(event, extra_prompt=f"这是你之前的记忆，根据这些记忆重构现在的记忆:{state_pre}")
        event.stop_event()
    
    @filter.llm_tool(name="update_user_roster_id_dict") 
    async def update_user_roster_id_dict(self, event: AstrMessageEvent, 
                                name: str = None,
                                subject_id: str = None,
                                delete: bool = False    
                                ) -> MessageEventResult:

        '''更新user_roster_id_dict，当大模型发现某个记忆的主体与当前name不匹配但实际上是同一人时，可以调用这个工具来更新映射关系，以便正确检索记忆。
        当大模型需要更新映射关系时，必须提供 name 和 subject_id 参数。当需要删除映射关系时，提供 name 参数和 delete=True 即可，subject_id 参数可选。

        Args:
            name (str): 记忆名称。
            subject_id (str): 该记忆关联的对象/群组 ID（subject_id）
        '''
        if name is None:
            return "必须提供 name 参数。"

        if subject_id is None and not delete:
            return "必须提供 subject_id 参数。"
    
        self.user_roster.update(name, subject_id, delete=delete)
        return f"已更新 name '{name}' 与 subject_id '{subject_id}' 的映射关系。"

    @filter.llm_tool(name="search_memory_by_name") 
    async def search_memory_by_name(self, event: AstrMessageEvent, 
                                name: str = None
                                ) -> MessageEventResult:

        '''根据name搜索记忆并返回结果。
        大模型可以调用这个工具来搜索记忆，调用时请确保提供正确的参数
        当提到某个name时，大模型可以使用这个工具来检索与该name相关的记忆。
        当大模型需要检索与name相关记忆时，必须提供name

        Args:
            name (str): 记忆名称。
        '''
        if name is None:
            return "必须提供 name 参数。"

        subject_id = self.user_roster.id_dict.get(name)
        if subject_id is None:
            return f"未找到与 name '{name}' 相关的 subject_id。这是当前的 name-subject_id 映射: {self.user_roster.id_dict}。你可以根据这个内容查看是否有实际上是同一人但名字不同的情况。如果有，你必须调用update_user_roster_id_dict来把当前的name更新映射列表"
        else:
            mem_file_path = get_astrbot_data_path() + f"memory_store_{event.unified_msg_origin}.json" if not self.use_global else get_astrbot_data_path() + f"memory_store_global.json"  
            state = MemoryStore(mem_file_path).load()
            mem_info = self.process_mem_info(state, id_list=[subject_id])
            return mem_info

    @filter.llm_tool(name="check_user_roster_id_dict")
    async def check_user_roster_id_dict(self, event: AstrMessageEvent) -> MessageEventResult:
        '''检查当前的 name-subject_id 映射关系。'''
        return self.user_roster.id_dict

    @filter.llm_tool(name="update_one_memory") 
    async def update_one_memory(self, event: AstrMessageEvent, 
                                memory_type: Optional[str] = None,
                                action_type: Optional[str] = None,
                                memory_id: Optional[str] = None,
                                content: Optional[str] = None,
                                category: Optional[str] = None,
                                importance: Optional[int] = None,
                                expires_at: Optional[str] = None,
                                subject_id: Optional[str] = None
                                ) -> MessageEventResult:

        '''精准管理（增加/更新/删除）单条记忆。

        【执行前置检查】
        - 如果你需要修改或删除一条当前上下文中看不到的记忆，必须先调用 search_memory_by_name 确认该记忆的具体 memory_id。

        【🔴 SUBJECT_ID 绝对规则（极度重要）】
        - 只要记忆内容与当前交互的用户（或群组）相关（例如：用户的名字、喜好、经历、你们之间的约定），subject_id 必须填写该用户的真实 ID！
        - 绝对禁止将用户的私人信息存入 "global"！
        - "global" 仅限用于存放宇宙客观真理、AI 助手自身的全局系统设定。

        【操作动作指南】
        - 【新增记忆】：action_type="upsert"；memory_id 必须留空（系统会自动生成）；必须提供 content 和 subject_id。
        - 【更新记忆】：action_type="upsert"；必须提供精准的 memory_id 以覆盖原记忆；必须提供修改后的 content。
        - 【删除记忆】：action_type="delete"；必须提供精准的 memory_id；其他参数留空。

        Args:
            memory_type (str): 记忆所属层级。必填：core_memory(核心档案/事实) | long_term(长期目标/知识) | medium_term(近期连贯主题)。
            action_type (str): 操作类型。必填：upsert(新增或更新) | delete(删除)。
            memory_id (str, optional): 记忆的唯一标识符。新增时留空；更新/删除时必填。
            content (str, optional): 记忆的具体文本内容。upsert 操作时必填。
            category (str, optional): 记忆类别。可选：profile(档案) | preference(偏好) | task(任务) | fact(事实)。默认 "fact"。
            importance (int, optional): 记忆重要程度 (1-5的整数)，5为最重要。默认 3。
            expires_at (str, optional): 记忆过期时间 (YYYY-MM-DD)。留空表示永久有效。
            subject_id (str, optional): 记忆的归属者 ID。必须是具体的用户 ID，仅客观真理可使用 "global"。
        '''
        

        cur_state = {
            "memory_type": memory_type,
            "action_type": action_type,
            "memory_id": memory_id,
            "content": content,
            "category": category,
            "importance": importance,
            "expires_at": expires_at,
        }
        logger.info("update_one_memory called with: %s", cur_state)

        if memory_type not in {"core_memory", "long_term", "medium_term"}:
            return "无效的记忆类型，memory_type仅支持 core_memory、long_term 或 medium_term。"
        if action_type not in {"upsert", "delete"}:
            return "无效的操作类型，action_type仅支持 upsert 或 delete。"
        if not memory_id:
            return "必须提供 memory_id"
        if action_type == "upsert" and not content:
            return "upsert 操作必须提供 content。"
        
        if action_type == "upsert":
            operations = {
                memory_type: {
                    "upsert": [
                        {
                            "memory_id": memory_id,
                            "content": content,
                            "category": category or "fact",
                            "importance": importance if importance is not None else 3,
                            "expires_at": expires_at or "",
                            "subject_id": subject_id or "global",
                        }
                    ],
                    "delete": [],
                }
            }
        else:
            operations = {
                memory_type: {
                    "upsert": [],
                    "delete": [memory_id],
                }
            }

        mem_to_update = json.dumps(operations, ensure_ascii=False)
        report = self._handle_apply(event, mem_to_update)
        logger.info("State update report: %s", report)
        if report.startswith("Update Failed"):
            # await event.send(event.plain_result(report))
            return report
        else:
            # await event.send(event.plain_result("Update successful: " + report))
            return report

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
        # provider_id = await self.context.get_current_chat_provider_id(uid)
        # logger.info(f"uid:{uid}")

        #获取会话历史
        conv_mgr = self.context.conversation_manager
        curr_cid = await conv_mgr.get_curr_conversation_id(uid)
        conversation = await conv_mgr.get_conversation(uid, curr_cid)  # Conversation
        history = json.loads(conversation.history) if conversation and conversation.history else []

        #获取人格
        # system_prompt = await self.get_persona_system_prompt(uid)
        person_prompt = await self.context.persona_manager.get_default_persona_v3(uid)
        if not person_prompt:
            person_prompt = self.context.provider_manager.selected_default_persona["prompt"]
        # logger.info(f"人设提示词:{person_prompt}")

        mem_prompt = self._handle_prompt(event, history, full)
        if extra_prompt != "":
            mem_prompt = extra_prompt + "\n" + mem_prompt

        #发送信息到llm
        sys_msg = f"{person_prompt}"
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
        mem_file_path = get_astrbot_data_path() + f"memory_store_{uid}.json" if not self.use_global else get_astrbot_data_path() + f"memory_store_global.json"  
        if not Path(mem_file_path).exists() or full:
            task_prompt = "please refresh core/long-term/medium-term memory based on the entire conversation.\n"
        else:
            task_prompt = "please refresh core/long-term/medium-term memory based on the latest conversation.\n"
        state = MemoryStore(mem_file_path).load()
        state.pop("metadata", None)
        logger.info("创建记忆提示词，操作者: %s", uid)
        
        memory_snapshot = json.dumps(state, ensure_ascii=False, indent=2)
        cur_mem_prompt = (
            "You are an intelligent agent with a structured memory system. Below is your current memory snapshot.\n"
            "When updating your memories, follow these principles:\n"
            "1. Do NOT add any memory that already exists or is highly similar to an existing one.\n"
            "2. Proactively identify and forget memories that are outdated, irrelevant, or of low value.\n"
            "3. Keep your memory concise, focused, and up-to-date. Remove any redundant, obsolete, or trivial information.\n"
            "4. Only retain information that is useful for future reasoning, continuity, or identity.\n"
            "5. When in doubt, prefer fewer, higher-quality memories over more, lower-quality ones.\n"
            "6. Ensure that core memory remains stable and only changes when absolutely necessary.\n"
            "7. short-term memory is not needed to generate. Make sure all memory you generate is either core, long-term, or medium-term.\n"
            "\n**[Current Memory Snapshot]**\n"
            f"{memory_snapshot}"
            "\n**[Current subject_id]，use it if this memory is associated with a specific user or group**\n"
            f"{uid}\n"
        )
        
        template = (
            task_prompt +
            cur_mem_prompt + 
            self.config.get("mem_prompt", "") +
            "output JSON with the following sections (each is required and serves a distinct purpose):\n"
            "- summary: concise highlights of any changes across memories.\n"
            "- core_memory: enduring identity/profile/preferences/facts; anchor for consistency and rarely changes.\n"
            "- long_term: durable knowledge/goals worth keeping across many sessions; update cautiously.\n"
            "- medium_term: active themes/tasks spanning recent sessions that aid continuity.\n"
            "JSON Format:\n"
            "{\n"
            "  \"summary\": {\n"
            "    \"core_memory_highlights\": \"<summary of core memory changes>\",\n"
            "    \"long_term_highlights\": \"<summary of long-term changes>\",\n"
            "    \"medium_term_highlights\": \"<summary of medium-term changes>\",\n"
            "  },\n"
            "  \"core_memory\": {\n"
            "    \"upsert\": [{\n"
            "      \"memory_id\": \"reuse or system generated\",\n"
            "      \"content\": \"memory text\",\n"
            "      \"category\": \"profile|preference|task|fact\",\n"
            "      \"importance\": 1-5,\n"
            "      \"expires_at\": \"YYYY-MM-DD or leave blank\"\n"
            "      \"subject_id\": \"(who/which group this memory is associated with; use 'global' means global memory)\"\n"
            "    }],\n"
            "    \"delete\": [\"memory_id to delete\"]\n"
            "  },\n"
            "  \"long_term\": { same structure as core_memory },\n"
            "  \"medium_term\": { same structure as core_memory },\n"
            "}\n\n"
            "If no changes are needed, return empty upsert/delete and explain why in the summary."
        )
        logger.info(f"记忆提示词内容:{template}")
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
        mem_file_path = get_astrbot_data_path() + f"memory_store_{uid}.json" if not self.use_global else get_astrbot_data_path() + f"memory_store_global.json"  
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

        # Try to recover the first valid JSON object/array from mixed text output.
        decoder = json.JSONDecoder()
        for i, ch in enumerate(stripped):
            if ch not in "[{":
                continue
            try:
                _, end = decoder.raw_decode(stripped[i:])
                return stripped[i : i + end]
            except Exception:
                continue
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

        summary_block = operations.get("summary")
        # if isinstance(summary_block, dict):
        #     state.setdefault("metadata", {}).setdefault("summary", {}).update(summary_block)

        # state.setdefault("metadata", {})["last_update"] = now
        state.pop("metadata", None)
        report_lines.append(self._format_report_line("核心记忆", core_result))
        report_lines.append(self._format_report_line("长期", lt_result))
        report_lines.append(self._format_report_line("中期", mt_result))

        if isinstance(summary_block, dict) and summary_block:
            core_high = summary_block.get("core_memory_highlights", "无")
            lt_high = summary_block.get("long_term_highlights", "无")
            mt_high = summary_block.get("medium_term_highlights", "无")
            report_lines.append("概述:\n- 核心: " + core_high + "\n- 长期: " + lt_high + "\n- 中期: " + mt_high)

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
        index = {item.get("memory_id"): item for item in bucket if item.get("memory_id")}

        upserts = operations.get("upsert") or []
        if not isinstance(upserts, list):
            upserts = []

        for raw_entry in upserts:
            if not isinstance(raw_entry, dict):
                continue
            content = (raw_entry.get("content") or "").strip()
            subject_id = (raw_entry.get("subject_id") or "global").strip()
            if not content:
                continue
            
            #创建副本，用于之后更新
            entry = raw_entry.copy()
            entry["content"] = content
            entry["subject_id"] = subject_id
            entry["updated_at"] = timestamp
            entry.setdefault("category", "fact" if is_long_term else "task")
            entry.setdefault("importance", 3)

            entry_id = entry.get("memory_id") or self._generate_entry_id(is_long_term)
            entry["memory_id"] = entry_id

            if entry_id in index:#如果已经存在，更新内容并保留原有的 created_at
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
