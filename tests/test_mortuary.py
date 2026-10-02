from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.clock import FrozenClock
from app.core.errors import ConflictError
from app.database import close_connection
from app.mortuary.service import MortuaryService


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


def _make_resource(client, code: str, capacity: int = 1):
    response = client.post("/api/mortuary/resources?actor=scheduler", json={"code": code, "name": f"{code} 厅", "kind": "farewell_hall", "site_code": "SITE-1", "capacity": capacity, "attributes": {}})
    assert response.status_code == 201, response.text
    return response.json()


def _reservation_payload(case: dict, key: str, start: str, end: str, code: str = "HALL-A") -> dict:
    return {"resource_code": code, "case_id": case["id"], "start_at": start, "end_at": end, "purpose": "家属告别", "created_by": "scheduler", "idempotency_key": key}


def test_existing_window_enclosing_new_slot_is_conflict(client):
    """事故复现：九点到十二点已占用，十点到十一点必须被拒。"""
    first = create_case(client, "CASE-WIN-001")
    _make_resource(client, "HALL-A")
    morning = client.post("/api/mortuary/reservations", json=_reservation_payload(first, "win-long-001", "2026-10-01T09:00:00Z", "2026-10-01T12:00:00Z"))
    assert morning.status_code == 201

    second = create_case(client, "CASE-WIN-002")
    nested = client.post("/api/mortuary/reservations", json=_reservation_payload(second, "win-inner-001", "2026-10-01T10:00:00Z", "2026-10-01T11:00:00Z"))
    assert nested.status_code == 409
    context = nested.json()["error"]["context"]
    # 冲突响应必须指出占用来源：是哪一户、哪张预约、什么时段。
    assert context["conflict_ids"] == [morning.json()["id"]]
    occupant = context["occupants"][0]
    assert occupant["reservation_id"] == morning.json()["id"]
    assert occupant["case_ref"] == "CASE-WIN-001"
    assert occupant["decedent_name"] == "张德安"
    assert occupant["start_at"] == "2026-10-01T09:00:00+00:00"
    assert occupant["end_at"] == "2026-10-01T12:00:00+00:00"

    # 资源查询必须反映同一结果：十点到十一点仍只有第一户。
    schedule = client.get("/api/mortuary/resources/HALL-A/schedule?start_at=2026-10-01T10:00:00Z&end_at=2026-10-01T11:00:00Z")
    assert schedule.status_code == 200
    body = schedule.json()
    assert body["confirmed_count"] == 1
    assert body["fully_booked"] is True
    assert body["reservations"][0]["case_ref"] == "CASE-WIN-001"


def test_overlap_shapes_and_touching_intervals_agree(client):
    """包住、跨过、部分重叠、首尾相接四种形态给出一致结论。"""
    first = create_case(client, "CASE-OVL-001")
    second = create_case(client, "CASE-OVL-002")
    _make_resource(client, "HALL-B")
    base = client.post("/api/mortuary/reservations", json=_reservation_payload(first, "ovl-base-001", "2026-10-03T10:00:00Z", "2026-10-03T12:00:00Z", code="HALL-B"))
    assert base.status_code == 201

    def attempt(key, start, end):
        return client.post("/api/mortuary/reservations", json=_reservation_payload(second, key, start, end, code="HALL-B"))

    # 新时段跨过既有预约
    assert attempt("ovl-straddle", "2026-10-03T09:00:00Z", "2026-10-03T13:00:00Z").status_code == 409
    # 前部重叠
    assert attempt("ovl-head", "2026-10-03T09:00:00Z", "2026-10-03T10:30:00Z").status_code == 409
    # 后部重叠
    assert attempt("ovl-tail", "2026-10-03T11:30:00Z", "2026-10-03T13:00:00Z").status_code == 409
    # 同刻开始
    assert attempt("ovl-samestart", "2026-10-03T10:00:00Z", "2026-10-03T10:30:00Z").status_code == 409
    # 首尾相接：十二点结束、十二点开始，可以预约
    touching = attempt("ovl-touch-end", "2026-10-03T12:00:00Z", "2026-10-03T13:00:00Z")
    assert touching.status_code == 201, touching.text
    # 首尾相接（前侧）
    touching_front = attempt("ovl-touch-start", "2026-10-03T08:00:00Z", "2026-10-03T10:00:00Z")
    assert touching_front.status_code == 201, touching_front.text


