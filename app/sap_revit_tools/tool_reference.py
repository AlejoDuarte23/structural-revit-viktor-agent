"""Single-file tool to build a SAP2000 model from stored analytical JSON and save reactions."""

from __future__ import annotations

import json
import logging
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

ANALYTICAL_MODEL_STORAGE_KEY = "acc_analytical_model_json"
SUPPORT_COORDINATES_STORAGE_KEY = "model_support_coordinates"
REACTION_LOADS_STORAGE_KEY = "model_reaction_loads"
SAP_PROGID = "CSI.SAP2000.API.SapObject"
DEFAULT_SAVE_MODEL_PATH = None

CSI_LOADPATTERN_DEAD = 1
CSI_LOADPATTERN_LIVE = 3
CSI_COMBO_LINEAR_ADDITIVE = 0
CSI_CNAME_LOADCASE = 0
CSI_ITEMTYPE_OBJECTS = 0
CSI_DIR_GLOBAL_Z = 6
LOAD_COMPONENT_TOLERANCE = 1e-9
DEFAULT_SUPPORT_RESTRAINT = [1, 1, 1, 1, 1, 1]
DEFAULT_SAP_UNITS = 6
DEFAULT_STEEL_MATERIAL = "S355"
DEFAULT_CONCRETE_MATERIAL = "C30"
DEFAULT_SLAB_THICKNESS_M = 0.15
DEFAULT_SUPPORT_POLICY = "from_payload_or_lowest_z_fixed"
DEFAULT_APPLY_SUPPORTS = True
DEFAULT_APPLY_LOADS = True
DEFAULT_RUN_ANALYSIS = True
DEFAULT_INITIALIZE_BLANK_MODEL = True
DEFAULT_DEAD_SELF_WEIGHT_MULTIPLIER = 0.0
DEFAULT_DEAD_CASE_NAME = "DL"
DEFAULT_LIVE_CASE_NAME = "LL"
DEFAULT_DESIGN_COMBOS: list[tuple[str, dict[str, float]]] = [
    ("DL_PLUS_LL", {"DL": 1.0, "LL": 1.0}),
    ("1.2DL_1.5LL", {"DL": 1.2, "LL": 1.5}),
    ("DL_0.6LL", {"DL": 1.0, "LL": 0.6}),
]


class SourceRefModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: str
    revit_element_id: int | None = None
    revit_unique_id: str | None = None


class SupportSpecModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    restraint: list[int]

    @field_validator("restraint", mode="before")
    @classmethod
    def validate_restraint(cls, value: Any) -> list[int]:
        if isinstance(value, dict):
            keys = ("U1", "U2", "U3", "R1", "R2", "R3")
            return [int(value.get(key, 0)) for key in keys]

        if isinstance(value, (list, tuple)):
            if len(value) != 6:
                raise ValueError("support restraint must contain 6 values")
            return [int(item) for item in value]

        raise ValueError("support restraint must be a 6-item list or a U1..R3 object")


class NodeMetadataModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    coord_key: str | None = None
    source_refs: list[SourceRefModel] = Field(default_factory=list)
    support: SupportSpecModel | None = None


class LineMetadataModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    revit_element_id: int | None = None
    revit_unique_id: str | None = None
    structural_role: str | None = None
    material_name: str | None = None
    section_shape: str | None = None
    cross_section_rotation_deg: float | None = None
    physical_element_id: int | None = None
    physical_unique_id: str | None = None


class ForceVectorModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class AreaUniformLoadModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: str
    load_case_name: str
    load_category_name: str | None = None
    load_nature_name: str | None = None
    is_hosted: bool | None = None
    is_constrained_on_host: bool | None = None
    is_projected: bool | None = None
    orient_to: str | None = None
    force_vector_global_kn_per_m2: ForceVectorModel
    num_reference_points: int | None = None
    host_element_id: int | None = None
    host_unique_id: str | None = None

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str) -> str:
        if value != "area_uniform":
            raise ValueError("only 'area_uniform' area loads are supported")
        return value


class AreaMetadataModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    revit_element_id: int | None = None
    revit_unique_id: str | None = None
    structural_role: str | None = None
    material_name: str | None = None
    thickness_mm: float | None = None
    physical_element_id: int | None = None
    physical_unique_id: str | None = None
    openings: list[dict[str, Any]] = Field(default_factory=list)
    loads: list[AreaUniformLoadModel] = Field(default_factory=list)


class ExportNodeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: int
    x: float
    y: float
    z: float
    metadata: NodeMetadataModel | None = None


class ExportLineModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_id: int
    Ni: int
    Nj: int
    section: str
    type: str
    metadata: LineMetadataModel | None = None


class ExportAreaModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    area_id: int
    nodes: list[int]
    section: str
    type: str
    metadata: AreaMetadataModel | None = None

    @field_validator("nodes")
    @classmethod
    def validate_nodes(cls, value: list[int]) -> list[int]:
        if len(value) < 3:
            raise ValueError("area must have at least 3 nodes")
        return value


class RevitAnalyticalSapImportModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[ExportNodeModel]
    lines: list[ExportLineModel]
    areas: list[ExportAreaModel]

    @model_validator(mode="after")
    def validate_references(self) -> "RevitAnalyticalSapImportModel":
        node_ids = [node.node_id for node in self.nodes]
        line_ids = [line.line_id for line in self.lines]
        area_ids = [area.area_id for area in self.areas]

        if len(node_ids) != len(set(node_ids)):
            raise ValueError("node_id values must be unique")
        if len(line_ids) != len(set(line_ids)):
            raise ValueError("line_id values must be unique")
        if len(area_ids) != len(set(area_ids)):
            raise ValueError("area_id values must be unique")

        node_id_set = set(node_ids)
        for line in self.lines:
            if line.Ni not in node_id_set or line.Nj not in node_id_set:
                raise ValueError(f"line {line.line_id} references a missing node")

        for area in self.areas:
            missing = [node_id for node_id in area.nodes if node_id not in node_id_set]
            if missing:
                raise ValueError(f"area {area.area_id} references missing nodes: {missing}")

        return self


class BuildSapFromAnalyticalModelArgs(BaseModel):
    """No-input tool schema."""

    model_config = ConfigDict(extra="forbid")


class Sap2000Session:
    def __init__(self) -> None:
        self.helper = None
        self.SapObject = None
        self.SapModel = None
        self._pythoncom = None

    def __enter__(self) -> "Sap2000Session":
        try:
            import pythoncom
            import win32com.client as win32
        except ImportError as exc:
            raise RuntimeError(
                "pywin32 is required to connect to SAP2000. Install pywin32 in the runtime environment."
            ) from exc

        self._pythoncom = pythoncom
        pythoncom.CoInitialize()
        try:
            self.helper = win32.Dispatch("SAP2000v1.Helper")
            self.SapObject = self.helper.GetObject(SAP_PROGID)
            if self.SapObject is None:
                raise RuntimeError(
                    "Could not attach. In SAP2000 use: Tools -> Set as active instance for API. "
                    "Also ensure SAP2000 and Python run with the same admin level and are 64-bit."
                )
            self.SapModel = self.SapObject.SapModel
            if self.SapModel is None:
                raise RuntimeError("Attached SapObject has SapModel=None.")
            return self
        except Exception:
            pythoncom.CoUninitialize()
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.SapModel = None
            self.SapObject = None
            self.helper = None
        finally:
            if self._pythoncom is not None:
                self._pythoncom.CoUninitialize()
                self._pythoncom = None


def _parse_getnamelist_result(result: Any) -> tuple[list[str], int]:
    if not isinstance(result, tuple):
        raise RuntimeError(f"GetNameList returned non-tuple: {type(result)} {result}")

    ints = [value for value in result if isinstance(value, int)]
    lists = [value for value in result if isinstance(value, (list, tuple))]
    if not ints or not lists:
        raise RuntimeError(f"Could not parse GetNameList return: {result}")

    ret = 0 if 0 in ints else int(ints[-1])
    names = max(lists, key=len)
    return [str(name) for name in names], ret


def _read_name_list(getter: Any, *args: Any) -> list[str]:
    try:
        names, ret = _parse_getnamelist_result(getter(*args))
    except Exception:
        names, ret = _parse_getnamelist_result(getter())

    if ret != 0:
        raise RuntimeError(f"GetNameList failed (ret={ret})")
    return names


def get_all_point_names(SapModel: Any) -> list[str]:
    return _read_name_list(SapModel.PointObj.GetNameList, 0, [])


def get_all_load_cases(SapModel: Any) -> list[str]:
    return _read_name_list(SapModel.LoadCases.GetNameList, 0, [])


def get_all_load_combos(SapModel: Any) -> list[str]:
    return _read_name_list(SapModel.RespCombo.GetNameList, 0, [])


def get_all_load_patterns(SapModel: Any) -> list[str]:
    return _read_name_list(SapModel.LoadPatterns.GetNameList, 0, [])


def get_all_frame_section_names(SapModel: Any) -> list[str]:
    return _read_name_list(SapModel.PropFrame.GetNameList, 0, [])


def run_analysis(SapModel: Any) -> None:
    ret = SapModel.Analyze.RunAnalysis()
    if ret != 0:
        raise RuntimeError(f"Analyze.RunAnalysis failed (ret={ret})")


def _build_temp_model_path() -> Path:
    temp_dir = Path(tempfile.gettempdir()) / "sap2000-analytical-models"
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir / f"analytical-model-{uuid.uuid4().hex}.sdb"


def save_model(SapModel: Any, target_path: str | Path) -> Path:
    save_path = Path(target_path).resolve()
    save_path.parent.mkdir(parents=True, exist_ok=True)
    ret = SapModel.File.Save(str(save_path))
    if ret != 0:
        raise RuntimeError(f"SapModel.File.Save failed for {save_path} (ret={ret})")
    return save_path


def ensure_blank_model(SapModel: Any, units: int = 6) -> None:
    ret = SapModel.InitializeNewModel(units)
    if ret != 0:
        raise RuntimeError(f"InitializeNewModel failed (ret={ret})")

    ret = SapModel.File.NewBlank()
    if ret != 0:
        raise RuntimeError(f"File.NewBlank failed (ret={ret})")


