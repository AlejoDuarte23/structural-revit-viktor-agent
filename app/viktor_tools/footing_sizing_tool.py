"""Foundation pad sizing tool integration for the new VIKTOR app."""

import json
import logging
from typing import Any

from pydantic import BaseModel, Field, TypeAdapter

from .base import ViktorTool

logger = logging.getLogger(__name__)

SUPPORT_COORDINATES_STORAGE_KEY = "model_support_coordinates"
REACTION_LOADS_STORAGE_KEY = "model_reaction_loads"
FOOTING_SIZING_STORAGE_KEY = "footing_sizing_results"


class NodeCoordinate(BaseModel):
    """Single node coordinate entry."""

    node_name: str = Field(description="Node identifier")
    x: float = Field(default=0.0, description="X coordinate in meters")
    y: float = Field(default=0.0, description="Y coordinate in meters")
    z: float = Field(default=0.0, description="Z coordinate in meters")


class LoadCaseEntry(BaseModel):
    """Single load case entry for a node."""

    lc_name: str = Field(description="Load case name")
    node_name: str = Field(description="Node name this load case applies to")
    f1: float = Field(default=0.0, description="Force in X direction (kN)")
    f2: float = Field(default=0.0, description="Force in Y direction (kN)")
    f3: float = Field(default=0.0, description="Force in Z direction (kN)")
    m1: float = Field(default=0.0, description="Moment about X axis (kN.m)")
    m2: float = Field(default=0.0, description="Moment about Y axis (kN.m)")
    m3: float = Field(default=0.0, description="Moment about Z axis (kN.m)")


class SoilSection(BaseModel):
    """Sizing inputs."""

    q_allow: float = Field(
        default=150.0,
        description="Allowable bearing pressure in kN/m2",
    )
    gamma_c: float = Field(
        default=25.0,
        description="Concrete unit weight in kN/m3",
    )
    depth: float = Field(
        default=0.5,
        description="Foundation depth in meters",
    )
    b_min: float = Field(
        default=1.5,
        description="Minimum pad size in meters",
    )


class NodesSection(BaseModel):
    """Nodes table section."""

    nodes_table: list[NodeCoordinate] = Field(
        default_factory=list,
        description="Node coordinates table",
    )


class LoadCasesSection(BaseModel):
    """Load cases table section."""

    load_cases_table: list[LoadCaseEntry] = Field(
        default_factory=list,
        description="Load cases table",
    )


class FootingSizingInput(BaseModel):
    """Complete parametrization payload for the new sizing app."""

    soil: SoilSection = Field(default_factory=SoilSection)
    nodes_section: NodesSection = Field(default_factory=NodesSection)
    lc_section: LoadCasesSection = Field(default_factory=LoadCasesSection)


class FootingSizingResultEntry(BaseModel):
    """Single exported pad sizing result."""

    node_id: str
    B: float
    L: float
    x: float
    y: float
    z: float
    acting_bearing_pressure: float


class SupportCoordinateEntry(BaseModel):
    """Support coordinate row from storage."""

    Joint: str
    X: float
    Y: float
    Z: float


class ReactionLoadEntry(BaseModel):
    """Reaction load row from storage."""

    F1: float
    F2: float
    F3: float
    M1: float
    M2: float
    M3: float


class FootingSizingTool(ViktorTool):
    """Tool to run footing pad sizing via the VIKTOR app."""

    def __init__(
        self,
        sizing_input: FootingSizingInput,
        workspace_id: int = 2141,
        entity_id: int = 11536,
        method_name: str = "download_results",
    ):
        super().__init__(workspace_id, entity_id)
        self.sizing_input = sizing_input
        self.method_name = method_name

    def build_payload(self) -> dict[str, Any]:
        """Build the API payload matching the new app parametrization."""
        return {
            "method_name": self.method_name,
            "params": self.sizing_input.model_dump(mode="json"),
            "poll_result": True,
        }

    def run_and_download(self) -> list[dict[str, Any]]:
        """Run the job and download the JSON result."""
        job = self.run()
        return self.download_result(job)

    def run_and_parse(self) -> list[FootingSizingResultEntry]:
        """Run the job and parse the export list."""
        content = self.run_and_download()
        return TypeAdapter(list[FootingSizingResultEntry]).validate_python(content)


