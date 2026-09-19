from openai import OpenAI

from pathlib import Path

import json

# 1、模型配置
API_KEY = "sk-baabc0c085e94945ace399b561a0c07d"
BASE_URL = "https://api.deepseek.com/v1"
MODEL = "deepseek-flash"

# # 测试模型的连通性
# client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

# response = client.chat.completions.create(
#     model=MODEL,
#     messages=[{"role": "user", "content": "只回复：连接成功"}],
# )

# print("模型回复：", response.choices[0].message.content)
# print("模型回复：", response.model)


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
                "type": "file" if item.is_file else "dir",
            }
        )
    return {"path": str(full_path), "files": items}

# edit_file
def edit_file(path: str, old_str: str, new_str: str) -> dict:
    """old_str为老文本，new_str为新文本"""
    full_path = resolve_path(path)
    if old_str == "":
        full_path.write_text(new_str, encoding="utf-8")
        return {"path": str(full_path), "action": "created_file"}
    
    original = full_path.read_text(encoding="utf-8")
    if old_str not in original:
        return {"path": str(full_path), "action": "old_str not found"}

    edited = original.replace(old_str, new_str, 1)
    full_path.write_text(edited, encoding="utf-8")
    return {"path": str(full_path), "action": "edited"}

# 1. read_file
# Path("lesson_note.txt").write_text(
#     "这是一次测试，Agent = 模型 + 工具 + 循环",
#     encoding="utf-8",
# )

# print(read_file("lesson_note.txt"))
# print(list_files("."))

# print(edit_file("hello.py", "", "print('hello')\n"))

# print(edit_file("hello.py", "hello", "hello 你好  aaa"))

# print(read_file("hello.py"))


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
            "name": "edit_files",
            "description": "编辑文件: old_str 为空则创建/覆盖，非空则替换",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_str": {"type": "string"},
                    "new_str": {"type": "string"},
                },
                "required": ["path", "old-str", "new_str"],
            },
        },
    },
]

# print("python中注册的工具：", list(TOOL_REGISTRY))
# print("模型看到的工具：", [item["function"]["name"] for item in TOOLS])

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

# 5、Agent Loop
class SimpleAgent:
    """最小 AI 编程智能体。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        client=None,
    ) -> None:
        self.client = client or OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.conversation = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    def chat(self, user_message: str) -> str:
        """处理一条用户消息，运行完整 Agent Loop。"""
        print(f"\n 用户：{user_message}")
        self.conversation.append(
            {"role": "user", "content": user_message}
        )

        while True:
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
                self.conversation.append(
                    {"role": "assistant", "content": final_text}
                )
                print(f"🤖 Agent：{final_text}")
                return final_text

            # 关键协议：先保存包含 tool_calls 的 assistant 消息。
            self.conversation.append(
                {
                    "role": "assistant",
                    "content": message.content,
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
                tool_args = json.loads(call.function.arguments)
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

                print(
                    "  工具结果："
                    + json.dumps(result, ensure_ascii=False)
                )
                self.conversation.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )


# agent = SimpleAgent(API_KEY, BASE_URL, MODEL)
# print("coding agent初始化成功")

# 1、创建文件
# agent.chat("创建 greet.py，写一个 greet(name) 函数并打印 greet('Pi') 的结果")

# 2. 修改文件
# agent.chat("把 greet.py 的问候改成中文，并保留原有的函数结构")


# 3. list 查看目录
# agent.chat("列出当前目录，并且告诉我哪些才是 python 文件")

# error
# agent.chat("读取一个不存在的文件 xx.py, 并解释发生了什么")


def main() -> None:
    if API_KEY == "xxx":
        raise SystemExit("请先把 agent.py 顶部的 API_KEY 配置好")

    agent = SimpleAgent(API_KEY, BASE_URL, MODEL)
    print(" coding agent 已经启动，输入 exit 退出")

    while True:
        task = input("\n 你的任务：").strip()
        if not task or task.lower() == "exit":
            break
        agent.chat(task)



if __name__ == "__main__":
    main()








