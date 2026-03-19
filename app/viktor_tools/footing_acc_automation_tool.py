"""Start and poll the footing ACC automation."""

import json
import logging
import os
import time
import uuid
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from pydantic import BaseModel, TypeAdapter

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"success", "failedUpload", "cancelled"}
FOOTING_ACC_WORKITEM_STORAGE_KEY = "last_footing_acc_workitem"
FOOTING_SIZING_STORAGE_KEY = "footing_sizing_results"
INPUT_MODEL_PARAMETER_NAME = "inputModel"
INPUT_MODEL_LOCAL_NAME = "input.rvt"
OUTPUT_MODEL_PARAMETER_NAME = "resultModel"
OUTPUT_MODEL_LOCAL_NAME = "result.rvt"
DEFAULT_OUTPUT_FILE_NAME = "footing_output.rvt"


class RunFootingAccAutomationArgs(BaseModel):
    """No user input is required for the footing ACC automation."""


class PollFootingAccAutomationArgs(BaseModel):
    """No user input is required when polling the latest footing ACC workitem."""


class GetLastFootingAccWorkitemArgs(BaseModel):
    """No user input is required when reading the latest footing ACC workitem."""


class FootingStorageEntry(BaseModel):
    """Storage shape for footing entries."""

    node_id: str | None = None
    B: float
    L: float
    x: float
    y: float
    z: float
    acting_bearing_pressure: float | None = None


class FootingAddinEntry(BaseModel):
    """Shape expected by the Autodesk add-in."""

    B: float
    L: float
    x: float
    y: float
    z: float


class FootingAccWorkitemState(BaseModel):
    """Stored metadata for the active ACC footing workitem."""

    workitem_id: str
    project_id: str
    input_item_urn: str
    output_folder_id: str
    output_file_name: str
    started_at_epoch: int
    footing_count: int
    last_status: str | None = None
    last_report_url: str | None = None


def require_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _get_selected_autodesk_file(ctx: Any) -> Any:
    run_context = getattr(ctx, "context", None)
    autodesk_file = getattr(run_context, "autodesk_file", None)
    if autodesk_file is None:
        raise ValueError("Select an Autodesk model first.")
    return autodesk_file


def _get_storage_text(vkt: Any, key: str) -> str:
    stored_file = vkt.Storage().get(key, scope="entity")
    if not stored_file:
        raise ValueError(f"Missing Viktor Storage key '{key}'.")
    return stored_file.getvalue_binary().decode("utf-8")


def _load_footing_entries_from_storage(vkt: Any) -> list[FootingAddinEntry]:
    raw = _get_storage_text(vkt, FOOTING_SIZING_STORAGE_KEY)
    entries = TypeAdapter(list[FootingStorageEntry]).validate_json(raw)
    normalized = [
        FootingAddinEntry(B=entry.B, L=entry.L, x=entry.x, y=entry.y, z=entry.z)
        for entry in entries
    ]
    if not normalized:
        raise ValueError(
            f"Storage key '{FOOTING_SIZING_STORAGE_KEY}' does not contain any footing entries."
        )
    return normalized


def _build_unique_output_file_name(file_name: str) -> str:
    path = Path(file_name)
    suffix = path.suffix or ".rvt"
    return f"{path.stem}_{uuid.uuid4().hex[:5]}{suffix}"


def _delete_workitem_state(vkt: Any) -> None:
    try:
        vkt.Storage().delete(FOOTING_ACC_WORKITEM_STORAGE_KEY, scope="entity")
    except Exception:
        pass


def _write_workitem_state(vkt: Any, state: FootingAccWorkitemState) -> None:
    vkt.Storage().set(
        FOOTING_ACC_WORKITEM_STORAGE_KEY,
        data=vkt.File.from_data(state.model_dump_json(indent=2)),
        scope="entity",
    )


def _read_workitem_state(vkt: Any) -> FootingAccWorkitemState | None:
    stored_file = vkt.Storage().get(FOOTING_ACC_WORKITEM_STORAGE_KEY, scope="entity")
    if not stored_file:
        return None
    raw = stored_file.getvalue_binary().decode("utf-8")
    return FootingAccWorkitemState.model_validate_json(raw)


def _elapsed_seconds(started_at_epoch: int) -> int:
    return max(0, int(time.time()) - started_at_epoch)


