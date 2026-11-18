def execute(customer_id, percent):
    # A real actor would call a messaging API here.
    return {"sent": True, "customer_id": customer_id, "coupon": f"SAVE{percent}"}