def define_steel_material(
    SapModel: Any,
    material_name: str,
    e_modulus: float = 210000000.0,
    poisson_ratio: float = 0.3,
    thermal_coefficient: float = 1.2e-5,
) -> str:
    ret = SapModel.PropMaterial.SetMaterial(material_name, 1)
    if ret != 0:
        raise RuntimeError(f"PropMaterial.SetMaterial failed for {material_name} (ret={ret})")

    ret = SapModel.PropMaterial.SetMPIsotropic(
        material_name,
        e_modulus,
        poisson_ratio,
        thermal_coefficient,
    )
    if ret != 0:
        raise RuntimeError(f"PropMaterial.SetMPIsotropic failed for {material_name} (ret={ret})")
    return material_name


def define_concrete_material(
    SapModel: Any,
    material_name: str,
    e_modulus: float = 25000000.0,
    poisson_ratio: float = 0.2,
    thermal_coefficient: float = 1.0e-5,
) -> str:
    ret = SapModel.PropMaterial.SetMaterial(material_name, 2)
    if ret != 0:
        raise RuntimeError(f"PropMaterial.SetMaterial failed for {material_name} (ret={ret})")

    ret = SapModel.PropMaterial.SetMPIsotropic(
        material_name,
        e_modulus,
        poisson_ratio,
        thermal_coefficient,
    )
    if ret != 0:
        raise RuntimeError(f"PropMaterial.SetMPIsotropic failed for {material_name} (ret={ret})")
    return material_name


def define_slab_area_section(
    SapModel: Any,
    section_name: str,
    material_name: str,
    thickness: float,
) -> str:
    shell_type_shell_thin = 1
    color = 0

    if hasattr(SapModel.PropArea, "SetShell_1"):
        ret = SapModel.PropArea.SetShell_1(
            section_name,
            shell_type_shell_thin,
            True,
            material_name,
            color,
            thickness,
            thickness,
        )
        if ret != 0:
            raise RuntimeError(f"PropArea.SetShell_1 failed for {section_name} (ret={ret})")
        return section_name

    if hasattr(SapModel.PropArea, "SetShell"):
        ret = SapModel.PropArea.SetShell(
            section_name,
            shell_type_shell_thin,
            material_name,
            color,
            thickness,
            thickness,
        )
        if ret != 0:
            raise RuntimeError(f"PropArea.SetShell failed for {section_name} (ret={ret})")
        return section_name

    raise RuntimeError("SAP2000 did not expose a supported shell area-property initializer.")


def _database_section_label(section_label: str) -> str:
    return section_label.strip().replace(" ", "")


def _section_database_candidates(section_label: str) -> list[str]:
    normalized = section_label.strip().upper()
    if normalized.startswith(("UB", "UC")):
        return ["BSShapes2006.pro", "BSShapes.pro"]
    return ["BSShapes2006.pro", "BSShapes.pro", "AISC15.pro", "AISC14.pro"]


def import_frame_section_from_label(
    SapModel: Any,
    section_label: str,
    material_name: str,
) -> str:
    section_name = _database_section_label(section_label)
    existing = set(get_all_frame_section_names(SapModel))
    if section_name in existing:
        return section_name

    failures: list[str] = []
    for database_file in _section_database_candidates(section_label):
        ret = SapModel.PropFrame.ImportProp(
            section_name,
            material_name,
            database_file,
            section_name,
        )
        if ret == 0:
            return section_name
        failures.append(f"{database_file}:{section_name} ret={ret}")

    raise RuntimeError(
        f"PropFrame.ImportProp failed for section {section_label}. Tried: {', '.join(failures)}"
    )


def create_points_from_payload(
    SapModel: Any,
    payload: RevitAnalyticalSapImportModel,
) -> dict[int, str]:
    point_names: dict[int, str] = {}
    for node in payload.nodes:
        point_name = str(node.node_id)
        result = SapModel.PointObj.AddCartesian(node.x, node.y, node.z, " ", point_name)
        if not isinstance(result, tuple) or len(result) < 2:
            raise RuntimeError(f"PointObj.AddCartesian returned unexpected result for node {point_name}: {result}")

        ret, sap_name = result[0], result[1]
        if ret != 0:
            raise RuntimeError(f"PointObj.AddCartesian failed for node {point_name} (ret={ret})")

        point_names[node.node_id] = str(sap_name or point_name)
    return point_names


def create_frames_from_payload(
    SapModel: Any,
    payload: RevitAnalyticalSapImportModel,
    point_names: dict[int, str],
    material_name: str,
) -> dict[int, str]:
    imported_sections: dict[str, str] = {}
    frame_names: dict[int, str] = {}

    for line in payload.lines:
        section_label = str(line.section)
        if section_label not in imported_sections:
            imported_sections[section_label] = import_frame_section_from_label(
                SapModel,
                section_label=section_label,
                material_name=material_name,
            )

        frame_name = str(line.line_id)
        result = SapModel.FrameObj.AddByPoint(
            point_names[line.Ni],
            point_names[line.Nj],
            frame_name,
            imported_sections[section_label],
            "Global",
        )
        if not isinstance(result, tuple) or len(result) < 2:
            raise RuntimeError(f"FrameObj.AddByPoint returned unexpected result for frame {frame_name}: {result}")

        ret, sap_name = result[0], result[1]
        if ret != 0:
            raise RuntimeError(f"FrameObj.AddByPoint failed for frame {frame_name} (ret={ret})")

        frame_names[line.line_id] = str(sap_name or frame_name)

    return frame_names


