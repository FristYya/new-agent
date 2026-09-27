"""第二节最终回归测试。
优先使用 ``python agent_v2.py --check stepN`` 边写边测；
完成七步后，再运行本文件做一次完整回归，确认各层组合后仍然正确。
"""

import tempfile
import unittest
from pathlib import Path

import agent_v2 as lesson


class ClassroomCheckpointTests(unittest.TestCase):
    """把 agent_v2.py 的七个课堂检查点接入 unittest。"""

    def test_step1_tool_layers(self):
        """三层工具能够包装，并生成正确的模型 Schema。"""

        lesson.check_step1_tool_layers()

    def test_step2_arguments_and_schema(self):
        """参数别名可规范化，缺字段和错类型会被拒绝。"""

        lesson.check_step2_arguments_and_schema()

    def test_step3_before_hook(self):
        """越界路径和敏感文件在执行前被安全钩子拦截。"""

        lesson.check_step3_before_hook()

    def test_step4_error_as_message(self):
        """执行异常变成 ToolResultMessage，成功结果经过脱敏。"""

        lesson.check_step4_error_as_message()

    def test_step5_batch_order(self):
        """并行读取不会破坏工具结果的协议顺序。"""

        lesson.check_step5_batch_order()

    def test_step6_message_boundary(self):
        """内部消息正确翻译，AuditMessage 不进入模型上下文。"""

        lesson.check_step6_message_boundary()

    def test_step7_offline_agent_loop(self):
        """Fake Client 能离线跑通两轮 Agent Loop。"""

        lesson.check_step7_offline_agent_loop()


class ProductionInvariantTests(unittest.TestCase):
    """补充检查七个步骤背后的关键生产不变量。"""

    def test_sequential_edit_changes_only_first_match(self):
        """edit_file 是串行副作用工具，而且只替换第一处匹配。"""

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "demo.py"
            target.write_text("old\nold\n", encoding="utf-8")

            tools = lesson.build_tools(workspace)
            results = lesson.execute_tool_batch(
                [
                    lesson.ToolCall(
                        "write-1",
                        "edit_file",
                        {
                            "path": "demo.py",
                            "old_str": "old",
                            "new_str": "new",
                        },
                    )
                ],
                tools,
                lesson.ToolContext(workspace),
            )

            # 工具成功，并且文件仍保留第二处 old。
            self.assertFalse(results[0].is_error)
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "new\nold\n",
            )

    def test_unknown_tool_is_a_lookup_error_message(self):
        """模型调用不存在的工具时，错误不能穿透 Agent Loop。"""

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            results = lesson.execute_tool_batch(
                [lesson.ToolCall("bad-1", "delete_everything", {})],
                lesson.build_tools(workspace),
                lesson.ToolContext(workspace),
            )

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].is_error)
        self.assertEqual(results[0].content["stage"], "lookup")
        self.assertEqual(results[0].tool_call_id, "bad-1")

    def test_long_result_is_truncated_before_model_boundary(self):
        """超长工具结果在进入消息历史前会被截断。"""

        with tempfile.TemporaryDirectory() as tmp:
            tools = lesson.build_tools(Path(tmp))
            cleaned = lesson.after_tool_call(
                tools["read_file"],
                {"content": "x" * (lesson.MAX_TOOL_RESULT_TEXT + 100)},
            )

        self.assertTrue(cleaned["content"].endswith("…[TRUNCATED]"))
        self.assertLess(
            len(cleaned["content"]),
            lesson.MAX_TOOL_RESULT_TEXT + 100,
        )

    def test_sensitive_file_blocked_for_write_too(self):
        """敏感文件策略同时保护读取和写入路径。"""

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result = lesson.prepare_tool_call(
                lesson.ToolCall(
                    "write-env",
                    "edit_file",
                    {
                        "path": ".env",
                        "old_str": "",
                        "new_str": "SECRET=value",
                    },
                ),
                lesson.build_tools(workspace),
                lesson.ToolContext(workspace),
            )

        self.assertIsInstance(result, lesson.ToolResultMessage)
        self.assertTrue(result.is_error)
        self.assertEqual(result.content["stage"], "before_tool_call")


if __name__ == "__main__":
    # 允许直接运行 python test_agent_v2.py，也允许使用 unittest -v。
    unittest.main()
