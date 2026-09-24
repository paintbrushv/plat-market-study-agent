import asyncio
import sys

from agents.sdk.runner import run_comp_set_overview

if __name__ == "__main__":
    metro = sys.argv[1] if len(sys.argv) > 1 else "austin"
    print(asyncio.run(run_comp_set_overview(metro)))
