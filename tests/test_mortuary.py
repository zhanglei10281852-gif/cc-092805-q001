from __future__ import annotations


def create_case(client, ref: str = "CASE-001") -> dict:
    response = client.post("/api/mortuary/cases?actor=intake-clerk", json={"external_ref": ref, "decedent_name": "张德安", "identity_number": "ID-440100-1938", "death_time": "2026-09-27T08:30:00Z", "received_from": "市第二医院", "family_contact": "张明", "family_phone": "13800000000", "special_notes": "家属要求核对随身物品"})
    assert response.status_code == 201, response.text
    return response.json()


def test_case_custody_timeline_and_idempotency(client):
    case = create_case(client)
    payload = {"from_location": "接运车辆A", "to_location": "冷藏室C-01", "seal_code": "SEAL-1001", "requested_by": "driver-li", "idempotency_key": "custody-case-001"}
    requested = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json=payload)
    repeated = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json=payload)
    assert requested.status_code == 201
    assert repeated.json()["id"] == requested.json()["id"]
    accepted = client.post(f"/api/mortuary/custody-transfers/{requested.json()['id']}/accept", json={"accepted_by": "keeper-wang", "observed_seal_code": "SEAL-1001", "condition_note": "封签完整"})
    assert accepted.status_code == 200
    detail = client.get(f"/api/mortuary/cases/{case['id']}").json()
    assert detail["status"] == "in_custody"
    assert detail["current_location"] == "冷藏室C-01"
    assert [event["event_type"] for event in detail["timeline"]] == ["case.registered", "custody.requested", "custody.accepted"]


def test_resource_reservation_and_cancellation(client):
    first = create_case(client, "CASE-R-001")
    second = create_case(client, "CASE-R-002")
    assert client.post("/api/mortuary/resources?actor=scheduler", json={"code": "HALL-A", "name": "送别厅A", "kind": "farewell_hall", "site_code": "SITE-1", "capacity": 1, "attributes": {"seats": 120}}).status_code == 201
    payload = {"resource_code": "HALL-A", "case_id": first["id"], "start_at": "2026-09-29T09:00:00Z", "end_at": "2026-09-29T10:00:00Z", "purpose": "家属告别", "created_by": "scheduler", "idempotency_key": "hall-a-first-001"}
    reserved = client.post("/api/mortuary/reservations", json=payload)
    assert reserved.status_code == 201
    next_slot = dict(payload, case_id=second["id"], start_at="2026-09-29T10:00:00Z", end_at="2026-09-29T11:00:00Z", idempotency_key="hall-a-second-001")
    assert client.post(f"/api/mortuary/reservations/{reserved.json()['id']}/cancel?actor=scheduler&reason=家属调整时间").status_code == 200
    assert client.post("/api/mortuary/reservations", json=next_slot).status_code == 201


def test_orders_invoice_and_payment(client):
    case = create_case(client, "CASE-F-001")
    order_ids = []
    for code, quantity, price in (("body-care", 1, 80000), ("farewell-hall", 2, 120000)):
        order = client.post("/api/mortuary/service-orders", json={"case_id": case["id"], "service_code": code, "quantity": quantity, "unit_price_cents": price, "requested_by": "family-service", "notes": "已与家属核对"})
        assert order.status_code == 201
        order_ids.append(order.json()["id"])
        assert client.post(f"/api/mortuary/service-orders/{order.json()['id']}/confirm?actor=finance-reviewer").status_code == 200
    invoice = client.post("/api/mortuary/invoices", json={"case_id": case["id"], "order_ids": order_ids, "created_by": "cashier"})
    assert invoice.status_code == 201
    payment = {"amount_cents": 100000, "channel": "bank", "external_reference": "PAY-20260928-001", "received_by": "cashier"}
    assert client.post(f"/api/mortuary/invoices/{invoice.json()['id']}/payments", json=payment).status_code == 200
    repeated = client.post(f"/api/mortuary/invoices/{invoice.json()['id']}/payments", json=payment)
    assert repeated.status_code == 200 and repeated.json()["paid_cents"] == 100000


def test_burial_right_normal_renewal(client):
    right = client.post("/api/mortuary/burial-rights?actor=cemetery-clerk", json={"plot_code": "A-01-001", "holder_name": "李冬梅", "holder_identity": "ID-3301-001", "starts_on": "2025-01-01", "expires_on": "2045-01-01", "case_id": None})
    assert right.status_code == 201
    renewed = client.post(f"/api/mortuary/burial-rights/{right.json()['id']}/renew", json={"years": 5, "handled_by": "cemetery-clerk", "payment_reference": "RIGHT-PAY-001"})
    assert renewed.status_code == 200 and renewed.json()["expires_on"] == "2050-01-01"


def test_invalid_seal_rejects_transfer(client):
    case = create_case(client, "CASE-SEAL-001")
    transfer = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json={"from_location": "医院太平间", "to_location": "冷藏室C-02", "seal_code": "SEAL-2001", "requested_by": "driver-chen", "idempotency_key": "custody-seal-001"})
    response = client.post(f"/api/mortuary/custody-transfers/{transfer.json()['id']}/accept", json={"accepted_by": "keeper-zhao", "observed_seal_code": "SEAL-WRONG", "condition_note": "编号不符"})
    assert response.status_code == 409
    detail = client.get(f"/api/mortuary/cases/{case['id']}").json()
    assert detail["status"] == "registered"
