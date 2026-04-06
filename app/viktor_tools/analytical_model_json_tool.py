"""Submit ACC automation on the selected Autodesk model for later polling."""

import logging
import os
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ANALYTICAL_MODEL_STORAGE_KEY = "acc_analytical_model_json"


class ExtractAnalyticalModelJsonArgs(BaseModel):
    """Arguments for submitting the analytical model ACC automation."""

    output_file_name: str = Field(
        default="analytical_model.json",
        description="Output file name to create in ACC.",
    )
    storage_key: str = Field(
        default=ANALYTICAL_MODEL_STORAGE_KEY,
        description="Viktor Storage key where the downloaded JSON should be stored.",
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


def _build_unique_output_file_name(file_name: str) -> str:
    path = Path(file_name)
    suffix = path.suffix or ".json"
    return f"{path.stem}_{uuid.uuid4().hex[:5]}{suffix}"


async def extract_analytical_model_json_func(ctx: Any, args: str) -> str:
    try:
        import viktor as vkt
        from aps_automation_sdk import (
            ActivityInputParameterAcc,
            ActivityOutputParameterAcc,
            WorkItemAcc,
        )

        from app.aec import get_acc_automation_context
        from app.viktor_tools.acc_workitem_polling_tool import (
            ANALYTICAL_JOB_STORAGE_KEY,
            PendingAccJob,
            save_pending_job,
        )
    except ImportError as e:
        return f"Error importing required modules: {e}."

    payload = ExtractAnalyticalModelJsonArgs.model_validate_json(args)

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

        if not output_acc._storage_id:
            raise RuntimeError("ACC output storage id was not created during submission.")

        save_pending_job(
            vkt,
            ANALYTICAL_JOB_STORAGE_KEY,
            PendingAccJob(
                job_type="analytical_model_json",
                workitem_id=workitem_id,
                project_id=acc_context.project_id,
                folder_id=acc_context.output_folder_id,
                file_name=output_file_name,
                output_storage_id=output_acc._storage_id,
                storage_key=payload.storage_key,
            ),
        )

        return (
            "Submitted ACC analytical model automation successfully. "
            f"Work item id: {workitem_id}. "
            f"Output ACC file name: {output_file_name}. "
            "Use 'poll_analytical_model_acc_job' to check completion and store the JSON in Viktor Storage."
        )

    except Exception as e:
        logger.exception("Unexpected error in extract_analytical_model_json_func")
        return f"Error running ACC analytical model automation: {type(e).__name__}: {e}"


def extract_analytical_model_json_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="extract_analytical_model_json",
        description=(
            "Submit the ACC analytical model automation for the selected Autodesk model. "
            "Uses the current Autodesk file to resolve project id, input item lineage URN, and output folder id. "
            "Stores the pending ACC job metadata, including the output storage id, so a later poll can "
            "finalize the ACC file, download the generated JSON, and store it in Viktor Storage with key "
            "'acc_analytical_model_json' by default. "
            "Requires APS_ACTIVITY_FULL_ALIAS and APS_ACTIVITY_SIGNATURE environment variables."
        ),
        params_json_schema=ExtractAnalyticalModelJsonArgs.model_json_schema(),
        on_invoke_tool=extract_analytical_model_json_func,
    )
