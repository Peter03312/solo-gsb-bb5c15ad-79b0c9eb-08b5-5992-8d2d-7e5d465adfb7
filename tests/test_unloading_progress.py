"""卸箱执行进度（现场逐步登记）API 端到端测试。"""

import threading

from fastapi.testclient import TestClient

from app.main import app


# 主夹具：A01/A02（臂 -2），B01（臂 +5）；配载结果
# C001(10,港1)->A02(上)、C002(30,港2)->A01(下)、C003(10,港1)->B01。
# 初始总力矩 = 10*(-2) + 30*(-2) + 10*5 = -30
SLOT_PAYLOAD = {
    "slots": [
        {"code": "A01", "stack": "A", "tier": 1, "capacity": 40, "arm": -2.0},
        {"code": "A02", "stack": "A", "tier": 2, "capacity": 15, "arm": -2.0},
        {"code": "B01", "stack": "B", "tier": 1, "capacity": 15, "arm": 5.0},
    ],
    "adjacency": [],
    "incompatible_classes": [],
    "min_moment": -100000,
    "max_moment": 100000,
}

MANIFEST_PAYLOAD = {
    "containers": [
        {"no": "C001", "weight": 10, "hazmat": None, "discharge_order": 1},
        {"no": "C002", "weight": 30, "hazmat": None, "discharge_order": 2},
        {"no": "C003", "weight": 10, "hazmat": None, "discharge_order": 1},
    ]
}

WIDE_PORTS = {
    "ports": [
        {"discharge_order": 1, "min_remaining_moment": -100, "max_remaining_moment": 100},
        {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 100},
    ]
}


def _seed_and_confirm(client, slots=None, manifest=None):
    sr = client.post("/api/slot-revisions", json=slots or SLOT_PAYLOAD) \
        .json()["slots_revision"]
    mr = client.post("/api/manifest-revisions", json=manifest or MANIFEST_PAYLOAD) \
        .json()["manifest_revision"]
    plan = client.post("/api/plans/compute", json={}).json()
    client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    return plan


def _seed_with_unloading(client, ports=None, slots=None, manifest=None):
    plan = _seed_and_confirm(client, slots=slots, manifest=manifest)
    r = client.post(f"/api/plans/{plan['plan_id']}/unloading",
                    json=ports or WIDE_PORTS)
    assert r.status_code == 201, r.text
    return plan


def _register(client, plan_id, op, container, version):
    return client.post(f"/api/plans/{plan_id}/unloading-progress", json={
        "operation_no": op, "container_no": container, "expected_version": version,
    })


# ---------- 基本登记与偏离预案 ----------


def test_register_deviation_from_planned_next_step(client):
    """预案下一步为 C001，现场改选 C003（同港、栈顶、力矩达标）应登记成功。"""
    plan = _seed_with_unloading(client)
    pid = plan["plan_id"]
    planned = [s["container_no"] for s in
               client.get(f"/api/plans/{pid}/unloading").json()["steps"]]
    assert planned == ["C001", "C003", "C002"]

    r = _register(client, pid, "OP-1", "C003", 0)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["version"] == 1
    assert data["plan_status"] == "confirmed" and data["read_only"] is False
    assert data["initial_moment"] == -30 and data["remaining_moment"] == -80
    assert data["current_port"] == 1 and data["completed"] is False
    assert data["remaining_containers"] == ["C001", "C002"]
    # 已卸步骤携带操作号、期望版本与冻结箱重/力臂快照
    step = data["done_steps"][0]
    assert (step["step_no"], step["operation_no"], step["container_no"]) == \
        (1, "OP-1", "C003")
    assert step["expected_version"] == 0 and step["remaining_moment"] == -80
    assert step["weight"] == 10 and step["arm"] == 5 and step["slot_code"] == "B01"
    # 剩余推荐顺序按全程箱号字典序规则：C001 -> C002，步序接续
    rec = [(s["step_no"], s["container_no"], s["remaining_moment"])
           for s in data["recommended_steps"]]
    assert rec == [(2, "C001", -60), (3, "C002", 0)]
    # 冻结港区间随进度返回
    assert [(p["discharge_order"], p["min_remaining_moment"],
             p["max_remaining_moment"]) for p in data["ports"]] == [
        (1, -100, 100), (2, -100, 100)
    ]


