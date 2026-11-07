"""画布 schema 回归：连线形状 / 控制点 / 画布默认曲直必须能通过 PUT 校验并原样落盘。

背景：update_canvas 使用 CanvasIn.model_dump() 后 cur.update(d)。
Pydantic 默认丢弃未声明字段，因此这里锁住新增字段，防止后续重构把它们静默剥掉。
"""
from api.canvases import CanvasIn


def _base_payload(**overrides):
    payload = {
        "name": "测试画布",
        "nodes": [
            {"id": 1, "agent_id": 10, "x": 0, "y": 0},
            {"id": 2, "agent_id": 11, "x": 200, "y": 120},
        ],
        "edges": [
            {"id": 1, "from_node": 1, "to_node": 2, "type": "chat"},
        ],
    }
    payload.update(overrides)
    return payload


def test_edge_shape_and_waypoints_survive_model_dump():
    payload = _base_payload(
        edge_shape="straight",
        edges=[
            {
                "id": 1,
                "from_node": 1,
                "to_node": 2,
                "type": "chat",
                "shape": "straight",
                "waypoints": [{"x": 120, "y": 60}, {"x": 160, "y": 40}],
            }
        ],
    )
    dumped = CanvasIn(**payload).model_dump()

    assert dumped["edge_shape"] == "straight"
    assert dumped["edges"][0]["shape"] == "straight"
    assert dumped["edges"][0]["waypoints"] == [
        {"x": 120.0, "y": 60.0},
        {"x": 160.0, "y": 40.0},
    ]


def test_legacy_canvas_gets_defaults():
    """旧数据没有新字段：校验通过，并补上曲线默认值。"""
    dumped = CanvasIn(**_base_payload()).model_dump()

    assert dumped["edge_shape"] == "curve"
    assert dumped["edges"][0]["shape"] == "curve"
    assert dumped["edges"][0]["waypoints"] == []


def test_curve_without_waypoints_is_stable():
    dumped = CanvasIn(**_base_payload(edge_shape="curve")).model_dump()

    assert dumped["edges"][0]["shape"] == "curve"
    assert dumped["edges"][0]["waypoints"] == []
