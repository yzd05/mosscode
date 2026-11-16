"""Compare normalized evaluation reports without launching an agent."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.harbor.results import controlled_document, estimate_cost, markdown, read_json, write_json  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description="核对实验条件，生成逐任务评估对照；不会调用模型")
    parser.add_argument("results", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True, help="Markdown 输出路径")
    parser.add_argument("--prices", type=Path, help="用明确的共同价格快照重算费用，不改原始文件")
    args = parser.parse_args(argv)
    try:
        documents = []
        for path in args.results:
            data = read_json(path)
            if "rows" in data:
                data = controlled_document(data, path)
            documents.append(data)
        repricing = read_json(args.prices) if args.prices else None
        if repricing:
            import math

            if (
                not repricing.get("source")
                or not repricing.get("date")
                or any(
                    type(repricing.get(k)) not in (int, float) or not math.isfinite(repricing[k]) or repricing[k] < 0
                    for k in ("input", "cached_input", "output")
                )
            ):
                raise ValueError("共同价格快照需记录来源、日期及非负 USD/百万 Token 价格")
            for doc in documents:
                doc["manifest"]["prices"] = repricing
                for row in doc["attempts"]:
                    row["cost_usd"] = estimate_cost(row, repricing)
                    row["cost_source"] = "uniform_price_snapshot"
        text, summaries = markdown(documents)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    md_output = args.output.with_suffix(".md") if args.output.suffix.lower() == ".json" else args.output
    json_output = args.output if args.output.suffix.lower() == ".json" else args.output.with_suffix(".json")
    md_output.parent.mkdir(parents=True, exist_ok=True)
    md_output.write_text(text, encoding="utf-8")
    write_json(
        json_output,
        {"sources": [str(p.resolve()) for p in args.results], "summary": summaries, "repricing": repricing},
    )


if __name__ == "__main__":
    main()
