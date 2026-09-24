"""馆际作品借展的领域核心。

只依赖标准库，集中表达四类业务规则：

1. 复合作品结构：实体作品 → 组成区段 → 作者贡献（含合作顺序、题跋）。
2. 借展协议：约束展期、展厅、照度、运输、保险与数字传播用途。
3. 状态交接：出库/到馆/布展/撤展/归还由交接双方签认；重复扫码幂等；
   发现损伤立即冻结后续动作并保全前后图像哈希。冻结由事故状态派生
   （任一事故未解除即冻结）；解除按解除编号幂等，须有处理人、结论与
   证据摘要，全部事故关闭后交接才恢复；状态可快照/恢复以支持重启续办。
4. 策展展签：发布日期确认后锁定证据快照，后来的学术更正只产生新版，
   不改变旧版展签；任意版本都可从展签定位到实体、贡献区段、当前保管
   责任、授权范围与未解除风险。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional


def json_dumps(value: Any) -> str:
    """领域内统一的 JSON 序列化（键排序、不转义中文），供指纹与持久化使用。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

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
    """状态报告：交接时由双方签认，发现损伤时带损伤说明与前后图像哈希。"""

    condition: str  # 良好 / 损伤
    image_hashes: list[str]
    damage_note: str = ""
    before_hashes: list[str] = field(default_factory=list)
    after_hashes: list[str] = field(default_factory=list)
    note: str = ""


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
class Resolution:
    """书面复核解除：处理人、结论与证据摘要缺一不可，按解除编号幂等。"""

    resolution_id: str
    handler: str
    conclusion: str
    evidence_summary: str
    resolved_on: str
    request_fingerprint: str


