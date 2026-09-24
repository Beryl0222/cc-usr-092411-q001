"""HTTP 层契约：验证路由分发与 400/409 状态码映射。"""

import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import service
from service import ApiState, Handler


def post_json(base_url, path, payload):
    request = Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urlopen(request, timeout=2)


def get_json(base_url, path):
    with urlopen(f"{base_url}{path}", timeout=2) as response:
        return response.status, json.load(response)


def read_error(error):
    return error.code, json.loads(error.read().decode("utf-8"))


class ApiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 每个测试类使用独立端口；进程内 STATE 由各用例使用不同作品号隔离。
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_register_and_fetch_work(self):
        with post_json(self.base_url, "/works", {
            "title": "HTTP 烟霭图", "kind": "独立作品", "owner_org": "戊馆",
        }) as response:
            body = json.load(response)
        self.assertEqual(response.status, 200)
        work_id = body["work"]["work_id"]
        with urlopen(f"{self.base_url}/works/{work_id}", timeout=2) as response:
            self.assertEqual(json.load(response)["work"]["owner_org"], "戊馆")

    def test_invalid_kind_is_400(self):
        with self.assertRaises(HTTPError) as error:
            post_json(self.base_url, "/works", {"title": "x", "kind": "瓷器", "owner_org": "戊馆"})
        code, body = read_error(error.exception)
        self.assertEqual(code, 400)
        self.assertIn("作品类型", body["error"])

    def test_malformed_json_is_400(self):
        request = Request(
            f"{self.base_url}/works", data=b"{not-json",
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=2)
        self.assertEqual(error.exception.code, 400)

    def test_duplicate_scan_is_409_and_chain_continues(self):
        with post_json(self.base_url, "/works", {
            "title": "HTTP 溪山图", "kind": "独立作品", "owner_org": "甲馆",
        }) as response:
            work_id = json.load(response)["work"]["work_id"]

        def handover(htype, scan, fr, tr, forg, torg):
            return post_json(self.base_url, "/handovers", {
                "work_id": work_id, "type": htype, "scan_code": scan,
                "on_date": "2026-09-22",
                "from_party": {"org": forg, "role": fr, "person": "甲"},
                "to_party": {"org": torg, "role": tr, "person": "乙"},
                "report": {"condition": "良好", "image_hashes": ["h"]},
            })

        with handover("出库", "NET-1", "出借馆", "运输方", "甲馆", "运输") as response:
            self.assertEqual(response.status, 200)
        with self.assertRaises(HTTPError) as error:
            handover("到馆", "NET-1", "运输方", "承借馆", "运输", "乙馆")
        code, body = read_error(error.exception)
        self.assertEqual(code, 409)
        self.assertIn("重复扫码", body["error"])
        # 新扫码办理到馆成功，证明被拒绝的重复扫码没有破坏生命周期。
        with handover("到馆", "NET-2", "运输方", "承借馆", "运输", "乙馆") as response:
            self.assertEqual(json.load(response)["resulting_status"], "待布展")

    def test_damage_freezes_next_handover_and_risk_reports_it(self):
        with post_json(self.base_url, "/works", {
            "title": "HTTP 秋林图", "kind": "独立作品", "owner_org": "甲馆",
        }) as response:
            work_id = json.load(response)["work"]["work_id"]

        def handover(htype, scan, report):
            return post_json(self.base_url, "/handovers", {
                "work_id": work_id, "type": htype, "scan_code": scan,
                "on_date": "2026-09-22",
                "from_party": {"org": "甲馆", "role": "出借馆" if htype == "出库" else "运输方", "person": "甲"},
                "to_party": {"org": "运输", "role": "运输方" if htype == "出库" else "承借馆", "person": "乙"},
                "report": report,
            })

        handover("出库", "D-1", {"condition": "良好", "image_hashes": ["ok"]}).close()
        with handover("到馆", "D-2", {
            "condition": "损伤", "damage_note": "新增折痕",
            "before_hashes": ["a" * 64], "after_hashes": ["b" * 64],
        }) as response:
            self.assertTrue(json.load(response)["frozen"])
        with self.assertRaises(HTTPError) as error:
            handover("布展", "D-3", {"condition": "良好", "image_hashes": ["ok"]})
        self.assertEqual(error.exception.code, 409)
        with urlopen(f"{self.base_url}/works/{work_id}/risk", timeout=2) as response:
            risk = json.load(response)
        self.assertTrue(risk["frozen"])
        self.assertEqual(len(risk["open_risks"]), 1)
        self.assertEqual(risk["open_risks"][0]["before_hashes"], ["a" * 64])


