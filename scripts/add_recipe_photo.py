"""给某道菜指定一张自己的成品图（OSS/COS 等），优先于 Pixabay 匹配。

用法：
  // 新增或更新一道菜的图片
  python scripts/add_recipe_photo.py "镇安腊肉炒土豆" "https://xxx.oss-cn-shanghai.aliyuncs.com/dishes/zhenan.webp"

  // 带署名（图片来自别人时填写）
  python scripts/add_recipe_photo.py "镇安腊肉炒土豆" "https://.../zhenan.webp" \
      --credit-name "Photo by 某某" --credit-url "https://example.com/photo"

  // 查看/删除已有条目
  python scripts/add_recipe_photo.py --list
  python scripts/add_recipe_photo.py "镇安腊肉炒土豆" --delete

写入后无需重启服务：接口每个请求都会查这张表并优先跳转到你给的地址。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import RecipePhoto  # noqa: E402


def _valid_image_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _list_rows() -> int:
    with SessionLocal() as db:
        rows = db.scalars(select(RecipePhoto).order_by(RecipePhoto.recipe_name)).all()
    if not rows:
        print("目录还是空的。")
        return 0
    print(f"共 {len(rows)} 条：")
    for row in rows:
        print(f"- {row.recipe_name}  ->  {row.image_url}")
    return 0


def _delete_row(name: str) -> int:
    with SessionLocal() as db:
        row = db.scalar(select(RecipePhoto).where(RecipePhoto.recipe_name == name))
        if row is None:
            print(f"没有找到：{name}")
            return 1
        db.delete(row)
        db.commit()
    print(f"已删除：{name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="给菜名绑定一张自己的成品图")
    parser.add_argument("name", nargs="?", help="菜名，例如 镇安腊肉炒土豆")
    parser.add_argument("url", nargs="?", help="https 图片地址")
    parser.add_argument("--alt", default="", help="图片描述，默认「菜名成品图片」")
    parser.add_argument("--credit-name", default="", help="署名文本")
    parser.add_argument("--credit-url", default="", help="署名链接")
    parser.add_argument("--list", action="store_true", help="列出全部条目")
    parser.add_argument("--delete", action="store_true", help="删除该菜名的条目")
    args = parser.parse_args(argv)

    if args.list:
        return _list_rows()
    if not args.name:
        parser.error("需要菜名，或使用 --list")
    name = args.name.strip()
    if args.delete:
        return _delete_row(name)
    if not args.url:
        parser.error("需要图片地址")
    url = args.url.strip()
    if not _valid_image_url(url):
        print("图片地址必须是无账号密码的 https 链接。", file=sys.stderr)
        return 2
    if args.credit_url and not _valid_image_url(args.credit_url):
        print("署名链接必须是无账号密码的 https 链接。", file=sys.stderr)
        return 2

    with SessionLocal() as db:
        row = db.scalar(select(RecipePhoto).where(RecipePhoto.recipe_name == name))
        action = "更新" if row is not None else "新增"
        if row is None:
            row = RecipePhoto(recipe_name=name)
            db.add(row)
        row.image_url = url
        row.image_alt = args.alt.strip() or f"{name}成品图片"
        row.credit_name = args.credit_name.strip()
        row.credit_url = args.credit_url.strip()
        db.commit()
    print(f"{action}：{name} -> {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
