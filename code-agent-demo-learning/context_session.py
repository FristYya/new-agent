try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

from pathlib import Path
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import json

# 1、模型配置
API_KEY = "sk-baabc0c085e94945ace399b561a0c07d"
BASE_URL = "https://api.deepseek.com/v1"
MODEL = "deepseek-flash"

# 2、定义工具
def resolve_path(path_str: str) -> Path:
    """把相对路径转换成绝对路径"""
    path = Path(path_str).expanduser()
    return path.resolve() if not path.is_absolute() else path

# read_file
def read_file(path: str) -> dict:
    """读取文本文件。"""
    full_path = resolve_path(path)
    content = full_path.read_text(encoding="utf-8")
    return {"file_path": str(full_path), "content": content}

# list_file
def list_files(path: str) -> dict:
    """读取当前目录下的文件和子目录"""
    full_path = resolve_path(path)
    items = []
    for item in sorted(full_path.iterdir()):
        items.append(
            {
                "filename": item.name,
                "type": "file" if item.is_file() else "dir",
            }
        )
    return {"path": str(full_path), "files": items}

# edit_file
def edit_file(path: str, old_str: str, new_str: str) -> dict:
    """old_str为老文本，new_str为新文本"""
    full_path = resolve_path(path)
    if old_str == "":
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(new_str, encoding="utf-8")
        return {"path": str(full_path), "action": "created_file"}
    
    original = full_path.read_text(encoding="utf-8")
    if old_str not in original:
        return {"path": str(full_path), "action": "old_str not found"}

    edited = original.replace(old_str, new_str, 1)
    full_path.write_text(edited, encoding="utf-8")
    return {"path": str(full_path), "action": "edited"}

# 3、工具注册
TOOL_REGISTRY = {
    "read_file": read_file,
    "list_files": list_files,
    "edit_file": edit_file,
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件的完整内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出目录中的文件和子目录",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录路径"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "编辑文件: old_str 为空则创建/覆盖，非空则替换",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_str": {"type": "string"},
                    "new_str": {"type": "string"},
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
    },
]


# 4、工具输出治理 -----------------------------------------------------------
def trim_single_line(line: str, max_chars: int, keep: str = "head") -> str:
    """限制单行字符数，防止一整行超限绕过行数限制"""
    if max_chars <= 0:
        return ""
    if len(line) <= max_chars:
        return line

    # 省略号也占一个字符，所以正文预算要 - 1
    body_size = max_chars - 1
    if keep == "head":
        return line[:body_size] + "..."
    if keep == "tail":
        return "..." + line[-body_size:] if body_size else ""

    raise ValueError("keep 只能是 head 或者 tail")


def _trim_utf8(text: str, max_bytes: int, keep: str) -> str:
    """按照 utf-8 字节截断, ignore只丢弃边界处不完整的字符"""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    part = raw[:max_bytes] if keep == "head" else raw[-max_bytes:]
    return part.decode("utf-8", errors="ignore")

def _trim_text(
        text: str,
        *,
        keep: str,
        max_lines: int,
        max_bytes: int,
        max_line_chars: int,
) -> dict:
    """依次限制总行数、单行字符数、总的 utf-8 字节数"""
    lines = text.splitlines()
    original_bytes = len(text.encode("utf-8"))

    # head 保留开头，tail保留结尾
    selected = lines[:max_lines] if keep == "head" else lines[-max_lines:]
    has_long_line = any(len(line) > max_line_chars for line in selected)
    selected = [trim_single_line(line, max_line_chars, keep) for line in selected]
    before_bytes_trim = "\n".join(selected)
    content = _trim_utf8(before_bytes_trim, max_bytes, keep)

    reasons = []
    if len(lines) > max_lines:
        reasons.append("lines")
    if has_long_line:
        reasons.append("single_line_chars")
    if len(before_bytes_trim.encode("utf-8")) > max_bytes:
        reasons.append("bytes")

    return {
        "content": content,
        "truncated": bool(reasons),
        "strategy": keep,
        "truncated_by": reasons,
        "original_lines": len(lines),
        "original_bytes": original_bytes,
        "output_line": len(content.splitlines()),
        "output_bytes": len(content.encode("utf-8")),
    }

