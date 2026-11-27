"""Isolated Judge & Independent Oracle subsystem for Blind Challenge Lab (Prompt 13)."""

from app.judge.eval_harness import run_baseline_comparison_harness
from app.judge.oracle import evaluate_blind_challenge

__all__ = ["evaluate_blind_challenge", "run_baseline_comparison_harness"]
