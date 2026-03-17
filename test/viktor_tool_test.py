import sys

from app.viktor_tools.wind_loads_tool import (
    WindLoadInput,
    WindLoadTool,
    WindLoadOutput,
)
from app.viktor_tools.structural_analysis_tool import (
    StructuralAnalysisInput,
    StructuralAnalysisStep1,
    StructuralAnalysisStep2,
    StructuralAnalysisTool,
    StructuralAnalysisOutput,
)
from app.viktor_tools.sensitivity_analysis_tool import (
    SensitivityAnalysisInput,
    SensitivityAnalysisStep1,
    SensitivityAnalysisStep2,
    SensitivityAnalysisStep4,
    SensitivityAnalysisTool,
    SensitivityAnalysisOutput,
)
from app.viktor_tools.geometry_tool import (
    GeometryGeneration,
    GeometryGenerationTool,
    Model,
)


def test_wind_loads():
    """Test WindLoadTool.run_and_parse returns WindLoadOutput"""
    wind_input = WindLoadInput(
        risk_category="II",
        site_elevation_m=138.0,
        bridge_length=20000,
        bridge_width=4500,
        bridge_height=3000,
        roof_pitch_angle=12,
        n_divisions=4,
        cross_section="HSS200×200×8",
        exposure_category="C",
        wind_speed_ms=47.0,
    )
    tool = WindLoadTool(wind_input=wind_input)

    result = tool.run_and_parse()

    # Verify type
    assert isinstance(result, WindLoadOutput), (
        f"Expected WindLoadOutput, got {type(result)}"
    )

    assert result.qz_kpa > 0
    assert result.p_kpa > 0


def test_structural_analysis():
    """Test StructuralAnalysisTool.run_and_parse returns StructuralAnalysisOutput"""
    structural_input = StructuralAnalysisInput(
        step_1=StructuralAnalysisStep1(
            bridge_length=20000,
            bridge_width=4500,
            bridge_height=3000,
            n_divisions=4,
            cross_section="HSS200x200x8",
        ),
        step_2=StructuralAnalysisStep2(
            load_q=4,
            wind_pressure=1.5,
            wind_cf=1.6,
        ),
    )
    tool = StructuralAnalysisTool(structural_input=structural_input)

    result = tool.run_and_parse()

    # Verify type
    assert isinstance(result, StructuralAnalysisOutput), (
        f"Expected StructuralAnalysisOutput, got {type(result)}"
    )

    assert result.critical_combination is not None
    assert result.max_displacements_mm is not None


def test_sensitivity_analysis():
    """Test SensitivityAnalysisTool.run_and_parse returns SensitivityAnalysisOutput"""
    sensitivity_input = SensitivityAnalysisInput(
        step_1=SensitivityAnalysisStep1(
            bridge_length=20000,
            bridge_width=4500,
            n_divisions=4,
            cross_section="HSS200x200x8",
        ),
        step_2=SensitivityAnalysisStep2(
            load_q=4,
            wind_pressure=1.5,
        ),
        step_4=SensitivityAnalysisStep4(
            min_height=1000,
            max_height=7000,
            n_steps=10,
        ),
    )
    tool = SensitivityAnalysisTool(sensitivity_input=sensitivity_input)

    result = tool.run_and_parse()

    # Verify type
    assert isinstance(result, SensitivityAnalysisOutput), (
        f"Expected SensitivityAnalysisOutput, got {type(result)}"
    )

    assert len(result.sensitivity_analysis) > 0


def test_geometry_generation():
    """Test GeometryGenerationTool.run_and_parse returns Model"""
    geometry = GeometryGeneration(
        bridge_length=20000,
        bridge_width=4500,
        bridge_height=3000,
        n_divisions=4,
        cross_section="HSS200×200×8",
    )
    tool = GeometryGenerationTool(geometry=geometry)

    result = tool.run_and_parse()

    # Verify type
    assert isinstance(result, Model), f"Expected Model, got {type(result)}"

    assert result.metadata.total_nodes > 0
    assert result.metadata.total_lines > 0


def run_all_tests():
    """Run all tool tests and report results (standalone mode)"""
    print("\n" + "#" * 60)
    print("VIKTOR TOOLS - run_and_parse VALIDATION TESTS")
    print("#" * 60)

    tests = [
        ("WindLoadTool", test_wind_loads),
        ("StructuralAnalysisTool", test_structural_analysis),
        ("SensitivityAnalysisTool", test_sensitivity_analysis),
        ("GeometryGenerationTool", test_geometry_generation),
    ]

    results = {}
    for name, test_func in tests:
        try:
            test_func()
            results[name] = "✅ PASSED"
        except AssertionError as e:
            results[name] = f"❌ FAILED: {e}"
        except Exception as e:
            results[name] = f"❌ ERROR: {type(e).__name__}: {e}"

    # Summary
    print("TEST SUMMARY")
    print("#" * 60)
    for name, status in results.items():
        print(f"  {name}: {status}")

    passed = sum(1 for s in results.values() if s.startswith("✅"))
    total = len(results)
    print(f"Total: {passed}/{total} tests passed")

    if passed == total:
        print("All tests passed!✅")
    else:
        print("Some tests failed!❌")
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
