EXTRACT_SYSTEM = """You read one web page about a residential real-estate project in Mumbai and fill a fixed form.

Rules:
- Only record what the page states. If a field is not stated, leave it null. Never estimate or infer numbers.
- Carpet area only. If the page gives built-up or super built-up area and not carpet, leave carpet null.
- Rate per sq ft: record the numeric range. basis = "base" if the page calls it a base/basic rate or price per sq ft of carpet without charges, "all_in" if it says inclusive of all charges / all-inclusive, otherwise "undisclosed".
- If a total price is given for a flat but no per-sq-ft rate, leave the rate null.
- possession: the proposed possession / completion date, as YYYY-MM or YYYY-MM-DD. Prefer the RERA proposed date if both appear.
- status: "new_launch" (recently launched, pre-launch, bookings open), "under_construction", "ready" (ready to move / ready possession),
  "completed" (OC received, completed), "resale" (page is a resale listing), else null.
- rera_numbers: MahaRERA registration numbers of the form P + 11 digits. List every one mentioned for this project.
- building_type: "single" for one tower, "multi_tower" for several towers, "complex" for a township / gated community with many buildings and phases.
- amenities: every project amenity named, one per entry. category: sport_fitness (pool, gym, courts, jogging track, yoga), social_leisure (clubhouse, hall, kids play, garden, deck, lounge, theatre), convenience_safety (CCTV, parking, power backup, EV charging, rain water harvesting, lift, security, intercom), other. scope "unit" only if the amenity is inside the flat (modular kitchen, AC); else "project".
- If the page is about a different project than the one named in the hint, return all nulls.
"""

EXTRACT_USER = """Project hint: {name} by {builder}, near {locality}.
Page source: {source}
Page URL: {url}

Page text:
{text}
"""

SAME_PROJECT_SYSTEM = """You decide whether two records refer to the same residential project in Mumbai.
Treat marketing suffixes, tower or phase names, and the builder's name inside the project name as noise.
Different phases of one master project are the SAME project. A different project by the same builder in the same area is NOT."""

SAME_PROJECT_USER = """Record A: name="{a_name}", builder="{a_builder}", rera="{a_rera}", address="{a_address}"
Record B: name="{b_name}", builder="{b_builder}", rera="{b_rera}", address="{b_address}"
Are A and B the same project?"""

NARRATE_SYSTEM = """You write one short plain-English sentence per item for a builder's sales team comparing nearby projects.
You are given computed numbers only. Do not introduce any number, name, or fact that is not in the input.
Do not restate every number; say what the numbers mean for the comparison (e.g. earliest handover, largest flats, priced above, cannot be verified).
Be direct, no hedging, no marketing tone, at most 25 words per sentence."""

NARRATE_CARDS_USER = """Own project: {own}
Radius: {radius_km} km. Set summary: {set_summary}

Competitors (one sentence each, keyed by id):
{items}
"""

NARRATE_COMPARE_USER = """Own project: {own}
Comparison payload (one sentence per section key: rate, possession, carpet, configurations, structure, amenities):
{payload}
"""
