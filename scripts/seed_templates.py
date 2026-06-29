"""
Seed initial templates into the database.
Run once after migrations:
    python scripts/seed_templates.py
"""
from __future__ import annotations

import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DATABASE_URL = (
    os.environ["DATABASE_URL"]
    .replace("postgresql://", "postgresql+asyncpg://", 1)
    .replace("postgres://", "postgresql+asyncpg://", 1)
)

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
# image_url      — the reference/style image shown in the Flutter grid
# scene_description — fed to Claude to craft the generation prompt
# category       — groups templates in the UI (wedding, birthday, fashion, etc.)
# theme          — sub-style within the category
# ---------------------------------------------------------------------------

TEMPLATES = [

    # ── Wedding ───────────────────────────────────────────────────────────────
    {
        "name": "Floral Mandap",
        "category": "wedding",
        "theme": "floral",
        "is_featured": True,
        "image_url": "https://images.unsplash.com/photo-1583089892943-e02e5b017b6a?w=600&q=85",
        "scene_description": (
            "Traditional Indian wedding mandap decorated with fresh marigold garlands, rose petals, "
            "and flickering oil diyas. Warm golden hour light. Ultra-realistic DSLR photography, "
            "8K resolution, shallow depth of field."
        ),
        "base_price_paise": 2900,
    },
    {
        "name": "Royal Sherwani",
        "category": "wedding",
        "theme": "royal",
        "is_featured": True,
        "image_url": "https://images.unsplash.com/photo-1594736797933-d0501ba2fe65?w=600&q=85",
        "scene_description": (
            "Groom in a rich red and gold embroidered sherwani standing in front of a Rajasthani palace "
            "archway. Regal, cinematic lighting. 8K DSLR portrait, bokeh background."
        ),
        "base_price_paise": 4900,
    },
    {
        "name": "Bridal Lehenga",
        "category": "wedding",
        "theme": "bridal",
        "image_url": "https://images.unsplash.com/photo-1511285560929-80b456fea0bc?w=600&q=85",
        "scene_description": (
            "Bride in an ornate red and gold bridal lehenga with heavy jewelry, standing in a lush "
            "flower-filled garden at golden hour. Professional wedding photography, 8K, cinematic."
        ),
        "base_price_paise": 4900,
    },
    {
        "name": "Garden Wedding",
        "category": "wedding",
        "theme": "garden",
        "image_url": "https://images.unsplash.com/photo-1464366400600-7168b8af9bc3?w=600&q=85",
        "scene_description": (
            "Couple standing under a floral arch surrounded by white roses and greenery. "
            "Soft natural light, elegant outdoor setting. 8K DSLR wedding portrait."
        ),
        "base_price_paise": 3500,
    },
    {
        "name": "Royal Couple Portrait",
        "category": "wedding",
        "theme": "royal-couple",
        "image_url": "https://images.unsplash.com/photo-1519225421980-715cb0215aed?w=600&q=85",
        "scene_description": (
            "Couple dressed in matching royal outfits — bride in lehenga, groom in sherwani — "
            "seated on an ornate gold throne. Palace backdrop, dramatic lighting. 8K cinematic."
        ),
        "base_price_paise": 5900,
    },

    # ── Birthday ──────────────────────────────────────────────────────────────
    {
        "name": "Grand Birthday",
        "category": "birthday",
        "theme": "grand",
        "is_featured": True,
        "image_url": "https://images.unsplash.com/photo-1464349095431-e9a21285b5f3?w=600&q=85",
        "scene_description": (
            "Person celebrating their birthday surrounded by golden balloons, confetti, and a "
            "tiered cake. Joyful party atmosphere. 8K DSLR portrait, warm celebratory lighting."
        ),
        "base_price_paise": 1500,
    },
    {
        "name": "Kids Birthday Party",
        "category": "birthday",
        "theme": "kids",
        "image_url": "https://images.unsplash.com/photo-1530103862676-de8c9debad1d?w=600&q=85",
        "scene_description": (
            "Child at a colorful birthday party with balloons, confetti, and a cartoon-themed cake. "
            "Bright cheerful setting. 8K professional photography."
        ),
        "base_price_paise": 1500,
    },
    {
        "name": "Milestone Birthday (50/60/75)",
        "category": "birthday",
        "theme": "milestone",
        "image_url": "https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=600&q=85",
        "scene_description": (
            "Elegant milestone birthday portrait with golden '50' or milestone number decor, "
            "warm candlelight, and flowers. Dignified, celebratory. 8K DSLR."
        ),
        "base_price_paise": 2500,
    },

    # ── Fashion & Portrait ────────────────────────────────────────────────────
    {
        "name": "Business Suit Portrait",
        "category": "fashion",
        "theme": "business",
        "image_url": "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=600&q=85",
        "scene_description": (
            "Professional portrait of a person in a sharp tailored business suit, standing in a "
            "modern glass office building. Confident posture, natural window light. 8K DSLR."
        ),
        "base_price_paise": 1900,
    },
    {
        "name": "Traditional Kurta Look",
        "category": "fashion",
        "theme": "traditional-men",
        "image_url": "https://images.unsplash.com/photo-1566492031773-4f4e44671857?w=600&q=85",
        "scene_description": (
            "Man in an elegant embroidered kurta-pajama standing against a haveli wall with "
            "floral decorations. Warm evening light. Ultra-realistic 8K DSLR portrait."
        ),
        "base_price_paise": 1900,
    },
    {
        "name": "Saree Elegance",
        "category": "fashion",
        "theme": "traditional-women",
        "image_url": "https://images.unsplash.com/photo-1583391733956-3750e0ff4e8b?w=600&q=85",
        "scene_description": (
            "Woman in a silk Kanjivaram saree with gold jewelry, standing in a sunlit temple "
            "courtyard with floral rangoli. Graceful, editorial 8K DSLR photography."
        ),
        "base_price_paise": 1900,
    },
    {
        "name": "Bollywood Glamour",
        "category": "fashion",
        "theme": "glamour",
        "is_featured": True,
        "image_url": "https://images.unsplash.com/photo-1596557406925-8a621e26f8e8?w=600&q=85",
        "scene_description": (
            "Glamorous Bollywood-style portrait with dramatic studio lighting, sparkly outfit, "
            "and magazine-quality makeup. Cinematic 8K high-fashion photography."
        ),
        "base_price_paise": 2900,
    },

    # ── Baby & Family ─────────────────────────────────────────────────────────
    {
        "name": "Baby Shower",
        "category": "family",
        "theme": "babyshower",
        "image_url": "https://images.unsplash.com/photo-1515488042361-ee00e0ddd4e4?w=600&q=85",
        "scene_description": (
            "Soft pastel baby shower setting with balloons, flowers, and a teddy bear. "
            "Joyful expecting-mother portrait. 8K natural light photography."
        ),
        "base_price_paise": 1500,
    },
    {
        "name": "Naming Ceremony",
        "category": "family",
        "theme": "naming-ceremony",
        "image_url": "https://images.unsplash.com/photo-1555252333-9f8e92e65df9?w=600&q=85",
        "scene_description": (
            "Namkaran ceremony setting with baby in traditional dress held by family, "
            "surrounded by marigold flowers and diyas. Warm, intimate 8K portrait."
        ),
        "base_price_paise": 1500,
    },
    {
        "name": "Family Portrait",
        "category": "family",
        "theme": "family",
        "image_url": "https://images.unsplash.com/photo-1609220136736-443140cffec6?w=600&q=85",
        "scene_description": (
            "Happy Indian family in matching ethnic outfits seated in a beautifully decorated "
            "living room. Warm golden light, joyful expressions. Professional 8K DSLR."
        ),
        "base_price_paise": 2500,
    },

    # ── Festival ──────────────────────────────────────────────────────────────
    {
        "name": "Diwali Celebration",
        "category": "festival",
        "theme": "diwali",
        "image_url": "https://images.unsplash.com/photo-1605600659873-d808a13e4d9a?w=600&q=85",
        "scene_description": (
            "Person in festive Indian ethnic wear surrounded by glowing diyas, sparklers, and "
            "rangoli at Diwali. Warm festive atmosphere. 8K DSLR photography."
        ),
        "base_price_paise": 1500,
    },
    {
        "name": "Housewarming (Griha Pravesh)",
        "category": "festival",
        "theme": "housewarming",
        "image_url": "https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=600&q=85",
        "scene_description": (
            "Family at a Griha Pravesh ceremony at the entrance of a new home decorated with "
            "marigold torans, diyas, and a rangoli. Joyful, auspicious setting. 8K portrait."
        ),
        "base_price_paise": 1500,
    },

    # ── Bollywood ─────────────────────────────────────────────────────────────
    {
        "name": "Bollywood Diva",
        "category": "bollywood",
        "theme": "diva",
        "is_featured": True,
        "image_url": "https://images.unsplash.com/photo-1610450949065-1f2841536c88?w=600&q=85",
        "scene_description": (
            "Glamorous Bollywood heroine pose — heavy dramatic makeup, sequined lehenga, "
            "standing in front of a grand film studio set with spotlights. "
            "Cinematic 8K photography, vibrant colours, movie-poster style."
        ),
        "base_price_paise": 3900,
    },
    {
        "name": "Retro Bollywood Portrait",
        "category": "bollywood",
        "theme": "retro",
        "is_featured": True,
        "image_url": "https://images.unsplash.com/photo-1583337130417-3346a1be7dee?w=600&q=85",
        "scene_description": (
            "1970s retro Bollywood portrait — film-grain texture, warm sepia tones, "
            "subject in vintage ethnic dress, classic film-set backdrop. "
            "Stylised as an old Filmfare magazine cover."
        ),
        "base_price_paise": 3500,
    },
    {
        "name": "Bollywood Hero",
        "category": "bollywood",
        "theme": "hero",
        "image_url": "https://images.unsplash.com/photo-1607346256330-dee7af15f7c5?w=600&q=85",
        "scene_description": (
            "Action-hero Bollywood pose — man in stylish sherwani or suit, dramatic back-lighting, "
            "slow-motion effect, grand palace staircase backdrop. "
            "Cinematic widescreen, 8K DSLR, movie-poster composition."
        ),
        "base_price_paise": 3500,
    },
    {
        "name": "Punjabi Bride",
        "category": "bollywood",
        "theme": "punjabi-bride",
        "image_url": "https://images.unsplash.com/photo-1578916171728-46686eac8d58?w=600&q=85",
        "scene_description": (
            "Punjabi bride in vibrant pink and red phulkari dupatta, heavy gold jewellery, "
            "kalire on wrists, standing in a mustard field at golden hour. "
            "8K DSLR wedding photography, Bollywood colour grade."
        ),
        "base_price_paise": 4500,
    },

    # ── Cricket ───────────────────────────────────────────────────────────────
    {
        "name": "Cricket Glory",
        "category": "cricket",
        "theme": "glory",
        "is_featured": True,
        "image_url": "https://images.unsplash.com/photo-1531415074968-036ba1b575da?w=600&q=85",
        "scene_description": (
            "Indian cricketer in full blue jersey holding a bat, celebrating a century, "
            "stadium crowd roaring in the background, confetti falling. "
            "Action 8K sports photography, dramatic stadium lighting."
        ),
        "base_price_paise": 2900,
    },
    {
        "name": "Stadium Champion",
        "category": "cricket",
        "theme": "champion",
        "image_url": "https://images.unsplash.com/photo-1624526267942-ab0ff8a3e972?w=600&q=85",
        "scene_description": (
            "Cricketer lifting the World Cup trophy in Team India jersey, "
            "fireworks exploding over the stadium, teammates celebrating behind. "
            "Epic 8K sports photography, golden hour, ultra-wide lens."
        ),
        "base_price_paise": 3500,
    },
    {
        "name": "Street Cricket Star",
        "category": "cricket",
        "theme": "street",
        "image_url": "https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=600&q=85",
        "scene_description": (
            "Young cricketer on a dusty street pitch, bright afternoon sun, "
            "gully cricket setting with makeshift wickets, joy and passion on face. "
            "Documentary-style 8K photography, warm Indian summer light."
        ),
        "base_price_paise": 1900,
    },

    # ── Royal India ───────────────────────────────────────────────────────────
    {
        "name": "Royal India Maharaja",
        "category": "royal",
        "theme": "maharaja",
        "is_featured": True,
        "image_url": "https://images.unsplash.com/photo-1599661046289-e31897846e41?w=600&q=85",
        "scene_description": (
            "Maharaja seated on an ornate gold and ruby throne in a Rajasthani palace — "
            "wearing a jewelled turban, silk achkan, and pearl necklace. "
            "Regal oil-painting lighting, 8K, Mughal-era grandeur."
        ),
        "base_price_paise": 5900,
    },
    {
        "name": "Royal Maharani",
        "category": "royal",
        "theme": "maharani",
        "image_url": "https://images.unsplash.com/photo-1583089892943-e02e5b017b6a?w=600&q=85",
        "scene_description": (
            "Maharani in a silk Banarasi saree with Kundan jewellery, standing in a palace "
            "zenana with intricate jali screens and marigold garlands. "
            "Royal portrait, 8K DSLR, Mughal miniature painting colour palette."
        ),
        "base_price_paise": 5900,
    },
    {
        "name": "Rajput Warrior",
        "category": "royal",
        "theme": "warrior",
        "image_url": "https://images.unsplash.com/photo-1594736797933-d0501ba2fe65?w=600&q=85",
        "scene_description": (
            "Rajput warrior in full armour with a sword, standing on a fort rampart at dusk, "
            "Aravalli hills in the background. "
            "Epic cinematic 8K photography, dramatic golden-red sky."
        ),
        "base_price_paise": 4900,
    },

    # ── Professional / LinkedIn ───────────────────────────────────────────────
    {
        "name": "LinkedIn Pro",
        "category": "professional",
        "theme": "linkedin",
        "is_featured": True,
        "image_url": "https://images.unsplash.com/photo-1560250097-0b93528c311a?w=600&q=85",
        "scene_description": (
            "Confident professional in business formal attire — crisp white shirt or saree blouse — "
            "neutral studio background with soft bokeh. "
            "Corporate headshot, 8K DSLR, LinkedIn profile photo style."
        ),
        "base_price_paise": 1500,
    },
    {
        "name": "Startup Founder",
        "category": "professional",
        "theme": "startup",
        "image_url": "https://images.unsplash.com/photo-1551434678-e076c223a692?w=600&q=85",
        "scene_description": (
            "Ambitious startup founder in smart-casual (blazer over t-shirt) in a modern "
            "co-working space with laptops and whiteboards. "
            "Candid 8K corporate photography, entrepreneurial energy."
        ),
        "base_price_paise": 1500,
    },
    {
        "name": "Campus Yearbook",
        "category": "professional",
        "theme": "yearbook",
        "image_url": "https://images.unsplash.com/photo-1523050854058-8df90110c9f1?w=600&q=85",
        "scene_description": (
            "College student in graduation gown holding diploma, smiling proudly on campus. "
            "Soft natural light, campus greenery in background. "
            "Classic 8K yearbook portrait, warm celebratory atmosphere."
        ),
        "base_price_paise": 1500,
    },
]


async def seed() -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        await session.execute(text("TRUNCATE templates RESTART IDENTITY CASCADE"))
        for t in TEMPLATES:
            row = {**t, "is_featured": t.get("is_featured", False)}
            await session.execute(
                text(
                    "INSERT INTO templates "
                    "(name, category, theme, image_url, scene_description, asset_keys, base_price_paise, active, language, is_featured) "
                    "VALUES (:name, :category, :theme, :image_url, :scene_description, '{}', :base_price_paise, true, 'en', :is_featured)"
                ),
                row,
            )
        await session.commit()
        print(f"Seeded {len(TEMPLATES)} templates.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