def _parse_add_area_result(result: Any) -> tuple[int, str]:
    if not isinstance(result, tuple):
        raise RuntimeError(f"AreaObj.AddByPoint returned non-tuple: {type(result)} {result}")

    ret = None
    area_name = ""
    for value in result:
        if isinstance(value, int):
            ret = value
        elif isinstance(value, str):
            area_name = value

    if ret is None:
        raise RuntimeError(f"Could not parse AreaObj.AddByPoint return: {result}")
    return int(ret), str(area_name)


def _infer_area_thickness(area: ExportAreaModel, default_thickness: float) -> float:
    if area.metadata and area.metadata.thickness_mm is not None:
        return float(area.metadata.thickness_mm) / 1000.0

    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*mm", area.section, flags=re.IGNORECASE)
    if match:
        return float(match.group(1)) / 1000.0

    return float(default_thickness)


def create_areas_from_payload(
    SapModel: Any,
    payload: RevitAnalyticalSapImportModel,
    point_names: dict[int, str],
    concrete_material_name: str,
    default_slab_thickness: float,
) -> dict[int, str]:
    if not payload.areas:
        return {}

    define_concrete_material(SapModel, material_name=concrete_material_name)

    section_thickness: dict[str, float] = {}
    for area in payload.areas:
        section_thickness.setdefault(area.section, _infer_area_thickness(area, default_slab_thickness))

    for section_name, thickness in section_thickness.items():
        define_slab_area_section(
            SapModel,
            section_name=section_name,
            material_name=concrete_material_name,
            thickness=thickness,
        )

    area_names: dict[int, str] = {}
    for area in payload.areas:
        area_name = str(area.area_id)
        result = SapModel.AreaObj.AddByPoint(
            len(area.nodes),
            [point_names[node_id] for node_id in area.nodes],
            area_name,
            area.section,
            area_name,
        )
        ret, sap_name = _parse_add_area_result(result)
        if ret != 0:
            raise RuntimeError(f"AreaObj.AddByPoint failed for {area_name} (ret={ret})")
        area_names[area.area_id] = sap_name or area_name

    return area_names


def collect_payload_supports(payload: RevitAnalyticalSapImportModel) -> dict[int, list[int]]:
    supports: dict[int, list[int]] = {}
    for node in payload.nodes:
        support = node.metadata.support if node.metadata else None
        if support is not None:
            supports[node.node_id] = list(support.restraint)
    return supports


def build_supports_from_lowest_z(
    payload: RevitAnalyticalSapImportModel,
    restraint: list[int],
) -> dict[int, list[int]]:
    if not payload.nodes:
        return {}

    min_z = min(float(node.z) for node in payload.nodes)
    return {
        node.node_id: list(restraint)
        for node in payload.nodes
        if abs(float(node.z) - min_z) <= 1e-9
    }


def resolve_supports(
    payload: RevitAnalyticalSapImportModel,
    support_policy: str,
    default_support_restraint: list[int],
) -> tuple[dict[int, list[int]], str]:
    payload_supports = collect_payload_supports(payload)

    if support_policy == "none":
        return {}, "Support assignment disabled."

    if support_policy == "from_payload":
        return payload_supports, f"Using {len(payload_supports)} supports from payload."

    if support_policy == "lowest_z_fixed":
        generated = build_supports_from_lowest_z(payload, default_support_restraint)
        return generated, f"Generated {len(generated)} lowest-Z supports."

    if payload_supports:
        return payload_supports, f"Using {len(payload_supports)} supports from payload."

    generated = build_supports_from_lowest_z(payload, default_support_restraint)
    return generated, f"Payload had no supports. Generated {len(generated)} lowest-Z supports."


def assign_supports_by_node_ids(
    SapModel: Any,
    point_names: dict[int, str],
    restraints_by_node_id: dict[int, list[int]],
) -> dict[int, dict[str, Any]]:
    assigned: dict[int, dict[str, Any]] = {}

    for node_id, restraint_values in restraints_by_node_id.items():
        if node_id not in point_names:
            raise RuntimeError(f"Point name for node {node_id} was not created in SAP2000.")

        if len(restraint_values) != 6:
            raise ValueError(f"Support restraint for node {node_id} must have 6 values.")

        point_name = point_names[node_id]
        result = SapModel.PointObj.SetRestraint(point_name, [int(value) for value in restraint_values])
        ret = result[0] if isinstance(result, tuple) else result
        if ret != 0:
            raise RuntimeError(f"PointObj.SetRestraint failed for point {point_name} (ret={ret})")

        assigned[node_id] = {
            "point_name": point_name,
            "restraint": [int(value) for value in restraint_values],
        }

    return assigned


def infer_load_pattern_type(load_payload: dict[str, Any]) -> int:
    tokens = " ".join(
        str(load_payload.get(key, ""))
        for key in ("load_category_name", "load_nature_name", "load_case_name")
    ).lower()
    return CSI_LOADPATTERN_DEAD if "dead" in tokens else CSI_LOADPATTERN_LIVE


