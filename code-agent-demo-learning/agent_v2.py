"""第二节课堂实战：在第一节课后作业完成版上继续开发。

本文件最初由 ``homework_agent.py`` 复制而来。这样做的目的不是维护两份
相同代码，而是保留第一节作业的稳定快照，再在副本上逐步学习 Pi 的
Tools 与 Messages 设计。课堂上所有第二节改动都只写在本文件。

第二节新增七个可以单独验证的能力：

1. Tool、AgentTool、ToolDefinition 三层工具边界；
2. 参数预处理与不可跳过的运行时 Schema 校验；
3. before_tool_call 副作用前安全拦截；
4. 异常消息化、结果脱敏与截断；
5. 只读工具并行、写工具串行、结果有序回注；
6. 内部消息与模型协议之间的翻译边界；
7. 使用 Fake Client 离线跑通完整 Agent Loop。

运行 ``python agent_v2.py --check step1`` 可以只验证一个步骤；
运行 ``python agent_v2.py --check all`` 可以执行全部七个检查点。
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import tempfile
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


MAX_SEARCH_RESULTS = 50
MAX_MATCH_TEXT_CHARS = 180


# ---------------------------------------------------------------------------
# 已完成框架：时间、哈希、路径安全和文件扫描
# ---------------------------------------------------------------------------


def utc_now() -> str:
    """返回带时区的 UTC 时间，供审计日志记录。"""

    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    """返回稳定的 SHA-256 摘要。"""

    return hashlib.sha256(data).hexdigest()


def resolve_inside_workspace(workspace: Path, path: str) -> Path:
    """把路径解析为绝对路径，并阻止它逃离工作区。"""

    root = workspace.expanduser().resolve()
    raw = Path(path).expanduser()
    candidate = raw.resolve() if raw.is_absolute() else (root / raw).resolve()

    if candidate != root and root not in candidate.parents:
        raise PermissionError(f"路径越过工作区：{path}")
    return candidate


def looks_binary(data: bytes) -> bool:
    """使用 NUL 字节和 UTF-8 解码判断二进制文件。"""

    if b"\x00" in data:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _iter_search_files(target: Path) -> list[Path]:
    """返回稳定排序的候选文件列表。"""

    if target.is_file():
        return [target]
    if not target.exists():
        raise FileNotFoundError(f"搜索路径不存在：{target}")
    if not target.is_dir():
        raise ValueError(f"搜索路径不是文件或目录：{target}")
    return sorted(item for item in target.rglob("*") if item.is_file())


def _compact_match_text(line: str, max_line_chars: int) -> str:
    """清理匹配行，并截断过长内容。"""

    compact = line.strip()
    if len(compact) > max_line_chars:
        return compact[:max_line_chars] + "…"
    return compact


# ---------------------------------------------------------------------------
# 作业 1：文本搜索
# ---------------------------------------------------------------------------


def search_text(
    query: str,
    path: str,
    *,
    workspace: Path,
    max_results: int = MAX_SEARCH_RESULTS,
    max_line_chars: int = MAX_MATCH_TEXT_CHARS,
) -> dict[str, Any]:
    """在工作区内搜索文本，最多返回 50 个结构化命中。"""
    # 第一道校验：空字符串没有搜索意义，也容易掩盖模型参数错误。
    if not isinstance(query, str) or not query:
        raise ValueError("query 必须是非空字符串")

    # 统一把工作区和目标路径转成绝对、规范化路径。
    root = workspace.expanduser().resolve()
    target = resolve_inside_workspace(root, path)

    # 路径合法但文件不存在时，返回明确错误，而不是静默得到空结果。
    if not target.exists():
        raise FileNotFoundError(f"搜索路径不存在：{target}")

    # 如果传入单个文件，只搜索它；如果传入目录，递归收集普通文件。
    # sorted() 让返回顺序稳定，方便测试和模型复现问题。
    files = [target] if target.is_file() else sorted(
        item for item in target.rglob("*") if item.is_file()
    )

    # matches 保存真正回传给模型的结构化结果。
    matches = []
    # confirmed_count 至少统计到第 51 条，用于证明结果确实被截断。
    confirmed_count = 0
    # 记录跳过多少二进制文件，便于排障和观测。
    skipped_binary_files = 0
    truncated = False

    for file_path in files:
        # 每个候选文件再次做路径校验，防止文件符号链接逃逸。
        try:
            safe_file = resolve_inside_workspace(root, str(file_path))
            data = safe_file.read_bytes()
        except (PermissionError, OSError):
            continue

        if looks_binary(data):
            skipped_binary_files += 1
            continue

        # splitlines() 按行搜索；start=1 让行号和编辑器保持一致。
        for line_number, line in enumerate(
            data.decode("utf-8").splitlines(),
            start=1,
        ):
            # 当前行没有查询词时立即进入下一行。
            if query not in line:
                continue

            confirmed_count += 1
            if len(matches) < max_results:
                # 去掉两端空白，并限制单条结果的长度。
                compact_line = line.strip()
                if len(compact_line) > max_line_chars:
                    compact_line = compact_line[:max_line_chars] + "…"

                # 模型需要文件、行号和原文片段才能继续定位与修改。
                matches.append(
                    {
                        "file": str(safe_file.relative_to(root)),
                        "line": line_number,
                        "text": compact_line,
                    }
                )
            else:
                # 找到第 51 条即可确认结果被截断，不再扫描整个大仓库。
                truncated = True
                break
        if truncated:
            break

    # 返回字典而不是拼接字符串，方便模型、UI 和测试分别消费字段。
    return {
        "query": query,
        "path": str(target.relative_to(root)) if target != root else ".",
        "matches": matches,
        "returned_matches": len(matches),
        "total_matches_at_least": confirmed_count,
        "truncated": truncated,
        "skipped_binary_files": skipped_binary_files,
        "message": "没有找到匹配内容" if not matches else "搜索完成",
    }


# ---------------------------------------------------------------------------
# 作业 2：重复工具调用熔断
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepeatDecision:
    # 本次调用的稳定指纹，可用于日志与问题定位。
    fingerprint: str
    # 当前调用在队尾连续出现了多少次。
    consecutive_count: int
    # 是否已经达到熔断阈值。
    should_terminate: bool


class RepeatedCallGuard:
    """记录最近 10 次调用，阻止连续三次完全相同的调用。"""

    def __init__(self, history_size: int = 10, threshold: int = 3):
        # threshold 是连续重复多少次才熔断，不是历史中出现的总次数。
        self.threshold = threshold
        # deque(maxlen=N) 会自动淘汰最早记录，避免历史无限增长。
        self.history = deque(maxlen=history_size)

    def fingerprint(self, tool_name: str, arguments: dict[str, Any]) -> str:
        # sort_keys 消除 JSON 字段顺序不同造成的假差异。
        canonical = json.dumps(
            {"tool": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        # 固定长度指纹适合比较和写入日志，不保存可能很长的原始参数。
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def observe(self, tool_name: str, arguments: dict[str, Any]) -> RepeatDecision:
        # 每执行一次工具前，都先生成本次调用的稳定指纹。
        current = self.fingerprint(tool_name, arguments)
        self.history.append(current)

        # 只从队尾统计“连续相同”，遇到不同调用立即停止。
        consecutive_count = 0
        for item in reversed(self.history):
            if item != current:
                break
            consecutive_count += 1

        return RepeatDecision(
            fingerprint=current,
            consecutive_count=consecutive_count,
            # 只有连续次数达到阈值才终止；历史中零散出现不算循环。
            should_terminate=consecutive_count >= self.threshold,
        )

# ---------------------------------------------------------------------------
# 作业 3：写入审批与幂等执行
# propose、approve、reject 和审计框架均已完成，学员只实现 execute。
# ---------------------------------------------------------------------------


@dataclass
class WriteProposal:
    """保存一次写文件请求的完整审批快照。

    审批的不是一句模糊的“允许写文件”，而是“允许把某个文件的某个版本，
    改成某段确定内容”。所以路径、旧版本哈希、新内容哈希和组合后的 payload
    哈希都必须保存在提案中，执行阶段才能重新校验。
    """

    # 每份提案的唯一编号；终端中的 approve 命令就是批准这个编号。
    approval_id: str

    # 相对于工作区的路径，避免把机器上的绝对路径暴露给模型。
    relative_path: str

    # 真正准备写入的完整内容。repr=False 防止调试打印时泄露或刷屏。
    content: str = field(repr=False)

    # 生成预览时目标文件的哈希；执行前必须再次比较，防止覆盖他人修改。
    base_sha256: str

    # 新内容的哈希；执行前重新计算，防止提案对象在内存中被篡改。
    content_sha256: str

    # path + base_sha256 + content_sha256 的组合哈希，代表这次具体写入。
    payload_sha256: str

    # 展示给用户审阅的 unified diff；propose 阶段只预览，不落盘。
    preview: str

    # 谁发起了请求，以及请求发生的时间，用于审计追踪。
    requested_by: str
    requested_at: str

    # 状态机从 pending 开始，随后只能进入 approved/rejected/executed。
    status: str = "pending"

    # 只有真正批准后才填写；None 表示尚未批准。
    approved_by: str | None = None
    approved_at: str | None = None


class WriteApprovalManager:
    """管理“生成预览 → 用户审批 → 安全执行”的完整状态机。"""

    # 新文件在生成预览时还不存在，因此使用稳定哨兵值表示它的基础版本。
    MISSING_FILE_HASH = "<missing>"

    def __init__(
        self,
        workspace: Path,
        *,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        # 把工作区规范化成绝对路径；后续目标路径都必须经过安全解析。
        self.workspace = workspace.expanduser().resolve()

        # 时钟可以注入：生产代码使用 utc_now，测试可以替换成固定时间。
        self.clock = clock

        # 保存 approval_id -> WriteProposal，构成当前进程内的审批存储。
        self.proposals: dict[str, WriteProposal] = {}

        # 保存 idempotency_key -> 执行结果，防止重试造成重复写入。
        self.idempotency_results: dict[str, dict[str, Any]] = {}

        # 追加式审计日志：记录谁在什么时候进行了什么动作。
        self.event_log: list[dict[str, Any]] = []

    def _current_hash(self, path: Path) -> str:
        """读取目标当前版本；不存在时返回统一哨兵值。"""

        # 不存在不是错误，因为 Agent 也可能要创建新文件。
        if not path.exists():
            return self.MISSING_FILE_HASH

        # 本课程只允许写普通文件，不允许把目录当成文件覆盖。
        if not path.is_file():
            raise ValueError(f"目标不是普通文件：{path}")

        # 哈希原始字节，避免换行符或编码转换改变版本判断。
        return sha256_bytes(path.read_bytes())

    def _payload_hash(
        self,
        relative_path: str,
        base_sha256: str,
        content_sha256: str,
    ) -> str:
        """为一次具体写入生成稳定的业务指纹。"""

        # canonical JSON 固定字段顺序和分隔符，保证相同输入得到相同哈希。
        canonical = json.dumps(
            {
                "path": relative_path,
                "base_sha256": base_sha256,
                "content_sha256": content_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

        # 组合哈希绑定路径、旧版本和新内容；任一项变化都会产生新指纹。
        return sha256_bytes(canonical.encode("utf-8"))

    def _append_event(
        self,
        event: str,
        proposal: WriteProposal,
        *,
        actor: str,
        **details: Any,
    ) -> None:
        """向审计日志追加一条结构化事件。"""

        # 公共字段集中生成，避免 propose/approve/execute 各写一套日志。
        self.event_log.append(
            {
                "event": event,
                "at": self.clock(),
                "actor": actor,
                "approval_id": proposal.approval_id,
                "path": proposal.relative_path,
                "base_sha256": proposal.base_sha256,
                "content_sha256": proposal.content_sha256,
                "payload_sha256": proposal.payload_sha256,
                **details,
            }
        )

    def _get_proposal(self, approval_id: str) -> WriteProposal:
        """按审批编号取回提案，并把底层 KeyError 转成可读错误。"""

        try:
            return self.proposals[approval_id]
        except KeyError as exc:
            raise KeyError(f"未知 approval_id：{approval_id}") from exc

    def _atomic_write_text(self, target: Path, content: str) -> None:
        """先写同目录临时文件，再原子替换目标文件。"""

        # 临时文件必须与目标同目录，os.replace 才能可靠地原子替换。
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        try:
            # os.fdopen 接管描述符，退出 with 时自动关闭。
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)

                # flush 把 Python 缓冲区推给操作系统。
                handle.flush()

                # fsync 要求系统把数据同步到底层存储，降低断电丢失风险。
                os.fsync(handle.fileno())

            # 读者只会看到完整旧文件或完整新文件，不会看到半份内容。
            os.replace(temporary_name, target)
        except Exception:
            # 任一步失败都清理临时文件，同时保留原始异常。
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
            raise

    def propose(
        self,
        path: str,
        content: str,
        *,
        requested_by: str = "agent",
    ) -> dict[str, Any]:
        """生成 diff 和 approval_id，但不写文件。"""

        # Schema 只是第一层校验，运行时仍要验证真实 Python 类型。
        if not isinstance(content, str):
            raise TypeError("content 必须是字符串")

        # 统一使用工作区路径守卫，阻止 ../ 和符号链接逃逸。
        target = resolve_inside_workspace(self.workspace, path)

        # 本示例不自动创建目录，避免模型悄悄扩大写入范围。
        if not target.parent.exists():
            raise FileNotFoundError(f"父目录不存在：{target.parent}")

        # 旧内容只用于生成 diff；新文件以空内容参与比较。
        old_bytes = target.read_bytes() if target.exists() else b""
        old_text = old_bytes.decode("utf-8", errors="replace")

        # 同时记录旧版本、新内容与相对路径，供审批和执行阶段复核。
        base_sha256 = self._current_hash(target)
        content_sha256 = sha256_bytes(content.encode("utf-8"))
        relative_path = str(target.relative_to(self.workspace))
        payload_sha256 = self._payload_hash(
            relative_path,
            base_sha256,
            content_sha256,
        )

        # unified_diff 才是用户真正要审阅的变更内容。
        preview = "\n".join(
            difflib.unified_diff(
                old_text.splitlines(),
                content.splitlines(),
                fromfile=f"a/{relative_path}",
                tofile=f"b/{relative_path}",
                lineterm="",
            )
        )

        # 新提案默认是 pending；此处绝对不能写入磁盘。
        proposal = WriteProposal(
            approval_id=uuid.uuid4().hex,
            relative_path=relative_path,
            content=content,
            base_sha256=base_sha256,
            content_sha256=content_sha256,
            payload_sha256=payload_sha256,
            preview=preview or "（内容没有变化）",
            requested_by=requested_by,
            requested_at=self.clock(),
        )

        # 先保存提案，再记录事件；approve 命令通过 approval_id 找到它。
        self.proposals[proposal.approval_id] = proposal
        self._append_event("write_proposed", proposal, actor=requested_by)

        # 返回可公开字段，不把完整 content 复制到模型上下文。
        return {
            "approval_id": proposal.approval_id,
            "status": proposal.status,
            "path": relative_path,
            "base_sha256": base_sha256,
            "content_sha256": content_sha256,
            "payload_sha256": payload_sha256,
            "preview": proposal.preview,
        }

    def approve(self, approval_id: str, *, approved_by: str) -> dict[str, Any]:
        """批准一份具体的写入提案。"""

        # approval_id 必须对应真实提案。
        proposal = self._get_proposal(approval_id)

        # 只允许 pending -> approved，避免重复批准或批准已拒绝提案。
        if proposal.status != "pending":
            raise ValueError(f"审批状态不是 pending：{proposal.status}")

        # 批准前第一次乐观锁检查：看到 diff 后文件不能已经发生变化。
        target = resolve_inside_workspace(self.workspace, proposal.relative_path)
        if self._current_hash(target) != proposal.base_sha256:
            raise RuntimeError("文件在预览后发生变化，请重新生成预览")

        # 保存批准人和时间，形成可追责的审批记录。
        proposal.status = "approved"
        proposal.approved_by = approved_by
        proposal.approved_at = self.clock()
        self._append_event("write_approved", proposal, actor=approved_by)

        # 返回 payload 哈希，让上层知道批准的是哪一份确定内容。
        return {
            "approval_id": approval_id,
            "status": proposal.status,
            "approved_by": approved_by,
            "payload_sha256": proposal.payload_sha256,
        }

    def reject(self, approval_id: str, *, rejected_by: str) -> dict[str, Any]:
        """拒绝一份待审批提案。"""

        proposal = self._get_proposal(approval_id)

        # 同样只允许处理 pending 提案；终态不能被反复改写。
        if proposal.status != "pending":
            raise ValueError(f"审批状态不是 pending：{proposal.status}")

        # rejected 是终态，execute 会拒绝执行它。
        proposal.status = "rejected"
        self._append_event("write_rejected", proposal, actor=rejected_by)
        return {"approval_id": approval_id, "status": proposal.status}

    def execute(
        self,
        approval_id: str,
        idempotency_key: str,
        *,
        actor: str = "agent",
    ) -> dict[str, Any]:
        """执行已批准的写入，并保证版本安全与幂等。"""

        # 幂等键由一次用户动作或上层请求生成；空值无法标识重试。
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key 必须是非空字符串")

        # 先取提案，以便把幂等键与这一次具体 payload 绑定。
        proposal = self._get_proposal(approval_id)

        # 如果幂等键已经执行过，就不能再次产生写文件副作用。
        cached = self.idempotency_results.get(idempotency_key)
        if cached is not None:
            # 同一个键只能属于同一个 payload，不能复用别人的执行结果。
            if cached["payload_sha256"] != proposal.payload_sha256:
                raise ValueError("幂等键已被另一个写入内容使用")

            # 返回缓存结果的副本，避免调用方修改内部缓存。
            replay = dict(cached)
            replay["idempotent_replay"] = True
            self._append_event(
                "write_replayed",
                proposal,
                actor=actor,
                idempotency_key=idempotency_key,
            )
            return replay

        # 没有明确批准就禁止写入；pending 和 rejected 都会在这里被拦截。
        if proposal.status != "approved":
            raise PermissionError(f"写入尚未获批：{proposal.status}")

        # 防止提案对象在内存中被改写：content 必须仍与审批时哈希一致。
        actual_content_hash = sha256_bytes(proposal.content.encode("utf-8"))
        if actual_content_hash != proposal.content_sha256:
            raise RuntimeError("提案内容在批准后发生变化，拒绝执行")

        # 再经过一次路径守卫，不能因为提案来自内存就信任路径。
        target = resolve_inside_workspace(self.workspace, proposal.relative_path)

        # 执行前第二次做乐观锁检查，关闭“批准后、写入前”的竞态窗口。
        if self._current_hash(target) != proposal.base_sha256:
            raise RuntimeError("文件在批准后发生变化，拒绝执行；请重新预览和审批")

        # 所有校验通过后，才允许产生真正的磁盘副作用。
        self._atomic_write_text(target, proposal.content)

        # 写入成功后再推进状态；失败时仍保持 approved，便于排查与重试。
        proposal.status = "executed"
        result = {
            "approval_id": proposal.approval_id,
            "status": proposal.status,
            "path": proposal.relative_path,
            "content_sha256": proposal.content_sha256,
            "payload_sha256": proposal.payload_sha256,
            "idempotency_key": idempotency_key,
            "idempotent_replay": False,
        }

        # 缓存执行结果；相同幂等键重试时只返回结果，不再写磁盘。
        self.idempotency_results[idempotency_key] = dict(result)

        # 最后写入审计日志，带上批准人和幂等键，形成完整证据链。
        self._append_event(
            "write_executed",
            proposal,
            actor=actor,
            approved_by=proposal.approved_by,
            idempotency_key=idempotency_key,
        )
        return result

# ---------------------------------------------------------------------------
# 已完成框架：工作区绑定和工具运行时（不要修改）
# ---------------------------------------------------------------------------


class HomeworkToolRuntime:
    """自动把所有工具绑定到同一个工作区。"""

    def __init__(self, workspace: Path) -> None:
        # 工作区只在运行时创建时指定一次，模型无法在参数中替换它。
        self.workspace = workspace.expanduser().resolve()

        # 审批管理器和重复调用守卫都属于当前 Agent 实例的运行时状态。
        self.approvals = WriteApprovalManager(self.workspace)
        self.repeat_guard = RepeatedCallGuard()

    def read_file(self, path: str) -> dict[str, Any]:
        """读取工作区内文本文件，并只向模型返回相对路径。"""

        # 即使模型传绝对路径或 ../，也必须先经过工作区边界检查。
        target = resolve_inside_workspace(self.workspace, path)
        return {
            "file_path": str(target.relative_to(self.workspace)),
            "content": target.read_text(encoding="utf-8"),
        }

    def list_files(self, path: str) -> dict[str, Any]:
        """列出工作区内目录；不递归展开，避免一次返回过多内容。"""

        target = resolve_inside_workspace(self.workspace, path)

        # 目录内容排序后再返回，保证相同工作区得到稳定结果。
        items = [
            {
                "filename": item.name,
                "type": "file" if item.is_file() else "dir",
            }
            for item in sorted(target.iterdir())
        ]
        return {
            "path": (
                str(target.relative_to(self.workspace))
                if target != self.workspace
                else "."
            ),
            "files": items,
        }

    def search_text(self, query: str, path: str) -> dict[str, Any]:
        """把模型参数转交给已经实现安全边界的搜索函数。"""

        # workspace 由运行时自动注入，模型和学员都不用传。
        return search_text(query, path, workspace=self.workspace)

    def propose_write_file(self, path: str, content: str) -> dict[str, Any]:
        """只生成写入提案；这个工具本身绝不修改目标文件。"""

        return self.approvals.propose(path, content, requested_by="agent")

    def write_file(self, approval_id: str, idempotency_key: str) -> dict[str, Any]:
        """执行已批准提案；所有安全校验集中在审批管理器中。"""

        return self.approvals.execute(
            approval_id,
            idempotency_key,
            actor="agent",
        )


# ---------------------------------------------------------------------------
# 已完成框架：模型配置、工具 Schema 和 Agent Loop
# 离线作业测试不会调用模型，也不需要 API Key。
# ---------------------------------------------------------------------------

# 注意：这个服务的密钥带有 ``sk-`` 前缀。之前漏掉前缀会直接返回 401。
API_KEY = "sk-baabc0c085e94945ace399b561a0c07d"
BASE_URL = "https://api.deepseek.com/v1"
MODEL = "deepseek-flash"


# 这里是模型能够“看见”的工具契约。
# Python 实现放在 HomeworkToolRuntime 中，两者通过完全相同的工具名连接。
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取工作区内文件的完整内容",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出工作区内目录的文件和子目录",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_text",
            "description": "在工作区内搜索文本，最多返回 50 个命中",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["query", "path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_write_file",
            "description": "生成写入预览和审批 ID，但不写文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "执行已经由用户批准的写入",
            "parameters": {
                "type": "object",
                "properties": {
                    "approval_id": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["approval_id", "idempotency_key"],
                "additionalProperties": False,
            },
        },
    },
]


SYSTEM_PROMPT = """你是一个编程助手智能体。

