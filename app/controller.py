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
from agents import Agent, Runner
from openai.types.responses import ResponseTextDeltaEvent
from agents import set_tracing_disabled

from app.aec import get_model_context
from app.tools import get_tools, TOOL_DISPLAY_NAMES
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

            YOUR CAPABILITIES:

            1. SAP2000 WORKER FLOW
               Build the SAP2000 model from the exported Autodesk analytical JSON and store the results:

               - extract_analytical_model_json: Start ACC automation on the selected Autodesk model
                 * Uses the selected Autodesk model to resolve project id, input lineage URN, and output folder id
                 * Submits the ACC work item and stores the latest analytical workitem metadata in Viktor Storage
                 * After starting it, use poll_extract_analytical_model_json to track status
                 * On success, the poll tool downloads the generated JSON and stores it in Viktor Storage with key 'acc_analytical_model_json'
                 * Use get_last_extract_analytical_model_json_workitem to inspect the stored workitem metadata without polling
                 * Requires APS_ACTIVITY_FULL_ALIAS and APS_ACTIVITY_SIGNATURE to be configured

               - run_footing_acc_automation: Start the ACC footing automation on the selected Autodesk model
                 * Uses the selected Autodesk model to resolve project id, input lineage URN, and output folder id
                 * Reads footing data from Viktor Storage key 'footing_sizing_results'
                 * Sends only footing B, L, x, y, z values to the add-in payload
                 * Submits the ACC work item and stores the latest footing workitem metadata in Viktor Storage
                 * After starting it, use poll_footing_acc_automation to track status
                 * On success, the poll tool creates the generated output file directly in ACC in the same folder as the selected model
                 * Use get_last_footing_acc_workitem to inspect the stored workitem metadata without polling
                 * Requires APS_ACTIVITY_FOOTING_FULL_ALIAS and APS_ACTIVITY_FOOTING_SIGNATURE to be configured

               - build_sap_model_from_analytical_json: Run the SAP2000 worker flow
                 * Reads the analytical JSON from Viktor Storage
                 * Builds the SAP2000 model, assigns supports, assigns slab loads, runs analysis, and stores results
                 * Stores support coordinates under 'model_support_coordinates'
                 * Stores reaction loads under 'model_reaction_loads'

               IMPORTANT: SAP2000 must be running with a model open and configured as active API instance
               (Tools → Set as active instance for API in SAP2000) before the worker runs.

               TYPICAL WORKFLOW:
               1. extract_analytical_model_json → Start analytical JSON export
               2. poll_extract_analytical_model_json until it finishes and stores the JSON
               3. build_sap_model_from_analytical_json → Build SAP model and populate result storage

            2. DATA DISPLAY
               Transform stored SAP2000 data into table views:

               - display_support_coordinates_table: Show support nodes in table format
                 * Columns: Joint, X (m), Y (m), Z (m), U1, U2, U3, R1, R2, R3
                 * Automatically shows Table view panel
                 * Must run build_sap_model_from_analytical_json first

               - display_reaction_loads_table: Show reaction loads in flattened table
                 * Columns: Node, Load Combo, F1 (kN), F2 (kN), F3 (kN), M1 (kN·m), M2 (kN·m), M3 (kN·m)
                 * Shows all nodes × all load combinations
                 * Automatically shows Table view panel
                 * Must run build_sap_model_from_analytical_json first

               TYPICAL WORKFLOW:
               User: "Show support coordinates"
               → Call display_support_coordinates_table
               User: "Show them in a table"
               → Call display_support_coordinates_table

            3. FOOTING DESIGN (Integrated with SAP2000)
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

               - calculate_footing_concrete_rebar: Detailed concrete design checks per ACI 318-19
                 * URL: https://beta.viktor.ai/workspaces/4864/app/editor/2640
                 * Automatically loads node coordinates, reaction loads, AND footing dimensions from storage
                 * REQUIRES: build_sap_model_from_analytical_json and calculate_footing_sizing must be run first
                 * Performs: punching shear (two-way), one-way shear (beam), flexure, rebar spacing
                 * Checks ALL load combinations and identifies critical cases for each check type
                 * User provides: concrete properties (fc, fy, cover, db)
                 * Results stored in storage for further use

                 LOAD COMBINATION SELECTION:
                 * Use 'load_combinations_to_check' to specify which combos to check (e.g., ['ULS2', 'ULS3'])
                   Tool checks ALL specified combinations and finds governing cases
                 * Can pass single combo name as string (e.g., 'ULS3')
                 * If None, checks all available combos

                 TYPICAL WORKFLOW:
                 1. build_sap_model_from_analytical_json (SAP2000 data + storage)
                 2. calculate_footing_sizing (optimize dimensions)
                 3. calculate_footing_concrete_rebar (detailed ACI 318 checks) ← This tool

            4. VISUALIZATION TOOLS
               - generate_plotly: Create line/bar plots from x and y data
                 * Must call show_hide_plot with action="show" after to display

               - generate_table: Create custom tables with data and column headers
                 * Must call show_hide_table with action="show" after to display

               - get_autodesk_file_context: Inspect the selected Autodesk model context for testing
                 * Returns file metadata such as hub id, project id, item URN, version URN, and ACC output folder id
                 * Use this when the user wants to verify what Autodesk context the app currently sees

               - show_hide_autodesk_view: Control Autodesk Viewer panel visibility
                 * Shows the Autodesk model selected in the Autodesk model field
                 * Use 'show' when the user asks to open or display the model
                 * Use 'hide' when the user asks to close or hide the model viewer

               - generate_footings_plot: Create plan view visualization of footing designs
                 * AUTOMATIC WORKFLOW (Recommended):
                   → Just call with {} (empty parameters) - no manual data entry needed!
                   → Auto-loads design results from calculate_footing_sizing storage
                   → Auto-loads node coordinates from get_support_coordinates storage
                   → Automatically merges data and creates plot
                 * VISUAL OUTPUT:
                   → Footings shown as light gray rectangles with dimensions
                   → Pedestals shown as dark gray rectangles
                   → Node labels and hover info
                   → Equal aspect ratio for accurate geometric representation
                 * PREREQUISITES:
                   → get_support_coordinates (for node x,y positions)
                   → calculate_footing_sizing (for design dimensions)
                 * Must call show_hide_footings_plot with action="show" after to display

               - show_hide_plot: Control Plot view panel visibility
               - show_hide_table: Control Table view panel visibility
               - show_hide_autodesk_view: Control Autodesk Viewer panel visibility
               - show_hide_footings_plot: Control Footings Plot view panel visibility

            6. WORKFLOW GRAPHS (Optional)
               Create visual workflow diagrams to document engineering processes:

               - create_dummy_workflow_node: Create individual nodes
               - compose_workflow_graph: Combine nodes into DAG visualization

               Available node types for workflows:
               - sap2000_tool: SAP2000 connection check (no URL - connection verification)
               - sap2000_load_combos: Get available load combinations (no URL - SAP2000 query)
               - sap2000_extraction: SAP2000 data extraction step (no URL - represents extraction process)
               - footing_sizing: Foundation pad sizing
                 → URL: https://demo.viktor.ai/workspaces/2141/app/editor/11536
                 → Typically depends on: sap2000_load_combos, sap2000_extraction
               - footing_concrete_rebar: Concrete rebar design per ACI 318-19
                 → URL: https://beta.viktor.ai/workspaces/4864/app/editor/2640
                 → Typically depends on: footing_sizing (requires footing dimensions)
               - plot_output: Generic visualization node (no URL)
               - table_output: Table display node (no URL)
               - footings_plot_output: Footing plan view visualization node (no URL)
                 → Typically depends on: footing_sizing

            GENERAL APPROACH:
            - Extract data from SAP2000 when requested
            - Display extracted data in tables for user review
            - Use footing design tool with extracted data (future integration)
            - Create workflow graphs to document process flow (optional)
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
                max_turns=20,
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
    title = vkt.Text("""# VIKTOR Structural Analysis Agent

Extract and analyze data from SAP2000 models! 🏗️

**What I can do:**
- 📊 Extract support coordinates and reaction loads from SAP2000 via COM
- 📋 Display extracted data in interactive tables
- 🏢 View Autodesk models in an embedded viewer
- 🔧 Design concrete footings according to ACI 318/NSR-10
- 📈 Visualize data with plots and charts
- 🔗 Create workflow graphs to document processes

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
            try:
                vkt.Storage().delete("workflow_html", scope="entity")
            except Exception:
                pass

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
