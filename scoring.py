def tier(r): return "critical" if r >= .75 else "high" if r >= .5 else "moderate" if r >= .25 else "low"
def priority(risk, value):
    """Funding priority = ecological/economic value x risk. Multiplicative so a high-risk/low-value reef can't auto-outrank
    a moderate-risk/high-value one. Reefs near certain loss (risk>0.95) are discounted: funding is less likely to change the outcome."""
    return value * risk * (0.8 if risk > 0.95 else 1.0)
ACTIONS = {"critical": ["Deploy emergency shading / cooling-water mitigation where feasible", "Pre-position coral-gardening / larval-rearing teams", "Close site to non-essential tourism and anchoring"],
           "high": ["Increase bleaching surveys to weekly", "Cut local stressors (runoff, fishing pressure)", "Stage rapid-response funding"],
           "moderate": ["Maintain biweekly monitoring", "Prepare response plan, verify partner capacity"], "low": ["Routine monitoring; no extra funding action"]}