def test_capacity_released_on_cancel_and_complete(client):
    family_a = create_case(client, "CASE-CAP-001")
    family_b = create_case(client, "CASE-CAP-002")
    family_c = create_case(client, "CASE-CAP-003")
    family_d = create_case(client, "CASE-CAP-004")
    _make_resource(client, "HALL-C", capacity=2)

    a = client.post("/api/mortuary/reservations", json=_reservation_payload(family_a, "cap-key-a", "2026-10-05T09:00:00Z", "2026-10-05T11:00:00Z", code="HALL-C"))
    b = client.post("/api/mortuary/reservations", json=_reservation_payload(family_b, "cap-key-b", "2026-10-05T09:30:00Z", "2026-10-05T10:30:00Z", code="HALL-C"))
    assert a.status_code == 201 and b.status_code == 201

    third = client.post("/api/mortuary/reservations", json=_reservation_payload(family_c, "cap-key-c", "2026-10-05T10:00:00Z", "2026-10-05T10:30:00Z", code="HALL-C"))
    assert third.status_code == 409
    assert third.json()["error"]["context"]["capacity"] == 2
    assert third.json()["error"]["context"]["occupancy"] == 2

    # 取消后释放容量，第三户可以进入；重复取消不会产生额外影响
    assert client.post(f"/api/mortuary/reservations/{a.json()['id']}/cancel?actor=scheduler&reason=家属改期").status_code == 200
    assert client.post(f"/api/mortuary/reservations/{a.json()['id']}/cancel?actor=scheduler&reason=家属改期").status_code == 200
    assert third.status_code == 409  # 旧响应对象不变
    retry = client.post("/api/mortuary/reservations", json=_reservation_payload(family_c, "cap-key-c", "2026-10-05T10:00:00Z", "2026-10-05T10:30:00Z", code="HALL-C"))
    assert retry.status_code == 201, retry.text

    # 完成同样释放容量；已完成的预约不再阻挡后续安排
    done = client.post(f"/api/mortuary/reservations/{b.json()['id']}/complete?actor=attendant-zhao")
    assert done.status_code == 200 and done.json()["status"] == "completed"
    assert client.post(f"/api/mortuary/reservations/{b.json()['id']}/complete?actor=attendant-zhao").status_code == 200
    fourth = client.post("/api/mortuary/reservations", json=_reservation_payload(family_d, "cap-key-d", "2026-10-05T09:30:00Z", "2026-10-05T11:00:00Z", code="HALL-C"))
    assert fourth.status_code == 201, fourth.text
    # 已完成的预约不能取消
    assert client.post(f"/api/mortuary/reservations/{b.json()['id']}/cancel?actor=scheduler&reason=误操作").status_code == 409

    schedule = client.get("/api/mortuary/resources/HALL-C/schedule?start_at=2026-10-05T09:00:00Z&end_at=2026-10-05T11:00:00Z").json()
    statuses = sorted(r["status"] for r in schedule["reservations"])
    assert statuses == ["cancelled", "completed", "confirmed", "confirmed"]
    assert schedule["confirmed_count"] == 2


def test_repeated_submission_returns_original_reservation(client):
    family = create_case(client, "CASE-IDEM-001")
    _make_resource(client, "HALL-D")
    payload = _reservation_payload(family, "idem-repeat-001", "2026-10-06T09:00:00Z", "2026-10-06T10:00:00Z", code="HALL-D")
    first = client.post("/api/mortuary/reservations", json=payload)
    assert first.status_code == 201
    repeated = client.post("/api/mortuary/reservations", json=payload)
    assert repeated.status_code == 201
    assert repeated.json()["id"] == first.json()["id"]

    schedule = client.get("/api/mortuary/resources/HALL-D/schedule?start_at=2026-10-06T08:00:00Z&end_at=2026-10-06T11:00:00Z").json()
    assert schedule["confirmed_count"] == 1  # 没有重复扣名额

    other = create_case(client, "CASE-IDEM-002")
    divergent = client.post("/api/mortuary/reservations", json=_reservation_payload(other, "idem-repeat-001", "2026-10-06T11:00:00Z", "2026-10-06T12:00:00Z", code="HALL-D"))
    assert divergent.status_code == 409

    detail = client.get(f"/api/mortuary/cases/{family['id']}").json()
    assert [e["event_type"] for e in detail["timeline"]].count("reservation.confirmed") == 1


def test_case_timeline_and_resource_schedule_stay_consistent(client):
    family = create_case(client, "CASE-TL-001")
    _make_resource(client, "HALL-E")
    reserved = client.post("/api/mortuary/reservations", json=_reservation_payload(family, "tl-key-001", "2026-10-07T08:00:00Z", "2026-10-07T09:30:00Z", code="HALL-E"))
    assert reserved.status_code == 201
    client.post(f"/api/mortuary/reservations/{reserved.json()['id']}/cancel?actor=scheduler&reason=家属申请改期")

    case_view = client.get(f"/api/mortuary/cases/{family['id']}").json()
    assert [r["status"] for r in case_view["reservations"]] == ["cancelled"]
    assert [e["event_type"] for e in case_view["timeline"]] == ["case.registered", "reservation.confirmed", "reservation.cancelled"]

    schedule = client.get("/api/mortuary/resources/HALL-E/schedule?start_at=2026-10-07T08:00:00Z&end_at=2026-10-07T09:30:00Z").json()
    assert schedule["confirmed_count"] == 0
    assert schedule["fully_booked"] is False
    assert schedule["reservations"][0]["status"] == "cancelled"  # 取消记录仍可查，但不占容量

    # 释放后同档期可被新家庭使用
    other = create_case(client, "CASE-TL-002")
    replacement = client.post("/api/mortuary/reservations", json=_reservation_payload(other, "tl-key-002", "2026-10-07T08:00:00Z", "2026-10-07T09:30:00Z", code="HALL-E"))
    assert replacement.status_code == 201


