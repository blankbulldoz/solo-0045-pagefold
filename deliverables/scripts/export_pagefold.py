#!/usr/bin/env python3
"""读法一：用仓库内 pagefold 包把镜像导出为带类型标签的 Pagefold JSON。

完全复用 pagefold 的登记读取链路（sqlite_image.mounted → catalog.describe →
constraints.check_database → rows.read_image），不重新实现任何编码规则。产出两份文件：

  pagefold-export.json   结构化整库导出（目录 + 每张表的键/值原子），供程序对照
  pagefold-rows.jsonl    每行一条，显式给出 table / key / values / 键定位，供逐行核对

原子标签与 README 一致：i=整数（字符串承载，避免 JS 精度问题）、r=实数（IEEE-754 hex）、
t=文本、b=二进制（base64）、n=NULL。以 `python3 -I` 运行。
"""

import argparse
import json
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pagefold.catalog import describe  # noqa: E402
from pagefold.constraints import check_database  # noqa: E402
from pagefold.rows import read_image  # noqa: E402
from pagefold.sqlite_image import mounted  # noqa: E402

TYPE_LABEL = {"i": "integer", "r": "real", "t": "text", "b": "blob", "n": "null"}


def main() -> None:
    parser = argparse.ArgumentParser(description="导出 Pagefold 带类型 JSON")
    parser.add_argument("mirror", type=Path)
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args()

    data = args.mirror.read_bytes()
    with mounted(data) as db:
        catalog = describe(db)
        check_database(db)
        images = read_image(db, catalog)

    structured = {
        "source": args.mirror.name,
        "schema_signature": catalog["signature"],
        "atom_legend": {
            "i": "integer，数值以字符串精确承载",
            "r": "real，IEEE-754 双精度 hex 表示",
            "t": "text",
            "b": "blob，base64 承载",
            "n": "NULL",
        },
        "tables": [],
    }
    jsonl_records = []

    for table in catalog["tables"]:
        name = table["name"]
        key_cols = table["key"]
        payload = {
            "table": name,
            "columns": table["columns"],
            "declared_types": table["types"],
            "key_columns": key_cols,
            "row_count": len(images[name]),
            "rows": [],
        }
        for token in sorted(images[name]):
            key = json.loads(token)
            values = images[name][token]
            record = {
                "table": name,
                "key_columns": key_cols,
                "key": key,
                "value_columns": table["columns"],
                "values": values,
                "value_storage_types": [
                    None if atom[0] == "n" else TYPE_LABEL[atom[0]] for atom in values
                ],
            }
            payload["rows"].append(record)
            jsonl_records.append(record)
        structured["tables"].append(payload)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / "pagefold-export.json"
    jsonl_path = args.out_dir / "pagefold-rows.jsonl"
    json_path.write_text(json.dumps(structured, ensure_ascii=False, indent=2), encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for record in jsonl_records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    print(f"wrote {json_path}")
    print(f"wrote {jsonl_path} ({len(jsonl_records)} rows)")


if __name__ == "__main__":
    main()
