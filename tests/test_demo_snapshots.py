"""Check the fictional Grafana snapshot payloads made for the public demo."""

import importlib.util
import unittest
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[1] / "src/nanohpc/website-source/scripts/build-demo-snapshots.py"


def load_builder() -> ModuleType:
    """Load the snapshot builder script as a module."""
    spec = importlib.util.spec_from_file_location("build_demo_snapshots", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DemoSnapshotTest(unittest.TestCase):
    """The demo charts use the same units as the deployed dashboards."""

    def test_gpu_memory_is_a_percent(self) -> None:
        """The GPU memory chart's sample values are % of each GPU's memory, between 0 and 100."""
        builder = load_builder()
        for index, machine in enumerate(builder.GPU_NODES):
            values = builder.series("GPU memory used", machine, index, builder.POINTS)
            self.assertTrue(all(0 <= value <= 100 for value in values), (machine, max(values)))


if __name__ == "__main__":
    unittest.main()