可用工具：read_file、list_files、search_text、propose_write_file、write_file。
修改文件时，必须先生成预览并等待用户批准，再执行写入。
"""


class SimpleAgent:
    """已经接好工具和工作区的最小 Coding Agent。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        client: Any | None = None,
        workspace: Path | None = None,
        max_model_rounds: int = 20,
    ) -> None:
        if client is None:
            if OpenAI is None:
                raise RuntimeError("请先执行：pip install openai")
            # 限制重试次数和单次请求时长，避免课堂演示遇到网络问题时
            # 长时间没有反馈。API 异常会在 chat() 中转成简短中文提示。
            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=30.0,
                max_retries=1,
            )

        self.client = client
        self.model = model
        self.max_model_rounds = max_model_rounds
        self.runtime = HomeworkToolRuntime(workspace or Path.cwd())
        self.tool_registry = {
            "read_file": self.runtime.read_file,
            "list_files": self.runtime.list_files,
            "search_text": self.runtime.search_text,
            "propose_write_file": self.runtime.propose_write_file,
            "write_file": self.runtime.write_file,
        }
        self.conversation: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    def approve_write(
        self,
        approval_id: str,
        approved_by: str = "user",
    ) -> dict[str, Any]:
        """宿主侧审批入口；它不会暴露为模型可以自行调用的工具。"""

        return self.runtime.approvals.approve(
            approval_id,
            approved_by=approved_by,
        )

    def reject_write(
        self,
        approval_id: str,
        rejected_by: str = "user",
    ) -> dict[str, Any]:
        """宿主侧拒绝入口；只有终端用户能够触发。"""

        return self.runtime.approvals.reject(
            approval_id,
            rejected_by=rejected_by,
        )

    def _finish_after_repeat_error(self) -> str:
        """熔断后禁止继续调用工具，只允许模型生成一次总结。"""

        # tool_choice="none" 是硬约束：即使模型还想调用工具也不能继续。
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[dict(item) for item in self.conversation],
            tools=TOOLS,
            tool_choice="none",
        )
        message = response.choices[0].message
        final_text = message.content or "检测到重复工具调用，Trace 已结束。"
        self.conversation.append({"role": "assistant", "content": final_text})
        print(f"🛑 Agent：{final_text}")
        return final_text

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        """把 SDK 的长堆栈转换成适合课堂排障的短消息。"""

        error_name = type(exc).__name__
        raw_message = str(exc).strip() or "没有返回错误详情"

        if error_name == "AuthenticationError":
            advice = "请检查 API_KEY 是否正确，并确认密钥前缀没有遗漏。"
        elif error_name in {"APIConnectionError", "ConnectError"}:
            advice = "请检查网络连接、BASE_URL 和代理设置。"
        elif error_name in {"BadRequestError", "UnprocessableEntityError"}:
            advice = "请检查 MODEL 名称、工具 Schema 和消息格式。"
        elif error_name == "RateLimitError":
            advice = "请求过于频繁或额度不足，请稍后重试。"
        else:
            advice = "请根据错误信息检查模型配置或本轮消息。"

        return f"模型请求失败（{error_name}）：{raw_message}\n排查建议：{advice}"

    def _append_tool_result(self, tool_call_id: str, result: Any) -> None:
        """统一记录并打印工具结果，保证协议消息结构一致。"""

        content = json.dumps(result, ensure_ascii=False)
        print(f"  📋 工具结果：{content}")
        self.conversation.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": content,
            }
        )

    def chat(self, user_message: str) -> str:
        """运行 Agent Loop；学员无需修改。"""

        print(f"\n📝 用户：{user_message}")
        # 每条用户消息是一条独立 Trace。重复调用熔断不应跨 Trace 累积，
        # 否则上一轮最后一次调用会误伤下一轮的第一次调用。
        self.runtime.repeat_guard = RepeatedCallGuard()

        # 如果模型请求失败，回滚到本轮开始前，避免半截消息污染后续问答。
        conversation_checkpoint = len(self.conversation)
        self.conversation.append({"role": "user", "content": user_message})

        try:
            model_round = 0
            while True:
                model_round += 1
                if model_round > self.max_model_rounds:
                    final_text = (
                        f"本轮已达到 {self.max_model_rounds} 次模型调用上限，"
                        "为避免无限循环，Trace 已结束。"
                    )
                    self.conversation.append(
                        {"role": "assistant", "content": final_text}
                    )
                    print(f"🛑 Agent：{final_text}")
                    return final_text

                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=self.conversation,
                    tools=TOOLS,
                    tool_choice="auto",
                )
                message = response.choices[0].message

                if not message.tool_calls:
                    final_text = message.content or ""
                    self.conversation.append(
                        {"role": "assistant", "content": final_text}
                    )
                    print(f"🤖 Agent：{final_text}")
                    return final_text

                # 关键协议：先保存包含全部 tool_calls 的 assistant 消息。
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

                terminate_trace = False
                for call in message.tool_calls:
                    tool_name = call.function.name

                    # 一旦本批次前面的调用触发熔断，后续调用不能再执行；
                    # 但仍必须为每个 tool_call_id 回注结果，否则模型 API
                    # 会因缺少工具响应而返回 400。
                    if terminate_trace:
                        self._append_tool_result(
                            call.id,
                            {
                                "error": {
                                    "code": "trace_already_terminated",
                                    "message": "本批次前序调用已触发熔断，本工具未执行",
                                },
                                "terminate_trace": True,
                            },
                        )
                        continue

                    try:
                        tool_args = json.loads(call.function.arguments)
                        if not isinstance(tool_args, dict):
                            raise TypeError("工具参数必须是 JSON 对象")
                    except Exception as exc:
                        self._append_tool_result(
                            call.id,
                            {
                                "error": {
                                    "code": "invalid_tool_arguments",
                                    "message": str(exc),
                                }
                            },
                        )
                        continue

                    print(
                        f"  🔧 调用工具：{tool_name}"
                        f"({json.dumps(tool_args, ensure_ascii=False)})"
                    )

                    # 熔断判断由作业 2 的实现控制，但接入点已由框架写好。
                    repeat = self.runtime.repeat_guard.observe(tool_name, tool_args)
                    if repeat.should_terminate:
                        self._append_tool_result(
                            call.id,
                            {
                                "error": {
                                    "code": "repeated_tool_call",
                                    "message": "同一工具与参数已连续出现 3 次",
                                    "tool_name": tool_name,
                                    "fingerprint": repeat.fingerprint,
                                    "consecutive_count": repeat.consecutive_count,
                                },
                                "terminate_trace": True,
                            },
                        )
                        terminate_trace = True
                        continue

                    tool_function = self.tool_registry.get(tool_name)
                    try:
                        if tool_function is None:
                            raise ValueError(f"未知工具：{tool_name}")
                        result = tool_function(**tool_args)
                    except Exception as exc:
                        result = {
                            "error": {
                                "code": "tool_execution_error",
                                "type": type(exc).__name__,
                                "message": str(exc),
                            }
                        }

                    self._append_tool_result(call.id, result)

                if terminate_trace:
                    return self._finish_after_repeat_error()
        except Exception as exc:
            # 清除本轮尚未完成的 user/assistant/tool 消息，后续仍可继续问答。
            del self.conversation[conversation_checkpoint:]
            final_text = self._friendly_error(exc)
            print(f"❌ {final_text}")
            return final_text


