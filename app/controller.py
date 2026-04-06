import asyncio
import json
import logging
import queue
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from typing import Any
from collections.abc import Callable

import viktor as vkt
from agents import Agent, ItemHelpers, Runner
from openai.types.responses import ResponseTextDeltaEvent
from agents import set_tracing_disabled

from app.aec import get_model_context
from app.tools import get_tools, TOOL_DISPLAY_NAMES
from app.workflow_graph.state import delete_canvas_state, load_canvas_state
from app.workflow_graph.viewer import WorkflowViewer
from app.viktor_tools.plotting_tool import PlotTool
from app.viktor_tools.table_tool import TableTool

from dotenv import load_dotenv

import plotly.graph_objects as go

load_dotenv()

logger = logging.getLogger(__name__)

# Event loop management for async agent in sync VIKTOR context
event_loop: asyncio.AbstractEventLoop | None = None
event_loop_thread: threading.Thread | None = None

set_tracing_disabled(True)


@dataclass
class AgentContext:
    autodesk_file: Any | None = None


def ensure_loop() -> asyncio.AbstractEventLoop:
    """Ensure a background event loop is running."""
    global event_loop, event_loop_thread
    if event_loop and event_loop.is_running():
        return event_loop
    event_loop = asyncio.new_event_loop()
    event_loop_thread = threading.Thread(
        target=event_loop.run_forever, name="agent-loop", daemon=True
    )
    event_loop_thread.start()
    return event_loop


def run_async(coro):
    """Run async coroutine in background loop and wait for result."""
    loop = ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result()


def _extract_call_id(raw: Any) -> str | None:
    if isinstance(raw, dict):
        return (raw.get("call_id") or raw.get("id") or raw.get("tool_call_id")) and str(
            raw.get("call_id") or raw.get("id") or raw.get("tool_call_id")
        )
    for attr in ("call_id", "id", "tool_call_id"):
        v = getattr(raw, attr, None)
        if v:
            return str(v)
    return None


def _extract_tool_name(raw: Any) -> str:
    # Responses function-call items typically have a top-level "name" (or equivalent)
    if isinstance(raw, dict):
        if raw.get("name"):
            return str(raw["name"])
        fn = raw.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            return str(fn["name"])
        if raw.get("tool_name"):
            return str(raw["tool_name"])
    for attr in ("name", "tool_name", "function_name"):
        v = getattr(raw, attr, None)
        if v:
            return str(v)
    fn = getattr(raw, "function", None)
    if fn is not None and getattr(fn, "name", None):
        return str(fn.name)
    return "tool"


