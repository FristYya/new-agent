"""三个核心作业方法的离线测试

作业与测试类的对应关系：

- 作业 1 ``search_text``：``SearchTextTests``；
- 作业 2 ``RepeatedCallGuard.observe``：``RepeatedCallGuardTests``；
- 作业 3 ``WriteApprovalManager.execute``：``WriteApprovalTests``。

单独运行一个方法：
``python -m unittest -v test_homework.测试类名.测试方法名``
"""

import sys
import tempfile
import unittest
from pathlib import Path

import homework_agent as agent


def report_test_result(title: str, **details) -> None:
    """把断言验证过的关键状态输出到终端，方便观察。"""

    print(f"\n  ✅ {title}", file=sys.stderr, flush=True)
    for name, value in details.items():
        print(f"     - {name}: {value}", file=sys.stderr, flush=True)


class SearchTextTests(unittest.TestCase):
    def test_search_rejects_path_outside_workspace(self):
        """验证搜索工具不能通过 ../ 读取工作区外的数据。"""

        # 每条测试都使用全新的临时目录，测试结束后自动删除。
        with tempfile.TemporaryDirectory() as tmp:
            # 把临时目录作为本次测试唯一允许访问的工作区。
            workspace = Path(tmp)

            # assertRaises 同时负责两件事：
            # 1. 要求代码必须抛出 PermissionError；
            # 2. 通过 as caught 保存异常对象，供后面打印真实错误。
            with self.assertRaises(PermissionError) as caught:
                # ../outside.txt 试图跳到工作区父目录，必须被路径守卫拒绝。
                agent.search_text("secret", "../outside.txt", workspace=workspace)

            # unittest 显示 ok 的含义是“预期异常正确发生”，不是越界读取成功。
            report_test_result(
                "安全边界生效，捕获到预期异常",
                exception_type=type(caught.exception).__name__,
                exception_message=str(caught.exception),
                rejected_path="../outside.txt",
            )

    def test_search_returns_empty_result_instead_of_error(self):
        """验证没有命中时返回结构化空结果，而不是抛异常。"""

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            # 创建一个确定不包含 missing 的文本文件。
            (workspace / "hello.py").write_text(
                "print('hello')\n",
                encoding="utf-8",
            )

            # 在工作区根目录搜索不存在的字符串。
            result = agent.search_text("missing", ".", workspace=workspace)

        # matches 必须是空列表，调用方不需要捕获“没有结果”异常。
        self.assertEqual(result["matches"], [])

        # returned_matches 与 matches 长度保持一致。
        self.assertEqual(result["returned_matches"], 0)

        # 没有结果并不代表结果被截断。
        self.assertFalse(result["truncated"])

        # 给模型一条明确、稳定、可读的状态消息。
        self.assertEqual(result["message"], "没有找到匹配内容")

        report_test_result(
            "无匹配时返回正常空结果，而不是异常",
            query="missing",
            returned_matches=result["returned_matches"],
            truncated=result["truncated"],
            message=result["message"],
        )

    def test_search_caps_results_at_50_and_marks_truncated(self):
        """验证大量命中不会无限占用模型上下文。"""

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            # 构造 60 行匹配数据，故意超过最多返回 50 条的限制。
            lines = [f"needle line {index}" for index in range(60)]
            (workspace / "many.txt").write_text(
                "\n".join(lines),
                encoding="utf-8",
            )

            result = agent.search_text("needle", ".", workspace=workspace)

        # 返回给模型的结果严格限制为 50 条。
        self.assertEqual(len(result["matches"]), 50)

        # truncated=True 明确告诉调用方：文件里还有更多命中。
        self.assertTrue(result["truncated"])

        # 实现找到第 51 条后即可停止，所以这里只承诺“至少 51 条”。
        self.assertEqual(result["total_matches_at_least"], 51)

        # 第一条命中必须保留正确的相对路径和从 1 开始的行号。
        self.assertEqual(result["matches"][0]["line"], 1)
        self.assertEqual(result["matches"][0]["file"], "many.txt")

        report_test_result(
            "搜索结果上限生效",
            input_matches=60,
            returned_matches=len(result["matches"]),
            total_matches_at_least=result["total_matches_at_least"],
            truncated=result["truncated"],
        )

    def test_search_skips_binary_and_truncates_long_line(self):
        """验证二进制过滤与单行文本截断能够同时工作。"""

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            # NUL 字节是本课程判断二进制文件的简单信号。
            (workspace / "binary.bin").write_bytes(b"needle\x00binary")

            # 创建包含搜索词、但长度超过展示上限的文本行。
            (workspace / "long.txt").write_text(
                "needle " + "x" * 300,
                encoding="utf-8",
            )

            result = agent.search_text("needle", ".", workspace=workspace)

        # 二进制文件被跳过，所以只剩 long.txt 的一条结果。
        self.assertEqual(result["returned_matches"], 1)
        self.assertEqual(result["skipped_binary_files"], 1)

        # 超长文本必须以省略号结尾，避免一行占满模型上下文。
        self.assertTrue(result["matches"][0]["text"].endswith("…"))

        report_test_result(
            "二进制文件被跳过，超长文本被截断",
            skipped_binary_files=result["skipped_binary_files"],
            returned_file=result["matches"][0]["file"],
            text_ends_with_ellipsis=result["matches"][0]["text"].endswith("…"),
        )