def test_full_execution_until_completed(client):
    plan = _seed_with_unloading(client)
    pid = plan["plan_id"]
    assert _register(client, pid, "OP-1", "C003", 0).status_code == 201
    assert _register(client, pid, "OP-2", "C001", 1).status_code == 201
    r = _register(client, pid, "OP-3", "C002", 2)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["version"] == 3 and data["completed"] is True
    assert data["current_port"] is None
    assert data["remaining_moment"] == 0
    assert data["remaining_containers"] == [] and data["recommended_steps"] == []
    assert [s["container_no"] for s in data["done_steps"]] == \
        ["C003", "C001", "C002"]
    # 查询视图与登记返回一致
    got = client.get(f"/api/plans/{pid}/unloading-progress")
    assert got.status_code == 200 and got.json() == data


def test_query_returns_progress_view(client):
    plan = _seed_with_unloading(client)
    pid = plan["plan_id"]
    _register(client, pid, "OP-1", "C001", 0)
    got = client.get(f"/api/plans/{pid}/unloading-progress")
    assert got.status_code == 200
    data = got.json()
    assert data["version"] == 1 and data["remaining_moment"] == -10
    assert data["current_port"] == 1
    assert [s["container_no"] for s in data["recommended_steps"]] == ["C003", "C002"]


def test_query_before_first_registration_404(client):
    plan = _seed_with_unloading(client)
    r = client.get(f"/api/plans/{plan['plan_id']}/unloading-progress")
    assert r.status_code == 404 and "尚无卸箱执行进度" in r.json()["detail"]
    assert client.get("/api/plans/nope/unloading-progress").status_code == 404


# ---------- 前置条件与版本核对 ----------


def test_register_without_unloading_plan_rejected_409(client):
    plan = _seed_and_confirm(client)  # 已确认但未规划卸箱顺序
    r = _register(client, plan["plan_id"], "OP-1", "C001", 0)
    assert r.status_code == 409 and "没有当前卸箱计划" in r.json()["detail"]


def test_register_on_draft_plan_rejected_409(client):
    client.post("/api/slot-revisions", json=SLOT_PAYLOAD)
    client.post("/api/manifest-revisions", json=MANIFEST_PAYLOAD)
    plan = client.post("/api/plans/compute", json={}).json()
    r = _register(client, plan["plan_id"], "OP-1", "C001", 0)
    assert r.status_code == 409 and "尚未确认" in r.json()["detail"]


def test_register_unknown_plan_404(client):
    r = _register(client, "nope", "OP-1", "C001", 0)
    assert r.status_code == 404


def test_first_registration_requires_version_zero(client):
    plan = _seed_with_unloading(client)
    r = _register(client, plan["plan_id"], "OP-1", "C001", 1)
    assert r.status_code == 409 and "版本" in r.json()["detail"]
    # 进度未留下：再次按版本 0 登记成功
    assert _register(client, plan["plan_id"], "OP-2", "C001", 0).status_code == 201


def test_stale_expected_version_rejected_409(client):
    plan = _seed_with_unloading(client)
    pid = plan["plan_id"]
    assert _register(client, pid, "OP-1", "C001", 0).status_code == 201
    r = _register(client, pid, "OP-2", "C003", 0)  # 期望版本过期
    assert r.status_code == 409 and "过期" in r.json()["detail"]
    # 进度不变：版本仍为 1，已卸步骤不变
    data = client.get(f"/api/plans/{pid}/unloading-progress").json()
    assert data["version"] == 1
    assert [s["container_no"] for s in data["done_steps"]] == ["C001"]
    # 按正确版本登记成功
    assert _register(client, pid, "OP-2", "C003", 1).status_code == 201


def test_invalid_input_rejected_422(client):
    plan = _seed_with_unloading(client)
    pid = plan["plan_id"]
    assert _register(client, pid, "", "C001", 0).status_code == 422
    assert _register(client, pid, "  ", "C001", 0).status_code == 422
    assert _register(client, pid, "OP-1", "", 0).status_code == 422
    assert _register(client, pid, "OP-1", "C001", -1).status_code == 422


# ---------- 箱项合法性（逐港/栈顶/力矩/可续卸） ----------


def test_container_not_in_plan_rejected_422(client):
    plan = _seed_with_unloading(client)
    r = _register(client, plan["plan_id"], "OP-1", "C999", 0)
    assert r.status_code == 422 and "不在" in r.json()["detail"]
    assert client.get(f"/api/plans/{plan['plan_id']}/unloading-progress") \
        .status_code == 404  # 未留下任何进度


def test_container_of_later_port_rejected_422(client):
    plan = _seed_with_unloading(client)
    r = _register(client, plan["plan_id"], "OP-1", "C002", 0)  # C002 属港 2
    assert r.status_code == 422 and "当前港" in r.json()["detail"]
    assert client.get(f"/api/plans/{plan['plan_id']}/unloading-progress") \
        .status_code == 404


