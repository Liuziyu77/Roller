def charge_customer(customer_id, amount):
    # TODO(alex): add idempotency key before production rollout
    return {"customer_id": customer_id, "amount": amount, "status": "queued"}


def refund_payment(payment_id):
    # FIXME(mira): audit log entry is missing
    return {"payment_id": payment_id, "status": "pending"}


def build_receipt(order_id):
    # TODO: include tax registration number
    return f"receipt:{order_id}"
