"""Integration test for Flask app endpoints."""
import json
import pytest
from app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_index_route(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"ContinuAI" in res.data


def test_analyze_demo_and_api_flow(client):
    # 1. Trigger analyze on demo source
    res = client.post("/api/analyze", json={"source": "demo", "departing": "Priya Nair"})
    assert res.status_code == 200
    data = res.get_json()
    assert "id" in data
    hid = data["id"]

    # 2. Get handover data
    res = client.get(f"/api/h/{hid}")
    assert res.status_code == 200
    h_data = res.get_json()
    assert h_data["status"] == "draft"
    items = h_data["data"]["items"]
    assert len(items) > 0

    # 3. Check item confidence format
    for item in items:
        assert item["confidence"] in ("confirmed", "likely", "inferred")
        assert 0.0 <= item["confidence_score"] <= 1.0

    # 4. Approve an item
    first_item_id = items[0]["id"]
    res = client.post(f"/api/h/{hid}/item", json={
        "item_id": first_item_id,
        "action": "approve",
        "actor": "Lead Reviewer"
    })
    assert res.status_code == 200

    # 5. Publish handover
    res = client.post(f"/api/h/{hid}/publish", json={"actor": "Lead Reviewer"})
    assert res.status_code == 200
    assert res.get_json().get("ok") is True

    # 6. View handover by role
    res = client.get(f"/api/h/{hid}/view?role=backend")
    assert res.status_code == 200
    view_data = res.get_json()
    assert "items" in view_data
    assert "graph" in view_data

    # 7. Acknowledge receipt
    res = client.post(f"/api/h/{hid}/ack", json={"name": "New Backend Engineer", "role": "backend"})
    assert res.status_code == 200
