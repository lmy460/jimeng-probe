"""最小可运行示例：先零消耗编译报文，再按需真实生成。

    python examples/quickstart.py

真实生成需要 JIMENG_SESSIONID；未配置时脚本只做 dry-run（不触网、不扣积分）。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jimeng import Account, Catalog, GenerateRequest, JimengClient  # noqa: E402


def main() -> None:
    catalog = Catalog.load()
    sessionid = os.environ.get("JIMENG_SESSIONID", "").strip()

    client = JimengClient(
        Account(id="demo", sessionid=sessionid or "dry-run-only", cooldown_s=0.0),
        catalog=catalog,
    )

    req = GenerateRequest(
        prompt="雨夜霓虹街道，赛博朋克，湿润路面反射",
        model="jimeng-4.6",
        ratio="16:9",
        resolution="2k",
        n=4,
        dry_run=True,
    )
    preview = client.generate(req)
    print("== dry-run ==")
    print(json.dumps({"plan": preview["request"], "usage": preview["usage"]}, ensure_ascii=False, indent=2))

    if not sessionid:
        print("\n未设置 JIMENG_SESSIONID，跳过真实生成。")
        return

    req.dry_run = False
    req.idempotency_key = "quickstart-demo-1"
    req.max_credits = preview["usage"]["credits_estimated"]
    result = client.generate(req)
    print("\n== result ==")
    print(json.dumps({"status": result["status"], "usage": result["usage"], "images": [d.get("url") for d in result["data"]]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