def test_container_not_on_stack_top_rejected_422(client):
    slots = {
        "slots": [
            {"code": "T1", "stack": "T", "tier": 1, "capacity": 20, "arm": 1.0},
            {"code": "T2", "stack": "T", "tier": 2, "capacity": 20, "arm": 1.0},
        ],
        "adjacency": [], "incompatible_classes": [],
        "min_moment": -100000, "max_moment": 100000,
    }
    manifest = {"containers": [
        {"no": "E1", "weight": 10, "hazmat": None, "discharge_order": 1},
        {"no": "E2", "weight": 10, "hazmat": None, "discharge_order": 1},
    ]}
    ports = {"ports": [{"discharge_order": 1, "min_remaining_moment": -100,
                        "max_remaining_moment": 100}]}
    plan = _seed_with_unloading(client, ports=ports, slots=slots,
                                manifest=manifest)
    pid = plan["plan_id"]
    # E1 在 E2 下方：先取 E1 违反堆栈先后
    r = _register(client, pid, "OP-1", "E1", 0)
    assert r.status_code == 422 and "上层" in r.json()["detail"]
    assert _register(client, pid, "OP-2", "E2", 0).status_code == 201
    assert _register(client, pid, "OP-3", "E1", 1).status_code == 201


def test_moment_outside_port_window_rejected_422(client):
    ports = {"ports": [
        {"discharge_order": 1, "min_remaining_moment": -100, "max_remaining_moment": -30},
        {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 100},
    ]}
    plan = _seed_with_unloading(client, ports=ports)
    pid = plan["plan_id"]
    # 先卸 C001 后剩余 -10，不在港 1 区间 [-100,-30] 内
    r = _register(client, pid, "OP-1", "C001", 0)
    assert r.status_code == 422 and "原定允许区间" in r.json()["detail"]
    assert client.get(f"/api/plans/{pid}/unloading-progress").status_code == 404
    # 先卸 C003（剩余 -80）合法
    assert _register(client, pid, "OP-2", "C003", 0).status_code == 201


def test_duplicate_unload_rejected_422(client):
    plan = _seed_with_unloading(client)
    pid = plan["plan_id"]
    assert _register(client, pid, "OP-1", "C001", 0).status_code == 201
    r = _register(client, pid, "OP-2", "C001", 1)
    assert r.status_code == 422 and "不得重复卸箱" in r.json()["detail"]
    data = client.get(f"/api/plans/{pid}/unloading-progress").json()
    assert data["version"] == 1 and len(data["done_steps"]) == 1


def test_infeasible_continuation_rejected_422_progress_unchanged(client):
    """单步力矩达标但剩余箱无法续卸：拒绝并说明阻碍，进度不变。"""
    # P(15,臂1)、Q(12,臂-1)、D(5,臂1) 均港 1，区间 [-10,10]；初始力矩 8。
    # 预案：P(-7)->Q(5)->D(0)。若先卸 D：剩余 3 ∈ [-10,10]，
    # 但 P 卸后 -12、Q 卸后 15 均越界 -> 无可行后续。
    slots = {
        "slots": [
            {"code": "S1", "stack": "S1", "tier": 1, "capacity": 15, "arm": 1.0},
            {"code": "S2", "stack": "S2", "tier": 1, "capacity": 12, "arm": -1.0},
            {"code": "S3", "stack": "S3", "tier": 1, "capacity": 5, "arm": 1.0},
        ],
        "adjacency": [], "incompatible_classes": [],
        "min_moment": -100000, "max_moment": 100000,
    }
    manifest = {"containers": [
        {"no": "P", "weight": 15, "hazmat": None, "discharge_order": 1},
        {"no": "Q", "weight": 12, "hazmat": None, "discharge_order": 1},
        {"no": "D", "weight": 5, "hazmat": None, "discharge_order": 1},
    ]}
    ports = {"ports": [{"discharge_order": 1, "min_remaining_moment": -10,
                        "max_remaining_moment": 10}]}
    plan = _seed_with_unloading(client, ports=ports, slots=slots,
                                manifest=manifest)
    pid = plan["plan_id"]
    planned = [s["container_no"] for s in
               client.get(f"/api/plans/{pid}/unloading").json()["steps"]]
    assert planned == ["P", "Q", "D"]

    r = _register(client, pid, "OP-1", "D", 0)
    assert r.status_code == 422, r.text
    assert "无法" in r.json()["detail"] and "卸完全部剩余箱" in r.json()["detail"]
    # 进度不变：无任何已卸步骤
    assert client.get(f"/api/plans/{pid}/unloading-progress").status_code == 404
    # 按预案顺序仍可正常执行到底
    assert _register(client, pid, "OP-2", "P", 0).status_code == 201
    assert _register(client, pid, "OP-3", "Q", 1).status_code == 201
    r = _register(client, pid, "OP-4", "D", 2)
    assert r.status_code == 201 and r.json()["completed"] is True


