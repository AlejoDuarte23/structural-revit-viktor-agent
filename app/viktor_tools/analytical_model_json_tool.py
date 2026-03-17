"""Run ACC automation on the selected Autodesk model and store the JSON result."""

import json
import logging
import os
import inspect
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ANALYTICAL_MODEL_STORAGE_KEY = "acc_analytical_model_json"
TERMINAL_STATUSES = {"success", "failedUpload", "cancelled"}


class ExtractAnalyticalModelJsonArgs(BaseModel):
    """Arguments for running the analytical model ACC automation."""

    output_file_name: str = Field(
        default="analytical_model.json",
        description="Output file name to create in ACC.",
    )
    storage_key: str = Field(
        default=ANALYTICAL_MODEL_STORAGE_KEY,
        description="Viktor Storage key where the downloaded JSON should be stored.",
    )
    max_wait: int = Field(
        default=1200,
        ge=10,
        description="Maximum time in seconds to wait for the ACC work item.",
    )
    interval: int = Field(
        default=10,
        ge=1,
        description="Polling interval in seconds for work item status updates.",
    )


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


def _poll_workitem_status(
    workitem_id: str,
    token3lo: str,
    *,
    max_wait: int,
    interval: int,
) -> dict[str, Any]:
    from aps_automation_sdk.core import get_workitem_status

    elapsed = 0
    status_payload: dict[str, Any] = {}

    while elapsed <= max_wait:
        status_payload = get_workitem_status(workitem_id, token3lo)
        status = status_payload.get("status", "unknown")
        report_url = status_payload.get("reportUrl")
        print(f"[{elapsed:>3}s] status={status} report_url={report_url}")

        if status in TERMINAL_STATUSES:
            return status_payload

        time.sleep(interval)
        elapsed += interval

    raise TimeoutError(
        f"ACC work item did not finish within {max_wait} seconds (workitem_id={workitem_id})."
    )


def _print_poll_event(event: Any) -> None:
    print(
        f"[{getattr(event, 'elapsed_seconds', 0):>3}s] "
        f"status={getattr(event, 'status', 'unknown')} "
        f"report_url={getattr(event, 'report_url', None)}"
    )


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

    payload = ExtractAnalyticalModelJsonArgs.model_validate_json(args)

    try:
        autodesk_file = _get_selected_autodesk_file(ctx)
        acc_context = get_acc_automation_context(autodesk_file)
        activity_full_alias = require_env("APS_ACTIVITY_FULL_ALIAS")
        activity_signature = require_env("APS_ACTIVITY_SIGNATURE")

        input_acc = ActivityInputParameterAcc(
            name="InputModel",
            localName="input.rvt",
            verb="get",
            description="Input Revit model from ACC",
            required=True,
            is_engine_input=True,
            project_id=acc_context.project_id,
            linage_urn=acc_context.input_item_urn,
        )
        output_acc = ActivityOutputParameterAcc(
            name="ResultJson",
            localName=payload.output_file_name,
            verb="put",
            description="Analytical model JSON output",
            project_id=acc_context.project_id,
            folder_id=acc_context.output_folder_id,
            file_name=payload.output_file_name,
        )

        workitem = WorkItemAcc(
            parameters=[input_acc, output_acc],
            activity_full_alias=activity_full_alias,
        )

        print(
            "Submitting ACC work item "
            f"for project_id={acc_context.project_id} item_urn={acc_context.input_item_urn}"
        )

        execute_signature = inspect.signature(workitem.execute)
        if "activity_signature" in execute_signature.parameters:
            status_payload = workitem.execute(
                token=acc_context.token3lo,
                activity_signature=activity_signature,
                max_wait=payload.max_wait,
                interval=payload.interval,
                on_event=_print_poll_event,
            )
        else:
            workitem_id = workitem.run_public_activity(
                token3lo=acc_context.token3lo,
                activity_signature=activity_signature,
            )
            print(f"Work item created: {workitem_id}")
            status_payload = _poll_workitem_status(
                workitem_id,
                acc_context.token3lo,
                max_wait=payload.max_wait,
                interval=payload.interval,
            )

        status = status_payload.get("status")
        report_url = status_payload.get("reportUrl")
        if status != "success":
            return (
                f"ACC analytical model automation finished with status '{status}'. "
                f"Report URL: {report_url or 'n/a'}"
            )

        created_item = output_acc.create_acc_item(acc_context.token3lo)
        output_item_urn = ((created_item or {}).get("data") or {}).get("id")
        print(f"ACC lineage URN: {output_item_urn}")

        with TemporaryDirectory() as tmpdir:
            local_output = Path(tmpdir) / payload.output_file_name
            output_acc.download_to(str(local_output), acc_context.token3lo)
            print(f"Downloaded to: {local_output}")

            parsed_json = json.loads(local_output.read_text(encoding="utf-8"))
            data_json = json.dumps(parsed_json, indent=2)

        vkt.Storage().set(
            payload.storage_key,
            data=vkt.File.from_data(data_json),
            scope="entity",
        )

        return (
            "Successfully generated analytical model JSON from ACC automation. "
            f"Stored JSON in Viktor Storage with key '{payload.storage_key}'. "
            f"Output ACC item URN: {output_item_urn or 'unknown'}."
        )

    except Exception as e:
        logger.exception("Unexpected error in extract_analytical_model_json_func")
        return f"Error running ACC analytical model automation: {type(e).__name__}: {e}"


def extract_analytical_model_json_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="extract_analytical_model_json",
        description=(
            "Run the ACC analytical model automation for the selected Autodesk model. "
            "Uses the current Autodesk file to resolve project id, input item lineage URN, and output folder id. "
            "Prints polling status updates while the ACC work item runs, then downloads the generated JSON "
            "and stores it in Viktor Storage with key 'acc_analytical_model_json' by default. "
            "Requires APS_ACTIVITY_FULL_ALIAS and APS_ACTIVITY_SIGNATURE environment variables."
        ),
        params_json_schema=ExtractAnalyticalModelJsonArgs.model_json_schema(),
        on_invoke_tool=extract_analytical_model_json_func,
    )
