from rest_framework import serializers


class LocationField(serializers.CharField):
    def to_internal_value(self, data):
        # DRF's CharField otherwise coerces numbers into strings.
        if not isinstance(data, str):
            self.fail('invalid')
        return super().to_internal_value(data)


class RouteRequestSerializer(serializers.Serializer):
    start = LocationField(max_length=255, trim_whitespace=True)
    finish = LocationField(max_length=255, trim_whitespace=True)

    def validate(self, attrs):
        if attrs['start'].casefold() == attrs['finish'].casefold():
            raise serializers.ValidationError('Start and finish must be different locations.')
        return attrs