def _build_status_response(
    state: FootingAccWorkitemState,
    *,
    status: str,
    done: bool,
    report_url: str | None = None,
    output_item_urn: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "workitem_id": state.workitem_id,
        "status": status,
        "elapsed_seconds": _elapsed_seconds(state.started_at_epoch),
        "report_url": report_url,
        "done": done,
        "output_file_name": state.output_file_name,
        "footing_count": state.footing_count,
    }
    if output_item_urn:
        payload["output_item_urn"] = output_item_urn
    return json.dumps(payload, indent=2)


def _finalize_footing_acc_workitem(state: FootingAccWorkitemState) -> str | None:
    from aps_automation_sdk import ActivityOutputParameterAcc

    from app.aec import APS_AUTOMATION_OAUTH_INTEGRATION, get_token

    token3lo = get_token(APS_AUTOMATION_OAUTH_INTEGRATION)
    output_acc = ActivityOutputParameterAcc(
        name=OUTPUT_MODEL_PARAMETER_NAME,
        localName=OUTPUT_MODEL_LOCAL_NAME,
        verb="put",
        description="Footing automation output model",
        project_id=state.project_id,
        folder_id=state.output_folder_id,
        file_name=state.output_file_name,
    )

    created_item = output_acc.create_acc_item(token3lo)
    output_item_urn = ((created_item or {}).get("data") or {}).get("id")
    print(f"ACC lineage URN: {output_item_urn}")
    return output_item_urn


async def run_footing_acc_automation_func(ctx: Any, args: str) -> str:
    try:
        import viktor as vkt
        from aps_automation_sdk import (
            ActivityInputParameterAcc,
            ActivityOutputParameterAcc,
            WorkItemAcc,
        )
        from aps_automation_sdk.classes import UploadActivityInputParameter

        from app.aec import get_acc_automation_context
    except ImportError as e:
        return f"Error importing required modules: {e}."

    RunFootingAccAutomationArgs.model_validate_json(args or "{}")

    try:
        footings = _load_footing_entries_from_storage(vkt)
        footing_payload = [entry.model_dump(mode="json") for entry in footings]

        autodesk_file = _get_selected_autodesk_file(ctx)
        acc_context = get_acc_automation_context(autodesk_file)
        activity_full_alias = require_env("APS_ACTIVITY_FOOTING_FULL_ALIAS")
        activity_signature = require_env("APS_ACTIVITY_FOOTING_SIGNATURE")
        output_file_name = _build_unique_output_file_name(DEFAULT_OUTPUT_FILE_NAME)

        input_acc = ActivityInputParameterAcc(
            name=INPUT_MODEL_PARAMETER_NAME,
            localName=INPUT_MODEL_LOCAL_NAME,
            verb="get",
            description="Input Revit model from ACC",
            required=True,
            is_engine_input=True,
            project_id=acc_context.project_id,
            linage_urn=acc_context.input_item_urn,
        )

        with NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump(footing_payload, tmp, separators=(",", ":"))
            footing_payload_path = tmp.name

        try:
            footings_json = UploadActivityInputParameter(
                name="footingPayload",
                folder_id=acc_context.output_folder_id,
                project_id=acc_context.project_id,
                localName="pad_foundations.json",
                file_name="pad_foundations.json",
                file_path=footing_payload_path,
                verb="get",
                description="Pad foundations JSON payload",
                required=True,
            )
            output_acc = ActivityOutputParameterAcc(
                name=OUTPUT_MODEL_PARAMETER_NAME,
                localName=OUTPUT_MODEL_LOCAL_NAME,
                verb="put",
                description="Footing automation output model",
                project_id=acc_context.project_id,
                folder_id=acc_context.output_folder_id,
                file_name=output_file_name,
            )

            workitem = WorkItemAcc(
                parameters=[input_acc, footings_json, output_acc],
                activity_full_alias=activity_full_alias,
            )

            print(
                "Submitting footing ACC work item "
                f"for project_id={acc_context.project_id} item_urn={acc_context.input_item_urn} "
                f"with {len(footing_payload)} footing entries"
            )

            workitem_id = workitem.run_public_activity(
                token3lo=acc_context.token3lo,
                activity_signature=activity_signature,
            )
            print(f"Work item created: {workitem_id}")
        finally:
            try:
                os.unlink(footing_payload_path)
            except OSError:
                pass

        _delete_workitem_state(vkt)
        state = FootingAccWorkitemState(
            workitem_id=workitem_id,
            project_id=acc_context.project_id,
            input_item_urn=acc_context.input_item_urn,
            output_folder_id=acc_context.output_folder_id,
            output_file_name=output_file_name,
            started_at_epoch=int(time.time()),
            footing_count=len(footing_payload),
            last_status="submitted",
        )
        _write_workitem_state(vkt, state)

        return json.dumps(
            {
                "workitem_id": workitem_id,
                "status": "submitted",
                "elapsed_seconds": 0,
                "done": False,
                "output_file_name": output_file_name,
                "footing_count": len(footing_payload),
            },
            indent=2,
        )

    except Exception as e:
        logger.exception("Unexpected error in run_footing_acc_automation_func")
        return f"Error starting footing ACC automation: {type(e).__name__}: {e}"


