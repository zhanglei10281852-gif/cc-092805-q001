from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.clock import FrozenClock
from app.core.errors import ConflictError
from app.database import close_connection
from app.mortuary.service import MortuaryService


def create_case(client, ref: str) -> dict:
    response = client.post("/api/mortuary/cases?actor=intake-clerk", json={"external_ref": ref, "decedent_name": "张德安", "identity_number": "ID-440100-1938", "death_time": "2026-09-27T08:30:00Z", "received_from": "市第二医院", "family_contact": "张明", "family_phone": "13800000000", "special_notes": ""})
    assert response.status_code == 201, response.text
    return response.json()


def create_resource(client, code: str, capacity: int = 1, kind: str = "farewell_hall") -> dict:
    response = client.post("/api/mortuary/resources?actor=scheduler", json={"code": code, "name": f"设施{code}", "kind": kind, "site_code": "SITE-1", "capacity": capacity, "attributes": {}})
    assert response.status_code == 201, response.text
    return response.json()


def reserve(client, resource_code: str, case_id: int, start_at: str, end_at: str, key: str, purpose: str = "告别仪式"):
    return client.post("/api/mortuary/reservations", json={"resource_code": resource_code, "case_id": case_id, "start_at": start_at, "end_at": end_at, "purpose": purpose, "created_by": "scheduler", "idempotency_key": key})


def schedule(client, code: str, start_at: str = "2026-09-01T00:00:00Z", end_at: str = "2026-12-01T00:00:00Z") -> dict:
    response = client.get(f"/api/mortuary/resources/{code}/schedule?start_at={start_at}&end_at={end_at}")
    assert response.status_code == 200, response.text
    return response.json()


def test_contained_spanning_and_partial_overlaps_are_rejected(client):
    cases = [create_case(client, f"CASE-O-{index}") for index in range(5)]
    create_resource(client, "HALL-OVERLAP")
    first = reserve(client, "HALL-OVERLAP", cases[0]["id"], "2026-09-29T09:00:00Z", "2026-09-29T12:00:00Z", "overlap-key-0001")
    assert first.status_code == 201
    # 既有预约包住新时段（事故还原：九点至十二点内插入十点至十一点）
    assert reserve(client, "HALL-OVERLAP", cases[1]["id"], "2026-09-29T10:00:00Z", "2026-09-29T11:00:00Z", "overlap-key-0002").status_code == 409
    # 新时段跨过既有预约
    assert reserve(client, "HALL-OVERLAP", cases[2]["id"], "2026-09-29T08:00:00Z", "2026-09-29T13:00:00Z", "overlap-key-0003").status_code == 409
    # 左右两侧部分重叠
    assert reserve(client, "HALL-OVERLAP", cases[3]["id"], "2026-09-29T08:00:00Z", "2026-09-29T09:30:00Z", "overlap-key-0004").status_code == 409
    assert reserve(client, "HALL-OVERLAP", cases[4]["id"], "2026-09-29T11:30:00Z", "2026-09-29T13:00:00Z", "overlap-key-0005").status_code == 409
    # 资源查询仍只反映首户家庭的占用
    hall = schedule(client, "HALL-OVERLAP")
    assert hall["occupied"] == 1
    assert hall["reservations"][0]["case_id"] == cases[0]["id"]


def test_adjacent_slots_are_allowed_in_any_arrival_order(client):
    cases = [create_case(client, f"CASE-A-{index}") for index in range(6)]
    create_resource(client, "HALL-ADJ-1")
    create_resource(client, "HALL-ADJ-2")
    # 顺序一：先早后晚，首尾相接不视为冲突
    assert reserve(client, "HALL-ADJ-1", cases[0]["id"], "2026-09-29T09:00:00Z", "2026-09-29T10:00:00Z", "adjacent-key-001").status_code == 201
    assert reserve(client, "HALL-ADJ-1", cases[1]["id"], "2026-09-29T10:00:00Z", "2026-09-29T11:00:00Z", "adjacent-key-002").status_code == 201
    # 顺序二：先晚后早，结论一致
    assert reserve(client, "HALL-ADJ-2", cases[2]["id"], "2026-09-29T10:00:00Z", "2026-09-29T11:00:00Z", "adjacent-key-003").status_code == 201
    assert reserve(client, "HALL-ADJ-2", cases[3]["id"], "2026-09-29T09:00:00Z", "2026-09-29T10:00:00Z", "adjacent-key-004").status_code == 201
    assert reserve(client, "HALL-ADJ-2", cases[4]["id"], "2026-09-29T11:00:00Z", "2026-09-29T12:00:00Z", "adjacent-key-005").status_code == 201
    # 填满后跨越接缝的时段仍与两侧冲突
    assert reserve(client, "HALL-ADJ-2", cases[5]["id"], "2026-09-29T09:30:00Z", "2026-09-29T10:30:00Z", "adjacent-key-006").status_code == 409


