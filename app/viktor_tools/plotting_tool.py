import viktor as vkt
from pydantic import BaseModel, Field
from typing import Any, Literal


class PlotTool(BaseModel):
    """Arguments for a line plot tool"""

    x: list[float] = Field(..., description="X-axis values")
    y: list[float] = Field(..., description="Y-axis values")
    xlabel: str = Field(default="X", description="Label for the X-axis")
    ylabel: str = Field(default="Y", description="Label for the Y-axis")


class ShowHidePlotArgs(BaseModel):
    """Arguments for show/hide plot tool"""

    action: Literal["show", "hide"] = Field(
        ...,
        description="Action to perform: 'show' to display the plot view, 'hide' to hide it",
    )


async def display_dashboard_func(ctx: Any, args: str) -> str | None:
    """Displays Plot in plotly"""

    payload = PlotTool.model_validate_json(args)

    if payload:
        vkt.Storage().set(
            "PlotTool",
            data=vkt.File.from_data(payload.model_dump_json()),
            scope="entity",
        )
        return "Plotly Graph generated. Open the Model Viewer panel to view it."
    return f"Validation error Incorrect Outputs {args}"


async def show_hide_plot_func(ctx: Any, args: str) -> str:
    """Show or hide the plot view."""
    payload = ShowHidePlotArgs.model_validate_json(args)
    action = payload.action

    if action == "show":
        print("Showing Plot View")
    else:
        print("Hiding Plot View")

    vkt.Storage().set(
        "show_plot",
        data=vkt.File.from_data(action),
        scope="entity",
    )
    print(f"Plot Visibility State Changed to {action}")
    return f"Plot Visibility State Changed to {action}"


def generate_plot() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="generate_plotly",
        description=(
            "Generate a Plotly line plot with markers. "
            "Takes x-axis and y-axis values as lists of floats, plus optional xlabel and ylabel. "
            "The plot will be displayed in the Plot view panel."
        ),
        params_json_schema=PlotTool.model_json_schema(),
        on_invoke_tool=display_dashboard_func,
    )


def show_hide_plot_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="show_hide_plot",
        description=(
            "Show or hide the Plot view panel. "
            "Pass 'show' to display the plot view, 'hide' to hide it."
        ),
        params_json_schema=ShowHidePlotArgs.model_json_schema(),
        on_invoke_tool=show_hide_plot_func,
    )