def canonical_load_case_name(load_payload: dict[str, Any]) -> str:
    load_pattern_type = infer_load_pattern_type(load_payload)
    if load_pattern_type == CSI_LOADPATTERN_DEAD:
        return DEFAULT_DEAD_CASE_NAME
    return DEFAULT_LIVE_CASE_NAME


def ensure_load_pattern(
    SapModel: Any,
    name: str,
    load_pattern_type: int,
    self_weight_multiplier: float = 0.0,
    add_analysis_case: bool = False,
) -> str:
    if name in set(get_all_load_patterns(SapModel)):
        return name

    result = SapModel.LoadPatterns.Add(
        name,
        load_pattern_type,
        float(self_weight_multiplier),
        bool(add_analysis_case),
    )
    ret = result[0] if isinstance(result, tuple) else result
    if ret != 0:
        raise RuntimeError(f"LoadPatterns.Add failed for {name} (ret={ret})")
    return name


def recreate_static_linear_case_from_pattern(
    SapModel: Any,
    case_name: str,
    pattern_name: str,
    scale_factor: float = 1.0,
) -> str:
    result = SapModel.LoadCases.StaticLinear.SetCase(case_name)
    ret = result[0] if isinstance(result, tuple) else result
    if ret != 0:
        raise RuntimeError(f"LoadCases.StaticLinear.SetCase failed for {case_name} (ret={ret})")

    result = SapModel.LoadCases.StaticLinear.SetLoads(
        case_name,
        1,
        ["Load"],
        [pattern_name],
        [float(scale_factor)],
    )
    ret = result[0] if isinstance(result, tuple) else result
    if ret != 0:
        raise RuntimeError(f"LoadCases.StaticLinear.SetLoads failed for {case_name} (ret={ret})")
    return case_name


def recreate_linear_additive_combo(
    SapModel: Any,
    combo_name: str,
    case_scale_factors: dict[str, float],
) -> str:
    existing_case_names = set(get_all_load_cases(SapModel))
    if combo_name in existing_case_names:
        raise RuntimeError(
            f"Response combo name {combo_name} conflicts with an existing load case."
        )

    existing_combo_names = set(get_all_load_combos(SapModel))
    if combo_name in existing_combo_names:
        result = SapModel.RespCombo.Delete(combo_name)
        ret = result[0] if isinstance(result, tuple) else result
        if ret != 0:
            raise RuntimeError(f"RespCombo.Delete failed for {combo_name} (ret={ret})")

    result = SapModel.RespCombo.Add(combo_name, CSI_COMBO_LINEAR_ADDITIVE)
    ret = result[0] if isinstance(result, tuple) else result
    if ret != 0:
        raise RuntimeError(f"RespCombo.Add failed for {combo_name} (ret={ret})")

    for case_name, scale_factor in case_scale_factors.items():
        try:
            result = SapModel.RespCombo.SetCaseList(
                combo_name,
                CSI_CNAME_LOADCASE,
                case_name,
                float(scale_factor),
            )
        except Exception:
            import pythoncom
            from win32com.client import VARIANT

            cname_type = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, CSI_CNAME_LOADCASE)
            result = SapModel.RespCombo.SetCaseList(
                combo_name,
                cname_type,
                case_name,
                float(scale_factor),
            )
        ret = result[0] if isinstance(result, tuple) else result
        if ret != 0:
            raise RuntimeError(
                f"RespCombo.SetCaseList failed for combo {combo_name}, case {case_name} (ret={ret})"
            )

    return combo_name


def extract_global_z_load(load_payload: dict[str, Any]) -> float | None:
    vector = load_payload.get("force_vector_global_kn_per_m2")
    if not isinstance(vector, dict):
        return None

    x = float(vector.get("x", 0.0))
    y = float(vector.get("y", 0.0))
    z = float(vector.get("z", 0.0))
    if abs(x) > LOAD_COMPONENT_TOLERANCE or abs(y) > LOAD_COMPONENT_TOLERANCE:
        return None
    return z


def collect_area_loads(payload: RevitAnalyticalSapImportModel) -> list[dict[str, Any]]:
    loads: list[dict[str, Any]] = []
    for area in payload.areas:
        if area.metadata is None:
            continue
        for load in area.metadata.loads:
            load_payload = load.model_dump(mode="python", exclude_none=True)
            load_payload["area_id"] = area.area_id
            loads.append(load_payload)
    return loads


def assign_uniform_area_load(
    SapModel: Any,
    area_name: str,
    load_pattern_name: str,
    value: float,
    direction: int = CSI_DIR_GLOBAL_Z,
    coordinate_system: str = "Global",
    replace: bool = True,
) -> None:
    result = SapModel.AreaObj.SetLoadUniform(
        area_name,
        load_pattern_name,
        float(value),
        int(direction),
        bool(replace),
        coordinate_system,
        CSI_ITEMTYPE_OBJECTS,
    )
    ret = result[0] if isinstance(result, tuple) else result
    if ret != 0:
        raise RuntimeError(
            f"AreaObj.SetLoadUniform failed for area {area_name}, pattern {load_pattern_name} (ret={ret})"
        )


