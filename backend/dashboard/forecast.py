"""Honest forecasting for the Dar Al Ber dashboard.

With <2 years of monthly data, seasonal (STL) decomposition is unreliable, so we fit a
LINEAR TREND and attach a genuine 95% PREDICTION INTERVAL derived from the residual
scatter (textbook OLS interval — it widens the further out you project, reflecting real
uncertainty). No fake confidence bands on a bare extrapolation. numpy only.
"""
from __future__ import annotations

import numpy as np


def forecast_series(values: list[float], horizon: int) -> dict:
    y = np.asarray([float(v or 0) for v in values], dtype=float)
    n = int(y.size)
    if n < 4:
        return {"error": "not enough history to forecast (need >= 4 months)"}
    horizon = max(1, min(12, int(horizon)))
    x = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    fitted = slope * x + intercept
    resid = y - fitted
    dof = max(1, n - 2)
    s = float(np.sqrt(np.sum(resid ** 2) / dof))          # residual std error
    xbar = float(x.mean())
    sxx = float(np.sum((x - xbar) ** 2)) or 1.0
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = float(1 - np.sum(resid ** 2) / ss_tot) if ss_tot > 0 else 0.0
    strength = ("none" if r2 < 0.1 else "weak" if r2 < 0.3 else "moderate" if r2 < 0.6 else "strong")
    z = 1.96
    fc = []
    for h in range(1, horizon + 1):
        xf = n - 1 + h
        pt = max(0.0, float(slope * xf + intercept))          # non-negative domain (AED / counts)
        se = s * float(np.sqrt(1.0 + 1.0 / n + (xf - xbar) ** 2 / sxx))  # prediction interval
        fc.append({
            "step_ahead": h,
            "point": round(pt),
            "low95": max(0, round(pt - z * se)),               # clamp: value cannot be negative
            "high95": round(pt + z * se),
        })
    method = ("linear trend (OLS) with 95% prediction interval from residual scatter"
              if r2 >= 0.1 else
              "NO significant linear trend (r2 near 0): the point is roughly the recent average, "
              "shown with a 95% prediction band")
    return {
        "method": method,
        "trend_strength": strength,
        "n_months_fit": n,
        "r2": round(r2, 3),
        "slope_per_month": round(float(slope), 1),
        "forecast": fc,
    }
