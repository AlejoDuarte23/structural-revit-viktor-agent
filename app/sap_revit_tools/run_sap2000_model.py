"""Worker entrypoint for SAP2000Analysis."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tool_reference import (
    DEFAULT_SAVE_MODEL_PATH,
    RevitAnalyticalSapImportModel,
    Sap2000Session,
    _build_temp_model_path,
    apply_uniform_area_loads_from_revit_export,
    assign_supports_by_node_ids,
    collect_area_loads,
    create_default_design_combos,
    get_support_reactions_all_results,
    import_structural_model_from_payload,
    resolve_supports,
    run_analysis,
    save_model,
)


def _load_inputs() -> tuple[RevitAnalyticalSapImportModel, dict[str, Any]]:
    payload = json.loads((Path.cwd() / "inputs.json").read_text(encoding="utf-8"))
    analytical_model = RevitAnalyticalSapImportModel.model_validate(payload["analytical_model"])
    settings = payload.get("settings", {})
    return analytical_model, settings


def main() -> None:
    analytical_model, settings = _load_inputs()

    supports_by_node_id, support_note = resolve_supports(
        analytical_model,
        support_policy=str(settings.get("support_policy", "from_payload_or_lowest_z_fixed")),
        default_support_restraint=list(settings.get("default_support_restraint", [1, 1, 1, 1, 1, 1])),
    )
    area_loads = collect_area_loads(analytical_model)

    with Sap2000Session(
        attach_to_instance=bool(settings.get("attach_to_instance", False)),
        create_if_missing=bool(settings.get("create_if_missing", False)),
        program_path=settings.get("program_path"),
    ) as sap:
        sap_result = import_structural_model_from_payload(
            sap.SapModel,
            analytical_model,
            material_name=str(settings.get("material_name", "S355")),
            concrete_material_name=str(settings.get("concrete_material_name", "C30")),
            default_slab_thickness=float(settings.get("default_slab_thickness", 0.15)),
            initialize_blank_model=bool(settings.get("initialize_blank_model", True)),
            units=int(settings.get("units", 6)),
        )

        loading_result: dict[str, Any] | None = None
        if bool(settings.get("apply_supports", True)) and supports_by_node_id:
            assign_supports_by_node_ids(
                sap.SapModel,
                point_names=sap_result["points"],
                restraints_by_node_id=supports_by_node_id,
            )

        if bool(settings.get("apply_loads", True)) and area_loads and sap_result["areas"]:
            loading_result = apply_uniform_area_loads_from_revit_export(
                sap.SapModel,
                area_name_by_area_id=sap_result["areas"],
                load_payloads=area_loads,
                default_self_weight_multiplier=float(settings.get("dead_self_weight_multiplier", 0.0)),
            )
            loading_result["combos"] = create_default_design_combos(
                sap.SapModel,
                available_case_names=loading_result["cases"],
            )

        save_path = None
        if bool(settings.get("save_model", True)):
            save_path = save_model(
                sap.SapModel,
                DEFAULT_SAVE_MODEL_PATH or _build_temp_model_path(),
            )

        if bool(settings.get("run_analysis", True)):
            run_analysis(sap.SapModel)

        supports, reactions = get_support_reactions_all_results(sap.SapModel)

    output = {
        "supports": supports,
        "reactions": reactions,
        "metadata": {
            "support_note": support_note,
            "points_created": len(sap_result["points"]),
            "frames_created": len(sap_result["frames"]),
            "areas_created": len(sap_result["areas"]),
            "result_count_per_node": len(next(iter(reactions.values()))) if reactions else 0,
            "loading_summary": (
                f"{len(loading_result['assigned_loads'])} assigned area loads "
                f"and {len(loading_result.get('combos', []))} hard-coded combos"
                if loading_result is not None
                else "no area loads assigned"
            ),
            "save_path": str(save_path) if save_path else None,
        },
    }
    (Path.cwd() / "output.json").write_text(json.dumps(output, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
