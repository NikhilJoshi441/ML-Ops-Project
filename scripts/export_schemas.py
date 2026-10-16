"""Writes the published data contracts (JSON Schema) to contracts/."""
import json
from pathlib import Path

from triage.contracts.dispute import DisputeReport, FeedbackRequest
from triage.contracts.results import Recommendation, TriageBrief

out = Path(__file__).resolve().parents[1] / "contracts"
for name, model in {"dispute_report.v1": DisputeReport, "feedback.v1": FeedbackRequest,
                    "triage_brief.v1": TriageBrief, "run_result.v1": Recommendation}.items():
    (out / f"{name}.schema.json").write_text(json.dumps(model.model_json_schema(), indent=1))
    print("wrote", name)
