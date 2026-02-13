"""LLM evaluator for snapshot decision timelines.

Loads snapshots, builds eval records, sends to Gemini for structured assessment.
Returns a dict with per-snapshot grades + actionable insights (Tier A/B).
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from trader.db.database import Database, get_snapshot, get_watch_by_snapshot, insert_evaluation
from trader.knowledge.store import KnowledgeStore
from trader.llm.extract import extract_json
from trader.reflection.eval_record import (
    build_eval_record,
    eval_record_to_markdown,
)

EVALUATION_PROMPT = """\
You are evaluating a trading research pipeline's decisions.

## Task
For each decision point in the timeline(s) below, assess:
1. Was the decision appropriate given the available context?
2. Were tools used efficiently (redundant calls, missing tools)?
3. Were there signals the agent missed or misinterpreted?
4. What concrete improvements can be made?

## Timelines

{timelines}

## Output Format (strict JSON)

Return ONLY a JSON object with this structure:
{{
  "snapshots": [
    {{
      "snapshot_id": "...",
      "grade": "A|B|C|D|F",
      "summary": "2-3 sentence assessment",
      "assessments": [
        {{"node_type": "triage|agent_round|tool_call|prediction|watch_entry|watch_exit", "agent": "agent_name_or_empty", "rating": "good|ok|poor", "note": "specific observation"}}
      ]
    }}
  ],
  "insights": [
    {{
      "text": "actionable insight text",
      "tier": "A|B",
      "category": "triage|exploration|tool_use|prediction|watch_management",
      "action": {{}}
    }}
  ]
}}

### Tier A insights (auto-applicable, no code changes):
- {{"type": "add_skip_keyword", "keyword": "..."}} — add headline skip keyword
- {{"type": "add_signal_pattern", "pattern": {{"text": "...", "score": 1}}}} — add signal pattern
- {{"type": "add_anti_pattern", "pattern": {{"text": "...", "score": 1}}}} — add anti-pattern
- {{"type": "add_search_template", "template": "..."}} — add search template
- {{"type": "add_model_note", "note": "..."}} — add model note

### Tier B insights (require code changes):
- {{"type": "code_suggestion", "description": "...", "target": "file_or_module", "rationale": "..."}}

Be specific and actionable. Grade A = excellent decisions, F = critical mistakes.
Only include insights that are clearly supported by the timeline data.
"""


def evaluate_snapshots(
    *,
    snapshot_ids: list[str],
    db: Database,
    knowledge: KnowledgeStore,
    model: str | None = None,
) -> dict[str, Any]:
    """Run LLM evaluation on the given snapshots.

    Returns a dict matching the JSON schema in EVALUATION_PROMPT.
    """
    from google import genai
    from google.genai import types

    model = model or os.getenv("REFLECTION_MODEL", "gemini-3-flash-preview")
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY (or GOOGLE_API_KEY)")

    # Build markdown timelines for each snapshot
    timeline_parts: list[str] = []
    for sid in snapshot_ids:
        snap = get_snapshot(db, sid)
        if snap is None:
            timeline_parts.append(f"## Snapshot {sid}\n\n*Not found in database.*\n")
            continue
        watch = get_watch_by_snapshot(db, sid)
        record = build_eval_record(snap, watch)
        timeline_parts.append(eval_record_to_markdown(record))

    timelines_text = "\n---\n\n".join(timeline_parts)
    prompt = EVALUATION_PROMPT.format(timelines=timelines_text)

    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.3,
        ),
    )

    result = extract_json(resp.text)

    # Ensure expected structure
    if "snapshots" not in result:
        result["snapshots"] = []
    if "insights" not in result:
        result["insights"] = []

    # Persist evaluation to DB
    evaluation_id = str(uuid.uuid4())
    try:
        insert_evaluation(
            db,
            evaluation_id=evaluation_id,
            snapshot_ids=snapshot_ids,
            evaluation=result,
        )
    except Exception:
        pass  # Don't fail the evaluation if persistence fails

    result["evaluation_id"] = evaluation_id
    return result
