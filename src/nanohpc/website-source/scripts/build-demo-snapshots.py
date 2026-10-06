"""Build fictional Grafana snapshot payloads for the public demo."""

import json
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "files" / "grafana"
END = datetime(2026, 10, 5, 12, tzinfo=UTC)
POINTS = 85
TIMES = [int((END - timedelta(hours=168 - index * 2)).timestamp() * 1000) for index in range(POINTS)]
NODES = ("H100", "H200", "B200", "Threadripper", "DGX")
GPU_NODES = ("H100", "H200", "B200", "DGX")


def series(title: str, name: str, index: int, count: int) -> list[float]:
    """Return stable, smooth sample values at two-hour intervals."""
    seed = sum(ord(char) for char in title) + index * 19
    baseline = {
        "Allocated GPUs": 12,
        "Running jobs": 4,
        "Queue size": 6,
        "Allocated GPU-hours": 35,
        "Fair-share factor": 0.7,
        "CPU in use": 40,
        "Available memory": 180,
        "Available filesystem space": 700,
        "GPU utilization": 55,
        "GPU memory used": 42,
        "GPU temperature": 60,
        "GPU power": 280,
    }[title]
    if title == "Available memory":
        baseline = {"H100": 180, "H200": 180, "B200": 380, "Threadripper": 90, "DGX": 90}[name]
    spread = baseline * 0.2
    unit = 1024**3 if title in ("Available memory", "Available filesystem space", "GPU memory used") else 1
    values = [
        round(
            max(0, baseline + spread * math.sin(step / 7 + seed) + spread * 0.3 * math.cos(step / 13 + seed / 3))
            * unit,
            2,
        )
        for step in range(count)
    ]
    if title in ("Allocated GPUs", "Running jobs", "Queue size"):
        values = [round(value) for value in values]
        if title == "Allocated GPUs":
            values = [min(14, value) for value in values]
        values[-1] = {"Allocated GPUs": 12, "Running jobs": 4, "Queue size": 6}[title]
    return values


def time_frame(title: str, name: str, index: int) -> dict:
    """Give one Grafana series a time field and its fictional values."""
    return {
        "refId": chr(65 + index),
        "name": name,
        "fields": [
            {"name": "Time", "type": "time", "values": TIMES, "config": {}},
            {"name": name, "type": "number", "values": series(title, name, index, POINTS), "config": {}},
        ],
    }


def job_frame() -> dict:
    """Give Grafana's table panel only fictional job records."""
    rows = [
        ("101", "Alice", "protein-fit", "RUNNING", 3, 24, "H100", 2500),
        ("102", "Bob", "image-train", "RUNNING", 3, 32, "H200", 2100),
        ("103", "Mike", "sim-batch", "RUNNING", 2, 16, "DGX", 1900),
        ("104", "Alice", "analysis", "PENDING", 1, 16, "", 1400),
        ("105", "Mike", "benchmark", "PENDING", 1, 12, "", 1100),
        ("106", "Mike", "model-train", "RUNNING", 4, 20, "B200", 1800),
    ]
    columns = (
        ("Job ID", "string"),
        ("User", "string"),
        ("Name", "string"),
        ("State", "string"),
        ("GPUs", "number"),
        ("CPUs", "number"),
        ("Node", "string"),
        ("Priority", "number"),
    )
    return {
        "refId": "A",
        "fields": [
            {"name": name, "type": kind, "values": [row[position] for row in rows], "config": {}}
            for position, (name, kind) in enumerate(columns)
        ],
    }


def snapshot(kind: str) -> dict:
    """Copy the project's panel layout while removing live queries and links."""
    dashboard = json.loads((SOURCE / f"{kind}.json").read_text())
    dashboard["id"] = None
    dashboard["uid"] = f"nanohpc-demo-{kind}"
    dashboard["editable"] = False
    dashboard["refresh"] = ""
    dashboard["time"] = {"from": TIMES[0], "to": TIMES[-1]}
    dashboard["timezone"] = "utc"
    dashboard["templating"] = {"list": []}
    dashboard["annotations"] = {"list": []}
    dashboard["links"] = []
    dashboard["snapshot"] = {"timestamp": END.isoformat()}
    for panel in dashboard["panels"]:
        title = panel["title"]
        panel["datasource"] = None
        panel["targets"] = []
        panel["links"] = []
        panel.pop("transformations", None)
        if kind == "queue":
            panel["title"] = "Running and pending jobs"
            panel["snapshotData"] = [job_frame()]
            panel["fieldConfig"] = {"defaults": {"custom": {"filterable": True}}, "overrides": []}
            panel["options"] = {
                "showHeader": True,
                "cellHeight": "sm",
                "sortBy": [{"displayName": "Priority", "desc": True}],
            }
        else:
            names = (
                (GPU_NODES if title.startswith("GPU ") else NODES)
                if kind == "machines"
                else ("Alice", "Bob", "Mike")
                if kind == "usage"
                else (title,)
            )
            panel["snapshotData"] = [time_frame(title, name, index) for index, name in enumerate(names)]
    return {"dashboard": dashboard, "name": f"nanoHPC demo: {dashboard['title']}"}


def main(output: Path) -> None:
    """Write the four payloads for review or publishing."""
    output.mkdir(parents=True, exist_ok=True)
    for kind in ("queue", "queue-history", "usage", "machines"):
        (output / f"{kind}.json").write_text(json.dumps(snapshot(kind), separators=(",", ":")))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