def test_conflict_response_identifies_occupying_source(client):
    family_a = create_case(client, "CASE-SRC-A")
    family_b = create_case(client, "CASE-SRC-B")
    create_resource(client, "HALL-SRC")
    first = reserve(client, "HALL-SRC", family_a["id"], "2026-09-29T09:00:00Z", "2026-09-29T12:00:00Z", "source-key-0001", purpose="张家告别式")
    assert first.status_code == 201
    response = reserve(client, "HALL-SRC", family_b["id"], "2026-09-29T10:00:00Z", "2026-09-29T11:00:00Z", "source-key-0002", purpose="李家告别式")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "conflict"
    context = error["context"]
    assert context["resource_code"] == "HALL-SRC"
    assert context["conflict_ids"] == [first.json()["id"]]
    occupier = context["conflicts"][0]
    assert occupier["reservation_id"] == first.json()["id"]
    assert occupier["case_id"] == family_a["id"]
    assert occupier["case_ref"] == "CASE-SRC-A"
    assert occupier["start_at"] == "2026-09-29T09:00:00+00:00"
    assert occupier["end_at"] == "2026-09-29T12:00:00+00:00"
    assert occupier["purpose"] == "张家告别式"
    # 档案时间线与资源查询反映同一结果：第二户没有任何预约记录
    detail_b = client.get(f"/api/mortuary/cases/{family_b['id']}").json()
    assert [event["event_type"] for event in detail_b["timeline"]] == ["case.registered"]
    assert detail_b["reservations"] == []
    hall = schedule(client, "HALL-SRC")
    assert hall["occupied"] == 1
    assert hall["reservations"][0]["case_ref"] == "CASE-SRC-A"


def test_multi_capacity_resource_counts_and_releases_consistently(client):
    cases = [create_case(client, f"CASE-C-{index}") for index in range(4)]
    create_resource(client, "COLD-STORE", capacity=2, kind="cold_storage")
    first = reserve(client, "COLD-STORE", cases[0]["id"], "2026-09-29T09:00:00Z", "2026-09-29T12:00:00Z", "capacity-key-001")
    second = reserve(client, "COLD-STORE", cases[1]["id"], "2026-09-29T10:00:00Z", "2026-09-29T11:00:00Z", "capacity-key-002")
    assert first.status_code == 201 and second.status_code == 201
    # 容量为二，第三个重叠预约被拒绝
    assert reserve(client, "COLD-STORE", cases[2]["id"], "2026-09-29T10:30:00Z", "2026-09-29T11:30:00Z", "capacity-key-003").status_code == 409
    # 取消一个预约后名额准确释放，相同时段可以补位
    assert client.post(f"/api/mortuary/reservations/{first.json()['id']}/cancel?actor=scheduler&reason=家属改期").status_code == 200
    assert reserve(client, "COLD-STORE", cases[2]["id"], "2026-09-29T10:30:00Z", "2026-09-29T11:30:00Z", "capacity-key-003").status_code == 201
    # 完成一个预约同样释放名额
    assert client.post(f"/api/mortuary/reservations/{second.json()['id']}/complete?actor=scheduler").status_code == 200
    assert reserve(client, "COLD-STORE", cases[3]["id"], "2026-09-29T10:00:00Z", "2026-09-29T11:00:00Z", "capacity-key-004").status_code == 201
    assert schedule(client, "COLD-STORE")["occupied"] == 2


