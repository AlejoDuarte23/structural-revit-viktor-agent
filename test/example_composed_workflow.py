import asyncio

from app.tools import (
    ComposeWorkflowGraphArgs,
    DummyWorkflowNode,
    compose_workflow_graph_func,
)


async def main() -> None:
    """Example workflow with URLs for clickable tool icons."""
    payload = ComposeWorkflowGraphArgs(
        workflow_name="example_with_urls",
        nodes=[
            DummyWorkflowNode(
                node_id="geometry",
                node_type="geometry_generation",
                label="Geometry Generation",
                url="https://beta.viktor.ai/workspaces/4672/app/editor/2394",
            ),
            DummyWorkflowNode(
                node_id="wind",
                node_type="windload_analysis",
                label="Wind Load Analysis",
                url="https://beta.viktor.ai/workspaces/4675/app/editor/2397",
                depends_on=["geometry"],
            ),
            DummyWorkflowNode(
                node_id="structural",
                node_type="structural_analysis",
                label="Structural Analysis",
                url="https://beta.viktor.ai/workspaces/4672/app/editor/2394",
                depends_on=["wind"],
            ),
            DummyWorkflowNode(
                node_id="footing_cap",
                node_type="footing_capacity",
                label="Footing Capacity",
                url="https://beta.viktor.ai/workspaces/4682/app/editor/2404",
            ),
            DummyWorkflowNode(
                node_id="footing_design",
                node_type="footing_design",
                label="Footing Design",
                url="https://beta.viktor.ai/workspaces/4672/app/editor/2394",
                depends_on=["structural", "footing_cap"],
            ),
        ],
    )

    result = await compose_workflow_graph_func(None, payload.model_dump_json())
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