def apply_uniform_area_loads_from_revit_export(
    SapModel: Any,
    area_name_by_area_id: dict[int, str],
    load_payloads: list[dict[str, Any]],
    default_self_weight_multiplier: float = 0.0,
) -> dict[str, Any]:
    grouped_loads: dict[tuple[str, str], float] = {}
    pattern_names: set[str] = set()
    case_names: set[str] = set()
    assigned: list[dict[str, Any]] = []
    skipped: list[str] = []

    for load_payload in load_payloads:
        area_id = int(load_payload.get("area_id", 0))
        if area_id not in area_name_by_area_id:
            skipped.append(f"Skipped load for missing exported area id {area_id}.")
            continue

        if str(load_payload.get("kind", "")).lower() != "area_uniform":
            skipped.append(f"Skipped area {area_id} load with unsupported kind.")
            continue

        z_value = extract_global_z_load(load_payload)
        if z_value is None:
            skipped.append(
                f"Skipped area {area_id} load '{load_payload.get('load_case_name', '')}': "
                "only global Z uniform loads are supported."
            )
            continue

        load_pattern_type = infer_load_pattern_type(load_payload)
        load_pattern_name = canonical_load_case_name(load_payload)
        ensure_load_pattern(
            SapModel,
            name=load_pattern_name,
            load_pattern_type=load_pattern_type,
            self_weight_multiplier=default_self_weight_multiplier if load_pattern_type == CSI_LOADPATTERN_DEAD else 0.0,
            add_analysis_case=False,
        )
        recreate_static_linear_case_from_pattern(
            SapModel,
            case_name=load_pattern_name,
            pattern_name=load_pattern_name,
            scale_factor=1.0,
        )

        pattern_names.add(load_pattern_name)
        case_names.add(load_pattern_name)

        area_name = area_name_by_area_id[area_id]
        key = (area_name, load_pattern_name)
        grouped_loads[key] = grouped_loads.get(key, 0.0) + z_value

    for (area_name, load_pattern_name), z_value in grouped_loads.items():
        assign_uniform_area_load(
            SapModel,
            area_name=area_name,
            load_pattern_name=load_pattern_name,
            value=z_value,
        )
        assigned.append(
            {
                "area_name": area_name,
                "load_pattern_name": load_pattern_name,
                "value": z_value,
            }
        )

    return {
        "patterns": sorted(pattern_names),
        "cases": sorted(case_names),
        "assigned_loads": assigned,
        "skipped": skipped,
    }


def create_default_design_combos(
    SapModel: Any,
    available_case_names: list[str],
) -> list[str]:
    created_combo_names: list[str] = []
    available_case_set = set(available_case_names)

    for combo_name, case_scale_factors in DEFAULT_DESIGN_COMBOS:
        filtered_case_scale_factors = {
            case_name: scale_factor
            for case_name, scale_factor in case_scale_factors.items()
            if case_name in available_case_set
        }
        if not filtered_case_scale_factors:
            continue

        recreate_linear_additive_combo(
            SapModel,
            combo_name=combo_name,
            case_scale_factors=filtered_case_scale_factors,
        )
        created_combo_names.append(combo_name)

    return created_combo_names


def get_point_coords(SapModel: Any, point_name: str) -> tuple[float, float, float]:
    result = SapModel.PointObj.GetCoordCartesian(point_name, 0, 0, 0)
    if not isinstance(result, tuple) or len(result) != 4:
        raise RuntimeError(f"GetCoordCartesian returned unexpected format: {result}")

    z_sap, x_sap, y_sap, ret = result
    if ret != 0:
        raise RuntimeError(f"GetCoordCartesian({point_name}) failed (ret={ret})")

    return float(x_sap), float(y_sap), float(z_sap)


def get_point_restraint(SapModel: Any, point_name: str) -> list[int]:
    try:
        result = SapModel.PointObj.GetRestraint(point_name, [0, 0, 0, 0, 0, 0])
    except Exception:
        result = SapModel.PointObj.GetRestraint(point_name)

    if not isinstance(result, tuple):
        raise RuntimeError(f"GetRestraint returned non-tuple: {type(result)} {result}")

    restraint = None
    ret = None
    for value in result:
        if isinstance(value, int):
            ret = value
        if isinstance(value, (list, tuple)) and len(value) == 6:
            restraint = value

    if restraint is None or ret is None:
        raise RuntimeError(f"Could not parse GetRestraint return: {result}")

    if int(ret) != 0:
        raise RuntimeError(f"GetRestraint({point_name}) failed (ret={ret})")

    return [int(value) for value in restraint]


def get_support_nodes(SapModel: Any) -> list[dict[str, Any]]:
    supports: list[dict[str, Any]] = []
    for point_name in get_all_point_names(SapModel):
        restraint = get_point_restraint(SapModel, point_name)
        if not any(restraint):
            continue

        x, y, z = get_point_coords(SapModel, point_name)
        supports.append(
            {
                "Joint": point_name,
                "X": x,
                "Y": y,
                "Z": z,
                "Restraint": {
                    "U1": restraint[0],
                    "U2": restraint[1],
                    "U3": restraint[2],
                    "R1": restraint[3],
                    "R2": restraint[4],
                    "R3": restraint[5],
                },
            }
        )
    return supports


