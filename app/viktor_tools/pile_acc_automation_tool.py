"""Submit the pile ACC automation for later polling."""

import json
import logging
import os
import uuid
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from pydantic import BaseModel

from app.viktor_tools.pile_axial_capacity_tool import (
    PILE_AXIAL_CAPACITY_STORAGE_KEY,
    PileAxialCapacityOutput,
    PileCapExportParameters,
    PileCapPlacement,
)

logger = logging.getLogger(__name__)

INPUT_MODEL_PARAMETER_NAME = "inputModel"
INPUT_MODEL_LOCAL_NAME = "input.rvt"
INPUT_MODEL_DESCRIPTION = "Input Revit model"
INPUT_JSON_PARAMETER_NAME = "pilePayload"
INPUT_JSON_LOCAL_NAME = "pile_foundations.json"
INPUT_JSON_FILE_NAME = "pile_foundations.json"
INPUT_JSON_DESCRIPTION = "Pile foundations JSON payload"
OUTPUT_MODEL_PARAMETER_NAME = "resultModel"
OUTPUT_MODEL_LOCAL_NAME = "result.rvt"
OUTPUT_MODEL_DESCRIPTION = "Output Revit model"
DEFAULT_OUTPUT_FILE_NAME = "pile_output.rvt"
DEFAULT_FAMILY_NAME = "Pile Cap-3 Round Pile"
DEFAULT_TYPE_NAME = "Standard"
DEFAULT_UNITS = "Millimeters"


class RunPileAccAutomationArgs(BaseModel):
    """No user input is required for the pile ACC automation."""


class PileFoundationAddinPayload(BaseModel):
    """Final JSON shape expected by the Autodesk add-in."""

    familyName: str = DEFAULT_FAMILY_NAME
    typeName: str = DEFAULT_TYPE_NAME
    units: str = DEFAULT_UNITS
    parameters: PileCapExportParameters
    placements: list[PileCapPlacement]


def require_any_env(*names: str) -> str:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    raise RuntimeError(
        "Missing required environment variable. Checked: " + ", ".join(names)
    )


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


def _load_pile_payload_from_storage(vkt: Any) -> PileFoundationAddinPayload:
    raw = _get_storage_text(vkt, PILE_AXIAL_CAPACITY_STORAGE_KEY)
    exported = PileAxialCapacityOutput.model_validate_json(raw)
    if not exported.placements:
        raise ValueError(
            f"Storage key '{PILE_AXIAL_CAPACITY_STORAGE_KEY}' does not contain any placements."
        )
    return PileFoundationAddinPayload(
        parameters=exported.parameters,
        placements=exported.placements,
    )


def _build_unique_output_file_name(file_name: str) -> str:
    path = Path(file_name)
    suffix = path.suffix or ".rvt"
    return f"{path.stem}_{uuid.uuid4().hex[:5]}{suffix}"


async def run_pile_acc_automation_func(ctx: Any, args: str) -> str:
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
            PILE_JOB_STORAGE_KEY,
            PendingAccJob,
            save_pending_job,
        )
    except ImportError as e:
        return f"Error importing required modules: {e}."

    RunPileAccAutomationArgs.model_validate_json(args or "{}")

    try:
        pile_payload = _load_pile_payload_from_storage(vkt)
        autodesk_file = _get_selected_autodesk_file(ctx)
        acc_context = get_acc_automation_context(autodesk_file)
        activity_full_alias = require_any_env(
            "APS_ACTIVITY_PILE_FULL_ALIAS",
            "APS_ACTIVITY_PILE_FOUNDATION_FULL_ALIAS",
        )
        activity_signature = require_any_env(
            "APS_ACTIVITY_PILE_SIGNATURE",
            "APS_ACTIVITY_PILE_FOUNDATION_SIGNATURE",
        )
        output_file_name = _build_unique_output_file_name(DEFAULT_OUTPUT_FILE_NAME)

        input_acc = ActivityInputParameterAcc(
            name=INPUT_MODEL_PARAMETER_NAME,
            localName=INPUT_MODEL_LOCAL_NAME,
            verb="get",
            description=INPUT_MODEL_DESCRIPTION,
            required=True,
            is_engine_input=True,
            project_id=acc_context.project_id,
            linage_urn=acc_context.input_item_urn,
        )

        with NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump(pile_payload.model_dump(mode="json"), tmp, separators=(",", ":"))
            pile_payload_path = tmp.name
        try:
            payload_acc = UploadActivityInputParameter(
                name=INPUT_JSON_PARAMETER_NAME,
                folder_id=acc_context.output_folder_id,
                project_id=acc_context.project_id,
                localName=INPUT_JSON_LOCAL_NAME,
                file_name=INPUT_JSON_FILE_NAME,
                file_path=pile_payload_path,
                verb="get",
                description=INPUT_JSON_DESCRIPTION,
                required=True,
            )
            output_acc = ActivityOutputParameterAcc(
                name=OUTPUT_MODEL_PARAMETER_NAME,
                localName=OUTPUT_MODEL_LOCAL_NAME,
                verb="put",
                description=OUTPUT_MODEL_DESCRIPTION,
                project_id=acc_context.project_id,
                folder_id=acc_context.output_folder_id,
                file_name=output_file_name,
            )

            workitem = WorkItemAcc(
                parameters=[input_acc, payload_acc, output_acc],
                activity_full_alias=activity_full_alias,
            )

            print(
                "Submitting pile ACC work item "
                f"for project_id={acc_context.project_id} item_urn={acc_context.input_item_urn} "
                f"with {len(pile_payload.placements)} pile placements"
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
                PILE_JOB_STORAGE_KEY,
                PendingAccJob(
                    job_type="pile_acc_automation",
                    workitem_id=workitem_id,
                    project_id=acc_context.project_id,
                    folder_id=acc_context.output_folder_id,
                    file_name=output_file_name,
                    output_storage_id=output_acc._storage_id,
                ),
            )
        finally:
            Path(pile_payload_path).unlink(missing_ok=True)

        return (
            "Submitted the pile ACC automation successfully. "
            f"Prepared {len(pile_payload.placements)} placements from storage. "
            f"Work item id: {workitem_id}. "
            f"Output ACC file name: {output_file_name}. "
            "Use 'poll_pile_acc_job' to check completion and finalize the ACC output file."
        )

    except Exception as e:
        logger.exception("Unexpected error in run_pile_acc_automation_func")
        return f"Error running pile ACC automation: {type(e).__name__}: {e}"


def run_pile_acc_automation_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="run_pile_acc_automation",
        description=(
            "Submit the ACC pile automation for the selected Autodesk model. "
            "Reads pile cap export data from Viktor Storage key 'pile_axial_capacity_results', "
            "adds the required hardcoded family/type metadata, uploads it as 'pile_foundations.json', "
            "and stores the pending ACC job metadata, including the output storage id, so a later poll can "
            "finalize the generated output file in ACC without downloading it locally. "
            "Requires APS_ACTIVITY_PILE_FULL_ALIAS or APS_ACTIVITY_PILE_FOUNDATION_FULL_ALIAS, "
            "and APS_ACTIVITY_PILE_SIGNATURE or APS_ACTIVITY_PILE_FOUNDATION_SIGNATURE."
        ),
        params_json_schema=RunPileAccAutomationArgs.model_json_schema(),
        on_invoke_tool=run_pile_acc_automation_func,
    )
