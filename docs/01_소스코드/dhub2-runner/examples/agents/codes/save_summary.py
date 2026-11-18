import json
from datetime import datetime, timezone

import polars as pl


def run(summary, options=None, contexts=None):
    if isinstance(summary, str):
        summary = json.loads(summary)
    return {
        "output": pl.DataFrame(
            {
                "summarized_at": [datetime.now(timezone.utc).replace(tzinfo=None)],
                "sentiment": [summary.get("sentiment")],
                "summary": [summary.get("summary")],
                "top_issues": [", ".join(summary.get("top_issues") or [])],
            }
        )
    }