def main() -> None:
    """真实模型运行入口；离线作业测试不使用它。"""

    if API_KEY == "请填写你的 API Key":
        raise SystemExit("请先填写 homework_agent.py 顶部的 API_KEY")

    coding_agent = SimpleAgent(
        API_KEY,
        BASE_URL,
        MODEL,
        workspace=Path.cwd(),
    )
    print("✅ Homework Coding Agent 已启动。输入 exit 或空行退出。")
    print("审批命令：approve <approval_id>；拒绝命令：reject <approval_id>")

    while True:
        task = input("\n👉 你的任务：").strip()
        if not task or task.lower() == "exit":
            break
        try:
            if task.startswith("approve "):
                approval_id = task.removeprefix("approve ").strip()
                print(coding_agent.approve_write(approval_id))
                continue
            if task.startswith("reject "):
                approval_id = task.removeprefix("reject ").strip()
                print(coding_agent.reject_write(approval_id))
                continue
            coding_agent.chat(task)
        except Exception as exc:
            # 宿主命令输错 approval_id 或状态不正确时，也不应结束程序。
            print(f"❌ 命令执行失败（{type(exc).__name__}）：{exc}")


# ===========================================================================
# 第二节新增代码：Pi 风格 Tools 与 Messages
# ===========================================================================


