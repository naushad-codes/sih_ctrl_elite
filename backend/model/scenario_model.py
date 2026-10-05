"""Transparent accounting model for scenario projections.

These relationships are prototype bookkeeping equations. They are not calibrated
station physics, and absent thermal parameters deliberately produce UNKNOWN.
"""
from copy import deepcopy
from .constraints import evaluate_constraints

MODEL_VERSION = "v0.2.0-causal-accounting"


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _state_value(baseline: dict, field: str):
    item = baseline.get(field)
    return _number(item.get("value")) if isinstance(item, dict) else None


def run_scenario(baseline: dict, parameters: dict) -> dict:
    """Project fuel, power, thermal and water balances without mutating inputs."""
    base = deepcopy(baseline)
    duration = float(parameters.get("duration_hours", 24))
    chp_available = bool(parameters.get("chp_available", True))
    fuel_rate = _number(parameters.get("fuel_consumption_lph"))
    fuel_inflow_rate = _number(parameters.get("fuel_inflow_lph", 0.0))
    critical = _number(parameters.get("critical_load_kw"))
    deferrable = _number(parameters.get("deferrable_load_kw"))
    water_demand_day = _number(parameters.get("water_demand_l_per_day"))
    generation_baseline = _state_value(base, "power_available")
    fuel_start = _state_value(base, "fuel_reserve")
    water_start = _state_value(base, "water_reserve")
    production_baseline = _state_value(base, "water_production")

    generation = generation_baseline if chp_available else (0.0 if generation_baseline is not None else None)
    power_balance = None if generation is None or critical is None or deferrable is None else generation - critical - deferrable
    total_demand = None if critical is None or deferrable is None else critical + deferrable
    power_deficit = None if generation is None or total_demand is None else max(0.0, total_demand - generation)
    critical_deficit = None if generation is None or critical is None else max(0.0, critical - generation)

    fuel_consumed = None if fuel_rate is None else fuel_rate * duration
    fuel_inflow = None if fuel_inflow_rate is None else fuel_inflow_rate * duration
    fuel_raw_end = None if fuel_start is None or fuel_consumed is None or fuel_inflow is None else fuel_start + fuel_inflow - fuel_consumed
    fuel_end = None if fuel_raw_end is None else max(0.0, fuel_raw_end)
    fuel_shortage = None if fuel_raw_end is None else max(0.0, -fuel_raw_end)
    water_production = production_baseline if chp_available else (0.0 if production_baseline is not None else None)
    water_consumed = None if water_demand_day is None else water_demand_day * duration / 24.0
    water_end = None if water_start is None or water_production is None or water_consumed is None else max(
        0.0, water_start + water_production * duration - water_consumed
    )
    water_shortage = None if water_start is None or water_production is None or water_consumed is None else max(0.0, water_consumed - water_start - water_production * duration)

    # No current scenario form provides validated thermal kWth inputs. Do not
    # reinterpret indoor temperature or outdoor weather as heat output.
    thermal_supply = _number(parameters.get("thermal_supply_kwth"))
    thermal_demand = _number(parameters.get("thermal_demand_kwth"))
    thermal_balance = None if thermal_supply is None or thermal_demand is None else thermal_supply - thermal_demand
    thermal_deficit = None if thermal_balance is None else max(0.0, -thermal_balance)

    projected = {
        "power_available_kw": generation,
        "critical_load_kw": critical,
        "deferrable_load_kw": deferrable,
        "power_balance_kw": power_balance,
        "power_deficit_kw": power_deficit,
        "critical_power_deficit_kw": critical_deficit,
        "fuel_start_l": fuel_start,
        "fuel_consumed_l": fuel_consumed,
        "fuel_inflow_l": fuel_inflow,
        "fuel_l": fuel_end,
        "fuel_shortage_l": fuel_shortage,
        "water_start_l": water_start,
        "water_production_lph": water_production,
        "water_consumed_l": water_consumed,
        "water_l": water_end,
        "water_shortage_l": water_shortage,
        "thermal_supply_kwth": thermal_supply,
        "thermal_demand_kwth": thermal_demand,
        "thermal_balance_kwth": thermal_balance,
        "thermal_deficit_kwth": thermal_deficit,
    }
    model_inputs = {
        "power_available_kw": generation,
        "critical_load_kw": critical,
        "total_load_kw": total_demand,
        "fuel_l": fuel_end,
        "water_l": water_end,
    }
    constraints = [item for item in evaluate_constraints(model_inputs, {"critical_load_kw": critical}) if item.get("code") != "THERMAL_DEFICIT"]
    constraints.append({
        "code": "THERMAL_DEFICIT",
        "status": "UNKNOWN" if thermal_deficit is None else ("CONDITIONAL" if thermal_deficit > 0 else "NO_CONSTRAINT_DETECTED"),
        "value": thermal_deficit,
        "unit": "kWth",
        "explanation": "Thermal supply and demand are not configured." if thermal_deficit is None else "Prototype thermal balance from declared inputs.",
    })
    assumptions = [
        {"name": "CHP electrical relationship", "value": "available uses baseline generation; unavailable projects zero", "unit": "rule", "type": "DEMO ASSUMPTION"},
        {"name": "CHP water-production relationship", "value": "available uses baseline production; unavailable projects zero", "unit": "rule", "type": "DEMO ASSUMPTION"},
        {"name": "Fuel accounting", "value": "start + inflow − burn × duration", "unit": "L", "type": "PROTOTYPE ACCOUNTING"},
        {"name": "Fuel inflow assumption", "value": fuel_inflow_rate, "unit": "L/h", "type": "SCENARIO_ASSUMPTION"},
        {"name": "Water accounting", "value": "start + production × duration − demand × duration / 24", "unit": "L", "type": "PROTOTYPE ACCOUNTING"},
        {"name": "Power balance", "value": "generation − critical load − deferrable load", "unit": "kW", "type": "PROTOTYPE ACCOUNTING"},
        {"name": "Fuel consumption rate", "value": fuel_rate, "unit": "L/h", "type": "SCENARIO_ASSUMPTION"},
        {"name": "Projection duration", "value": duration, "unit": "h", "type": "SCENARIO_ASSUMPTION"},
    ]
    return {
        "projected": projected,
        "constraints": constraints,
        "assumptions": assumptions,
        "uncertainty": {"status": "NOT_MODELED", "fields": [], "explanation": "No validated uncertainty envelope is available for this prototype model."},
        "uncertainties": [
            "Generator efficiency and fuel inflow are UNKNOWN.",
            "Thermal supply/demand are not configured; thermal balance remains UNKNOWN.",
            "Relationships are transparent prototype accounting assumptions, not station-calibrated physics.",
        ],
        "model_version": MODEL_VERSION,
    }