class RepeatedCallGuardTests(unittest.TestCase):
    def test_third_consecutive_identical_call_terminates(self):
        """验证第三次连续相同调用触发熔断。"""

        # history_size=10 只保留最近十次调用；
        # threshold=3 表示连续第三次相同调用时终止 Trace。
        guard = agent.RepeatedCallGuard(history_size=10, threshold=3)

        # 三次工具名和参数完全相同。
        first = guard.observe("read_file", {"path": "a.py"})
        second = guard.observe("read_file", {"path": "a.py"})
        third = guard.observe("read_file", {"path": "a.py"})

        # 前两次允许执行，第三次才达到阈值。
        self.assertFalse(first.should_terminate)
        self.assertFalse(second.should_terminate)
        self.assertTrue(third.should_terminate)

        # 第三次决策必须明确报告连续次数为 3。
        self.assertEqual(third.consecutive_count, 3)

        report_test_result(
            "第三次连续相同调用触发熔断",
            consecutive_counts=[
                first.consecutive_count,
                second.consecutive_count,
                third.consecutive_count,
            ],
            should_terminate=[
                first.should_terminate,
                second.should_terminate,
                third.should_terminate,
            ],
        )

    def test_pagination_arguments_prevent_false_positive(self):
        """验证分页 offset 不同，不会被误判为死循环。"""

        guard = agent.RepeatedCallGuard()

        # 工具名相同，但 offset 每次不同，代表在读取下一页。
        decisions = [
            guard.observe("read_page", {"path": "large.log", "offset": offset})
            for offset in (0, 100, 200)
        ]

        # 三次调用都属于有效进展，不应触发熔断。
        self.assertFalse(any(item.should_terminate for item in decisions))

        report_test_result(
            "不同分页参数不会被误判为重复调用",
            offsets=[0, 100, 200],
            should_terminate=[item.should_terminate for item in decisions],
        )

    def test_argument_order_does_not_change_fingerprint(self):
        """验证 JSON 参数字段顺序不影响稳定指纹。"""

        guard = agent.RepeatedCallGuard()

        # 两个字典表达同一组参数，只是字段书写顺序不同。
        first = guard.fingerprint(
            "search_text",
            {"query": "x", "path": "."},
        )
        second = guard.fingerprint(
            "search_text",
            {"path": ".", "query": "x"},
        )

        # sort_keys=True 应让两次指纹完全一致。
        self.assertEqual(first, second)

        report_test_result(
            "参数字段顺序不影响稳定指纹",
            fingerprints_equal=first == second,
            fingerprint_prefix=first[:16],
        )

class WriteApprovalTests(unittest.TestCase):
    def test_proposal_and_unapproved_execution_do_not_write(self):
        """验证 pending 提案不能直接写入磁盘。"""

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "demo.py"

            # 准备已有文件，用于确认失败后旧内容没有被覆盖。
            target.write_text("old\n", encoding="utf-8")

            manager = agent.WriteApprovalManager(workspace)

            # propose 只生成 diff 和 approval_id，不应写文件。
            proposal = manager.propose("demo.py", "new\n")

            # 预览必须同时包含删除旧内容和新增新内容。
            self.assertIn("-old", proposal["preview"])
            self.assertIn("+new", proposal["preview"])

            # 未调用 approve 就 execute，必须抛 PermissionError。
            with self.assertRaises(PermissionError) as caught:
                manager.execute(proposal["approval_id"], "write-1")

            # 写入被拒绝后，磁盘仍然保留旧内容。
            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")

            report_test_result(
                "未审批写入已被拦截",
                exception_type=type(caught.exception).__name__,
                exception_message=str(caught.exception),
                file_content_after_attempt=target.read_text(
                    encoding="utf-8"
                ).rstrip(),
            )

    def test_approved_write_is_audited_and_idempotent(self):
        """验证批准后的写入、审计记录和幂等重放。"""

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "demo.py"
            target.write_text("old\n", encoding="utf-8")

            manager = agent.WriteApprovalManager(workspace)

            # Agent 提出写入请求，状态从 pending 开始。
            proposal = manager.propose(
                "demo.py",
                "new\n",
                requested_by="agent",
            )

            # 用户 alice 明确批准这份具体 proposal。
            manager.approve(
                proposal["approval_id"],
                approved_by="alice",
            )

            # 同一 approval_id 和幂等键连续执行两次，模拟网络重试。
            first = manager.execute(
                proposal["approval_id"],
                "write-42",
            )
            second = manager.execute(
                proposal["approval_id"],
                "write-42",
            )

            # 文件最终写成新内容。
            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")

            # 第一次是真写，第二次必须返回缓存结果。
            self.assertFalse(first["idempotent_replay"])
            self.assertTrue(second["idempotent_replay"])

            # 审计事件必须完整反映提议、批准、执行和重放。
            self.assertEqual(
                [event["event"] for event in manager.event_log],
                [
                    "write_proposed",
                    "write_approved",
                    "write_executed",
                    "write_replayed",
                ],
            )

            # executed 事件还要记录批准人和目标路径。
            executed = manager.event_log[2]
            self.assertEqual(executed["approved_by"], "alice")
            self.assertEqual(executed["path"], "demo.py")

            report_test_result(
                "已审批写入成功，重复请求命中幂等结果",
                first_idempotent_replay=first["idempotent_replay"],
                second_idempotent_replay=second["idempotent_replay"],
                final_file_content=target.read_text(
                    encoding="utf-8"
                ).rstrip(),
                audit_events=[
                    event["event"] for event in manager.event_log
                ],
            )

    def test_file_change_after_approval_blocks_execution(self):
        """验证批准后文件被别人修改时，旧审批自动失效。"""

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "demo.py"
            target.write_text("old\n", encoding="utf-8")

            manager = agent.WriteApprovalManager(workspace)
            proposal = manager.propose(
                "demo.py",
                "approved content\n",
            )
            manager.approve(
                proposal["approval_id"],
                approved_by="alice",
            )

            # 模拟批准之后，另一个进程抢先修改了文件。
            target.write_text(
                "changed by someone else\n",
                encoding="utf-8",
            )

            # execute 必须重新比较 base_sha256，并拒绝覆盖新版本。
            with self.assertRaisesRegex(
                RuntimeError,
                "批准后发生变化",
            ) as caught:
                manager.execute(
                    proposal["approval_id"],
                    "write-43",
                )

            report_test_result(
                "文件版本冲突已被拦截",
                exception_type=type(caught.exception).__name__,
                exception_message=str(caught.exception),
                file_content_preserved=target.read_text(
                    encoding="utf-8"
                ).rstrip(),
            )

    def test_idempotency_key_cannot_be_reused_for_another_payload(self):
        """验证一个幂等键不能被不同写入内容复用。"""

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            manager = agent.WriteApprovalManager(workspace)

            # 第一份 payload 使用 same-key 成功写入。
            first = manager.propose("a.txt", "A")
            manager.approve(
                first["approval_id"],
                approved_by="alice",
            )
            manager.execute(
                first["approval_id"],
                "same-key",
            )

            # 第二份 payload 的路径和内容都不同。
            second = manager.propose("b.txt", "B")
            manager.approve(
                second["approval_id"],
                approved_by="alice",
            )

            # same-key 已绑定第一份 payload，再使用必须报错。
            with self.assertRaisesRegex(
                ValueError,
                "另一个写入内容",
            ) as caught:
                manager.execute(
                    second["approval_id"],
                    "same-key",
                )

            report_test_result(
                "幂等键误用已被拦截",
                exception_type=type(caught.exception).__name__,
                exception_message=str(caught.exception),
                reused_key="same-key",
            )


if __name__ == "__main__":
    unittest.main()

