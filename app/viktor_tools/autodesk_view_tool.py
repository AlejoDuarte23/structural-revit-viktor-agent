from typing import Any, Literal

import viktor as vkt
from pydantic import BaseModel, Field


class ShowHideAutodeskViewArgs(BaseModel):
    """Arguments for show/hide Autodesk viewer tool."""

    action: Literal["show", "hide"] = Field(
        ...,
        description="Action to perform: 'show' to display the Autodesk viewer, 'hide' to hide it",
    )


async def show_hide_autodesk_view_func(ctx: Any, args: str) -> str:
    """Show or hide the Autodesk viewer."""
    payload = ShowHideAutodeskViewArgs.model_validate_json(args)
    action = payload.action

    if action == "show":
        print("Showing Autodesk Viewer")
    else:
        print("Hiding Autodesk Viewer")

    vkt.Storage().set(
        "show_autodesk_view",
        data=vkt.File.from_data(action),
        scope="entity",
    )
    print(f"Autodesk Viewer Visibility State Changed to {action}")
    return f"Autodesk Viewer Visibility State Changed to {action}, if this task is in the Execution Plan mark it as complete."


def show_hide_autodesk_view_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="show_hide_autodesk_view",
        description=(
            "Show or hide the Autodesk viewer panel. "
            "Pass 'show' to display the selected Autodesk model, 'hide' to hide the viewer."
        ),
        params_json_schema=ShowHideAutodeskViewArgs.model_json_schema(),
        on_invoke_tool=show_hide_autodesk_view_func,
    )
