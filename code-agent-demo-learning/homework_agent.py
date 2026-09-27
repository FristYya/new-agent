"""第一节课后作业 + 第二节生产化扩展：可控的 Homework Coding Agent。

相较于第一节的 ``agent.py``，本文件完成了五次关键升级：

1. 所有文件能力绑定到一个工作区，阻止路径越界；
2. 新增结构化文本搜索，并治理二进制文件、长行和结果数量；
3. 使用稳定指纹识别连续重复工具调用，必要时终止 Trace；
4. 把直接写文件改造成“提案、审批、执行、审计”的状态机；
5. 为模型异常、多工具调用和命令输入增加可恢复的错误边界。

代码中的注释不仅解释“这一行做什么”，还会解释它保护的是哪个生产风险。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import tempfile
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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


if __name__ == "__main__":
    main()

