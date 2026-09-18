"""
Request/response schema for POST /optimize-energy.

Field names, types and cardinalities follow the Problem Statement (sections
07 and 10) exactly. The judge harness checks these by exact field name, so
nothing here should be renamed without also updating the spec reference.
"""
from rest_framework import serializers


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------

class HourEntrySerializer(serializers.Serializer):
    hour = serializers.IntegerField(min_value=0, max_value=23)
    demand_kwh = serializers.FloatField(min_value=0)
    solar_kwh = serializers.FloatField(min_value=0)
    tariff_bdt_per_kwh = serializers.FloatField(min_value=0)


class BatterySerializer(serializers.Serializer):
    capacity_kwh = serializers.FloatField(min_value=0)
    initial_energy_kwh = serializers.FloatField(min_value=0)
    minimum_energy_kwh = serializers.FloatField(min_value=0)
    max_charge_kwh_per_hour = serializers.FloatField(min_value=0)
    max_discharge_kwh_per_hour = serializers.FloatField(min_value=0)

    def validate(self, data):
        if data["initial_energy_kwh"] > data["capacity_kwh"]:
            raise serializers.ValidationError(
                "initial_energy_kwh cannot exceed capacity_kwh."
            )
        if data["minimum_energy_kwh"] > data["capacity_kwh"]:
            raise serializers.ValidationError(
                "minimum_energy_kwh cannot exceed capacity_kwh."
            )
        return data


class OptimizeRequestSerializer(serializers.Serializer):
    scenario_id = serializers.CharField(max_length=200)
    operator_notes = serializers.ListField(
        child=serializers.CharField(allow_blank=False, trim_whitespace=True),
        min_length=1,
        max_length=3,
    )
    hours = serializers.ListField(
        child=HourEntrySerializer(), min_length=24, max_length=24
    )
    battery = BatterySerializer()

    def validate_hours(self, hours):
        seen = sorted(h["hour"] for h in hours)
        if seen != list(range(24)):
            raise serializers.ValidationError(
                "hours must contain exactly one entry for each hour 0..23."
            )
        return hours

    def validate(self, data):
        # Re-order hours by hour index so downstream code can index directly.
        data["hours"] = sorted(data["hours"], key=lambda h: h["hour"])
        return data


# ---------------------------------------------------------------------------
# Response schema (used for internal consistency / optional serialization
# of the dict the optimizer returns; the view builds this dict directly).
# ---------------------------------------------------------------------------

class DirectiveInterpretationSerializer(serializers.Serializer):
    note_index = serializers.IntegerField()
    applies = serializers.BooleanField()
    directive_type = serializers.CharField()
    structured_adjustment = serializers.JSONField(allow_null=True)
    explanation = serializers.CharField()


class HourlyPlanEntrySerializer(serializers.Serializer):
    hour = serializers.IntegerField()
    grid_kwh = serializers.FloatField()
    solar_used_kwh = serializers.FloatField()
    battery_action = serializers.ChoiceField(choices=["charge", "discharge", "idle"])
    battery_kwh = serializers.FloatField()
    battery_energy_after_kwh = serializers.FloatField()


class OptimizeResponseSerializer(serializers.Serializer):
    scenario_id = serializers.CharField()
    directive_interpretation = DirectiveInterpretationSerializer(many=True)
    hourly_plan = HourlyPlanEntrySerializer(many=True)
    total_grid_kwh = serializers.FloatField()
    total_cost_bdt = serializers.FloatField()
    peak_grid_kwh = serializers.FloatField()
    plan_summary = serializers.CharField()