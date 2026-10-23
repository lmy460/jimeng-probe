"""命令行入口：python -m jimeng <command>"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from .accounts import AccountPool, load_accounts
from .catalog import Catalog
from .client import JimengClient
from .errors import JimengError
from .payload import build_generate_body
from .probe import ProbeRunner, static_matrix
from .request import GenerateRequest, ReferenceImage


def _pool() -> AccountPool:
    return AccountPool(load_accounts(), catalog=Catalog.load())


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_models(args: argparse.Namespace) -> int:
    catalog = Catalog.load()
    if args.json:
        _print(catalog.dump())
        return 0
    print(f"{'模型':<14}{'req_key':<40}{'单次张数':<10}{'分辨率':<16}{'置信度'}")
    for spec in catalog.all():
        print(
            f"{spec.name:<14}{spec.req_key:<40}{spec.outputs_per_submit:<10}"
            f"{','.join(spec.resolutions):<16}{spec.confidence}"
        )
    return 0


def cmd_credit(args: argparse.Namespace) -> int:
    _print({"accounts": _pool().refresh_all()})
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    pool = _pool()
    refs = [ReferenceImage(path=path) for path in (args.image or [])]
    refs += [ReferenceImage(url=url) for url in (args.image_url or [])]
    req = GenerateRequest(
        prompt=args.prompt,
        model=args.model,
        negative_prompt=args.negative or "",
        images=tuple(refs),
        ratio=args.ratio,
        resolution=args.resolution,
        n=args.n,
        seed=args.seed,
        sample_strength=args.strength,
        intelligent_ratio=True if args.auto_ratio else None,
        dry_run=args.dry_run,
        wait=not args.no_wait,
        timeout_s=args.timeout,
        max_credits=args.max_credits,
        idempotency_key=args.idempotency_key,
    )
    client = pool.client(args.account)
    try:
        result = client.generate(req)
    except JimengError as exc:
        _print(exc.to_dict())
        return 1

    if args.dry_run:
        _print(result)
        return 0

    if args.out and result.get("data"):
        _save_images(client, result, args.out)
    _print(result)
    return 0 if result["status"] in ("succeeded", "submitted") else 1


def _save_images(client: JimengClient, result: Dict[str, Any], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    for item in result["data"]:
        url = item.get("url")
        if not url:
            continue
        resp = client.transport.request("GET", url, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status != 200:
            item["save_error"] = f"HTTP {resp.status}"
            continue
        path = os.path.join(out_dir, f"{result['id']}_{item['index']:02d}.png")
        with open(path, "wb") as fh:
            fh.write(resp.body)
        item["saved_to"] = path


def cmd_probe(args: argparse.Namespace) -> int:
    if not args.live:
        report = {
            "live": False,
            "findings": [static_matrix(Catalog.load(), args.model).to_dict()],
            "note": "静态模式只校验报文与能力目录；加 --live 才会真实提交并消耗积分",
        }
        _print(report)
        return 0

    runner = ProbeRunner(_pool().client(args.account), max_credits=args.max_credits)
    report = runner.run(
        args.probes.split(",") if args.probes else None,
        model=args.model,
        ratios=args.ratios.split(",") if args.ratios else None,
        resolutions=args.resolutions.split(",") if args.resolutions else None,
        live=True,
    )
    if args.write:
        report["calibration_path"] = runner.write_calibration()
    _print(report)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .gateway import serve

    serve(_pool(), host=args.host, port=args.port)
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """零网络自检：编译一份报文并打印关键字段。"""
    plan = GenerateRequest(
        prompt="self test", model=args.model, ratio=args.ratio, resolution=args.resolution, n=args.n
    ).normalize(Catalog.load())
    body = build_generate_body(plan)
    draft = json.loads(body["draft_content"]) if isinstance(body["draft_content"], str) else body["draft_content"]
    generate = draft["component_list"][0]["abilities"]["generate"]
    _print(
        {
            "plan": plan.to_dict(),
            "payload": {
                "root_model": body["extend"]["root_model"],
                "image_ratio": generate["core_param"].get("image_ratio"),
                "large_image_info": generate["core_param"]["large_image_info"],
                "generate_number": generate["gen_option"]["generate_number"],
                "prompt": generate["core_param"]["prompt"],
            },
            "billing": {
                "submissions": plan.submissions,
                "planned_images": plan.planned_images,
                "estimated_credits": plan.estimated_credits,
            },
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jimeng", description="即梦网页端探测与复用工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("models", help="列出模型能力目录")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("credit", help="查询账号积分余额")
    p.set_defaults(func=cmd_credit)

    p = sub.add_parser("generate", help="生成图片")
    p.add_argument("--prompt", required=True)
    p.add_argument("--model", default="jimeng-4.5")
    p.add_argument("--negative", default="")
    p.add_argument("--image", action="append", help="本地参考图路径，可重复")
    p.add_argument("--image-url", action="append", help="远程参考图 URL，可重复")
    p.add_argument("--ratio", default="1:1")
    p.add_argument("--resolution", default="2k", choices=["1k", "2k", "4k"])
    p.add_argument("--n", type=int, default=1)
    p.add_argument("--seed", type=int, default=-1)
    p.add_argument("--strength", type=float, default=0.5)
    p.add_argument("--auto-ratio", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="只编译报文与预估成本，不提交（0 消耗）")
    p.add_argument("--no-wait", action="store_true", help="只提交不等待结果")
    p.add_argument("--timeout", type=float, default=180.0)
    p.add_argument("--max-credits", type=float, default=None)
    p.add_argument("--idempotency-key", default=None)
    p.add_argument("--account", default=None)
    p.add_argument("--out", default=None, help="下载产物目录")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("probe", help="能力探测")
    p.add_argument("--live", action="store_true", help="真实提交（消耗积分）")
    p.add_argument("--probes", default=None, help="static,auth,count,ratio,resolution,cost")
    p.add_argument("--model", default="jimeng-4.5")
    p.add_argument("--ratios", default=None)
    p.add_argument("--resolutions", default=None)
    p.add_argument("--max-credits", type=float, default=8.0)
    p.add_argument("--write", action="store_true", help="把结论写入 calibration.json")
    p.add_argument("--account", default=None)
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("serve", help="启动 HTTP 网关")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("selftest", help="零网络报文自检")
    p.add_argument("--model", default="jimeng-4.5")
    p.add_argument("--ratio", default="16:9")
    p.add_argument("--resolution", default="2k")
    p.add_argument("--n", type=int, default=1)
    p.set_defaults(func=cmd_selftest)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except JimengError as exc:
        _print(exc.to_dict())
        return 1


if __name__ == "__main__":
    sys.exit(main())
