import time

from fastapi.testclient import TestClient

from spark_freeze.app import create_app
from spark_freeze.runtime import Runtime


def test_demo_controls_and_report(tmp_path):
    runtime = Runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        limit = time.monotonic() + 3
        while not client.get("/api/state").json()["camera"]["ready"] and time.monotonic() < limit:
            time.sleep(.05)
        assert client.get("/api/frame.jpg").headers["content-type"] == "image/jpeg"
        assert client.post("/api/config", json={"mode": "demo", "players": 2}).status_code == 200
        time.sleep(.2)
        assert client.post("/api/control", json={"action": "start"}).status_code == 200
        assert client.post("/api/config", json={"players": 3}).status_code == 409
        assert client.post("/api/control", json={"action": "demo_move", "player_id": 1}).status_code == 409
        with runtime.lock:
            runtime.engine.transition("red", 10)
        assert client.post("/api/control", json={"action": "demo_move", "player_id": 1}).status_code == 200
        time.sleep(1)
        state = client.get("/api/state").json()
        assert len(state["evidence"]) == 1
        record = state["evidence"][0]
        assert client.get(record["before_url"]).status_code == 200
        assert client.get(record["after_url"]).status_code == 200
        review = client.post(f"/api/evidence/{record['id']}/review", json={"decision": "confirm"})
        assert review.json()["players"][0]["status"] == "eliminated"
        report = client.get("/api/report")
        assert report.json()["evidence"][0]["status"] == "confirmed"
        assert "attachment" in report.headers["content-disposition"]
        assert client.post("/api/control", json={"action": "estop"}).json()["phase"] == "estop"
        assert client.post("/api/control", json={"action": "resume"}).status_code == 409


def test_tokens_and_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_CONTROL_TOKEN", "secret-local-controller")
    runtime = Runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.post("/api/control", json={"action": "estop"}).status_code == 401
        assert client.post("/api/operator/heartbeat", headers={"X-Control-Token": "secret-local-controller"}).status_code == 200
        assert client.post("/api/robot/tick", json={"yaw": 0, "odom_age": 0, "scan_age": 0}).status_code == 401
        assert client.post("/api/config", json={"players": 12}, headers={"X-Control-Token": "secret-local-controller"}).status_code == 422
        assert client.get("/api/evidence/bad/bad/before.jpg").status_code == 404


def test_camera_loss_pauses_webcam_game(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.engine.mode = "webcam"
    runtime.engine.camera["ready"] = True
    runtime.engine.players[0]["visible"] = True
    runtime.engine.start()
    # Exercise watchdog independently of camera hardware/model loading.
    import threading
    thread = threading.Thread(target=runtime._clock_loop)
    thread.start()
    time.sleep(.15)
    runtime.stop_event.set()
    thread.join()
    assert runtime.engine.phase == "paused"
    assert runtime.engine.camera["ready"] is False


def test_old_inference_cannot_enter_a_new_red_phase(tmp_path):
    import numpy as np
    runtime = Runtime(tmp_path)
    old_epoch = runtime.engine.baseline_epoch
    runtime.engine.transition("red", 10)
    runtime._publish(np.zeros((100, 100, 3), dtype=np.uint8),
                     [dict(player_id=1, visible=True, confidence=1, motion=1, ambiguous=False)],
                     True, runtime.generation, {}, expected_epoch=old_epoch)
    assert runtime.engine.players[0]["visible"] is False
    assert runtime.engine.streaks == {}
