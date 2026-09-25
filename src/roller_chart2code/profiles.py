"""Freeze the actual task prompt, not a reconstructed interpretation of it."""
from __future__ import annotations

import argparse
import hashlib
from importlib.resources import files
import json


PROFILES = {
    "single-render-v2": 1,
    "loop-v1": 3,
    "loop-v2": 3,
    "loop-v3": 3,
    "loop-v4-hard": 20,
    "loop-v5-evidence": 20,
}


def freeze_profile(name: str, reference: str, *, max_renders: int | None = None) -> dict:
    if name not in PROFILES:
        raise ValueError(f"unknown Chart2Code profile: {name}")
    if not isinstance(reference, str) or not reference or any(c in reference for c in "\r\n\0"):
        raise ValueError("reference must be a non-empty single-line path")
    hard = name in {"loop-v4-hard", "loop-v5-evidence"}
    if max_renders is not None and (isinstance(max_renders, bool) or not isinstance(max_renders, int)):
        raise ValueError("max_renders must be an integer")
    if max_renders is not None and not hard and max_renders != PROFILES[name]:
        raise ValueError("fixed profile render budget cannot be silently changed")
    maximum = PROFILES[name] if max_renders is None else max_renders
    if hard and not 4 <= maximum <= 20:
        raise ValueError("hard-loop max_renders must be in 4..20, matching original preparation contract")
    source = files("roller_chart2code").joinpath("prompts", name + ".txt").read_text(encoding="utf-8")
    prompt = source.replace("{reference}", reference).replace("{max_renders}", str(maximum))
    loop = name.startswith("loop-")
    policy = {
        "schema_version": "roller.chart2code-profile/v2",
        "scope": "historical_experiment",
        "name": name, "reference": reference, "max_render_attempts": maximum,
        "prompt_template_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "output_view_policy": "every_successful_render" if loop else "forbidden",
        "revision_policy": "optional" if loop else "forbidden",
        "qualification_ready": True,
        "unresolved_questions": [],
        "visual_quality": "not_evaluated",
    }
    # This digest can be used by resume/aggregation without storing env secrets.
    digest = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**policy, "profile_sha256": digest, "prompt": prompt}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Describe a frozen Chart2Code profile; no writes or model calls")
    parser.add_argument("--profile", choices=tuple(PROFILES), default="single-render-v2")
    parser.add_argument("--reference", default="inputs/reference.png")
    parser.add_argument("--max-renders", type=int)
    parser.add_argument("--include-prompt", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = freeze_profile(args.profile, args.reference, max_renders=args.max_renders)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.include_prompt:
        result.pop("prompt")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
