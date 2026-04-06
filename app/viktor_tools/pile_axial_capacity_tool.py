"""Pile axial capacity app integration following the footing sizing pattern."""

import json
import logging
from typing import Any

from pydantic import BaseModel, Field, TypeAdapter

from .base import ViktorTool

logger = logging.getLogger(__name__)

SUPPORT_COORDINATES_STORAGE_KEY = "model_support_coordinates"
REACTION_LOADS_STORAGE_KEY = "model_reaction_loads"
PILE_AXIAL_CAPACITY_STORAGE_KEY = "pile_axial_capacity_results"


class NodeEntry(BaseModel):
    """Single node coordinate entry for the remote app."""

    node_name: str = Field(description="Node identifier")
    x: float = Field(default=0.0, description="X coordinate in meters")
    y: float = Field(default=0.0, description="Y coordinate in meters")
    z: float = Field(default=0.0, description="Z coordinate in meters")


class ReactionLoadCaseEntry(BaseModel):
    """Single reaction load row for the remote app."""

    case_name: str = Field(description="Load case or load combination name")
    node: str = Field(description="Node identifier")
    F1: float = Field(default=0.0, description="Force in X direction (kN)")
    F2: float = Field(default=0.0, description="Force in Y direction (kN)")
    F3: float = Field(default=0.0, description="Force in Z direction (kN)")
    M1: float = Field(default=0.0, description="Moment about X axis (kN-m)")
    M2: float = Field(default=0.0, description="Moment about Y axis (kN-m)")
    M3: float = Field(default=0.0, description="Moment about Z axis (kN-m)")


class NodesSection(BaseModel):
    """Nodes section matching the VIKTOR parametrization."""

    nodes: list[NodeEntry] = Field(default_factory=list)


class ReactionLoadsSection(BaseModel):
    """Reaction loads section matching the VIKTOR parametrization."""

    load_cases: list[ReactionLoadCaseEntry] = Field(default_factory=list)


class PileSection(BaseModel):
    pile_diameter: float = Field(default=450.0, description="Pile diameter in mm")
    pile_length: float = Field(default=8000.0, description="Pile length in mm")
    pile_centres_horizontal: float = Field(
        default=1350.0,
        description="Horizontal spacing between the two bottom piles in mm",
    )
    pile_centres_vertical: float = Field(
        default=1350.0,
        description="Vertical spacing between top and bottom piles in mm",
    )


class CapSection(BaseModel):
    pile_cap_thickness: float = Field(
        default=750.0,
        description="Pile cap thickness in mm",
    )
    clearance: float = Field(
        default=375.0,
        description="Offset from pile centre to cap edge in mm",
    )
    width_indent: float = Field(
        default=500.0,
        description="Horizontal indent on each side at the top in mm",
    )
    length2: float = Field(
        default=750.0,
        description="Straight side length before the slope starts in mm",
    )
    column_size: float = Field(
        default=500.0,
        description="Equivalent square column or pedestal size in mm",
    )
    clear_cover: float = Field(
        default=75.0,
        description="Bottom clear cover in mm",
    )
    bar_diameter: float = Field(
        default=25.0,
        description="Main bar diameter in mm",
    )


class SoilSection(BaseModel):
    soil_name: str = Field(default="Medium Dense Sand", description="Soil label")
    unit_weight: float = Field(
        default=18.0,
        description="Soil unit weight in kN/m^3",
    )
    friction_angle: float = Field(
        default=32.0,
        description="Soil friction angle in degrees",
    )
    factor_of_safety: float = Field(
        default=2.5,
        description="Global factor of safety",
    )
    soil_notes: str = Field(default="", description="Additional soil notes")


class ConcreteSection(BaseModel):
    concrete_strength: float = Field(
        default=30.0,
        description="Concrete strength f'c in MPa",
    )
    phi_shear: float = Field(
        default=0.75,
        description="Shear reduction factor",
    )


class PileAxialCapacityInput(BaseModel):
    """Full remote-app parametrization payload."""

    nodes_section: NodesSection = Field(default_factory=NodesSection)
    reaction_loads_section: ReactionLoadsSection = Field(
        default_factory=ReactionLoadsSection
    )
    pile_section: PileSection = Field(default_factory=PileSection)
    cap_section: CapSection = Field(default_factory=CapSection)
    soil_section: SoilSection = Field(default_factory=SoilSection)
    concrete_section: ConcreteSection = Field(default_factory=ConcreteSection)


