import asyncio
from textwrap import dedent

from agents import Agent, Runner
from dotenv import load_dotenv
from app.tools import get_tools

load_dotenv()
agent = Agent(
    name="Viktor Agent",
    instructions=dedent(
        """You are a helpful assistant that replies with succinct, on-point responses and uses tools to create
        workflows. Use 'GeometryGeneration' first to define the structure geometry, then run the load estimation
        tools (which require geometry). The 'Structural Analysis' tool requires the load estimation results and
        generates internal forces and reaction loads. Finally, use the 'footing design' tool, which requires the
        reaction loads and depends on the 'footing capacity' tool (which has no dependencies).
    """
    ),
    model="gpt-5",
    tools=get_tools(),
)


async def main():
    result = await Runner.run(
        agent,
        "I want to perform a structural analysis including the main load actions and also design concrete pads. Please come up with a workflow.",
    )
    print(result.final_output)


asyncio.run(main())
