from __future__ import annotations

COUNTRIES = [
    ("JP", "Japan"),
    ("FR", "France"),
    ("IT", "Italy"),
    ("ES", "Spain"),
    ("PT", "Portugal"),
    ("GR", "Greece"),
    ("US", "United States"),
    ("CA", "Canada"),
    ("BR", "Brazil"),
    ("AU", "Australia"),
]

CITIES = [
    ("TYO", "Tokyo", "JP"),
    ("KYO", "Kyoto", "JP"),
    ("OSA", "Osaka", "JP"),
    ("PAR", "Paris", "FR"),
    ("LYO", "Lyon", "FR"),
    ("NCE", "Nice", "FR"),
    ("ROM", "Rome", "IT"),
    ("FLR", "Florence", "IT"),
    ("VCE", "Venice", "IT"),
    ("MAD", "Madrid", "ES"),
    ("BCN", "Barcelona", "ES"),
    ("SVQ", "Seville", "ES"),
    ("LIS", "Lisbon", "PT"),
    ("OPO", "Porto", "PT"),
    ("FAO", "Faro", "PT"),
    ("ATH", "Athens", "GR"),
    ("SKG", "Thessaloniki", "GR"),
    ("JTR", "Santorini", "GR"),
    ("NYC", "New York", "US"),
    ("SFO", "San Francisco", "US"),
    ("HNL", "Honolulu", "US"),
    ("YYZ", "Toronto", "CA"),
    ("YVR", "Vancouver", "CA"),
    ("YUL", "Montreal", "CA"),
    ("RIO", "Rio de Janeiro", "BR"),
    ("SAO", "Sao Paulo", "BR"),
    ("SSA", "Salvador", "BR"),
    ("SYD", "Sydney", "AU"),
    ("MEL", "Melbourne", "AU"),
    ("BNE", "Brisbane", "AU"),
]

ATTRACTION_TYPES = [
    "Historic Quarter",
    "National Museum",
    "Botanical Garden",
    "Riverside Walk",
    "Central Market",
    "Art Museum",
    "Scenic Lookout",
    "Cultural Centre",
    "Old Town Square",
    "Heritage District",
]

HOTEL_NAMES = [
    "Harborlight Hotel",
    "Juniper House",
    "Grand Meridian",
    "The Lantern Inn",
    "Cedar & Stone",
    "Blue Horizon Suites",
    "Old Town Residence",
    "The Gallery Hotel",
    "Parkside Rooms",
    "Riverside Lodge",
]

AIRLINE_NAMES = [
    "Northstar Air",
    "Bluebird Airways",
    "Cedar Airlines",
    "Meridian Air",
    "Sunrise Connect",
    "Atlas Wings",
]

ACTIVITY_NAMES = [
    "Guided history walk",
    "Local food tasting",
    "Architecture tour",
    "Museum highlights",
    "Coastal day trip",
    "Market discovery",
    "Garden and nature walk",
    "Evening cultural show",
    "Small-group city tour",
    "Cooking workshop",
]


def attraction_rows() -> list[tuple[str, str, str]]:
    return [
        (
            f"attraction-{index + 1:03}",
            f"{CITIES[index % len(CITIES)][1]} {ATTRACTION_TYPES[index % len(ATTRACTION_TYPES)]}",
            CITIES[index % len(CITIES)][0],
        )
        for index in range(100)
    ]
