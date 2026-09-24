"""馆际作品借展的领域核心。

只依赖标准库，集中表达四类业务规则：

1. 复合作品结构：实体作品 → 组成区段 → 作者贡献（含合作顺序、题跋）。
2. 借展协议：约束展期、展厅、照度、运输、保险与数字传播用途。
3. 状态交接：出库/到馆/布展/撤展/归还由交接双方签认；重复扫码幂等；
   发现损伤立即冻结后续动作并保全前后图像哈希。
4. 策展展签：发布日期确认后锁定证据快照，后来的学术更正只产生新版，
   不改变旧版展签；任意版本都可从展签定位到实体、贡献区段、当前保管
   责任、授权范围与未解除风险。
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------

ROLES = ("出借馆", "承借馆", "运输方", "策展人")

WORK_KINDS = ("独立作品", "合作画", "历史图录", "长卷")

CONTRIBUTION_KINDS = ("作画", "题跋", "书引首", "鉴藏印", "题签")

# 交接类型固定，顺序即借展生命周期
HANDOVER_TYPES = ("出库", "到馆", "布展", "撤展", "归还")

# 每次交接后实体所处的保管状态
STATUS_AFTER = {
    "出库": "运输中",
    "到馆": "待布展",
    "布展": "展出中",
    "撤展": "待归还",
    "归还": "已归还",
}

# 各交接类型的法定交出方 / 接收方角色
TRANSFER_PAIRS = {
    "出库": ("出借馆", "运输方"),
    "到馆": ("运输方", "承借馆"),
    "布展": ("承借馆", "承借馆"),
    "撤展": ("承借馆", "运输方"),
    "归还": ("运输方", "出借馆"),
}

LABEL_STATUS = ("草拟", "已发布", "已更正")

# 持久化状态文件格式标识。
STATE_FORMAT = "museum-loan-state/1"

# 旧数据里已解除事故没有解除编号：加载时按此前缀加事故号确定性补齐，
# 使重放与迟到请求的判断不依赖随机生成。
LEGACY_RESOLUTION_PREFIX = "legacy-resolution:"


def _locked(method: Callable) -> Callable:
    """串行化领域变更：并发解除同一事故时只有一个请求能成功。"""

    @functools.wraps(method)
    def wrapper(self: "LoanRegistry", *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class DomainError(ValueError):
    """请求违反领域规则（作为 4xx 返回给调用方）。"""


class ConflictError(DomainError):
    """状态冲突（重复交接、已冻结、已锁定等），语义上是 409。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


def _new_id(prefix: str) -> str:
    _new_id.counter[prefix] = _new_id.counter.get(prefix, 0) + 1
    return f"{prefix}-{_new_id.counter[prefix]:04d}"


_new_id.counter = {}  # type: ignore[attr-defined]


@dataclass
class Work:
    """作品实体：一件可以被独立出借、运输、投保的物理对象。"""

    title: str
    kind: str
    owner_org: str  # 当前权属机构
    work_id: str = field(default_factory=lambda: _new_id("work"))
    segments: list["Segment"] = field(default_factory=list)
    contributions: list["Contribution"] = field(default_factory=list)

    def require_segment(self, segment_id: str) -> "Segment":
        for segment in self.segments:
            if segment.segment_id == segment_id:
                return segment
        raise DomainError(f"区段 {segment_id} 不属于作品 {self.work_id}")

    def to_ref(self) -> dict[str, str]:
        return {"work_id": self.work_id, "title": self.title, "kind": self.kind}


@dataclass
class Segment:
    """组成区段：长卷/合作画上可独立辨认的物理局部。"""

    label: str
    start_cm: float = 0.0
    end_cm: float = 0.0
    segment_id: str = field(default_factory=lambda: _new_id("seg"))
    note: str = ""


@dataclass
class Contribution:
    """作者贡献：谁、以何种方式、按什么合作顺序、落在哪个区段。"""

    author: str
    kind: str
    order: int
    segment_id: Optional[str] = None
    contribution_id: str = field(default_factory=lambda: _new_id("ctrb"))


@dataclass
class Agreement:
    """借展协议：约束条件随协议保存，授权范围由这里派生。"""

    agreement_id: str
    work_id: str
    lender_org: str
    borrower_org: str
    start_on: str  # 展期起（YYYY-MM-DD）
    end_on: str  # 展期止
    gallery: str
    max_lux: int
    transport: dict[str, Any]
    insurance: dict[str, Any]
    digital_rights: dict[str, Any]
    version: int = 1
    supersedes: Optional[str] = None

    def authorization_scope(self) -> dict[str, Any]:
        """从协议条款派生当前授权范围，供展签与风险视图引用。"""
        return {
            "agreement_id": self.agreement_id,
            "version": self.version,
            "exhibition_period": {"start": self.start_on, "end": self.end_on},
            "gallery": self.gallery,
            "max_lux": self.max_lux,
            "transport": self.transport,
            "insurance": self.insurance,
            "digital_rights": self.digital_rights,
        }


@dataclass
class Signature:
    org: str
    role: str
    person: str


@dataclass
class ConditionReport:
    """状态报告：交接时由双方签认，发现损伤时带损伤说明与前后图像哈希。

    一次交接可在长卷不同区段发现多处损伤：``damage_items`` 每项立为一条
    独立事故，须分别复核解除；只给 ``damage_note`` 时归一化为单项，旧请求
    与旧数据语义不变。
    """

    condition: str  # 良好 / 损伤
    image_hashes: list[str]
    damage_note: str = ""
    before_hashes: list[str] = field(default_factory=list)
    after_hashes: list[str] = field(default_factory=list)
    note: str = ""
    damage_items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Handover:
    handover_id: str
    work_id: str
    type: str
    scan_code: str
    from_party: Signature
    to_party: Signature
    report: ConditionReport
    on_date: str
    at_location: str
    linked_segments: list[str] = field(default_factory=list)

    @property
    def damaged(self) -> bool:
        return self.report.condition == "损伤" or bool(self.report.damage_note)