# ---------- 幂等：同操作号 ----------


def test_same_operation_same_content_retry_returns_first_result(client):
    plan = _seed_with_unloading(client)
    pid = plan["plan_id"]
    r1 = _register(client, pid, "OP-1", "C003", 0)
    assert r1.status_code == 201
    # 推进后续步骤后再重试：仍返回首次结果快照（version=1）
    assert _register(client, pid, "OP-2", "C001", 1).status_code == 201
    r2 = _register(client, pid, "OP-1", "C003", 0)
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    assert r2.json()["version"] == 1
    # 进度未重复生效
    data = client.get(f"/api/plans/{pid}/unloading-progress").json()
    assert data["version"] == 2 and len(data["done_steps"]) == 2


def test_same_operation_different_content_conflict_409(client):
    plan = _seed_with_unloading(client)
    pid = plan["plan_id"]
    assert _register(client, pid, "OP-1", "C003", 0).status_code == 201
    # 同号异箱
    r1 = _register(client, pid, "OP-1", "C001", 0)
    assert r1.status_code == 409 and "异内容冲突" in r1.json()["detail"]
    # 同号异期望版本
    r2 = _register(client, pid, "OP-1", "C003", 1)
    assert r2.status_code == 409 and "异内容冲突" in r2.json()["detail"]
    data = client.get(f"/api/plans/{pid}/unloading-progress").json()
    assert data["version"] == 1


def test_operation_no_globally_unique_across_plans(client):
    p1 = _seed_with_unloading(client)
    # 第二套槽位（编号不同，避免已确认槽位占用冲突）上的另一已确认方案
    slots2 = {
        "slots": [
            {"code": "D01", "stack": "D", "tier": 1, "capacity": 40, "arm": -2.0},
            {"code": "D02", "stack": "D", "tier": 2, "capacity": 15, "arm": -2.0},
            {"code": "E01", "stack": "E", "tier": 1, "capacity": 15, "arm": 5.0},
        ],
        "adjacency": [], "incompatible_classes": [],
        "min_moment": -100000, "max_moment": 100000,
    }
    p2 = _seed_with_unloading(client, slots=slots2)
    assert _register(client, p1["plan_id"], "OP-G", "C001", 0).status_code == 201
    # 同一操作号用于另一方案 -> 冲突
    r = _register(client, p2["plan_id"], "OP-G", "C001", 0)
    assert r.status_code == 409 and "另一方案" in r.json()["detail"]


# ---------- 冻结：重排原计划不改已卸记录与后续核算 ----------


