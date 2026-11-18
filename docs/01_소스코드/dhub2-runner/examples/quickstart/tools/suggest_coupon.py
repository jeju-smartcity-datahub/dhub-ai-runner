TIERS = [(200_000, 15), (100_000, 10), (0, 5)]


def execute(total_amount):
    percent = next(p for floor, p in TIERS if total_amount >= floor)
    return {"percent": percent, "reason": f"paid total {total_amount:,} KRW"}
