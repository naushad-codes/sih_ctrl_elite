"""Constraint checks. Missing inputs abstain instead of inferring a diagnosis."""
def evaluate_constraints(projected: dict, inputs: dict) -> list[dict]:
    generation = projected.get("power_available_kw")
    total_load = projected.get("total_load_kw", inputs.get("critical_load_kw"))
    checks = [
        ("POWER_DEFICIT", generation, total_load, "kW", "Projected generation is below critical plus deferrable load."),
        ("FUEL_RESERVE", projected.get("fuel_shortage_l", projected.get("fuel_l")), 0, "L", "Projected fuel demand exceeds available inventory."),
        ("WATER_SHORTAGE", projected.get("water_shortage_l", projected.get("water_l")), 0, "L", "Projected water demand exceeds available storage and production."),
    ]
    results = []
    results.append({"code": "THERMAL_DEFICIT", "status": "UNKNOWN", "unit": "°C", "explanation": "Insufficient evidence: an approved minimum indoor temperature threshold is not configured."})
    for code, value, threshold, unit, explanation in checks:
        if value is None or threshold is None:
            results.append({"code": code, "status": "UNKNOWN", "unit": unit, "explanation": "Insufficient evidence to calculate this constraint."})
        elif (code in {"FUEL_RESERVE", "WATER_SHORTAGE"} and value > threshold) or value < threshold:
            results.append({"code": code, "status": "CONDITIONAL", "unit": unit, "explanation": explanation})
        else:
            results.append({"code": code, "status": "NO_CONSTRAINT_DETECTED", "unit": unit, "explanation": "No constraint detected for the configured scenario assumptions."})
    return results
