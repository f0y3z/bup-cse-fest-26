import json
from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse
from rest_framework import status

from .serializers import OptimizeRequestSerializer
from .guardrails import build_directive_interpretation, validate_directive
from .optimizer import solve_energy_optimization, OptimizationError


class GridWiseTestCase(TestCase):
    def setUp(self):
        # Build a standard, valid 24-hour test payload
        self.valid_payload = {
            "scenario_id": "TEST-SCENARIO-01",
            "operator_notes": [
                "Solar panel maintenance between 1 PM and 3 PM will drop generation to 20%.",
                "Cafeteria special lunch menu today."
            ],
            "battery": {
                "capacity_kwh": 100.0,
                "initial_energy_kwh": 50.0,
                "minimum_energy_kwh": 10.0,
                "max_charge_kwh_per_hour": 25.0,
                "max_discharge_kwh_per_hour": 25.0
            },
            "hours": [
                {
                    "hour": i,
                    "demand_kwh": 30.0 if 8 <= i <= 18 else 10.0,
                    "solar_kwh": 40.0 if 10 <= i <= 15 else 0.0,
                    "tariff_bdt_per_kwh": 15.0 if 17 <= i <= 21 else 5.0
                }
                for i in range(24)
            ]
        }

    # -----------------------------------------------------------------------
    # 1. Serializer & Request Schema Tests
    # -----------------------------------------------------------------------
    def test_serializer_valid_payload(self):
        serializer = OptimizeRequestSerializer(data=self.valid_payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_serializer_missing_hours_fails(self):
        invalid = self.valid_payload.copy()
        invalid["hours"] = invalid["hours"][:-1]  # Only 23 hours
        serializer = OptimizeRequestSerializer(data=invalid)
        self.assertFalse(serializer.is_valid())
        self.assertIn("hours", serializer.errors)

    def test_serializer_initial_exceeds_capacity_fails(self):
        invalid = self.valid_payload.copy()
        invalid["battery"] = invalid["battery"].copy()
        invalid["battery"]["initial_energy_kwh"] = 150.0  # Capacity is 100
        serializer = OptimizeRequestSerializer(data=invalid)
        self.assertFalse(serializer.is_valid())

    # -----------------------------------------------------------------------
    # 2. Guardrail Validator Tests
    # -----------------------------------------------------------------------
    def test_guardrail_unsupported_directive_type_defaults_to_noop(self):
        raw_llm_output = [
            {
                "note_index": 0,
                "directive_type": "invalid_magic_type",
                "structured_adjustment": {"hours": [12]}
            }
        ]
        validated = build_directive_interpretation(
            raw_llm_output, ["Some note"], battery_capacity_kwh=100.0
        )
        self.assertEqual(len(validated), 1)
        self.assertEqual(validated[0]["directive_type"], "no_op")
        self.assertFalse(validated[0]["applies"])

    def test_guardrail_clamps_solar_factor(self):
        raw_directive = {
            "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [12, 13], "factor": 1.5},
            "explanation": "Out of bounds factor test"
        }
        res = validate_directive(raw_directive, note_index=0, battery_capacity_kwh=100.0)
        self.assertEqual(res["directive_type"], "solar_reduction")
        self.assertEqual(res["structured_adjustment"]["factor"], 1.0)

    # -----------------------------------------------------------------------
    # 3. Optimization & Math Solver Tests
    # -----------------------------------------------------------------------
    def test_solver_executes_successfully(self):
        directives = [
            {
                "note_index": 0,
                "applies": True,
                "directive_type": "solar_reduction",
                "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
                "explanation": "20% solar remaining"
            },
            {
                "note_index": 1,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": "Ignored menu"
            }
        ]
        serializer = OptimizeRequestSerializer(data=self.valid_payload)
        serializer.is_valid()
        result = solve_energy_optimization(serializer.validated_data, directives)

        self.assertIn("hourly_plan", result)
        self.assertEqual(len(result["hourly_plan"]), 24)
        self.assertIn("total_cost_bdt", result)
        
        # Verify end-of-day battery neutrality (EOD SoC == Initial SoC)
        last_hour_soc = result["hourly_plan"][23]["battery_energy_after_kwh"]
        self.assertAlmostEqual(last_hour_soc, self.valid_payload["battery"]["initial_energy_kwh"], delta=0.01)

    # -----------------------------------------------------------------------
    # 4. API End-to-End Endpoint Tests
    # -----------------------------------------------------------------------
    def test_health_check_endpoint(self):
        response = self.client.get(reverse('health_check'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {"status": "ok"})

    @patch('core.views.parse_operator_notes')
    def test_optimize_energy_post_success(self, mock_llm):
        mock_llm.return_value = [
            {
                "note_index": 0,
                "applies": True,
                "directive_type": "no_charge_window",
                "structured_adjustment": {"hours": [17, 18]},
                "explanation": "No charge during peak hours"
            },
            {
                "note_index": 1,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": "Distractor note"
            }
        ]

        response = self.client.post(
            reverse('optimize_energy'),
            data=json.dumps(self.valid_payload),
            content_type="application/json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["scenario_id"], "TEST-SCENARIO-01")
        self.assertEqual(len(data["directive_interpretation"]), 2)