class FootingSizingFlatInput(BaseModel):
    """Agent-facing input. Nodes and loads are auto-loaded from storage."""

    q_allow: float = Field(
        default=150.0,
        description="Allowable bearing pressure in kN/m2",
    )
    gamma_c: float = Field(
        default=25.0,
        description="Concrete unit weight in kN/m3",
    )
    depth: float = Field(
        default=0.5,
        description="Foundation depth in meters",
    )
    b_min: float = Field(
        default=1.5,
        description="Minimum pad size in meters",
    )
    load_combinations_to_check: list[str] | str | None = Field(
        default=None,
        description=(
            "Load combinations to include. Pass a list, a single combination name, "
            "or leave empty to use all available combinations."
        ),
    )


def _load_storage_json(vkt: Any, key: str) -> Any:
    stored_file = vkt.Storage().get(key, scope="entity")
    if not stored_file:
        raise ValueError(f"Missing Viktor Storage key '{key}'.")
    return json.loads(stored_file.getvalue_binary().decode("utf-8"))


def _normalize_load_combinations(
    value: list[str] | str | None,
) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return value


async def calculate_footing_sizing_func(ctx: Any, args: str) -> str:
    """Auto-loads SAP2000 data from storage and runs foundation pad sizing."""
    flat_input = FootingSizingFlatInput.model_validate_json(args)

    try:
        import viktor as vkt
    except ImportError:
        return "❌ Viktor module not available - cannot access storage."

    try:
        support_coords_raw = _load_storage_json(vkt, SUPPORT_COORDINATES_STORAGE_KEY)
        reaction_loads_raw = _load_storage_json(vkt, REACTION_LOADS_STORAGE_KEY)
    except ValueError as e:
        return (
            "❌ Required SAP2000 storage data is missing.\n"
            "Please run these tools first:\n"
            "1. get_support_coordinates\n"
            "2. get_reaction_loads\n"
            f"Details: {e}"
        )
    except json.JSONDecodeError as e:
        logger.exception("Failed to parse SAP2000 storage data")
        return f"❌ Error parsing SAP2000 data from storage: {e}"
    except Exception as e:
        logger.exception("Unexpected error loading SAP2000 data")
        return f"❌ Error loading SAP2000 data from storage: {e}"

    support_coords = TypeAdapter(list[SupportCoordinateEntry]).validate_python(
        support_coords_raw
    )

    node_coords = [
        NodeCoordinate(
            node_name=support.Joint,
            x=support.X,
            y=support.Y,
            z=support.Z,
        )
        for support in support_coords
    ]

    combos_to_check = _normalize_load_combinations(
        flat_input.load_combinations_to_check
    )
    load_cases_list: list[LoadCaseEntry] = []

    for support in support_coords:
        node_name = support.Joint
        node_reactions_raw = reaction_loads_raw.get(node_name)
        if not isinstance(node_reactions_raw, dict):
            return (
                f"❌ No reaction data found for support node '{node_name}'.\n"
                "Please ensure get_reaction_loads extracted all support nodes."
            )

        available_combos = list(node_reactions_raw.keys())
        combo_names = combos_to_check or available_combos

        missing_combos = [combo for combo in combo_names if combo not in node_reactions_raw]
        if missing_combos:
            return (
                f"❌ Missing load combinations {missing_combos} for node '{node_name}'.\n"
                f"Available combinations: {', '.join(available_combos)}"
            )

        for combo_name in combo_names:
            reaction = ReactionLoadEntry.model_validate(node_reactions_raw[combo_name])
            load_cases_list.append(
                LoadCaseEntry(
                    lc_name=combo_name,
                    node_name=node_name,
                    f1=reaction.F1,
                    f2=reaction.F2,
                    f3=reaction.F3,
                    m1=reaction.M1,
                    m2=reaction.M2,
                    m3=reaction.M3,
                )
            )

    logger.info(
        "Prepared %s nodes and %s load cases for foundation pad sizing",
        len(node_coords),
        len(load_cases_list),
    )

    sizing_input = FootingSizingInput(
        soil=SoilSection(
            q_allow=flat_input.q_allow,
            gamma_c=flat_input.gamma_c,
            depth=flat_input.depth,
            b_min=flat_input.b_min,
        ),
        nodes_section=NodesSection(nodes_table=node_coords),
        lc_section=LoadCasesSection(load_cases_table=load_cases_list),
    )

    try:
        tool = FootingSizingTool(sizing_input=sizing_input)
        result = tool.run_and_parse()
    except Exception as e:
        logger.exception("Footing sizing request failed")
        return f"❌ Error running footing sizing tool: {type(e).__name__}: {e}"

    try:
        result_data = [entry.model_dump(mode="json") for entry in result]
        vkt.Storage().set(
            FOOTING_SIZING_STORAGE_KEY,
            data=vkt.File.from_data(json.dumps(result_data, indent=2)),
            scope="entity",
        )
        logger.info("Stored footing sizing results in storage")
    except Exception as e:
        logger.warning("Failed to store footing sizing results: %s", e)

    if not result:
        return "Footing sizing completed, but no governing pad sizing results were returned."

    design_summaries = [
        {
            "node": entry.node_id,
            "pad_m": f"{entry.B:.2f}x{entry.L:.2f}",
            "xyz_m": [round(entry.x, 3), round(entry.y, 3), round(entry.z, 3)],
            "acting_bearing_pressure": round(entry.acting_bearing_pressure, 3),
        }
        for entry in result
    ]

    combo_msg = (
        f" using load combinations: {combos_to_check}"
        if combos_to_check
        else " using all available load combinations"
    )

    return (
        f"✅ Footing sizing completed successfully{combo_msg}. "
        f"Generated governing pad sizing for {len(result)} nodes.\n\n"
        f"Results: {json.dumps({'nodes_sized': len(result), 'designs': design_summaries}, indent=2)}"
    )