def test_completion_is_idempotent_and_terminal(client):
    case = create_case(client, "CASE-DONE-1")
    create_resource(client, "HALL-DONE")
    reserved = reserve(client, "HALL-DONE", case["id"], "2026-09-29T09:00:00Z", "2026-09-29T10:00:00Z", "done-key-00001")
    assert reserved.status_code == 201
    reservation_id = reserved.json()["id"]
    completed = client.post(f"/api/mortuary/reservations/{reservation_id}/complete?actor=keeper-wang")
    assert completed.status_code == 200 and completed.json()["status"] == "completed"
    repeated = client.post(f"/api/mortuary/reservations/{reservation_id}/complete?actor=keeper-wang")
    assert repeated.status_code == 200 and repeated.json()["id"] == reservation_id
    # 已完成的预约不能取消
    assert client.post(f"/api/mortuary/reservations/{reservation_id}/cancel?actor=keeper-wang&reason=重复操作").status_code == 409
    detail = client.get(f"/api/mortuary/cases/{case['id']}").json()
    assert [event["event_type"] for event in detail["timeline"]] == ["case.registered", "reservation.confirmed", "reservation.completed"]
    assert schedule(client, "HALL-DONE")["occupied"] == 0


def test_cancellation_releases_capacity_and_updates_all_views(client):
    first = create_case(client, "CASE-CXL-1")
    second = create_case(client, "CASE-CXL-2")
    create_resource(client, "HALL-CXL")
    reserved = reserve(client, "HALL-CXL", first["id"], "2026-09-29T09:00:00Z", "2026-09-29T10:00:00Z", "cancel-key-0001")
    assert reserved.status_code == 201
    assert client.post(f"/api/mortuary/reservations/{reserved.json()['id']}/cancel?actor=scheduler&reason=家属调整时间").status_code == 200
    # 取消后时段立即可再约，容量被准确释放
    rebooked = reserve(client, "HALL-CXL", second["id"], "2026-09-29T09:00:00Z", "2026-09-29T10:00:00Z", "cancel-key-0002")
    assert rebooked.status_code == 201
    detail = client.get(f"/api/mortuary/cases/{first['id']}").json()
    assert [event["event_type"] for event in detail["timeline"]] == ["case.registered", "reservation.confirmed", "reservation.cancelled"]
    assert detail["reservations"][0]["status"] == "cancelled"
    hall = schedule(client, "HALL-CXL")
    assert hall["occupied"] == 1
    assert hall["reservations"][0]["case_id"] == second["id"]


def test_duplicate_submission_returns_original_without_consuming_capacity(client):
    cases = [create_case(client, f"CASE-I-{index}") for index in range(3)]
    create_resource(client, "HALL-IDEM", capacity=2)
    payload = {"resource_code": "HALL-IDEM", "case_id": cases[0]["id"], "start_at": "2026-09-29T09:00:00Z", "end_at": "2026-09-29T10:00:00Z", "purpose": "告别仪式", "created_by": "scheduler", "idempotency_key": "idem-key-00001"}
    first = client.post("/api/mortuary/reservations", json=payload)
    replay = client.post("/api/mortuary/reservations", json=payload)
    assert first.status_code == 201 and replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    # 重复提交不重复扣名额：容量为二，仍只剩一个空位
    assert reserve(client, "HALL-IDEM", cases[1]["id"], "2026-09-29T09:00:00Z", "2026-09-29T10:00:00Z", "idem-key-00002").status_code == 201
    assert reserve(client, "HALL-IDEM", cases[2]["id"], "2026-09-29T09:00:00Z", "2026-09-29T10:00:00Z", "idem-key-00003").status_code == 409
    assert schedule(client, "HALL-IDEM")["occupied"] == 2
    # 同一幂等键对应不同内容必须拒绝
    changed = dict(payload, start_at="2026-09-29T10:00:00Z", end_at="2026-09-29T11:00:00Z")
    assert client.post("/api/mortuary/reservations", json=changed).status_code == 409
    detail = client.get(f"/api/mortuary/cases/{cases[0]['id']}").json()
    assert [event["event_type"] for event in detail["timeline"]].count("reservation.confirmed") == 1


def test_schedule_endpoint_validates_window_and_resource(client):
    create_resource(client, "HALL-Q")
    assert client.get("/api/mortuary/resources/HALL-Q/schedule?start_at=2026-09-29T00:00:00Z").status_code == 422
    assert client.get("/api/mortuary/resources/HALL-Q/schedule?start_at=2026-09-29T10:00:00Z&end_at=2026-09-29T09:00:00Z").status_code == 422
    assert client.get("/api/mortuary/resources/NO-SUCH/schedule").status_code == 404


