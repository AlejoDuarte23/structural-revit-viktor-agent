"""Start and poll ACC analytical model automation runs."""

import json
import logging
import os
import time
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ANALYTICAL_MODEL_STORAGE_KEY = "acc_analytical_model_json"
ANALYTICAL_MODEL_WORKITEM_STORAGE_KEY = "last_analytical_model_json_workitem"
TERMINAL_STATUSES = {"success", "failedUpload", "cancelled"}


class ExtractAnalyticalModelJsonArgs(BaseModel):
    """Arguments for starting the analytical model ACC automation."""

    output_file_name: str = Field(
        default="analytical_model.json",
        description="Output file name to create in ACC.",
    )
    storage_key: str = Field(
        default=ANALYTICAL_MODEL_STORAGE_KEY,
        description="Viktor Storage key where the downloaded JSON should be stored after success.",
    )


class PollAnalyticalModelJsonArgs(BaseModel):
    """No user input is required when polling the latest analytical model ACC workitem."""


class GetLastAnalyticalModelJsonWorkitemArgs(BaseModel):
    """No user input is required when reading the latest analytical model ACC workitem."""


class AnalyticalModelWorkitemState(BaseModel):
    """Stored metadata for the active ACC analytical model workitem."""

    workitem_id: str
    project_id: str
    input_item_urn: str
    output_folder_id: str
    output_file_name: str
    storage_key: str
    started_at_epoch: int
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


def _build_unique_output_file_name(file_name: str) -> str:
    path = Path(file_name)
    suffix = path.suffix or ".json"
    return f"{path.stem}_{uuid.uuid4().hex[:5]}{suffix}"


def _delete_workitem_state(vkt: Any) -> None:
    try:
        vkt.Storage().delete(ANALYTICAL_MODEL_WORKITEM_STORAGE_KEY, scope="entity")
    except Exception:
        pass


def _write_workitem_state(vkt: Any, state: AnalyticalModelWorkitemState) -> None:
    vkt.Storage().set(
        ANALYTICAL_MODEL_WORKITEM_STORAGE_KEY,
        data=vkt.File.from_data(state.model_dump_json(indent=2)),
        scope="entity",
    )


def _read_workitem_state(vkt: Any) -> AnalyticalModelWorkitemState | None:
    stored_file = vkt.Storage().get(ANALYTICAL_MODEL_WORKITEM_STORAGE_KEY, scope="entity")
    if not stored_file:
        return None
    raw = stored_file.getvalue_binary().decode("utf-8")
    return AnalyticalModelWorkitemState.model_validate_json(raw)


def _elapsed_seconds(started_at_epoch: int) -> int:
    return max(0, int(time.time()) - started_at_epoch)


def _build_status_response(
    state: AnalyticalModelWorkitemState,
    *,
    status: str,
    done: bool,
    report_url: str | None = None,
    output_item_urn: str | None = None,
    stored_storage_key: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "workitem_id": state.workitem_id,
        "status": status,
        "elapsed_seconds": _elapsed_seconds(state.started_at_epoch),
        "report_url": report_url,
        "done": done,
        "output_file_name": state.output_file_name,
    }
    if stored_storage_key:
        payload["stored_storage_key"] = stored_storage_key
    if output_item_urn:
        payload["output_item_urn"] = output_item_urn
    return json.dumps(payload, indent=2)


def _finalize_analytical_model_json(vkt: Any, state: AnalyticalModelWorkitemState) -> tuple[str | None, str]:
    from aps_automation_sdk import ActivityOutputParameterAcc

    from app.aec import APS_AUTOMATION_OAUTH_INTEGRATION, get_token

    token3lo = get_token(APS_AUTOMATION_OAUTH_INTEGRATION)
    output_acc = ActivityOutputParameterAcc(
        name="exportJson",
        localName="analytical_export.json",
        verb="put",
        description="Analytical model JSON output",
        project_id=state.project_id,
        folder_id=state.output_folder_id,
        file_name=state.output_file_name,
    )

    created_item = output_acc.create_acc_item(token3lo)
    output_item_urn = ((created_item or {}).get("data") or {}).get("id")
    print(f"ACC lineage URN: {output_item_urn}")

    with TemporaryDirectory() as tmpdir:
        local_output = Path(tmpdir) / state.output_file_name
        output_acc.download_to(str(local_output), token3lo)
        print(f"Downloaded to: {local_output}")

        parsed_json = json.loads(local_output.read_text(encoding="utf-8"))
        data_json = json.dumps(parsed_json, indent=2)

    vkt.Storage().set(
        state.storage_key,
        data=vkt.File.from_data(data_json),
        scope="entity",
    )

    return output_item_urn, state.storage_key


