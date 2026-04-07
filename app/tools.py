import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, Json

import viktor as vkt

from app.workflow_graph.models import PlanTodo, ProgressStep, WorkflowPlan, WorkflowProgress
from app.workflow_graph.state import build_canvas_state, load_canvas_state, save_canvas_state
from app.viktor_tools.footing_sizing_tool import calculate_footing_sizing_tool
from app.viktor_tools.pile_axial_capacity_tool import (
    calculate_pile_axial_capacity_tool,
)
from app.viktor_tools.analytical_model_json_tool import (
    extract_analytical_model_json_tool,
)
from app.viktor_tools.acc_workitem_polling_tool import (
    poll_analytical_model_acc_job_tool,
    poll_footing_acc_job_tool,
    poll_pile_acc_job_tool,
)
from app.viktor_tools.footing_acc_automation_tool import (
    run_footing_acc_automation_tool,
)
from app.viktor_tools.pile_acc_automation_tool import run_pile_acc_automation_tool
from app.viktor_tools.autodesk_context_tool import get_autodesk_file_context_tool
from app.viktor_tools.autodesk_view_tool import show_hide_autodesk_view_tool
from app.viktor_tools.plotting_tool import generate_plot, show_hide_plot_tool
from app.viktor_tools.table_tool import generate_table, show_hide_table_tool
from app.sap_revit_tools.tool_reference_comptypes import (
    build_sap_model_from_analytical_json_comptypes_tool as build_sap_model_from_analytical_json_tool,
)
from app.sap_tools.display_reaction_loads_table import (
    display_reaction_loads_table_tool,
)


# Friendly display names for tools in chat
TOOL_DISPLAY_NAMES: dict[str, str] = {
    "calculate_footing_sizing": "Footing Sizing",
    "calculate_pile_axial_capacity": "Pile Axial Capacity",
    "generate_plotly": "Generate Plot",
    "generate_table": "Generate Table",
    "extract_analytical_model_json": "Submit Analytical ACC Job",
    "poll_analytical_model_acc_job": "Poll Analytical ACC Job",
    "run_footing_acc_automation": "Submit ACC Footing Model",
    "poll_footing_acc_job": "Poll Footing ACC Job",
    "run_pile_acc_automation": "Submit ACC Pile Model",
    "poll_pile_acc_job": "Poll Pile ACC Job",
    "get_autodesk_file_context": "Get ACC File Information",
    "show_hide_autodesk_view": "Display Revit Model",
    "show_hide_plot": "Show/Hide Plot",
    "show_hide_table": "Show/Hide Table",
    "create_dummy_workflow_node": "Create Workflow Node",
    "compose_workflow_graph": "Compose Workflow Graph",
    "get_workflow_plan": "Get Workflow Plan",
    "set_workflow_plan": "Set Workflow Plan",
    "update_workflow_plan": "Update Workflow Plan",
    "set_workflow_progress": "Set Workflow Progress",
    "build_sap_model_from_analytical_json": "Create SAP Model",
    "display_reaction_loads_table": "Display Reaction Loads",
}


class Workflow(BaseModel):
    pass


class GeometryGeneration(BaseModel):
    structure_width: float = Field(..., description="Widht of the structure in mm")
    structure_lenght: float = Field(..., description="Lenght of the structure in mm")
    structure_height: float = Field(..., description="Height of the structure in mm")
    csc_section: Literal["UB200x30", "310UBx46"]


class WindloadAnalysis(BaseModel):
    region: Literal["A", "B", "C", "D"]
    wind_speed: float = Field(..., description="Wind speed in m/s")
    exposure_level: Literal["A", "B", "C", "D"]


class Result(BaseModel):
    pass


class StructuralAnalysis(BaseModel):
    geometry_result: Result
    wind_result: Result | None = None


class FootingCapacity(BaseModel):
    soil_cateogory: Literal["A", "B", "C", "D", "F"]
    foundation_type: Literal["Footing", "Pile", "Slab"]


class FootingDesign(BaseModel):
    reaction_loads: list[float]
    footing_capacity_result: Result


