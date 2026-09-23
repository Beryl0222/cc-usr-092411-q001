"""HTTP 层契约：验证路由分发与 400/409 状态码映射。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import Handler


def post_json(base_url, path, payload):
    request = Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urlopen(request, timeout=2)


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


if __name__ == "__main__":
    unittest.main()
