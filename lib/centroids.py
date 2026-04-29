"""
lib/centroids.py — Geographic centroid lookups for approximate geocoding.

Cropcirclecenter (CCC) reports country + county + landmark prose but no GPS.
For the public map we approximate location as the centroid of the county
(when known) or country (fallback). Each lookup returns the centroid plus
an estimated precision radius in meters — the public site uses precision
to render approximate dots distinctly from EXIF-exact dots.

Centroid sources: Wikipedia ceremonial/historic county and country pages,
2026; rounded to 4 decimal degrees (~11 m) which is irrelevant given the
precision radius. Hand-curated, not exhaustive.
"""
from __future__ import annotations

from typing import Optional

# County name -> (lat, lng, approx_radius_m)
# Radius is roughly half the county's diameter — populated dots will jitter
# within this radius once we add jitter on the client.
UK_COUNTIES: dict[str, tuple[float, float, int]] = {
    # English ceremonial counties most relevant to crop circles
    "wiltshire":         (51.3492,  -1.9927, 30000),
    "hampshire":         (51.0577,  -1.3081, 35000),
    "oxfordshire":       (51.7613,  -1.2475, 30000),
    "berkshire":         (51.4540,  -1.0349, 25000),
    "somerset":          (51.0608,  -2.9637, 40000),
    "dorset":            (50.7488,  -2.3445, 30000),
    "gloucestershire":   (51.8642,  -2.2382, 30000),
    "buckinghamshire":   (51.8072,  -0.8127, 28000),
    "cambridgeshire":    (52.2053,   0.1218, 35000),
    "essex":             (51.7659,   0.5715, 35000),
    "suffolk":           (52.1872,   0.9708, 40000),
    "norfolk":           (52.6140,   0.8864, 50000),
    "kent":              (51.2787,   0.5217, 40000),
    "surrey":            (51.2362,  -0.5704, 22000),
    "sussex":            (50.9095,  -0.4517, 40000),
    "east sussex":       (50.9097,   0.2710, 30000),
    "west sussex":       (50.9281,  -0.5165, 30000),
    "devon":             (50.7156,  -3.5309, 50000),
    "cornwall":          (50.4429,  -4.7900, 50000),
    "warwickshire":      (52.2823,  -1.5849, 30000),
    "worcestershire":    (52.1923,  -2.2200, 25000),
    "herefordshire":     (52.0765,  -2.6544, 30000),
    "shropshire":        (52.6160,  -2.7450, 40000),
    "staffordshire":     (52.8793,  -2.0573, 35000),
    "leicestershire":    (52.6369,  -1.1398, 30000),
    "northamptonshire":  (52.2740,  -0.8757, 30000),
    "bedfordshire":      (52.0024,  -0.4658, 22000),
    "hertfordshire":     (51.8098,  -0.2377, 25000),
    "lincolnshire":      (53.2308,  -0.5407, 50000),
    "nottinghamshire":   (53.1000,  -1.0000, 30000),
    "derbyshire":        (53.1305,  -1.5510, 35000),
    "yorkshire":         (53.9591,  -1.0815, 70000),  # historic county - whole region
    "north yorkshire":   (54.2510,  -1.4140, 60000),
    "south yorkshire":   (53.5440,  -1.3940, 25000),
    "west yorkshire":    (53.7997,  -1.5492, 28000),
    "east yorkshire":    (53.8420,  -0.4321, 35000),
    "cheshire":          (53.2120,  -2.5700, 30000),
    "lancashire":        (53.7632,  -2.7032, 35000),
    "cumbria":           (54.5772,  -2.7975, 50000),
    "northumberland":    (55.2083,  -2.0784, 50000),
    # Welsh
    "powys":             (52.2870,  -3.4360, 50000),
    "gwynedd":           (52.9279,  -4.0581, 50000),
    "carmarthenshire":   (51.8737,  -4.3081, 35000),
    # Scottish (rare but possible)
    "perthshire":        (56.5950,  -3.7400, 45000),
    "fife":              (56.2082,  -3.1495, 25000),
    # Northern Irish
    "down":              (54.4053,  -5.7340, 25000),
}

