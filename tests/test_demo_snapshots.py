"""Check the fictional Grafana snapshot payloads made for the public demo."""

import importlib.util
import json
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

    def test_security_snapshot_has_fictional_data_in_every_panel(self) -> None:
        """The Security page's demo snapshot: no live queries, a value for every number and chart, the world map
        from Grafana's own files, and no single addresses."""
        builder = load_builder()
        payload = builder.snapshot("security")
        dashboard = payload["dashboard"]
        self.assertEqual(dashboard["uid"], "nanohpc-demo-security")
        self.assertNotIn("[%", json.dumps(payload))
        for panel in dashboard["panels"]:
            with self.subTest(panel=panel["title"]):
                self.assertEqual(panel["targets"], [])
                if panel["type"] != "text":
                    self.assertTrue(panel["snapshotData"])
        geomap = next(panel for panel in dashboard["panels"] if panel["type"] == "geomap")
        self.assertEqual(geomap["options"]["layers"][0]["location"]["gazetteer"], "public/gazetteer/countries.json")
        # No single addresses: countries and networks only.
        self.assertNotIn("address", json.dumps(payload).lower())


if __name__ == "__main__":
    unittest.main()
