"""事故解除与作品冻结关系的领域回归契约。

复现并固定第二次运输损伤暴露出的缺陷：

- 复损：第一次事故解除后又出现开放事故时，重放旧解除不得改变冻结状态；
- 旧请求迟到：同一解除编号内容变化报冲突，原结论保留；
- 两起事故逆序解除：只解除一起仍冻结，全部关闭才恢复交接；
- 重复扫码：冻结期间被拒绝的扫码不被消费，解冻后仍可用；
- 并发解除：同一解除编号并发提交只能成功一次；
- 重启：快照落盘/恢复后交接链继续；
- 旧数据：缺少解除标识的已解除事故按确定规则补编；
- 视图一致：风险查询与展签证据快照展示全部历史事故与当前冻结原因。
"""

import json
import threading
import unittest

from domain import (
    ConflictError,
    DomainError,
    Incident,
    LEGACY_RESOLUTION_HANDLER,
    LoanRegistry,
    _new_id,
)
from domain_contract import agreement_payload, handover_payload


class RepeatDamageFixture:
    """出库 → 到馆损伤（事故一）→ 解除 → 布展 → 撤展再次损伤（事故二）。"""

    def __init__(self):
        self.registry = LoanRegistry()
        work = self.registry.register_work("云山无尽图卷", "长卷", "甲馆")
        self.work_id = work["work"]["work_id"]
        self.registry.create_agreement(agreement_payload(self.work_id))
        self.registry.record_handover(
            handover_payload(self.work_id, "出库", "SCAN-1", "2026-09-25"))
        first = self.registry.record_handover(handover_payload(
            self.work_id, "到馆", "SCAN-2", "2026-09-27",
            report={"condition": "损伤", "damage_note": "首运：天杆磕痕",
                    "before_hashes": ["a" * 64], "after_hashes": ["b" * 64]},
        ))
        self.incident_1 = first["incident_id"]
        self.resolution_1 = {
            "resolution_id": "RES-R1",
            "handler": "复核员周某",
            "conclusion": "修复加固后双方确认可继续运输",
            "evidence_summary": "前后比对照 sha256:aaa…/bbb…，修复记录 JG-09",
            "resolved_on": "2026-09-28",
        }
        self.registry.resolve_incident(self.incident_1, dict(self.resolution_1))
        self.registry.record_handover(
            handover_payload(self.work_id, "布展", "SCAN-3", "2026-09-30"))
        second = self.registry.record_handover(handover_payload(
            self.work_id, "撤展", "SCAN-4", "2027-01-05",
            report={"condition": "损伤", "damage_note": "二运：尾纸新折痕",
                    "before_hashes": ["c" * 64], "after_hashes": ["d" * 64]},
        ))
        self.incident_2 = second["incident_id"]


def resolution_payload(**overrides):
    payload = {
        "resolution_id": "RES-X",
        "handler": "复核员吴某",
        "conclusion": "复核通过，可继续流转",
        "evidence_summary": "复核照与修复单齐全",
        "resolved_on": "2026-09-28",
    }
    payload.update(overrides)
    return payload