def trim_text_head(
    text: str,
    max_lines: int = 2_000,
    max_bytes: int = 50_000,
    max_line_chars: int = 500,
) -> dict:
    """保留开头，适合源代码、配置和普通文本文件。"""
    return _trim_text(
        text,
        keep="head",
        max_lines=max_lines,
        max_bytes=max_bytes,
        max_line_chars=max_line_chars,
    )


def trim_text_tail(
    text: str,
    max_lines: int = 2_000,
    max_bytes: int = 50_000,
    max_line_chars: int = 500,
) -> dict:
    """保留结尾，适合未来接入的日志、测试和构建输出。"""
    return _trim_text(
        text,
        keep="tail",
        max_lines=max_lines,
        max_bytes=max_bytes,
        max_line_chars=max_line_chars,
    )


def prepare_tool_result(
    tool_name: str,
    result: dict,
    *,
    read_max_lines: int = 2_000,
    read_max_bytes: int = 50_000,
    read_max_line_chars: int = 500,
    list_max_items: int = 200,
) -> str:
    """针对三个真实工具，生成进入模型上下文的结果。"""
    if "error" in result:
        # 错误信息通常很短，必须完整交给模型用于自我修正。
        model_result = dict(result)
    elif tool_name == "read_file":
        # 源代码通常从定义和 import 开始，因此 read_file 保留头部。
        view = trim_text_head(
            result.get("content", ""),
            max_lines=read_max_lines,
            max_bytes=read_max_bytes,
            max_line_chars=read_max_line_chars,
        )
        model_result = dict(result)
        model_result["content"] = view["content"]
        if view["truncated"]:
            model_result["_context"] = {
                **{key: value for key, value in view.items() if key != "content"},
                "note": "完整内容仍保存在 file_path，可缩小范围后再次读取",
            }
    elif tool_name == "list_files":
        # 目录是结构化列表，按完整条目裁剪，不能切坏 JSON。
        files = result.get("files", [])
        model_result = dict(result)
        model_result["files"] = files[:list_max_items]
        if len(files) > list_max_items:
            model_result["_context"] = {
                "truncated": True,
                "strategy": "first_items",
                "original_items": len(files),
                "kept_items": list_max_items,
                "note": "请进入更具体的子目录后再次调用 list_files",
            }
    elif tool_name == "edit_file":
        # 编辑结果很短，path 和 action 都是关键事实，直接完整保留。
        model_result = dict(result)
    else:
        model_result = {"error": f"未知工具：{tool_name}"}

    return json.dumps(model_result, ensure_ascii=False)




