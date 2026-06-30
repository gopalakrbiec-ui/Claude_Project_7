"""
Reset templates and generate high-quality AI reference images for each.

Steps:
  1. Delete all existing templates (cascade-deletes nothing — orders keep their template_id FK)
  2. Insert new templates defined in TEMPLATES below
  3. For each template, generate a 768x1344 face-free reference image via fal-ai/flux/dev
  4. Upload to R2 public bucket and update templates.image_url

Run from Railway API console:
    python scripts/seed_and_generate.py

Required env vars:
    DATABASE_URL, FAL_API_KEY, S3_ENDPOINT_URL, S3_ACCESS_KEY_ID,
    S3_SECRET_ACCESS_KEY, S3_BUCKET_NAME, R2_PUBLIC_BASE
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = (
    os.environ["DATABASE_URL"]
    .replace("postgresql://", "postgresql+asyncpg://", 1)
    .replace("postgres://", "postgresql+asyncpg://", 1)
)

FAL_API_KEY = os.environ.get("FAL_API_KEY") or os.environ.get("GEN_PROVIDER_API_KEY", "")
os.environ["FAL_KEY"] = FAL_API_KEY

S3_ENDPOINT   = os.environ.get("S3_ENDPOINT_URL", "")
S3_KEY        = os.environ.get("S3_ACCESS_KEY_ID", "")
S3_SECRET     = os.environ.get("S3_SECRET_ACCESS_KEY", "")
S3_BUCKET     = os.environ.get("S3_BUCKET_NAME", "weddingapp")
S3_REGION     = os.environ.get("S3_REGION", "auto")
R2_PUBLIC_BASE = os.environ.get("R2_PUBLIC_BASE", "https://pub-a4fafa5a7fa94188b454190c60de3862.r2.dev").rstrip("/")

# ---------------------------------------------------------------------------
# TEMPLATES — fill this in with the 5 themes x 5 variants each
# Each entry: name, category, theme, scene_description, image_prompt
#   image_prompt  — detailed face-free prompt for fal-ai/flux/dev
#   scene_description — fed to Claude when building the generation prompt
# ---------------------------------------------------------------------------

TEMPLATES: list[dict] = [

    # ── 1. SUITS AND COATS FOR MEN ──────────────────────────────────────────
    {
        "name": "Charcoal Hotel Lobby",
        "category": "suits",
        "theme": "suits_coats",
        "is_featured": True,
        "base_price_paise": 900,
        "scene_description": "Editorial men's fashion: charcoal grey three-piece wool suit, marble-floored hotel lobby, soft window light, GQ magazine style, shallow depth of field.",
        "image_prompt": "Editorial portrait of a man in a charcoal grey three-piece wool suit, standing in a marble-floored hotel lobby, soft window light from the left, shallow depth of field, shot on 85mm lens, muted color grade, GQ magazine style, no face visible, clothing and environment focus, luxury fashion photography.",
    },
    {
        "name": "Camel Overcoat City Street",
        "category": "suits",
        "theme": "suits_coats",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Full-body men's fashion: camel wool overcoat over navy suit, rain-slicked city street at dusk, neon reflections on pavement, cinematic film grain.",
        "image_prompt": "Full-body shot of a man wearing a long camel-colored wool overcoat over a navy suit, walking on a rain-slicked city street at dusk, neon reflections on wet pavement, cinematic film grain, 35mm lens, moody urban atmosphere, no face visible, fashion editorial style.",
    },
    {
        "name": "Black Tuxedo Detail",
        "category": "suits",
        "theme": "suits_coats",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Close-up of a tailored black tuxedo jacket lapel with boutonnière, dramatic side lighting, dark studio background, luxury fashion catalog aesthetic.",
        "image_prompt": "Close-up detail shot of a tailored black tuxedo jacket lapel with a white boutonnière, dramatic side lighting, dark studio background, macro lens texture on fine fabric weave, luxury fashion catalog aesthetic, no face, product photography.",
    },
    {
        "name": "Pinstripe Gentleman's Study",
        "category": "suits",
        "theme": "suits_coats",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Man in double-breasted pinstripe suit, leather armchair, wood-paneled study, warm amber lighting, vintage gentleman's club atmosphere, medium format film look.",
        "image_prompt": "Man in a double-breasted pinstripe suit seated in a leather armchair in a wood-paneled study, warm amber lighting, vintage gentleman's club atmosphere, shot on medium format film look, cigar smoke haze, no face visible, fashion lifestyle photography.",
    },
    {
        "name": "Houndstooth Autumn Park",
        "category": "suits",
        "theme": "suits_coats",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Outdoor autumn portrait: houndstooth tweed coat and scarf, falling leaves in a park, golden hour backlight, bokeh background, lifestyle fashion photography.",
        "image_prompt": "Outdoor autumn portrait of a man in a houndstooth tweed coat and tartan scarf, standing among falling leaves in a park, golden hour backlight, beautiful bokeh background, lifestyle fashion photography, no face visible, warm autumnal color palette.",
    },

    # ── 2. RETRO BOLLYWOOD PORTRAITS ─────────────────────────────────────────
    {
        "name": "1970s Bollywood Actress",
        "category": "bollywood",
        "theme": "retro_bollywood",
        "is_featured": True,
        "base_price_paise": 900,
        "scene_description": "1970s Bollywood actress portrait: big curled hairstyle, bold winged eyeliner, vintage silk saree, painted cloud backdrop, warm sepia film grain, classic glamour lighting.",
        "image_prompt": "1970s Bollywood actress portrait backdrop, big curled hairstyle wig on stand, bold winged eyeliner makeup kit, vintage silk saree draped on mannequin, soft studio backdrop with hand-painted clouds, warm sepia-toned film grain, classic glamour three-point lighting setup, no person, vintage Indian film studio aesthetic.",
    },
    {
        "name": "Retro Bollywood Hero",
        "category": "bollywood",
        "theme": "retro_bollywood",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Retro Bollywood hero: slicked-back hair, aviator sunglasses, open-collar polyester shirt, vintage Ambassador car, sunset backlight, 1980s film poster color grading.",
        "image_prompt": "Retro Bollywood hero portrait backdrop, vintage white Ambassador car against a vivid orange sunset, aviator sunglasses on the dashboard, open-collar polyester shirt on a hanger, dramatic sunset backlight, 1980s Indian film poster color grading, no person, vintage Bollywood aesthetic.",
    },
    {
        "name": "Bollywood Dance Poster",
        "category": "bollywood",
        "theme": "retro_bollywood",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Vintage Bollywood dance: flared embroidered outfit, colorful hand-painted cinema banner background, dramatic spotlighting, grainy 35mm film texture.",
        "image_prompt": "Colorful hand-painted Indian cinema banner background with ornate floral motifs, dramatic stage spotlighting, rich jewel-toned colors, flared embroidered costume fabric swirling, grainy 35mm film texture overlay, no person, vintage Bollywood dance poster aesthetic.",
    },
    {
        "name": "Classic Bollywood Romance",
        "category": "bollywood",
        "theme": "retro_bollywood",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Old-school Bollywood romance: flowering tree, soft-focus lens, pastel color palette, hand-tinted photograph style, 1960s film still aesthetic.",
        "image_prompt": "Lush flowering tree with pink and white blossoms, soft-focus dreamy lens, pastel color palette, hand-tinted vintage photograph style, 1960s Indian film still aesthetic, garden setting with soft diffused light, romantic atmosphere, no people.",
    },
    {
        "name": "Bollywood Villain Noir",
        "category": "bollywood",
        "theme": "retro_bollywood",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Classic Bollywood villain: dark tinted glasses, velvet blazer, smoky studio lighting, dramatic shadows, theatrical noir styling, 1970s thriller poster.",
        "image_prompt": "Dark smoky studio lighting with dramatic theatrical shadows, velvet blazer draped on a chair, pair of dark tinted glasses on a table, noir-style light rays through venetian blinds, 1970s Indian thriller poster color palette, moody cinematic composition, no person.",
    },

    # ── 3. CRICKET GLORY POSTERS ─────────────────────────────────────────────
    {
        "name": "Cover Drive Action",
        "category": "cricket",
        "theme": "cricket_glory",
        "is_featured": True,
        "base_price_paise": 900,
        "scene_description": "Dynamic cricket batsman mid-cover-drive, stadium floodlights, motion blur on ball, dust from pitch, dramatic low-angle hero composition, sports poster lighting.",
        "image_prompt": "Dynamic cricket action backdrop, stadium floodlights blazing, cricket ball with motion blur trails, dust kicked up from the pitch, dramatic low-angle hero composition, cricket bat and stumps visible, no person, high-energy sports poster lighting, cinematic sports advertisement style.",
    },
    {
        "name": "Bowler's Delivery Stride",
        "category": "cricket",
        "theme": "cricket_glory",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Cricket bowler at delivery stride peak, intense stadium atmosphere, golden hour rim light, high-contrast sports photography, blurred crowd background.",
        "image_prompt": "Cricket pitch at golden hour, cricket ball mid-air, stadium crowd blurred in background, golden rim light from the side, high-contrast sports photography style, cricket stumps and crease markings visible, intense atmosphere, no person, sports poster composition.",
    },
    {
        "name": "Flying Catch",
        "category": "cricket",
        "theme": "cricket_glory",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Fielder diving for a catch, freeze-frame action, grass and dirt spray mid-air, dramatic stadium lighting, cinematic sports advertisement.",
        "image_prompt": "Cricket outfield at stadium, cricket ball suspended in air, grass and dirt particles frozen mid-spray, dramatic stadium floodlight beams, wide green pitch, freeze-frame action composition, no person, cinematic sports advertisement lighting, high-speed photography aesthetic.",
    },
    {
        "name": "Victory Celebration",
        "category": "cricket",
        "theme": "cricket_glory",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Cricket team victory celebration, confetti in the air, stadium lights with lens flare, wide-angle triumphant composition.",
        "image_prompt": "Cricket stadium victory scene, golden confetti raining down, stadium floodlights creating dramatic lens flares, Indian flag waving in stands, empty victory podium, trophy gleaming, wide-angle triumphant composition, no people, celebratory atmosphere.",
    },
    {
        "name": "Bat and Ball Impact",
        "category": "cricket",
        "theme": "cricket_glory",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Extreme close-up: cricket bat striking ball, dust and light sparks, dark dramatic background, bold typography-ready poster layout.",
        "image_prompt": "Extreme close-up of a cricket bat striking a red leather ball, dust particles and light sparks exploding from the impact point, dark dramatic stadium background, bold typographic poster layout composition, no person, product photography meets sports art.",
    },

    # ── 4. ROYAL INDIAN GRANDEUR ─────────────────────────────────────────────
    {
        "name": "Maharaja on Gold Throne",
        "category": "royal",
        "theme": "royal_grandeur",
        "is_featured": True,
        "base_price_paise": 900,
        "scene_description": "Maharaja on ornate gold throne, marble palace hall, jewelled turban, silk brocade robes, golden light through arched windows, hyper-detailed regal textures.",
        "image_prompt": "Ornate gold throne in a marble palace hall, jewelled turban resting on the throne, silk brocade robes draped over the armrest, soft golden light streaming through grand arched Mughal windows, hyper-detailed regal textures, no person, royal Indian palace interior, opulent and majestic.",
    },
    {
        "name": "Sheesh Mahal Queen",
        "category": "royal",
        "theme": "royal_grandeur",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Royal Indian queen setting: red and gold lehenga, elaborate jewelry, Sheesh Mahal mirrored hall, candle-lit ambiance, opulent symmetrical composition.",
        "image_prompt": "Sheesh Mahal mirrored palace hall with thousands of mirror fragments reflecting candlelight, red and gold embroidered lehenga draped on a mannequin stand, elaborate gold and ruby jewelry on a velvet cushion, symmetrical opulent composition, warm candle-lit ambiance, no person, royal Indian grandeur.",
    },
    {
        "name": "Royal Elephant Procession",
        "category": "royal",
        "theme": "royal_grandeur",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Grand palace courtyard, elephants in ceremonial gold drapery, royal procession, sandstone architecture, warm dusty sunlight, epic wide-angle cinematic shot.",
        "image_prompt": "Grand Rajasthani palace courtyard, two elephants adorned in ceremonial red and gold velvet drapery with golden headpieces, sandstone architecture with intricate carvings, warm dusty golden sunlight, epic wide-angle cinematic composition, no people riding, majestic and ceremonial atmosphere.",
    },
    {
        "name": "Royal Jewelled Sword",
        "category": "royal",
        "theme": "royal_grandeur",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Close-up royal hands: traditional gold jewelry, ornate jewel-encrusted sword, dark velvet background, dramatic chiaroscuro lighting.",
        "image_prompt": "Close-up of ornate jewel-encrusted ceremonial sword on dark velvet background, surrounded by traditional Indian gold jewelry — necklace, bangles, rings, dramatic chiaroscuro lighting with deep shadows, hyper-detailed macro photography, no person, luxury heritage product photography.",
    },
    {
        "name": "Royal Wedding Mandap",
        "category": "royal",
        "theme": "royal_grandeur",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Royal Indian wedding mandap: intricately carved sandstone pillars, marigold and rose petal decorations, soft diffused daylight, rich saturated color palette.",
        "image_prompt": "Royal Indian wedding mandap with intricately carved sandstone pillars, cascading marigold and rose petal garlands, sacred fire pit in the center, soft diffused daylight filtering through floral canopy, rich saturated color palette, no people, opulent traditional wedding setting.",
    },

    # ── 5. BEYOND LOVE ───────────────────────────────────────────────────────
    {
        "name": "Cosmic Silhouette",
        "category": "romance",
        "theme": "beyond_love",
        "is_featured": True,
        "base_price_paise": 900,
        "scene_description": "Surreal: two silhouettes reaching across a starlit cosmic void, glowing nebula colors, dreamlike soft focus, ethereal romantic atmosphere.",
        "image_prompt": "Surreal cosmic backdrop with a starlit void, glowing nebula in blues, purples and golds, two abstract silhouette outlines reaching toward each other without touching, dreamlike soft focus, ethereal romantic atmosphere, fine-art conceptual photography, no real faces.",
    },
    {
        "name": "Wings of Light",
        "category": "romance",
        "theme": "beyond_love",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Couple silhouettes with translucent light wings, soft pastel gradient sky, conceptual fine-art photography style.",
        "image_prompt": "Two abstract silhouettes back-to-back with translucent ethereal wings of light extending from their shoulders, soft pastel gradient sky in rose and lavender, conceptual fine-art photography style, glowing luminous wing textures, no real faces, romantic dreamlike atmosphere.",
    },
    {
        "name": "Double Exposure Bloom",
        "category": "romance",
        "theme": "beyond_love",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Double exposure: silhouette blended with a blooming flower field, golden hour light, soft bokeh, emotional and poetic visual storytelling.",
        "image_prompt": "Double exposure fine-art: two merged silhouettes blended seamlessly with a blooming wildflower field in golden hour light, soft bokeh flowers in foreground, warm amber and pink color palette, emotional and poetic visual storytelling, no real faces, conceptual photography.",
    },
    {
        "name": "Love Light Trails",
        "category": "romance",
        "theme": "beyond_love",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Abstract love: intertwining light trails forming a heart in darkness, long-exposure style, deep blue and warm orange contrast.",
        "image_prompt": "Abstract long-exposure light painting: intertwining glowing light trails forming a heart shape in deep darkness, deep cobalt blue and warm orange contrasting light streams, bokeh light particles, romantic conceptual fine-art, no people, long-exposure photography aesthetic.",
    },
    {
        "name": "Above the Clouds Bridge",
        "category": "romance",
        "theme": "beyond_love",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Couple silhouettes on a misty bridge above the clouds, soft diffused light, minimalist dreamlike composition, surreal romance.",
        "image_prompt": "Minimalist misty bridge suspended above a sea of clouds at sunrise, soft diffused ethereal light, dreamlike surreal composition, two tiny abstract silhouettes holding hands in the distance, pastel sky reflections on the cloud surface, conceptual surreal romance fine-art photography, no real faces.",
    },

    # ── 6. ROYAL BIKES ───────────────────────────────────────────────────────
    {
        "name": "Palace Gate Heritage Ride",
        "category": "bikes",
        "theme": "royal_bikes",
        "is_featured": True,
        "base_price_paise": 900,
        "scene_description": "Vintage Royal Enfield motorcycle at grand palace gate, regal embroidered jacket, golden hour lighting, heritage advertisement composition.",
        "image_prompt": "Vintage Royal Enfield bullet motorcycle parked in front of ornate Rajasthani palace gates, embroidered royal jacket draped over the seat, golden hour warm light, heritage motorcycle advertisement composition, no rider, rich textures of chrome and brass, majestic atmosphere.",
    },
    {
        "name": "Chrome Engine Macro",
        "category": "bikes",
        "theme": "royal_bikes",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Close-up chrome and brass motorcycle engine with royal engravings, dramatic studio lighting, macro lens texture, luxury product photography.",
        "image_prompt": "Extreme close-up macro of a chrome and brass vintage motorcycle engine with intricate royal Indian filigree engravings, dramatic studio lighting with rim highlights, rich metallic textures, luxury product photography style, no person, heritage craftsmanship aesthetic.",
    },
    {
        "name": "Gold Filigree Custom Bike",
        "category": "bikes",
        "theme": "royal_bikes",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Royal-themed custom motorcycle with gold filigree detailing, red carpet leading to palace courtyard, dramatic symmetrical composition, cinematic lighting.",
        "image_prompt": "Royal-themed custom motorcycle with intricate gold filigree detailing on tank and fenders, parked on a red velvet carpet leading toward a grand palace courtyard, dramatic symmetrical architectural composition, cinematic lighting with lens flare, no rider, opulent heritage aesthetic.",
    },
    {
        "name": "Desert Fort Road Rider",
        "category": "bikes",
        "theme": "royal_bikes",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Rider in royal Rajasthani attire on vintage motorcycle through desert fort road, dust trail, warm sunset tones, epic adventure poster style.",
        "image_prompt": "Vintage motorcycle on a dusty desert road leading to a sandstone Rajasthani fort, dust trail rising behind, warm orange-gold sunset tones, epic adventure travel poster composition, no rider visible, just the motorcycle and the dramatic landscape, cinematic wide shot.",
    },
    {
        "name": "Palace Night Silhouette",
        "category": "bikes",
        "theme": "royal_bikes",
        "is_featured": False,
        "base_price_paise": 900,
        "scene_description": "Motorcycle silhouette against illuminated palace at night, string lights, warm glow, reflective wet ground, moody cinematic atmosphere.",
        "image_prompt": "Vintage motorcycle silhouette parked against a grandly illuminated Indian palace at night, warm string lights and palace lighting reflecting on wet cobblestone ground, moody cinematic atmosphere, deep blue night sky, no rider, romantic heritage night photography.",
    },
]

# ---------------------------------------------------------------------------
# Generation helpers
# ---------------------------------------------------------------------------

def _s3_client():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_KEY,
        aws_secret_access_key=S3_SECRET,
        region_name=S3_REGION,
    )


async def _generate_image(prompt: str) -> bytes:
    import fal_client
    import httpx

    logger.info("  Generating image...")
    result = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: fal_client.run(
            "fal-ai/flux/dev",
            arguments={
                "prompt": prompt,
                "image_size": {"width": 768, "height": 1344},
                "num_inference_steps": 35,
                "guidance_scale": 3.5,
                "num_images": 1,
                "enable_safety_checker": False,
            },
        ),
    )
    images = result.get("images") or []
    if not images:
        raise RuntimeError(f"No images in fal result: {result}")

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(images[0]["url"])
        resp.raise_for_status()
        return resp.content


def _upload(s3, image_bytes: bytes, key: str) -> str:
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=image_bytes,
        ContentType="image/png",
        ACL="public-read",
    )
    return f"{R2_PUBLIC_BASE}/{key}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    if not TEMPLATES:
        logger.error("TEMPLATES list is empty — add your 5 themes first")
        return

    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    s3 = _s3_client()

    async with factory() as session:
        # Step 1: Remove all existing data (order matters — FK constraints)
        logger.info("Clearing orders, jobs, and templates...")
        await session.execute(text("DELETE FROM generation_jobs"))
        await session.execute(text("DELETE FROM credit_ledger WHERE ref_type = 'order'"))
        await session.execute(text("DELETE FROM orders"))
        await session.execute(text("DELETE FROM templates"))
        await session.execute(text("ALTER SEQUENCE templates_id_seq RESTART WITH 1"))
        await session.commit()
        logger.info("All cleared.")

        # Step 2: Insert new templates + generate images
        for i, tmpl in enumerate(TEMPLATES, start=1):
            name            = tmpl["name"]
            category        = tmpl["category"]
            theme           = tmpl.get("theme", category)
            scene_desc      = tmpl["scene_description"]
            image_prompt    = tmpl["image_prompt"]
            base_price      = tmpl.get("base_price_paise", 2500)
            is_featured     = tmpl.get("is_featured", False)

            logger.info("[%d/%d] %s — %s", i, len(TEMPLATES), category, name)

            try:
                image_bytes = await _generate_image(image_prompt)
            except Exception as exc:
                logger.error("  Image generation failed: %s", exc)
                image_bytes = None

            image_url = ""
            if image_bytes:
                key = f"template-images/{i}-{uuid.uuid4().hex[:8]}.png"
                image_url = _upload(s3, image_bytes, key)
                logger.info("  Uploaded: %s", image_url)
            else:
                logger.warning("  No image — template will show placeholder")

            await session.execute(text("""
                INSERT INTO templates
                    (name, category, theme, scene_description, image_url,
                     base_price_paise, is_featured, active, asset_keys)
                VALUES
                    (:name, :category, :theme, :scene_description, :image_url,
                     :base_price_paise, :is_featured, true, '{}')
            """), {
                "name": name,
                "category": category,
                "theme": theme,
                "scene_description": scene_desc,
                "image_url": image_url,
                "base_price_paise": base_price,
                "is_featured": is_featured,
            })
            await session.commit()
            logger.info("  Saved to DB.")

    await engine.dispose()
    logger.info("Done — %d templates created.", len(TEMPLATES))


if __name__ == "__main__":
    asyncio.run(main())