# ISO 3166-1 alpha-2 -> (lat, lng, radius_m). Radius = approximate country
# half-extent; coarse but gives a single dot per country.
COUNTRIES: dict[str, tuple[float, float, int]] = {
    "GB": (54.0000,  -2.0000, 400000),  # United Kingdom
    "UK": (54.0000,  -2.0000, 400000),  # alias seen in CCC
    "US": (39.8283, -98.5795, 1500000),
    "CA": (56.1304, -106.3468, 2000000),
    "DE": (51.1657,  10.4515, 350000),
    "NL": (52.1326,   5.2913, 150000),
    "BE": (50.5039,   4.4699, 100000),
    "CH": (46.8182,   8.2275, 120000),
    "AT": (47.5162,  14.5501, 200000),
    "FR": (46.6034,   1.8883, 450000),
    "IT": (41.8719,  12.5674, 400000),
    "ES": (40.4637,  -3.7492, 400000),
    "PT": (39.3999,  -8.2245, 200000),
    "PL": (51.9194,  19.1451, 300000),
    "CZ": (49.8175,  15.4730, 150000),
    "SK": (48.6690,  19.6990, 130000),
    "HU": (47.1625,  19.5033, 150000),
    "RU": (61.5240, 105.3188, 3000000),
    "RO": (45.9432,  24.9668, 200000),
    "BG": (42.7339,  25.4858, 150000),
    "GR": (39.0742,  21.8243, 250000),
    "HR": (45.1000,  15.2000, 150000),
    "SI": (46.1512,  14.9955, 80000),
    "BA": (43.9159,  17.6791, 100000),
    "SE": (60.1282,  18.6435, 600000),
    "NO": (60.4720,   8.4689, 600000),
    "FI": (61.9241,  25.7482, 500000),
    "DK": (56.2639,   9.5018, 150000),
    "IS": (64.9631, -19.0208, 200000),
    "IE": (53.4129,  -8.2439, 180000),
    "BR": (-14.2350, -51.9253, 1500000),
    "AR": (-38.4161, -63.6167, 1300000),
    "CL": (-35.6751, -71.5430, 1000000),
    "MX": (23.6345, -102.5528, 1100000),
    "ZA": (-30.5595,  22.9375, 700000),
    "AU": (-25.2744, 133.7751, 1800000),
    "NZ": (-40.9006, 174.8860, 600000),
    "JP": (36.2048, 138.2529, 600000),
    "CN": (35.8617, 104.1954, 2000000),
    "IN": (20.5937,  78.9629, 1500000),
    "TR": (38.9637,  35.2433, 600000),
    "IL": (31.0461,  34.8516, 100000),
    "RS": (44.0165,  21.0059, 130000),
    "EE": (58.5953,  25.0136, 130000),
    "LV": (56.8796,  24.6032, 130000),
    "LT": (55.1694,  23.8813, 130000),
    "UA": (48.3794,  31.1656, 500000),
}


def lookup_uk_county(county_text: Optional[str]) -> Optional[tuple[float, float, int]]:
    if not county_text:
        return None
    key = county_text.strip().lower()
    # Light normalization for common variants
    key = key.replace("&", "and")
    key = key.removeprefix("the ").strip()
    if key in UK_COUNTIES:
        return UK_COUNTIES[key]
    # Some pages use "County Down", "Co. Wiltshire" etc.
    for prefix in ("county ", "co. ", "co "):
        if key.startswith(prefix):
            stripped = key[len(prefix):].strip()
            if stripped in UK_COUNTIES:
                return UK_COUNTIES[stripped]
    return None


def lookup_country(country_code: Optional[str]) -> Optional[tuple[float, float, int]]:
    if not country_code:
        return None
    return COUNTRIES.get(country_code.strip().upper())


# Map common CCC country names + flag codes to ISO-2.
COUNTRY_TEXT_TO_CODE: dict[str, str] = {
    "england":         "GB",
    "scotland":        "GB",
    "wales":           "GB",
    "northern ireland":"GB",
    "ireland":         "IE",
    "united kingdom":  "GB",
    "uk":              "GB",
    "usa":             "US",
    "united states":   "US",
    "us":              "US",
    "germany":         "DE",
    "deutschland":     "DE",
    "netherlands":     "NL",
    "the netherlands": "NL",
    "holland":         "NL",
    "belgium":         "BE",
    "switzerland":     "CH",
    "schweiz":         "CH",
    "austria":         "AT",
    "france":          "FR",
    "italy":           "IT",
    "italia":          "IT",
    "spain":           "ES",
    "portugal":        "PT",
    "poland":          "PL",
    "czech republic":  "CZ",
    "czechia":         "CZ",
    "slovakia":        "SK",
    "hungary":         "HU",
    "russia":          "RU",
    "russian federation":"RU",
    "romania":         "RO",
    "bulgaria":        "BG",
    "greece":          "GR",
    "croatia":         "HR",
    "slovenia":        "SI",
    "bosnia":          "BA",
    "bosnia and herzegovina":"BA",
    "sweden":          "SE",
    "norway":          "NO",
    "finland":         "FI",
    "denmark":         "DK",
    "iceland":         "IS",
    "brazil":          "BR",
    "brasil":          "BR",
    "argentina":       "AR",
    "chile":           "CL",
    "mexico":          "MX",
    "south africa":    "ZA",
    "australia":       "AU",
    "new zealand":     "NZ",
    "japan":           "JP",
    "china":           "CN",
    "india":           "IN",
    "turkey":          "TR",
    "israel":          "IL",
    "serbia":          "RS",
    "estonia":         "EE",
    "latvia":          "LV",
    "lithuania":       "LT",
    "ukraine":         "UA",
    "canada":          "CA",
}


def country_to_iso2(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    return COUNTRY_TEXT_TO_CODE.get(text.strip().lower())
