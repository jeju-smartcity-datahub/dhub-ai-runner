import random
from datetime import datetime, timedelta, timezone

import polars as pl

CUSTOMERS = [("c1", "Kim"), ("c2", "Lee"), ("c3", "Park"), ("c4", "Choi")]
STATUSES = ["paid", "paid", "paid", "refunded"]


def run(options=None, contexts=None):
    count = int((options or {}).get("count", 20))
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    rows = []
    for i in range(count):
        customer_id, name = random.choice(CUSTOMERS)
        rows.append(
            {
                "order_id": f"{now:%Y%m%d%H%M%S}-{i:03d}",
                "customer_id": customer_id,
                "customer_name": name,
                "amount": random.randint(1, 50) * 1000,
                "status": random.choice(STATUSES),
                "ordered_at": now - timedelta(days=random.randint(0, 6)),
            }
        )
    return {"orders": pl.DataFrame(rows)}