@pytest.fixture()
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("PEACEFUL_CARE_DATABASE_PATH", str(tmp_path / "service.db"))
    close_connection()
    clock = FrozenClock(datetime(2026, 9, 30, 8, 0, tzinfo=UTC))
    yield MortuaryService(clock=clock)
    close_connection()


def service_case(service: MortuaryService, ref: str) -> dict:
    return service.create_case({"external_ref": ref, "decedent_name": "张德安", "identity_number": None, "death_time": datetime(2026, 9, 27, 8, 30, tzinfo=UTC), "received_from": "市第二医院", "family_contact": "张明", "family_phone": "13800000000", "special_notes": ""}, actor="intake-clerk")


def service_reservation(case_id: int, start: datetime, end: datetime, key: str) -> dict:
    return {"resource_code": "HALL-NIGHT", "case_id": case_id, "start_at": start, "end_at": end, "purpose": "跨日守灵", "created_by": "scheduler", "idempotency_key": key}


def test_cross_day_booking_blocks_later_arrivals_with_controlled_clock(service):
    service.create_resource({"code": "HALL-NIGHT", "name": "夜间送别厅", "kind": "farewell_hall", "site_code": "SITE-1", "capacity": 1, "attributes": {}}, actor="scheduler")
    first, second, third, fourth = (service_case(service, f"CASE-N-{index}") for index in range(4))
    # 到达顺序一：跨日预约（十月一日二十二点至十月二日凌晨两点）先登记
    night = service.reserve(service_reservation(first["id"], datetime(2026, 10, 1, 22, 0, tzinfo=UTC), datetime(2026, 10, 2, 2, 0, tzinfo=UTC), "night-key-00001"))
    assert night["created_at"] == "2026-09-30T08:00:00+00:00"
    service.clock.advance(hours=2)
    # 次日凌晨跨过既有预约的时段被拒绝，与到达顺序无关
    with pytest.raises(ConflictError):
        service.reserve(service_reservation(second["id"], datetime(2026, 10, 2, 1, 0, tzinfo=UTC), datetime(2026, 10, 2, 3, 0, tzinfo=UTC), "night-key-00002"))
    # 与跨日预约在凌晨两点首尾相接，允许安排
    adjacent = service.reserve(service_reservation(third["id"], datetime(2026, 10, 2, 2, 0, tzinfo=UTC), datetime(2026, 10, 2, 4, 0, tzinfo=UTC), "night-key-00003"))
    assert adjacent["created_at"] == "2026-09-30T10:00:00+00:00"
    # 次日相同时段属于不同夜晚，不被误判冲突
    assert service.reserve(service_reservation(fourth["id"], datetime(2026, 10, 2, 22, 0, tzinfo=UTC), datetime(2026, 10, 3, 2, 0, tzinfo=UTC), "night-key-00004"))["status"] == "confirmed"
    hall = service.resource_schedule("HALL-NIGHT", datetime(2026, 10, 1, 0, 0, tzinfo=UTC), datetime(2026, 10, 3, 0, 0, tzinfo=UTC))
    assert hall["occupied"] == 3
    assert [row["case_id"] for row in hall["reservations"]] == [first["id"], third["id"], fourth["id"]]


def test_cross_day_booking_rejected_when_it_arrives_second(service):
    service.create_resource({"code": "HALL-NIGHT", "name": "夜间送别厅", "kind": "farewell_hall", "site_code": "SITE-1", "capacity": 1, "attributes": {}}, actor="scheduler")
    first, second = (service_case(service, f"CASE-M-{index}") for index in range(2))
    # 到达顺序二：凌晨时段先登记，跨日预约随后到达，结论必须一致
    assert service.reserve(service_reservation(first["id"], datetime(2026, 10, 2, 1, 0, tzinfo=UTC), datetime(2026, 10, 2, 3, 0, tzinfo=UTC), "order-key-00001"))["status"] == "confirmed"
    with pytest.raises(ConflictError):
        service.reserve(service_reservation(second["id"], datetime(2026, 10, 1, 22, 0, tzinfo=UTC), datetime(2026, 10, 2, 2, 0, tzinfo=UTC), "order-key-00002"))
    hall = service.resource_schedule("HALL-NIGHT")
    assert hall["occupied"] == 1
    assert hall["reservations"][0]["case_id"] == first["id"]