def workflow_agent_sync_stream(
    chat_history: list[dict[str, str]],
    *,
    autodesk_file: Any | None = None,
    on_done: Callable[[], None] | None = None,
    show_tool_progress: bool = True,
) -> Iterator[str]:
    """
    Sync generator for vkt.ChatResult that streams agent output token-by-token
    using Runner.run_streamed + result.stream_events().
    """
    q: queue.Queue[object] = queue.Queue()
    sentinel = object()

    loop = ensure_loop()

    async def _produce() -> None:
        call_id_to_name: dict[str, str] = {}
        try:
            agent = Agent[AgentContext](
                name="Structural Analysis Assistant",
                instructions=dedent(
                    """You are a helpful assistant for structural engineering tasks using Autodesk analytical export, SAP2000 worker integration, and footing design tools.

            STYLE RULES:
            - Be succinct and friendly - avoid over-elaboration
            - Don't aggressively propose actions - wait for user direction
            - Provide clear, concise responses
            - Only suggest next steps when explicitly asked or when clarification is needed
            - Markdown is allowed, but don't use tables; format with bold, headings, sections, and links.
            - You MUST emit a separate assistant message item before EVERY poll tool call for poll_footing_acc_job poll_footing_acc_job
            
            YOUR CAPABILITIES:

            1. ACC / REVIT CONTEXT
               Work with the selected Autodesk model before running backend flows:

               - get_autodesk_file_context: Inspect the selected Autodesk model context
                 * Returns file metadata such as hub id, project id, item URN, version URN, and ACC output folder id
                 * Use this when the user wants to verify which ACC/Revit file the app is currently pointing at

               - show_hide_autodesk_view: Control Autodesk Viewer panel visibility
                 * Shows the Autodesk model selected in the Autodesk model field
                 * Use action='show' when the user asks to open or display the model
                 * Use action='hide' when the user asks to close or hide the model viewer

               - extract_analytical_model_json: Submit Revit analytical model automation
                 * Submits the ACC automation on the selected Autodesk model
                 * Uses the selected Autodesk model to resolve project id, input lineage URN, and output folder id
                 * Stores the pending ACC job metadata, including the output storage id
                 * Returns a work item id for later polling
                 * Does not store the analytical JSON immediately
                 * Requires APS_ACTIVITY_FULL_ALIAS and APS_ACTIVITY_SIGNATURE to be configured
                 * You MUST emit a separate assistant message item before EVERY Submits tool call

               - poll_analytical_model_acc_job: Check analytical ACC job status
                 * Polls the latest submitted analytical ACC work item once
                 * If the work item is successful, finalizes the ACC file, downloads the JSON,
                   and stores it in Viktor Storage with key 'acc_analytical_model_json'
                 * If the work item is still running, returns the current status and report URL
                 * You MUST emit a separate assistant message item before EVERY run_footing_acc_automation,
                   tool call
 
               LONG-RUNNING ACC JOBS:
               - The Agents SDK has a built-in agent loop that can keep calling tools until the task is complete
               - When the user wants an ACC job followed through to completion in the same run,
                 use an agentic polling loop
               - You MUST emit a separate assistant message item before EVERY poll tool call
               - The message MUST be plain assistant text and should sound natural
               - Do not call a poll tool silently and do not chain poll tool calls back-to-back without that message
               - After submitting the job, send a short natural status update before each poll
                 such as "I'm checking the ACC job status now."
               - Then call the matching poll tool with its default wait so checks happen about every 10 seconds
               - If the poll tool says the job is still running, send another short natural update and poll again
               - Stop only when the poll tool returns a terminal status
               - Only continue to downstream tools after the required ACC finalization and storage step is complete

            2. SAP2000 WORKER FLOW
               Build and run the SAP2000 model from the exported analytical JSON:

               - build_sap_model_from_analytical_json: Create and run the SAP2000 model
                 * Reads the analytical JSON from Viktor Storage
                 * Builds the SAP2000 model, assigns supports, assigns slab loads, runs analysis, and stores results
                 * Stores support coordinates under 'model_support_coordinates'
                 * Stores reaction loads under 'model_reaction_loads'

               IMPORTANT: SAP2000 must be running with a model open and configured as active API instance
               (Tools → Set as active instance for API in SAP2000) before the worker runs.

               TYPICAL WORKFLOW:
               1. extract_analytical_model_json → Submit analytical export
               2. poll_analytical_model_acc_job → Repeat until success and JSON storage is complete
               3. build_sap_model_from_analytical_json → Create and run the SAP2000 model

            3. DATA DISPLAY
               Transform stored SAP2000 data into reviewable outputs:

               - display_support_coordinates_table: Show support nodes in table format
                 * Columns: Joint, X (m), Y (m), Z (m), U1, U2, U3, R1, R2, R3
                 * Automatically shows Table view panel
                 * Must run build_sap_model_from_analytical_json first

               - display_reaction_loads_table: Show reaction loads in flattened table
                 * Columns: Node, Load Combo, F1 (kN), F2 (kN), F3 (kN), M1 (kN·m), M2 (kN·m), M3 (kN·m)
                 * Shows all nodes × all load combinations
                 * Automatically shows Table view panel
                 * Must run build_sap_model_from_analytical_json first

            4. FOOTING WORKFLOW
               - calculate_footing_sizing: Run foundation pad sizing
                 * URL: https://demo.viktor.ai/workspaces/2141/app/editor/11536
                 * Automatically loads node coordinates and reaction loads from SAP2000 storage
                 * REQUIRES: build_sap_model_from_analytical_json must be run first
                 * Sends soil.q_allow, soil.gamma_c, soil.depth, and soil.b_min to the app
                 * Sends nodes_section.nodes_table and lc_section.load_cases_table built from SAP2000 storage
                 * Stores the exported governing pad sizing list in 'footing_sizing_results'

                 LOAD COMBINATION SELECTION:
                 * Use 'load_combinations_to_check' to specify which combos to use (e.g., ['ULS2', 'ULS3'])
                   Tool includes all specified combinations per node
                 * Can pass single combo name as string (e.g., 'ULS3')
                 * If None, uses all available combos for optimization

               - calculate_pile_axial_capacity: Run the pile axial capacity export app
                 * URL: https://demo.viktor.ai/workspaces/2232/app/editor/11640
                 * Automatically loads node coordinates and reaction loads from SAP2000 storage
                 * REQUIRES: build_sap_model_from_analytical_json must be run first
                 * Sends nodes_section.nodes and reaction_loads_section.load_cases built from SAP2000 storage
                 * Sends pile_section, cap_section, soil_section, and concrete_section from tool inputs
                 * Calls the remote app method 'download_results'
                 * Stores the parsed exported JSON in 'pile_axial_capacity_results'
                 * Use 'load_combinations_to_check' to limit the checked combinations; if omitted, all combinations are included

               - run_footing_acc_automation: Submit the ACC footing model automation
                 * Uses the selected Autodesk model to resolve project id, input lineage URN, and output folder id
                 * Reads footing data from Viktor Storage key 'footing_sizing_results'
                 * Sends only footing B, L, x, y, z values to the add-in payload
                 * Submits the job and stores the pending ACC job metadata, including the output storage id
                 * Returns a work item id for later polling
                 * Does not download the result locally or store it in Viktor Storage
                 * Requires APS_ACTIVITY_FOOTING_FULL_ALIAS and APS_ACTIVITY_FOOTING_SIGNATURE to be configured

               - run_pile_acc_automation: Submit the ACC pile model automation
                 * Uses the selected Autodesk model to resolve project id, input lineage URN, and output folder id
                 * Reads pile data from Viktor Storage key 'pile_axial_capacity_results'
                 * Adds familyName, typeName, and units required by the add-in payload
                 * Uploads the payload as 'pile_foundations.json'
                 * Submits the job and stores the pending ACC job metadata, including the output storage id
                 * Returns a work item id for later polling
                 * Does not download the result locally or store it in Viktor Storage
                 * Requires APS_ACTIVITY_PILE_FULL_ALIAS or APS_ACTIVITY_PILE_FOUNDATION_FULL_ALIAS,
                   and APS_ACTIVITY_PILE_SIGNATURE or APS_ACTIVITY_PILE_FOUNDATION_SIGNATURE

               - poll_footing_acc_job: Check footing ACC job status
                 * Polls the latest submitted footing ACC work item once
                 * If the work item is successful, finalizes the ACC output file in ACC
                 * If the work item is still running, returns the current status and report URL

               - poll_pile_acc_job: Check pile ACC job status
                 * Polls the latest submitted pile ACC work item once
                 * If the work item is successful, finalizes the ACC output file in ACC
                 * If the work item is still running, returns the current status and report URL

               TYPICAL WORKFLOW OPTIONS:
               Default footing workflow:
               1. get_autodesk_file_context
               2. show_hide_autodesk_view (when the user wants the model displayed)
               3. extract_analytical_model_json
               4. poll_analytical_model_acc_job
               5. build_sap_model_from_analytical_json
               6. calculate_footing_sizing
               7. run_footing_acc_automation
               8. poll_footing_acc_job

               Alternative pile workflow:
               1. get_autodesk_file_context
               2. show_hide_autodesk_view (when the user wants the model displayed)
               3. extract_analytical_model_json
               4. poll_analytical_model_acc_job
               5. build_sap_model_from_analytical_json
               6. calculate_pile_axial_capacity
               7. run_pile_acc_automation
               8. poll_pile_acc_job

               DEFAULT BEHAVIOR:
               - Use the footing workflow by default
               - Do not switch to the pile workflow unless the user explicitly asks for piles
                 or confirms they want the pile option
               - If the user asks for foundation automation without specifying footing vs piles,
                 proceed with footing sizing and mention that a pile-based alternative is available

            5. VISUALIZATION TOOLS
               - generate_plotly: Create line/bar plots from x and y data
                 * Must call show_hide_plot with action="show" after to display

               - generate_table: Create custom tables with data and column headers
                 * Must call show_hide_table with action="show" after to display

               - show_hide_plot: Control Plot view panel visibility
               - show_hide_table: Control Table view panel visibility
               - show_hide_autodesk_view: Control Autodesk Viewer panel visibility

            6. WORKFLOW GRAPHS (Optional)
               Create visual workflow diagrams to document engineering processes.

               **CRITICAL: Always Track Task Progress**
               When a workflow plan exists, you MUST update task statuses as you work:
               - **BEFORE updating any task**: ALWAYS call 'get_workflow_plan' first to see existing task IDs and statuses
               - If 'get_workflow_plan' reports missing prerequisites instead of a plan,
                 do not treat that as a hard failure
               - In that case, create the missing workflow prerequisites first:
                 run 'compose_workflow_graph' if the graph does not exist, then run 'set_workflow_plan'
               - Mark tasks as "in_progress" when you START executing them
               - Mark tasks as "completed" immediately when you FINISH them successfully
               - Mark tasks as "failed" if they encounter errors
               - Use 'update_workflow_plan' with the EXACT task IDs from get_workflow_plan

               Tools available:
               - create_dummy_workflow_node: Create individual nodes
               - compose_workflow_graph: Combine nodes into DAG visualization
               - get_workflow_plan: Get current plan with all task IDs and statuses (CALL THIS FIRST!)
               - set_workflow_plan: Populate the plan card shown on the workflow graph canvas
               - update_workflow_plan: Update plan items and statuses on the workflow graph
               - set_workflow_progress: Show or clear the execution progress tracker below the plan

               Example workflow with status updates:
               1. Check plan: get_workflow_plan() → returns existing task IDs
                  If it returns a missing-prerequisite response, first call compose_workflow_graph() and set_workflow_plan()
               2. Start task: update_workflow_plan(todos=[{"id": "extract_analytical", "status": "in_progress"}])
               3. Execute: extract_analytical_model_json(...)
               4. Complete task: update_workflow_plan(todos=[{"id": "extract_analytical", "status": "completed"}])
               5. Check plan again: get_workflow_plan() → see updated statuses
               6. Start next task: update_workflow_plan(todos=[{"id": "build_sap_model", "status": "in_progress"}])
               7. And so on...

               **IMPORTANT**: Never create new tasks when updating - always use existing task IDs from get_workflow_plan!

               Available node types for workflows:
               - get_autodesk_file_context: "Get ACC File Information"
               - show_hide_autodesk_view: "Display Revit Model"
                 → Typically depends on: get_autodesk_file_context
               - extract_analytical_model_json: "Get Revit Analytical Model"
                 → Typically depends on: get_autodesk_file_context
               - build_sap_model_from_analytical_json: "Create SAP Model"
                 → Typically depends on: extract_analytical_model_json
               - display_support_coordinates_table: "Display Coordinate Table"
                 → Typically depends on: build_sap_model_from_analytical_json
               - calculate_footing_sizing: "Footing Sizing"
                 → URL: https://demo.viktor.ai/workspaces/2141/app/editor/11536
                 → Typically depends on: build_sap_model_from_analytical_json
               - calculate_pile_axial_capacity: "Pile Axial Capacity"
                 → URL: https://demo.viktor.ai/workspaces/2232/app/editor/11640
                 → Typically depends on: build_sap_model_from_analytical_json
               - run_footing_acc_automation: "Finalize ACC Footing Model"
                 → Typically depends on: get_autodesk_file_context, build_sap_model_from_analytical_json, calculate_footing_sizing
               - run_pile_acc_automation: "Finalize ACC Pile Model"
                 → Typically depends on: get_autodesk_file_context, build_sap_model_from_analytical_json, calculate_pile_axial_capacity
               - plot_output: Generic visualization node (no URL)
               - table_output: Table display node (no URL)

            GENERAL APPROACH:
            - Start from the selected ACC/Revit model context
            - Show the Autodesk viewer when the user wants to inspect the model
            - Export analytical data before building the SAP2000 model
            - Display support coordinates when the user wants a quick verification table
            - Run footing sizing before the ACC footing automation
            - For long-running ACC jobs, use short assistant progress messages plus repeated poll tool calls until completion
            - Create workflow graphs to document process flow (optional)
            - **ALWAYS update plan task statuses** when a workflow plan is active:
              * Call get_workflow_plan FIRST to see existing task IDs and their current statuses
              * If get_workflow_plan reports missing prerequisites, create the workflow graph and plan first instead of failing
              * Call update_workflow_plan to mark tasks as "in_progress" when starting (use exact IDs from get_workflow_plan)
              * Call update_workflow_plan to mark tasks as "completed" when done (use exact IDs from get_workflow_plan)
              * Call update_workflow_plan to mark tasks as "failed" if errors occur (use exact IDs from get_workflow_plan)
              * NEVER create new tasks - always update existing ones using the IDs from get_workflow_plan
            """
                ),
                model="gpt-5-mini",
                tools=get_tools(),
            )

            # Streamed run (no await here); events are consumed via async iterator.
            result = Runner.run_streamed(
                agent,
                input=chat_history,
                context=AgentContext(autodesk_file=autodesk_file),
                max_turns=100,
            )

            async for event in result.stream_events():
                # Token streaming from raw response delta events
                if event.type == "raw_response_event" and isinstance(
                    event.data, ResponseTextDeltaEvent
                ):
                    if event.data.delta:
                        q.put(event.data.delta)
                    continue

                if not show_tool_progress:
                    continue

                # Higher-level run item events (tool called/output, etc.)
                if event.type == "run_item_stream_event":
                    item = event.item
                    raw = getattr(item, "raw_item", None)

                    if (
                        event.name == "message_output_created"
                        and getattr(item, "type", None) == "message_output_item"
                    ):
                        text = ItemHelpers.text_message_output(item).strip()
                        if text.startswith("Progress:"):
                            q.put(f"\n\n{text}\n\n")
                        continue

                    if event.name == "tool_called":
                        cid = _extract_call_id(raw)
                        tool_name = _extract_tool_name(raw)
                        if cid:
                            call_id_to_name[cid] = tool_name
                        display_name = TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
                        q.put(f"\n\n> ⚙️ Running **{display_name}**\n")
                        continue

                    if event.name == "tool_output":
                        cid = _extract_call_id(raw)
                        tool_name = call_id_to_name.get(cid or "", "tool")
                        display_name = TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
                        q.put(f"\n> ✅ Done **{display_name}**\n\n")
                        continue

        except Exception as e:
            q.put(f"\n\n⚠️ {type(e).__name__}: {e}\n")
        finally:
            q.put(sentinel)

    asyncio.run_coroutine_threadsafe(_produce(), loop)

    def _gen() -> Iterator[str]:
        while True:
            item = q.get()
            if item is sentinel:
                break
            yield item  # type: ignore[misc]
        if on_done:
            on_done()

    return _gen()


