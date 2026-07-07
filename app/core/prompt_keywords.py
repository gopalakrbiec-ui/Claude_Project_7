from __future__ import annotations

"""
Tap-to-build prompt keywords — lets users construct a rich generation prompt
by selecting chips instead of typing free text.

Each group is a distinct UI section (collapsible tab/accordion). Tags are
namespaced as "group_key:tag_key" to avoid collisions where the same word
means something slightly different across groups (e.g. "Hugging" under
Romantic Actions vs Motion).

build_prompt_fragment() takes the selected "group:tag" strings and returns
one descriptive sentence fragment ready to prepend/append to a user's free
text prompt or a template's scene_description.
"""

# Each group: (label, emoji, {tag_key: (display_label, descriptive_fragment)})
KEYWORD_GROUPS: dict[str, dict] = {
    "people": {
        "label": "People & Relationships", "emoji": "👥",
        "tags": {
            "alone": ("Alone", "a solo person"),
            "together": ("Together", "people together"),
            "couple": ("Couple", "a romantic couple"),
            "family": ("Family", "a family"),
            "friends": ("Friends", "a group of friends"),
            "parents": ("Parents", "parents"),
            "mother": ("Mother", "a mother"),
            "father": ("Father", "a father"),
            "grandparents": ("Grandparents", "grandparents"),
            "siblings": ("Siblings", "siblings"),
        },
    },
    "expression": {
        "label": "Expressions", "emoji": "😊",
        "tags": {
            "smiling": ("Smiling", "smiling warmly"),
            "laughing": ("Laughing", "laughing joyfully"),
            "happy": ("Happy", "looking happy"),
            "joyful": ("Joyful", "radiating joy"),
            "romantic": ("Romantic", "sharing a romantic moment"),
            "emotional": ("Emotional", "showing deep emotion"),
            "excited": ("Excited", "looking excited"),
            "calm": ("Calm", "calm and serene"),
            "confident": ("Confident", "confident and poised"),
            "serious": ("Serious", "with a serious expression"),
        },
    },
    "romantic_action": {
        "label": "Romantic Actions", "emoji": "❤️",
        "tags": {
            "holding_hands": ("Holding Hands", "holding hands"),
            "hugging": ("Hugging", "hugging each other"),
            "kissing": ("Kissing", "sharing a kiss"),
            "forehead_kiss": ("Forehead Kiss", "sharing a tender forehead kiss"),
            "looking_into_eyes": ("Looking Into Eyes", "looking into each other's eyes"),
            "proposal": ("Proposal", "in a proposal moment, one on one knee"),
            "dancing": ("Dancing", "dancing together"),
            "walking_together": ("Walking Together", "walking together"),
            "cuddling": ("Cuddling", "cuddling closely"),
            "piggyback_ride": ("Piggyback Ride", "sharing a playful piggyback ride"),
        },
    },
    "pose": {
        "label": "Poses", "emoji": "🧍",
        "tags": {
            "standing": ("Standing", "standing pose"),
            "sitting": ("Sitting", "sitting pose"),
            "walking": ("Walking", "walking pose"),
            "running": ("Running", "running pose"),
            "jumping": ("Jumping", "mid-jump pose"),
            "kneeling": ("Kneeling", "kneeling pose"),
            "looking_back": ("Looking Back", "looking back over the shoulder"),
            "looking_up": ("Looking Up", "looking upward"),
            "side_profile": ("Side Profile", "side profile view"),
            "close_up": ("Close-Up", "close-up framing"),
        },
    },
    "celebration": {
        "label": "Celebrations", "emoji": "🎉",
        "tags": {
            "birthday": ("Birthday", "birthday celebration"),
            "wedding": ("Wedding", "wedding celebration"),
            "engagement": ("Engagement", "engagement ceremony"),
            "anniversary": ("Anniversary", "anniversary celebration"),
            "graduation": ("Graduation", "graduation celebration"),
            "baby_shower": ("Baby Shower", "baby shower celebration"),
            "housewarming": ("Housewarming", "housewarming celebration"),
            "festival": ("Festival", "festival celebration"),
            "party": ("Party", "party celebration"),
            "celebration": ("Celebration", "festive celebration atmosphere"),
        },
    },
    "family_moment": {
        "label": "Family Moments", "emoji": "👶",
        "tags": {
            "newborn": ("Newborn", "a newborn baby"),
            "holding_baby": ("Holding Baby", "holding a baby lovingly"),
            "playing_together": ("Playing Together", "playing together"),
            "reading_together": ("Reading Together", "reading together"),
            "cooking_together": ("Cooking Together", "cooking together"),
            "family_dinner": ("Family Dinner", "a family dinner"),
            "picnic": ("Picnic", "a family picnic"),
            "bedtime_story": ("Bedtime Story", "a bedtime story moment"),
            "family_portrait": ("Family Portrait", "a formal family portrait"),
            "reunion": ("Reunion", "a joyful family reunion"),
        },
    },
    "location": {
        "label": "Locations", "emoji": "🌄",
        "tags": {
            "beach": ("Beach", "on a beautiful beach"),
            "mountains": ("Mountains", "in scenic mountains"),
            "waterfall": ("Waterfall", "near a waterfall"),
            "forest": ("Forest", "in a lush forest"),
            "garden": ("Garden", "in a blooming garden"),
            "temple": ("Temple", "at a temple"),
            "palace": ("Palace", "at a grand palace"),
            "village": ("Village", "in a rustic village"),
            "city": ("City", "in a city setting"),
            "home": ("Home", "at home"),
        },
    },
    "time_of_day": {
        "label": "Time of Day", "emoji": "🌅",
        "tags": {
            "sunrise": ("Sunrise", "during sunrise"),
            "morning": ("Morning", "in the morning"),
            "afternoon": ("Afternoon", "in the afternoon"),
            "sunset": ("Sunset", "during sunset"),
            "golden_hour": ("Golden Hour", "during golden hour"),
            "blue_hour": ("Blue Hour", "during blue hour"),
            "night": ("Night", "at night"),
            "moonlight": ("Moonlight", "under moonlight"),
        },
    },
    "weather": {
        "label": "Weather", "emoji": "🌦️",
        "tags": {
            "rain": ("Rain", "in gentle rain"),
            "snow": ("Snow", "amid falling snow"),
            "sunny": ("Sunny", "on a sunny day"),
            "cloudy": ("Cloudy", "under a cloudy sky"),
            "misty": ("Misty", "in misty conditions"),
            "fog": ("Fog", "amid soft fog"),
            "autumn": ("Autumn", "in an autumn setting"),
            "spring": ("Spring", "in a spring setting"),
            "monsoon": ("Monsoon", "during the monsoon season"),
            "winter": ("Winter", "in a winter setting"),
        },
    },
    "clothing": {
        "label": "Clothing", "emoji": "👗",
        "tags": {
            "traditional": ("Traditional", "traditional attire"),
            "casual": ("Casual", "casual clothing"),
            "formal": ("Formal", "formal attire"),
            "bridal": ("Bridal", "bridal attire"),
            "sherwani": ("Sherwani", "an elegant sherwani"),
            "saree": ("Saree", "an elegant saree"),
            "kurta": ("Kurta", "a traditional kurta"),
            "suit": ("Suit", "a tailored suit"),
            "gown": ("Gown", "an elegant gown"),
            "ethnic": ("Ethnic", "ethnic wear"),
        },
    },
    "lighting": {
        "label": "Lighting", "emoji": "💡",
        "tags": {
            "natural_light": ("Natural Light", "natural lighting"),
            "soft_light": ("Soft Light", "soft diffused lighting"),
            "golden_light": ("Golden Light", "warm golden lighting"),
            "cinematic": ("Cinematic", "cinematic lighting"),
            "studio_lighting": ("Studio Lighting", "studio lighting"),
            "candlelight": ("Candlelight", "warm candlelight"),
            "fairy_lights": ("Fairy Lights", "glowing fairy lights"),
            "backlit": ("Backlit", "backlit silhouette lighting"),
        },
    },
    "camera_style": {
        "label": "Camera Style", "emoji": "📷",
        "tags": {
            "dslr": ("DSLR", "captured with a professional DSLR"),
            "portrait": ("Portrait", "portrait framing"),
            "wide_angle": ("Wide Angle", "wide-angle framing"),
            "close_up": ("Close-Up", "close-up framing"),
            "full_body": ("Full Body", "full body framing"),
            "aerial_view": ("Aerial View", "aerial view framing"),
            "cinematic_shot": ("Cinematic Shot", "cinematic shot composition"),
            "shallow_dof": ("Shallow Depth of Field", "shallow depth of field"),
        },
    },
    "style": {
        "label": "Style", "emoji": "🎭",
        "tags": {
            "realistic": ("Realistic", "photorealistic style"),
            "luxury": ("Luxury", "luxurious style"),
            "vintage": ("Vintage", "vintage style"),
            "royal": ("Royal", "royal aesthetic"),
            "modern": ("Modern", "modern style"),
            "elegant": ("Elegant", "elegant style"),
            "minimalist": ("Minimalist", "minimalist style"),
            "traditional": ("Traditional", "traditional style"),
            "glamorous": ("Glamorous", "glamorous style"),
            "dreamy": ("Dreamy", "dreamy atmosphere"),
        },
    },
    "decoration": {
        "label": "Decorations", "emoji": "🌸",
        "tags": {
            "flowers": ("Flowers", "flowers"),
            "balloons": ("Balloons", "balloons"),
            "fairy_lights": ("Fairy Lights", "fairy lights"),
            "candles": ("Candles", "candles"),
            "confetti": ("Confetti", "confetti"),
            "rangoli": ("Rangoli", "a rangoli design"),
            "garlands": ("Garlands", "flower garlands"),
            "lanterns": ("Lanterns", "lanterns"),
        },
    },
    "motion": {
        "label": "Motion (Video-Friendly)", "emoji": "🎬",
        "tags": {
            "dancing": ("Dancing", "dancing energetically"),
            "twirling": ("Twirling", "twirling gracefully"),
            "walking": ("Walking", "walking gracefully"),
            "running": ("Running", "running joyfully"),
            "hugging": ("Hugging", "embracing warmly"),
            "spinning": ("Spinning", "spinning playfully"),
            "flying_veil": ("Flying Veil", "veil flowing in the wind"),
            "hair_flowing": ("Hair Flowing", "hair flowing in the breeze"),
            "slow_motion": ("Slow Motion", "in slow motion"),
            "fireworks": ("Fireworks", "with fireworks bursting in the background"),
        },
    },
    "effects": {
        "label": "Effects", "emoji": "✨",
        "tags": {
            "bokeh": ("Bokeh", "soft bokeh background"),
            "sun_rays": ("Sun Rays", "sun rays streaming"),
            "lens_flare": ("Lens Flare", "cinematic lens flare"),
            "floating_petals": ("Floating Petals", "floating flower petals"),
            "sparkles": ("Sparkles", "magical sparkles"),
            "butterflies": ("Butterflies", "butterflies fluttering"),
            "snowflakes": ("Snowflakes", "gentle snowflakes falling"),
            "falling_leaves": ("Falling Leaves", "falling autumn leaves"),
            "floating_lights": ("Floating Lights", "floating lights"),
            "magical_glow": ("Magical Glow", "a magical glow"),
        },
    },
    "orientation": {
        "label": "Orientation", "emoji": "📐",
        # aspect_ratio hint instead of a prompt fragment — frontend uses this
        # to auto-set the aspect ratio picker when this chip is tapped.
        "tags": {
            "portrait": ("Portrait", "9:16"),
            "landscape": ("Landscape", "16:9"),
            "square": ("Square", "1:1"),
            "vertical_reel": ("Vertical Reel", "9:16"),
            "wide_screen": ("Wide Screen", "16:9"),
        },
    },
}


def get_keyword_groups_response() -> list[dict]:
    """Serialise KEYWORD_GROUPS for the GET /prompts/keyword-groups endpoint."""
    groups = []
    for group_key, group in KEYWORD_GROUPS.items():
        groups.append({
            "key": group_key,
            "label": group["label"],
            "emoji": group["emoji"],
            "tags": [
                {"key": f"{group_key}:{tag_key}", "label": label}
                for tag_key, (label, _fragment) in group["tags"].items()
            ],
        })
    return groups


def build_prompt_fragment(selected: list[str]) -> str:
    """
    Convert selected "group:tag" strings into one descriptive sentence
    fragment. Unknown/malformed entries are silently skipped. The
    "orientation" group is excluded — it's an aspect-ratio hint for the
    frontend, not prompt text.
    """
    fragments: list[str] = []
    for entry in selected:
        if ":" not in entry:
            continue
        group_key, tag_key = entry.split(":", 1)
        if group_key == "orientation":
            continue
        group = KEYWORD_GROUPS.get(group_key)
        if not group:
            continue
        tag = group["tags"].get(tag_key)
        if not tag:
            continue
        fragments.append(tag[1])

    return ", ".join(fragments)