async def poll_footing_acc_automation_func(_ctx: Any, args: str) -> str:
    try:
        import viktor as vkt
        from aps_automation_sdk.core import get_workitem_status

        from app.aec import APS_AUTOMATION_OAUTH_INTEGRATION, get_token
    except ImportError as e:
        return f"Error importing required modules: {e}."

    PollFootingAccAutomationArgs.model_validate_json(args or "{}")

    try:
        state = _read_workitem_state(vkt)
        if state is None:
            return "No active footing ACC workitem found in Viktor Storage."

        token3lo = get_token(APS_AUTOMATION_OAUTH_INTEGRATION)
        status_payload = get_workitem_status(state.workitem_id, token3lo)
        status = status_payload.get("status", "unknown")
        report_url = status_payload.get("reportUrl")
        print(
            f"[{_elapsed_seconds(state.started_at_epoch):>3}s] "
            f"status={status} report_url={report_url}"
        )

        if status == "success":
            output_item_urn = _finalize_footing_acc_workitem(state)
            _delete_workitem_state(vkt)
            return _build_status_response(
                state,
                status=status,
                done=True,
                report_url=report_url,
                output_item_urn=output_item_urn,
            )

        if status in TERMINAL_STATUSES:
            _delete_workitem_state(vkt)
            return _build_status_response(
                state,
                status=status,
                done=True,
                report_url=report_url,
            )

        state.last_status = status
        state.last_report_url = report_url
        _write_workitem_state(vkt, state)
        return _build_status_response(
            state,
            status=status,
            done=False,
            report_url=report_url,
        )

    except Exception as e:
        logger.exception("Unexpected error while polling footing ACC automation")
        return f"Error polling footing ACC automation: {type(e).__name__}: {e}"


async def get_last_footing_acc_workitem_func(_ctx: Any, args: str) -> str:
    try:
        import viktor as vkt
    except ImportError as e:
        return f"Error importing required modules: {e}."

    GetLastFootingAccWorkitemArgs.model_validate_json(args or "{}")

    try:
        state = _read_workitem_state(vkt)
        if state is None:
            return "No active footing ACC workitem found in Viktor Storage."

        return json.dumps(
            {
                "workitem_id": state.workitem_id,
                "status": state.last_status or "submitted",
                "elapsed_seconds": _elapsed_seconds(state.started_at_epoch),
                "report_url": state.last_report_url,
                "output_file_name": state.output_file_name,
                "footing_count": state.footing_count,
            },
            indent=2,
        )

    except Exception as e:
        logger.exception("Unexpected error while reading footing ACC workitem")
        return f"Error reading footing ACC workitem: {type(e).__name__}: {e}"


def run_footing_acc_automation_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="run_footing_acc_automation",
        description=(
            "Start the ACC footing automation for the selected Autodesk model. "
            "Reads footing sizing data from Viktor Storage key 'footing_sizing_results', "
            "submits the workitem, and stores it as the last footing ACC workitem in Viktor Storage. "
            "After submission, use poll_footing_acc_automation to track status and finalize the ACC output item. "
            "Requires APS_ACTIVITY_FOOTING_FULL_ALIAS and APS_ACTIVITY_FOOTING_SIGNATURE environment variables."
        ),
        params_json_schema=RunFootingAccAutomationArgs.model_json_schema(),
        on_invoke_tool=run_footing_acc_automation_func,
    )


def poll_footing_acc_automation_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="poll_footing_acc_automation",
        description=(
            "Poll the latest footing ACC workitem stored in Viktor Storage. "
            "Returns the current status and elapsed time. On success, it finalizes the ACC output item "
            "and clears the stored workitem."
        ),
        params_json_schema=PollFootingAccAutomationArgs.model_json_schema(),
        on_invoke_tool=poll_footing_acc_automation_func,
    )


def get_last_footing_acc_workitem_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="get_last_footing_acc_workitem",
        description=(
            "Read the latest footing ACC workitem metadata stored in Viktor Storage without polling Autodesk."
        ),
        params_json_schema=GetLastFootingAccWorkitemArgs.model_json_schema(),
        on_invoke_tool=get_last_footing_acc_workitem_func,
    )
