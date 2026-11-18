import json
from datetime import datetime, timezone

import polars as pl


def run(analysis, options=None, contexts=None):
    if isinstance(analysis, str):
        analysis = json.loads(analysis)
    return {
        "insights": pl.DataFrame(
            {
                "analyzed_at": [datetime.now(timezone.utc).replace(tzinfo=None)],
                "risk_level": [analysis.get("risk_level")],
                "summary": [analysis.get("summary")],
                "alerts": [json.dumps(analysis.get("alerts") or [], ensure_ascii=False)],
            }
        )
    }