def calculate_footing_sizing_tool() -> Any:
    """Create the footing sizing function tool for the agent."""
    from agents import FunctionTool

    return FunctionTool(
        name="calculate_footing_sizing",
        description=(
            "Run the new foundation pad sizing app. "
            "Automatically loads node coordinates from 'model_support_coordinates' "
            "and reaction loads from 'model_reaction_loads', then sends them to the VIKTOR app "
            "using the new parametrization with soil.q_allow, soil.gamma_c, soil.depth, soil.b_min, "
            "nodes_section.nodes_table, and lc_section.load_cases_table. "
            "Stores the exported governing pad sizing list in Viktor Storage with key 'footing_sizing_results'.\n\n"
            "PREREQUISITES:\n"
            "- Must run 'get_support_coordinates' first\n"
            "- Must run 'get_reaction_loads' first\n\n"
            "OPTIONAL PARAMETERS:\n"
            "- q_allow: allowable bearing pressure in kN/m2\n"
            "- gamma_c: concrete unit weight in kN/m3\n"
            "- depth: foundation depth in m\n"
            "- b_min: minimum pad size in m\n"
            "- load_combinations_to_check: restrict to specific combos or use all by default\n\n"
            "URL: https://demo.viktor.ai/workspaces/2141/app/editor/11536"
        ),
        params_json_schema=FootingSizingFlatInput.model_json_schema(),
        on_invoke_tool=calculate_footing_sizing_func,
    )