def test_replan_after_first_registration_keeps_frozen_windows(client):
    """首次登记冻结港区间；此后重排原计划，后续核算仍按冻结区间。"""
    # 港1：A(10,臂1)、B(20,臂1)、C(5,臂-1)；港2：D(25,臂-1)。初始力矩 0。
    slots = {
        "slots": [
            {"code": "SA", "stack": "SA", "tier": 1, "capacity": 10, "arm": 1.0},
            {"code": "SB", "stack": "SB", "tier": 1, "capacity": 20, "arm": 1.0},
            {"code": "SC", "stack": "SC", "tier": 1, "capacity": 5, "arm": -1.0},
            {"code": "SD", "stack": "SD", "tier": 1, "capacity": 25, "arm": -1.0},
        ],
        "adjacency": [], "incompatible_classes": [],
        "min_moment": -100000, "max_moment": 100000,
    }
    manifest = {"containers": [
        {"no": "A", "weight": 10, "hazmat": None, "discharge_order": 1},
        {"no": "B", "weight": 20, "hazmat": None, "discharge_order": 1},
        {"no": "C", "weight": 5, "hazmat": None, "discharge_order": 1},
        {"no": "D", "weight": 25, "hazmat": None, "discharge_order": 2},
    ]}
    ports = {"ports": [
        {"discharge_order": 1, "min_remaining_moment": -30, "max_remaining_moment": 10},
        {"discharge_order": 2, "min_remaining_moment": -10, "max_remaining_moment": 10},
    ]}
    plan = _seed_with_unloading(client, ports=ports, slots=slots,
                                manifest=manifest)
    pid = plan["plan_id"]
    # 首次登记：偏离预案先卸 B（剩余 -20 ∈ [-30,10]），冻结区间 [-30,10]
    assert _register(client, pid, "OP-1", "B", 0).status_code == 201

    # 重排原计划：新区间 [-28,10]（不含 -30），原子替换卸箱顺序
    new_ports = {"ports": [
        {"discharge_order": 1, "min_remaining_moment": -28, "max_remaining_moment": 10},
        {"discharge_order": 2, "min_remaining_moment": -10, "max_remaining_moment": 10},
    ]}
    r = client.post(f"/api/plans/{pid}/unloading", json=new_ports)
    assert r.status_code == 201, r.text

    # 后续核算仍按冻结区间：卸 A 后剩余 -30 ∈ 冻结 [-30,10]（新区间 [-28,10] 不含）
    r2 = _register(client, pid, "OP-2", "A", 1)
    assert r2.status_code == 201, r2.text
    data = r2.json()
    assert data["remaining_moment"] == -30
    # 已卸记录不改、进度视图仍展示冻结区间与初始力矩
    assert [s["container_no"] for s in data["done_steps"]] == ["B", "A"]
    assert data["initial_moment"] == 0
    assert [(p["discharge_order"], p["min_remaining_moment"],
             p["max_remaining_moment"]) for p in data["ports"]] == [
        (1, -30, 10), (2, -10, 10)
    ]
    # 剩余推荐同样按冻结区间：C -> D
    assert [s["container_no"] for s in data["recommended_steps"]] == ["C", "D"]
    # 查询视图一致
    assert client.get(f"/api/plans/{pid}/unloading-progress").json() == data


# ---------- 方案被取代后只读 ----------


def test_superseded_plan_progress_read_only(client):
    plan = _seed_with_unloading(client)
    pid = plan["plan_id"]
    assert _register(client, pid, "OP-1", "C001", 0).status_code == 201

    # 新箱清单修订 -> 修订并确认新方案，旧方案被取代
    client.post("/api/manifest-revisions", json={
        "containers": [
            {"no": "C001", "weight": 15, "hazmat": None, "discharge_order": 1}
        ]
    })
    p2 = client.post(f"/api/plans/{pid}/amend", json={}).json()
    ok = client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": 1, "manifest_revision": 2
    })
    assert ok.status_code == 200

    # 只读：再登记返回 409
    r = _register(client, pid, "OP-2", "C003", 1)
    assert r.status_code == 409 and "只读" in r.json()["detail"]
    # 历史进度仍可查
    got = client.get(f"/api/plans/{pid}/unloading-progress")
    assert got.status_code == 200
    data = got.json()
    assert data["plan_status"] == "superseded" and data["read_only"] is True
    assert data["version"] == 1
    assert [s["container_no"] for s in data["done_steps"]] == ["C001"]


# ---------- 并发登记 ----------


def test_concurrent_registration_same_expected_version_serializes(client):
    """两个线程以同一期望版本并发登记不同箱：恰一个成功，另一个版本冲突。"""
    plan = _seed_with_unloading(client)
    pid = plan["plan_id"]
    results = {}

    def register(op, cno, key):
        local = TestClient(app)
        r = local.post(f"/api/plans/{pid}/unloading-progress", json={
            "operation_no": op, "container_no": cno, "expected_version": 0,
        })
        results[key] = r.status_code

    t1 = threading.Thread(target=register, args=("OP-A", "C001", "a"))
    t2 = threading.Thread(target=register, args=("OP-B", "C003", "b"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results.values()) == [201, 409]
    data = client.get(f"/api/plans/{pid}/unloading-progress").json()
    # 只留下成功一步，无部分进度
    assert data["version"] == 1 and len(data["done_steps"]) == 1


def test_concurrent_same_operation_same_content_one_wins(client):
    """同操作号同内容并发重试：一个 201、一个 200，结果一致。"""
    plan = _seed_with_unloading(client)
    pid = plan["plan_id"]
    results = []

    def register():
        local = TestClient(app)
        r = local.post(f"/api/plans/{pid}/unloading-progress", json={
            "operation_no": "OP-X", "container_no": "C001", "expected_version": 0,
        })
        results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=register) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(code for code, _ in results) == [200, 201]
    assert results[0][1] == results[1][1]
    data = client.get(f"/api/plans/{pid}/unloading-progress").json()
    assert data["version"] == 1 and len(data["done_steps"]) == 1