def get_visibility(params, **kwargs):
    if not params.chat:
        entities = vkt.Storage().list(scope="entity")
        for entity in entities:
            if entity == "show_plot":
                vkt.Storage().delete("show_plot", scope="entity")
            if entity == "PlotTool":
                vkt.Storage().delete("PlotTool", scope="entity")

    try:
        out_bool = vkt.Storage().get("show_plot", scope="entity").getvalue()
        print(f"{out_bool=}")
        if out_bool == "show":
            return True
        return False
    except Exception:
        # If there is no data, then view is hidden.
        return False


def get_table_visibility(params, **kwargs):
    if not params.chat:
        entities = vkt.Storage().list(scope="entity")
        for entity in entities:
            if entity == "show_table":
                vkt.Storage().delete("show_table", scope="entity")
            if entity == "TableTool":
                vkt.Storage().delete("TableTool", scope="entity")

    try:
        out_bool = vkt.Storage().get("show_table", scope="entity").getvalue()
        print(f"{out_bool=}")
        if out_bool == "show":
            return True
        return False
    except Exception:
        # If there is no data, then view is hidden.
        return False


def get_footings_plot_visibility(params, **kwargs):
    if not params.chat:
        entities = vkt.Storage().list(scope="entity")
        for entity in entities:
            if entity == "show_footings_plot":
                vkt.Storage().delete("show_footings_plot", scope="entity")
            if entity == "PlotFootingsTool":
                vkt.Storage().delete("PlotFootingsTool", scope="entity")

    try:
        out_bool = vkt.Storage().get("show_footings_plot", scope="entity").getvalue()
        print(f"footings_plot {out_bool=}")
        if out_bool == "show":
            return True
        return False
    except Exception:
        # If there is no data, then view is hidden.
        return False


