"""
Evaluation framework for the retrieval + context stack.

    dataset.py     GoldenCase / GoldenDataset — ground truth
    runner.py      executes the pipeline, capturing every intermediate
    metrics.py     pure metric functions (P/R/F1, MRR, nDCG, groundedness…)
    scoring.py     RunTrace + GoldenCase → CaseScore → AggregateScore
    benchmark.py   fusion-threshold sweep, ranking-weight coordinate descent
    visualizer.py  dependency-free inline SVG charts
    report.py      single-file HTML dashboard + JSON artefact
    cases/         the golden dataset

Read-only with respect to the system under test: nothing here mutates Neo4j,
Qdrant, Postgres or any prompt. It drives the public components exactly as
production does.
"""
from .dataset import GoldenCase, GoldenDataset, load_dataset, save_dataset
from .metrics import mrr, ndcg, precision_at_k, prf, recall_at_k
from .runner import EvaluationRunner, RunnerConfig, RunTrace
from .scoring import AggregateScore, CaseScore, aggregate, score_case

__all__ = [
    "GoldenCase", "GoldenDataset", "load_dataset", "save_dataset",
    "EvaluationRunner", "RunnerConfig", "RunTrace",
    "CaseScore", "AggregateScore", "score_case", "aggregate",
    "prf", "mrr", "ndcg", "precision_at_k", "recall_at_k",
]