class RepeatDamageTest(unittest.TestCase):
    def setUp(self):
        self.ctx = RepeatDamageFixture()
        self.registry = self.ctx.registry

    def test_second_incident_refreezes_work(self):
        risk = self.registry.risk_view(self.ctx.work_id)
        self.assertTrue(risk["frozen"])
        self.assertEqual(len(risk["incidents"]), 2)
        self.assertEqual(len(risk["open_risks"]), 1)
        self.assertEqual(risk["open_risks"][0]["incident_id"], self.ctx.incident_2)
        self.assertEqual([r["incident_id"] for r in risk["frozen_reasons"]],
                         [self.ctx.incident_2])

    def test_replay_of_resolved_incident_returns_original_and_keeps_freeze(self):
        before = self.registry.risk_view(self.ctx.work_id)
        replay = self.registry.resolve_incident(
            self.ctx.incident_1, dict(self.ctx.resolution_1))
        # 重放返回原结果，明确标注为重放，且当前冻结原因不变。
        self.assertTrue(replay["replayed"])
        self.assertTrue(replay["resolved"])
        self.assertEqual(replay["resolution_id"], "RES-R1")
        self.assertTrue(replay["work_frozen"])
        self.assertEqual(replay["open_incident_ids"], [self.ctx.incident_2])
        self.assertEqual(replay["resolution"]["conclusion"],
                         self.ctx.resolution_1["conclusion"])

        after = self.registry.risk_view(self.ctx.work_id)
        self.assertEqual(after["frozen"], before["frozen"])
        self.assertEqual(after["open_risks"], before["open_risks"])
        self.assertEqual(after["frozen_reasons"], before["frozen_reasons"])
        # 解冻仍然被挡：第二次事故未关闭前归还不能办理。
        with self.assertRaises(ConflictError):
            self.registry.record_handover(
                handover_payload(self.ctx.work_id, "归还", "SCAN-5", "2027-01-07"))

    def test_replay_does_not_mutate_recorded_resolution(self):
        incident_1 = next(i for i in self.registry.incidents if i.incident_id == self.ctx.incident_1)
        original_conclusion = incident_1.resolution.conclusion
        for _ in range(3):
            self.registry.resolve_incident(self.ctx.incident_1, dict(self.ctx.resolution_1))
        self.assertEqual(incident_1.resolution.conclusion, original_conclusion)
        self.assertEqual(incident_1.resolution.handler, "复核员周某")

    def test_resolving_second_incident_resumes_chain(self):
        result = self.registry.resolve_incident(self.ctx.incident_2, resolution_payload(
            resolution_id="RES-R2", conclusion="尾纸折痕已平整并重新囊护"))
        self.assertFalse(result["replayed"])
        self.assertFalse(result["work_frozen"])
        self.assertEqual(result["open_incident_ids"], [])
        done = self.registry.record_handover(
            handover_payload(self.ctx.work_id, "归还", "SCAN-5", "2027-01-07"))
        self.assertEqual(done["resulting_status"], "已归还")
        self.assertFalse(self.registry.get_work_view(self.ctx.work_id)["frozen"])


class StaleResolutionRequestTest(unittest.TestCase):
    def setUp(self):
        self.registry = LoanRegistry()
        work = self.registry.register_work("秋林群鹿", "独立作品", "甲馆")
        self.work_id = work["work"]["work_id"]
        self.registry.create_agreement(agreement_payload(self.work_id))
        self.registry.record_handover(
            handover_payload(self.work_id, "出库", "SCAN-1", "2026-09-25"))
        damaged = self.registry.record_handover(handover_payload(
            self.work_id, "到馆", "SCAN-2", "2026-09-27",
            report={"condition": "损伤", "damage_note": "边缘磨损",
                    "before_hashes": ["1" * 64], "after_hashes": ["2" * 64]},
        ))
        self.incident_id = damaged["incident_id"]

    def test_same_resolution_id_with_changed_content_is_conflict(self):
        first = resolution_payload()
        accepted = self.registry.resolve_incident(self.incident_id, first)
        self.assertFalse(accepted["replayed"])

        # 迟到的旧请求：同一解除编号，但结论被改写。
        for altered in (
            resolution_payload(conclusion="被旧客户端改写的另一条结论"),
            resolution_payload(handler="另一个处理人"),
            resolution_payload(evidence_summary="另一份证据"),
            resolution_payload(resolved_on="2026-10-01"),
        ):
            with self.assertRaises(ConflictError):
                self.registry.resolve_incident(self.incident_id, altered)

        # 冲突后原结论原样保留；完全一致的重放仍返回原结果。
        incident = next(i for i in self.registry.incidents if i.incident_id == self.incident_id)
        self.assertEqual(incident.resolution.conclusion, "复核通过，可继续流转")
        replay = self.registry.resolve_incident(self.incident_id, resolution_payload())
        self.assertTrue(replay["replayed"])

    def test_resolution_id_cannot_be_reused_for_another_incident(self):
        self.registry.resolve_incident(self.incident_id, resolution_payload())
        # 直接构造第二起开放事故（旧数据/并登记口径），编号不得借用。
        other = Incident(
            incident_id=_new_id("incident"), work_id=self.work_id,
            handover_id="handover-other", on_date="2026-09-29", note="又一处损伤",
            before_hashes=[], after_hashes=[],
        )
        self.registry.incidents.append(other)
        with self.assertRaises(ConflictError):
            self.registry.resolve_incident(other.incident_id, resolution_payload())

    def test_resolution_requires_handler_conclusion_evidence_and_id(self):
        with self.assertRaises(DomainError):
            self.registry.resolve_incident(self.incident_id, {"conclusion": "x"})
        with self.assertRaises(DomainError):
            self.registry.resolve_incident(
                self.incident_id, resolution_payload(resolution_id=""))
        with self.assertRaises(DomainError):
            self.registry.resolve_incident(
                self.incident_id, resolution_payload(handler=""))
        with self.assertRaises(DomainError):
            self.registry.resolve_incident(
                self.incident_id, resolution_payload(evidence_summary=""))
        with self.assertRaises(DomainError):
            self.registry.resolve_incident(
                self.incident_id, resolution_payload(conclusion=""))

    def test_resolving_twice_with_new_id_is_conflict(self):
        self.registry.resolve_incident(self.incident_id, resolution_payload())
        with self.assertRaises(ConflictError):
            self.registry.resolve_incident(
                self.incident_id, resolution_payload(resolution_id="RES-NEW"))


class TwoIncidentsReverseOrderTest(unittest.TestCase):
    """两起事故同时挂账时逆序解除：先关第二起，作品仍冻结；全关才恢复。"""

    def test_close_second_then_first(self):
        registry = LoanRegistry()
        work = registry.register_work("千里江山", "长卷", "甲馆")
        work_id = work["work"]["work_id"]
        registry.create_agreement(agreement_payload(work_id))
        registry.record_handover(
            handover_payload(work_id, "出库", "SCAN-1", "2026-09-25"))
        registry.record_handover(
            handover_payload(work_id, "到馆", "SCAN-2", "2026-09-27"))
        # 两起开放事故同时存在（可由旧数据迁移/并登记进入）。
        incident_1 = Incident(
            incident_id=_new_id("incident"), work_id=work_id,
            handover_id=registry.handovers[0].handover_id,
            on_date="2026-09-25", note="事故一：引首磨白",
            before_hashes=["1" * 64], after_hashes=["2" * 64],
        )
        incident_2 = Incident(
            incident_id=_new_id("incident"), work_id=work_id,
            handover_id=registry.handovers[1].handover_id,
            on_date="2026-09-27", note="事故二：画心水印",
            before_hashes=["3" * 64], after_hashes=["4" * 64],
        )
        registry.incidents.extend([incident_1, incident_2])

        risk = registry.risk_view(work_id)
        self.assertTrue(risk["frozen"])
        self.assertEqual({r["incident_id"] for r in risk["frozen_reasons"]},
                         {incident_1.incident_id, incident_2.incident_id})
        with self.assertRaises(ConflictError):
            registry.record_handover(
                handover_payload(work_id, "布展", "SCAN-3", "2026-09-30"))

        # 逆序：先解除事故二。
        closed_second = registry.resolve_incident(
            incident_2.incident_id, resolution_payload(resolution_id="RES-2"))
        self.assertTrue(closed_second["work_frozen"])
        self.assertEqual(closed_second["open_incident_ids"], [incident_1.incident_id])
        self.assertTrue(registry.get_work_view(work_id)["frozen"])
        with self.assertRaises(ConflictError):
            registry.record_handover(
                handover_payload(work_id, "布展", "SCAN-3", "2026-09-30"))

        # 再解除事故一，全部事故关闭后才恢复交接。
        closed_first = registry.resolve_incident(
            incident_1.incident_id, resolution_payload(resolution_id="RES-1"))
        self.assertFalse(closed_first["work_frozen"])
        registry.record_handover(
            handover_payload(work_id, "布展", "SCAN-3", "2026-09-30"))

        # 风险视图保留全部历史事故，只是不再冻结。
        risk_after = registry.risk_view(work_id)
        self.assertFalse(risk_after["frozen"])
        self.assertEqual(len(risk_after["incidents"]), 2)
        self.assertEqual(risk_after["open_risks"], [])
        self.assertTrue(all(i["resolved"] for i in risk_after["incidents"]))