@pytest.fixture()
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("PEACEFUL_CARE_DATABASE_PATH", str(tmp_path / "mortuary.db"))
    close_connection()
    clock = FrozenClock(datetime(2026, 10, 1, 0, 0, tzinfo=UTC))
    yield MortuaryService(clock=clock)
    close_connection()


def _svc_case(service: MortuaryService, ref: str) -> int:
    case = service.create_case(
        {"external_ref": ref, "decedent_name": "张德安", "identity_number": None, "death_time": datetime(2026, 9, 30, 8, 0, tzinfo=UTC), "received_from": "市第二医院", "family_contact": "张明", "family_phone": "13800000000", "special_notes": ""},
        "scheduler",
    )
    return case["id"]


def _svc_reserve(service: MortuaryService, case_id: int, key: str, start: datetime, end: datetime):
    return service.reserve({"resource_code": "HALL-X", "case_id": case_id, "start_at": start, "end_at": end, "purpose": "跨日告别", "created_by": "scheduler", "idempotency_key": key})


CROSS_DAY_WINDOWS = {
    # 跨午夜的长预约
    "long": (datetime(2026, 10, 1, 22, 0, tzinfo=UTC), datetime(2026, 10, 2, 2, 0, tzinfo=UTC)),
    # 完全落在长预约内部
    "inner": (datetime(2026, 10, 1, 23, 0, tzinfo=UTC), datetime(2026, 10, 2, 0, 0, tzinfo=UTC)),
    # 与长预约尾部重叠（次日凌晨，02:00 整与 after 首尾相接）
    "tail": (datetime(2026, 10, 2, 1, 0, tzinfo=UTC), datetime(2026, 10, 2, 2, 0, tzinfo=UTC)),
    # 与长预约首尾相接（次日）
    "after": (datetime(2026, 10, 2, 2, 0, tzinfo=UTC), datetime(2026, 10, 2, 3, 0, tzinfo=UTC)),
    # 与长预约首尾相接（前晚）
    "before": (datetime(2026, 10, 1, 20, 0, tzinfo=UTC), datetime(2026, 10, 1, 22, 0, tzinfo=UTC)),
}


@pytest.mark.parametrize(
    "order,expected_confirmed",
    [
        (["long", "inner", "tail", "after", "before"], {"long", "after", "before"}),
        (["inner", "long", "tail", "after", "before"], {"inner", "tail", "after", "before"}),
        (["tail", "long", "inner", "after", "before"], {"tail", "inner", "after", "before"}),
        (["before", "after", "inner", "tail", "long"], {"before", "after", "inner", "tail"}),
        (["inner", "tail", "before", "long", "after"], {"inner", "tail", "before", "after"}),
    ],
)
def test_cross_day_overlap_independent_of_arrival_order(service, order, expected_confirmed):
    """跨日预约在不同到达顺序下都不会被重叠安排（可控时间 FrozenClock）。"""
    service.create_resource({"code": "HALL-X", "name": "跨日厅", "kind": "farewell_hall", "site_code": "SITE-1", "capacity": 1, "attributes": {}}, "scheduler")
    case_ids = {name: _svc_case(service, f"CASE-XD-{name}") for name in CROSS_DAY_WINDOWS}

    confirmed: set[str] = set()
    blockers: dict[str, str] = {}
    for name in order:
        start, end = CROSS_DAY_WINDOWS[name]
        try:
            reservation = _svc_reserve(service, case_ids[name], f"xd-{name}", start, end)
        except ConflictError as exc:
            assert name not in expected_confirmed
            blockers[name] = exc.context["occupants"][0]["case_ref"]
        else:
            assert name in expected_confirmed
            assert reservation["status"] == "confirmed"
            confirmed.add(name)

    assert confirmed == expected_confirmed

    # 被拒者必须能从冲突响应中知道占用来源
    if "long" in confirmed:
        assert blockers["inner"] == "CASE-XD-long"
        assert blockers["tail"] == "CASE-XD-long"

    # 资源查询与判定结果一致：整段窗口内的已确认预约彼此不重叠，数量等于容量允许集合
    schedule = service.resource_schedule(
        "HALL-X",
        "2026-10-01T00:00:00+00:00",
        "2026-10-03T00:00:00+00:00",
    )
    scheduled = [r for r in schedule["reservations"] if r["status"] == "confirmed"]
    assert {r["idempotency_key"] for r in scheduled} == {f"xd-{n}" for n in expected_confirmed}
    assert schedule["confirmed_count"] == len(expected_confirmed)
    for i, left in enumerate(scheduled):
        for right in scheduled[i + 1:]:
            assert left["end_at"] <= right["start_at"] or right["end_at"] <= left["start_at"]

