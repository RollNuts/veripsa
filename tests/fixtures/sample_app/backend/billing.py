"""Billing — READS the `payment_gateway_url` config key (config coupling to worker.py, same key)."""


def charge(cfg, amount: int):
    url = cfg["payment_gateway_url"]
    return f"POST {url} amount={amount}"
