from app.workflow_graph.models import Connection, Node, Workflow


def example_workflow() -> Workflow:
    return Workflow(
        nodes=[
            Node(id="geometry", title="Geometry Generation App", type="geometry"),
            Node(
                id="seismic",
                title="Seismic Analysis App",
                type="seismic",
                depends_on=[Connection(node_id="geometry")],
            ),
            Node(
                id="wind",
                title="Wind Load Analysis App",
                type="wind",
                depends_on=[Connection(node_id="geometry")],
            ),
            Node(
                id="structural",
                title="Structural Analysis App",
                type="structural",
                depends_on=[Connection(node_id="seismic"), Connection(node_id="wind")],
            ),
            Node(
                id="footing_cap",
                title="Footing Capacities",
                type="footing_cap",
            ),
            Node(
                id="footing_design",
                title="Footing Design",
                type="footing_design",
                depends_on=[
                    Connection(node_id="structural"),
                    Connection(node_id="footing_cap"),
                ],
            ),
        ]
    )


if __name__ == "__main__":
    from app.workflow_graph.viewer import WorkflowViewer

    WorkflowViewer(example_workflow).show()
