import runpy
from pathlib import Path

EXAMPLES = Path(__file__).parent.parent / "examples"


def test_shop_example_runs_and_its_results_hold():
    runpy.run_path(str(EXAMPLES / "shop.py"), run_name="__main__")
