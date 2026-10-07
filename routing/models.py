from django.db import models


class FuelStation(models.Model):
    """A fuel station imported from fuel-prices-for-be-assessment.csv.

    Field names follow the dataset's actual columns. The supplied CSV
    contains no coordinate columns, so latitude/longitude remain NULL
    until a later section populates them (geocoding is out of scope
    for Section 2).
    """

    # Original station identifier ("OPIS Truckstop ID"); unique so the
    # same station can never be stored twice.
    opis_truckstop_id = models.CharField(max_length=16, unique=True)
    truckstop_name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    # "Rack ID" — source information from the CSV (not required).
    rack_id = models.CharField(max_length=16, null=True, blank=True)
    # "Retail Price" — DecimalField, never FloatField, for money.
    retail_price = models.DecimalField(max_digits=10, decimal_places=8)
    # The dataset has no coordinates; nullable for now.
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    # Section 3: recorded failed geocoding attempts (preprocessing command).
    # A station with coordinates is never geocoded again; a station marked
    # failed is skipped on later runs so batches stay restartable.
    geocode_failed = models.BooleanField(default=False)
    geocode_error = models.TextField(blank=True, default="")

    def __str__(self):
        return f"{self.truckstop_name} ({self.city}, {self.state})"