class FrozenScanNotConsumedTest(unittest.TestCase):
    def test_scan_rejected_while_frozen_is_usable_after_resolution(self):
        registry = LoanRegistry()
        work = registry.register_work("松风图", "独立作品", "甲馆")
        work_id = work["work"]["work_id"]
        registry.create_agreement(agreement_payload(work_id))
        registry.record_handover(
            handover_payload(work_id, "出库", "SCAN-1", "2026-09-25"))
        registry.record_handover(handover_payload(
            work_id, "到馆", "SCAN-2", "2026-09-27",
            report={"condition": "损伤", "damage_note": "到馆折痕",
                    "before_hashes": ["1" * 64], "after_hashes": ["2" * 64]},
        ))
        incident_id = registry.risk_view(work_id)["open_risks"][0]["incident_id"]
        # 冻结期间尝试布展，扫码先被冻结规则拦下，不应被消费。
        with self.assertRaises(ConflictError):
            registry.record_handover(
                handover_payload(work_id, "布展", "SCAN-3", "2026-09-30"))
        registry.resolve_incident(incident_id, resolution_payload(resolution_id="RES-1"))
        accepted = registry.record_handover(
            handover_payload(work_id, "布展", "SCAN-3", "2026-09-30"))
        self.assertEqual(accepted["scan_code"], "SCAN-3")
        # 同一扫码第二次仍按重复扫码拒绝。
        with self.assertRaises(ConflictError):
            registry.record_handover(
                handover_payload(work_id, "撤展", "SCAN-3", "2027-01-05"))


