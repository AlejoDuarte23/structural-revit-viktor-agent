"""Inspect the selected Autodesk file context for testing and debugging."""

import json
from typing import Any

from pydantic import BaseModel, Field


class GetAutodeskFileContextArgs(BaseModel):
    """Arguments for inspecting the selected Autodesk file context."""

    include_version_info: bool = Field(
        default=True,
        description="Whether to include viewer and ACC version context information.",
    )


def _get_selected_autodesk_file(ctx: Any) -> Any:
    run_context = getattr(ctx, "context", None)
    autodesk_file = getattr(run_context, "autodesk_file", None)
    if autodesk_file is None:
        raise ValueError("Select an Autodesk model first.")
    return autodesk_file


async def get_autodesk_file_context_func(ctx: Any, args: str) -> str:
    from app.aec import get_acc_automation_context, get_model_context

    payload = GetAutodeskFileContextArgs.model_validate_json(args)

    try:
        autodesk_file = _get_selected_autodesk_file(ctx)
        viewer_context = get_model_context(autodesk_file)
        acc_context = get_acc_automation_context(autodesk_file)
    except Exception as e:
        return f"Error retrieving Autodesk file context: {type(e).__name__}: {e}"

    file_context: dict[str, Any] = {
        "name": getattr(autodesk_file, "name", None),
        "display_name": getattr(autodesk_file, "display_name", None),
        "hub_id": getattr(autodesk_file, "hub_id", None),
        "project_id": getattr(autodesk_file, "project_id", None),
        "folder_id": getattr(autodesk_file, "folder_id", None),
        "item_id": getattr(autodesk_file, "item_id", None),
        "item_urn": getattr(autodesk_file, "urn", None),
    }

    if payload.include_version_info:
        file_context |= {
            "region": viewer_context.region,
            "viewer_version_urn": viewer_context.version_urn,
            "acc_input_item_urn": acc_context.input_item_urn,
            "acc_output_folder_id": acc_context.output_folder_id,
            "acc_version_urn": acc_context.version_urn,
        }

    filtered_context = {k: v for k, v in file_context.items() if v is not None}
    return json.dumps(filtered_context, indent=2)


def get_autodesk_file_context_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="get_autodesk_file_context",
        description=(
            "Inspect the selected Autodesk file context for testing. "
            "Returns metadata such as hub id, project id, item URN, version URN, and ACC output folder id."
            "Remember to update execution plan"
        ),
        params_json_schema=GetAutodeskFileContextArgs.model_json_schema(),
        on_invoke_tool=get_autodesk_file_context_func,
    )