def _start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class IncidentResolutionApiTest(unittest.TestCase):
    """通过 HTTP 验证事故解除与冻结关系（含真实重启）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self._tmp.name, "loan-state.json")
        # 让 Handler 使用带持久化的独立记录，避免与其他用例共享进程内 STATE。
        self._orig_state = service.STATE
        service.STATE = ApiState(state_file=self.state_file)
        self.server, self.thread = _start_server()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        service.STATE = self._orig_state
        self._tmp.cleanup()

    def _register_scroll(self):
        with post_json(self.base_url, "/works", {
            "title": "HTTP 复损长卷", "kind": "长卷", "owner_org": "甲馆",
        }) as response:
            return json.load(response)["work"]["work_id"]

    def _agreement(self, work_id):
        post_json(self.base_url, "/agreements", {
            "work_id": work_id, "lender_org": "甲馆", "borrower_org": "乙馆",
            "start_on": "2026-10-01", "end_on": "2026-12-31", "gallery": "三号厅",
            "max_lux": 50, "transport": {"mode": "专车恒温"},
            "insurance": {"coverage": "钉到钉"},
            "digital_rights": {"web": True},
        }).close()

    def _handover(self, work_id, htype, scan, day, report=None, location="甲馆库房"):
        pairs = {
            "出库": ("出借馆", "运输方", "甲馆", "长风运输"),
            "到馆": ("运输方", "承借馆", "长风运输", "乙馆"),
            "布展": ("承借馆", "承借馆", "乙馆", "乙馆"),
            "撤展": ("承借馆", "运输方", "乙馆", "长风运输"),
            "归还": ("运输方", "出借馆", "长风运输", "甲馆"),
        }
        fr, tr, forg, torg = pairs[htype]
        return post_json(self.base_url, "/handovers", {
            "work_id": work_id, "type": htype, "scan_code": scan,
            "on_date": day, "at_location": location,
            "from_party": {"org": forg, "role": fr, "person": "甲"},
            "to_party": {"org": torg, "role": tr, "person": "乙"},
            "report": report or {"condition": "良好", "image_hashes": ["ok"]},
        })

    def _resolve(self, incident_id, resolution_id, note="复核通过", **extra):
        payload = {
            "resolution_id": resolution_id,
            "resolver": "复核员钱某",
            "resolution_note": note,
            "evidence_summary": "前后图像哈希与检视记录一致",
        }
        payload.update(extra)
        return post_json(
            self.base_url, f"/incidents/{incident_id}/resolve", payload
        )

    def test_redamage_replay_conflict_reverse_order_and_duplicate_scan(self):
        work_id = self._register_scroll()
        self._agreement(work_id)
        self._handover(work_id, "出库", "S-1", "2026-09-25").close()

        with self._handover(work_id, "到馆", "S-2", "2026-09-27", {
            "condition": "损伤", "damage_note": "第一次运输折痕",
            "before_hashes": ["a" * 64], "after_hashes": ["b" * 64],
        }) as response:
            first_incident = json.load(response)["incident_id"]

        first_request = {
            "resolution_id": "R-1", "resolver": "复核员钱某",
            "resolution_note": "修复后确认可继续", "evidence_summary": "哈希保全",
        }
        with post_json(self.base_url, f"/incidents/{first_incident}/resolve",
                       first_request) as response:
            self.assertEqual(response.status, 200)
            body = json.load(response)
        self.assertFalse(body["replayed"])
        self.assertFalse(body["work_frozen"])

        # 交接推进到第二次运输，撤展时复损。
        self._handover(work_id, "布展", "S-3", "2026-09-30",
                       location="乙馆三号厅").close()
        with self._handover(work_id, "撤展", "S-4", "2027-01-05", {
            "condition": "损伤", "damage_note": "第二次运输尾纸撕裂",
            "before_hashes": ["c" * 64], "after_hashes": ["d" * 64],
        }) as response:
            second_incident = json.load(response)["incident_id"]

        # 风险查询一致展示全部历史事故与当前冻结原因。
        _, risk = get_json(self.base_url, f"/works/{work_id}/risk")
        self.assertTrue(risk["frozen"])
        self.assertEqual(len(risk["incidents"]), 2)
        self.assertEqual([r["incident_id"] for r in risk["open_risks"]],
                         [second_incident])
        self.assertTrue(any(second_incident in r for r in risk["frozen_reason"]))

        # 重放第一次解除：返回原结果（replayed），但不解冻。
        with post_json(self.base_url, f"/incidents/{first_incident}/resolve",
                       dict(first_request)) as response:
            replay = json.load(response)
        self.assertTrue(replay["replayed"])
        self.assertTrue(replay["work_frozen"])
        self.assertEqual(replay["open_incident_ids"], [second_incident])

        # 作品仍冻结：归还被 409 拒绝，不能绕过复核。
        with self.assertRaises(HTTPError) as error:
            self._handover(work_id, "归还", "S-5", "2027-01-07")
        self.assertEqual(error.exception.code, 409)
        error.exception.close()

        # 同号内容变化 → 409 冲突。
        changed = dict(first_request, resolution_note="被篡改的结论")
        with self.assertRaises(HTTPError) as error:
            post_json(self.base_url, f"/incidents/{first_incident}/resolve", changed)
        self.assertEqual(error.exception.code, 409)
        error.exception.close()

        # 迟到的另一编号请求解除第二次事故的“旧单”也不能冒充：
        # 用 R-1（已属于第一起）去解除第二起 → 409。
        with self.assertRaises(HTTPError) as error:
            self._resolve(second_incident, "R-1", "迟到旧请求")
        self.assertEqual(error.exception.code, 409)
        error.exception.close()

        # 解除第二起后全部事故关闭，交接恢复。
        with self._resolve(second_incident, "R-2", "尾纸修复，双方确认") as response:
            body = json.load(response)
        self.assertFalse(body["work_frozen"])

        # 重复扫码：旧码 S-2 不能在归还上复用，被 409 拒绝且不消费。
        with self.assertRaises(HTTPError) as error:
            self._handover(work_id, "归还", "S-2", "2027-01-07")
        self.assertEqual(error.exception.code, 409)
        error.exception.close()
        with self._handover(work_id, "归还", "S-6", "2027-01-07") as response:
            self.assertEqual(json.load(response)["resulting_status"], "已归还")

    def test_two_incidents_reverse_resolution_via_multi_damage(self):
        work_id = self._register_scroll()
        self._agreement(work_id)
        self._handover(work_id, "出库", "M-1", "2026-09-25").close()
        with self._handover(work_id, "到馆", "M-2", "2026-09-27", {
            "condition": "损伤",
            "damage_items": [
                {"note": "画心折痕", "before_hashes": ["a" * 64], "after_hashes": ["b" * 64]},
                {"note": "尾纸水渍", "before_hashes": ["c" * 64], "after_hashes": ["d" * 64]},
            ],
        }) as response:
            body = json.load(response)
        first_id, second_id = body["incident_ids"]

        # 逆序解除第二起，作品仍冻结，布展 409。
        with self._resolve(second_id, "RR-2") as response:
            self.assertTrue(json.load(response)["work_frozen"])
        with self.assertRaises(HTTPError) as error:
            self._handover(work_id, "布展", "M-3", "2026-09-30")
        self.assertEqual(error.exception.code, 409)
        error.exception.close()

        # 缺处理人/证据摘要的解除 → 400。
        with self.assertRaises(HTTPError) as error:
            post_json(self.base_url, f"/incidents/{first_id}/resolve",
                      {"resolution_id": "RR-1", "resolution_note": "结论"})
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

        with self._resolve(first_id, "RR-1") as response:
            self.assertFalse(json.load(response)["work_frozen"])
        with self._handover(work_id, "布展", "M-3", "2026-09-30") as response:
            self.assertEqual(json.load(response)["resulting_status"], "展出中")

    def test_chain_resumes_after_real_process_restart(self):
        work_id = self._register_scroll()
        self._agreement(work_id)
        self._handover(work_id, "出库", "P-1", "2026-09-25").close()
        with self._handover(work_id, "到馆", "P-2", "2026-09-27", {
            "condition": "损伤", "damage_note": "折痕",
            "before_hashes": ["a" * 64], "after_hashes": ["b" * 64],
        }) as response:
            incident_id = json.load(response)["incident_id"]
        self._resolve(incident_id, "P-R", "复核通过").close()
        self._handover(work_id, "布展", "P-3", "2026-09-30").close()

        # 真实“重启”：关闭服务，用同一状态文件构建全新 ApiState 再启动。
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        service.STATE = ApiState(state_file=self.state_file)
        self.server, self.thread = _start_server()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

        _, work_view = get_json(self.base_url, f"/works/{work_id}")
        self.assertEqual(work_view["custody"]["status"], "展出中")
        self.assertFalse(work_view["frozen"])

        # 解除幂等键跨重启有效：重放返回原结果。
        with self._resolve(incident_id, "P-R", "复核通过") as response:
            self.assertTrue(json.load(response)["replayed"])
        # 扫码记录持久保留。
        with self.assertRaises(HTTPError) as error:
            self._handover(work_id, "撤展", "P-1", "2027-01-05")
        self.assertEqual(error.exception.code, 409)
        error.exception.close()
        # 交接链继续直至归还。
        self._handover(work_id, "撤展", "P-4", "2027-01-05").close()
        with self._handover(work_id, "归还", "P-5", "2027-01-07") as response:
            self.assertEqual(json.load(response)["resulting_status"], "已归还")


if __name__ == "__main__":
    unittest.main()
