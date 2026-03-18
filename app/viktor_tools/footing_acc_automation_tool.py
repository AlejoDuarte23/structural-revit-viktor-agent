"""Run the footing ACC automation and create a new output file in ACC."""

import inspect
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"success", "failedUpload", "cancelled"}
FOOTING_SIZING_STORAGE_KEY = "footing_sizing_results"
INPUT_MODEL_PARAMETER_NAME = "inputModel"
INPUT_MODEL_LOCAL_NAME = "input.rvt"
FOOTINGS_JSON_PARAMETER_NAME = "footings"
FOOTINGS_JSON_LOCAL_NAME = "footings.json"
OUTPUT_MODEL_PARAMETER_NAME = "resultModel"
OUTPUT_MODEL_LOCAL_NAME = "result.rvt"
DEFAULT_OUTPUT_FILE_NAME = "footing_output.rvt"
DEFAULT_MAX_WAIT = 1200
DEFAULT_INTERVAL = 10


class RunFootingAccAutomationArgs(BaseModel):
    """No user input is required for the footing ACC automation."""


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


def _build_unique_output_file_name(file_name: str) -> str:
    path = Path(file_name)
    suffix = path.suffix or ".rvt"
    return f"{path.stem}_{uuid.uuid4().hex[:5]}{suffix}"


async def run_footing_acc_automation_func(ctx: Any, args: str) -> str:
    try:
        import viktor as vkt
        from aps_automation_sdk import (
            ActivityInputParameterAcc,
            ActivityJsonParameter,
            ActivityOutputParameterAcc,
            WorkItemAcc,
        )

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

        from tempfile import NamedTemporaryFile
        import json
        from aps_automation_sdk.classes  import UploadActivityInputParameter
         
        with NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump(footing_payload, tmp, separators=(",", ":"))
            footing_payload_path = tmp.name
         
        footings_json = UploadActivityInputParameter(
            name="footingPayload",
            folder_id= acc_context.output_folder_id,
            project_id= acc_context.project_id,
            localName="pad_foundations.json",
            file_name="pad_foundations.json",
            file_path= footing_payload_path,
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

        execute_signature = inspect.signature(workitem.execute)
        if "activity_signature" in execute_signature.parameters:
            status_payload = workitem.execute(
                token=acc_context.token3lo,
                activity_signature=activity_signature,
                max_wait=DEFAULT_MAX_WAIT,
                interval=DEFAULT_INTERVAL,
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
                max_wait=DEFAULT_MAX_WAIT,
                interval=DEFAULT_INTERVAL,
            )

        status = status_payload.get("status")
        report_url = status_payload.get("reportUrl")
        if status != "success":
            return (
                f"ACC footing automation finished with status '{status}'. "
                f"Report URL: {report_url or 'n/a'}"
            )

        created_item = output_acc.create_acc_item(acc_context.token3lo)
        output_item_urn = ((created_item or {}).get("data") or {}).get("id")
        print(f"ACC lineage URN: {output_item_urn}")

        return (
            "Successfully generated the footing ACC output file. "
            f"Prepared {len(footing_payload)} footing entries from storage. "
            f"Created ACC file '{output_file_name}' in the same folder as the selected model. "
            f"Output ACC item URN: {output_item_urn or 'unknown'}. "
            f"Report URL: {report_url or 'n/a'}."
        )

    except Exception as e:
        logger.exception("Unexpected error in run_footing_acc_automation_func")
        return f"Error running footing ACC automation: {type(e).__name__}: {e}"


def run_footing_acc_automation_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="run_footing_acc_automation",
        description=(
            "Run the ACC footing automation for the selected Autodesk model. "
            "Reads footing sizing data from Viktor Storage key 'footing_sizing_results', "
            "normalizes it to the add-in payload with B, L, x, y, z, and creates the generated output file "
            "directly in ACC in the same folder as the selected model without downloading it locally. "
            "Requires APS_ACTIVITY_FOOTING_FULL_ALIAS and APS_ACTIVITY_FOOTING_SIGNATURE environment variables."
        ),
        params_json_schema=RunFootingAccAutomationArgs.model_json_schema(),
        on_invoke_tool=run_footing_acc_automation_func,
    )
