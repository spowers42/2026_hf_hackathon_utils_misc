# Trail Fact Sheet Generation Prompt

Use this prompt with Claude (with web search enabled) to generate trail fact
sheets for the remaining ~76 trails. Run one trail at a time. You only need
to provide the trail name and route variant — Claude researches everything else.

---

## Instructions for the generating LLM

You are populating a structured trail fact sheet for a White Mountains NH
hiking planning tool. Before writing any JSON, use web search to look up
authoritative data for this trail from the sources listed below. Then output
a single valid JSON object matching the schema exactly.

**Step 1 — Research first.** Search for the trail using these sources in
priority order:

1. **AMC White Mountain Guide** data (distances, elevation gains, times) —
   search for "[trail name] AMC White Mountain Guide" to find cited figures
2. **Peakbagger.com** — for peak elevations, coordinates, and route data
3. **USFS White Mountain National Forest** (fs.usda.gov/whitemountain) —
   for trailhead details, parking, fees, and any current advisories
4. **AllTrails** (alltrails.com) — for the specific trail URL slug,
   user-reported conditions, and above-treeline estimates
5. **Mt. Washington Observatory** (mountwashington.org) — for any
   Presidential Range or above-treeline weather context

Prioritize AMC Guide figures for distance and elevation gain when they
conflict with AllTrails — AllTrails tends to round and sometimes measures
different route variants.

**Step 2 — Write the JSON.** Apply the rules below strictly, then output
only the JSON. No preamble, explanation, or markdown fences.

---

## Rules

**1. Never invent factual data.**
If after searching you cannot find a confident value for a required numeric
field (distance, elevation, coordinates), write `"VERIFY"` as the value.
A human reviewer will check all `"VERIFY"` entries before the record enters
the training set. It is better to flag uncertainty than to guess.

**2. Weather exposure and fall risk are different hazards. Never conflate them.**

- **Weather exposure** = vulnerability to wind, cold, rain, lightning, and
  rapid condition changes due to above-treeline terrain or long commitment
  distances from shelter. Treeline in the Whites is ~4,400 ft.
- **Fall risk** = the consequence of a slip or stumble due to steep terrain,
  drop-offs, scrambling sections, wet or icy rock, or narrow ridges with
  significant falls on one or both sides.

A trail can have severe weather exposure and minimal fall risk (e.g. a broad
open plateau with a well-graded path), or high fall risk and no weather
exposure (e.g. a steep technical trail entirely below treeline). Assess each
independently in the `exposure` block and in every hazard entry.

**3. Every hazard entry requires `hazard_category`.**
- `"weather-and-environment"`: weather-exposure, hypothermia-risk,
  lightning, ice-and-snow, stream-crossing, navigation, crowd-congestion
- `"terrain-and-fall"`: steep-terrain-fall-risk, exposed-ridge-fall-risk,
  scrambling-fall-risk, waterfall-proximity-fall-risk, rockfall

**4. Difficulty ratings are defined scales — apply them consistently.**

`difficulty.fitness` (cardiovascular/muscular demand):
- 1 = <500 ft gain, <3 miles
- 2 = 500–1,500 ft gain, 3–5 miles
- 3 = 1,500–2,500 ft gain, 5–8 miles
- 4 = 2,500–3,500 ft gain, 8–12 miles
- 5 = >3,500 ft gain or >12 miles, or both

`difficulty.technical` (skill demand — specifically fall-risk skill):
- 1 = graded trail, no drops, no hands needed
- 2 = occasional uneven terrain, minor rock steps
- 3 = steep sections or scrambling requiring careful foot placement
- 4 = sustained scrambling, hands often needed, drop-off consequences
- 5 = technical climbing moves, serious fall consequences throughout

`difficulty.notes` must clearly separate what is a fitness challenge from
what is a technical/fall-risk challenge.

**5. `above_treeline_miles` is a weather exposure metric** — the miles a
hiker spends without tree shelter. Use 4,400 ft as the treeline threshold.
If you cannot find a reliable figure, estimate based on where the route
profile crosses 4,400 ft and write `"VERIFY"` in the notes.

**6. Links: use real URLs, mark uncertain ones.**
Always include these link types when applicable:
- `official-trail-page` — USFS WMNF page for this trail (always include)
- `weather-forecast` — Observatory Higher Summits Forecast (any above-treeline route)
- `alltrails` — the specific AllTrails page for this route
- `amc-hut-reservation` — if an AMC hut is on or near the route
- `map-download` — AMC map page if relevant

Use the verified URL you found in your research. If you found the AllTrails
or USFS page during research, use that exact URL. If you did not find a
specific page, write `"VERIFY"` as the URL — never construct a URL by
guessing at slugs.

Stable base URLs for reference:
- USFS WMNF: `https://www.fs.usda.gov/recarea/whitemountain/`
- Observatory Higher Summits: `https://www.mountwashington.org/experience-the-weather/higher-summits-forecast/`
- AMC huts: `https://www.outdoors.org/destinations/new-hampshire/[hut-name]-hut/`

**7. `progression` fields reference trail IDs from the list below.**
Only reference trails that genuinely prepare for or naturally follow this
one. Do not force connections. Use `[]` rather than inventing references.