class ConcurrentResolutionTest(unittest.TestCase):
    def test_concurrent_resolution_succeeds_once(self):
        registry = LoanRegistry()
        work = registry.register_work("寒林图", "独立作品", "甲馆")
        work_id = work["work"]["work_id"]
        registry.create_agreement(agreement_payload(work_id))
        registry.record_handover(
            handover_payload(work_id, "出库", "SCAN-1", "2026-09-25"))
        damaged = registry.record_handover(handover_payload(
            work_id, "到馆", "SCAN-2", "2026-09-27",
            report={"condition": "损伤", "damage_note": "并发竞争用损伤",
                    "before_hashes": ["1" * 64], "after_hashes": ["2" * 64]},
        ))
        incident_id = damaged["incident_id"]

        results: list[dict] = []
        errors: list[Exception] = []
        start = threading.Barrier(8)

        def submit():
            start.wait()
            try:
                results.append(registry.resolve_incident(
                    incident_id, resolution_payload()))
            except (ConflictError, DomainError) as error:
                errors.append(error)

        threads = [threading.Thread(target=submit) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # 同内容并发：恰好一次首次受理，其余全部作为重放返回，无异常。
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(sum(1 for r in results if not r["replayed"]), 1)
        self.assertEqual(sum(1 for r in results if r["replayed"]), 7)
        incident = next(i for i in registry.incidents if i.incident_id == incident_id)
        self.assertTrue(incident.resolved)
        self.assertEqual(incident.resolution.resolution_id, "RES-X")
        # 解冻后交接可继续。
        registry.record_handover(
            handover_payload(work_id, "布展", "SCAN-3", "2026-09-30"))

    def test_concurrent_conflicting_content_only_one_wins(self):
        registry = LoanRegistry()
        work = registry.register_work("盘车图", "独立作品", "甲馆")
        work_id = work["work"]["work_id"]
        registry.create_agreement(agreement_payload(work_id))
        registry.record_handover(
            handover_payload(work_id, "出库", "SCAN-1", "2026-09-25"))
        damaged = registry.record_handover(handover_payload(
            work_id, "到馆", "SCAN-2", "2026-09-27",
            report={"condition": "损伤", "damage_note": "竞争内容",
                    "before_hashes": ["1" * 64], "after_hashes": ["2" * 64]},
        ))
        incident_id = damaged["incident_id"]

        accepted: list[dict] = []
        conflicts = {"changed": 0, "other": 0}
        lock = threading.Lock()
        start = threading.Barrier(6)

        def submit(altered: bool):
            start.wait()
            payload = resolution_payload(
                conclusion="迟到旧请求的结论") if altered else resolution_payload()
            try:
                result = registry.resolve_incident(incident_id, payload)
                with lock:
                    accepted.append(result)
            except ConflictError:
                with lock:
                    conflicts["changed" if altered else "other"] += 1

        threads = (
            [threading.Thread(target=submit, args=(False,)) for _ in range(3)]
            + [threading.Thread(target=submit, args=(True,)) for _ in range(3)]
        )
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # 先到的那一组内容整体成立（1 次首次受理 + 2 次重放），
        # 另一组 3 个内容不同的请求全部冲突；状态只可能落在其中一种结论上。
        self.assertEqual(len(accepted), 3)
        self.assertEqual(sum(1 for r in accepted if not r["replayed"]), 1)
        self.assertEqual(sum(1 for r in accepted if r["replayed"]), 2)
        self.assertEqual(len({r["resolution_note"] for r in accepted}), 1)
        self.assertEqual(conflicts["changed"] + conflicts["other"], 3)


class SnapshotRestartTest(unittest.TestCase):
    def _build_chain_to_second_damage(self) -> tuple[LoanRegistry, str, str, str]:
        registry = LoanRegistry()
        work = registry.register_work("重启长卷", "长卷", "甲馆")
        work_id = work["work"]["work_id"]
        registry.create_agreement(agreement_payload(work_id))
        registry.record_handover(
            handover_payload(work_id, "出库", "SCAN-1", "2026-09-25"))
        registry.record_handover(handover_payload(
            work_id, "到馆", "SCAN-2", "2026-09-27",
            report={"condition": "损伤", "damage_note": "首运损伤",
                    "before_hashes": ["1" * 64], "after_hashes": ["2" * 64]},
        ))
        incident_1 = registry.risk_view(work_id)["open_risks"][0]["incident_id"]
        registry.resolve_incident(incident_1, resolution_payload(resolution_id="RES-1"))
        registry.record_handover(
            handover_payload(work_id, "布展", "SCAN-3", "2026-09-30"))
        registry.record_handover(handover_payload(
            work_id, "撤展", "SCAN-4", "2027-01-05",
            report={"condition": "损伤", "damage_note": "撤展复损",
                    "before_hashes": ["3" * 64], "after_hashes": ["4" * 64]},
        ))
        incident_2 = registry.risk_view(work_id)["open_risks"][0]["incident_id"]
        return registry, work_id, incident_1, incident_2

    def test_restart_then_resolve_and_complete_return(self):
        registry, work_id, incident_1, incident_2 = self._build_chain_to_second_damage()
        data = registry.snapshot()
        # 序列化/反序列化一次，模拟落盘后换进程启动。
        restarted = LoanRegistry()
        restarted.restore_state(json.loads(json.dumps(data, ensure_ascii=False)))

        risk = restarted.risk_view(work_id)
        self.assertTrue(risk["frozen"])
        self.assertEqual(risk["open_risks"][0]["incident_id"], incident_2)
        with self.assertRaises(ConflictError):
            restarted.record_handover(
                handover_payload(work_id, "归还", "SCAN-5", "2027-01-07"))

        # 重放第一次解除：仍然只是原结果，重启不改变幂等语义。
        replay = restarted.resolve_incident(incident_1, resolution_payload(resolution_id="RES-1"))
        self.assertTrue(replay["replayed"])
        self.assertTrue(restarted.risk_view(work_id)["frozen"])

        restarted.resolve_incident(incident_2, resolution_payload(
            resolution_id="RES-2", conclusion="复损已复核，准予归还"))
        done = restarted.record_handover(
            handover_payload(work_id, "归还", "SCAN-5", "2027-01-07"))
        self.assertEqual(done["resulting_status"], "已归还")

        # 再重启一次：已归还状态与全部历史事故保持。
        again = LoanRegistry()
        again.restore_state(json.loads(json.dumps(restarted.snapshot(), ensure_ascii=False)))
        self.assertEqual(again.get_work_view(work_id)["custody"]["status"], "已归还")
        self.assertEqual(len(again.risk_view(work_id)["incidents"]), 2)
        self.assertFalse(again.risk_view(work_id)["frozen"])

    def test_legacy_snapshot_without_resolution_id_gets_deterministic_id(self):
        registry, work_id, incident_1, _ = self._build_chain_to_second_damage()
        data = registry.snapshot()
        # 模拟 format=1 旧数据：事故已解除但没有解除标识，只有结论字符串。
        data["format"] = 1
        for raw in data["incidents"]:
            if raw["incident_id"] == incident_1:
                raw["resolved"] = True
                raw["resolution"] = None
                raw["resolution_note"] = "旧版留下的复核结论"

        restored = LoanRegistry()
        restored.restore_state(data)
        incident = next(i for i in restored.incidents if i.incident_id == incident_1)
        self.assertTrue(incident.resolved)
        self.assertEqual(incident.resolution.resolution_id, f"RES-{incident_1}-LEGACY")
        self.assertEqual(incident.resolution.handler, LEGACY_RESOLUTION_HANDLER)
        self.assertEqual(incident.resolution.conclusion, "旧版留下的复核结论")
        # 确定性：同一份旧数据再恢复一次，补编结果完全一致。
        again = LoanRegistry()
        again.restore_state(json.loads(json.dumps(data, ensure_ascii=False)))
        incident_again = next(i for i in again.incidents if i.incident_id == incident_1)
        self.assertEqual(incident_again.resolution.resolution_id,
                         incident.resolution.resolution_id)
        # 另一起事故仍开放，冻结照常派生，旧解除的补编不会解冻。
        self.assertTrue(restored.risk_view(work_id)["frozen"])


class IncidentHistoryViewsTest(unittest.TestCase):
    def test_risk_and_label_snapshot_show_full_history_and_freeze_reasons(self):
        ctx = RepeatDamageFixture()
        registry = ctx.registry
        work_id = ctx.work_id

        registry.create_label(work_id, "运输伤痕可追溯的长卷", [])
        published = registry.publish_label(work_id, "2027-01-06")
        snapshot = published["evidence_snapshot"]
        self.assertTrue(snapshot["frozen"])
        self.assertEqual(len(snapshot["incidents"]), 2)
        self.assertEqual(len(snapshot["open_risks"]), 1)
        self.assertEqual(snapshot["open_risks"][0]["incident_id"], ctx.incident_2)
        self.assertEqual([r["incident_id"] for r in snapshot["frozen_reasons"]],
                         [ctx.incident_2])
        # 历史事故一带着解除结论留在快照里。
        first = next(i for i in snapshot["incidents"] if i["incident_id"] == ctx.incident_1)
        self.assertTrue(first["resolved"])
        self.assertEqual(first["resolution"]["resolution_id"], "RES-R1")

        # 解除事故二后发布新版展签：全部事故在档，但冻结原因清空。
        registry.resolve_incident(ctx.incident_2, resolution_payload(
            resolution_id="RES-R2", conclusion="复损复核通过"))
        registry.correct_label(work_id, "归还前复核记录", [])
        new_published = registry.publish_label(work_id, "2027-01-06")
        new_snapshot = new_published["evidence_snapshot"]
        self.assertFalse(new_snapshot["frozen"])
        self.assertEqual(new_snapshot["frozen_reasons"], [])
        self.assertEqual(len(new_snapshot["incidents"]), 2)
        self.assertEqual(new_snapshot["open_risks"], [])
        # 旧版展签快照永不改变。
        old = registry.label_version(work_id, 1)["evidence_snapshot"]
        self.assertTrue(old["frozen"])
        self.assertEqual(len(old["open_risks"]), 1)


if __name__ == "__main__":
    unittest.main()