class DummyWorkflowNode(BaseModel):
    node_id: str = Field(..., description="Unique id for this workflow node")
    node_type: Literal[
        "sap2000_tool",
        "sap2000_load_combos",
        "sap2000_extraction",
        "footing_sizing",
        "calculate_footing_sizing",
        "calculate_pile_axial_capacity",
        "get_autodesk_file_context",
        "show_hide_autodesk_view",
        "extract_analytical_model_json",
        "build_sap_model_from_analytical_json",
        "display_reaction_loads_table",
        "run_footing_acc_automation",
        "run_pile_acc_automation",
        "plot_output",
        "table_output",
    ] = Field(..., description="Type of workflow node to add to the graph")
    label: str = Field(..., description="Human-readable label for the node")
    url: str | None = Field(
        default=None,
        description=(
            "Optional URL to the VIKTOR app tool. Leave empty/null for local visualization "
            "nodes, viewer nodes, storage-backed automation nodes, and backend workflow steps "
            "that do not open a dedicated app page."
        ),
    )
    inputs: Json[Any] = Field(
        default="{}",
        description=(
            "Input parameters for the node, provided as a JSON string. "
            'Example: \'{"wind_speed": 45.0, "region": "B"}\'.'
        ),
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="List of upstream node_ids this node depends on",
    )


async def create_dummy_workflow_node_func(
    _ctx: Any,
    args: str,
) -> str:
    payload = DummyWorkflowNode.model_validate_json(args)
    return f"Node '{payload.node_id}' ({payload.node_type}) created successfully."


def create_dummy_workflow_node_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="create_dummy_workflow_node",
        description="Create a dummy workflow-node JSON artifact for graph composition.",
        params_json_schema=DummyWorkflowNode.model_json_schema(),
        on_invoke_tool=create_dummy_workflow_node_func,
    )


class ComposeWorkflowGraphArgs(BaseModel):
    workflow_name: Annotated[str, Field(description="Name for the composed workflow")]
    nodes: Annotated[
        list[DummyWorkflowNode],
        Field(description="Workflow nodes with dependencies to compose"),
    ]


def toposort_edges(nodes: list[str], edges: list[tuple[str, str]]) -> bool:
    indegree: dict[str, int] = {n: 0 for n in nodes}
    outgoing: dict[str, list[str]] = {n: [] for n in nodes}
    for src, dst in edges:
        outgoing.setdefault(src, []).append(dst)
        indegree[dst] = indegree.get(dst, 0) + 1

    queue = [n for n, deg in indegree.items() if deg == 0]
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        for nxt in outgoing.get(node, []):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    return visited == len(nodes)


async def compose_workflow_graph_func(ctx: Any, args: str) -> str:
    payload = ComposeWorkflowGraphArgs.model_validate_json(args)

    ids = [n.node_id for n in payload.nodes]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"Duplicate node_id(s): {', '.join(duplicates)}")

    id_set = set(ids)
    missing_deps: dict[str, list[str]] = {}
    edges: list[tuple[str, str]] = []
    for n in payload.nodes:
        unknown = [d for d in n.depends_on if d not in id_set]
        if unknown:
            missing_deps[n.node_id] = unknown
        for d in n.depends_on:
            edges.append((d, n.node_id))
    if missing_deps:
        msg = "; ".join(
            f"{nid} -> missing {', '.join(deps)}" for nid, deps in missing_deps.items()
        )
        raise ValueError(f"Unknown dependency node_id(s): {msg}")

    if not toposort_edges(ids, edges):
        raise ValueError("Cycle detected in depends_on; workflow_graph expects a DAG.")

    from app.workflow_graph.models import Connection, Node, Workflow
    from app.workflow_graph.viewer import WorkflowViewer

    # Default fallback URL (not applied to non-URL workflow steps)
    default_url = "https://beta.viktor.ai/workspaces/4672/app/editor/2394"
    non_url_node_types = {
        "sap2000_tool",
        "sap2000_load_combos",
        "sap2000_extraction",
        "get_autodesk_file_context",
        "show_hide_autodesk_view",
        "extract_analytical_model_json",
        "build_sap_model_from_analytical_json",
        "display_reaction_loads_table",
        "run_footing_acc_automation",
        "plot_output",
        "table_output",
    }

    workflow = Workflow(
        nodes=[
            Node(
                id=n.node_id,
                title=n.label,
                type=n.node_type,
                url=None
                if n.node_type in non_url_node_types
                else (n.url or default_url),
                depends_on=[Connection(node_id=d) for d in n.depends_on],
            )
            for n in payload.nodes
        ]
    )

    canvas_state = build_canvas_state(payload.workflow_name, workflow)
    viewer = WorkflowViewer(lambda: canvas_state)
    html_content = viewer.write()  # Returns HTML string

    try:
        save_canvas_state(canvas_state)
        data_json = json.dumps(
            {
                "html": html_content,
                "workflow_name": payload.workflow_name,
            }
        )
        vkt.Storage().set(
            "workflow_html",
            data=vkt.File.from_data(data_json),
            scope="entity",
        )
    except Exception:
        pass

    return (
        f"Workflow '{payload.workflow_name}' created successfully with "
        f"{len(payload.nodes)} nodes and {len(edges)} connections. "
        "The workflow graph has been updated and a plan panel now appears in the top-left of the canvas."
    )


