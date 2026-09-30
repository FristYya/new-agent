"""按课程进度逐节 print 测试，不使用 unittest。

运行方式：
    python3 print_test_context_session.py 2
    python3 print_test_context_session.py 3
    python3 print_test_context_session.py 4
    python3 print_test_context_session.py all
"""

import argparse
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

def title(text: str) -> None:
    """打印醒目的步骤标题，方便课堂逐步观察。"""
    print("\n" + "=" * 70)
    print(text)
    print("=" * 70)


def print_json(value) -> None:
    """打印中文 JSON，不把中文转成 Unicode 转义。"""
    print(json.dumps(value, ensure_ascii=False, indent=2))



def test_section_2() -> None:
    from context_session import (
        list_files,
        prepare_tool_result,
        read_file,
        trim_text_head, 
        trim_text_tail,
    )

    title("测试工具输出治理")

    project_dir = Path(__file__).resolve().parent
    demo_file = project_dir / "test_data" / "context_demo.txt"
    demo_directory = project_dir / "test_data" / "list_demo"

    print("1、read_file: 打印项目内准备好的测试文件")
    raw_result = read_file(str(demo_file))
    print_json(raw_result)

    print("\n2. read_file 保留头部，并且触发单行限制")
    print(
        prepare_tool_result(
            "read_file",
            raw_result,
            read_max_lines=3,
            read_max_bytes=100,
            read_max_line_chars=12,
        )
    )

    print("\n3. trim_text_tail: 保留固定文件的最后两行")
    print_json(
        trim_text_tail(
            raw_result["content"],
            max_lines=2,
            max_bytes=100,
            max_line_chars=20,
        )
    )

    print("\n4. utf-8限制")
    print_json(trim_text_head("中文中文中文", max_bytes=7))

    print("\n5. list_file 先打印完整目录")
    directory_result = list_files(str(demo_directory))
    print_json(directory_result)

    print("\n6. list_file 只打印两个条目")
    print_json(
        prepare_tool_result(
            "list_files",
            directory_result,
            list_max_items=2
        )
    )


def test_section_3() -> None:
    from context_session import compact_messages, should_compact

    title("上下文压缩")

    messages = [
        {"role": "user", "content": "请创建一个 python 示例项目"},
        {"role": "assistant", "content": "我先查看入口文件"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "function": {"name": "read_file"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "read_file",
            "content": json.dumps(
                {"file_path": "/demo/app.py", "content": "print('old')"},
                ensure_ascii=False,
            ),
        },
        {"role": "assistant", "content": "入口文件已确定"},
        {"role": "user", "content": "继续修改 app.py, 并保留原函数名"},
    ]

    print("1、使用小预算，测试是否要压缩")
    print(should_compact(messages, context_window=100, reserve_tokens=40))

    print("2、压缩后：结构化摘要+最近原文")
    compacted = compact_messages(
        messages,
        context_window=100,
        reserve_tokens=40,
        keep_recent_tokens=25,
    )
    print_json(compacted)

    print("3、最近原本不能是一条孤立的tool")
    print("合法切点：",compacted[1]["role"] != "tool")


def test_section_4() -> None:
    """第四节结束时运行：验证分支、持久化和完整 Agent Loop。"""
    from context_session import (
        API_KEY,
        BASE_URL,
        MODEL,
        SessionTree,
        SimpleAgent,
    )

    title("第四节：Session Tree 与 Agent Loop")

    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = Path(temp_dir)
        log_path = workspace / "session.jsonl"

        print("1. checkout 后追加消息会产生新分支，不删除方案 A")
        tree = SessionTree(str(log_path))
        root_id = tree.append({"role": "user", "content": "设计登录模块"})
        tree.append({"role": "assistant", "content": "方案 A：Cookie"})
        branch_a = tree.messages()
        tree.checkout(root_id)
        tree.append({"role": "assistant", "content": "方案 B：Token"})
        print("方案 A：")
        print_json(branch_a)
        print("方案 B：")
        print_json(tree.messages())

        print("\n2. 从 JSONL 恢复当前分支")
        restored = SessionTree.from_jsonl(str(log_path))
        print_json(restored.messages())

        print("\n3. 用假模型跑完整循环：模型 -> edit_file -> 模型")
        output_path = workspace / "agent_created.py"
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=FakeCompletions(output_path))
        )
        agent = SimpleAgent(
            API_KEY,
            BASE_URL,
            MODEL,
            client=fake_client,
        )
        answer = agent.chat("创建一个打印 hello 的 Python 文件")
        print("最终回答：", answer)
        print("文件内容：", output_path.read_text(encoding="utf-8").strip())
        print("完整会话角色：", [m["role"] for m in agent.session_tree.messages()])

                     





##============================================#


class FakeCompletions:
    """第一次要求 edit_file，收到工具结果后给最终回答。"""

    def __init__(self, output_path: Path) -> None:
        self.output_path = output_path

    def create(self, **kwargs):
        messages = kwargs["messages"]
        if messages[-1]["role"] != "tool":
            call = SimpleNamespace(
                id="fake-call-1",
                function=SimpleNamespace(
                    name="edit_file",
                    arguments=json.dumps(
                        {
                            "path": str(self.output_path),
                            "old_str": "",
                            "new_str": "print('hello from agent')\n",
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            message = SimpleNamespace(content="", tool_calls=[call])
        else:
            message = SimpleNamespace(content="文件已经创建完成。", tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])



def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("section", choices=["2", "3", "4", "all"], nargs="?", default="all")
    section = parser.parse_args().section

    if section in {"2", "all"}:
        test_section_2()
    if section in {"3", "all"}:
        test_section_3()
    if section in {"4", "all"}:
        test_section_4()


if __name__ == "__main__":
    main()