class SupportCoordinateEntry(BaseModel):
    """Support coordinate row loaded from SAP storage."""

    Joint: str
    X: float
    Y: float
    Z: float


class ReactionLoadEntry(BaseModel):
    """Reaction row loaded from SAP storage."""

    F1: float
    F2: float
    F3: float
    M1: float
    M2: float
    M3: float


class PileCapExportParameters(BaseModel):
    """Parsed JSON export parameters returned by download_results."""

    foundationThickness: float
    widthIndent: float
    pileLength: float
    pileDiameter: float
    pileCentresVertical: float
    pileCentresHorizontal: float
    length1: float
    length2: float
    pileCutOut: float
    clearance: float


class PileCapPlacement(BaseModel):
    """Single cap placement returned by download_results."""

    x: float
    y: float
    z: float


class PileAxialCapacityOutput(BaseModel):
    """Parsed JSON export structure returned by the remote app."""

    parameters: PileCapExportParameters
    placements: list[PileCapPlacement]


class PileAxialCapacityTool(ViktorTool):
    """Tool to run the pile axial capacity app via the VIKTOR job API."""

    def __init__(
        self,
        pile_input: PileAxialCapacityInput,
        workspace_id: int = 2232,
        entity_id: int = 11640,
        method_name: str = "download_results",
    ):
        super().__init__(workspace_id, entity_id)
        self.pile_input = pile_input
        self.method_name = method_name

    def build_payload(self) -> dict[str, Any]:
        return {
            "method_name": self.method_name,
            "params": self.pile_input.model_dump(mode="json"),
            "poll_result": True,
        }

    def run_and_download(self) -> dict[str, Any]:
        job = self.run()
        return self.download_result(job)

    def run_and_parse(self) -> PileAxialCapacityOutput:
        content = self.run_and_download()
        return PileAxialCapacityOutput.model_validate(content)


class PileAxialCapacityFlatInput(BaseModel):
    """Agent-facing input. Nodes and reaction tables are auto-loaded from storage."""

    pile_diameter: float = Field(default=450.0, description="Pile diameter in mm")
    pile_length: float = Field(default=8000.0, description="Pile length in mm")
    pile_centres_horizontal: float = Field(
        default=1350.0,
        description="Horizontal pile spacing in mm",
    )
    pile_centres_vertical: float = Field(
        default=1350.0,
        description="Vertical pile spacing in mm",
    )
    pile_cap_thickness: float = Field(
        default=750.0,
        description="Pile cap thickness in mm",
    )
    clearance: float = Field(
        default=375.0,
        description="Offset from pile centre to cap edge in mm",
    )
    width_indent: float = Field(
        default=500.0,
        description="Top width indent in mm",
    )
    length2: float = Field(
        default=750.0,
        description="Straight side length before the slope starts in mm",
    )
    column_size: float = Field(
        default=500.0,
        description="Equivalent square column or pedestal size in mm",
    )
    clear_cover: float = Field(
        default=75.0,
        description="Bottom clear cover in mm",
    )
    bar_diameter: float = Field(
        default=25.0,
        description="Main bar diameter in mm",
    )
    soil_name: str = Field(default="Medium Dense Sand", description="Soil label")
    unit_weight: float = Field(
        default=18.0,
        description="Soil unit weight in kN/m^3",
    )
    friction_angle: float = Field(
        default=32.0,
        description="Soil friction angle in degrees",
    )
    factor_of_safety: float = Field(
        default=2.5,
        description="Global factor of safety",
    )
    soil_notes: str = Field(default="", description="Additional soil notes")
    concrete_strength: float = Field(
        default=30.0,
        description="Concrete strength f'c in MPa",
    )
    phi_shear: float = Field(
        default=0.75,
        description="Shear reduction factor",
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


async def calculate_pile_axial_capacity_func(ctx: Any, args: str) -> str:
    """Auto-load SAP storage, call the remote pile app, and store parsed JSON output."""
    flat_input = PileAxialCapacityFlatInput.model_validate_json(args)

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
    node_entries = [
        NodeEntry(node_name=support.Joint, x=support.X, y=support.Y, z=support.Z)
        for support in support_coords
    ]

    combos_to_check = _normalize_load_combinations(
        flat_input.load_combinations_to_check
    )
    load_case_entries: list[ReactionLoadCaseEntry] = []

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
            load_case_entries.append(
                ReactionLoadCaseEntry(
                    case_name=combo_name,
                    node=node_name,
                    F1=reaction.F1,
                    F2=reaction.F2,
                    F3=reaction.F3,
                    M1=reaction.M1,
                    M2=reaction.M2,
                    M3=reaction.M3,
                )
            )

    pile_input = PileAxialCapacityInput(
        nodes_section=NodesSection(nodes=node_entries),
        reaction_loads_section=ReactionLoadsSection(load_cases=load_case_entries),
        pile_section=PileSection(
            pile_diameter=flat_input.pile_diameter,
            pile_length=flat_input.pile_length,
            pile_centres_horizontal=flat_input.pile_centres_horizontal,
            pile_centres_vertical=flat_input.pile_centres_vertical,
        ),
        cap_section=CapSection(
            pile_cap_thickness=flat_input.pile_cap_thickness,
            clearance=flat_input.clearance,
            width_indent=flat_input.width_indent,
            length2=flat_input.length2,
            column_size=flat_input.column_size,
            clear_cover=flat_input.clear_cover,
            bar_diameter=flat_input.bar_diameter,
        ),
        soil_section=SoilSection(
            soil_name=flat_input.soil_name,
            unit_weight=flat_input.unit_weight,
            friction_angle=flat_input.friction_angle,
            factor_of_safety=flat_input.factor_of_safety,
            soil_notes=flat_input.soil_notes,
        ),
        concrete_section=ConcreteSection(
            concrete_strength=flat_input.concrete_strength,
            phi_shear=flat_input.phi_shear,
        ),
    )

    try:
        tool = PileAxialCapacityTool(pile_input=pile_input)
        result = tool.run_and_parse()
    except Exception as e:
        logger.exception("Pile axial capacity request failed")
        return f"❌ Error running pile axial capacity tool: {type(e).__name__}: {e}"

    try:
        vkt.Storage().set(
            PILE_AXIAL_CAPACITY_STORAGE_KEY,
            data=vkt.File.from_data(
                json.dumps(result.model_dump(mode="json"), indent=2)
            ),
            scope="entity",
        )
        logger.info("Stored pile axial capacity export in storage")
    except Exception as e:
        logger.warning("Failed to store pile axial capacity export: %s", e)

    combo_msg = (
        f" using load combinations: {combos_to_check}"
        if combos_to_check
        else " using all available load combinations"
    )

    return (
        f"✅ Pile axial capacity export completed successfully{combo_msg}. "
        f"Prepared {len(result.placements)} placements. "
        f"Export pile length: {result.parameters.pileLength:.0f} mm. "
        f"Stored parsed JSON in Viktor Storage with key '{PILE_AXIAL_CAPACITY_STORAGE_KEY}'.\n\n"
        f"Results: {json.dumps(result.model_dump(mode='json'), indent=2)}"
    )


def calculate_pile_axial_capacity_tool() -> Any:
    """Create the function tool for the remote pile axial capacity app."""
    from agents import FunctionTool

    return FunctionTool(
        name="calculate_pile_axial_capacity",
        description=(
            "Run the pile axial capacity VIKTOR app and export its JSON layout result. "
            "Automatically loads node coordinates from 'model_support_coordinates' "
            "and reaction loads from 'model_reaction_loads', then sends them to the remote VIKTOR app "
            "using the nested parametrization with nodes_section.nodes, "
            "reaction_loads_section.load_cases, pile_section, cap_section, soil_section, "
            "and concrete_section. Stores the parsed exported JSON in Viktor Storage "
            "with key 'pile_axial_capacity_results'.\n\n"
            "PREREQUISITES:\n"
            "- Must run 'get_support_coordinates' first\n"
            "- Must run 'get_reaction_loads' first\n\n"
            "OPTIONAL PARAMETERS:\n"
            "- pile geometry: pile_diameter, pile_length, pile_centres_horizontal, pile_centres_vertical\n"
            "- cap geometry: pile_cap_thickness, clearance, width_indent, length2, column_size, clear_cover, bar_diameter\n"
            "- soil: soil_name, unit_weight, friction_angle, factor_of_safety, soil_notes\n"
            "- concrete: concrete_strength, phi_shear\n"
            "- load_combinations_to_check: restrict to specific combos or use all by default\n\n"
            "URL: https://demo.viktor.ai/workspaces/2232/app/editor/11640"
        ),
        params_json_schema=PileAxialCapacityFlatInput.model_json_schema(),
        on_invoke_tool=calculate_pile_axial_capacity_func,
    )