def compose_workflow_graph_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="compose_workflow_graph",
        description="Compose nodes into a workflow_graph DAG and render a self-contained HTML graph.",
        params_json_schema=ComposeWorkflowGraphArgs.model_json_schema(),
        on_invoke_tool=compose_workflow_graph_func,
    )


class WorkflowPlanTodoInput(BaseModel):
    id: str = Field(..., description="Stable todo id for this plan item")
    label: str = Field(..., description="Short todo label shown in the plan")
    status: Literal["pending", "in_progress", "completed", "cancelled"] = Field(
        default="pending",
        description="Current status for this todo item",
    )
    description: str | None = Field(
        default=None,
        description="Optional detail shown when the todo row is expanded",
    )


class SetWorkflowPlanArgs(BaseModel):
    title: str = Field(..., description="Plan title shown in the overlay card")
    description: str | None = Field(
        default=None,
        description="Optional plan description below the title",
    )
    todos: list[WorkflowPlanTodoInput] = Field(
        ...,
        description="Ordered todo items for the workflow plan",
    )
    max_visible_todos: int = Field(
        default=4,
        ge=1,
        description="Maximum number of todos shown before the overlay collapses into a '+ more' row",
    )


class UpdateWorkflowPlanTodoInput(BaseModel):
    id: str = Field(..., description="Existing todo id to update")
    label: str | None = Field(
        default=None,
        description="Optional replacement label for the todo",
    )
    status: Literal["pending", "in_progress", "completed", "cancelled"] | None = Field(
        default=None,
        description="Optional replacement status for the todo",
    )
    description: str | None = Field(
        default=None,
        description="Optional replacement description for the todo",
    )


class GetWorkflowPlanArgs(BaseModel):
    pass


class UpdateWorkflowPlanArgs(BaseModel):
    title: str | None = Field(default=None, description="Optional replacement plan title")
    description: str | None = Field(
        default=None,
        description="Optional replacement plan description",
    )
    max_visible_todos: int | None = Field(
        default=None,
        ge=1,
        description="Optional replacement max visible todo count",
    )
    todos: list[UpdateWorkflowPlanTodoInput] = Field(
        default_factory=list,
        description="Todo updates matched by id",
    )
    append_missing: bool = Field(
        default=False,
        description="Append unknown todo ids instead of failing. Added todos require a label.",
    )


class WorkflowProgressStepInput(BaseModel):
    id: str = Field(..., description="Stable progress step id")
    label: str = Field(..., description="Short progress step label")
    description: str | None = Field(
        default=None,
        description="Optional detail text for this progress step",
    )
    status: Literal["pending", "in_progress", "completed", "failed"] = Field(
        default="pending",
        description="Current execution status for this step",
    )