@dataclass
class Incident:
    """损伤事件：冻结后所有未完成交接，图像哈希作为证据保全。"""

    incident_id: str
    work_id: str
    handover_id: str
    on_date: str
    note: str
    before_hashes: list[str]
    after_hashes: list[str]
    resolved: bool = False
    resolution: Optional[Resolution] = None

    @property
    def resolution_note(self) -> str:
        return self.resolution.conclusion if self.resolution else ""


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

    def __init__(self) -> None:
        self.works: dict[str, Work] = {}
        self.agreements: dict[str, Agreement] = {}
        self._agreement_history: dict[str, list[str]] = {}  # work_id → 协议ID（含历史版本）
        self.handovers: list[Handover] = []
        self._scan_codes: set[str] = set()
        self.incidents: list[Incident] = []
        self.labels: dict[str, list[LabelVersion]] = {}
        # 冻结不再单独存状态位：作品冻结 = 该作品存在未解除事故。
        # 锁只负责进程内并发命令串行化（HTTP 为 ThreadingHTTPServer）。
        self._lock = threading.RLock()
        self._resolution_index: dict[str, str] = {}  # resolution_id → incident_id
        self._resolution_seq = 0

    # -- 作品结构 ----------------------------------------------------------

    def register_work(
        self,
        title: str,
        kind: str,
        owner_org: str,
        segments: Optional[list[dict[str, Any]]] = None,
        contributions: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        with self._lock:
            return self._register_work(title, kind, owner_org, segments, contributions)

    def _register_work(
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
        return self.get_work_view(work.work_id)

    # -- 协议 --------------------------------------------------------------

    def create_agreement(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            return self._create_agreement(payload)

    def _create_agreement(self, payload: dict[str, Any]) -> dict[str, Any]:
        work = self._work(payload["work_id"])
        agreement_id = payload.get("agreement_id") or f"AGR-{work.work_id}-v1"
        self._validate_agreement_id(agreement_id)
        agreement = self._build_agreement(agreement_id, work.work_id, payload, version=1)
        self.agreements[agreement_id] = agreement
        self._agreement_history.setdefault(work.work_id, []).append(agreement_id)
        return self._agreement_view(agreement)

    def reschedule_agreement(
        self, current_agreement_id: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """跨馆改期：协议条款变更生成新版本，旧版保留，已发布展签不受影响。"""
        with self._lock:
            return self._reschedule_agreement(current_agreement_id, changes)

    def _reschedule_agreement(
        self, current_agreement_id: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
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

    def record_handover(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            return self._record_handover(payload)

    def _record_handover(self, payload: dict[str, Any]) -> dict[str, Any]:
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
            incident = Incident(
                incident_id=_new_id("incident"),
                work_id=work.work_id,
                handover_id=handover.handover_id,
                on_date=handover.on_date,
                note=report.damage_note,
                before_hashes=list(report.before_hashes),
                after_hashes=list(report.after_hashes),
            )
            self.incidents.append(incident)
            return self._handover_view(handover, frozen=True, incident_id=incident.incident_id)

        return self._handover_view(handover)

    def resolve_incident(self, incident_id: str, payload: dict[str, Any] | str) -> dict[str, Any]:
        """凭书面复核结论解除一起事故。

        规则：
        - 解除必须携带处理人、复核结论与证据摘要；
        - 同一 ``resolution_id`` 的完全重放返回原结果且不改变任何当前状态；
        - 同一 ``resolution_id`` 内容变化按冲突拒绝（旧请求迟到不会覆盖新结论）；
        - 解除编号全局唯一，不能用于两起不同事故；
        - 作品只有在全部事故关闭后才恢复交接。
        """
        with self._lock:
            data = self._resolution_payload(payload, incident_id)
            incident = next((i for i in self.incidents if i.incident_id == incident_id), None)
            if incident is None:
                raise DomainError(f"损伤事件 {incident_id} 不存在")

            resolution_id = data["resolution_id"]
            fingerprint = self._resolution_fingerprint(incident_id, data)

            # 该解除编号此前已被受理（可能针对另一起事故，或就是本事故的重放）。
            owner_incident_id = self._resolution_index.get(resolution_id)
            if owner_incident_id is not None:
                if owner_incident_id != incident_id:
                    raise ConflictError(
                        f"解除编号 {resolution_id} 已用于事故 {owner_incident_id}，不能重复使用"
                    )
                if incident.resolution is None:
                    # 索引存在但事故未关闭只可能来自损坏的旧数据：拒绝而不是凭空改写状态。
                    raise ConflictError(f"解除编号 {resolution_id} 状态不一致，须人工核查")
                if incident.resolution.request_fingerprint != fingerprint:
                    raise ConflictError(
                        f"解除编号 {resolution_id} 的请求内容与首次受理时不一致，拒绝覆盖"
                    )
                # 完全重放：原结果原样返回，当前状态不做任何改动。
                return self._resolution_view(incident, replayed=True)

            if incident.resolved:
                raise ConflictError(
                    f"损伤事件 {incident_id} 已凭解除 {incident.resolution.resolution_id} 关闭，"
                    "重复解除须使用原解除编号重放"
                )

            resolution = Resolution(
                resolution_id=resolution_id,
                handler=data["handler"],
                conclusion=data["conclusion"],
                evidence_summary=data["evidence_summary"],
                resolved_on=data["resolved_on"],
                request_fingerprint=fingerprint,
            )
            incident.resolved = True
            incident.resolution = resolution
            self._resolution_index[resolution_id] = incident_id
            return self._resolution_view(incident, replayed=False)

    @staticmethod
    def _resolution_payload(payload: dict[str, Any] | str, incident_id: str) -> dict[str, str]:
        # HTTP 请求必须携带解除编号；只给结论字符串的旧式内部调用按事故编号确定性补编。
        legacy_internal = not isinstance(payload, dict)
        if legacy_internal:
            payload = {"conclusion": str(payload)}
        resolution_id = str(payload.get("resolution_id") or "").strip()
        if not resolution_id:
            if not legacy_internal:
                raise DomainError("解除请求必须携带解除编号（resolution_id），用于幂等重放")
            resolution_id = f"RES-{incident_id}"
        conclusion = str(payload.get("conclusion") or payload.get("resolution_note") or "").strip()
        if not conclusion:
            raise DomainError("解除损伤须填写处理与复核结论")
        handler = str(payload.get("handler") or payload.get("handled_by") or "").strip()
        if not handler:
            if not legacy_internal:
                raise DomainError("解除损伤须登记处理人")
            handler = LEGACY_RESOLUTION_HANDLER
        evidence_summary = str(payload.get("evidence_summary") or "").strip()
        if not evidence_summary:
            if not legacy_internal:
                raise DomainError("解除损伤须填写证据摘要")
            evidence_summary = "旧版调用未提供证据摘要，按解除受理记录补登"
        resolved_on = ""
        if payload.get("resolved_on"):
            resolved_on = LoanRegistry._date(payload["resolved_on"], "解除日期").isoformat()
        return {
            "resolution_id": resolution_id,
            "handler": handler,
            "conclusion": conclusion,
            "evidence_summary": evidence_summary,
            "resolved_on": resolved_on,
        }

    @staticmethod
    def _resolution_fingerprint(incident_id: str, data: dict[str, str]) -> str:
        """解除请求内容的指纹：同编号内容必须逐字一致才算重放。"""
        material = json_dumps(
            [
                incident_id,
                data["handler"],
                data["conclusion"],
                data["evidence_summary"],
                data["resolved_on"],
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _assert_lifecycle(self, work_id: str, handover_type: str) -> None:
        completed = [h.type for h in self.handovers if h.work_id == work_id]
        expected = HANDOVER_TYPES[len(completed)]
        if handover_type != expected:
            raise ConflictError(
                f"作品 {work_id} 下一次交接应为 {expected}，不能直接办理 {handover_type}"
            )

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
        if condition == "损伤" and not damage_note:
            raise DomainError("损伤报告必须填写损伤说明")
        return ConditionReport(
            condition=condition,
            image_hashes=hashes,
            damage_note=damage_note,
            before_hashes=[LoanRegistry._image_hash(h) for h in raw.get("before_hashes", [])],
            after_hashes=[LoanRegistry._image_hash(h) for h in raw.get("after_hashes", [])],
            note=str(raw.get("note", "") or ""),
        )

    @staticmethod
    def _image_hash(value: str) -> str:
        """登记图像证据哈希；已是 64 位十六进制（sha256）时原样保全，否则计算。"""
        value = str(value)
        if re.fullmatch(r"[0-9a-fA-F]{64}", value):
            return value.lower()
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    # -- 策展展签 ----------------------------------------------------------

    def create_label(self, work_id: str, narrative: str, citations: list[dict[str, str]]) -> dict[str, Any]:
        with self._lock:
            return self._create_label(work_id, narrative, citations)

    def _create_label(self, work_id: str, narrative: str, citations: list[dict[str, str]]) -> dict[str, Any]:
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
        return self._label_view(work_id, version)

    def publish_label(self, work_id: str, published_on: str) -> dict[str, Any]:
        """发布日期确认：锁定证据快照。快照只含当时的数据，之后不再变化。"""
        with self._lock:
            return self._publish_label(work_id, published_on)

    def _publish_label(self, work_id: str, published_on: str) -> dict[str, Any]:
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
        return self._label_view(work_id, current)

    def correct_label(self, work_id: str, narrative: str, citations: list[dict[str, str]]) -> dict[str, Any]:
        """学术更正：另起新版本，旧版展签与其证据快照原样保留。"""
        with self._lock:
            return self._correct_label(work_id, narrative, citations)

    def _correct_label(self, work_id: str, narrative: str, citations: list[dict[str, str]]) -> dict[str, Any]:
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
        return self._label_view(work_id, new_version)

    def _evidence_snapshot(self, work_id: str) -> dict[str, Any]:
        """发布时点的证据快照：作品、区段、贡献、协议、交接与全部事故/未解除风险。"""
        work = self.works[work_id]
        agreement = self._current_agreement(work_id)
        handovers = [self._handover_view(h) for h in self.handovers if h.work_id == work_id]
        incidents = self._incidents_for(work_id)
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
            # 历史事故全部保全；open_risks 只保留仍开放的，两者口径一致。
            "incidents": incidents,
            "open_risks": [i for i in incidents if not i["resolved"]],
            "frozen": self._is_frozen(work_id),
            "frozen_reasons": self._frozen_reasons(work_id),
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
        with self._lock:
            return self._get_work_view(work_id)

    def _get_work_view(self, work_id: str) -> dict[str, Any]:
        work = self._work(work_id)
        agreement = self._current_agreement(work_id)
        incidents = self._incidents_for(work_id)
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
            "incidents": incidents,
            "frozen": self._is_frozen(work_id),
            "frozen_reasons": self._frozen_reasons(work_id),
        }

    def risk_view(self, work_id: str) -> dict[str, Any]:
        """从展签/策展侧回答：实体在哪、谁保管、授权到哪、风险是否解除。"""
        with self._lock:
            return self._risk_view(work_id)

    def _risk_view(self, work_id: str) -> dict[str, Any]:
        work = self._work(work_id)
        agreement = self._current_agreement(work_id)
        incidents = self._incidents_for(work_id)
        open_risks = [i for i in incidents if not i["resolved"]]
        return {
            "work": work.to_ref(),
            "custody": self._custody(work_id),
            "authorization": agreement.authorization_scope() if agreement else None,
            # 全部历史事故随风险视图返回，开放事故同时列在 open_risks。
            "incidents": incidents,
            "open_risks": open_risks,
            "frozen": bool(open_risks),
            "frozen_reasons": self._frozen_reasons(work_id),
        }

    def locate_segment(self, work_id: str, segment_id: str) -> dict[str, Any]:
        """局部状态争议入口：从区段定位实体、贡献、当前保管与风险。"""
        with self._lock:
            return self._locate_segment(work_id, segment_id)

    def _locate_segment(self, work_id: str, segment_id: str) -> dict[str, Any]:
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
        linked_handover_ids = {
            h.handover_id
            for h in self.handovers
            if h.work_id == work_id and h.linked_segments and segment_id in h.linked_segments
        }
        segment_incidents = [
            self._incident_view(i)
            for i in self.incidents
            if i.work_id == work_id and i.handover_id in linked_handover_ids
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
            "frozen_reasons": self._frozen_reasons(work_id),
        }

    def label_version(self, work_id: str, version: Optional[int] = None) -> dict[str, Any]:
        with self._lock:
            return self._label_version(work_id, version)

    def _label_version(self, work_id: str, version: Optional[int] = None) -> dict[str, Any]:
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
            view["frozen_reason"] = "发现损伤，后续动作冻结"
            incident = next(
                (i for i in self.incidents if i.incident_id == incident_id), None
            ) if incident_id else None
            if incident is not None:
                view["incident_id"] = incident.incident_id
                view["frozen_reasons"] = [self._freeze_reason(incident)]
        return view

    # -- 冻结派生与事故视图 --------------------------------------------------

    def _is_frozen(self, work_id: str) -> bool:
        """冻结完全由事故状态派生：存在任一未解除事故即冻结。"""
        return any(not i.resolved for i in self.incidents if i.work_id == work_id)

    def _open_incident_ids(self, work_id: str) -> list[str]:
        return [i.incident_id for i in self.incidents if i.work_id == work_id and not i.resolved]

    def _freeze_reason(self, incident: Incident) -> dict[str, Any]:
        return {
            "incident_id": incident.incident_id,
            "handover_id": incident.handover_id,
            "on_date": incident.on_date,
            "note": incident.note,
            "reason": f"事故 {incident.incident_id} 尚未解除：{incident.note}",
        }

    def _frozen_reasons(self, work_id: str) -> list[dict[str, Any]]:
        return [
            self._freeze_reason(i)
            for i in self.incidents
            if i.work_id == work_id and not i.resolved
        ]

    def _incidents_for(self, work_id: str) -> list[dict[str, Any]]:
        return [self._incident_view(i) for i in self.incidents if i.work_id == work_id]

    @staticmethod
    def _resolution_dict(resolution: Resolution) -> dict[str, Any]:
        return {
            "resolution_id": resolution.resolution_id,
            "handler": resolution.handler,
            "conclusion": resolution.conclusion,
            "evidence_summary": resolution.evidence_summary,
            "resolved_on": resolution.resolved_on,
        }

    def _incident_view(self, incident: Incident) -> dict[str, Any]:
        view = {
            "incident_id": incident.incident_id,
            "work_id": incident.work_id,
            "handover_id": incident.handover_id,
            "on_date": incident.on_date,
            "note": incident.note,
            "before_hashes": list(incident.before_hashes),
            "after_hashes": list(incident.after_hashes),
            "resolved": incident.resolved,
            "resolution": self._resolution_dict(incident.resolution) if incident.resolution else None,
        }
        # 兼容旧调用方：结论继续以 resolution_note 平铺暴露。
        view["resolution_note"] = incident.resolution.conclusion if incident.resolution else ""
        return view

    def _resolution_view(self, incident: Incident, replayed: bool) -> dict[str, Any]:
        return {
            "incident_id": incident.incident_id,
            "work_id": incident.work_id,
            "resolved": True,
            "replayed": replayed,
            # 只有全部事故关闭，作品才真正恢复交接。
            "work_frozen": self._is_frozen(incident.work_id),
            "open_incident_ids": self._open_incident_ids(incident.work_id),
            "resolution_id": incident.resolution.resolution_id,
            "resolution": self._resolution_dict(incident.resolution),
            "resolution_note": incident.resolution.conclusion,
        }

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

    # -- 持久化（快照 / 恢复，供重启后继续交接） ------------------------------

    SNAPSHOT_FORMAT = 2

    def snapshot(self) -> dict[str, Any]:
        """导出可 JSON 化的完整状态；冻结状态不导出，恢复时由事故重新派生。"""
        with self._lock:
            return {
                "format": self.SNAPSHOT_FORMAT,
                "id_counter": dict(_new_id.counter),
                "works": [self._work_to_dict(w) for w in self.works.values()],
                "agreements": [self._agreement_to_dict(a) for a in self.agreements.values()],
                "agreement_history": {k: list(v) for k, v in self._agreement_history.items()},
                "handovers": [self._handover_to_dict(h) for h in self.handovers],
                "incidents": [self._incident_to_dict(i) for i in self.incidents],
                "labels": {
                    work_id: [self._label_to_dict(v) for v in versions]
                    for work_id, versions in self.labels.items()
                },
            }

    def restore_state(self, data: dict[str, Any]) -> None:
        """从快照恢复；接受 format=1 的旧数据（事故缺少解除标识时确定性补编）。"""
        with self._lock:
            registry = LoanRegistry()
            fmt = int(data.get("format", 1))
            if fmt > self.SNAPSHOT_FORMAT:
                raise DomainError(f"状态快照格式 {fmt} 高于当前支持的 {self.SNAPSHOT_FORMAT}")

            for raw in data.get("works", []):
                work = Work(
                    title=raw["title"], kind=raw["kind"], owner_org=raw["owner_org"],
                    work_id=raw["work_id"],
                )
                for s in raw.get("segments", []):
                    work.segments.append(Segment(
                        label=s["label"], start_cm=s.get("start_cm", 0.0),
                        end_cm=s.get("end_cm", 0.0), segment_id=s["segment_id"],
                        note=s.get("note", ""),
                    ))
                for c in raw.get("contributions", []):
                    work.contributions.append(Contribution(
                        author=c["author"], kind=c.get("kind", "作画"), order=c["order"],
                        segment_id=c.get("segment_id"), contribution_id=c["contribution_id"],
                    ))
                registry.works[work.work_id] = work

            for raw in data.get("agreements", []):
                registry.agreements[raw["agreement_id"]] = Agreement(**{
                    key: raw[key] for key in (
                        "agreement_id", "work_id", "lender_org", "borrower_org",
                        "start_on", "end_on", "gallery", "max_lux", "transport",
                        "insurance", "digital_rights", "version", "supersedes",
                    ) if key in raw
                })
            registry._agreement_history = {
                k: list(v) for k, v in data.get("agreement_history", {}).items()
            }

            for raw in data.get("handovers", []):
                fr = raw["from_party"]
                tr = raw["to_party"]
                rep = raw["report"]
                handover = Handover(
                    handover_id=raw["handover_id"], work_id=raw["work_id"], type=raw["type"],
                    scan_code=raw["scan_code"],
                    from_party=Signature(org=fr.get("org", ""), role=fr["role"], person=fr["person"]),
                    to_party=Signature(org=tr.get("org", ""), role=tr["role"], person=tr["person"]),
                    report=ConditionReport(
                        condition=rep.get("condition", "良好"),
                        image_hashes=list(rep.get("image_hashes", [])),
                        damage_note=rep.get("damage_note", ""),
                        before_hashes=list(rep.get("before_hashes", [])),
                        after_hashes=list(rep.get("after_hashes", [])),
                        note=rep.get("note", ""),
                    ),
                    on_date=raw["on_date"], at_location=raw.get("at_location", ""),
                    linked_segments=list(raw.get("linked_segments", [])),
                )
                registry.handovers.append(handover)
                registry._scan_codes.add(handover.scan_code)

            for raw in data.get("incidents", []):
                incident = Incident(
                    incident_id=raw["incident_id"], work_id=raw["work_id"],
                    handover_id=raw["handover_id"], on_date=raw["on_date"],
                    note=raw.get("note", ""),
                    before_hashes=list(raw.get("before_hashes", [])),
                    after_hashes=list(raw.get("after_hashes", [])),
                    resolved=bool(raw.get("resolved", False)),
                )
                resolution_raw = raw.get("resolution")
                # 任何“已解除但没有解除记录”的数据（典型为 format=1 旧库）都按
                # 事故编号确定性补编，绝不把缺少解除凭证的事故静默当作已关闭。
                if incident.resolved and resolution_raw is None:
                    resolution_raw = self._legacy_resolution(raw)
                if resolution_raw is not None:
                    incident.resolved = True
                    incident.resolution = self._resolution_from_dict(
                        incident.incident_id, resolution_raw
                    )
                    registry._resolution_index[
                        incident.resolution.resolution_id
                    ] = incident.incident_id
                registry.incidents.append(incident)

            for work_id, versions in data.get("labels", {}).items():
                registry.labels[work_id] = [
                    LabelVersion(
                        version=v["version"], status=v["status"], narrative=v["narrative"],
                        citations=v.get("citations", []), evidence=v.get("evidence", {}),
                        published_on=v.get("published_on"), frozen=bool(v.get("frozen", False)),
                    )
                    for v in versions
                ]

            counter = dict(data.get("id_counter", {}))
            # 即便旧快照没带计数器，也要从现有编号推高水位，避免重启后撞号。
            self._bump_id_counter(registry, counter)
            _new_id.counter.clear()
            _new_id.counter.update(counter)

            self.__dict__.update(registry.__dict__)

    @staticmethod
    def _legacy_resolution(raw_incident: dict[str, Any]) -> dict[str, Any]:
        """旧数据缺少解除标识：按事故编号确定性补编，字段取值固定可重放。"""
        incident_id = raw_incident["incident_id"]
        conclusion = str(raw_incident.get("resolution_note") or "（旧数据迁移：原记录未留存结论）")
        return {
            "resolution_id": f"RES-{incident_id}-LEGACY",
            "handler": LEGACY_RESOLUTION_HANDLER,
            "conclusion": conclusion,
            "evidence_summary": "旧版解除记录缺少解除标识，按事故编号确定性补编",
            "resolved_on": raw_incident.get("on_date", ""),
            "legacy": True,
        }

    def _resolution_from_dict(self, incident_id: str, raw: dict[str, Any]) -> Resolution:
        data = {
            "resolution_id": raw["resolution_id"],
            "handler": raw["handler"],
            "conclusion": raw["conclusion"],
            "evidence_summary": raw["evidence_summary"],
            "resolved_on": raw.get("resolved_on", ""),
        }
        return Resolution(
            resolution_id=data["resolution_id"],
            handler=data["handler"],
            conclusion=data["conclusion"],
            evidence_summary=data["evidence_summary"],
            resolved_on=data["resolved_on"],
            request_fingerprint=raw.get("request_fingerprint")
            or self._resolution_fingerprint(incident_id, data),
        )

    @staticmethod
    def _bump_id_counter(registry: "LoanRegistry", counter: dict[str, Any]) -> None:
        """从快照内现有编号推高 ID 水位，旧快照缺计数器时也不会撞号。"""
        def consider(identifier: str) -> None:
            match = re.fullmatch(r"([a-z]+)-(\d+)", str(identifier))
            if match:
                prefix, number = match.group(1), int(match.group(2))
                counter[prefix] = max(int(counter.get(prefix, 0)), number)

        for work in registry.works.values():
            consider(work.work_id)
            for segment in work.segments:
                consider(segment.segment_id)
            for contribution in work.contributions:
                consider(contribution.contribution_id)
        for handover in registry.handovers:
            consider(handover.handover_id)
        for incident in registry.incidents:
            consider(incident.incident_id)

    @staticmethod
    def _work_to_dict(work: Work) -> dict[str, Any]:
        return {
            "title": work.title, "kind": work.kind, "owner_org": work.owner_org,
            "work_id": work.work_id,
            "segments": [
                {"label": s.label, "start_cm": s.start_cm, "end_cm": s.end_cm,
                 "segment_id": s.segment_id, "note": s.note}
                for s in work.segments
            ],
            "contributions": [
                {"author": c.author, "kind": c.kind, "order": c.order,
                 "segment_id": c.segment_id, "contribution_id": c.contribution_id}
                for c in work.contributions
            ],
        }

    @staticmethod
    def _agreement_to_dict(agreement: Agreement) -> dict[str, Any]:
        return {
            "agreement_id": agreement.agreement_id, "work_id": agreement.work_id,
            "lender_org": agreement.lender_org, "borrower_org": agreement.borrower_org,
            "start_on": agreement.start_on, "end_on": agreement.end_on,
            "gallery": agreement.gallery, "max_lux": agreement.max_lux,
            "transport": agreement.transport, "insurance": agreement.insurance,
            "digital_rights": agreement.digital_rights, "version": agreement.version,
            "supersedes": agreement.supersedes,
        }

    @staticmethod
    def _handover_to_dict(handover: Handover) -> dict[str, Any]:
        return {
            "handover_id": handover.handover_id, "work_id": handover.work_id,
            "type": handover.type, "scan_code": handover.scan_code,
            "on_date": handover.on_date, "at_location": handover.at_location,
            "linked_segments": list(handover.linked_segments),
            "from_party": {"org": handover.from_party.org, "role": handover.from_party.role,
                           "person": handover.from_party.person},
            "to_party": {"org": handover.to_party.org, "role": handover.to_party.role,
                         "person": handover.to_party.person},
            "report": {
                "condition": handover.report.condition,
                "image_hashes": list(handover.report.image_hashes),
                "damage_note": handover.report.damage_note,
                "before_hashes": list(handover.report.before_hashes),
                "after_hashes": list(handover.report.after_hashes),
                "note": handover.report.note,
            },
        }

    def _incident_to_dict(self, incident: Incident) -> dict[str, Any]:
        result = {
            "incident_id": incident.incident_id, "work_id": incident.work_id,
            "handover_id": incident.handover_id, "on_date": incident.on_date,
            "note": incident.note,
            "before_hashes": list(incident.before_hashes),
            "after_hashes": list(incident.after_hashes),
            "resolved": incident.resolved, "resolution": None,
        }
        if incident.resolution is not None:
            result["resolution"] = {
                "resolution_id": incident.resolution.resolution_id,
                "handler": incident.resolution.handler,
                "conclusion": incident.resolution.conclusion,
                "evidence_summary": incident.resolution.evidence_summary,
                "resolved_on": incident.resolution.resolved_on,
                "request_fingerprint": incident.resolution.request_fingerprint,
            }
        return result

    @staticmethod
    def _label_to_dict(version: LabelVersion) -> dict[str, Any]:
        return {
            "version": version.version, "status": version.status,
            "narrative": version.narrative, "citations": version.citations,
            "evidence": version.evidence, "published_on": version.published_on,
            "frozen": version.frozen,
        }


LEGACY_RESOLUTION_HANDLER = "（旧数据迁移）"


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