async def extract_analytical_model_json_func(ctx: Any, args: str) -> str:
    try:
        import viktor as vkt
        from aps_automation_sdk import (
            ActivityInputParameterAcc,
            ActivityOutputParameterAcc,
            WorkItemAcc,
        )

        from app.aec import get_acc_automation_context
    except ImportError as e:
        return f"Error importing required modules: {e}."

    payload = ExtractAnalyticalModelJsonArgs.model_validate_json(args or "{}")

    try:
        autodesk_file = _get_selected_autodesk_file(ctx)
        acc_context = get_acc_automation_context(autodesk_file)
        activity_full_alias = require_env("APS_ACTIVITY_FULL_ALIAS")
        activity_signature = require_env("APS_ACTIVITY_SIGNATURE")
        output_file_name = _build_unique_output_file_name(payload.output_file_name)

        input_acc = ActivityInputParameterAcc(
            name="inputModel",
            localName="input.rvt",
            verb="get",
            description="Input Revit model from ACC",
            required=True,
            is_engine_input=True,
            project_id=acc_context.project_id,
            linage_urn=acc_context.input_item_urn,
        )
        output_acc = ActivityOutputParameterAcc(
            name="exportJson",
            localName="analytical_export.json",
            verb="put",
            description="Analytical model JSON output",
            project_id=acc_context.project_id,
            folder_id=acc_context.output_folder_id,
            file_name=output_file_name,
        )

        workitem = WorkItemAcc(
            parameters=[input_acc, output_acc],
            activity_full_alias=activity_full_alias,
        )

        print(
            "Submitting ACC work item "
            f"for project_id={acc_context.project_id} item_urn={acc_context.input_item_urn}"
        )

        workitem_id = workitem.run_public_activity(
            token3lo=acc_context.token3lo,
            activity_signature=activity_signature,
        )
        print(f"Work item created: {workitem_id}")

        _delete_workitem_state(vkt)
        state = AnalyticalModelWorkitemState(
            workitem_id=workitem_id,
            project_id=acc_context.project_id,
            input_item_urn=acc_context.input_item_urn,
            output_folder_id=acc_context.output_folder_id,
            output_file_name=output_file_name,
            storage_key=payload.storage_key,
            started_at_epoch=int(time.time()),
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
                "storage_key": payload.storage_key,
            },
            indent=2,
        )

    except Exception as e:
        logger.exception("Unexpected error in extract_analytical_model_json_func")
        return f"Error starting ACC analytical model automation: {type(e).__name__}: {e}"


async def poll_extract_analytical_model_json_func(_ctx: Any, args: str) -> str:
    try:
        import viktor as vkt
        from aps_automation_sdk.core import get_workitem_status

        from app.aec import APS_AUTOMATION_OAUTH_INTEGRATION, get_token
    except ImportError as e:
        return f"Error importing required modules: {e}."

    PollAnalyticalModelJsonArgs.model_validate_json(args or "{}")

    try:
        state = _read_workitem_state(vkt)
        if state is None:
            return "No active analytical model ACC workitem found in Viktor Storage."

        token3lo = get_token(APS_AUTOMATION_OAUTH_INTEGRATION)
        status_payload = get_workitem_status(state.workitem_id, token3lo)
        status = status_payload.get("status", "unknown")
        report_url = status_payload.get("reportUrl")
        print(
            f"[{_elapsed_seconds(state.started_at_epoch):>3}s] "
            f"status={status} report_url={report_url}"
        )

        if status == "success":
            output_item_urn, stored_storage_key = _finalize_analytical_model_json(vkt, state)
            _delete_workitem_state(vkt)
            return _build_status_response(
                state,
                status=status,
                done=True,
                report_url=report_url,
                output_item_urn=output_item_urn,
                stored_storage_key=stored_storage_key,
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
        logger.exception("Unexpected error while polling ACC analytical model automation")
        return f"Error polling ACC analytical model automation: {type(e).__name__}: {e}"


async def get_last_extract_analytical_model_json_workitem_func(_ctx: Any, args: str) -> str:
    try:
        import viktor as vkt
    except ImportError as e:
        return f"Error importing required modules: {e}."

    GetLastAnalyticalModelJsonWorkitemArgs.model_validate_json(args or "{}")

    try:
        state = _read_workitem_state(vkt)
        if state is None:
            return "No active analytical model ACC workitem found in Viktor Storage."

        return json.dumps(
            {
                "workitem_id": state.workitem_id,
                "status": state.last_status or "submitted",
                "elapsed_seconds": _elapsed_seconds(state.started_at_epoch),
                "report_url": state.last_report_url,
                "output_file_name": state.output_file_name,
                "storage_key": state.storage_key,
            },
            indent=2,
        )

    except Exception as e:
        logger.exception("Unexpected error while reading the analytical model ACC workitem")
        return f"Error reading analytical model ACC workitem: {type(e).__name__}: {e}"


def extract_analytical_model_json_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="extract_analytical_model_json",
        description=(
            "Start the ACC analytical model automation for the selected Autodesk model. "
            "Returns the submitted workitem id immediately and stores it as the last analytical ACC workitem in Viktor Storage. "
            "After the run succeeds, call poll_extract_analytical_model_json to download the generated JSON "
            "and store it in Viktor Storage with key 'acc_analytical_model_json' by default. "
            "Requires APS_ACTIVITY_FULL_ALIAS and APS_ACTIVITY_SIGNATURE environment variables."
        ),
        params_json_schema=ExtractAnalyticalModelJsonArgs.model_json_schema(),
        on_invoke_tool=extract_analytical_model_json_func,
    )


def poll_extract_analytical_model_json_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="poll_extract_analytical_model_json",
        description=(
            "Poll the latest analytical model ACC workitem stored in Viktor Storage. "
            "Returns the current status and elapsed time. On success, it downloads the generated JSON, "
            "stores it in Viktor Storage, and clears the stored workitem."
        ),
        params_json_schema=PollAnalyticalModelJsonArgs.model_json_schema(),
        on_invoke_tool=poll_extract_analytical_model_json_func,
    )


def get_last_extract_analytical_model_json_workitem_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="get_last_extract_analytical_model_json_workitem",
        description=(
            "Read the latest analytical model ACC workitem metadata stored in Viktor Storage without polling Autodesk."
        ),
        params_json_schema=GetLastAnalyticalModelJsonWorkitemArgs.model_json_schema(),
        on_invoke_tool=get_last_extract_analytical_model_json_workitem_func,
    )