class SetWorkflowProgressArgs(BaseModel):
    title: str = Field(
        default="Execution Progress",
        description="Progress section title shown below the plan",
    )
    steps: list[WorkflowProgressStepInput] = Field(
        default_factory=list,
        description="Ordered execution steps for the progress tracker",
    )
    elapsed_time_ms: int | None = Field(
        default=None,
        ge=0,
        description="Optional elapsed time in milliseconds",
    )
    clear: bool = Field(
        default=False,
        description="Clear the progress tracker instead of replacing it",
    )


def _require_canvas_state():
    state = load_canvas_state()
    if state is None:
        raise ValueError(
            "No workflow graph is available yet. Run 'compose_workflow_graph' first."
        )
    return state


def _missing_workflow_plan_response(*, reason: str) -> str:
    import json

    response = {
        "status": "missing_prerequisite",
        "reason": reason,
        "next_steps": [
            "compose_workflow_graph",
            "set_workflow_plan",
        ],
    }
    return json.dumps(response, indent=2)


async def get_workflow_plan_func(_ctx: Any, args: str) -> str:
    GetWorkflowPlanArgs.model_validate_json(args or "{}")
    state = load_canvas_state()
    if state is None:
        return _missing_workflow_plan_response(
            reason=(
                "No workflow graph is available yet. Create one with "
                "'compose_workflow_graph' before requesting the workflow plan."
            )
        )

    if state.plan is None:
        return _missing_workflow_plan_response(
            reason=(
                f"Workflow graph '{state.workflow_name}' exists but no workflow plan has been set yet. "
                "Run 'set_workflow_plan' before trying to update plan tasks."
            )
        )

    import json
    plan_data = {
        "title": state.plan.title,
        "description": state.plan.description,
        "workflow_name": state.workflow_name,
        "todos": [
            {
                "id": todo.id,
                "label": todo.label,
                "status": todo.status,
                "description": todo.description,
            }
            for todo in state.plan.todos
        ],
    }
    return (
        f"Current workflow plan for '{state.workflow_name}':\n"
        f"{json.dumps(plan_data, indent=2)}"
    )


async def set_workflow_plan_func(_ctx: Any, args: str) -> str:
    payload = SetWorkflowPlanArgs.model_validate_json(args)
    state = _require_canvas_state()
    state.plan = WorkflowPlan(
        id=state.plan.id if state.plan else "workflow-plan",
        title=payload.title,
        description=payload.description,
        todos=[
            PlanTodo(
                id=todo.id,
                label=todo.label,
                status=todo.status,
                description=todo.description,
            )
            for todo in payload.todos
        ],
        max_visible_todos=payload.max_visible_todos,
    )
    save_canvas_state(state)
    return (
        f"Workflow plan updated with {len(payload.todos)} todo items for "
        f"'{state.workflow_name}'."
    )


async def update_workflow_plan_func(_ctx: Any, args: str) -> str:
    payload = UpdateWorkflowPlanArgs.model_validate_json(args)
    state = _require_canvas_state()
    if state.plan is None:
        raise ValueError(
            "No workflow plan exists yet. Run 'set_workflow_plan' after creating the workflow."
        )

    todos_by_id = {todo.id: todo for todo in state.plan.todos}
    missing_ids: list[str] = []
    appended = 0
    updated = 0

    for todo_update in payload.todos:
        todo = todos_by_id.get(todo_update.id)
        if todo is None:
            if not payload.append_missing:
                missing_ids.append(todo_update.id)
                continue
            if not todo_update.label:
                raise ValueError(
                    f"Todo '{todo_update.id}' does not exist and needs a label to be appended."
                )
            todo = PlanTodo(
                id=todo_update.id,
                label=todo_update.label,
                status=todo_update.status or "pending",
                description=todo_update.description,
            )
            state.plan.todos.append(todo)
            todos_by_id[todo.id] = todo
            appended += 1
            continue

        if todo_update.label is not None:
            todo.label = todo_update.label
        if todo_update.status is not None:
            todo.status = todo_update.status
        if todo_update.description is not None:
            todo.description = todo_update.description
        updated += 1

    if missing_ids:
        raise ValueError(
            "Unknown todo id(s): "
            + ", ".join(missing_ids)
            + ". Pass append_missing=true to add them."
        )

    if payload.title is not None:
        state.plan.title = payload.title
    if payload.description is not None:
        state.plan.description = payload.description
    if payload.max_visible_todos is not None:
        state.plan.max_visible_todos = payload.max_visible_todos

    save_canvas_state(state)
    return (
        f"Workflow plan updated for '{state.workflow_name}' "
        f"({updated} modified, {appended} appended)."
    )


