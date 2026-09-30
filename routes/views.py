from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .serializers import RouteRequestSerializer
from .services.route_planner import LocationNotFoundError, plan_route
from .services.routing import RoutingConfigurationError, RoutingProviderError, RouteNotFoundError


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
    except LocationNotFoundError as exc:
        # Unresolvable locations consistently use 422; invalid input uses DRF 400.
        return _error('location_not_found', f'Could not resolve the {exc.field} location.', 422)
    except RoutingConfigurationError:
        return _error('routing_configuration_error', 'Routing service is not configured.', 500)
    except RouteNotFoundError:
        return _error('route_not_found', 'No driving route could be found.', 422)
    except RoutingProviderError:
        return _error('routing_provider_error', 'Routing provider could not complete the request.', 502)
    return Response(result.as_dict())


def _error(code, message, status):
    return Response({'error': {'code': code, 'message': message}}, status=status)