def get_autodesk_view_visibility(params, **kwargs):
    if not params.chat:
        entities = vkt.Storage().list(scope="entity")
        for entity in entities:
            if entity == "show_autodesk_view":
                vkt.Storage().delete("show_autodesk_view", scope="entity")

    try:
        out_bool = vkt.Storage().get("show_autodesk_view", scope="entity").getvalue()
        print(f"autodesk_view {out_bool=}")
        if out_bool == "show":
            return True
        return False
    except Exception:
        # If there is no data, then view is hidden.
        return False


class Parametrization(vkt.Parametrization):
    title = vkt.Text("""# VIKTOR Workflow Agent

Support structural engineering workflows across Autodesk, SAP2000, ACC, and existing VIKTOR apps.

**What I can do:**
- 🔗 Connect to ACC
- 🏢 Use the Autodesk API
- 🧩 Integrate with SAP2000
- ⚙️ Use existing VIKTOR apps
- 📊 Analyze multiple design alternatives

""")
    autodesk_file = vkt.AutodeskFileField(
        "Autodesk model",
        oauth2_integration="aps-automation-webinar-alejandro",
    )
    chat = vkt.Chat("", method="call_llm")


class Controller(vkt.Controller):
    parametrization = Parametrization

    def call_llm(self, params, **kwargs) -> vkt.ChatResult | None:
        """Handle chat interaction with the workflow agent."""
        if not params.chat:
            return None

        messages = params.chat.get_messages()
        chat_history = [{"role": m["role"], "content": m["content"]} for m in messages]

        text_stream = workflow_agent_sync_stream(
            chat_history,
            autodesk_file=params.autodesk_file,
            on_done=self._update_workflow_storage,  # run after stream completes
            show_tool_progress=True,  # emoji tool status lines
        )

        return vkt.ChatResult(params.chat, text_stream)

    def _update_workflow_storage(self) -> None:
        """Scan for generated workflows and store the latest one."""
        workflows_dir = Path.cwd() / "workflow_graph" / "generated_workflows"
        if not workflows_dir.exists():
            return

        # Find the most recently modified workflow
        workflow_dirs = [d for d in workflows_dir.iterdir() if d.is_dir()]
        if not workflow_dirs:
            return

        latest_dir = max(workflow_dirs, key=lambda d: d.stat().st_mtime)
        html_path = latest_dir / "index.html"

        if html_path.exists():
            html_content = html_path.read_text(encoding="utf-8")
            data_json = json.dumps(
                {
                    "html": html_content,
                    "workflow_name": latest_dir.name,
                }
            )
            vkt.Storage().set(
                "workflow_html",
                data=vkt.File.from_data(data_json),
                scope="entity",
            )

    @vkt.WebView("Workflow Graph", width=100)
    def workflow_view(self, params, **kwargs) -> vkt.WebResult:
        """Display the generated workflow graph."""
        # Clear storage when chat is reset
        if not params.chat:
            delete_canvas_state()
            try:
                vkt.Storage().delete("workflow_html", scope="entity")
            except Exception:
                pass

        canvas_state = load_canvas_state()
        if canvas_state is not None:
            viewer = WorkflowViewer(lambda: canvas_state)
            return vkt.WebResult(html=viewer.write())

        try:
            stored_file = vkt.Storage().get("workflow_html", scope="entity")
            if stored_file:
                data_json = stored_file.getvalue_binary().decode("utf-8")
                data = json.loads(data_json)
                html_content = data.get("html", "")
                if html_content:
                    return vkt.WebResult(html=html_content)
        except Exception:
            pass

        # Default placeholder when no workflow exists
        placeholder_html = "<!DOCTYPE html><html><head><style>body { margin: 0; background-color: white; }</style></head><body></body></html>"
        return vkt.WebResult(html=placeholder_html)

    @vkt.WebView(
        "Viewer",
        width=100,
        duration_guess=30,
        visible=get_autodesk_view_visibility,
    )
    def show_cad_model(self, params, **kwargs) -> vkt.WebResult:
        if not params.autodesk_file:
            placeholder_html = (
                "<!DOCTYPE html><html><head><style>"
                "body { margin: 0; font-family: sans-serif; display: grid; place-items: center; "
                "min-height: 100vh; color: #475569; background: #f8fafc; }"
                ".message { padding: 24px; text-align: center; }"
                "</style></head><body><div class='message'>"
                "Select an Autodesk model to open the viewer."
                "</div></body></html>"
            )
            return vkt.WebResult(html=placeholder_html)

        from aps_viewer_sdk import APSViewer

        context = get_model_context(params.autodesk_file)
        viewer = APSViewer(
            urn=context.version_urn,
            token=context.token,
            views_selector=True,
        )
        return vkt.WebResult(html=viewer.write())

    @vkt.PlotlyView("Plot Tool", width=100, visible=get_visibility)
    def plot_view(self, params, **kwargs) -> vkt.PlotlyResult:
        if not params.chat:
            try:
                vkt.Storage().delete("PlotTool", scope="entity")
            except Exception:
                pass
        try:
            raw = vkt.Storage().get("PlotTool", scope="entity").getvalue()  # str
            logger.info(f"Plot raw data: {raw}")
            tool_input = PlotTool.model_validate(json.loads(raw))
            logger.info(f"Plot tool_input: {tool_input}")

            fig = go.Figure(
                data=[
                    go.Scatter(
                        x=tool_input.x,
                        y=tool_input.y,
                        mode="lines+markers",
                        line=dict(color="blue", width=2),
                        marker=dict(color="red", size=8),
                    )
                ],
                layout=go.Layout(
                    title="Line Plot",
                    xaxis_title=tool_input.xlabel,
                    yaxis_title=tool_input.ylabel,
                ),
            )
        except Exception as e:
            logger.exception(f"Error in plot_view: {e}")
            fig = go.Figure()

        return vkt.PlotlyResult(fig.to_json())

    @vkt.TableView("Table Tool", width=100, visible=get_table_visibility)
    def table_view(self, params, **kwargs) -> vkt.TableResult:
        if not params.chat:
            try:
                vkt.Storage().delete("TableTool", scope="entity")
            except Exception:
                pass
        try:
            raw = (
                vkt.Storage()
                .get("TableTool", scope="entity")
                .getvalue_binary()
                .decode("utf-8")
            )
            logger.info(f"Table raw data: {raw}")
            tool_input = TableTool.model_validate_json(raw)
            logger.info(f"Table tool_input: {tool_input}")
            return vkt.TableResult(
                data=tool_input.data, column_headers=tool_input.column_headers
            )
        except Exception as e:
            logger.exception(f"Error in table_view: {e}")
            return vkt.TableResult([["Error", "using Tool"]])

    @vkt.PlotlyView("Footings Plot", width=100, visible=get_footings_plot_visibility)
    def footings_plot_view(self, params, **kwargs) -> vkt.PlotlyResult:
        if not params.chat:
            try:
                vkt.Storage().delete("PlotFootingsTool", scope="entity")
            except Exception:
                pass
        try:
            from app.viktor_tools.plot_footings_tool import PlotFootingsInput

            raw = (
                vkt.Storage()
                .get("PlotFootingsTool", scope="entity")
                .getvalue_binary()
                .decode("utf-8")
            )
            logger.info(f"Footings plot raw data: {raw}")
            tool_input = PlotFootingsInput.model_validate_json(raw)
            logger.info(f"Footings plot tool_input: {tool_input}")

            # Create Plotly figure
            fig = go.Figure()

            # Colors
            footing_color = "rgba(180, 180, 180, 0.6)"
            pedestal_color = "rgba(100, 100, 100, 0.8)"

            # Track bounds for layout
            all_x = []
            all_y = []

            for footing in tool_input.footings:
                # Skip nodes with missing coordinates
                if footing.x is None or footing.y is None:
                    logger.warning(
                        f"Skipping node {footing.node_name} - missing coordinates"
                    )
                    continue

                cx = footing.x
                cy = footing.y
                node_name = footing.node_name

                if footing.B is not None and footing.L is not None:
                    # Node has design - draw footing and pedestal
                    B = footing.B
                    L = footing.L
                    h = footing.h or 0.0
                    ped = footing.pedestal_size or 0.0
                    ped_h = footing.pedestal_height or 0.0

                    # Draw footing rectangle
                    x0, x1 = cx - B / 2, cx + B / 2
                    y0, y1 = cy - L / 2, cy + L / 2
                    hover_text = f"{node_name}<br>Footing: {B:.2f}m × {L:.2f}m<br>Thickness: {h * 1000:.0f}mm"
                    if footing.pedestal_height is not None:
                        hover_text += f"<br>Depth: {(ped_h + h) * 1000:.0f}mm"
                    if footing.total_weight is not None:
                        hover_text += f"<br>Weight: {footing.total_weight:.1f}kN"
                    if footing.governing_combo:
                        hover_text += f"<br>Combo: {footing.governing_combo}"

                    fig.add_trace(
                        go.Scatter(
                            x=[x0, x1, x1, x0, x0],
                            y=[y0, y0, y1, y1, y0],
                            mode="lines",
                            fill="toself",
                            fillcolor=footing_color,
                            line=dict(color="rgba(100,100,100,1)", width=2),
                            name=f"{node_name} Footing",
                            hoverinfo="text",
                            text=hover_text,
                            showlegend=False,
                        )
                    )

                    # Draw pedestal rectangle if exists
                    if ped > 0:
                        px0, px1 = cx - ped / 2, cx + ped / 2
                        py0, py1 = cy - ped / 2, cy + ped / 2
                        ped_hover = f"{node_name}<br>Pedestal: {ped * 1000:.0f}mm × {ped * 1000:.0f}mm<br>Height: {ped_h * 1000:.0f}mm"
                        fig.add_trace(
                            go.Scatter(
                                x=[px0, px1, px1, px0, px0],
                                y=[py0, py0, py1, py1, py0],
                                mode="lines",
                                fill="toself",
                                fillcolor=pedestal_color,
                                line=dict(color="rgba(50,50,50,1)", width=2),
                                name=f"{node_name} Pedestal",
                                hoverinfo="text",
                                text=ped_hover,
                                showlegend=False,
                            )
                        )

                    # Add node label
                    fig.add_annotation(
                        x=cx,
                        y=cy,
                        text=f"<b>{node_name}</b>",
                        showarrow=False,
                        font=dict(size=11, color="white"),
                        bgcolor="rgba(50,50,50,0.7)",
                        borderpad=4,
                    )

                    all_x.extend([x0, x1])
                    all_y.extend([y0, y1])
                else:
                    # Node without design - just mark position
                    fig.add_trace(
                        go.Scatter(
                            x=[cx],
                            y=[cy],
                            mode="markers+text",
                            marker=dict(size=12, color="red", symbol="x"),
                            text=[node_name],
                            textposition="top center",
                            name=f"{node_name} (No design)",
                            showlegend=False,
                        )
                    )
                    all_x.append(cx)
                    all_y.append(cy)

            # Calculate plot bounds
            if all_x and all_y:
                margin = 2.0
                x_range = [min(all_x) - margin, max(all_x) + margin]
                y_range = [min(all_y) - margin, max(all_y) + margin]
            else:
                x_range = [-5, 20]
                y_range = [-5, 20]

            # Layout
            fig.update_layout(
                title=tool_input.title,
                xaxis=dict(
                    title="X (m)",
                    scaleanchor="y",
                    scaleratio=1,
                    range=x_range,
                    showgrid=True,
                    gridcolor="rgba(200, 200, 200, 0.3)",
                    griddash="dash",
                ),
                yaxis=dict(
                    title="Y (m)",
                    range=y_range,
                    showgrid=True,
                    gridcolor="rgba(200, 200, 200, 0.3)",
                    griddash="dash",
                ),
                plot_bgcolor="white",
                margin=dict(l=60, r=60, t=60, b=60),
            )

            return vkt.PlotlyResult(fig.to_json())

        except Exception as e:
            logger.exception(f"Error in footings_plot_view: {e}")
            # Return empty figure on error
            fig = go.Figure()
            fig.update_layout(title="Error loading footings plot")
            return vkt.PlotlyResult(fig.to_json())