# ---------------------------------------------------------------------------
# Step 1：三层工具模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    """第一层：模型能够看到的工具“名片”。

    这一层只回答三个问题：工具叫什么、能做什么、参数长什么样。
    它不保存 Python 执行函数，也不知道工作区或 UI 的存在。
    """

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class AgentTool:
    """第二层：Agent Core 实际调度的统一工具接口。

    ``execute`` 已经绑定产品上下文，因此 Agent Loop 只传业务参数即可。
    ``prepare_arguments`` 用于兼容模型常见的、明确可修复的格式差异。
    ``execution_mode`` 告诉调度器该工具能否与同批工具并行。
    """

    tool: Tool
    execute: Callable[[dict[str, Any]], Any]
    prepare_arguments: Callable[[Any], dict[str, Any]]
    execution_mode: str = "sequential"

    @property
    def name(self) -> str:
        """提供便捷属性，调用方无需写 tool.tool.name。"""

        return self.tool.name


@dataclass(frozen=True)
class ToolContext:
    """产品运行上下文；本课只放工作区，生产中还可加入 UI 回调等。"""

    workspace: Path


@dataclass
class ToolDefinition:
    """第三层：产品适配层中的工具定义。

    handler 接收 ToolContext，因此文件工具可以使用当前项目工作区，
    同时 Agent Core 不需要依赖任何具体桌面应用或 Web 产品。
    """

    tool: Tool
    handler: Callable[[ToolContext, dict[str, Any]], Any]
    prepare_arguments: Callable[[Any], dict[str, Any]]
    execution_mode: str = "sequential"


def wrap_tool_definition(
    definition: ToolDefinition,
    context: ToolContext,
) -> AgentTool:
    """把产品 ToolDefinition 包装成 Agent Core 使用的 AgentTool。

    闭包 ``execute`` 捕获 context。调用方以后只需要传 arguments，
    这就是依赖注入：产品环境从外部注入，核心循环不主动寻找全局变量。
    """

    def execute(arguments: dict[str, Any]) -> Any:
        return definition.handler(context, arguments)

    return AgentTool(
        tool=definition.tool,
        execute=execute,
        prepare_arguments=definition.prepare_arguments,
        execution_mode=definition.execution_mode,
    )


def to_openai_tools(tools: dict[str, AgentTool]) -> list[dict[str, Any]]:
    """只把模型需要的字段翻译为 OpenAI 兼容 Tool Schema。"""

    return [
        {
            "type": "function",
            "function": {
                "name": item.tool.name,
                "description": item.tool.description,
                "parameters": item.tool.parameters,
            },
        }
        for item in tools.values()
    ]


# ---------------------------------------------------------------------------
# Step 2：参数预处理与运行时 Schema
# ---------------------------------------------------------------------------


class ToolValidationError(ValueError):
    """表示模型参数不符合工具契约。"""


def prepare_default_arguments(raw_arguments: Any) -> dict[str, Any]:
    """把 JSON 字符串或字典规范为新的字典对象。

    这里只修复“字符串需要 JSON 解析”这种确定格式，不猜测缺失的业务值。
    """

    if isinstance(raw_arguments, str):
        try:
            raw_arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise ToolValidationError(f"工具参数不是合法 JSON：{exc}") from exc

    if not isinstance(raw_arguments, dict):
        raise ToolValidationError("工具参数必须是 JSON object")

    # 返回副本，避免预处理函数修改模型响应对象中的原始参数。
    return dict(raw_arguments)


def prepare_file_arguments(raw_arguments: Any) -> dict[str, Any]:
    """兼容文件工具中已知的 ``file_path`` 别名。"""

    prepared = prepare_default_arguments(raw_arguments)

    # 只有 path 缺失时才接受 file_path；如果两者同时存在，不偷偷覆盖 path。
    if "path" not in prepared and "file_path" in prepared:
        prepared["path"] = prepared.pop("file_path")

    return prepared


def _matches_json_type(value: Any, expected: str) -> bool:
    """实现本课所需的最小 JSON Schema 类型判断。"""

    mapping = {
        "string": str,
        "object": dict,
        "array": list,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
    }
    expected_type = mapping.get(expected)
    if expected_type is None:
        raise ToolValidationError(f"课程示例暂不支持 Schema 类型：{expected}")

    # Python 的 bool 是 int 子类；integer/number 校验时要显式排除布尔值。
    if expected in {"number", "integer"} and isinstance(value, bool):
        return False
    return isinstance(value, expected_type)


def validate_tool_arguments(
    schema: dict[str, Any],
    arguments: dict[str, Any],
) -> None:
    """在真正执行工具前进行不可跳过的运行时校验。"""

    if not isinstance(arguments, dict):
        raise ToolValidationError("工具参数必须是 object")

    properties = schema.get("properties", {})
    required = schema.get("required", [])

    # 第一类错误：模型漏掉了 Python 函数真正需要的参数。
    for field_name in required:
        if field_name not in arguments:
            raise ToolValidationError(f"缺少必填参数：{field_name}")

    # additionalProperties=False 时，未知字段也要拒绝，避免拼写错误被忽略。
    if schema.get("additionalProperties") is False:
        unknown = set(arguments) - set(properties)
        if unknown:
            raise ToolValidationError(
                "包含未知参数：" + ", ".join(sorted(unknown))
            )

    # 第二类错误：字段存在，但 Python 类型与 Schema 不一致。
    for field_name, value in arguments.items():
        field_schema = properties.get(field_name)
        if field_schema is None:
            continue
        expected = field_schema.get("type")
        if expected and not _matches_json_type(value, expected):
            raise ToolValidationError(
                f"参数 {field_name} 必须是 {expected}，"
                f"实际是 {type(value).__name__}"
            )


FILE_PATH_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
    "additionalProperties": False,
}

EDIT_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "old_str": {"type": "string"},
        "new_str": {"type": "string"},
    },
    "required": ["path", "old_str", "new_str"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# 产品文件工具与工具构建
# ---------------------------------------------------------------------------


def _read_file_product(
    context: ToolContext,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """产品层的读取实现；路径已经由 before hook 检查过。"""

    target = resolve_inside_workspace(context.workspace, arguments["path"])
    return {
        "path": str(target.relative_to(context.workspace)),
        "content": target.read_text(encoding="utf-8"),
    }


def _list_files_product(
    context: ToolContext,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """产品层的目录列举实现。"""

    target = resolve_inside_workspace(context.workspace, arguments["path"])
    return {
        "path": (
            "."
            if target == context.workspace.resolve()
            else str(target.relative_to(context.workspace.resolve()))
        ),
        "files": [
            {
                "name": item.name,
                "type": "file" if item.is_file() else "dir",
            }
            for item in sorted(target.iterdir())
        ],
    }


def _edit_file_product(
    context: ToolContext,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """产品层的编辑实现；本节用它演示 sequential 副作用工具。"""

    target = resolve_inside_workspace(context.workspace, arguments["path"])
    old_str = arguments["old_str"]
    new_str = arguments["new_str"]

    if old_str == "":
        target.write_text(new_str, encoding="utf-8")
        return {"path": arguments["path"], "action": "created_or_overwritten"}

    original = target.read_text(encoding="utf-8")
    if old_str not in original:
        raise ValueError("old_str 在目标文件中不存在")

    target.write_text(original.replace(old_str, new_str, 1), encoding="utf-8")
    return {"path": arguments["path"], "action": "edited"}


def build_tool_definitions() -> list[ToolDefinition]:
    """创建产品层的三种文件工具定义。"""

    return [
        ToolDefinition(
            tool=Tool("read_file", "读取工作区中的文本文件", FILE_PATH_SCHEMA),
            handler=_read_file_product,
            prepare_arguments=prepare_file_arguments,
            execution_mode="parallel",
        ),
        ToolDefinition(
            tool=Tool("list_files", "列出工作区中的目录", FILE_PATH_SCHEMA),
            handler=_list_files_product,
            prepare_arguments=prepare_file_arguments,
            execution_mode="parallel",
        ),
        ToolDefinition(
            tool=Tool("edit_file", "创建文件或替换指定文本", EDIT_FILE_SCHEMA),
            handler=_edit_file_product,
            prepare_arguments=prepare_file_arguments,
            # 写操作具有副作用，不能与同批写操作并行。
            execution_mode="sequential",
        ),
    ]


def build_tools(workspace: Path) -> dict[str, AgentTool]:
    """把产品定义绑定到指定工作区，生成 Agent Core 工具注册表。"""

    context = ToolContext(workspace.expanduser().resolve())
    wrapped = [
        wrap_tool_definition(definition, context)
        for definition in build_tool_definitions()
    ]
    return {tool.name: tool for tool in wrapped}


# ---------------------------------------------------------------------------
# Step 3：before hook 与准备管道
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """模型提出的一次工具调用；tool_call_id 是调用与结果之间的主键。

    它属于“准备管道”的输入，而不是三层工具模型本身，所以从 Step 1
    移到 Step 3。arguments 暂时保留 Any，后续由预处理和 Schema 校验收窄。
    """

    tool_call_id: str
    tool_name: str
    arguments: Any


@dataclass
class PreparedToolCall:
    """已经完成查找、预处理、Schema 校验和安全检查的调用。

    只有得到这个对象，调用才可以进入真正的执行阶段。
    """

    call: ToolCall
    tool: AgentTool
    arguments: dict[str, Any]


SENSITIVE_FILENAMES = {
    ".env",
    ".env.local",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
}


def before_tool_call(
    tool: AgentTool,
    arguments: dict[str, Any],
    context: ToolContext,
) -> None:
    """在副作用发生前执行权限与策略检查。

    Schema 只判断形状是否正确；这里判断当前 Agent 是否被允许执行操作。
    """

    path_value = arguments.get("path")
    if path_value is None:
        return

    # resolve_inside_workspace 会解析 .. 和符号链接后再判断真实父子关系。
    target = resolve_inside_workspace(context.workspace, path_value)

    # 即使敏感文件位于工作区内，也不允许模型读取或修改。
    if target.name in SENSITIVE_FILENAMES:
        raise PermissionError(f"禁止访问敏感文件：{target.name}")

    # 示例策略：只允许编辑常见文本文件，避免模型覆盖任意二进制文件。
    if tool.name == "edit_file" and target.suffix.lower() not in {
        ".py", ".txt", ".md", ".json", ".yaml", ".yml", ""
    }:
        raise PermissionError(f"不允许编辑此文件类型：{target.suffix}")


def error_result(
    call: ToolCall,
    stage: str,
    exc: Exception,
) -> "ToolResultMessage":
    """把任意管道异常转换成统一的内部工具结果消息。"""

    return ToolResultMessage(
        tool_call_id=call.tool_call_id,
        tool_name=call.tool_name,
        content={
            "stage": stage,
            "error_type": type(exc).__name__,
            "error": str(exc),
        },
        is_error=True,
    )


def prepare_tool_call(
    call: ToolCall,
    tools: dict[str, AgentTool],
    context: ToolContext,
) -> PreparedToolCall | "ToolResultMessage":
    """依次完成查找、预处理、Schema 校验和 before hook。"""

    tool = tools.get(call.tool_name)
    if tool is None:
        return error_result(
            call,
            "lookup",
            KeyError(f"未知工具：{call.tool_name}"),
        )

    try:
        prepared_arguments = tool.prepare_arguments(call.arguments)
    except Exception as exc:
        return error_result(call, "prepare_arguments", exc)

    try:
        validate_tool_arguments(tool.tool.parameters, prepared_arguments)
    except Exception as exc:
        return error_result(call, "validate_arguments", exc)

    try:
        before_tool_call(tool, prepared_arguments, context)
    except Exception as exc:
        return error_result(call, "before_tool_call", exc)

    return PreparedToolCall(call, tool, prepared_arguments)


# ---------------------------------------------------------------------------
# Step 4：执行、异常消息化与结果治理
# ---------------------------------------------------------------------------


@dataclass
class ExecutedToolCall:
    """底层执行阶段的中间结果；异常保存在 error 字段，不向外抛出。

    它属于 Step 4 的执行结果模型，不属于 Step 1 的工具 Schema。
    """

    prepared: PreparedToolCall
    result: Any = None
    error: Exception | None = None


def execute_prepared_tool_call(prepared: PreparedToolCall) -> ExecutedToolCall:
    """执行已准备调用，并把 Python 异常保存在数据结构中。"""

    try:
        result = prepared.tool.execute(prepared.arguments)
        return ExecutedToolCall(prepared=prepared, result=result)
    except Exception as exc:
        # 异常不能穿过工具边界摧毁整个 Agent Loop。
        return ExecutedToolCall(prepared=prepared, error=exc)


API_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{8,}")
MAX_TOOL_RESULT_TEXT = 2_000


def _clean_tool_value(value: Any) -> Any:
    """递归脱敏并限制工具结果中的超长文本。"""

    if isinstance(value, str):
        cleaned = API_KEY_PATTERN.sub("[REDACTED_API_KEY]", value)
        if len(cleaned) > MAX_TOOL_RESULT_TEXT:
            return cleaned[:MAX_TOOL_RESULT_TEXT] + "…[TRUNCATED]"
        return cleaned
    if isinstance(value, list):
        return [_clean_tool_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _clean_tool_value(item) for key, item in value.items()}
    return value


def after_tool_call(tool: AgentTool, result: Any) -> Any:
    """工具成功后统一治理输出，再允许它进入消息历史。"""

    # 当前策略与具体工具无关，但保留 tool 参数方便未来按工具定制。
    _ = tool
    return _clean_tool_value(result)


def finalize_tool_call(executed: ExecutedToolCall) -> "ToolResultMessage":
    """把执行中间态转换成 Agent 消息系统中的最终工具结果。"""

    call = executed.prepared.call
    if executed.error is not None:
        return error_result(call, "execute", executed.error)

    return ToolResultMessage(
        tool_call_id=call.tool_call_id,
        tool_name=call.tool_name,
        content=after_tool_call(executed.prepared.tool, executed.result),
        is_error=False,
    )


# ---------------------------------------------------------------------------
# Step 5：批量执行与有序回注
# ---------------------------------------------------------------------------


def execute_tool_batch(
    calls: list[ToolCall],
    tools: dict[str, AgentTool],
    context: ToolContext,
) -> list["ToolResultMessage"]:
    """执行一批工具调用，并始终按照模型原始顺序返回结果。"""

    # 准备阶段保持串行，让校验与安全日志具有确定顺序。
    prepared_items = [
        prepare_tool_call(call, tools, context)
        for call in calls
    ]

    # 只有全部调用都准备成功、且全部声明为 parallel，才启用并发。
    all_prepared = all(
        isinstance(item, PreparedToolCall)
        for item in prepared_items
    )
    all_parallel = all_prepared and all(
        item.tool.execution_mode == "parallel"
        for item in prepared_items
        if isinstance(item, PreparedToolCall)
    )

    if all_parallel and len(prepared_items) > 1:
        # executor.map 与输入顺序一致，即使任务真实完成顺序不同。
        with ThreadPoolExecutor(max_workers=len(prepared_items)) as executor:
            executed_items = list(
                executor.map(execute_prepared_tool_call, prepared_items)
            )
        return [finalize_tool_call(item) for item in executed_items]

    results: list[ToolResultMessage] = []
    for item in prepared_items:
        if isinstance(item, ToolResultMessage):
            # 准备阶段已经失败，直接保留对应位置的错误消息。
            results.append(item)
        else:
            results.append(finalize_tool_call(execute_prepared_tool_call(item)))
    return results


# ---------------------------------------------------------------------------
# Step 6：内部消息类型与模型协议翻译
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserMessage:
    """用户输入的内部表示。"""

    content: str


@dataclass
class AssistantMessage:
    """模型回复；既可以含文本，也可以含结构化 ToolCall。"""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class ToolResultMessage:
    """工具结果；调用 ID 保证结果能与原始请求正确配对。"""

    tool_call_id: str
    tool_name: str
    content: Any
    is_error: bool = False


@dataclass(frozen=True)
class AuditMessage:
    """仅供 UI、日志和持久化使用，默认不会发送给模型。"""

    content: str


AgentMessage = UserMessage | AssistantMessage | ToolResultMessage | AuditMessage


def transform_context(
    messages: list[AgentMessage],
    *,
    max_messages: int = 50,
) -> list[AgentMessage]:
    """选择本轮允许进入模型上下文的消息。

    选择与翻译刻意分成两步：这里负责过滤审计消息和裁剪历史，
    ``convert_to_llm`` 只负责协议格式转换。
    """

    visible = [item for item in messages if not isinstance(item, AuditMessage)]
    return visible[-max_messages:]


def convert_to_llm(messages: list[AgentMessage]) -> list[dict[str, Any]]:
    """把内部丰富消息严格翻译成模型支持的三种角色。"""

    converted: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, UserMessage):
            converted.append({"role": "user", "content": message.content})
            continue

        if isinstance(message, AssistantMessage):
            item: dict[str, Any] = {
                "role": "assistant",
                "content": message.content,
            }
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call.tool_call_id,
                        "type": "function",
                        "function": {
                            "name": call.tool_name,
                            "arguments": json.dumps(
                                call.arguments,
                                ensure_ascii=False,
                            ),
                        },
                    }
                    for call in message.tool_calls
                ]
            converted.append(item)
            continue

        if isinstance(message, ToolResultMessage):
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": json.dumps(message.content, ensure_ascii=False),
                }
            )
            continue

        if isinstance(message, AuditMessage):
            # 即使调用方忘记先 transform_context，这里也拒绝审计消息越界。
            continue

        raise TypeError(f"未知内部消息类型：{type(message).__name__}")

    return converted


# ---------------------------------------------------------------------------
# Step 7：把 Tools 与 Messages 接回完整 Agent Loop
# ---------------------------------------------------------------------------


class PiStyleCodingAgent:
    """使用第二节工具管道与消息系统的 Coding Agent。"""

    def __init__(
        self,
        workspace: Path,
        *,
        client: Any | None = None,
        model: str = MODEL,
        max_rounds: int = 12,
    ) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.context = ToolContext(self.workspace)
        self.tools = build_tools(self.workspace)
        self.api_tools = to_openai_tools(self.tools)
        self.model = model
        self.max_rounds = max_rounds

        # 客户端可注入：真实运行用 OpenAI，检查点使用 Fake Client。
        self.client = client or OpenAI(
            api_key=API_KEY,
            base_url=BASE_URL,
            timeout=30.0,
            max_retries=1,
        )

        # 内部状态保存丰富消息，而不是直接保存 OpenAI 字典。
        self.messages: list[AgentMessage] = []

    @staticmethod
    def _parse_model_tool_calls(message: Any) -> list[ToolCall]:
        """把 SDK 返回的工具调用翻译为内部 ToolCall。"""

        calls: list[ToolCall] = []
        for raw_call in message.tool_calls or []:
            try:
                arguments = json.loads(raw_call.function.arguments)
            except json.JSONDecodeError:
                # 保留原始字符串，让 prepare_arguments 产生结构化错误消息。
                arguments = raw_call.function.arguments
            calls.append(
                ToolCall(
                    raw_call.id,
                    raw_call.function.name,
                    arguments,
                )
            )
        return calls

    def chat(self, user_text: str) -> str:
        """运行完整 Agent Loop，直到模型给出无工具调用的最终回答。"""

        self.messages.append(UserMessage(user_text))

        for round_number in range(1, self.max_rounds + 1):
            # 模型每轮只能看到 transform + convert 后的严格协议消息。
            llm_messages = [
                {
                    "role": "system",
                    "content": (
                        "你是编程助手。需要时调用工具；收到工具结果后继续回答。"
                    ),
                },
                *convert_to_llm(transform_context(self.messages)),
            ]

            response = self.client.chat.completions.create(
                model=self.model,
                messages=llm_messages,
                tools=self.api_tools,
                tool_choice="auto",
            )
            raw_message = response.choices[0].message
            calls = self._parse_model_tool_calls(raw_message)

            self.messages.append(
                AssistantMessage(
                    content=raw_message.content,
                    tool_calls=calls,
                )
            )

            if not calls:
                answer = raw_message.content or ""
                print(f"🤖 Agent：{answer}")
                return answer

            print(f"  🔄 第 {round_number} 轮：执行 {len(calls)} 个工具调用")
            results = execute_tool_batch(calls, self.tools, self.context)
            self.messages.extend(results)

            # 审计消息保存在内部历史，但 transform_context 会阻止它进入模型。
            self.messages.append(
                AuditMessage(
                    f"第 {round_number} 轮完成 {len(results)} 个工具调用"
                )
            )

        answer = f"达到 {self.max_rounds} 轮上限，Trace 已终止。"
        self.messages.append(AssistantMessage(content=answer))
        print(f"🛑 Agent：{answer}")
        return answer


class FakeCompletions:
    """模拟兼容 OpenAI 的 chat.completions 接口。"""

    def __init__(self) -> None:
        # 保存请求，检查点可以验证第二轮模型实际看到了哪些消息。
        self.requests: list[dict[str, Any]] = []

        first_message = SimpleNamespace(
            content="我先查看目录。",
            tool_calls=[
                SimpleNamespace(
                    id="call_7",
                    function=SimpleNamespace(
                        name="list_files",
                        arguments=json.dumps({"path": "."}),
                    ),
                )
            ],
        )
        second_message = SimpleNamespace(
            content="目录查看完成。",
            tool_calls=None,
        )

        # 固定两轮响应，离线测试不受网络和模型随机性影响。
        self.responses = iter(
            [
                SimpleNamespace(
                    choices=[SimpleNamespace(message=first_message)]
                ),
                SimpleNamespace(
                    choices=[SimpleNamespace(message=second_message)]
                ),
            ]
        )

    def create(self, **kwargs: Any) -> Any:
        """记录请求并返回下一条固定响应。"""

        self.requests.append(kwargs)
        return next(self.responses)


# ---------------------------------------------------------------------------
# 七个课堂检查点：每完成一个功能立即执行
# ---------------------------------------------------------------------------


def _assert_raises(
    expected_type: type[Exception],
    callback: Callable[[], Any],
) -> Exception:
    """不依赖 unittest 的轻量异常断言，方便直接运行 agent_v2.py。"""

    try:
        callback()
    except expected_type as exc:
        return exc
    except Exception as exc:
        raise AssertionError(
            f"预期 {expected_type.__name__}，实际 {type(exc).__name__}"
        ) from exc
    raise AssertionError(f"预期抛出 {expected_type.__name__}，但没有异常")


def check_step1_tool_layers() -> None:
    """验证三层工具已经正确包装并转换为模型 Schema。

    这个检查点只使用 Step 1 已经定义的类型和函数，不依赖后续
    ``build_tools``、参数校验或安全钩子。因此课堂讲完 Step 1 后，
    学员就能独立理解并验证“产品定义 -> AgentTool -> 模型 Schema”。
    """

    with tempfile.TemporaryDirectory() as tmp:
        context = ToolContext(Path(tmp))

        # 用一个最小产品工具定义演示第三层。handler 可以读取 context，
        # 但模型最终只能看到 name、description 和 parameters。
        definition = ToolDefinition(
            tool=Tool(
                name="read_file",
                description="读取工作区中的文本文件",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            ),
            handler=lambda current_context, arguments: {
                "workspace": str(current_context.workspace),
                "path": arguments["path"],
            },
            prepare_arguments=lambda arguments: dict(arguments),
            execution_mode="parallel",
        )

        # 产品层定义绑定上下文后，转换为 Agent Core 统一调度的第二层。
        agent_tool = wrap_tool_definition(definition, context)
        tools = {agent_tool.name: agent_tool}

        # 第二层还能真正执行，并且不需要调用者重复传 workspace。
        result = agent_tool.execute({"path": "demo.py"})
        assert result["workspace"] == str(Path(tmp))
        assert result["path"] == "demo.py"
        assert agent_tool.execution_mode == "parallel"

        # 到模型边界时，只导出第一层 Tool 中允许公开的 Schema 字段。
        api_tools = to_openai_tools(tools)
        assert api_tools == [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "读取工作区中的文本文件",
                    "parameters": definition.tool.parameters,
                },
            }
        ]


def check_step2_arguments_and_schema() -> None:
    """验证参数兼容、必填字段和类型校验。"""

    prepared = prepare_file_arguments('{"file_path": "demo.py"}')
    assert prepared == {"path": "demo.py"}
    validate_tool_arguments(FILE_PATH_SCHEMA, prepared)

    missing = _assert_raises(
        ToolValidationError,
        lambda: validate_tool_arguments(FILE_PATH_SCHEMA, {}),
    )
    assert "缺少必填参数" in str(missing)

    wrong_type = _assert_raises(
        ToolValidationError,
        lambda: validate_tool_arguments(FILE_PATH_SCHEMA, {"path": 123}),
    )
    assert "必须是 string" in str(wrong_type)


def check_step3_before_hook() -> None:
    """验证越界路径和敏感文件在底层执行前被拒绝。"""

    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        tools = build_tools(workspace)
        context = ToolContext(workspace)

        outside = prepare_tool_call(
            ToolCall("call_outside", "read_file", {"path": "../secret.txt"}),
            tools,
            context,
        )
        sensitive = prepare_tool_call(
            ToolCall("call_env", "read_file", {"path": ".env"}),
            tools,
            context,
        )

    assert isinstance(outside, ToolResultMessage)
    assert outside.is_error
    assert outside.content["stage"] == "before_tool_call"
    assert isinstance(sensitive, ToolResultMessage)
    assert sensitive.is_error
    assert "敏感文件" in sensitive.content["error"]


def check_step4_error_as_message() -> None:
    """验证执行异常消息化，以及成功结果脱敏。"""

    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        tools = build_tools(workspace)
        results = execute_tool_batch(
            [ToolCall("call_missing", "read_file", {"path": "missing.py"})],
            tools,
            ToolContext(workspace),
        )
        cleaned = after_tool_call(
            tools["read_file"],
            {"content": "token=sk-1234567890abcdef"},
        )

    assert len(results) == 1
    assert results[0].is_error
    assert results[0].content["stage"] == "execute"
    assert results[0].tool_call_id == "call_missing"
    assert "[REDACTED_API_KEY]" in cleaned["content"]


def check_step5_batch_order() -> None:
    """验证只读工具可并行，结果仍按原 ToolCall 顺序返回。"""

    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        (workspace / "a.txt").write_text("A", encoding="utf-8")
        (workspace / "b.txt").write_text("B", encoding="utf-8")
        tools = build_tools(workspace)

        results = execute_tool_batch(
            [
                ToolCall("first", "read_file", {"path": "a.txt"}),
                ToolCall("second", "read_file", {"path": "b.txt"}),
            ],
            tools,
            ToolContext(workspace),
        )

    assert [item.tool_call_id for item in results] == ["first", "second"]
    assert [item.content["content"] for item in results] == ["A", "B"]
    assert not any(item.is_error for item in results)


def check_step6_message_boundary() -> None:
    """验证内部消息正确翻译，审计消息不会进入模型上下文。"""

    messages: list[AgentMessage] = [
        UserMessage("读取 demo.py"),
        AssistantMessage(
            content="我先读取文件。",
            tool_calls=[
                ToolCall("call_6", "read_file", {"path": "demo.py"})
            ],
        ),
        ToolResultMessage(
            tool_call_id="call_6",
            tool_name="read_file",
            content={"content": "print('ok')"},
        ),
        AuditMessage("这条审计消息只给 UI 看"),
    ]

    converted = convert_to_llm(transform_context(messages))
    assert [item["role"] for item in converted] == [
        "user",
        "assistant",
        "tool",
    ]
    assert converted[1]["tool_calls"][0]["id"] == "call_6"
    assert converted[2]["tool_call_id"] == "call_6"
    assert "审计消息" not in json.dumps(converted, ensure_ascii=False)


def check_step7_offline_agent_loop() -> None:
    """使用 Fake Client 验证工具与消息共同组成完整闭环。"""

    with tempfile.TemporaryDirectory() as tmp:
        fake_completions = FakeCompletions()
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=fake_completions)
        )
        coding_agent = PiStyleCodingAgent(Path(tmp), client=fake_client)
        answer = coding_agent.chat("列出目录")

        assert answer == "目录查看完成。"
        assert len(fake_completions.requests) == 2

        # 第二轮请求必须包含工具结果，但不能包含内部 AuditMessage。
        second_messages = fake_completions.requests[1]["messages"]
        serialized = json.dumps(second_messages, ensure_ascii=False)
        assert any(item["role"] == "tool" for item in second_messages)
        assert "完成 1 个工具调用" not in serialized


CHECKPOINTS: dict[str, tuple[str, Callable[[], None]]] = {
    "step1": ("三层工具与模型 Schema", check_step1_tool_layers),
    "step2": ("参数预处理与运行时校验", check_step2_arguments_and_schema),
    "step3": ("before hook 安全拦截", check_step3_before_hook),
    "step4": ("错误消息化与结果治理", check_step4_error_as_message),
    "step5": ("批量执行与有序回注", check_step5_batch_order),
    "step6": ("消息翻译边界", check_step6_message_boundary),
    "step7": ("Fake Client 离线 Agent Loop", check_step7_offline_agent_loop),
}


def run_checkpoint(name: str) -> None:
    """运行一个检查点，或按注册顺序运行全部检查点。"""

    if name != "all" and name not in CHECKPOINTS:
        raise ValueError(
            f"未知检查点：{name}；可选值为 {', '.join(CHECKPOINTS)} 或 all"
        )

    selected = list(CHECKPOINTS) if name == "all" else [name]
    for step_name in selected:
        title, callback = CHECKPOINTS[step_name]
        print(f"\n🧪 {step_name}｜{title}")
        callback()
        print(f"✅ {step_name} 通过")

    print(f"\n🎉 已通过 {len(selected)} 组检查。")


def second_lesson_main() -> None:
    """第二节命令行入口：既能跑检查点，也能启动真实模型。"""

    parser = argparse.ArgumentParser(
        description="第二节 Pi 风格 Coding Agent 课堂实战",
    )
    parser.add_argument(
        "--check",
        choices=[*CHECKPOINTS, "all"],
        help="运行指定课堂检查点；all 表示运行全部",
    )
    args = parser.parse_args()

    if args.check:
        run_checkpoint(args.check)
        return

    coding_agent = PiStyleCodingAgent(Path.cwd())
    print("✅ Pi Style Coding Agent 已启动。输入 exit 或空行退出。")
    while True:
        task = input("\n👉 你的任务：").strip()
        if not task or task.lower() == "exit":
            break
        try:
            coding_agent.chat(task)
        except Exception as exc:
            print(f"❌ 问答失败（{type(exc).__name__}）：{exc}")


if __name__ == "__main__":
    second_lesson_main()
