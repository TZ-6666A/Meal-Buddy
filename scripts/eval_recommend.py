"""推荐质量评测。

用法：
  python scripts/eval_recommend.py --offline        # 只跑本地决策引擎，不调模型、不花钱
  python scripts/eval_recommend.py                  # 调真实接口，会消耗 token（需确认）
  python scripts/eval_recommend.py --base-url http://127.0.0.1:8000/api/v1
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import decision  # noqa: E402

CASES = ROOT / "eval" / "cases.json"


def load_cases() -> list[dict]:
    return json.loads(CASES.read_text(encoding="utf-8"))


def check_names(names: list[str], expect: dict) -> list[str]:
    errors: list[str] = []
    if len(names) < expect.get("min_recipes", 1):
        errors.append(f"候选为空（期望至少 {expect.get('min_recipes', 1)} 道）")
    for bad in expect.get("must_not_contain", []):
        hits = [n for n in names if bad in n]
        if hits:
            errors.append(f"命中忌口「{bad}」：{'、'.join(hits[:3])}")
    return errors


def run_offline(cases: list[dict]) -> list[tuple[str, bool, str, float]]:
    results = []
    for case in cases:
        started = time.perf_counter()
        mode = case.get("mode", "cook")
        answers = {"time": str(case["available_minutes"]), "style": ""} if mode == "cook" else {}
        ranking = decision.ranked(
            mode, answers, case.get("condition", ""), case.get("allergies", []), []
        )
        names = [name for name, _ in ranking]
        errors = check_names(names, case["expect"])
        results.append((case["id"], not errors, "；".join(errors), time.perf_counter() - started))
    return results


def run_online(cases: list[dict], base_url: str) -> list[tuple[str, bool, str, float]]:
    results = []
    for case in cases:
        started = time.perf_counter()
        allergy_note = "在线模式不验证忌口，仅离线模式生效" if case.get("allergies") else ""
        payload = {
            "ingredients": case.get("ingredients", []),
            "available_minutes": case.get("available_minutes", 30),
            "dietary_preferences": "",
            "condition": case.get("condition", ""),
        }
        request = urllib.request.Request(
            f"{base_url}/recommendations",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "ignore")
            reason = f"HTTP {exc.code}: {body[:120]}"
            if allergy_note:
                reason += f"；{allergy_note}"
            results.append((case["id"], False, reason, time.perf_counter() - started))
            continue
        except Exception as exc:  # noqa: BLE001
            reason = f"请求失败：{exc}"
            if allergy_note:
                reason += f"；{allergy_note}"
            results.append((case["id"], False, reason, time.perf_counter() - started))
            continue

        recipes = data.get("recipes", [])
        errors = []
        if len(recipes) < case["expect"].get("min_recipes", 1):
            errors.append(f"菜谱数量不足（{len(recipes)}）")
        for recipe in recipes:
            if recipe.get("prep_minutes", 0) > case["expect"].get("max_prep_minutes", 999):
                errors.append(f"{recipe.get('name')} 超时 {recipe.get('prep_minutes')} 分钟")
            texts = [recipe.get("name", "")] + [i.get("name", "") for i in recipe.get("ingredients", [])]
            if not allergy_note:
                for bad in case["expect"].get("must_not_contain", []):
                    if any(bad in text for text in texts):
                        errors.append(f"{recipe.get('name')} 命中忌口「{bad}」")
        if allergy_note:
            errors.append(allergy_note)
        results.append((case["id"], not any(e != allergy_note for e in errors), "；".join(errors), time.perf_counter() - started))
    return results


def print_table(results: list[tuple[str, bool, str, float]]) -> None:
    width = max(len(r[0]) for r in results) + 2
    print(f"{'用例'.ljust(width)}结果   耗时      说明")
    print("-" * (width + 34))
    for case_id, ok, reason, elapsed in results:
        status = "通过" if ok else "失败"
        print(f"{case_id.ljust(width)}{status}  {elapsed:6.2f}s  {reason}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="只跑本地决策引擎，不调模型")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--yes", action="store_true", help="在线模式跳过确认")
    args = parser.parse_args()

    cases = load_cases()

    if args.offline:
        print(f"离线评测：{len(cases)} 条用例，不调用模型\n")
        results = run_offline(cases)
    else:
        print(f"在线评测：{len(cases)} 条用例，将调用模型 {len(cases)} 次。")
        print("这会消耗 token，是否继续？输入 y 确认：", end="")
        if not args.yes and input().strip().lower() != "y":
            print("已取消。")
            return 0
        results = run_online(cases, args.base_url.rstrip("/"))

    print_table(results)
    failed = [r for r in results if not r[1]]
    print(f"\n合计 {len(results)} 条，通过 {len(results) - len(failed)} 条，失败 {len(failed)} 条")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