@dataclass
class Incident:
    """损伤事件：冻结后所有未完成交接，图像哈希作为证据保全。

    解除（Resolution）是一条不可变的复核记录：幂等键 ``resolution_id``
    由请求方提供（解除编号），记录处理人、复核结论与证据摘要。同一编号
    内容完全一致的重放返回原结果但不再改变状态；内容变化报冲突；遗留
    数据没有编号时按 ``LEGACY_PREFIX + incident_id`` 确定补齐。
    """

    incident_id: str
    work_id: str
    handover_id: str
    on_date: str
    note: str
    before_hashes: list[str]
    after_hashes: list[str]
    resolved: bool = False
    resolution_id: Optional[str] = None
    resolver: str = ""
    resolution_note: str = ""
    evidence_summary: str = ""
    resolved_on: Optional[str] = None


@dataclass
class LabelVersion:
    """展签的一个不可变版本。发布即锁定证据快照。"""

    version: int
    status: str
    narrative: str
    citations: list[dict[str, str]]
    evidence: dict[str, Any]
    published_on: Optional[str]
    frozen: bool


# ---------------------------------------------------------------------------
# 领域服务
# ---------------------------------------------------------------------------


class LoanRegistry:
    """保存全部借展记录并强制业务规则。

    对外使用命令式方法（register_work / record_handover / ...），
    查询通过 get_work_view / label_version / risk_view 等只读视图。
    """

    def __init__(self, state_file: Optional[str] = None) -> None:
        self.works: dict[str, Work] = {}
        self.agreements: dict[str, Agreement] = {}
        self._agreement_history: dict[str, list[str]] = {}  # work_id → 协议ID（含历史版本）
        self.handovers: list[Handover] = []
        self._scan_codes: set[str] = set()
        self.incidents: list[Incident] = []
        self.labels: dict[str, list[LabelVersion]] = {}
        # 解除编号 → 事故号，保证解除编号全局唯一、并发只成功一次。
        self._resolution_index: dict[str, str] = {}
        # 冻结不是独立写入的状态：只要还有未解除事故，作品就必须保持冻结，
        # 因此由事故清单派生（见 _is_frozen），任何解除重放都无法错误解冻。
        self._lock = threading.RLock()
        self._state_file = state_file
        if state_file and os.path.exists(state_file):
            self.load_state(state_file)

    # -- 作品结构 ----------------------------------------------------------

    @_locked
    def register_work(
        self,
        title: str,
        kind: str,
        owner_org: str,
        segments: Optional[list[dict[str, Any]]] = None,
        contributions: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        if not title or not title.strip():
            raise DomainError("作品名称不能为空")
        if kind not in WORK_KINDS:
            raise DomainError(f"作品类型须为 {WORK_KINDS} 之一")
        if not owner_org or not owner_org.strip():
            raise DomainError("必须登记当前权属机构")

        work = Work(title=title.strip(), kind=kind, owner_org=owner_org.strip())

        segment_ids: list[str] = []
        for raw in segments or []:
            segment_id = raw.get("segment_id") or _new_id("seg")
            if segment_id in segment_ids:
                raise DomainError(f"区段编号 {segment_id} 重复")
            segment = Segment(
                label=raw["label"],
                start_cm=float(raw.get("start_cm", 0.0)),
                end_cm=float(raw.get("end_cm", 0.0)),
                segment_id=segment_id,
                note=raw.get("note", ""),
            )
            if segment.end_cm < segment.start_cm:
                raise DomainError(f"区段 {segment.label} 的终点不能早于起点")
            work.segments.append(segment)
            segment_ids.append(segment.segment_id)

        orders: set[int] = set()
        for raw in contributions or []:
            order = int(raw["order"])
            if order <= 0:
                raise DomainError("合作顺序须从 1 开始")
            if order in orders:
                raise DomainError(f"合作顺序 {order} 重复")
            orders.add(order)
            contribution_kind = raw.get("kind", "作画")
            if contribution_kind not in CONTRIBUTION_KINDS:
                raise DomainError(f"贡献类型须为 {CONTRIBUTION_KINDS} 之一")
            segment_id = raw.get("segment_id")
            if segment_id is not None and segment_id not in segment_ids:
                raise DomainError(f"贡献指向不存在的区段 {segment_id}")
            work.contributions.append(
                Contribution(
                    author=raw["author"],
                    kind=contribution_kind,
                    order=order,
                    segment_id=segment_id,
                )
            )

        self.works[work.work_id] = work
        self._persist()
        return self.get_work_view(work.work_id)

    # -- 协议 --------------------------------------------------------------

    @_locked
    def create_agreement(self, payload: dict[str, Any]) -> dict[str, Any]:
        work = self._work(payload["work_id"])
        agreement_id = payload.get("agreement_id") or f"AGR-{work.work_id}-v1"
        self._validate_agreement_id(agreement_id)
        agreement = self._build_agreement(agreement_id, work.work_id, payload, version=1)
        self.agreements[agreement_id] = agreement
        self._agreement_history.setdefault(work.work_id, []).append(agreement_id)
        self._persist()
        return self._agreement_view(agreement)

    @_locked
    def reschedule_agreement(
        self, current_agreement_id: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """跨馆改期：协议条款变更生成新版本，旧版保留，已发布展签不受影响。"""
        old = self.agreements.get(current_agreement_id)
        if old is None:
            raise DomainError(f"协议 {current_agreement_id} 不存在")
        work = self._work(old.work_id)
        if self._is_frozen(work.work_id):
            raise ConflictError(f"作品 {work.work_id} 已因损伤冻结，须先解除风险")

        merged: dict[str, Any] = {
            "lender_org": old.lender_org,
            "borrower_org": old.borrower_org,
            "start_on": old.start_on,
            "end_on": old.end_on,
            "gallery": old.gallery,
            "max_lux": old.max_lux,
            "transport": old.transport,
            "insurance": old.insurance,
            "digital_rights": old.digital_rights,
        }
        merged.update(changes)

        new_id = changes.get("agreement_id") or self._next_agreement_id(work.work_id, old.version + 1)
        self._validate_agreement_id(new_id)
        agreement = self._build_agreement(new_id, work.work_id, merged, version=old.version + 1)
        agreement.supersedes = current_agreement_id
        self.agreements[new_id] = agreement
        self._agreement_history.setdefault(work.work_id, []).append(new_id)
        self._persist()
        return self._agreement_view(new_id)

    def _build_agreement(
        self, agreement_id: str, work_id: str, payload: dict[str, Any], version: int
    ) -> Agreement:
        start_on = self._date(payload["start_on"], "展期开始")
        end_on = self._date(payload["end_on"], "展期结束")
        if end_on < start_on:
            raise DomainError("展期结束日不能早于开始日")
        max_lux = int(payload["max_lux"])
        if max_lux <= 0:
            raise DomainError("照度上限须为正数（勒克斯）")
        agreement = Agreement(
            agreement_id=agreement_id,
            work_id=work_id,
            lender_org=payload["lender_org"],
            borrower_org=payload["borrower_org"],
            start_on=start_on.isoformat(),
            end_on=end_on.isoformat(),
            gallery=payload["gallery"],
            max_lux=max_lux,
            transport=dict(payload.get("transport") or {}),
            insurance=dict(payload.get("insurance") or {}),
            digital_rights=dict(payload.get("digital_rights") or {}),
            version=version,
        )
        return agreement

    def _validate_agreement_id(self, agreement_id: str) -> None:
        if agreement_id in self.agreements:
            raise ConflictError(f"协议编号 {agreement_id} 已存在")

    @staticmethod
    def _next_agreement_id(work_id: str, version: int) -> str:
        return f"AGR-{work_id}-v{version}"

    # -- 状态交接 ----------------------------------------------------------

    @_locked
    def record_handover(self, payload: dict[str, Any]) -> dict[str, Any]:
        work = self._work(payload["work_id"])
        handover_type = payload["type"]
        if handover_type not in HANDOVER_TYPES:
            raise DomainError(f"交接类型须为 {HANDOVER_TYPES} 之一")

        # 冻结与生命周期先校验，未成立的交接不得消费扫码标识。
        if self._is_frozen(work.work_id):
            raise ConflictError(f"作品 {work.work_id} 已因损伤冻结，后续交接全部中止")

        scan_code = str(payload["scan_code"])
        if not scan_code.strip():
            raise DomainError("扫码标识不能为空")
        if scan_code in self._scan_codes:
            previous = next(h.handover_id for h in self.handovers if h.scan_code == scan_code)
            raise ConflictError(
                f"扫码 {scan_code} 已在交接 {previous} 使用，重复扫码不能产生第二次交接"
            )

        expected_from, expected_to = TRANSFER_PAIRS[handover_type]
        from_party = self._signature(payload["from_party"], expected_from)
        to_party = self._signature(payload["to_party"], expected_to)
        self._assert_lifecycle(work.work_id, handover_type)

        linked_segments = list(payload.get("linked_segments") or [])
        for segment_id in linked_segments:
            work.require_segment(segment_id)

        report = self._condition_report(payload.get("report") or {})
        handover = Handover(
            handover_id=_new_id("handover"),
            work_id=work.work_id,
            type=handover_type,
            scan_code=scan_code,
            from_party=from_party,
            to_party=to_party,
            report=report,
            on_date=self._date(payload["on_date"], "交接日期").isoformat(),
            at_location=str(payload.get("at_location", "")),
            linked_segments=linked_segments,
        )
        self._scan_codes.add(scan_code)
        self.handovers.append(handover)

        if handover.damaged:
            # 一次交接可发现多处损伤：逐项立为独立事故，须分别复核解除。
            if report.damage_items:
                items = report.damage_items
            else:
                items = [
                    {
                        "note": report.damage_note,
                        "segment_id": None,
                        "before_hashes": list(report.before_hashes),
                        "after_hashes": list(report.after_hashes),
                    }
                ]
            incident_ids: list[str] = []
            for item in items:
                incident = Incident(
                    incident_id=_new_id("incident"),
                    work_id=work.work_id,
                    handover_id=handover.handover_id,
                    on_date=handover.on_date,
                    note=item["note"],
                    before_hashes=list(item["before_hashes"]),
                    after_hashes=list(item["after_hashes"]),
                )
                self.incidents.append(incident)
                incident_ids.append(incident.incident_id)
            self._persist()
            view = self._handover_view(handover, frozen=True, incident_id=incident_ids[0])
            view["incident_ids"] = incident_ids
            return view

        self._persist()
        return self._handover_view(handover, frozen=False)

    @_locked
    def resolve_incident(self, incident_id: str, details: Any) -> dict[str, Any]:
        """凭书面复核结论解除事故。

        ``details`` 为请求体，须携带：

        - ``resolution_id``：解除编号（幂等键，全局唯一）；
        - ``resolver``：处理人；
        - ``resolution_note``：复核结论；
        - ``evidence_summary``：证据摘要（图像/报告要点）。

        语义：

        - 事故仍开放：四要素缺一不可，登记后事故关闭；作品只有在**全部**
          事故关闭后才真正解冻（冻结状态由事故清单派生）。
        - 事故已解除且请求编号、内容与原记录**完全一致**：返回原结果，
          但不再改变任何当前状态（完全重放）。
        - 同一解除编号内容变化（处理人/结论/摘要不同）→ 409 冲突。
        - 已解除事故又收到别的编号（迟到的旧请求）→ 409 冲突。
        """
        if isinstance(details, str):
            # 兼容位置参数调用：只给了结论，没有解除编号。
            details = {"resolution_note": details}
        resolution_id = str(details.get("resolution_id") or "").strip()
        resolver = str(details.get("resolver") or "").strip()
        note = str(details.get("resolution_note") or "").strip()
        evidence = str(details.get("evidence_summary") or "").strip()
        resolved_on = self._date(
            details.get("resolved_on") or date.today().isoformat(), "解除日期"
        ).isoformat()

        incident = next((i for i in self.incidents if i.incident_id == incident_id), None)
        if incident is None:
            raise DomainError(f"损伤事件 {incident_id} 不存在")

        if incident.resolved:
            return self._replay_resolution(incident, resolution_id, resolver, note, evidence)

        # 仍开放：解除记录必须完整，缺一不可。
        missing = [
            name
            for name, value in (
                ("resolution_id", resolution_id),
                ("resolver", resolver),
                ("resolution_note", note),
                ("evidence_summary", evidence),
            )
            if not value
        ]
        if missing:
            raise DomainError(f"解除损伤须填写 {'/'.join(missing)}")
        owner = self._resolution_index.get(resolution_id)
        if owner is not None and owner != incident_id:
            raise ConflictError(
                f"解除编号 {resolution_id} 已用于事故 {owner}，不能重复用于其他事故"
            )

        incident.resolved = True
        incident.resolution_id = resolution_id
        incident.resolver = resolver
        incident.resolution_note = note
        incident.evidence_summary = evidence
        incident.resolved_on = resolved_on
        self._resolution_index[resolution_id] = incident_id
        self._persist()
        return self._resolution_view(incident, replayed=False)

    def _replay_resolution(
        self,
        incident: Incident,
        resolution_id: str,
        resolver: str,
        note: str,
        evidence: str,
    ) -> dict[str, Any]:
        """已解除事故的重放判定：完全一致返回原结果，任何差异都报冲突。"""
        stored_id = incident.resolution_id or f"{LEGACY_RESOLUTION_PREFIX}{incident.incident_id}"
        if resolution_id:
            if resolution_id != stored_id:
                raise ConflictError(
                    f"事故 {incident.incident_id} 已由解除编号 {stored_id} 解除，"
                    f"迟到请求 {resolution_id} 不能重复处理"
                )
        elif not stored_id.startswith(LEGACY_RESOLUTION_PREFIX):
            raise ConflictError(
                f"事故 {incident.incident_id} 已由解除编号 {stored_id} 解除，"
                "重放请求必须携带原解除编号"
            )
        incoming = (resolver, note, evidence)
        stored = (incident.resolver, incident.resolution_note, incident.evidence_summary)
        if incoming != stored:
            raise ConflictError(
                f"解除编号 {stored_id} 的请求内容与原解除记录不一致"
                "（处理人/复核结论/证据摘要冲突）"
            )
        # 完全重放：返回原结果，但不写入、不解冻、不改变任何当前状态。
        return self._resolution_view(incident, replayed=True)

    def _assert_lifecycle(self, work_id: str, handover_type: str) -> None:
        completed = [h.type for h in self.handovers if h.work_id == work_id]
        expected = HANDOVER_TYPES[len(completed)]
        if handover_type != expected:
            raise ConflictError(
                f"作品 {work_id} 下一次交接应为 {expected}，不能直接办理 {handover_type}"
            )

    # -- 冻结派生 ----------------------------------------------------------

    def _open_incidents(self, work_id: str) -> list[Incident]:
        return [i for i in self.incidents if i.work_id == work_id and not i.resolved]

    def _is_frozen(self, work_id: str) -> bool:
        """作品是否冻结：只要还有任一未解除事故即为冻结。"""
        return bool(self._open_incidents(work_id))

    def _freeze_reasons(self, work_id: str) -> list[str]:
        """当前冻结原因：逐条列出仍开放的事故。"""
        return [
            f"事故 {i.incident_id}（{i.on_date}）未解除：{i.note}"
            for i in self._open_incidents(work_id)
        ]

    def _incident_view(self, incident: Incident) -> dict[str, Any]:
        """单条事故的完整历史视图（含解除记录）。"""
        view: dict[str, Any] = {
            "incident_id": incident.incident_id,
            "work_id": incident.work_id,
            "handover_id": incident.handover_id,
            "on_date": incident.on_date,
            "note": incident.note,
            "before_hashes": list(incident.before_hashes),
            "after_hashes": list(incident.after_hashes),
            "resolved": incident.resolved,
        }
        if incident.resolved:
            view.update(
                {
                    "resolution_id": incident.resolution_id
                    or f"{LEGACY_RESOLUTION_PREFIX}{incident.incident_id}",
                    "resolver": incident.resolver,
                    "resolution_note": incident.resolution_note,
                    "evidence_summary": incident.evidence_summary,
                    "resolved_on": incident.resolved_on,
                }
            )
        return view

    def _resolution_view(self, incident: Incident, replayed: bool) -> dict[str, Any]:
        work_id = incident.work_id
        still_open = self._open_incidents(work_id)
        frozen = bool(still_open)
        result: dict[str, Any] = {
            "incident_id": incident.incident_id,
            "work_id": work_id,
            "resolved": True,
            "replayed": replayed,
            "resolution_id": incident.resolution_id
            or f"{LEGACY_RESOLUTION_PREFIX}{incident.incident_id}",
            "resolver": incident.resolver,
            "resolution_note": incident.resolution_note,
            "evidence_summary": incident.evidence_summary,
            "resolved_on": incident.resolved_on,
            "work_frozen": frozen,
            "frozen_reason": self._freeze_reasons(work_id),
            "open_incident_ids": [i.incident_id for i in still_open],
            # 全部历史事故，便于调用方核对全貌。
            "incidents": [self._incident_view(i) for i in self.incidents if i.work_id == work_id],
        }
        return result

    @staticmethod
    def _signature(raw: dict[str, Any], expected_role: str) -> Signature:
        if not raw or not raw.get("person"):
            raise DomainError("交接双方都须指定签认人")
        role = raw.get("role", expected_role)
        if role != expected_role:
            raise DomainError(f"该交接位置须由 {expected_role} 签认，收到的是 {role}")
        return Signature(org=raw.get("org", ""), role=role, person=raw["person"])

    @staticmethod
    def _condition_report(raw: dict[str, Any]) -> ConditionReport:
        condition = raw.get("condition", "良好")
        if condition not in ("良好", "损伤"):
            raise DomainError("状态结论须为 良好 或 损伤")
        hashes = [LoanRegistry._image_hash(h) for h in raw.get("image_hashes", [])]
        damage_note = str(raw.get("damage_note", "") or "").strip()

        # 多处损伤：每项独立说明与前后图像哈希，归一化为定序的损伤项。
        damage_items: list[dict[str, Any]] = []
        for idx, raw_item in enumerate(raw.get("damage_items") or []):
            item_note = str((raw_item or {}).get("note", "") or "").strip()
            if not item_note:
                raise DomainError(f"第 {idx + 1} 处损伤必须填写损伤说明")
            damage_items.append(
                {
                    "note": item_note,
                    "segment_id": raw_item.get("segment_id"),
                    "before_hashes": [
                        LoanRegistry._image_hash(h) for h in raw_item.get("before_hashes", [])
                    ],
                    "after_hashes": [
                        LoanRegistry._image_hash(h) for h in raw_item.get("after_hashes", [])
                    ],
                }
            )

        if condition == "损伤" and not damage_note and not damage_items:
            raise DomainError("损伤报告必须填写损伤说明")
        return ConditionReport(
            condition=condition,
            image_hashes=hashes,
            damage_note=damage_note,
            before_hashes=[LoanRegistry._image_hash(h) for h in raw.get("before_hashes", [])],
            after_hashes=[LoanRegistry._image_hash(h) for h in raw.get("after_hashes", [])],
            note=str(raw.get("note", "") or ""),
            damage_items=damage_items,
        )

    @staticmethod
    def _image_hash(value: str) -> str:
        """登记图像证据哈希；已是 64 位十六进制（sha256）时原样保全，否则计算。"""
        value = str(value)
        if re.fullmatch(r"[0-9a-fA-F]{64}", value):
            return value.lower()
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    # -- 策展展签 ----------------------------------------------------------

    @_locked
    def create_label(self, work_id: str, narrative: str, citations: list[dict[str, str]]) -> dict[str, Any]:
        work = self._work(work_id)
        if work_id in self.labels:
            raise ConflictError(f"作品 {work_id} 的展签已存在，应使用更正接口")
        if not narrative or not narrative.strip():
            raise DomainError("展签叙事不能为空")
        version = LabelVersion(
            version=1,
            status="草拟",
            narrative=narrative.strip(),
            citations=self._normalize_citations(citations),
            evidence={},
            published_on=None,
            frozen=False,
        )
        self.labels[work_id] = [version]
        self._persist()
        return self._label_view(work_id, version)

    @_locked
    def publish_label(self, work_id: str, published_on: str) -> dict[str, Any]:
        """发布日期确认：锁定证据快照。快照只含当时的数据，之后不再变化。"""
        versions = self.labels.get(work_id)
        if not versions:
            raise DomainError(f"作品 {work_id} 尚无展签")
        current = versions[-1]
        if current.frozen:
            raise ConflictError(f"展签 v{current.version} 已发布并锁定，不能重复发布")
        day = self._date(published_on, "发布日期")
        current.status = "已发布"
        current.published_on = day.isoformat()
        current.frozen = True
        current.evidence = self._evidence_snapshot(work_id)
        self._persist()
        return self._label_view(work_id, current)

    @_locked
    def correct_label(self, work_id: str, narrative: str, citations: list[dict[str, str]]) -> dict[str, Any]:
        """学术更正：另起新版本，旧版展签与其证据快照原样保留。"""
        versions = self.labels.get(work_id)
        if not versions:
            raise DomainError(f"作品 {work_id} 尚无展签")
        current = versions[-1]
        if not current.frozen:
            raise ConflictError("只能更正已发布的展签；草拟版可直接修改")
        new_version = LabelVersion(
            version=current.version + 1,
            status="草拟",
            narrative=narrative.strip(),
            citations=self._normalize_citations(citations),
            evidence={},
            published_on=None,
            frozen=False,
        )
        versions.append(new_version)
        self._persist()
        return self._label_view(work_id, new_version)

    def _evidence_snapshot(self, work_id: str) -> dict[str, Any]:
        """发布时点的证据快照：作品、区段、贡献、协议、交接与全部风险记录。

        含所有历史事故（开放与已解除，及解除证据）与当时的冻结原因；
        快照在发布时整体固化，之后事故状态再变化也不会回写旧版快照。
        """
        work = self.works[work_id]
        agreement = self._current_agreement(work_id)
        handovers = [self._handover_view(h) for h in self.handovers if h.work_id == work_id]
        incidents = [i for i in self.incidents if i.work_id == work_id]
        open_incidents = [self._incident_view(i) for i in incidents if not i.resolved]
        return {
            "snapshot_on": date.today().isoformat(),
            "work": {
                "work_id": work.work_id,
                "title": work.title,
                "kind": work.kind,
                "owner_org": work.owner_org,
            },
            "segments": [
                {
                    "segment_id": s.segment_id,
                    "label": s.label,
                    "start_cm": s.start_cm,
                    "end_cm": s.end_cm,
                }
                for s in work.segments
            ],
            "contributions": [
                {
                    "contribution_id": c.contribution_id,
                    "author": c.author,
                    "kind": c.kind,
                    "order": c.order,
                    "segment_id": c.segment_id,
                }
                for c in sorted(work.contributions, key=lambda c: c.order)
            ],
            "agreement_version": agreement.authorization_scope() if agreement else None,
            "handovers": handovers,
            "custody": self._custody(work_id),
            "open_risks": open_incidents,
            "incidents": [self._incident_view(i) for i in incidents],
            "frozen": self._is_frozen(work_id),
            "frozen_reason": self._freeze_reasons(work_id),
        }

    @staticmethod
    def _normalize_citations(citations: list[dict[str, str]]) -> list[dict[str, str]]:
        result = []
        for raw in citations or []:
            if not raw.get("ref"):
                raise DomainError("引用必须指向作品关系（work_id 或 segment_id）")
            result.append({"ref": raw["ref"], "note": raw.get("note", "")})
        return result

    # -- 查询视图 ----------------------------------------------------------

    def get_work_view(self, work_id: str) -> dict[str, Any]:
        work = self._work(work_id)
        agreement = self._current_agreement(work_id)
        return {
            "work": {
                "work_id": work.work_id,
                "title": work.title,
                "kind": work.kind,
                "owner_org": work.owner_org,
            },
            "segments": [
                {
                    "segment_id": s.segment_id,
                    "label": s.label,
                    "start_cm": s.start_cm,
                    "end_cm": s.end_cm,
                    "note": s.note,
                }
                for s in work.segments
            ],
            "contributions": [
                {
                    "contribution_id": c.contribution_id,
                    "author": c.author,
                    "kind": c.kind,
                    "order": c.order,
                    "segment_id": c.segment_id,
                }
                for c in sorted(work.contributions, key=lambda c: c.order)
            ],
            "current_agreement": agreement.agreement_id if agreement else None,
            "agreement_versions": list(self._agreement_history.get(work_id, [])),
            "custody": self._custody(work_id),
            "frozen": self._is_frozen(work_id),
            "frozen_reason": self._freeze_reasons(work_id),
            "incidents": [
                self._incident_view(i) for i in self.incidents if i.work_id == work_id
            ],
        }

    def risk_view(self, work_id: str) -> dict[str, Any]:
        """从展签/策展侧回答：实体在哪、谁保管、授权到哪、风险是否解除。"""
        work = self._work(work_id)
        agreement = self._current_agreement(work_id)
        incidents = [i for i in self.incidents if i.work_id == work_id]
        return {
            "work": work.to_ref(),
            "custody": self._custody(work_id),
            "authorization": agreement.authorization_scope() if agreement else None,
            "open_risks": [
                self._incident_view(i) for i in incidents if not i.resolved
            ],
            # 全部历史事故（含已解除及解除证据），与展签快照、解除响应一致。
            "incidents": [self._incident_view(i) for i in incidents],
            "frozen": self._is_frozen(work_id),
            "frozen_reason": self._freeze_reasons(work_id),
        }

    def locate_segment(self, work_id: str, segment_id: str) -> dict[str, Any]:
        """局部状态争议入口：从区段定位实体、贡献、当前保管与风险。"""
        work = self._work(work_id)
        segment = work.require_segment(segment_id)
        linked = [
            {
                "contribution_id": c.contribution_id,
                "author": c.author,
                "kind": c.kind,
                "order": c.order,
            }
            for c in sorted(work.contributions, key=lambda c: c.order)
            if c.segment_id == segment_id
        ]
        segment_handovers = [
            self._handover_view(h)
            for h in self.handovers
            if h.work_id == work_id and (not h.linked_segments or segment_id in h.linked_segments)
        ]
        segment_incidents = [
            self._incident_view(i)
            for i in self.incidents
            if i.work_id == work_id
            and any(
                h.linked_segments and segment_id in h.linked_segments
                for h in self.handovers
                if h.handover_id == i.handover_id
            )
        ]
        return {
            "work": work.to_ref(),
            "segment": {
                "segment_id": segment.segment_id,
                "label": segment.label,
                "start_cm": segment.start_cm,
                "end_cm": segment.end_cm,
            },
            "contributions": linked,
            "custody": self._custody(work_id),
            "segment_handovers": segment_handovers,
            "segment_incidents": segment_incidents,
            "frozen": self._is_frozen(work_id),
            "frozen_reason": self._freeze_reasons(work_id),
        }

    def label_version(self, work_id: str, version: Optional[int] = None) -> dict[str, Any]:
        versions = self.labels.get(self._work(work_id).work_id)
        if not versions:
            raise DomainError(f"作品 {work_id} 尚无展签")
        target = versions[-1] if version is None else next(
            (v for v in versions if v.version == version), None
        )
        if target is None:
            raise DomainError(f"展签 v{version} 不存在")
        return self._label_view(work_id, target)

    def _custody(self, work_id: str) -> dict[str, Any]:
        completed = [h for h in self.handovers if h.work_id == work_id]
        if not completed:
            work = self.works[work_id]
            return {"status": "在库", "custodian_role": "出借馆", "custodian_org": work.owner_org}
        last = completed[-1]
        return {
            "status": STATUS_AFTER[last.type],
            "custodian_role": last.to_party.role,
            "custodian_org": last.to_party.org,
            "since_handover": last.handover_id,
            "since": last.on_date,
        }

    def _current_agreement(self, work_id: str) -> Optional[Agreement]:
        history = self._agreement_history.get(work_id)
        if not history:
            return None
        return self.agreements[history[-1]]

    def _work(self, work_id: str) -> Work:
        work = self.works.get(work_id)
        if work is None:
            raise DomainError(f"作品 {work_id} 不存在")
        return work

    @staticmethod
    def _date(value: str, field_name: str) -> date:
        try:
            return date.fromisoformat(str(value))
        except (TypeError, ValueError):
            raise DomainError(f"{field_name} 须为 YYYY-MM-DD 日期")

    # -- 序列化 ------------------------------------------------------------

    def _agreement_view(self, ref: str | Agreement) -> dict[str, Any]:
        agreement = ref if isinstance(ref, Agreement) else self.agreements[ref]
        return {
            "agreement_id": agreement.agreement_id,
            "work_id": agreement.work_id,
            "version": agreement.version,
            "supersedes": agreement.supersedes,
            "lender_org": agreement.lender_org,
            "borrower_org": agreement.borrower_org,
            "start_on": agreement.start_on,
            "end_on": agreement.end_on,
            "gallery": agreement.gallery,
            "max_lux": agreement.max_lux,
            "transport": agreement.transport,
            "insurance": agreement.insurance,
            "digital_rights": agreement.digital_rights,
        }

    def _handover_view(self, handover: Handover, frozen: bool = False, incident_id: Optional[str] = None) -> dict[str, Any]:
        view = {
            "handover_id": handover.handover_id,
            "work_id": handover.work_id,
            "type": handover.type,
            "scan_code": handover.scan_code,
            "on_date": handover.on_date,
            "at_location": handover.at_location,
            "linked_segments": handover.linked_segments,
            "from_party": {"org": handover.from_party.org, "role": handover.from_party.role, "person": handover.from_party.person},
            "to_party": {"org": handover.to_party.org, "role": handover.to_party.role, "person": handover.to_party.person},
            "signed_by_both": bool(handover.from_party.person and handover.to_party.person),
            "resulting_status": STATUS_AFTER[handover.type],
            "condition": {
                "condition": handover.report.condition,
                "damage_note": handover.report.damage_note,
                "image_hashes": handover.report.image_hashes,
                "before_hashes": handover.report.before_hashes,
                "after_hashes": handover.report.after_hashes,
            },
        }
        if frozen:
            view["frozen"] = True
            view["frozen_reason"] = self._freeze_reasons(handover.work_id) or [
                "发现损伤，后续动作冻结"
            ]
        if incident_id:
            view["incident_id"] = incident_id
        return view

    def _label_view(self, work_id: str, version: LabelVersion) -> dict[str, Any]:
        return {
            "work_id": work_id,
            "version": version.version,
            "status": version.status,
            "frozen": version.frozen,
            "published_on": version.published_on,
            "narrative": version.narrative,
            "citations": version.citations,
            "evidence_snapshot": version.evidence,
        }

    # -- 持久化 ------------------------------------------------------------

    def snapshot_state(self) -> dict[str, Any]:
        """导出全部记录（含 ID 计数器），供重启后无损继续交接。"""
        return {
            "format": STATE_FORMAT,
            "id_counters": dict(_new_id.counter),  # type: ignore[attr-defined]
            "works": [
                {
                    "work_id": w.work_id,
                    "title": w.title,
                    "kind": w.kind,
                    "owner_org": w.owner_org,
                    "segments": [vars(s) for s in w.segments],
                    "contributions": [vars(c) for c in w.contributions],
                }
                for w in self.works.values()
            ],
            "agreements": [vars(a) for a in self.agreements.values()],
            "agreement_history": {k: list(v) for k, v in self._agreement_history.items()},
            "handovers": [
                {
                    "handover_id": h.handover_id,
                    "work_id": h.work_id,
                    "type": h.type,
                    "scan_code": h.scan_code,
                    "from_party": vars(h.from_party),
                    "to_party": vars(h.to_party),
                    "report": vars(h.report),
                    "on_date": h.on_date,
                    "at_location": h.at_location,
                    "linked_segments": list(h.linked_segments),
                }
                for h in self.handovers
            ],
            "incidents": [vars(i) for i in self.incidents],
            "labels": {
                work_id: [vars(v) for v in versions]
                for work_id, versions in self.labels.items()
            },
        }

    def load_state(self, raw: Any) -> None:
        """从快照恢复记录。

        - ``raw`` 为文件路径时读取 JSON，否则视为已解析的状态字典。
        - 旧数据中已解除事故缺少 ``resolution_id`` 等字段时，按
          ``legacy-resolution:<incident_id>`` 确定性补齐，冻结仍由
          “是否存在未解除事故”重新派生，不依赖任何旧的冻结标记。
        """
        if isinstance(raw, str):
            with open(raw, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        if not isinstance(raw, dict):
            raise DomainError("状态快照格式不正确")

        counters = raw.get("id_counters") or {}
        if isinstance(counters, dict):
            for prefix, value in counters.items():
                _new_id.counter[prefix] = max(  # type: ignore[attr-defined]
                    _new_id.counter.get(prefix, 0), int(value)
                )

        self.works = {}
        for raw_work in raw.get("works", []):
            work = Work(
                title=raw_work["title"],
                kind=raw_work["kind"],
                owner_org=raw_work["owner_org"],
                work_id=raw_work["work_id"],
            )
            work.segments = [Segment(**s) for s in raw_work.get("segments", [])]
            work.contributions = [Contribution(**c) for c in raw_work.get("contributions", [])]
            self.works[work.work_id] = work

        self.agreements = {
            a["agreement_id"]: Agreement(**a) for a in raw.get("agreements", [])
        }
        self._agreement_history = {
            k: list(v) for k, v in (raw.get("agreement_history") or {}).items()
        }

        self.handovers = []
        for raw_h in raw.get("handovers", []):
            report_data = dict(raw_h["report"])
            self.handovers.append(
                Handover(
                    handover_id=raw_h["handover_id"],
                    work_id=raw_h["work_id"],
                    type=raw_h["type"],
                    scan_code=raw_h["scan_code"],
                    from_party=Signature(**raw_h["from_party"]),
                    to_party=Signature(**raw_h["to_party"]),
                    report=ConditionReport(
                        condition=report_data.get("condition", "良好"),
                        image_hashes=list(report_data.get("image_hashes", [])),
                        damage_note=report_data.get("damage_note", ""),
                        before_hashes=list(report_data.get("before_hashes", [])),
                        after_hashes=list(report_data.get("after_hashes", [])),
                        note=report_data.get("note", ""),
                        damage_items=list(report_data.get("damage_items", [])),
                    ),
                    on_date=raw_h["on_date"],
                    at_location=raw_h.get("at_location", ""),
                    linked_segments=list(raw_h.get("linked_segments", [])),
                )
            )
        self._scan_codes = {h.scan_code for h in self.handovers}

        self.incidents = []
        self._resolution_index = {}
        for raw_i in raw.get("incidents", []):
            incident = Incident(
                incident_id=raw_i["incident_id"],
                work_id=raw_i["work_id"],
                handover_id=raw_i["handover_id"],
                on_date=raw_i["on_date"],
                note=raw_i.get("note", ""),
                before_hashes=list(raw_i.get("before_hashes", [])),
                after_hashes=list(raw_i.get("after_hashes", [])),
                resolved=bool(raw_i.get("resolved", False)),
                resolution_id=raw_i.get("resolution_id"),
                resolver=raw_i.get("resolver", ""),
                resolution_note=raw_i.get("resolution_note", ""),
                evidence_summary=raw_i.get("evidence_summary", ""),
                resolved_on=raw_i.get("resolved_on"),
            )
            # 旧数据兼容：已解除但无解除编号的记录确定性补齐。
            if incident.resolved and not incident.resolution_id:
                incident.resolution_id = f"{LEGACY_RESOLUTION_PREFIX}{incident.incident_id}"
            if incident.resolution_id:
                self._resolution_index[incident.resolution_id] = incident.incident_id
            self.incidents.append(incident)

        self.labels = {}
        for work_id, versions in (raw.get("labels") or {}).items():
            self.labels[work_id] = [LabelVersion(**v) for v in versions]

    def _persist(self) -> None:
        """变更后原子落盘（仅在配置了状态文件时）。"""
        if not self._state_file:
            return
        directory = os.path.dirname(os.path.abspath(self._state_file))
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".loan-state-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.snapshot_state(), handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._state_file)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise


# ---------------------------------------------------------------------------
# 应用外观：供 HTTP 层调用
# ---------------------------------------------------------------------------


@dataclass
class Route:
    method: str
    pattern: str
    handler: Callable[[LoanRegistry, dict[str, Any], dict[str, str]], dict[str, Any]]


def build_routes() -> list[Route]:
    return [
        Route("POST", r"^/works$", lambda reg, body, _: reg.register_work(
            body["title"], body["kind"], body["owner_org"],
            body.get("segments"), body.get("contributions"),
        )),
        Route("GET", r"^/works/(?P<id>[^/]+)$", lambda reg, _b, p: reg.get_work_view(p["id"])),
        Route("GET", r"^/works/(?P<id>[^/]+)/risk$", lambda reg, _b, p: reg.risk_view(p["id"])),
        Route("GET", r"^/works/(?P<id>[^/]+)/segments/(?P<sid>[^/]+)$",
              lambda reg, _b, p: reg.locate_segment(p["id"], p["sid"])),
        Route("POST", r"^/agreements$", lambda reg, body, _: reg.create_agreement(body)),
        Route("POST", r"^/agreements/(?P<id>[^/]+)/reschedule$",
              lambda reg, body, p: reg.reschedule_agreement(p["id"], body)),
        Route("POST", r"^/handovers$", lambda reg, body, _: reg.record_handover(body)),
        Route("POST", r"^/incidents/(?P<id>[^/]+)/resolve$",
              lambda reg, body, p: reg.resolve_incident(p["id"], body)),
        Route("POST", r"^/works/(?P<id>[^/]+)/labels$",
              lambda reg, body, p: reg.create_label(p["id"], body["narrative"], body.get("citations", []))),
        Route("POST", r"^/works/(?P<id>[^/]+)/labels/publish$",
              lambda reg, body, p: reg.publish_label(p["id"], body["published_on"])),
        Route("POST", r"^/works/(?P<id>[^/]+)/labels/correct$",
              lambda reg, body, p: reg.correct_label(p["id"], body["narrative"], body.get("citations", []))),
        Route("GET", r"^/works/(?P<id>[^/]+)/labels$",
              lambda reg, _b, p: reg.label_version(p["id"], None)),
        Route("GET", r"^/works/(?P<id>[^/]+)/labels/(?P<v>[0-9]+)$",
              lambda reg, _b, p: reg.label_version(p["id"], int(p["v"]))),
    ]