**8. `response_hints.safety_callout` must name the hazard type.**
Explicitly state whether the primary hazard is weather exposure, fall risk,
or both. A generic "be careful" is not acceptable.

**9. Voice in `response_hints`.**
- `beginner_framing`: warm, encouraging, honest about challenge, addresses
  the nervous first-timer or someone working toward the 4K footer list
- `expert_framing`: terse, peer tone, practical details the experienced
  hiker actually wants (route variants, conditions nuance, extensions)

---

## Known trail IDs (for progression references)

```
welch-dickey-loop
lonesome-lake-loop
mt-tecumseh
mt-moosilauke-beaver-brook
franconia-ridge-loop
mt-flume-liberty-loop
mt-garfield
mt-lafayette-bridle-path
mt-washington-lions-head
mt-washington-tuckerman-ravine
mt-washington-ammonoosuc-ravine
mt-adams-airline
mt-jefferson-caps-ridge
mt-madison-watson-path
mt-monroe-loop
mt-eisenhower-loop
mt-pierce-crawford-path
mt-jackson-webster-cliff
mt-carrigain-signal-ridge
mt-bond-bondcliff-loop
mt-north-twin-south-twin
mt-zealand-mt-hale
mt-carter-dome-19-mile
mt-wildcat-a-wildcat-ski-area
mt-moriah-kenduskeag
mt-cabot-kilkenny-ridge
mt-waumbek-starr-king
mt-owl-head
pemi-loop
zealand-notch-day-hike
```

---

## Schema reference

```json
{
  "id": "slug-style-id",
  "name": "Full Route Name",
  "peaks": [{"name": "...", "elevation_ft": 0, "is_4k_footer": true}],
  "region": "Presidential Range|Franconia Ridge|Pemigewasset Wilderness|Carter-Moriah Range|Sandwich Range|Cannon-Kinsman|Mahoosuc Range|Northern Presidentials|Southern Whites",
  "stats": {
    "distance_rt_miles": 0.0,
    "elevation_gain_ft": 0,
    "highest_point_ft": 0,
    "above_treeline_miles": 0.0,
    "typical_time_hrs": {"beginner": 0.0, "average": 0.0, "experienced": 0.0}
  },
  "difficulty": {
    "fitness": 1,
    "technical": 1,
    "overall_label": "easy|moderate|hard|very hard|expert only",
    "notes": "Separate fitness challenge from technical/fall-risk challenge explicitly."
  },
  "exposure": {
    "weather": {
      "rating": "none|low|moderate|high|severe",
      "description": "What creates weather exposure. Miles above treeline. Distance from shelter.",
      "mitigation": "What the hiker can do about it."
    },
    "fall_risk": {
      "rating": "none|low|moderate|high|severe",
      "description": "Where fall risk exists. Terrain type. Consequence of a fall.",
      "mitigation": "What the hiker can do about it."
    }
  },
  "seasons": {
    "recommended": ["Month"],
    "viable_with_gear": ["Month"],
    "avoid": ["Month"],
    "seasonal_notes": "..."
  },
  "tags": ["above-treeline-weather-exposure|fall-exposure-moderate|fall-exposure-high|scrambling|water-crossings|winter-viable|snowshoe-route|family-friendly|dog-friendly|amc-hut-nearby|lean-to-available|4k-footer|ridge-walk|loop-route|out-and-back|requires-permit|parking-fee|trail-running-popular|bushwhack-sections"],
  "hazards": [
    {
      "type": "weather-exposure|hypothermia-risk|lightning|ice-and-snow|stream-crossing|rockfall|navigation|crowd-congestion|steep-terrain-fall-risk|exposed-ridge-fall-risk|scrambling-fall-risk|waterfall-proximity-fall-risk",
      "hazard_category": "weather-and-environment|terrain-and-fall",
      "description": "Specific to this trail. Never generic.",
      "months_active": ["Month"]
    }
  ],
  "gear_tiers": {
    "three_season": ["..."],
    "shoulder_season": ["..."],
    "winter": ["..."]
  },
  "trailhead": {
    "name": "...", "lat": 0.0, "lon": 0.0,
    "parking": "...", "access_road": "...",
    "nearest_town": "...", "amenities": ["..."]
  },
  "amc_hut": null,
  "links": [
    {
      "type": "official-trail-page|alltrails|weather-forecast|permit-reservation|trip-report|map-download|amc-hut-reservation|summit-conditions",
      "label": "Human-readable label",
      "url": "https://... or VERIFY",
      "notes": "When the agent should surface this link"
    }
  ],
  "progression": {
    "good_next_step_after": ["trail-id"],
    "leads_naturally_to": ["trail-id"],
    "progression_notes": "Where this trail fits in a build-up ladder toward Mt. Washington or the 4K footer list."
  },
  "response_hints": {
    "beginner_framing": "Warm, encouraging, honest about challenge.",
    "expert_framing": "Terse, peer tone, practical details.",
    "safety_callout": "Must name weather hazard, fall hazard, or both. Trail-specific."
  }
}
```

---

## Request format

To generate a fact sheet, send this prompt followed by:

```
Generate a trail fact sheet for: [TRAIL NAME]
Route variant: [e.g. "via Beaver Brook Trail" or "standard loop" or "via Caps Ridge Trail"]
```

That is all the input needed. Research the rest.
