"""离线测试：用 FakeTransport 验证报文、计费、幂等与网关，不触网、不消耗积分。

运行： python -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jimeng.catalog import Catalog  # noqa: E402
from jimeng.client import Account, JimengClient  # noqa: E402
from jimeng.credits import parse_credit  # noqa: E402
from jimeng.errors import (  # noqa: E402
    BudgetExceeded,
    CapabilityUnsupported,
    CreditInsufficient,
    ParamInvalid,
)
from jimeng.gateway import Gateway  # noqa: E402
from jimeng.parsing import extract_images  # noqa: E402
from jimeng.probe import ProbeRunner  # noqa: E402
from jimeng.request import GenerateRequest, ReferenceImage  # noqa: E402
from jimeng.transport import Response  # noqa: E402
from jimeng.accounts import AccountPool  # noqa: E402


class FakeTransport:
    """模拟上游：只实现测试需要的五类端点，并记录调用。"""

    def __init__(self, balance: float = 40.0, images_per_submit: int = 4, width: int = 2048, height: int = 2048):
        self.balance = balance
        self.images_per_submit = images_per_submit
        self.width = width
        self.height = height
        self.history_status = 50
        self.calls = []
        self.bodies = []
        self.counter = 0

    def request(self, method, url, *, headers=None, json_body=None, data=None, timeout=None):
        self.calls.append((method, url))
        if json_body is not None:
            self.bodies.append((url, json_body))

        if "benefits/user_credit" in url:
            return self._json({"ret": "0", "data": {"credits": {"giftCredit": self.balance, "vipCredit": 0, "purchaseCredit": 0, "totalCredit": self.balance}}})
        if "get_upload_image_proof" in url:
            return self._json({"ret": "0", "data": {"proof_info": {"headers": {"X-Test": "1"}, "query_params": {"Action": "ApplyImageUpload"}, "image_uri": "tos-cn-i-test/abc"}}})
        if "imagex.bytedanceapi.com" in url:
            return Response(200, {}, b"{}")
        if "submit_audit_job" in url:
            return self._json({"ret": "0"})
        if "aigc_draft/generate" in url:
            self.counter += 1
            self.balance = max(0.0, self.balance - self.images_per_submit)
            return self._json({"ret": "0", "data": {"aigc_data": {"history_record_id": str(1000 + self.counter)}}})
        if "get_history_by_ids" in url:
            ids = (json_body or {}).get("history_ids") or ["1001"]
            item_list = [
                {
                    "image": {
                        "large_images": [
                            {
                                "url": f"https://cdn.example.com/img_{ids[0]}_{i}.png",
                                "width": self.width,
                                "height": self.height,
                            }
                            for i in range(self.images_per_submit)
                        ]
                    }
                }
            ]
            if self.history_status != 50:
                item_list = []
            return self._json(
                {
                    "ret": "0",
                    "data": {
                        "history_list": [
                            {
                                "history_id": ids[0],
                                "status": self.history_status,
                                "item_list": item_list,
                            }
                        ]
                    },
                }
            )
        if method == "GET" and url.startswith("http"):
            return Response(200, {}, b"\x89PNG\r\n")
        return self._json({"ret": "0"})

    @staticmethod
    def _json(payload):
        return Response(200, {"Content-Type": "application/json"}, json.dumps(payload).encode())


def make_client(**kwargs):
    fake = FakeTransport(**kwargs)
    account = Account(id="test", sessionid="sid_test", cooldown_s=0.0)
    return JimengClient(account, transport=fake, catalog=Catalog.load()), fake


class TestParamResolution(unittest.TestCase):
    def test_ratio_code_and_dimensions(self):
        catalog = Catalog.load()
        req = GenerateRequest(prompt="cat", model="jimeng-4.5", ratio="16:9", resolution="2k")
        plan = req.normalize(catalog)
        self.assertEqual(plan.ratio_code, 1)
        self.assertEqual((plan.width, plan.height), (2560, 1440))
        self.assertEqual(plan.resolution, "2k")

    def test_one_to_one_ratio_code_is_eight(self):
        catalog = Catalog.load()
        plan = GenerateRequest(prompt="x", ratio="1:1").normalize(catalog)
        self.assertEqual(plan.ratio_code, 8)

    def test_auto_ratio_omits_ratio_code(self):
        plan = GenerateRequest(prompt="x", model="jimeng-4.5", ratio="auto").normalize(Catalog.load())
        self.assertIsNone(plan.ratio_code)
        self.assertTrue(plan.intelligent_ratio)

    def test_min_billing_granularity(self):
        catalog = Catalog.load()
        one = GenerateRequest(prompt="x", model="jimeng-4.6", n=1).normalize(catalog)
        self.assertEqual((one.submissions, one.planned_images, one.estimated_credits), (1, 4, 4.0))
        two = GenerateRequest(prompt="x", model="jimeng-4.6", n=2).normalize(catalog)
        self.assertEqual((two.submissions, two.planned_images), (1, 4))
        four = GenerateRequest(prompt="x", model="jimeng-3.0", n=4).normalize(catalog)
        self.assertEqual((four.submissions, four.planned_images, four.estimated_credits), (4, 4, 4.0))
        two = GenerateRequest(prompt="x", model="jimeng-3.0", n=2).normalize(catalog)
        self.assertEqual((two.submissions, two.planned_images), (2, 2))

    def test_unsupported_combination(self):
        catalog = Catalog.load()
        with self.assertRaises(CapabilityUnsupported):
            GenerateRequest(prompt="x", model="jimeng-3.0", resolution="4k").normalize(catalog)
        with self.assertRaises(CapabilityUnsupported):
            GenerateRequest(prompt="x", model="jimeng-3.0", ratio="auto").normalize(catalog)
        with self.assertRaises(ParamInvalid):
            GenerateRequest(prompt="", model="jimeng-4.5").normalize(catalog)
        with self.assertRaises(ParamInvalid):
            GenerateRequest(prompt="x", n=9).normalize(catalog)

    def test_payload_structure(self):
        from jimeng.payload import build_generate_body

        plan = GenerateRequest(prompt="cat", ratio="9:16", resolution="1k", n=2).normalize(Catalog.load())
        body = build_generate_body(plan)
        self.assertIsInstance(body["draft_content"], str)
        draft = json.loads(body["draft_content"])
        abilities = draft["component_list"][0]["abilities"]["generate"]
        self.assertEqual(abilities["core_param"]["image_ratio"], 6)
        self.assertEqual(abilities["core_param"]["large_image_info"]["height"], 1024)
        self.assertEqual(abilities["gen_option"]["generate_number"], 2)
        self.assertEqual(body["extend"]["root_model"], "high_aes_general_v40l")
        self.assertIn("aid", body["http_common_info"])


class TestGeneration(unittest.TestCase):
    def test_dry_run_consumes_nothing(self):
        client, fake = make_client()
        result = client.generate(GenerateRequest(prompt="cat", dry_run=True))
        self.assertEqual(result["status"], "preview")
        self.assertEqual(result["usage"]["credits_charged"], 0.0)
        self.assertEqual(fake.calls, [])
        self.assertEqual(result["upstream"]["payload"]["extend"]["root_model"], "high_aes_general_v40l")

    def test_end_to_end_generation(self):
        client, fake = make_client(balance=40.0)
        result = client.generate(GenerateRequest(prompt="cat", model="jimeng-4.5", n=1, timeout_s=10, poll_interval_s=0.01))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(result["data"]), 4)
        self.assertEqual(result["usage"]["credits_charged"], 4.0)
        self.assertEqual(client.account.ledger.usage()["balance"]["total"], 36.0)
        self.assertEqual(result["data"][0]["url"], "https://cdn.example.com/img_1001_0.png")
        self.assertEqual(len(result["upstream"]["history_ids"]), 1)

    def test_wait_false_returns_pending(self):
        client, _ = make_client()
        result = client.generate(GenerateRequest(prompt="cat", wait=False))
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["pending"], ["1001"])
        self.assertEqual(result["usage"]["images_returned"], 0)

    def test_timeout_settles_conservatively(self):
        client, fake = make_client(balance=40.0)
        fake.history_status = 20
        result = client.generate(GenerateRequest(prompt="cat", timeout_s=0.01, poll_interval_s=0.01))
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["usage"]["credits_charged"], 4.0)
        self.assertEqual(result["pending"], ["1001"])
        self.assertIsNotNone(result["error"])

    def test_idempotent_cache_avoids_double_spend(self):
        client, fake = make_client()
        req = GenerateRequest(prompt="cat", idempotency_key="order-1", timeout_s=10, poll_interval_s=0.01)
        first = client.generate(req)
        submits_after_first = sum(1 for _, url in fake.calls if "aigc_draft" in url)
        second = client.generate(GenerateRequest(prompt="cat", idempotency_key="order-1", timeout_s=10, poll_interval_s=0.01))
        submits_after_second = sum(1 for _, url in fake.calls if "aigc_draft" in url)
        self.assertEqual(submits_after_first, 1)
        self.assertEqual(submits_after_second, 1)
        self.assertTrue(second["cached"])
        self.assertEqual(second["usage"]["credits_charged"], 0.0)
        self.assertEqual(first["data"], second["data"])

    def test_budget_guard_blocks_before_submit(self):
        client, fake = make_client()
        with self.assertRaises(BudgetExceeded):
            client.generate(GenerateRequest(prompt="cat", max_credits=1.0, timeout_s=5))
        self.assertEqual(fake.calls, [])

    def test_daily_quota_guard(self):
        client, _ = make_client(balance=40.0)
        client.account.ledger.daily_quota = 2.0
        with self.assertRaises(BudgetExceeded):
            client.generate(GenerateRequest(prompt="cat", timeout_s=5))

    def test_insufficient_credit(self):
        client, _ = make_client(balance=2.0)
        with self.assertRaises(CreditInsufficient):
            client.generate(GenerateRequest(prompt="cat", timeout_s=5))

    def test_img2img_uses_blend_and_prompt_hashes(self):
        client, fake = make_client(images_per_submit=1, balance=20.0)
        req = GenerateRequest(
            prompt="make it watercolor",
            model="jimeng-3.0",
            images=[ReferenceImage(base64="data:image/png;base64," + base64.b64encode(b"fake").decode())],
            timeout_s=10,
            poll_interval_s=0.01,
        )
        result = client.generate(req)
        body = [b for u, b in fake.bodies if "aigc_draft" in u][0]
        draft = json.loads(body["draft_content"])
        component = draft["component_list"][0]
        self.assertEqual(component["generate_type"], "blend")
        core = component["abilities"]["blend"]["core_param"]
        self.assertTrue(core["prompt"].endswith("##"))
        self.assertEqual(component["abilities"]["blend"]["ability_list"][0]["image_uri_list"], ["tos-cn-i-test/abc"])
        self.assertEqual(result["request"]["mode"], "img2img")


class TestParsing(unittest.TestCase):
    def test_credit_camel_and_snake(self):
        camel = parse_credit({"data": {"credits": {"giftCredit": 66, "purchaseCredit": 10, "vipCredit": 5, "totalCredit": 81}}})
        self.assertEqual((camel.gift, camel.purchase, camel.vip, camel.total), (66.0, 10.0, 5.0, 81.0))
        snake = parse_credit({"result": {"gift_credit": 12, "vip_credit": 3}})
        self.assertEqual((snake.gift, snake.vip, snake.total), (12.0, 3.0, 15.0))

    def test_extract_images_nested_and_deduped(self):
        record = {
            "item_list": [
                {"image": {"large_images": [{"url": "https://a/1.png", "width": 1024, "height": 1024}]}},
                {"image": {"large_images": [{"url": "https://a/1.png"}]}},
                {"common_attr": {"cover_url": "https://a/2.png"}},
            ]
        }
        images = extract_images(record)
        self.assertEqual([i["url"] for i in images], ["https://a/1.png", "https://a/2.png"])
        self.assertEqual(images[0]["width"], 1024)

    def test_extract_images_generic_fallback(self):
        record = {"data": {"anything": [{"image_url": "https://b/x.jpeg"}]}}
        self.assertEqual(extract_images(record)[0]["url"], "https://b/x.jpeg")


class TestProbeAndGateway(unittest.TestCase):
    def test_static_probe_reports_matrix(self):
        client, fake = make_client()
        report = ProbeRunner(client, max_credits=0).run(["static"], model="jimeng-4.5")
        self.assertEqual(report["findings"][0]["status"], "ok")
        detail = report["findings"][0]["detail"]
        self.assertEqual(detail["combos_ok"], detail["combos_total"])
        self.assertEqual(fake.calls, [])

    def test_live_probe_respects_budget(self):
        client, fake = make_client(images_per_submit=4, balance=10.0)
        report = ProbeRunner(client, max_credits=4.0).run(["count", "ratio"], model="jimeng-4.6", live=True, ratios=["1:1", "16:9"])
        self.assertLessEqual(report["spend"], 4.0)
        statuses = {f["probe"]: f["status"] for f in report["findings"]}
        self.assertEqual(statuses["count"], "ok")
        self.assertEqual(report["calibration"]["models"]["jimeng-4.6"]["outputs_per_submit"], 4)

    def test_gateway_endpoints(self):
        fake = FakeTransport(balance=40.0, images_per_submit=4)
        pool = AccountPool([Account(id="gw", sessionid="sid", cooldown_s=0.0)], transport=fake, catalog=Catalog.load())
        gw = Gateway(pool, token="secret")

        status, body = gw.handle("GET", "/healthz", b"", "")
        self.assertEqual(status, 200)
        self.assertEqual(body["accounts"], 1)

        status, body = gw.handle("GET", "/v1/models", b"", "")
        self.assertEqual(len(body["data"]), 7)

        payload = {"prompt": "cat", "dry_run": True}
        status, body = gw.handle("POST", "/v1/images/generations", json.dumps(payload).encode(), "application/json")
        self.assertEqual(status, 200)
        self.assertEqual(body["usage"]["credits_estimated"], 4.0)

        status, body = gw.handle("POST", "/v1/assets", json.dumps({"base64": "Zg==", "filename": "a.png"}).encode(), "application/json")
        self.assertEqual(status, 201)
        self.assertEqual(body["image_uri"], "tos-cn-i-test/abc")

    def test_gateway_pending_task_resolves_on_get(self):
        fake = FakeTransport(balance=40.0)
        pool = AccountPool([Account(id="gw", sessionid="sid", cooldown_s=0.0)], transport=fake, catalog=Catalog.load())
        gw = Gateway(pool)
        payload = {"prompt": "cat", "wait": False}
        status, created = gw.handle("POST", "/v1/images/generations", json.dumps(payload).encode(), "application/json")
        self.assertEqual(status, 202)
        self.assertEqual(created["status"], "submitted")
        status, resolved = gw.handle("GET", f"/v1/images/generations/{created['id']}", b"", "")
        self.assertEqual(resolved["status"], "succeeded")
        self.assertEqual(len(resolved["data"]), 4)

    def test_gateway_idempotency_header_flow(self):
        fake = FakeTransport(balance=40.0)
        pool = AccountPool([Account(id="gw", sessionid="sid", cooldown_s=0.0)], transport=fake, catalog=Catalog.load())
        gw = Gateway(pool)
        payload = {"prompt": "cat", "idempotency_key": "k1", "timeout_s": 10, "poll_interval_s": 0.01}
        status, first = gw.handle("POST", "/v1/images/generations", json.dumps(payload).encode(), "application/json")
        self.assertEqual(status, 200)
        status, second = gw.handle("POST", "/v1/images/generations", json.dumps(payload).encode(), "application/json")
        self.assertTrue(second["cached"])
        self.assertEqual(second["usage"]["credits_charged"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
