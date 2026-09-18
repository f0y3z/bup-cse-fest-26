import logging

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from .serializers import OptimizeRequestSerializer
from .guardrails import build_directive_interpretation
from .llm import parse_operator_notes
from .optimizer import solve_energy_optimization, OptimizationError

logger = logging.getLogger(__name__)


@api_view(['GET'])
def health_check(request):
    return Response({"status": "ok"}, status=status.HTTP_200_OK)


@api_view(['POST'])
def optimize_energy(request):
    # 1. Validate Request Schema (Returns 400 Bad Request on failure)
    serializer = OptimizeRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    validated_payload = serializer.validated_data

    try:
        # 2. LLM Translation (Raw output).
        #    The battery block is passed so the model can resolve relative
        #    notes such as "keep 50% of battery capacity in reserve".
        raw_directives = parse_operator_notes(
            validated_payload["operator_notes"],
            validated_payload["battery"],
        )

        # 3. Guardrail Validation (Guarantees safe, cleaned directives)
        validated_directives = build_directive_interpretation(
            raw_results=raw_directives,
            operator_notes=validated_payload["operator_notes"],
            battery_capacity_kwh=validated_payload["battery"]["capacity_kwh"],
        )

        # 4. Math Optimization & Final Validation
        result = solve_energy_optimization(validated_payload, validated_directives)
        return Response(result, status=status.HTTP_200_OK)

    except OptimizationError:
        # Controlled failure for an infeasible scenario. Details go to the log,
        # never to the response body.
        logger.exception("Optimization failed for scenario %s",
                         validated_payload.get("scenario_id"))
        return Response(
            {"error": "No feasible schedule could be produced for this scenario."},
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    except Exception:
        # Catch-all. Do not expose exception text, stack traces, or config.
        logger.exception("Internal error for scenario %s",
                         validated_payload.get("scenario_id"))
        return Response(
            {"error": "Internal server error."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )