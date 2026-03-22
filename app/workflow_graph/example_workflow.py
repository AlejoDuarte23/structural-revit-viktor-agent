from app.workflow_graph.models import Connection, Node, Workflow


def example_workflow() -> Workflow:
    return Workflow(
        nodes=[
            Node(
                id="acc_file_information",
                title="Get ACC File Information",
                type="get_autodesk_file_context",
            ),
            Node(
                id="display_revit_model",
                title="Display Revit Model",
                type="show_hide_autodesk_view",
                depends_on=[Connection(node_id="acc_file_information")],
            ),
            Node(
                id="revit_analytical_model",
                title="Get Revit Analytical Model",
                type="extract_analytical_model_json",
                depends_on=[Connection(node_id="acc_file_information")],
            ),
            Node(
                id="sap_model",
                title="Create SAP Model",
                type="build_sap_model_from_analytical_json",
                depends_on=[Connection(node_id="revit_analytical_model")],
            ),
            Node(
                id="coordinate_table",
                title="Display Coordinate Table",
                type="display_support_coordinates_table",
                depends_on=[Connection(node_id="sap_model")],
            ),
            Node(
                id="footing_sizing",
                title="Footing Sizing",
                type="calculate_footing_sizing",
                depends_on=[Connection(node_id="sap_model")],
            ),
            Node(
                id="acc_footing_model",
                title="Finalize ACC Footing Model",
                type="run_footing_acc_automation",
                depends_on=[
                    Connection(node_id="acc_file_information"),
                    Connection(node_id="sap_model"),
                    Connection(node_id="footing_sizing"),
                ],
            ),
        ]
    )


if __name__ == "__main__":
    from app.workflow_graph.viewer import WorkflowViewer

    WorkflowViewer(example_workflow).show()
