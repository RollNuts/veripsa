"""Background worker — ALSO reads `payment_gateway_url` (so it co-depends on the same config key)."""


def reconcile(settings):
    endpoint = settings["payment_gateway_url"]
    return f"reconciling against {endpoint}"