def select_results_output(SapModel: Any, name: str) -> str:
    ret = SapModel.Results.Setup.DeselectAllCasesAndCombosForOutput()
    if ret != 0:
        raise RuntimeError(f"DeselectAllCasesAndCombosForOutput failed (ret={ret})")

    ret_case = SapModel.Results.Setup.SetCaseSelectedForOutput(name)
    if ret_case == 0:
        return "case"

    ret_combo = SapModel.Results.Setup.SetComboSelectedForOutput(name)
    if ret_combo == 0:
        return "combo"

    raise RuntimeError(f"Could not select '{name}' as case or combo.")


def get_joint_reaction_first_row(SapModel: Any, joint_name: str) -> dict[str, Any]:
    import pythoncom

    result = SapModel.Results.JointReact(
        joint_name,
        0,
        pythoncom.Missing,
        pythoncom.Missing,
        pythoncom.Missing,
        pythoncom.Missing,
        pythoncom.Missing,
        pythoncom.Missing,
        pythoncom.Missing,
        pythoncom.Missing,
        pythoncom.Missing,
        pythoncom.Missing,
        pythoncom.Missing,
        pythoncom.Missing,
    )

    if not isinstance(result, tuple) or len(result) != 13:
        raise RuntimeError(f"JointReact returned unexpected result for joint {joint_name}: {result}")

    (
        ret_code,
        _number_results,
        _obj,
        _elm,
        load_case_arr,
        step_type,
        step_num,
        f1,
        f2,
        f3,
        m1,
        m2,
        m3,
    ) = result

    if ret_code != 0:
        raise RuntimeError(f"JointReact({joint_name}) failed (ret={ret_code})")

    if not load_case_arr:
        return {
            "ResultName": "",
            "StepType": "",
            "StepNum": 0.0,
            "F1": 0.0,
            "F2": 0.0,
            "F3": 0.0,
            "M1": 0.0,
            "M2": 0.0,
            "M3": 0.0,
        }

    index = 0
    return {
        "ResultName": str(load_case_arr[index]),
        "StepType": str(step_type[index]),
        "StepNum": float(step_num[index]),
        "F1": float(f1[index]),
        "F2": float(f2[index]),
        "F3": float(f3[index]),
        "M1": float(m1[index]),
        "M2": float(m2[index]),
        "M3": float(m3[index]),
    }


def get_support_reactions_all_results(
    SapModel: Any,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    supports = get_support_nodes(SapModel)
    result_names = list(dict.fromkeys(get_all_load_combos(SapModel) + get_all_load_cases(SapModel)))
    reactions_by_joint: dict[str, dict[str, Any]] = {support["Joint"]: {} for support in supports}

    for result_name in result_names:
        selected_type = select_results_output(SapModel, result_name)
        for support in supports:
            joint_name = support["Joint"]
            reaction = get_joint_reaction_first_row(SapModel, joint_name)
            reactions_by_joint[joint_name][result_name] = {
                "Type": selected_type,
                "StepType": reaction["StepType"],
                "StepNum": reaction["StepNum"],
                "F1": reaction["F1"],
                "F2": reaction["F2"],
                "F3": reaction["F3"],
                "M1": reaction["M1"],
                "M2": reaction["M2"],
                "M3": reaction["M3"],
            }

    return supports, reactions_by_joint


def import_structural_model_from_payload(
    SapModel: Any,
    payload: RevitAnalyticalSapImportModel,
    *,
    material_name: str,
    concrete_material_name: str,
    default_slab_thickness: float,
    initialize_blank_model: bool,
    units: int,
) -> dict[str, Any]:
    if initialize_blank_model:
        ensure_blank_model(SapModel, units=units)

    define_steel_material(SapModel, material_name=material_name)
    point_names = create_points_from_payload(SapModel, payload)
    frame_names = create_frames_from_payload(
        SapModel,
        payload=payload,
        point_names=point_names,
        material_name=material_name,
    )
    area_names = create_areas_from_payload(
        SapModel,
        payload=payload,
        point_names=point_names,
        concrete_material_name=concrete_material_name,
        default_slab_thickness=default_slab_thickness,
    )
    return {
        "points": point_names,
        "frames": frame_names,
        "areas": area_names,
    }


def _read_binary_or_text(stored_file: Any) -> str:
    if hasattr(stored_file, "getvalue_binary"):
        return stored_file.getvalue_binary().decode("utf-8")
    if hasattr(stored_file, "getvalue"):
        value = stored_file.getvalue()
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)
    raise ValueError("Unsupported Viktor Storage file object.")


def load_analytical_model_from_source() -> tuple[RevitAnalyticalSapImportModel, str]:
    try:
        import viktor as vkt

        stored_file = vkt.Storage().get(ANALYTICAL_MODEL_STORAGE_KEY, scope="entity")
        if stored_file is not None:
            raw_json = _read_binary_or_text(stored_file)
            return (
                RevitAnalyticalSapImportModel.model_validate_json(raw_json),
                f"Loaded Viktor Storage key '{ANALYTICAL_MODEL_STORAGE_KEY}'",
            )
    except Exception as exc:
        logger.warning("Failed to read analytical model from Viktor Storage: %s", exc)

    raise FileNotFoundError(
        f"No analytical model JSON found in Viktor Storage key '{ANALYTICAL_MODEL_STORAGE_KEY}'."
    )


