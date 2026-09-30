from django.db import models


class FuelStation(models.Model):
    opis_truckstop_id = models.IntegerField()
    truckstop_name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    rack_id = models.IntegerField()
    retail_price = models.DecimalField(max_digits=12, decimal_places=8)

    class Meta:
        indexes = [
            models.Index(fields=['state', 'city'], name='fuel_station_state_city_idx'),
        ]
