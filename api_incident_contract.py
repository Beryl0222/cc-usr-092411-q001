"""事故解除/作品冻结的 HTTP 层回归契约。

覆盖真实 ThreadingHTTPServer 上的：
- 复损后重放第一次解除仍保持冻结、风险视图展示历史事故；
- 同一解除编号内容变化返回 409，缺处理人/编号返回 400；
- 并发解除只有一次首次受理；
- 状态文件重启后继续交接（归还）；
- 冻结期间重复扫码的拒绝/解冻后可用在接口侧同样成立。
"""

import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import service
from service import Handler


PAIRS = {
    "出库": ("出借馆", "运输方", "甲馆", "长风运输"),
    "到馆": ("运输方", "承借馆", "长风运输", "乙馆"),
    "布展": ("承借馆", "承借馆", "乙馆", "乙馆"),
    "撤展": ("承借馆", "运输方", "乙馆", "长风运输"),
    "归还": ("运输方", "出借馆", "长风运输", "甲馆"),
}


def request_json(base_url, method, path, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(f"{base_url}{path}", data=data, headers=headers, method=method)
    return urlopen(request, timeout=5)


def post(base_url, path, payload):
    return request_json(base_url, "POST", path, payload)


def read_error(error):
    return error.code, json.loads(error.read().decode("utf-8"))


def resolution(resolution_id="RES-1", **overrides):
    payload = {
        "resolution_id": resolution_id,
        "handler": "借展专员钱某",
        "conclusion": "修复师与双方馆员复核，确认可继续流转",
        "evidence_summary": "前后比对照片 sha256 齐全，附修复工单",
        "resolved_on": "2026-09-28",
    }
    payload.update(overrides)
    return payload


def handover_body(work_id, htype, scan, day, report=None, location="甲馆库房"):
    fr, tr, forg, torg = PAIRS[htype]
    return {
        "work_id": work_id, "type": htype, "scan_code": scan,
        "on_date": day, "at_location": location,
        "from_party": {"org": forg, "role": fr, "person": "甲"},
        "to_party": {"org": torg, "role": tr, "person": "乙"},
        "report": report or {"condition": "良好", "image_hashes": ["检视照"]},
    }


def damage_report(note):
    return {
        "condition": "损伤", "damage_note": note,
        "before_hashes": ["a" * 64], "after_hashes": ["b" * 64],
    }


class HttpIncidentRegressionTest(unittest.TestCase):
    server = None

    @classmethod
    def setUpClass(cls):
        cls.original_state = service.STATE
        cls.state_dir = tempfile.TemporaryDirectory()
        cls.state_file = os.path.join(cls.state_dir.name, "loan-state.json")
        service.STATE = service.ApiState(cls.state_file)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        service.STATE = cls.original_state
        cls.state_dir.cleanup()

    def setUp(self):
        self.tag = self.id().split(".")[-1].replace("_", "-")
        with post(self.base_url, "/works", {
            "title": f"HTTP 长卷 {self.tag}",
            "kind": "长卷", "owner_org": "甲馆",
        }) as response:
            self.work_id = json.load(response)["work"]["work_id"]
        post(self.base_url, "/agreements", {
            "work_id": self.work_id,
            "lender_org": "甲馆", "borrower_org": "乙馆",
            "start_on": "2026-10-01", "end_on": "2026-12-31",
            "gallery": "三号厅", "max_lux": 50,
            "transport": {"mode": "专车恒温"},
            "insurance": {"coverage": "钉到钉"},
            "digital_rights": {"web": True},
        }).close()

    def scan(self, code):
        return f"{self.tag}-{code}"

    def _damage_resolve_damage_again(self):
        post(self.base_url, "/handovers",
             handover_body(self.work_id, "出库", self.scan("S1"), "2026-09-25")).close()
        post(self.base_url, "/handovers",
             handover_body(self.work_id, "到馆", self.scan("S2"), "2026-09-27",
                           damage_report("第一次运输：天杆磕痕"))).close()
        with request_json(self.base_url, "GET", f"/works/{self.work_id}/risk") as response:
            incident_1 = json.load(response)["open_risks"][0]["incident_id"]
        with post(self.base_url, f"/incidents/{incident_1}/resolve",
                  resolution(f"RES-{self.tag}-FIRST")) as response:
            first_result = json.load(response)
        self.assertFalse(first_result["replayed"])
        post(self.base_url, "/handovers",
             handover_body(self.work_id, "布展", self.scan("S3"), "2026-09-30")).close()
        post(self.base_url, "/handovers",
             handover_body(self.work_id, "撤展", self.scan("S4"), "2027-01-05",
                           damage_report("第二次运输：尾纸新折痕"))).close()
        with request_json(self.base_url, "GET", f"/works/{self.work_id}/risk") as response:
            risk = json.load(response)
        incident_2 = risk["open_risks"][0]["incident_id"]
        return incident_1, incident_2

    def test_replay_first_resolution_keeps_freeze_until_second_closed(self):
        first_res = f"RES-{self.tag}-FIRST"
        second_res = f"RES-{self.tag}-SECOND"
        incident_1, incident_2 = self._damage_resolve_damage_again()

        # 完全重放第一次解除：200 + replayed，作品仍因第二次事故冻结。
        with post(self.base_url, f"/incidents/{incident_1}/resolve",
                  resolution(first_res)) as response:
            replay = json.load(response)
        self.assertEqual(response.status, 200)
        self.assertTrue(replay["replayed"])
        self.assertTrue(replay["work_frozen"])
        self.assertEqual(replay["open_incident_ids"], [incident_2])

        with self.assertRaises(HTTPError) as error:
            post(self.base_url, "/handovers",
                 handover_body(self.work_id, "归还", self.scan("S5"), "2027-01-07"))
        self.assertEqual(error.exception.code, 409)
        error.exception.close()

        # 风险视图一致展示全部历史事故与当前冻结原因。
        with request_json(self.base_url, "GET", f"/works/{self.work_id}/risk") as response:
            risk = json.load(response)
        self.assertTrue(risk["frozen"])
        self.assertEqual(len(risk["incidents"]), 2)
        self.assertEqual(len(risk["open_risks"]), 1)
        self.assertEqual(risk["frozen_reasons"][0]["incident_id"], incident_2)
        self.assertIn("尾纸新折痕", risk["frozen_reasons"][0]["note"])

        # 解除第二次事故后归还才放行。
        with post(self.base_url, f"/incidents/{incident_2}/resolve",
                  resolution(second_res, conclusion="复损复核通过，准予归还")) as response:
            closed = json.load(response)
        self.assertFalse(closed["work_frozen"])
        with post(self.base_url, "/handovers",
                  handover_body(self.work_id, "归还", self.scan("S5"), "2027-01-07")) as response:
            self.assertEqual(json.load(response)["resulting_status"], "已归还")

    def test_changed_content_same_id_is_409_and_missing_fields_are_400(self):
        post(self.base_url, "/handovers",
             handover_body(self.work_id, "出库", self.scan("S1"), "2026-09-25")).close()
        post(self.base_url, "/handovers",
             handover_body(self.work_id, "到馆", self.scan("S2"), "2026-09-27",
                           damage_report("边缘磨损"))).close()
        with request_json(self.base_url, "GET", f"/works/{self.work_id}/risk") as response:
            incident_id = json.load(response)["open_risks"][0]["incident_id"]

        res_id = f"RES-{self.tag}-409"
        post(self.base_url, f"/incidents/{incident_id}/resolve",
             resolution(res_id)).close()
        with self.assertRaises(HTTPError) as error:
            post(self.base_url, f"/incidents/{incident_id}/resolve",
                 resolution(res_id, conclusion="迟到请求改写的结论"))
        code, body = read_error(error.exception)
        self.assertEqual(code, 409)
        self.assertIn("不一致", body["error"])

        # 缺解除编号/处理人/证据摘要都是 400，事故仍只被解除一次。
        for bad in (
            {"conclusion": "无编号无处理人"},
            resolution("RES-PLACEHOLDER", handler=""),
            resolution("RES-PLACEHOLDER", evidence_summary=""),
        ):
            with self.assertRaises(HTTPError) as error:
                post(self.base_url, f"/incidents/{incident_id}/resolve", bad)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()

    def test_concurrent_resolution_has_single_first_acceptance(self):
        post(self.base_url, "/handovers",
             handover_body(self.work_id, "出库", self.scan("S1"), "2026-09-25")).close()
        post(self.base_url, "/handovers",
             handover_body(self.work_id, "到馆", self.scan("S2"), "2026-09-27",
                           damage_report("并发解除"))).close()
        with request_json(self.base_url, "GET", f"/works/{self.work_id}/risk") as response:
            incident_id = json.load(response)["open_risks"][0]["incident_id"]

        results = []
        errors = []
        barrier = threading.Barrier(8)

        def submit():
            barrier.wait()
            try:
                with post(self.base_url, f"/incidents/{incident_id}/resolve",
                          resolution(f"RES-{self.tag}-CONC")) as response:
                    results.append((response.status, json.load(response)))
            except HTTPError as error:
                errors.append(error.code)
                error.read()

        threads = [threading.Thread(target=submit) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertTrue(all(code == 200 for code, _ in results))
        self.assertEqual(sum(1 for _, body in results if not body["replayed"]), 1)
        self.assertEqual(sum(1 for _, body in results if body["replayed"]), 7)

    def test_rejected_scan_while_frozen_works_after_unfreeze(self):
        post(self.base_url, "/handovers",
             handover_body(self.work_id, "出库", self.scan("S1"), "2026-09-25")).close()
        post(self.base_url, "/handovers",
             handover_body(self.work_id, "到馆", self.scan("S2"), "2026-09-27",
                           damage_report("到馆折痕"))).close()
        with self.assertRaises(HTTPError) as error:
            post(self.base_url, "/handovers",
                 handover_body(self.work_id, "布展", self.scan("S3"), "2026-09-30"))
        self.assertEqual(error.exception.code, 409)
        error.exception.close()
        with request_json(self.base_url, "GET", f"/works/{self.work_id}/risk") as response:
            incident_id = json.load(response)["open_risks"][0]["incident_id"]
        post(self.base_url, f"/incidents/{incident_id}/resolve",
             resolution(f"RES-{self.tag}-SCAN")).close()
        with post(self.base_url, "/handovers",
                  handover_body(self.work_id, "布展", self.scan("S3"), "2026-09-30")) as response:
            self.assertEqual(json.load(response)["scan_code"], self.scan("S3"))

    def test_restart_with_state_file_resumes_pending_return(self):
        first_res = f"RES-{self.tag}-FIRST"
        second_res = f"RES-{self.tag}-SECOND"
        incident_1, incident_2 = self._damage_resolve_damage_again()

        # 模拟进程重启：丢弃内存状态，从同一状态文件恢复。
        service.STATE = service.ApiState(self.state_file)
        with request_json(self.base_url, "GET", f"/works/{self.work_id}/risk") as response:
            risk = json.load(response)
        self.assertTrue(risk["frozen"])
        self.assertEqual({i["incident_id"] for i in risk["incidents"]},
                         {incident_1, incident_2})

        # 旧解除重放语义在重启后保持。
        with post(self.base_url, f"/incidents/{incident_1}/resolve",
                  resolution(first_res)) as response:
            self.assertTrue(json.load(response)["replayed"])

        with post(self.base_url, f"/incidents/{incident_2}/resolve",
                  resolution(second_res, conclusion="复损复核通过，准予归还")) as response:
            self.assertFalse(json.load(response)["work_frozen"])
        with post(self.base_url, "/handovers",
                  handover_body(self.work_id, "归还", self.scan("S5"), "2027-01-07")) as response:
            done = json.load(response)
        self.assertEqual(done["resulting_status"], "已归还")

        # 落盘快照包含本作品的全部事故；再次重启仍是已归还、未冻结。
        with open(self.state_file, "r", encoding="utf-8") as handle:
            persisted = json.load(handle)
        self.assertEqual(
            [i["incident_id"] for i in persisted["incidents"] if i["work_id"] == self.work_id],
            [incident_1, incident_2],
        )
        service.STATE = service.ApiState(self.state_file)
        with request_json(self.base_url, "GET", f"/works/{self.work_id}/risk") as response:
            final_risk = json.load(response)
        self.assertFalse(final_risk["frozen"])
        with request_json(self.base_url, "GET", f"/works/{self.work_id}") as response:
            self.assertEqual(json.load(response)["custody"]["status"], "已归还")


if __name__ == "__main__":
    unittest.main()