async def set_workflow_progress_func(_ctx: Any, args: str) -> str:
    payload = SetWorkflowProgressArgs.model_validate_json(args)
    state = _require_canvas_state()

    if payload.clear:
        state.progress = None
        save_canvas_state(state)
        return f"Workflow progress cleared for '{state.workflow_name}'."

    state.progress = WorkflowProgress(
        id=state.progress.id if state.progress else "workflow-progress",
        title=payload.title,
        steps=[
            ProgressStep(
                id=step.id,
                label=step.label,
                description=step.description,
                status=step.status,
            )
            for step in payload.steps
        ],
        elapsed_time_ms=payload.elapsed_time_ms,
    )
    save_canvas_state(state)
    return (
        f"Workflow progress updated with {len(payload.steps)} steps for "
        f"'{state.workflow_name}'."
    )


def get_workflow_plan_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="get_workflow_plan",
        description=(
            "Get the current workflow plan with all todo items and their statuses. "
            "ALWAYS call this before updating the plan to see existing task IDs and statuses. "
            "This prevents creating duplicate tasks. If no workflow graph or plan exists yet, "
            "the tool returns a non-fatal prerequisite response telling you to run "
            "'compose_workflow_graph' and/or 'set_workflow_plan'."
        ),
        params_json_schema=GetWorkflowPlanArgs.model_json_schema(),
        on_invoke_tool=get_workflow_plan_func,
    )


def set_workflow_plan_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="set_workflow_plan",
        description=(
            "Populate or replace the plan card shown in the top-left of the current workflow graph. "
            "Use this after 'compose_workflow_graph' when you want a curated implementation or execution plan."
        ),
        params_json_schema=SetWorkflowPlanArgs.model_json_schema(),
        on_invoke_tool=set_workflow_plan_func,
    )


def update_workflow_plan_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="update_workflow_plan",
        description=(
            "Update todo labels, descriptions, and statuses in the current workflow plan overlay. "
            "Use stable todo ids created by 'set_workflow_plan' or the default ids derived from workflow nodes."
        ),
        params_json_schema=UpdateWorkflowPlanArgs.model_json_schema(),
        on_invoke_tool=update_workflow_plan_func,
    )


def set_workflow_progress_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="set_workflow_progress",
        description=(
            "Show, replace, or clear the execution progress tracker shown under the workflow plan card. "
            "Pass clear=true to remove the progress section."
        ),
        params_json_schema=SetWorkflowProgressArgs.model_json_schema(),
        on_invoke_tool=set_workflow_progress_func,
    )


def get_tools() -> list[Any]:
    return [
        create_dummy_workflow_node_tool(),
        compose_workflow_graph_tool(),
        get_workflow_plan_tool(),
        set_workflow_plan_tool(),
        update_workflow_plan_tool(),
        set_workflow_progress_tool(),
        build_sap_model_from_analytical_json_tool(),
        display_reaction_loads_table_tool(),
        calculate_footing_sizing_tool(),
        calculate_pile_axial_capacity_tool(),
        extract_analytical_model_json_tool(),
        poll_analytical_model_acc_job_tool(),
        run_footing_acc_automation_tool(),
        poll_footing_acc_job_tool(),
        run_pile_acc_automation_tool(),
        poll_pile_acc_job_tool(),
        get_autodesk_file_context_tool(),
        generate_plot(),
        generate_table(),
        show_hide_autodesk_view_tool(),
        show_hide_plot_tool(),
        show_hide_table_tool(),
    ]
