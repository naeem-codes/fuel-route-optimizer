import logging

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .serializers import RouteRequestSerializer
from .services.fuel_optimizer import FuelRouteInfeasibleError
from .services.route_planner import LocationNotFoundError, plan_route
from .services.routing import RoutingConfigurationError, RoutingProviderError, RouteNotFoundError

logger = logging.getLogger(__name__)


@api_view(['GET'])
@permission_classes([AllowAny])
def health(request):
    """Return a basic application health status."""
    return Response({'status': 'ok'})


@api_view(['POST'])
@permission_classes([AllowAny])
def plan_route_view(request):
    serializer = RouteRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        result = plan_route(**serializer.validated_data)
    except FuelRouteInfeasibleError:
        logger.warning('No feasible fuel plan found.')
        return _error('fuel_route_infeasible', 'A feasible fuel plan could not be found for this route.', 422)
    except LocationNotFoundError as exc:
        # Unresolvable locations consistently use 422; invalid input uses DRF 400.
        return _error('location_not_found', f'Could not resolve the {exc.field} location.', 422)
    except RoutingConfigurationError:
        logger.error('Routing configuration unavailable.')
        return _error('routing_configuration_error', 'Routing service is not configured.', 500)
    except RouteNotFoundError:
        return _error('route_not_found', 'No driving route could be found.', 422)
    except RoutingProviderError:
        logger.warning('Routing provider request failed.')
        return _error('routing_provider_error', 'Routing provider could not complete the request.', 502)
    return Response(result.as_dict())


def _error(code, message, status):
    return Response({'error': {'code': code, 'message': message}}, status=status)