def store_json_in_viktor(storage_key: str, data: Any) -> None:
    import viktor as vkt

    vkt.Storage().set(
        storage_key,
        data=vkt.File.from_data(json.dumps(data, indent=2)),
        scope="entity",
    )


async def build_sap_model_from_analytical_json_func(ctx: Any, args: str) -> str:
    del ctx

    try:
        raw_args = args.strip() if isinstance(args, str) else ""
        if raw_args and raw_args != "{}":
            BuildSapFromAnalyticalModelArgs.model_validate_json(raw_args)

        analytical_model, source_note = load_analytical_model_from_source()

        supports_by_node_id, support_note = resolve_supports(
            analytical_model,
            support_policy=DEFAULT_SUPPORT_POLICY,
            default_support_restraint=list(DEFAULT_SUPPORT_RESTRAINT),
        )
        area_loads = collect_area_loads(analytical_model)

        with Sap2000Session() as sap:
            sap_result = import_structural_model_from_payload(
                sap.SapModel,
                analytical_model,
                material_name=DEFAULT_STEEL_MATERIAL,
                concrete_material_name=DEFAULT_CONCRETE_MATERIAL,
                default_slab_thickness=DEFAULT_SLAB_THICKNESS_M,
                initialize_blank_model=DEFAULT_INITIALIZE_BLANK_MODEL,
                units=DEFAULT_SAP_UNITS,
            )

            assigned_supports: dict[int, dict[str, Any]] | None = None
            if DEFAULT_APPLY_SUPPORTS and supports_by_node_id:
                assigned_supports = assign_supports_by_node_ids(
                    sap.SapModel,
                    point_names=sap_result["points"],
                    restraints_by_node_id=supports_by_node_id,
                )

            loading_result: dict[str, Any] | None = None
            if DEFAULT_APPLY_LOADS and area_loads and sap_result["areas"]:
                loading_result = apply_uniform_area_loads_from_revit_export(
                    sap.SapModel,
                    area_name_by_area_id=sap_result["areas"],
                    load_payloads=area_loads,
                    default_self_weight_multiplier=DEFAULT_DEAD_SELF_WEIGHT_MULTIPLIER,
                )
                loading_result["combos"] = create_default_design_combos(
                    sap.SapModel,
                    available_case_names=loading_result["cases"],
                )

            save_path = save_model(
                sap.SapModel,
                DEFAULT_SAVE_MODEL_PATH or _build_temp_model_path(),
            )

            if DEFAULT_RUN_ANALYSIS:
                run_analysis(sap.SapModel)

            supports, reactions = get_support_reactions_all_results(sap.SapModel)

        store_json_in_viktor(SUPPORT_COORDINATES_STORAGE_KEY, supports)
        store_json_in_viktor(REACTION_LOADS_STORAGE_KEY, reactions)

        num_results_per_node = len(next(iter(reactions.values()))) if reactions else 0
        loading_summary = (
            f"{len(loading_result['assigned_loads'])} assigned area loads "
            f"and {len(loading_result.get('combos', []))} hard-coded combos"
            if loading_result is not None
            else "no area loads assigned"
        )
        save_note = (
            f" Model saved to {save_path}."
        )

        return (
            f"Imported analytical model into SAP2000. {source_note}. {support_note} "
            f"Created {len(sap_result['points'])} points, {len(sap_result['frames'])} frames, "
            f"and {len(sap_result['areas'])} areas. "
            f"Detected {len(supports)} support nodes and stored them in '{SUPPORT_COORDINATES_STORAGE_KEY}'. "
            f"Stored reactions for {len(reactions)} nodes in '{REACTION_LOADS_STORAGE_KEY}' "
            f"with {num_results_per_node} load cases/combos per node. "
            f"Loading summary: {loading_summary}.{save_note}"
        )

    except RuntimeError as exc:
        error_msg = str(exc)
        if "Could not attach" in error_msg:
            return (
                "Failed to connect to SAP2000. Please ensure:\n"
                "1. SAP2000 is running\n"
                "2. A model is open\n"
                "3. Tools -> Set as active instance for API\n"
                "4. SAP2000 and Python run at the same admin level\n"
                f"Error: {error_msg}"
            )
        return f"Error building SAP2000 model from analytical JSON: {error_msg}"
    except Exception as exc:
        logger.exception("Unexpected error in build_sap_model_from_analytical_json_func")
        return f"Unexpected error: {type(exc).__name__}: {exc}"


def build_sap_model_from_analytical_json_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="build_sap_model_from_analytical_json",
        description=(
            "Single-file SAP2000 import tool. Reads the analytical model JSON from Viktor Storage "
            "(key: 'acc_analytical_model_json'), parses and validates it, "
            "creates the SAP2000 points/frames/areas, assigns supports, applies supported area loads, "
            "runs analysis, then stores support coordinates and reaction loads back in Viktor Storage. "
            "This tool does not require user input."
        ),
        params_json_schema=BuildSapFromAnalyticalModelArgs.model_json_schema(),
        on_invoke_tool=build_sap_model_from_analytical_json_func,
    )