# 5、历史压缩 ---------------------------------------------------------------
def estimate_tokens(value: Any) -> int:
    """沿用pi的估算  4 个字符串 = 1 token，tokenizer"""
    # 字符串可以直接估算，字典或者列表优先转成json
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    # +3 再整除 4 = 向上取整
    return max(1, (len(text) + 3) // 4)

def should_compact(
        messages: list[dict],
        *,
        context_window: int,
        reserve_tokens: int,
) -> bool:
    """在上下文溢出前触发，为下一次模型输出预留空间"""
    used_tokens = sum(estimate_tokens(message) for message in messages)
    return used_tokens > context_window - reserve_tokens

def find_cut_index(messages: list[dict], keep_recent_tokens: int) -> int:
    """从后往前保留最近内容，并且必须保证切点不能落在tool上"""
    used_tokens = 0
    # cut_index初始化，表示一条信息也不保留
    cut_index = len(messages)

    # 从最新消息向前扫描
    for index in range(len(messages) -1, -1, -1):
        message_token = estimate_tokens(messages[index])
        # 已经保留了至少一条消息之后，下一条超出预算就直接停止
        if used_tokens + message_token > keep_recent_tokens and cut_index < len(messages):
            break;
        used_tokens += message_token
        cut_index = index

    while cut_index > 0 and messages[cut_index].get("role") == "tool":
        cut_index -= 1

    return cut_index

def build_summary(old_messages: list[dict], previous_summary: str = "") -> str:
    """把较早历史压成稳定字段"""
    # 四个列表分别收集目标、决定、已读文件和已修改文件，
    user_texts = []
    decisions = []
    read_files = []
    modified_files = []

    for message in old_messages:
        role = message.get("role")
        # content 统一转成字符串并且去掉收尾空白，避免None或非字符串影响处理
        content = str(message.get("content", "")).strip()
        if role == "user" and content:
            # 每条内容只去前 160 个字符，防止摘要也无限膨胀
            user_texts.append(content[:160])
        elif role == "assistant" and  content:
            decisions.append(content[:160])
        elif role == "tool":
            # 工具结果约定为json，如果解析失败，使用空字典，保证摘要过程不会被打断
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                data = {} 
            path = data.get("file_path") or data.get("path")
            if path and message.get("name") == "read_file":
                read_files.append(path)
            if path and message.get("name") == "edit_file":
                modified_files.append(path)

    # 固定字段比自由散文更可预测；dict.fromkeys 在保留原顺序的同时完成路径去重。
    return "\n".join(
        [
            "[历史摘要]",
            f"Goal: {user_texts[0] if user_texts else '未记录'}",
            "Constraints: 保留用户明确要求和最近消息原文",
            f"Progress: 已处理 {len(old_messages)} 条较早消息",
            f"Key Decisions: {' | '.join(decisions[-2:]) or '暂无'}",
            f"Files Read: {', '.join(dict.fromkeys(read_files)) or '暂无'}",
            f"Files Modified: {', '.join(dict.fromkeys(modified_files)) or '暂无'}",
            f"Next Steps: {user_texts[-1] if user_texts else '继续当前任务'}",
            # 多次压缩时带上旧摘要，让长期目标和历史约束能够继续传递。
            f"Previous Summary: {previous_summary[:300] or '暂无'}",
        ]
    )


def compact_messages(
        messages: list[dict],
        *,
        context_window: int = 8000,
        reserve_tokens: int = 2000,
        keep_recent_tokens: int = 2000,
        previous_summary: str = "",
) -> list[dict]:
    """返回历史摘要 + 最近原文；预算未超出时原样复制"""
    # 浅拷贝每条消息，避免压缩函数以外修改调用方维护的原始列表
    copied = [dict(message) for message in messages]

    if not should_compact(
        copied,
        context_window=context_window,
        reserve_tokens=reserve_tokens
    ):
        # 未达到阈值不做摘要，完整保留
        return copied

    cut_index = find_cut_index(copied, keep_recent_tokens)
    if cut_index == 0:
        # 没有找到可压缩的前缀
        return copied

    summary = build_summary(copied[:cut_index], previous_summary)

    return [{"role": "system", "content": summary}, *copied[cut_index:]]







# 6、Session Tree -----------------------------------------------------------
@dataclass(frozen=True)
class SessionNode:
    """节点不可变；parent_id 表示它从哪个历史节点继续。"""

    node_id: str
    parent_id: str | None
    message: dict
    created_at: str


class SessionTree:
    """追加式会话树：回退只移动 head，不删除历史。"""

    def __init__(self, log_path: str | None = None) -> None:
        self.nodes: dict[str, SessionNode] = {}
        self.head_id: str | None = None
        self.log_path = resolve_path(log_path) if log_path else None

    def append(self, message: dict) -> str:
        node = SessionNode(
            node_id=uuid4().hex,
            parent_id=self.head_id,
            message=dict(message),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.nodes[node.node_id] = node
        self.head_id = node.node_id

        # JSONL 只追加，不覆盖；程序中断时，旧节点仍可恢复。
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(asdict(node), ensure_ascii=False) + "\n")
        return node.node_id

    def checkout(self, node_id: str) -> None:
        """把 head 移到旧节点；下一次 append 就会产生新分支。"""
        if node_id not in self.nodes:
            raise KeyError(f"节点不存在：{node_id}")
        self.head_id = node_id

    def messages(self) -> list[dict]:
        """沿 parent_id 重建根节点到当前 head 的唯一消息路径。"""
        path = []
        current = self.head_id
        while current is not None:
            node = self.nodes[current]
            path.append(node.message)
            current = node.parent_id
        return list(reversed(path))

    @classmethod
    def from_jsonl(cls, log_path: str) -> "SessionTree":
        """从追加式 JSONL 日志恢复会话树。"""
        tree = cls()
        source = resolve_path(log_path)
        with source.open("r", encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                node = SessionNode(**json.loads(line))
                tree.nodes[node.node_id] = node
                tree.head_id = node.node_id
        tree.log_path = source
        return tree



# 4、系统提示词 coding agent
SYSTEM_PROMPT = """你是一个专业的编程助手智能体。你可以使用以下工具：
- read_file: 读取文件的全部内容
- list-files: 列出目录中的文件和子目录
- edit_file: 编辑文件，old_str 为空时则创建/覆盖, 非空时则替换

使用规则：
1. 先判断是否需要工具
2. 需要工具时直接发起工具调用
3. 收到工具结果后继续推理或回复
4. 创建文件时 old_str 传空字符串
5. 修改文件前后 read_file 了解当前内容
"""

# 8、Agent Loop
class SimpleAgent:
    """模型 -> 工具 -> 模型的最小循环，并接入压缩与 Session Tree。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        client=None,
        log_path: str | None = None,
        context_window: int = 8_000,
        reserve_tokens: int = 2_000,
        keep_recent_tokens: int = 2_000,
        max_tool_rounds: int = 8,
    ) -> None:
        if client is None:
            if OpenAI is None:
                raise RuntimeError("请先执行：python3 -m pip install openai")
            client = OpenAI(api_key=api_key, base_url=base_url)
        self.client = client
        self.model = model
        self.conversation = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.session_tree = SessionTree(log_path)
        self.session_tree.append(self.conversation[0])
        self.previous_summary = ""
        self.context_window = context_window
        self.reserve_tokens = reserve_tokens
        self.keep_recent_tokens = keep_recent_tokens
        self.max_tool_rounds = max_tool_rounds

    def _append_message(self, message: dict) -> None:
        """完整消息写入会话树，同时加入当前模型上下文。"""
        self.conversation.append(message)
        self.session_tree.append(message)

    def _compact_after_turn(self) -> None:
        """一轮结束后再压缩，避免把进行中的工具调用切成两半。"""
        history = self.conversation[1:]
        compacted = compact_messages(
            history,
            context_window=self.context_window,
            reserve_tokens=self.reserve_tokens,
            keep_recent_tokens=self.keep_recent_tokens,
            previous_summary=self.previous_summary,
        )
        if compacted != history:
            self.conversation = [self.conversation[0], *compacted]
            self.previous_summary = compacted[0]["content"]

    def chat(self, user_message: str) -> str:
        """处理一条用户消息，运行完整 Agent Loop。"""
        print(f"\n 用户：{user_message}")
        self._append_message({"role": "user", "content": user_message})

        for _ in range(self.max_tool_rounds + 1):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.conversation,
                tools=TOOLS,
                tool_choice="auto",
            )
            message = response.choices[0].message

            # 没有工具调用：输出最终回答，结束本次内层循环。
            if not message.tool_calls:
                final_text = message.content or ""
                self._append_message({"role": "assistant", "content": final_text})
                self._compact_after_turn()
                print(f"🤖 Agent：{final_text}")
                return final_text

            # 关键协议：先保存包含 tool_calls 的 assistant 消息。
            self._append_message(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in message.tool_calls
                    ],
                }
            )

            # 同一轮可能包含多个工具调用，必须全部执行。
            for call in message.tool_calls:
                tool_name = call.function.name
                try:
                    tool_args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError as exc:
                    tool_args = {}
                    result = {"error": f"工具参数不是合法 JSON：{exc}"}
                    self._append_message(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": tool_name,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                    continue
                tool_function = TOOL_REGISTRY.get(tool_name)
                print(
                    f"  调用工具：{tool_name}"
                    f"({json.dumps(tool_args, ensure_ascii=False)})"
                )

                try:
                    if tool_function is None:
                        raise ValueError(f"未知工具：{tool_name}")
                    result = tool_function(**tool_args)
                except Exception as exc:
                    result = {"error": str(exc)}

                # raw result 保留在真实文件/工具层；模型只接收受预算控制的视图。
                model_result = prepare_tool_result(tool_name, result)
                print("  工具结果（模型视图）：" + model_result)
                self._append_message(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": tool_name,
                        "content": model_result,
                    }
                )

        raise RuntimeError(f"工具调用超过上限：{self.max_tool_rounds}")


def main() -> None:
    if API_KEY == "xxx":
        raise SystemExit("请先把 context_session.py 顶部的 API_KEY 配置好")

    agent = SimpleAgent(API_KEY, BASE_URL, MODEL)
    print(" coding agent 已经启动，输入 exit 退出")

    while True:
        task = input("\n 你的任务：").strip()
        if not task or task.lower() == "exit":
            break
        agent.chat(task)



if __name__ == "__main__":
    main()
