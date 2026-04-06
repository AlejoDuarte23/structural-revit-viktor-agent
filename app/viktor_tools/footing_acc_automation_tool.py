"""Submit the footing ACC automation for later polling."""

import json
import logging
import os
import uuid
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from pydantic import BaseModel, TypeAdapter

logger = logging.getLogger(__name__)

FOOTING_SIZING_STORAGE_KEY = "footing_sizing_results"
INPUT_MODEL_PARAMETER_NAME = "inputModel"
INPUT_MODEL_LOCAL_NAME = "input.rvt"
OUTPUT_MODEL_PARAMETER_NAME = "resultModel"
OUTPUT_MODEL_LOCAL_NAME = "result.rvt"
DEFAULT_OUTPUT_FILE_NAME = "footing_output.rvt"


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
        FootingAddinEntry(B=entry.B, L=entry.L, x=entry.x, y=entry.z, z=entry.y)
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
        from app.viktor_tools.acc_workitem_polling_tool import (
            FOOTING_JOB_STORAGE_KEY,
            PendingAccJob,
            save_pending_job,
        )
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

            if not output_acc._storage_id:
                raise RuntimeError("ACC output storage id was not created during submission.")

            save_pending_job(
                vkt,
                FOOTING_JOB_STORAGE_KEY,
                PendingAccJob(
                    job_type="footing_acc_automation",
                    workitem_id=workitem_id,
                    project_id=acc_context.project_id,
                    folder_id=acc_context.output_folder_id,
                    file_name=output_file_name,
                    output_storage_id=output_acc._storage_id,
                ),
            )
        finally:
            Path(footing_payload_path).unlink(missing_ok=True)

        return (
            "Submitted the footing ACC automation successfully. "
            f"Prepared {len(footing_payload)} footing entries from storage. "
            f"Work item id: {workitem_id}. "
            f"Output ACC file name: {output_file_name}. "
            "Use 'poll_footing_acc_job' to check completion and finalize the ACC output file."
        )

    except Exception as e:
        logger.exception("Unexpected error in run_footing_acc_automation_func")
        return f"Error running footing ACC automation: {type(e).__name__}: {e}"


def run_footing_acc_automation_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="run_footing_acc_automation",
        description=(
            "Submit the ACC footing automation for the selected Autodesk model. "
            "Reads footing sizing data from Viktor Storage key 'footing_sizing_results', "
            "normalizes it to the add-in payload with B, L, x, y, z, and stores the pending ACC job metadata, "
            "including the output storage id, so a later poll can finalize the generated output file in ACC "
            "without downloading it locally. "
            "Requires APS_ACTIVITY_FOOTING_FULL_ALIAS and APS_ACTIVITY_FOOTING_SIGNATURE environment variables."
        ),
        params_json_schema=RunFootingAccAutomationArgs.model_json_schema(),
        on_invoke_tool=run_footing_acc_automation_func,
    )
