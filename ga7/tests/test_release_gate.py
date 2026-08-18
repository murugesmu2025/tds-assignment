from ga7.app.main import app


import asyncio
from httpx import AsyncClient, ASGITransport


def post(path, json):
    async def _post():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            return await ac.post(path, json=json)

    return asyncio.run(_post())


def make_base_payload():
    return {
        "target": "preview",
        "event": "pull_request",
        "ref": "refs/heads/feature/x",
        "workflow": {
            "trigger": "pull_request",
            "permissions": {"contents": "read", "packages": "write", "id-token": "none"},
            "testsPassed": True,
            "matrixComplete": True,
            "failFast": False,
            "actions": [{"owner": "actions", "name": "checkout", "ref": "v4"}],
        },
        "image": {
            "multiStage": True,
            "runsAsRoot": False,
            "secretMode": "none",
            "criticalVulnerabilities": 0,
            "digestPinned": True,
        },
    }


def test_promote_preview_ok():
    payload = make_base_payload()
    r = post("/release-gate", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["decision"] == "promote"
    assert data["violations"] == []


def test_block_mutable_action():
    payload = make_base_payload()
    payload["workflow"]["actions"] = [{"owner": "thirdparty", "name": "do", "ref": "v2"}]
    r = post("/release-gate", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["decision"] == "block"
    assert "MUTABLE_ACTION" in data["violations"]


def test_production_requires_approval_and_main_ref():
    payload = make_base_payload()
    payload["target"] = "production"
    payload["event"] = "push"
    payload["ref"] = "refs/heads/main"
    payload["workflow"]["environmentApproval"] = True
    r = post("/release-gate", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["decision"] == "promote"


def base_workflow(actions=None):
    return {
        "trigger": "push",
        "permissions": {"contents": "read", "packages": "write", "id-token": "none"},
        "testsPassed": True,
        "matrixComplete": True,
        "failFast": False,
        "actions": actions or [{"owner": "actions", "name": "checkout", "ref": "v4"}],
    }


def base_image():
    return {"multiStage": True, "runsAsRoot": False, "secretMode": "none", "criticalVulnerabilities": 0, "digestPinned": True}


def test_promote_preview_push():
    payload = {
        "target": "preview",
        "event": "push",
        "ref": "refs/heads/feature/x",
        "workflow": base_workflow(),
        "image": base_image(),
    }
    r = post("/release-gate", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["decision"] == "promote"
    assert data["violations"] == []


def test_block_pr_unsafe_trigger_and_tests():
    wf = base_workflow()
    wf["trigger"] = "pull_request_target"
    wf["testsPassed"] = False
    payload = {
        "target": "preview",
        "event": "pull_request",
        "ref": "refs/heads/feature/x",
        "workflow": wf,
        "image": base_image(),
    }
    r = post("/release-gate", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["decision"] == "block"
    assert set(data["violations"]) >= {"UNSAFE_PR_TRIGGER", "TESTS_INCOMPLETE"}


def test_block_mutable_action_and_image_issues():
    wf = base_workflow(actions=[{"owner": "thirdparty", "name": "cool", "ref": "v1"}])
    img = {"multiStage": False, "runsAsRoot": True, "secretMode": "arg", "criticalVulnerabilities": 2, "digestPinned": False}
    payload = {
        "target": "preview",
        "event": "push",
        "ref": "refs/heads/feature/x",
        "workflow": wf,
        "image": img,
    }
    r = post("/release-gate", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["decision"] == "block"
    # expect multiple violation codes
    assert set(data["violations"]) >= {"MUTABLE_ACTION", "SINGLE_STAGE_IMAGE", "ROOT_RUNTIME", "SECRET_IN_LAYER", "CRITICAL_CVE", "UNPINNED_IMAGE"}


def test_production_requires_main_and_approval():
    wf = base_workflow()
    wf["environmentApproval"] = False
    payload = {
        "target": "production",
        "event": "push",
        "ref": "refs/heads/feature/x",
        "workflow": wf,
        "image": base_image(),
    }
    r = post("/release-gate", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["decision"] == "block"
    assert set(data["violations"]) >= {"INVALID_PRODUCTION_REF", "APPROVAL_REQUIRED"}
