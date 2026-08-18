from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Dict, Optional

app = FastAPI()


class ActionSpec(BaseModel):
    owner: str
    name: str
    ref: str


class WorkflowSpec(BaseModel):
    trigger: str
    permissions: Dict[str, str]
    testsPassed: bool
    matrixComplete: bool
    failFast: bool
    actions: List[ActionSpec]
    environmentApproval: Optional[bool] = False


class ImageSpec(BaseModel):
    multiStage: bool
    runsAsRoot: bool
    secretMode: str
    criticalVulnerabilities: int
    digestPinned: bool


class ReleaseRequest(BaseModel):
    target: str
    event: str
    ref: str
    workflow: WorkflowSpec
    image: ImageSpec


@app.post("/release-gate")
def release_gate(req: ReleaseRequest):
    v = []

    # Permissions exact-match check
    expected_perms = {"contents": "read", "packages": "write", "id-token": "none"}
    if req.workflow.permissions != expected_perms:
        v.append("EXCESS_PERMISSION")

    # Pull request rules
    if req.event == "pull_request":
        if req.workflow.trigger != "pull_request":
            v.append("UNSAFE_PR_TRIGGER")

    # Tests, matrix, failFast
    if not (req.workflow.testsPassed and req.workflow.matrixComplete and req.workflow.failFast is False):
        v.append("TESTS_INCOMPLETE")

    # Actions pinning
    import re

    for a in req.workflow.actions:
        if a.owner == "actions":
            # allow version tags for actions/ owned actions
            continue
        # third-party action must be pinned to 40-char lowercase hex
        if not re.fullmatch(r"[0-9a-f]{40}", a.ref):
            v.append("MUTABLE_ACTION")
            break

    # Image rules
    if not req.image.multiStage:
        v.append("SINGLE_STAGE_IMAGE")
    if req.image.runsAsRoot:
        v.append("ROOT_RUNTIME")
    if req.image.secretMode not in ("none", "buildkit"):
        v.append("SECRET_IN_LAYER")
    if req.image.criticalVulnerabilities and req.image.criticalVulnerabilities > 0:
        v.append("CRITICAL_CVE")
    if not req.image.digestPinned:
        v.append("UNPINNED_IMAGE")

    # Production-specific checks
    if req.target == "production":
        if not (req.event == "push" and req.ref == "refs/heads/main"):
            v.append("INVALID_PRODUCTION_REF")
        if not req.workflow.environmentApproval:
            v.append("APPROVAL_REQUIRED")

    decision = "promote" if len(v) == 0 else "block"
    # unique violation codes
    violations = list(dict.fromkeys(v))
    return {"decision": decision, "violations": violations}
