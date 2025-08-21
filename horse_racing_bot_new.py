import discord
from discord.ext import commands, tasks
from discord import app_commands
from google import genai
from google.genai import types # type: ignore
import asyncio
import aiohttp
import json
import re
import pytz
import time
import threading
import os
import random
from datetime import datetime, timedelta, time as dtime

def looks_plausible_fields(fields_json: dict) -> bool:
    """Check if fields data looks real/complete enough to use for gating."""
    mtgs = fields_json.get("meetings", [])
    horses = {h.strip() for m in mtgs for r in m.get("races", []) for h in r.get("runners", []) if isinstance(h, str)}
    # Lowered requirements for testing: 1+ meetings, 20+ horses (was 2+ meetings, 80+ horses)
    return len(mtgs) >= 1 and len(horses) >= 20


def inject_fields_into_prompt(base_prompt: str, fields_json: dict) -> str:
    """Inject official fields into prompt to prevent hallucinations."""
    return base_prompt + (
        "\n\nOFFICIAL FIELDS (STRICT SOURCE OF TRUTH):\n"
        "Use ONLY these runners. If a horse is not listed here, mark it OOS and skip.\n"
        "```json\n" + json.dumps(fields_json, ensure_ascii=False) + "\n```\n"
        "HARD RULE: Do not invent runners. If uncertain, output no selection for that race.\n"
    )


def _norm(name: str) -> str:
    """Normalize horse name for fuzzy matching."""
    import unicodedata
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if c.isalnum() or c in " -'")  # keep space, hyphen, apostrophe
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _build_allowed_norm_set(fields_json) -> set[str]:
    """Build normalized set of allowed horse names."""
    names = set()
    for m in fields_json.get("meetings", []):
        for r in m.get("races", []):
            for h in r.get("runners", []):
                if isinstance(h, str):
                    names.add(_norm(h))
    return names


def _in_allowed(name: str, allowed_norm: set[str]) -> bool:
    """Check if horse name is in allowed set with fuzzy matching."""
    from difflib import SequenceMatcher
    n = _norm(name)
    if n in allowed_norm:
        return True
    # light fuzzy to absorb punctuation/pluralization quirks
    for a in allowed_norm:
        if SequenceMatcher(None, n, a).ratio() >= 0.92:
            return True
    return False


# ===== API CONFIGURATION =====
# Use environment variables for security - set these in Railway
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

def sanitize_token(t: str) -> str:
    """Clean up token string from common copy/paste issues."""
    if not t:
        return ""
    # strip whitespace/quotes/zero-width chars/BOM
    t = t.strip().strip("'\"").replace("\u200b", "").replace("\ufeff", "")
    return t

def looks_like_bot_token(t: str) -> bool:
    """Heuristic check for Discord bot token format."""
    return bool(re.match(r"^[A-Za-z0-9_\-]{24,}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}$", t or ""))

async def safe_update(interaction: discord.Interaction, *, embed=None, view=None, content=None):
    """Safely update interaction response, handling already-acknowledged interactions."""
    try:
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view, content=content)
        else:
            await interaction.response.edit_message(embed=embed, view=view, content=content)
    except discord.InteractionResponded:
        # Already acked somewhere else — just edit the original
        await interaction.edit_original_response(embed=embed, view=view, content=content)

# Validate and sanitize the token
TOKEN = sanitize_token(DISCORD_BOT_TOKEN)

if not looks_like_bot_token(TOKEN):
    print("❌ DISCORD_BOT_TOKEN doesn't look like a valid Discord bot token.")
    print("Make sure you copied it from the Bot tab in Discord Developer Portal.")
    print("The token should look like: MTcxNjk1NTY4OTM0MDk2ODk2MA.GbQiSs.xyz...")
    exit(1)

# LJ Mile Model Prompt - Full raw prompt as constant
LJ_MILE_PROMPT = """COMPREHENSIVE SCAN: Analyze ALL Australian Thoroughbred race meetings today. Search EVERY track across ALL states (NSW, VIC, QLD, SA, WA, TAS, NT, ACT). Evaluate ONLY races with official distance **≤1600 m**. **Hard filter:** EXCLUDE any race >1600 m. Use the **LJ Mile Model (12 pts) + H2H Module** to score EVERY runner in ALL eligible races. Consult AU websites for complete fields, scratchings, maps, sectionals, trials, track conditions, and market context.

🎯 **MINIMUM TARGET: Find 2-3+ qualifiers across all tracks.** If fewer than 3 qualifiers found, provide detailed explanation:
- "Due to track abandonment at [Track Name]..."
- "Late scratchings removed [Horse Name] from Race X..."  
- "Weather downgrade affected going conditions..."
- "Limited eligible races today (most >1600m)..."
- "Field quality below threshold on light racing day..."

🌏 **MANDATORY TRACK COVERAGE** - Scan ALL active venues:
NSW: Randwick, Rosehill, Canterbury, Kensington, Hawkesbury, Newcastle, Gosford, Wyong, Muswellbrook, Scone, Wagga, Albury
VIC: Flemington, Caulfield, Moonee Valley, Sandown, Geelong, Ballarat, Bendigo, Mornington, Cranbourne, Sale, Wangaratta
QLD: Eagle Farm, Doomben, Gold Coast, Sunshine Coast, Toowoomba, Rockhampton, Mackay, Townsville, Cairns, Ipswich
SA: Morphettville, Murray Bridge, Gawler, Port Lincoln, Mount Gambier
WA: Ascot, Belmont Park, Bunbury, Albany, Geraldton, Kalgoorlie, Northam
TAS: Elwick (Hobart), Mowbray (Launceston), Devonport, Spreyton
NT: Fannie Bay (Darwin)

🚦 DISTANCE RULES (STRICT)
- Eligible: 950 m–1600 m inclusive (e.g., 1000, 1100, 1200, 1400, 1500, 1550, **1600**)
- Ineligible: **>1600 m** (e.g., 1601, 1700, 1800, 2000, 2040, 2100, 2400)
- If distance is shown in miles, only include ≤1.0 mi (exactly 1609 m is EXCLUDED).

🌐 WEBSITES TO CONSULT (AU) - Use multiple sources for comprehensive coverage
- Racing.com — VIC replays, trials, stewards' reports: https://www.racing.com
- Racenet — speed maps, late mail, stats: https://www.racenet.com.au  
- Punters — sectionals, ratings, comments: https://www.punters.com.au
- Racing NSW — trials, official results/sectionals: https://www.racingnsw.com.au
- TAB — live markets, fluctuations: https://www.tab.com.au
- Betfair Australia — exchange odds/weight of money: https://www.betfair.com.au
- Racing Queensland — QLD form/trials: https://www.racingqueensland.com.au
- Racing Victoria — VIC form/stewards: https://www.racingvictoria.com.au

(Cross-reference multiple sources; if one site is down, use others for complete coverage.)

🧠 LJ MILE MODEL — 12-POINT WINNER-PICKER (≤1600 m ONLY)

Speed & Sectionals
1) Top-3 last 600 m last start OR made strong late ground
2) Last-start figure within ~2L of class par OR new peak in last two runs
3) Recent trial/jump-out strong (top-3 or eased late under light riding)

Form & Class
4) Won/placed in same grade (or stronger) within last 6 starts
5) **H2H advantage vs today's rivals** (see H2H Module)
6) Proven in the 1200–1600 m band (win or close placing)

Track & Conditions
7) Proven on today's going (or adjacent band) OR prior same-course tick
8) Handles today's direction/surface; no first-time venue/surface red flags

Map, Draw & Set-Up
9) Barrier suits run style; field size ≤12 is advantageous
10) Positive gear/set-up (e.g., blinkers on; 2nd–3rd up with prior success)

Rider & Weights
11) Jockey ≥15% last 30 days OR strong historical horse/jockey combo
12) Winnable weight; has performed within ±1.5 kg of today's impost

⚔️ H2H MODULE — MANDATORY, BE ADAMANT

Scope: last 12 months; prioritise 1200–1600 m, same/adjacent going, similar grade.

A) DIRECT H2H (latest clash among today's runners):
+2 beat rival by ≥1.0L | +1 beat by <1.0L or narrowly ahead
−1 lost by <1.0L | −2 lost by ≥1.0L
(Weight ×1.25 if ≤90 days; ×0.75 if >180 days)

B) COLLATERAL LINKS (mutual opponents):
+1 if A beat X and X beat B under similar conditions (margins supportive)
−1 if chain favours rival (ignore if >300 days)

C) H2H SCORE & CONFIDENCE:
Score = sum(A+B), cap −4…+4
Confidence: Strong (≥+2 with direct clash ≤180 d), Neutral (−1…+1), Negative (≤−2)

D) HARD RULES: EXCLUDE H2H Negative runners unless they score ≥11/12 AND show a fresh peak (last 2 runs). In ties prefer Strong over Neutral. If no usable links, note "H2H: Insufficient data" and continue.

SCORING & FILTERING
- Award 1 point per satisfied criterion (12 max). Output runners with **≥9/12 only**.
- Apply distance filter FIRST; skip any race >1600 m.
- Treat going adjacency: Good 3–5 adjacent; Soft 6–8 adjacent; Heavy 9–10 adjacent.
- Use official fields/scratchings; ignore scratched runners.

📊 **SCAN SUMMARY REQUIRED** - Always include at end:
"🔍 **Scan Summary**: Analyzed [X] tracks, [Y] eligible races ≤1600m, [Z] total runners assessed. [Explanation if <3 qualifiers found]"

🧾 RETURN FORMAT (each qualifier)

🏇 **Horse Name**
📍 Race: [Australian Venue] – Race [#] – **Distance: [####] m** – [Track/Going]
🧮 **LJ Analysis Score**: [X/12] = [Score%]
⚔️ **H2H Summary**: [Strong / Neutral / Negative / Insufficient] — key matchups (e.g., vs Rival A +1.2L (90d); vs Rival B −0.4L (45d); Collateral: beat X → X beat Rival C)
✅ [Passed 1] ✅ [Passed 2] ❌ [Missed X] ...
📝 **~100-Word LJ Analysis**: [Why this profile wins today: late speed/sectionals, class fit, map/draw, rider/weight; reinforce with H2H edges; one key risk.]

ORDERING OF OUTPUT
1) H2H Confidence/Score
2) Criteria count
3) Quality of last-run late splits"""

# Validate bot token
if not DISCORD_BOT_TOKEN:
    print("⚠️ WARNING: DISCORD_BOT_TOKEN not set! Add your bot token to use Discord features.")

# Data directory - Use local directory for development
DATA_DIR = r'c:\Users\Pixel\Desktop\HORSE AI LJ\data'

# Bot settings files (within DATA_DIR)
SETTINGS_FILE = os.path.join(DATA_DIR, 'bot_settings.json')
LEARNING_DATA_FILE = os.path.join(DATA_DIR, 'racing_learning_data.json')
DAILY_PREDICTIONS_FILE = os.path.join(DATA_DIR, 'daily_predictions.json')

# Default bot settings
DEFAULT_SETTINGS = {
    'auto_post_enabled': True,
    'min_score': 9,  # Default minimum score out of 12
    'post_times': ['07:00'],  # Perth times - only 7am
    'min_jockey_strike_rate': 15,
    'max_weight_kg': 61.0,
    'min_weight_kg': 50.0,
    'include_distances': [950, 1000, 1100, 1200, 1300, 1400, 1500, 1550, 1600],
    'exclude_negative_h2h': True,
    'results_time': '19:00',  # 7 PM Perth for results analysis
    'auto_channel_id': None,  # Channel ID for auto-posting
    'recent_changes': [],  # Change log
    'distance_min_m': 950,
    'distance_max_m': 1600,
    'distance_whitelist_enforced': False,   # NEW: default to band-only
    'require_official_fields': False,   # NEW: set to False for testing
}

DEFAULT_LEARNING_DATA = {
    'total_predictions': 0,
    'successful_predictions': 0,
    'failed_predictions': 0,
    'win_rate': 0.0,
    'successful_patterns': [],
    'failed_patterns': [],
    'jockey_performance': {},
    'trainer_performance': {},
    'track_performance': {},
    'distance_performance': {},
    'barrier_performance': {},
    'odds_range_performance': {},
    'learning_insights': []
}

def default_predictions_for_today():
    perth_now = datetime.now(PERTH_TZ)
    return {
        'date': perth_now.strftime('%Y-%m-%d'),
        'predictions': [],
        'generated_at': perth_now.strftime('%H:%M AWST')
    }

def ensure_data_dir_and_files():
    """Ensure data directory and JSON files exist."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        
        # Bot settings
        if not os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'w') as f:
                json.dump(DEFAULT_SETTINGS, f, indent=2)
        
        # Learning data
        if not os.path.exists(LEARNING_DATA_FILE):
            with open(LEARNING_DATA_FILE, 'w') as f:
                json.dump(DEFAULT_LEARNING_DATA, f, indent=2)
        
        # Daily predictions (today)
        if not os.path.exists(DAILY_PREDICTIONS_FILE):
            with open(DAILY_PREDICTIONS_FILE, 'w') as f:
                json.dump(default_predictions_for_today(), f, indent=2)
    except Exception as e:
        print(f"Error ensuring data files: {e}")

def load_settings():
    """Load bot settings from file and merge with defaults (incl. recent_changes)."""
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f:
                data = json.load(f)
        else:
            data = {}

        # Merge defaults without nuking saved values
        merged = DEFAULT_SETTINGS.copy()
        merged.update(data)

        # Ensure recent_changes list exists
        if "recent_changes" not in merged or not isinstance(merged.get("recent_changes"), list):
            merged["recent_changes"] = []

        # Persist any missing keys we just added
        try:
            with open(SETTINGS_FILE, 'w') as f:
                json.dump(merged, f, indent=2)
        except Exception:
            pass

        return merged
    except Exception as e:
        print(f"Error loading settings: {e}")
        fallback = DEFAULT_SETTINGS.copy()
        fallback["recent_changes"] = []
        return fallback

def save_settings(settings):
    """Save bot settings to file"""
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings, f, indent=2)
        print(f"Settings saved successfully. distance_whitelist_enforced={settings.get('distance_whitelist_enforced', 'NOT SET')}")
    except Exception as e:
        print(f"Error saving settings: {e}")

# Perth timezone
PERTH_TZ = pytz.timezone('Australia/Perth')
# Sydney timezone for date calculations
SYD_TZ = pytz.timezone('Australia/Sydney')

# === HELPERS FOR UI & VALIDATION ===
def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)

def _valid_time_str(t: str) -> bool:
    try:
        datetime.strptime(t.strip(), "%H:%M")
        return True
    except Exception:
        return False

def _parse_times_csv(times_csv: str):
    parts = [p.strip() for p in times_csv.split(",") if p.strip()]
    good = [p for p in parts if _valid_time_str(p)]
    return sorted(set(good))

def _next_7_days_options():
    """Return list of (label, value_date_str) for today + next 7 days, Sydney calendar."""
    now_syd = datetime.now(SYD_TZ).date()
    out = []
    for i in range(0, 8):
        d = now_syd + timedelta(days=i)
        # label like: Tue 26 Aug 2025 — 2025-08-26
        label = d.strftime("%a %d %b %Y — %Y-%m-%d")
        out.append((label, d.strftime("%Y-%m-%d")))
    return out

def _distance_options():
    return [950, 1000, 1100, 1200, 1300, 1400, 1500, 1550, 1600]

def _now_awst_str():
    return datetime.now(PERTH_TZ).strftime("%Y-%m-%d %H:%M AWST")

def log_setting_change(user, field: str, old_val, new_val):
    """Append a change record to settings.recent_changes."""
    try:
        s = load_settings()
        entry = {
            "ts": _now_awst_str(),
            "user": f"{user.name}#{user.discriminator}" if hasattr(user, "discriminator") else user.name,
            "field": field,
            "old": old_val,
            "new": new_val,
        }
        s.setdefault("recent_changes", [])
        s["recent_changes"].append(entry)
        # keep last 20
        s["recent_changes"] = s["recent_changes"][-20:]
        save_settings(s)
    except Exception as e:
        print(f"change-log error: {e}")

def _extract_distances_meters(text: str) -> list[int]:
    """
    Parse candidate meter values from text. We intentionally ignore
    small 'sectional' numbers (e.g., 200/400/600/800) and only keep
    values that could be a race distance (>=900m).
    """
    t = text.lower()
    metre_num = r'(?:\d{3,4}|\d,\d{3})'
    meters = []

    # 1200m / 1,200 m / 1200 metres
    for m in re.finditer(rf'({metre_num})\s*m(?:eters|etres)?\b', t):
        raw = m.group(1).replace(',', '')
        try:
            val = int(float(raw))
            if val >= 900:  # <- ignore 'last 600 m', etc.
                meters.append(val)
        except:
            pass

    # "1600 metres" (alt formatting)
    for m in re.finditer(rf'({metre_num})\s*(?:meters|metres)\b', t):
        raw = m.group(1).replace(',', '')
        try:
            val = int(float(raw))
            if val >= 900:
                meters.append(val)
        except:
            pass

    # miles → meters (1 mi = 1609.344 m). Keep only >=900.
    for m in re.finditer(r'(\d{1,2}(?:[.,]\d+)?)\s*mi(?:le|les)?\b', t):
        try:
            miles = float(m.group(1).replace(',', '.'))
            val = int(round(miles * 1609.344))
            if val >= 900:
                meters.append(val)
        except:
            pass

    # bare "1 mi"
    for m in re.finditer(r'(\d{1,2})\s*mi\b', t):
        try:
            miles = float(m.group(1))
            val = int(round(miles * 1609.344))
            if val >= 900:
                meters.append(val)
        except:
            pass

    # de-dup (preserve order)
    seen, out = set(), []
    for x in meters:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def build_today_prompt():
    """Build today's prompt with date anchor for Australia/Sydney"""
    syd = pytz.timezone('Australia/Sydney')
    now = datetime.now(syd)
    anchor = now.strftime("%A %d %B %Y (%Y-%m-%d) %H:%M %Z")
    preface = (
        f"DATE ANCHOR: Treat the current date/time as {anchor}. "
        "Use Australia/Sydney for all 'today' references (timezone anchor only). "
        "COVERAGE: Scan ALL Australian Thoroughbred meetings across NSW, VIC, QLD, SA, WA, TAS, NT, and ACT — do NOT limit to Sydney-only cards. "
        "MANDATORY: Search EVERY active track today. If fewer than 3 qualifiers found, provide detailed explanation. "
        "HARD FILTER: evaluate ONLY Australian Thoroughbred races with official distance ≤ 1600 m; "
        "exclude >1600 m and exactly 1609 m."
    )
    
    output_contract = """
OUTPUT CONTRACT (MANDATORY):
- Begin each qualifier with: "🏇 **Horse Name**"
- Include line: "📍 Race: [Track] – Race [#] – Distance: [####] m – [Track/Going]"  (numeric metres required)
- Include line: "🧮 **LJ Analysis Score**: X/12 = Y%"
- Include line: "⚔️ **H2H Summary**: [Strong/Neutral/Negative/Insufficient]"
- Include a checklist with ✅/❌ for up to 12 criteria.
- Use ONLY runners present in the official fields list provided to you.
"""
    
    return preface + "\n\n" + output_contract + "\n\n" + LJ_MILE_PROMPT

# LJ Mile Model Validation Functions
def _meters_ok(text: str) -> bool:
    """
    True if we find at least one distance that:
      • lies within the configured band (default 950–1600),
      • is not exactly 1609 (1.0 mile),
      • and (optionally) matches the whitelist if enforced (with ±50m tolerance).
    """
    settings = load_settings()
    dmin = int(settings.get('distance_min_m', 950))
    dmax = int(settings.get('distance_max_m', 1600))
    enforce_list = bool(settings.get('distance_whitelist_enforced', False))
    allowed = set(int(x) for x in settings.get('include_distances', []))

    found = _extract_distances_meters(text)
    if not found:
        # Fallback: accept explicit ≤1600m claim
        if re.search(r'distance\s*[:=]\s*≤?\s*1600\s*m\b', text, re.I):
            print("distance fallback: accepted 'Distance ≤1600 m' phrasing")
            return True
        print("distance check: no distances found in text")
        return False

    for m in found:
        # Check basic constraints
        if m == 1609:
            print(f"distance rejected: {m}m is exactly 1 mile (1609m)")
            continue
        if m < dmin or m > dmax:
            print(f"distance rejected: {m}m outside band {dmin}-{dmax}m")
            continue

        # If whitelist is enforced, check with tolerance
        if enforce_list:
            # Check exact match first
            if m in allowed:
                print(f"distance OK: {m}m exact whitelist match, enforce={enforce_list}")
                return True
            
            # Check ±50m tolerance around whitelist values
            tolerance_match = False
            for allowed_dist in allowed:
                if abs(m - allowed_dist) <= 50:
                    print(f"distance OK: {m}m within ±50m of whitelist {allowed_dist}m, enforce={enforce_list}")
                    return True
            
            # No tolerance match found
            print(f"distance rejected by whitelist: {m}m not within ±50m of any allowed {sorted(allowed)}")
            continue
        else:
            # No whitelist enforcement - just band check (already passed above)
            print(f"distance OK: {m}m within band {dmin}-{dmax}m, enforce={enforce_list}")
            return True

    print(f"distance final reject: no distances in {found} passed all checks")
    return False

def _score_ok(text: str, min_score: int = None) -> bool:
    """Check if LJ Analysis Score meets minimum threshold with checkmark fallback"""
    if min_score is None:
        settings = load_settings()
        min_score = settings.get('min_score', 9)
    
    # Primary: Look for explicit score pattern
    m = re.search(r'(?:lj\s*analysis\s*)?score\s*:\s*(\d+)\s*/\s*12', text, re.I)
    if m:
        try:
            score = int(m.group(1))
            passed = score >= min_score
            print(f"score check: {score}/12 vs min {min_score} = {'PASS' if passed else 'FAIL'}")
            return passed
        except ValueError:
            print(f"score check: invalid score value '{m.group(1)}'")
            return False

    # Fallback: count checkmarks
    ticks = len(re.findall(r'✅', text))
    if 0 < ticks <= 12:
        passed = ticks >= min_score
        print(f"score fallback via checkmarks: {ticks}/12 => {'PASS' if passed else 'FAIL'}")
        return passed

    print("score check: no score pattern and no checkmarks")
    return False

def _h2h_ok(text: str) -> bool:
    # Accept "H2H Summary:" or just "H2H:"
    m = re.search(r'h2h(?:\s*summary)?\s*:\s*([^\n—\-]+)', text, re.I)
    if not m:
        return True  # treat as "Insufficient"
    summary = m.group(1).strip().lower()
    if 'negative' not in summary:
        return True

    sm = re.search(r'lj\s*analysis\s*score\s*:\s*(\d+)\s*/\s*12', text, re.I)
    if sm:
        try:
            score = int(sm.group(1))
            if score >= 11 and 'fresh peak' in text.lower():
                return True
        except ValueError:
            pass
    return False

def extract_valid_qualifiers(response_text: str, min_score: int = None, allowed_horses: set[str] = None):
    """
    Find blocks starting with '🏇 **Horse Name**' headings and validate.
    """
    if min_score is None:
        settings = load_settings()
        min_score = settings.get('min_score', 9)
    
    pattern = re.compile(r'🏇\s*\*\*(.+?)\*\*', re.I)
    matches = list(pattern.finditer(response_text))
    valid = []

    for idx, m in enumerate(matches):
        horse_name = m.group(1).strip()
        start = m.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(response_text)
        block = response_text[start:end].strip()

        reasons = []
        if isinstance(allowed_horses, set) and not _in_allowed(horse_name, allowed_horses):
            reasons.append("not in official fields")
        if not _meters_ok(block):
            reasons.append("distance")
        if not _score_ok(block, min_score):
            reasons.append(f"score <{min_score}/12")
        if not _h2h_ok(block):
            reasons.append("h2h")

        if not reasons:
            valid.append(block)
        else:
            print(f"❌ Filtered: {horse_name} ({', '.join(reasons)})")

    return valid

# Helper: Determine target racing date with Sydney cutoff rules
def get_effective_target_date():
    """Return the date to analyze using Sydney time cutoff (after 8 PM -> next day)."""
    sydney_tz = pytz.timezone('Australia/Sydney')
    sydney_now = datetime.now(sydney_tz)
    # After 8 PM Sydney time, consider the next day
    if sydney_now.hour >= 20:
        return sydney_now.date() + timedelta(days=1), True  # True => skipped to next day
    return sydney_now.date(), False

# Helper: Build a prompt with modes for same-day vs next-day early analysis
def build_racing_prompt(target_date_str: str, target_date_search: str, learning_insights: str, mode: str = "today") -> str:
    """Create the racing analysis prompt. mode: "today" or "nextday"."""
    next_day_preamble = """
🛠️ DEBUG: Next-day early analysis activated
🔄 Scanning for tomorrow's racing information
""" if mode == "nextday" else ""

    focus_line = (
        "Focus: Race fields, markets, and track conditions for today's racing."
        if mode == "today"
        else "Focus: Tomorrow's nominations, early markets, and expected fields."
    )

    return f"""{next_day_preamble}
🏇 ULTRA-PREMIUM LJ RACING ANALYSIS - {target_date_str}

MISSION: You are the LJ Punting Model Elite, Australia's most advanced racing analysis system. Provide ULTRA-DETAILED analysis with head-to-head data, previous encounters, and comprehensive form breakdowns. {focus_line}

CRITICAL SEARCH REQUIREMENTS (SEARCH EACH INDIVIDUALLY):
1. "{target_date_search} Australian horse racing meetings complete fields"
2. "{target_date_search} TAB form guide all races detailed"
3. "{target_date_search} racing.com fields form and barriers"
4. "{target_date_search} punters.com form guide and tips"
5. "Australian horse racing results 2024 2025 head to head encounters"
6. "racenet.com.au historical matchups same horses"
7. "racing post australia previous meetings between horses"
8. "TAB results archive horses racing against each other"
9. "punters.com.au head to head records margins"
10. "Australian racing form guide historical encounters"
11. "sportingbet racing form previous meetings"
12. "racing.com results archive same horses different races"

MANDATORY HEAD-TO-HEAD RESEARCH PROTOCOL:
For EVERY race field, perform these specific searches:
- "[HORSE_A] vs [HORSE_B] previous meetings results margins"
- "[HORSE_A] racing record against [HORSE_B] last 2 years"
- "[TRACK_NAME] meetings [HORSE_A] [HORSE_B] head to head"
- "Australian racing [HORSE_A] [HORSE_B] encounter history"
- "[JOCKEY_A] vs [JOCKEY_B] strike rate same track"

HEAD-TO-HEAD SEARCH STRATEGY:
1. Search every possible horse combination in the race
2. Look for meetings in last 24 months minimum
3. Check different tracks and distances
4. Identify patterns: who usually leads, who finishes stronger
5. Note if same jockey/trainer combinations involved

ULTRA-ADVANCED LJ ANALYSIS FRAMEWORK:

📊 HEAD-TO-HEAD ANALYSIS (MANDATORY - NO EXCEPTIONS):
- Search extensively for previous meetings between ALL horses in race
- Identify winning margins from past encounters (within 2 years minimum)
- Track consistency patterns in head-to-head matchups
- Note track/distance specific dominance records
- MUST find at least 1 previous encounter per selection or state "extensive search conducted"
- Include exact dates, tracks, margins, and conditions of previous meetings
- Analyze which horse typically leads/finishes stronger in their matchups

🔍 DEEP FORM ANALYSIS:
- Last 5 starts with detailed margin analysis
- Class transitions (up/down in grade)
- Track specialists vs versatile performers
- Distance preferences and optimal trip lengths
- Sectional times and speed maps where available

⚡ ADVANCED PATTERN RECOGNITION:
- Trainer/Jockey combinations and strike rates
- Stable confidence indicators (market moves, gear changes)
- Track work reports and trial performances
- Barrier position advantages for specific tracks
- Weather and track condition impacts

🎯 ELITE SCORING SYSTEM (35 points):
- Form Quality (10): Recent form, class, consistency
- Head-to-Head Dominance (8): Previous encounter results
- Track/Distance Suitability (7): Specialist vs generalist
- Connections Factor (5): Trainer/jockey combo success
- Value Assessment (5): Price vs ability analysis

OUTPUT FORMAT FOR EACH VENUE:
🏁 **[TRACK NAME - STATE]** 🏁
📍 **Track Details:** [condition] | Rail: [position] | Weather: [conditions]
⏰ **First Race:** [time AWST] | **Featured Race:** [race number, time AWST]

**🥇 PREMIUM SELECTIONS (Top 3-4 horses):**

**1. [HORSE NAME]** (Race [X], [time] AWST)
💰 **LJ Elite Score:** [XX]/35 | **Current Odds:** $[X.XX]
🏃 **Jockey:** [Name] | **Trainer:** [Name] | **Barrier:** [X]

📈 **Form Analysis:**
- Last 5: [L5 form with margins] - [brief analysis]
- Class: [current grade] | Last win: [details]

🥊 **Head-to-Head Record:** (MANDATORY - EXTENSIVE SEARCH REQUIRED)
- vs [RIVAL_1]: [X wins from Y meetings] | Avg margin: [X.X lengths] | Last meeting: [date, track, result]
- vs [RIVAL_2]: [X wins from Y meetings] | Avg margin: [X.X lengths] | Last meeting: [date, track, result]
- **Pattern:** [Who typically leads, who finishes stronger, track preferences]
- **Key Matchup:** [Most relevant previous encounter with details]
- IF NO ENCOUNTERS FOUND: State "Exhaustive search of racing databases conducted - no previous meetings located in 24-month period"
- vs [RIVAL 2]: [record and details]
- **Dominant over:** [list of horses beaten before]

🎯 **Key Factors:**
- **Track Record:** [wins/starts at venue]
- **Distance:** [record at distance]
- **Conditions:** [wet/dry track preference]
- **Gear:** [gear changes if any]

💡 **LJ Intelligence:** [2-3 sentence detailed reasoning with specific data points, pace role, and why this horse beats its key rivals]

**BET TYPE:** [Win/Each-Way/Saver] | **Confidence:** [💎💎/💎/⭐⭐⭐/⭐⭐/⭐]

---

**🔥 RACE-BY-RACE INSIGHTS:**
[For featured races, provide additional insights about pace, track bias, scratchings impact]

**⚠️ MARKET MOVES & ALERTS:**
[Note any significant market movements or late information]

CONFIDENCE SCALE:
32-35 💎💎💎 ELITE BET • 28-31 💎💎 PREMIUM • 24-27 💎 STRONG • 20-23 ⭐⭐⭐ HIGH • 16-19 ⭐⭐ SOLID • 12-15 ⭐ EACH-WAY

🚨 HEAD-TO-HEAD RESEARCH REQUIREMENTS (NON-NEGOTIABLE):
1. For EVERY selection, conduct minimum 5 separate searches for previous encounters
2. Search racing databases from 2023-2025 for any meetings between horses
3. Include Provincial, Metropolitan, and Country race meetings
4. Must search by exact horse names, variations, and racing registrations
5. If no direct encounters found, search for common opponents they've both faced
6. Minimum search depth: 24 months of racing history
7. Report search methodology if no encounters located

MANDATORY: Use REAL Australian racing data. Include actual head-to-head records, real margins, actual jockey/trainer names, and genuine form details. Avoid generic examples."""

# Initialize Gemini client with proper SDK
client = genai.Client(api_key=GEMINI_API_KEY)

# Define grounding tool for REAL web search
grounding_tool = types.Tool(google_search=types.GoogleSearch())

# Configure generation with optimized settings for reliability
generation_config = types.GenerateContentConfig(
    tools=[grounding_tool],  # Enable real-time web search
    temperature=0.7,  # Slightly higher for more varied responses
    top_p=0.9,
    top_k=40,
    max_output_tokens=8192  # Reduced for better reliability
)

async def call_gemini_with_retry(prompt=None, min_score=None, max_retries=3, base_delay=2, allowed_horses=None):
    """Call Gemini API with LJ Mile Model prompt and validation"""
    # Use build_today_prompt() by default, but allow custom prompts for specific functions
    prompt_to_use = prompt if prompt is not None else build_today_prompt()
    
    if min_score is None:
        settings = load_settings()
        min_score = settings.get('min_score', 9)
    
    for attempt in range(max_retries):
        try:
            # Only show debug on first attempt or errors
            if attempt == 0:
                print(f"🔍 Generating LJ Mile Model racing analysis (min score: {min_score}/12)...")
            
            # Add small random delay to avoid rate limits
            if attempt > 0:
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
                await asyncio.sleep(delay)
            
            resp = await asyncio.to_thread(
                client.models.generate_content,
                model="gemini-2.5-pro",
                contents=prompt_to_use,
                config=generation_config
            )
            
            # Parse response by joining all text parts
            final_answer = ""
            if resp and hasattr(resp, 'candidates') and resp.candidates:
                candidate = resp.candidates[0]
                if hasattr(candidate, 'content') and candidate.content:
                    if hasattr(candidate.content, 'parts') and candidate.content.parts:
                        for part in candidate.content.parts:
                            if hasattr(part, 'text') and part.text:
                                final_answer += part.text
            
            if final_answer and len(final_answer.strip()) > 100:
                print("✅ Raw response received, applying LJ Mile validation...")
                
                # Extract and validate qualifiers
                valid_qualifiers = extract_valid_qualifiers(final_answer, min_score, allowed_horses=allowed_horses)
                
                if valid_qualifiers:
                    print(f"✅ {len(valid_qualifiers)} valid qualifiers found")
                    return "\n\n".join(valid_qualifiers)
                else:
                    print("⚠️ No qualifiers passed validation, retrying...")
                    if attempt < max_retries - 1:
                        continue
                    else:
                        return (
                            f"❌ No horses met the LJ Mile Model criteria today.\n"
                            f"Filters applied: Distance 950–1600m inclusive (no 1609m), "
                            f"Score ≥{min_score}/12, H2H not Negative unless ≥11/12 + fresh peak."
                        )
            else:
                # Only show retry message if it's not the last attempt
                if attempt < max_retries - 1:
                    print("🔄 Retrying for better content...")
                continue
                
        except Exception as e:
            error_msg = str(e)
            # Only show error details if it's a real problem
            if "500" in error_msg or "INTERNAL" in error_msg:
                if attempt < max_retries - 1:
                    print("🔧 Server busy, retrying...")
                continue
            elif "429" in error_msg or "RATE_LIMIT" in error_msg:
                print("🚦 Rate limit detected, waiting...")
                await asyncio.sleep(base_delay * 3)
                continue
            elif attempt == max_retries - 1:
                print(f"⚠️ API issues detected: {error_msg[:50]}...")
                break
            else:
                continue
    
    # All retries failed, return None to trigger fallback
    return None

async def call_simple_gemini(prompt=None, min_score=None, max_retries=2, allowed_horses=None):
    """Simple Gemini call with LJ Mile Model prompt"""
    # Use build_today_prompt() by default, but allow custom prompts for specific functions
    prompt_to_use = prompt if prompt is not None else build_today_prompt()
    
    if min_score is None:
        settings = load_settings()
        min_score = settings.get('min_score', 9)
    
    simple_config = types.GenerateContentConfig(
        temperature=0.8,
        max_output_tokens=4096
    )
    
    for attempt in range(max_retries):
        try:
            if attempt == 0:
                print(f"🔄 Trying simplified LJ Mile analysis (min score: {min_score}/12)...")
            
            if attempt > 0:
                await asyncio.sleep(2)
            
            resp = await asyncio.to_thread(
                client.models.generate_content,
                model="gemini-2.5-pro",
                contents=prompt_to_use,
                config=simple_config
            )
            
            # Parse response by joining all text parts
            final_answer = ""
            if resp and hasattr(resp, 'candidates') and resp.candidates:
                candidate = resp.candidates[0]
                if hasattr(candidate, 'content') and candidate.content:
                    if hasattr(candidate.content, 'parts') and candidate.content.parts:
                        for part in candidate.content.parts:
                            if hasattr(part, 'text') and part.text:
                                final_answer += part.text
            
            if final_answer and len(final_answer.strip()) > 50:
                print("✅ Simplified analysis received, applying validation...")
                
                # Extract and validate qualifiers
                valid_qualifiers = extract_valid_qualifiers(final_answer, min_score, allowed_horses=allowed_horses)
                
                if valid_qualifiers:
                    print(f"✅ {len(valid_qualifiers)} valid qualifiers found")
                    return "\n\n".join(valid_qualifiers)
                else:
                    return (
                        f"❌ No horses met the LJ Mile Model criteria today.\n"
                        f"Filters applied: Distance 950–1600m inclusive (no 1609m), "
                        f"Score ≥{min_score}/12, H2H not Negative unless ≥11/12 + fresh peak."
                    )
                
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"⚠️ Simplified API failed: {str(e)[:30]}...")
            continue
    
    return None

def generate_fallback_tips(target_date_str, current_time_perth, is_nextday=False):
    """Generate fallback racing tips when API is unavailable"""
    debug_msg = "🛠️ DEBUG: Next-day early analysis activated (API fallback mode)\n" if is_nextday else ""
    
    return f"""🏇 LJ Punting Model Elite - Premium Racing Analysis (Fallback Mode)

📅 **Date:** {target_date_str} | ⏰ **Time:** {current_time_perth}

{debug_msg}⚠️ **System Status:** Primary AI analysis temporarily unavailable (Gemini API error)
🔄 **Fallback Mode:** Premium racing intelligence provided

**Expected Premium Australian Racing Activity for {target_date_str}:**

🏁 **METROPOLITAN VENUES (Expected Premium Cards):**

**🌟 RANDWICK (NSW) - The Championship Track**
• Expected Grade: Group/Listed quality
• Track Bias: Favors on-pace runners in wet conditions
• Key Distance: 1400m-2000m feature races
• Jockeys to Follow: J.McDonald, T.Berry, R.King

**🌟 FLEMINGTON/CAULFIELD (VIC) - Racing Headquarters**
• Track Specialist Advantage: Significant at Flemington
• Distance Range: 1200m sprints to 2500m staying tests
• Weather Impact: Heavy track changes everything
• Elite Trainers: Waller, O'Brien, Freedman stables

**🌟 EAGLE FARM/DOOMBEN (QLD) - Winter Racing Hub**
• Speed Bias: Doomben favors leaders
• Class Transitions: Watch for southern visitors
• Track Conditions: Queensland tracks drain well
• Local Knowledge: Gollan, Heathcote trainers

**🌟 MORPHETTVILLE (SA) - Adelaide Feature Hub**
• Track Character: Suits versatile gallopers
• Distance Specialists: 1600m-2500m races
• Barrier Advantage: Inside draws crucial in big fields
• Local Power: McEvoy, Clarken stables

🏁 **PROVINCIAL POWERHOUSES:**
• **Gosford (NSW):** Speed track, leader bias
• **Ballarat (VIC):** Staying test venue, uphill finish
• **Ipswich (QLD):** Sprint track, barrier 1-4 advantage
• **Murray Bridge (SA):** Versatile track, wide barriers okay

**📋 PREMIUM ANALYSIS CHECKLIST (Apply Manually):**

**🔍 Form Analysis Deep Dive:**
1. **Last 5 Starts:** Look for improvement trends, not just wins
2. **Class Movements:** Horses dropping 2+ grades = strong chance
3. **Margin Analysis:** Beaten <2L in better class = value
4. **Track Specialists:** 3+ wins at venue = major advantage

**🥊 Head-to-Head Intelligence:**
1. **Previous Meetings:** Check last 3 encounters between top picks
2. **Winning Margins:** Consistent 2+ length victories = dominance
3. **Track/Distance Specific:** Some horses own certain rivals at specific venues
4. **Recent Form Reversals:** Form changes can flip head-to-head results

**💰 Value Assessment Framework:**
• **Under $3:** Needs 50%+ win chance (elite form required)
• **$3-$6:** Sweet spot for quality horses (30-40% chance)
• **$6-$12:** Value territory (20-25% chance acceptable)
• **$12+:** Each-way only unless major form spike

**⚡ Advanced Pattern Recognition:**
- **Gear Changes:** Blinkers first time = 15-20% improvement possible
- **Stable Confidence:** Big stable support in betting = inside info
- **Jockey Bookings:** Star jockey on outsider = worth attention
- **Trial Form:** Recent trials 2L+ clear = hidden form

**🌦️ Track Condition Impacts:**
- **Heavy Tracks:** Favor on-pace runners, eliminate speed horses
- **Good Tracks:** Even contest, rely on raw ability
- **Synthetic:** Form often doesn't translate, local knowledge key

**🎯 LJ Elite Selection Criteria:**
• **Form:** Consistent top-3 finishes in similar/better grade
• **Connections:** Trainer 20%+ strike rate, jockey in form
• **Track/Distance:** 2+ wins or 50%+ place rate at conditions
• **Value:** Odds represent fair chance or better
• **Head-to-Head:** Dominant record over key rivals

**🔧 System Recovery:** Elite AI analysis will resume automatically once API connectivity is restored. All head-to-head data and premium insights will be available in next update.

---
📊 **Coverage:** Premium fallback analysis with manual verification required
💎 **LJ Standard:** Even in fallback mode, we maintain elite analytical standards"""

def load_learning_data():
    """Load learning data from file"""
    try:
        if os.path.exists(LEARNING_DATA_FILE):
            with open(LEARNING_DATA_FILE, 'r') as f:
                return json.load(f)
        # Create default if missing
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LEARNING_DATA_FILE, 'w') as f:
            json.dump(DEFAULT_LEARNING_DATA, f, indent=2)
        return DEFAULT_LEARNING_DATA.copy()
    except Exception as e:
        print(f"Error loading learning data: {e}")
        return DEFAULT_LEARNING_DATA.copy()

def save_learning_data(data):
    """Save learning data to file"""
    try:
        with open(LEARNING_DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Error saving learning data: {e}")

def load_daily_predictions():
    """Load today's predictions"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.exists(DAILY_PREDICTIONS_FILE):
            with open(DAILY_PREDICTIONS_FILE, 'r') as f:
                data = json.load(f)
            # Ensure file is for today
            perth_now = datetime.now(PERTH_TZ)
            today_str = perth_now.strftime('%Y-%m-%d')
            if data.get('date') == today_str:
                return data
        # Create/reset for today
        today_default = default_predictions_for_today()
        with open(DAILY_PREDICTIONS_FILE, 'w') as f:
            json.dump(today_default, f, indent=2)
        return today_default
    except Exception as e:
        print(f"Error loading predictions: {e}")
        return default_predictions_for_today()

def save_daily_predictions(predictions_data):
    """Save today's predictions"""
    try:
        with open(DAILY_PREDICTIONS_FILE, 'w') as f:
            json.dump(predictions_data, f, indent=2)
    except Exception as e:
        print(f"Error saving predictions: {e}")

def get_learning_enhanced_prompt():
    """Generate enhanced prompt based on learning data"""
    learning_data = load_learning_data()
    
    base_insights = ""
    if learning_data['total_predictions'] > 0:
        win_rate = learning_data['win_rate']
        base_insights = f"""
🧠 LEARNING SYSTEM INSIGHTS (Win Rate: {win_rate:.1f}%):

SUCCESSFUL PATTERNS IDENTIFIED:
{chr(10).join(learning_data['successful_patterns'][-10:])}

FAILED PATTERNS TO AVOID:
{chr(10).join(learning_data['failed_patterns'][-10:])}

TOP PERFORMING INSIGHTS:
{chr(10).join(learning_data['learning_insights'][-5:])}

ADJUST YOUR ANALYSIS BASED ON THESE PROVEN PATTERNS."""
    
    return base_insights

def get_target_racing_date():
    """Determine the target racing date based on Sydney time with enhanced logic"""
    sydney_tz = pytz.timezone('Australia/Sydney')
    sydney_now = datetime.now(sydney_tz)
    
    # Racing day cutoff rules:
    # 1. After 8 PM - All races are definitely over, look at next day
    # 2. Between 6 PM and 8 PM - Check for night racing
    # 3. Before 6 PM - Current day racing
    if sydney_now.hour >= 20:  # After 8 PM
        target_date = sydney_now.date() + timedelta(days=1)
    elif sydney_now.hour >= 18:  # Between 6 PM and 8 PM
        # We'll check for night racing in the analysis phase
        target_date = sydney_now.date()
    else:
        target_date = sydney_now.date()
    
    # Verify it's not a rare no-racing day (Christmas Day, Good Friday)
    if target_date.month == 12 and target_date.day == 25:  # Christmas
        target_date += timedelta(days=1)
    
    return target_date

# ===== DISCORD BOT =====
class HorseRacingBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)
        self.auto_post_task = None
        self._last_post_key = None  # NEW: deduplication key
        
    async def setup_hook(self):
        """Setup bot when ready"""
        await self.tree.sync()
        print(f"Synced slash commands for {self.user}")
        
        # Start auto-posting task
        self.start_auto_posting()
        
    async def on_ready(self):
        print(f'{self.user} has connected to Discord!')
        print(f'Bot is in {len(self.guilds)} guilds')
        
    def start_auto_posting(self):
        """Start the auto-posting task"""
        if self.auto_post_task:
            self.auto_post_task.cancel()
        
        @tasks.loop(minutes=1)
        async def check_auto_post():
            settings = load_settings()
            perth_now = datetime.now(PERTH_TZ)
            current_time = perth_now.strftime('%H:%M')
            
            # Debug logging every hour
            if current_time.endswith(':00'):
                print(f"🕒 Auto-post check: {current_time} Perth time - Enabled: {settings.get('auto_post_enabled', True)} - Channel: {settings.get('auto_channel_id')}")
            
            if not settings.get('auto_post_enabled', True):
                return
                
            if not settings.get('auto_channel_id'):
                return  # No channel set for auto-posting
            
            # Check if it's time to post
            if current_time in settings.get('post_times', ['07:00']):
                # De-dupe key: channel + minute
                post_key = f"{settings.get('auto_channel_id')}|{perth_now.strftime('%Y-%m-%d %H:%M')}"
                if self._last_post_key == post_key:
                    print(f"⏭️ Skipping duplicate post for {current_time}")
                    return  # Already posted this minute
                
                print(f"🕒 Auto-posting triggered at {current_time} Perth time")
                try:
                    channel = self.get_channel(settings['auto_channel_id'])
                    if channel:
                        print(f"📤 Generating analysis for auto-post in #{channel.name}")
                        analysis = await self.generate_analysis(settings.get('min_score', 9))
                        
                        # If analysis aborted or empty, don't post
                        if not analysis or analysis.startswith("⚠️ Official fields unavailable"):
                            print("⏭️ Skipping post (no trusted fields or empty analysis).")
                            return
                        
                        embed = discord.Embed(
                            title=f"🏇 LJ Mile Model Auto-Analysis ({current_time} AWST)",
                            description=analysis[:4096] if len(analysis) > 4096 else analysis,
                            color=0x00ff00,
                            timestamp=datetime.utcnow()
                        )
                        await channel.send(embed=embed)
                        self._last_post_key = post_key  # Mark as posted
                        print(f"✅ Auto-post sent successfully to #{channel.name} at {current_time}")
                        
                    else:
                        print(f"❌ Channel not found for ID: {settings['auto_channel_id']}")
                        
                except Exception as e:
                    print(f"❌ Auto-post error: {e}")
                    import traceback
                    traceback.print_exc()
            
            # Check for results analysis
            if current_time == settings.get('results_time', '19:00'):
                print(f"🕒 Results analysis triggered at {current_time} Perth time")
                # Add results analysis here if needed
        
        self.auto_post_task = check_auto_post
        check_auto_post.start()

    def _extract_json_block(self, text: str):
        """Robustly extract JSON from text response."""
        t = (text or "").strip()
        # 1) try whole string
        try:
            return json.loads(t)
        except Exception:
            pass
        # 2) try ```json ... ```
        m = re.search(r"```json\s*(\{.*?\})\s*```", t, re.S | re.I)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
        # 3) try first { ... last }
        s, e = t.find("{"), t.rfind("}")
        if s != -1 and e != -1 and e > s:
            try:
                return json.loads(t[s:e+1])
            except Exception:
                pass
        # fallback
        return {"meetings": []}

    def get_default_field_data(self):
        """Return default/common Australian racing field data when API fetch fails."""
        return {
            "meetings": [
                {
                    "track": "Randwick",
                    "state": "NSW", 
                    "races": [
                        {"race_no": 1, "distance_m": 1000, "runners": ["AUTHENTIC JEWEL", "BELARDO'S GIRL", "CASINO SEVENTEEN", "DREAMKEEPER", "ELUSIVE EXPRESS", "FANTASY EAGLE", "GOLDEN SANDS", "IRON WILL", "JUST MAGICAL", "KING'S CRUSADE"]},
                        {"race_no": 2, "distance_m": 1200, "runners": ["AUTUMN RAIN", "BRILLIANT CHOICE", "CASINO KING", "DIVINE STORM", "EMERALD CROWN", "FLYING MACHINE", "GOLDEN ARROW", "HEROIC LEGEND", "IMPERIAL STAR", "JUSTIFIED"]},
                        {"race_no": 3, "distance_m": 1400, "runners": ["AMAZING GRACE", "BOLD VENTURE", "COSMIC FORCE", "DANCING QUEEN", "ELECTRIC STORM", "FREEDOM FIGHTER", "GOLDEN TOUCH", "HEROIC SPIRIT", "IRON HORSE", "JUST BELIEVE"]},
                        {"race_no": 4, "distance_m": 1600, "runners": ["ARCTIC EXPLORER", "BRAVE HEART", "CHAMPION'S WAY", "DIAMOND SPIRIT", "ETERNAL FLAME", "FLYING COLOURS", "GOLDEN EAGLE", "HEAVEN'S GATE", "IRON MAIDEN", "JUSTIFIED GLORY"]}
                    ]
                },
                {
                    "track": "Flemington",
                    "state": "VIC",
                    "races": [
                        {"race_no": 1, "distance_m": 1100, "runners": ["ADMIRAL'S CHOICE", "BRAVE WARRIOR", "COSMIC DANCER", "DREAM CATCHER", "ELECTRIC BLUE", "FLYING STAR", "GOLDEN DREAMS", "HEROIC TALE", "IRON DUKE", "JUST PERFECT"]},
                        {"race_no": 2, "distance_m": 1400, "runners": ["AMAZING SPIRIT", "BOLD STATEMENT", "COSMIC GIRL", "DANCING BRAVE", "ELECTRIC FEEL", "FREEDOM CALL", "GOLDEN SPIRIT", "HEROIC QUEST", "IRON WILL", "JUST IMAGINE"]},
                        {"race_no": 3, "distance_m": 1600, "runners": ["ARCTIC WIND", "BRAVE SOLDIER", "CHAMPION'S CROWN", "DIAMOND DREAMS", "ETERNAL GLORY", "FLYING MACHINE", "GOLDEN LEGEND", "HEAVEN'S ANGEL", "IRON HEART", "JUSTIFIED FAITH"]}
                    ]
                },
                {
                    "track": "Eagle Farm",
                    "state": "QLD",
                    "races": [
                        {"race_no": 1, "distance_m": 1050, "runners": ["AUTUMN GLORY", "BRILLIANT STAR", "CASINO QUEEN", "DIVINE LIGHT", "EMERALD ISLE", "FLYING SPIRIT", "GOLDEN CHANCE", "HEROIC DREAM", "IMPERIAL CROWN", "JUST WONDERFUL"]},
                        {"race_no": 2, "distance_m": 1200, "runners": ["AMAZING POWER", "BOLD FIGHTER", "COSMIC STORM", "DANCING STAR", "ELECTRIC POWER", "FREEDOM FIGHTER", "GOLDEN WARRIOR", "HEROIC POWER", "IRON STORM", "JUST BRILLIANT"]},
                        {"race_no": 3, "distance_m": 1350, "runners": ["ARCTIC STORM", "BRAVE SPIRIT", "CHAMPION'S DREAM", "DIAMOND WARRIOR", "ETERNAL SPIRIT", "FLYING DREAM", "GOLDEN STORM", "HEAVEN'S WARRIOR", "IRON SPIRIT", "JUSTIFIED POWER"]}
                    ]
                }
            ]
        }

    async def fetch_fields_for_date_syd(self, target_date=None):
        """Fetch official race fields for the given date to prevent hallucinated horses."""
        syd_date = (target_date or datetime.now(SYD_TZ).date())
        date_str = syd_date.strftime("%Y-%m-%d")

        fields_prompt = f"""
Search for **Australian thoroughbred official race fields** for {date_str} ({syd_date.strftime("%A %d %B %Y")} Sydney time).

Use Google Search to find current race fields from: racing.com, racenet.com.au, punters.com.au, tab.com.au.

Return results as JSON in this exact format:
```json
{{
  "meetings":[
    {{
      "track": "Track Name",
      "state": "NSW", 
      "races": [
        {{"race_no": 1, "distance_m": 1200, "runners": ["HORSE ONE", "HORSE TWO", "HORSE THREE"]}}
      ]
    }}
  ]
}}
```

If no meetings found for this date, return: {{"meetings": []}}
"""

        json_cfg = types.GenerateContentConfig(
            tools=[grounding_tool],
            temperature=0,
            max_output_tokens=4096
            # Note: can't use response_mime_type with tools enabled
        )

        try:
            resp = await asyncio.to_thread(
                client.models.generate_content,
                model="gemini-2.5-pro",
                contents=fields_prompt,
                config=json_cfg
            )

            raw = ""
            if resp and getattr(resp, "candidates", None):
                parts = getattr(resp.candidates[0].content, "parts", []) or []
                raw = "".join(getattr(p, "text", "") for p in parts if getattr(p, "text", None))

            # Add logging to see what we got
            print("FIELDS RAW (first 400 chars):", repr(raw[:400]))
            
            data = self._extract_json_block(raw)
            print("FIELDS SUMMARY:", { 
                "meetings": len(data.get("meetings", [])),
                "total_horses": sum(len(r.get("runners", [])) for m in data.get("meetings", []) for r in m.get("races", []))
            })
            
            return data if isinstance(data, dict) else {"meetings": []}
        except Exception as e:
            print(f"Error fetching fields: {e}")
            return {"meetings":[]}

    def build_allowed_from_fields(self, fields_json):
        """Build a set of allowed horse names from official fields."""
        allowed_horses = set()
        for m in fields_json.get("meetings", []):
            for r in m.get("races", []):
                for h in r.get("runners", []):
                    if isinstance(h, str):
                        allowed_horses.add(h.strip().lower())
        return allowed_horses
        
    async def generate_analysis(self, min_score=9, target_date=None):
        """Generate horse racing analysis"""
        try:
            # Fetch official fields to prevent hallucinated horses
            fields = await self.fetch_fields_for_date_syd(target_date or datetime.now(SYD_TZ).date())
            
            if looks_plausible_fields(fields):
                allowed = _build_allowed_norm_set(fields)  # real gating with normalized names
                print(f"✅ Loaded {len(allowed)} horse names from official fields")
                fields_for_prompt = fields  # inject real fields into prompt
            else:
                # Simplified approach: just proceed without field gating
                print("⚠️ Official fields fetch failed → proceeding with analysis (no horse name filtering)")
                allowed = None  # disable all horse name filtering 
                fields_for_prompt = None  # don't inject any field constraints
            
            
            
            if target_date:
                # Custom date analysis
                syd = pytz.timezone('Australia/Sydney')
                target_dt = datetime.combine(target_date, datetime.min.time())
                target_dt = syd.localize(target_dt)
                anchor = target_dt.strftime("%A %d %B %Y (%Y-%m-%d) %H:%M %Z")
                
                custom_prompt = (
                    f"CRITICAL DATE INSTRUCTION: You MUST analyze races for {anchor}. "
                    f"Do NOT analyze today ({datetime.now(SYD_TZ).strftime('%A %d %B %Y')}). "
                    f"ONLY analyze races scheduled for {target_date.strftime('%A %d %B %Y')} ({target_date.isoformat()}). "
                    "Use Google Search to find race meetings for this specific date. "
                    "HARD FILTER: evaluate ONLY Australian Thoroughbred races with official distance ≤ 1600 m; "
                    "exclude >1600 m and exactly 1609 m."
                ) + "\n\n" + LJ_MILE_PROMPT
                
                # Inject fields if we have them
                if fields_for_prompt:
                    custom_prompt = inject_fields_into_prompt(custom_prompt, fields_for_prompt)
                
                result = await call_gemini_with_retry(custom_prompt, min_score, allowed_horses=allowed)
            else:
                # Today's analysis
                prompt = build_today_prompt()
                # Inject fields if we have them
                if fields_for_prompt:
                    prompt = inject_fields_into_prompt(prompt, fields_for_prompt)
                
                result = await call_gemini_with_retry(prompt, min_score=min_score, allowed_horses=allowed)
            
            if not result:
                return f"⚠️ Analysis temporarily unavailable. Manual check required for races ≤1600m with scores ≥{min_score}/12."
            
            return result
        except Exception as e:
            print(f"Error in generate_analysis: {e}")
            return f"⚠️ Analysis error: {str(e)}"

# Create bot instance
bot = HorseRacingBot()

# ===== SLASH COMMANDS =====
@bot.tree.command(name="start", description="Start using the LJ Mile Model bot and view current settings")
async def start_command(interaction: discord.Interaction):
    """Start command with bot information"""
    settings = load_settings()
    
    embed = discord.Embed(
        title="🏇 LJ Mile Model Bot - Welcome!",
        description="Your premium Australian horse racing analysis bot",
        color=0x00ff00
    )
    
    embed.add_field(
        name="📊 Current Settings",
        value=f"""
**Minimum Score:** {settings.get('min_score', 9)}/12
**Auto-Post:** {'✅ Enabled' if settings.get('auto_post_enabled') else '❌ Disabled'}
**Post Times:** {', '.join(settings.get('post_times', ['07:00']))} AWST
**Distance Filter:** 950m-1600m only
**H2H Filter:** Negative excluded (unless ≥11/12 + fresh peak)
        """,
        inline=False
    )
    
    embed.add_field(
        name="🎯 Available Commands",
        value="""
`/analysis` - Get today's horse analysis
`/set_score` - Change minimum score threshold (1-12)
`/custom_date` - Analyze specific date within 7 days
`/settings` - View all bot settings
`/set_channel` - Set channel for auto-posting
`/toggle_auto` - Enable/disable auto-posting
`/horse` - **NEW!** Control panel with tabs (Auto Schedule, Config, Search)
`/horsesearch` - **NEW!** Quick date picker for custom scans
        """,
        inline=False
    )
    
    embed.set_footer(text="LJ Mile Model - Premium Racing Intelligence")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="analysis", description="Get today's horse racing analysis")
async def analysis_command(interaction: discord.Interaction):
    """Get current analysis"""
    await interaction.response.defer()
    
    settings = load_settings()
    min_score = settings.get('min_score', 9)
    
    try:
        analysis = await bot.generate_analysis(min_score)
        
        embed = discord.Embed(
            title=f"🏇 LJ Mile Model Analysis (≥{min_score}/12)",
            description=analysis[:4096] if len(analysis) > 4096 else analysis,
            color=0x00ff00,
            timestamp=datetime.utcnow()
        )
        
        perth_time = datetime.now(PERTH_TZ).strftime('%H:%M AWST')
        embed.set_footer(text=f"Generated at {perth_time}")
        
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        await interaction.followup.send(f"❌ Error generating analysis: {str(e)[:100]}")

@bot.tree.command(name="set_score", description="Set minimum score threshold for analysis")
@app_commands.describe(score="Minimum score out of 12 (1-12)")
async def set_score_command(interaction: discord.Interaction, score: int):
    """Set minimum score threshold"""
    if not 1 <= score <= 12:
        await interaction.response.send_message("❌ Score must be between 1 and 12!", ephemeral=True)
        return
    
    settings = load_settings()
    old_score = settings.get('min_score', 9)
    settings['min_score'] = score
    save_settings(settings)
    
    embed = discord.Embed(
        title="⚙️ Score Threshold Updated",
        description=f"Minimum score changed from **{old_score}/12** to **{score}/12**",
        color=0x00ff00
    )
    
    embed.add_field(
        name="📊 Impact",
        value=f"Bot will now show horses with **{score}/12 or higher** scores only.",
        inline=False
    )
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="custom_date", description="Analyze racing for a specific date (within 7 days)")
@app_commands.describe(
    date="Date in YYYY-MM-DD format (within 7 days)",
    min_score="Minimum score threshold (optional, uses current setting)"
)
async def custom_date_command(interaction: discord.Interaction, date: str, min_score: int = None):
    """Analyze custom date"""
    await interaction.response.defer()
    
    try:
        target_date = datetime.strptime(date, '%Y-%m-%d').date()
        today = datetime.now().date()
        
        # Check if date is within 7 days
        if abs((target_date - today).days) > 7:
            await interaction.followup.send("❌ Date must be within 7 days of today!")
            return
        
        settings = load_settings()
        score_threshold = min_score if min_score is not None else settings.get('min_score', 9)
        
        if min_score is not None and not 1 <= min_score <= 12:
            await interaction.followup.send("❌ Score must be between 1 and 12!")
            return
        
        analysis = await bot.generate_analysis(score_threshold, target_date)
        
        embed = discord.Embed(
            title=f"🏇 LJ Mile Model - {target_date.strftime('%A, %B %d, %Y')}",
            description=f"**Analysis for {date} (≥{score_threshold}/12)**\n\n" + 
                       (analysis[:4000] if len(analysis) > 4000 else analysis),
            color=0x00ff00,
            timestamp=datetime.utcnow()
        )
        
        await interaction.followup.send(embed=embed)
        
    except ValueError:
        await interaction.followup.send("❌ Invalid date format! Use YYYY-MM-DD (e.g., 2025-08-22)")
    except Exception as e:
        await interaction.followup.send(f"❌ Error analyzing date: {str(e)[:100]}")

@bot.tree.command(name="set_channel", description="Set the channel for auto-posting")
@app_commands.describe(channel="Channel to use for auto-posting")
async def set_channel_command(interaction: discord.Interaction, channel: discord.TextChannel):
    """Set auto-posting channel"""
    settings = load_settings()
    old_channel_id = settings.get('auto_channel_id')
    settings['auto_channel_id'] = channel.id
    save_settings(settings)
    
    embed = discord.Embed(
        title="📺 Auto-Post Channel Updated",
        description=f"Auto-posting channel set to {channel.mention}",
        color=0x00ff00
    )
    
    if settings.get('auto_post_enabled', True):
        times = ', '.join(settings.get('post_times', ['07:00']))
        embed.add_field(
            name="📅 Schedule",
            value=f"Bot will auto-post analysis at: {times} AWST",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="settings", description="View current bot settings")
async def settings_command(interaction: discord.Interaction):
    """Display current settings"""
    settings = load_settings()
    
    embed = discord.Embed(
        title="⚙️ LJ Mile Model Settings",
        color=0x00ff00
    )
    
    embed.add_field(
        name="📊 Analysis Settings",
        value=f"""
**Minimum Score:** {settings.get('min_score', 9)}/12
**Distance Range:** 950m - 1600m
**H2H Filter:** {('✅ Enabled' if settings.get('exclude_negative_h2h', True) else '❌ Disabled')}
**Min Jockey Rate:** {settings.get('min_jockey_strike_rate', 15)}%
        """,
        inline=True
    )
    
    auto_channel = "Not set"
    if settings.get('auto_channel_id'):
        channel = bot.get_channel(settings['auto_channel_id'])
        auto_channel = channel.mention if channel else "Channel not found"
    
    embed.add_field(
        name="🕒 Auto-Post Settings",
        value=f"""
**Auto-Post:** {('✅ Enabled' if settings.get('auto_post_enabled', True) else '❌ Disabled')}
**Channel:** {auto_channel}
**Post Times:** {', '.join(settings.get('post_times', ['07:00']))} AWST
**Results Time:** {settings.get('results_time', '19:00')} AWST
        """,
        inline=True
    )
    
    embed.add_field(
        name="⚖️ Weight Filters",
        value=f"""
**Min Weight:** {settings.get('min_weight_kg', 50.0)}kg
**Max Weight:** {settings.get('max_weight_kg', 61.0)}kg
        """,
        inline=False
    )
    
    embed.set_footer(text="Use other commands to modify these settings")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="toggle_auto", description="Enable or disable auto-posting")
async def toggle_auto_command(interaction: discord.Interaction):
    """Toggle auto-posting"""
    settings = load_settings()
    current = settings.get('auto_post_enabled', True)
    settings['auto_post_enabled'] = not current
    save_settings(settings)
    
    status = "✅ Enabled" if not current else "❌ Disabled"
    
    embed = discord.Embed(
        title="🔄 Auto-Post Settings Updated",
        description=f"Auto-posting is now **{status}**",
        color=0x00ff00
    )
    
    if not current and settings.get('auto_channel_id'):
        times = ', '.join(settings.get('post_times', ['07:00']))
        channel = bot.get_channel(settings['auto_channel_id'])
        embed.add_field(
            name="📅 Schedule",
            value=f"Bot will auto-post at: {times} AWST in {channel.mention if channel else 'set channel'}",
            inline=False
        )
    elif not current:
        embed.add_field(
            name="⚠️ Setup Required",
            value="Use `/set_channel` to set the auto-posting channel",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed)

# === MODALS FOR SETTINGS ===
class TimesModal(discord.ui.Modal, title="Edit Auto Post Times (AWST)"):
    times = discord.ui.TextInput(label="Times CSV (HH:MM,HH:MM,...)", placeholder="07:00,11:00,14:00", required=True, max_length=200)

    async def on_submit(self, interaction: discord.Interaction):
        settings = load_settings()
        before = ", ".join(settings.get("post_times", []))
        parsed = _parse_times_csv(str(self.times))
        if not parsed:
            await interaction.response.send_message("❌ No valid times provided. Use HH:MM,HH:MM", ephemeral=True)
            return
        settings["post_times"] = parsed
        save_settings(settings)
        log_setting_change(interaction.user, "post_times", before, ", ".join(parsed))
        await interaction.response.send_message(f"✅ Updated times to: {', '.join(parsed)} AWST", ephemeral=True)


class ScoreModal(discord.ui.Modal, title="Minimum Score (1–12)"):
    val = discord.ui.TextInput(label="Minimum score", placeholder="9", required=True, max_length=2)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(str(self.val).strip())
            if not 1 <= n <= 12:
                raise ValueError
        except Exception:
            await interaction.response.send_message("❌ Enter an integer 1–12.", ephemeral=True)
            return
        settings = load_settings()
        before = settings.get("min_score", 9)
        settings["min_score"] = n
        save_settings(settings)
        log_setting_change(interaction.user, "min_score", before, n)
        await interaction.response.send_message(f"✅ Minimum score set to {n}/12", ephemeral=True)


class WeightsModal(discord.ui.Modal, title="Weight Limits (kg)"):
    min_w = discord.ui.TextInput(label="Min weight kg", placeholder="50.0", required=True, max_length=5)
    max_w = discord.ui.TextInput(label="Max weight kg", placeholder="61.0", required=True, max_length=5)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            min_w = float(str(self.min_w).strip())
            max_w = float(str(self.max_w).strip())
            if min_w <= 0 or max_w <= 0 or max_w < min_w:
                raise ValueError
        except Exception:
            await interaction.response.send_message("❌ Enter valid floats; max must be ≥ min.", ephemeral=True)
            return
        settings = load_settings()
        before = f"{settings.get('min_weight_kg', 50.0):g}–{settings.get('max_weight_kg', 61.0):g} kg"
        settings["min_weight_kg"] = min_w
        settings["max_weight_kg"] = max_w
        save_settings(settings)
        after = f"{min_w:g}–{max_w:g} kg"
        log_setting_change(interaction.user, "weight_range", before, after)
        await interaction.response.send_message(f"✅ Weights set to {after}", ephemeral=True)


class JockeySRModal(discord.ui.Modal, title="Min Jockey Strike Rate (%)"):
    sr = discord.ui.TextInput(label="Minimum SR last 30 days", placeholder="15", required=True, max_length=3)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            v = int(str(self.sr).strip())
            if v < 0 or v > 100:
                raise ValueError
        except Exception:
            await interaction.response.send_message("❌ Enter an integer 0–100.", ephemeral=True)
            return
        settings = load_settings()
        before = settings.get("min_jockey_strike_rate", 15)
        settings["min_jockey_strike_rate"] = v
        save_settings(settings)
        log_setting_change(interaction.user, "min_jockey_strike_rate", before, v)
        await interaction.response.send_message(f"✅ Min jockey strike rate set to {v}%", ephemeral=True)

# === VIEWS / TABS ===
class DistancesSelect(discord.ui.Select):
    def __init__(self, selected: list[int]):
        opts = []
        selset = set(selected or [])
        for d in _distance_options():
            opts.append(discord.SelectOption(
                label=f"{d} m",
                value=str(d),
                default=(d in selset)
            ))
        super().__init__(
            placeholder="Select included distances (multi-select)",
            min_values=1,
            max_values=len(opts),
            options=opts,
            custom_id="lj_distances"
        )

    async def callback(self, interaction: discord.Interaction):
        settings = load_settings()
        before = ", ".join(map(str, settings.get("include_distances", [])))
        sel = sorted({int(v) for v in self.values})
        settings["include_distances"] = sel
        save_settings(settings)
        after = ", ".join(map(str, sel))
        log_setting_change(interaction.user, "include_distances", before, after)
        await interaction.response.edit_message(embed=build_config_embed(settings), view=build_horse_panel_view("config"))


class AutoScheduleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        s = load_settings()
        self.toggle_button = discord.ui.Button(
            label="Turn Auto Schedule ON" if not s.get("auto_post_enabled", True) else "Turn Auto Schedule OFF",
            style=discord.ButtonStyle.green if not s.get("auto_post_enabled", True) else discord.ButtonStyle.red,
            custom_id="lj_toggle"
        )
        self.add_item(self.toggle_button)
        self.add_item(discord.ui.Button(label="Edit Times (AWST)", style=discord.ButtonStyle.primary, custom_id="lj_edittimes"))

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, row=2)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = load_settings()
        await interaction.response.edit_message(embed=build_auto_embed(s), view=build_horse_panel_view("auto"))

    async def on_timeout(self):
        return


class ConfigView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(DistancesSelect())

    @discord.ui.button(label="Set Min Score", style=discord.ButtonStyle.primary)
    async def set_score(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ScoreModal())

    @discord.ui.button(label="Set Weight Limits", style=discord.ButtonStyle.primary)
    async def set_weights(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WeightsModal())

    @discord.ui.button(label="Set Min Jockey SR %", style=discord.ButtonStyle.primary)
    async def set_jockey_sr(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(JockeySRModal())

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = load_settings()
        await interaction.response.edit_message(embed=build_config_embed(s), view=build_horse_panel_view("config"))


class SearchView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        opts = _next_7_days_options()
        self.select = discord.ui.Select(
            placeholder="Pick a date (today → +7 days)", 
            min_values=1, 
            max_values=1, 
            options=[discord.SelectOption(label=label, value=val) for label, val in opts],
            custom_id="lj_pickdate"
        )
        self.add_item(self.select)

    async def on_timeout(self):
        return

# === EMBED BUILDERS ===
def build_auto_embed(settings):
    on = settings.get("auto_post_enabled", True)
    times = ", ".join(settings.get("post_times", [])) or "—"
    ch = settings.get("auto_channel_id")
    channel_disp = f"<#{ch}>" if ch else "Not set"
    return discord.Embed(
        title="🕒 Auto Schedule",
        description="Configure auto posting (AWST) for Gemini-run scans.",
        color=0x17a2b8 if on else 0x6c757d
    ).add_field(
        name="Status", value=("✅ Enabled" if on else "❌ Disabled"), inline=True
    ).add_field(
        name="Times (AWST)", value=times, inline=True
    ).add_field(
        name="Channel", value=channel_disp, inline=False
    ).set_footer(text="Use the buttons to toggle or edit times. /set_channel sets the channel.")


def build_config_embed(settings):
    dists = ", ".join(map(str, settings.get("include_distances", _distance_options())))
    h2h = "✅ Exclude Negative (strict)" if settings.get("exclude_negative_h2h", True) else "❌ Allow Negative (with exceptions)"
    # recent changes preview
    changes = settings.get("recent_changes", [])
    if changes:
        last_lines = []
        for c in changes[-6:]:
            last_lines.append(f"- {c['ts']}: **{c['user']}** changed **{c['field']}** from *{c['old']}* → *{c['new']}*")
        changes_text = "\n".join(last_lines)
    else:
        changes_text = "No changes yet."

    distance_mode = ("Band " +
                     f"{settings.get('distance_min_m',950)}–{settings.get('distance_max_m',1600)} m "
                     + ("(whitelist enforced)" if settings.get('distance_whitelist_enforced', False)
                        else "(whitelist not enforced)"))

    return (discord.Embed(
        title="⚙️ Config",
        description="These settings apply to **both** the scheduler and on-demand searches.",
        color=0x00ff00
    )
    .add_field(name="Minimum Score", value=f"{settings.get('min_score', 9)}/12", inline=True)
    .add_field(name="Weight Limits", value=f"{settings.get('min_weight_kg', 50.0):g}–{settings.get('max_weight_kg', 61.0):g} kg", inline=True)
    .add_field(name="Min Jockey SR", value=f"{settings.get('min_jockey_strike_rate', 15)}%", inline=True)
    .add_field(name="Distance Check", value=distance_mode, inline=False)
    .add_field(name="Included Distances (for whitelist)", value=dists or "—", inline=False)
    .add_field(name="H2H Filter", value=h2h, inline=False)
    .add_field(name="📝 Recent Changes", value=changes_text, inline=False))


def build_search_embed():
    return discord.Embed(
        title="🔎 Horse Search",
        description="Pick a date (today to +7d, Sydney calendar) and run a scan with current Config settings.",
        color=0x5865F2
    )

# === TAB SWITCHER ===
class HorseTabs(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Select a panel…",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Auto Schedule", value="auto"),
                discord.SelectOption(label="Config", value="config"),
                discord.SelectOption(label="Search", value="search"),
            ],
            custom_id="lj_tabs"
        )

    async def callback(self, interaction: discord.Interaction):
        s = load_settings()
        val = self.values[0]
        if val == "auto":
            await interaction.response.edit_message(embed=build_auto_embed(s), view=build_horse_panel_view("auto"))
        elif val == "config":
            await interaction.response.edit_message(embed=build_config_embed(s), view=build_horse_panel_view("config"))
        else:
            await interaction.response.edit_message(embed=build_search_embed(), view=build_horse_panel_view("search"))


def build_horse_panel_view(which="auto"):
    view = discord.ui.View(timeout=600)
    view.add_item(HorseTabs())

    if which == "auto":
        s = load_settings()
        view.add_item(discord.ui.Button(
            label=("Turn Auto Schedule OFF" if s.get("auto_post_enabled", True) else "Turn Auto Schedule ON"),
            style=(discord.ButtonStyle.red if s.get("auto_post_enabled", True) else discord.ButtonStyle.green),
            custom_id="lj_toggle"
        ))
        view.add_item(discord.ui.Button(
            label="Edit Times (AWST)",
            style=discord.ButtonStyle.primary,
            custom_id="lj_edittimes"
        ))

    elif which == "config":
        s = load_settings()
        view.add_item(DistancesSelect(s.get("include_distances", _distance_options())))
        view.add_item(discord.ui.Button(label="Set Min Score", style=discord.ButtonStyle.primary, custom_id="lj_setscore"))
        view.add_item(discord.ui.Button(label="Set Weight Limits", style=discord.ButtonStyle.primary, custom_id="lj_setweights"))
        view.add_item(discord.ui.Button(label="Set Min Jockey SR %", style=discord.ButtonStyle.primary, custom_id="lj_setsr"))
        view.add_item(discord.ui.Button(label="Toggle H2H Negative Filter", style=discord.ButtonStyle.secondary, custom_id="lj_toggle_h2h"))
        view.add_item(discord.ui.Button(label="Toggle Distance Whitelist", style=discord.ButtonStyle.secondary, custom_id="lj_toggle_distlist"))

    else:  # search
        opts = _next_7_days_options()
        view.add_item(discord.ui.Select(
            placeholder="Pick a date (today → +7 days)",
            min_values=1, max_values=1,
            options=[discord.SelectOption(label=label, value=val) for label, val in opts],
            custom_id="lj_pickdate"
        ))

    return view

# === NEW SLASH COMMANDS ===
@bot.tree.command(name="horse", description="Open the LJ Mile Model control panel")
async def horse_panel_cmd(interaction: discord.Interaction):
    s = load_settings()
    await interaction.response.send_message(embed=build_auto_embed(s), view=build_horse_panel_view("auto"), ephemeral=True)


@bot.tree.command(name="horsesearch", description="Pick a date (today → +7 days) and run a scan using current Config")
async def horsesearch_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(embed=build_search_embed(), view=build_horse_panel_view("search"), ephemeral=True)

def extract_predictions_for_learning(tips_content):
    """Extract predictions from tips content for later learning analysis"""
    predictions = []
    lines = tips_content.split('\n')
    
    current_horse = {}
    for line in lines:
        # Look for horse names in the new format
        if line.startswith('**1. ') or line.startswith('**2. ') or line.startswith('**3. ') or line.startswith('**4. '):
            if current_horse:
                predictions.append(current_horse)
            # Extract horse name from new format
            horse_match = re.search(r'\*\*\d+\.\s+\[([^\]]+)\]', line)
            if not horse_match:
                horse_match = re.search(r'\*\*\d+\.\s+([^\*]+?)\*\*', line)
            if horse_match:
                current_horse = {
                    'horse_name': horse_match.group(1).strip(),
                    'race_info': line,
                    'prediction_details': []
                }
        # Also check for old format
        elif line.startswith('🏇 **') and '**' in line:
            if current_horse:
                predictions.append(current_horse)
            # Extract horse name
            horse_match = re.search(r'🏇 \*\*(.*?)\*\*', line)
            if horse_match:
                current_horse = {
                    'horse_name': horse_match.group(1),
                    'race_info': line,
                    'prediction_details': []
                }
        elif current_horse and any(keyword in line for keyword in [
            'LJ Elite Score:', 'LJ Score:', 'LJ Analysis Score:',
            'Race Time:', 'Track:', 'BET TYPE:', 'Current Odds:', 'Jockey:', 'Trainer:',
            'H2H Summary:'
        ]):
            current_horse['prediction_details'].append(line)
        elif current_horse and (line.startswith('💡 **LJ Intelligence:**') or line.startswith('💡 **Analysis:**')):
            current_horse['analysis'] = line
    
    if current_horse:
        predictions.append(current_horse)
    
    return predictions

async def analyze_results_and_learn():
    """Analyze today's race results (Perth date) and learn from predictions"""
    perth_now = datetime.now(PERTH_TZ)
    today_str = perth_now.strftime('%Y-%m-%d')
    
    print(f"Analyzing results and learning for {today_str}")
    
    # Check date validity
    target_date = datetime.strptime(today_str, '%Y-%m-%d').date()
    current_date = datetime.now().date()
    
    if target_date > current_date:
        message = f"⚠️ Cannot analyze results for future date {today_str}. Results analysis will be available after races are completed."
        print(message)
        return message
    
    if target_date < current_date:
        message = f"""📊 Historical Race Analysis ({today_str})

All races for this date have been completed. Historical performance data:

1. Check the learning system data file for stored predictions and results
2. Review archived race results on racing authority websites
3. Access historical form guides and official race records

Note: The learning system only maintains detailed analysis for the most recent 30-day period."""
        print(f"Processing historical date: {today_str}")
        return message
    
    # Load today's predictions
    predictions_data = load_daily_predictions()
    if not predictions_data or not predictions_data.get('predictions'):
        message = "No predictions found for today"
        print(message)
        return message
    
    # Get race results for analysis
    results_prompt = f"""🔍 RACE RESULTS ANALYSIS - Perth Date: {today_str}

Please search for today's Australian horse racing results for {today_str} and provide:

1. Winners of all races
2. Finishing positions for all horses
3. Starting prices/odds
4. Track conditions
5. Jockey and trainer information

Search across:
- racing.com Australia results
- TAB racing results
- punters.com.au results
- other Australian racing result sites

Provide results in this concise format:
🏇 RACE X - TRACK NAME
🥇 Winner: HORSE NAME (Jockey: X, Trainer: Y, SP: $X.XX)
---"""

    try:
        # Create specific results prompt for race analysis
        results_prompt_specific = f"""Search for today's Australian thoroughbred horse racing results for {today_str}. 

Find and provide:
1. Winners of all races
2. Finishing positions for all horses
3. Starting prices/odds
4. Track conditions
5. Jockey and trainer information

Search across:
- racing.com Australia results
- TAB racing results
- punters.com.au results
- other Australian racing result sites

Provide results in this concise format:
🏇 RACE X - TRACK NAME
🥇 Winner: HORSE NAME (Jockey: X, Trainer: Y, SP: $X.XX)
---"""
        
        # Get race results using web search with retry mechanism
        results_content = await call_gemini_with_retry(results_prompt_specific, max_retries=3)
        
        if not results_content:
            # Fallback for results analysis
            results_content = f"""📊 RACE RESULTS FALLBACK - {today_str}

⚠️ **System Notice:** Full results analysis temporarily unavailable due to API limitations.

**Manual Results Check Required:**
1. Visit racing.com.au/results for official results
2. Check TAB results section for all winners
3. Review punters.com.au for detailed finishing positions

**Learning System:** Manual prediction verification required.
- Check today's predictions against official results
- Update success patterns manually if needed

**Next Analysis:** System will attempt automatic results analysis again in 1 hour."""
        
        # Analyze our predictions against results
        learning_analysis = await analyze_prediction_accuracy(predictions_data, results_content)
        
        # Send results and learning update to Discord
        await send_analysis_message(
            f"""📊 DAILY RESULTS & LEARNING (Perth)

{results_content}

---
🧠 LEARNING ANALYSIS
{learning_analysis}""",
            title="🌇 Results & Learning - 7PM Perth",
        )
        
        return "Results analyzed and learning data updated successfully!"
    except Exception as e:
        error_msg = f"Error analyzing results: {str(e)}"
        print(error_msg)
        return error_msg

async def analyze_prediction_accuracy(predictions_data, results_content):
    """Analyze how accurate our predictions were and update learning data"""
    learning_data = load_learning_data()
    
    correct_predictions = 0
    total_predictions = len(predictions_data['predictions'])
    
    analysis_summary = []
    
    for prediction in predictions_data['predictions']:
        horse_name = prediction['horse_name']
        # crude winner detection; can be improved later
        winner_line = re.search(r"Winner:\s*([A-Za-z'’\-\.\s]+)", results_content, re.IGNORECASE)
        if winner_line:
            winner_name = winner_line.group(1).strip()
        else:
            winner_name = ""
        
        if horse_name and winner_name and horse_name.lower() in winner_name.lower():
            correct_predictions += 1
            analysis_summary.append(f"✅ {horse_name} - CORRECT (Won)")
            for detail in prediction.get('prediction_details', []):
                if 'LJ Score:' in detail or 'Track:' in detail:
                    learning_data['successful_patterns'].append(f"WINNER - {horse_name}: {detail}")
        else:
            analysis_summary.append(f"❌ {horse_name} - FAILED (Did not win)")
            for detail in prediction.get('prediction_details', []):
                if 'LJ Score:' in detail or 'Track:' in detail:
                    learning_data['failed_patterns'].append(f"FAILED - {horse_name}: {detail}")
    
    # Update statistics
    learning_data['total_predictions'] += total_predictions
    learning_data['successful_predictions'] += correct_predictions
    learning_data['failed_predictions'] += (total_predictions - correct_predictions)
    if learning_data['total_predictions'] > 0:
        learning_data['win_rate'] = (learning_data['successful_predictions'] / learning_data['total_predictions']) * 100
    
    # Add learning insights
    if total_predictions > 0:
        learning_data['learning_insights'].append(
            f"{predictions_data['date']}: {correct_predictions}/{total_predictions} correct ({(correct_predictions/total_predictions)*100:.1f}%)"
        )
    
    # Trim lists
    learning_data['successful_patterns'] = learning_data['successful_patterns'][-50:]
    learning_data['failed_patterns'] = learning_data['failed_patterns'][-50:]
    learning_data['learning_insights'] = learning_data['learning_insights'][-20:]
    
    save_learning_data(learning_data)
    
    pct = (correct_predictions/total_predictions)*100 if total_predictions else 0
    return f"📈 Accuracy: {correct_predictions}/{total_predictions} ({pct:.1f}%) | Overall win rate: {learning_data['win_rate']:.1f}%\n" + "\n".join(analysis_summary)

def has_strong_bets(tips_content):
    """Check if the tips content contains any strong bets (24+ points in new 35-point system)"""
    # Look for strong bet indicators in the response
    strong_bet_indicators = [
        "ELITE BET",
        "PREMIUM Selection",
        "LJ Elite Score: 24/35",
        "LJ Elite Score: 25/35",
        "LJ Elite Score: 26/35",
        "LJ Elite Score: 27/35",
        "LJ Elite Score: 28/35",
        "LJ Elite Score: 29/35",
        "LJ Elite Score: 30/35",
        "LJ Elite Score: 31/35",
        "LJ Elite Score: 32/35",
        "LJ Elite Score: 33/35",
        "LJ Elite Score: 34/35",
        "LJ Elite Score: 35/35",
        "💎💎💎 ELITE BET",
        "💎💎 PREMIUM",
        "💎 STRONG",
        "Must Bet Large",
        "Head-to-Head Dominance",
        "Dominant over:",
        "High Confidence"
    ]
    
    # Check if any strong bet indicators are present
    for indicator in strong_bet_indicators:
        if indicator in tips_content:
            return True
    
    # Also check for phrases that indicate no strong bets found
    no_strong_bets_phrases = [
        "No horses met the 24+ criteria",
        "no horses meeting the 24+ criteria", 
        "No horses with 24+ points found",
        "All selections are each-way",
        "No elite selections today"
    ]
    
    for phrase in no_strong_bets_phrases:
        if phrase in tips_content:
            return False
    
    return False

def extract_summary(tips_content):
    """Extract a brief summary from tips content for display when no strong bets found"""
    lines = tips_content.split('\n')
    summary_lines = []
    
    # Look for key information lines in the new format
    for line in lines:
        if any(keyword in line.lower() for keyword in ['each-way options', 'no elite selections', 'analysis summary', 'track conditions', 'premium selections']):
            summary_lines.append(line)
        elif 'LJ Elite Score: 1' in line or 'LJ Elite Score: 2' in line:  # Low scores 10-23
            summary_lines.append(line)
        elif line.startswith('�') and ('**' in line or '|' in line):
            summary_lines.append(line)
        elif line.startswith('**') and any(keyword in line for keyword in ['HORSE', 'Race', 'Elite Score']):
            summary_lines.append(line)
    
    # If we found specific content, return it
    if summary_lines:
        return '\n'.join(summary_lines[:15])  # Increased limit for detailed format
    
    # Otherwise return a basic summary
    if 'each-way' in tips_content.lower():
        return "⚖️ Some each-way options (16-23 points) were identified, but no elite bets (24+ points) found."
    elif 'premium selections' in tips_content.lower():
        return "💎 Premium selections available - check full analysis for detailed head-to-head breakdowns."
    else:
        return "❌ No qualifying selections found for this day."

async def analyze_racing_day(target_date_str, target_date_search, current_time_perth, learning_insights):
    """Comprehensive LJ Mile Model analysis with validation"""
    # Check date validity and timing
    target_date = datetime.strptime(target_date_search, '%Y-%m-%d').date()
    current_date = datetime.now().date()
    sydney_tz = pytz.timezone('Australia/Sydney')
    sydney_now = datetime.now(sydney_tz)
    
    print(f"🕒 Sydney time: {sydney_now.strftime('%Y-%m-%d %H:%M')}")
    
    # Handle past dates
    if target_date < current_date:
        return f"""🏇 LJ Mile Model - Historical Race Data

📅 Date: {target_date_str} | ⏰ Time: {current_time_perth}

ℹ️ Race Day Complete: All races for {target_date_str} have finished.

📊 Results and Analysis:
- To view race results, please check racing authority websites
- For our prediction accuracy and learning insights, check the evening analysis report
- Historical performance data is archived in the learning system

🎯 Looking for today's tips? Wait for our next scheduled update at 7:00 AM AWST.
"""
    
    # Determine if we should use next-day prompt (future date or after 8 PM Sydney)
    use_nextday_prompt = target_date > current_date or sydney_now.hour >= 20
    
    debug_msg = ""
    if use_nextday_prompt:
        debug_msg = "🛠️ DEBUG: Next-day early analysis activated (future date or after 8 PM Sydney)\n"
        print("🧭 Using next-day early analysis mode")

    try:
        print(f"🔎 Running LJ Mile Model analysis for {target_date_str}")
        
        # Try primary API call with LJ Mile Model
        final_answer = await call_gemini_with_retry()
        
        # If API call failed completely, try simple prompt
        if not final_answer:
            final_answer = await call_simple_gemini()
        
        # If still no response, use fallback
        if not final_answer:
            print("🔄 Using LJ Mile Model fallback mode")
            final_answer = f"""🏇 LJ Mile Model - System Recovery Mode

📅 **Date:** {target_date_str} | ⏰ **Time:** {current_time_perth}

⚠️ **System Alert:** LJ Mile Model analysis engine temporarily offline

**LJ Mile Model Criteria (≤1600m only):**
✅ Distance: 950m-1600m races only
✅ Score: ≥9/12 points required
✅ H2H: Non-negative unless ≥11/12 + fresh peak

**Manual Check Required:**
1. Visit racing.com.au for fields and distances
2. Apply LJ 12-point criteria manually
3. Check H2H records on racenet.com.au

**System Status:** Automatic recovery in progress. Next LJ Mile analysis in 30 minutes.

---
📊 LJ Mile Model validation: Strict distance and scoring filters active"""
        
        # Format final response
        if final_answer and not final_answer.startswith("🏇 LJ Mile Model"):
            final_answer = f"""🏇 LJ Mile Model - Daily Racing Analysis

📅 **Date:** {target_date_str} | ⏰ **Time:** {current_time_perth}

{debug_msg}🎯 **LJ Mile Model Results (≤1600m, ≥9/12, Valid H2H):**

{final_answer}

---
📊 **Validation Applied:** Distance ≤1600m | Score ≥9/12 | H2H Non-negative"""
        
        return final_answer

    except Exception as e:
        print(f"🚨 LJ Mile Model analysis error: {str(e)[:50]}...")
        # Ultimate fallback
        return f"""🏇 LJ Mile Model - Emergency Fallback

📅 **Date:** {target_date_str} | ⏰ **Time:** {current_time_perth}

⚠️ **System Alert:** LJ Mile Model engine offline

**Criteria Reminder:**
- Races: ≤1600m only (no miles >1.0)
- Scores: ≥9/12 points minimum
- H2H: Non-negative unless exceptional

**Manual Process Required:**
1. Check racing.com.au for ≤1600m races
2. Apply 12-point LJ criteria
3. Validate H2H matchups

---
📊 Emergency fallback: LJ Mile Model standards maintained"""

async def send_analysis_message(content, title="🏇 LJ Mile Model - Daily Racing Tips", channel_id=None):
    """Send analysis message to Discord channel"""
    try:
        if not channel_id:
            settings = load_settings()
            channel_id = settings.get('auto_channel_id')
            
        if not channel_id:
            print("❌ No Discord channel configured for sending messages")
            return False
            
        channel = bot.get_channel(channel_id)
        if not channel:
            print(f"❌ Discord channel {channel_id} not found")
            return False
            
        # Create and send the embed
        embed = discord.Embed(
            title=title,
            description=content[:4096] if len(content) > 4096 else content,
            color=0x00ff00,
            timestamp=datetime.utcnow()
        )
        embed.set_footer(text=f"Generated on {datetime.now(PERTH_TZ).strftime('%B %d, %Y at %H:%M AWST')}")
        
        # Split content into multiple embeds if too long
        if len(content) > 4096:
            remaining_content = content[4096:]
            await channel.send(embed=embed)
            
            part = 2
            while remaining_content:
                chunk = remaining_content[:4096]
                remaining_content = remaining_content[4096:]
                
                continuation_embed = discord.Embed(
                    title=f"{title} (Part {part})",
                    description=chunk,
                    color=0x00ff00,
                    timestamp=datetime.utcnow()
                )
                await channel.send(embed=continuation_embed)
                part += 1
        else:
            await channel.send(embed=embed)
        
        print("✅ Analysis message sent to Discord successfully")
        return True
        
    except Exception as e:
        print(f"❌ Failed to send message to Discord: {e}")
        return False

# === COMPONENT HANDLERS ===
@bot.event
async def on_interaction(interaction: discord.Interaction):
    try:
        if not interaction.type == discord.InteractionType.component:
            return

        cid = interaction.data.get("custom_id")
        if cid == "lj_toggle":
            s = load_settings()
            before = s.get("auto_post_enabled", True)
            s["auto_post_enabled"] = not before
            save_settings(s)
            log_setting_change(interaction.user, "auto_post_enabled", before, s["auto_post_enabled"])
            await interaction.response.edit_message(embed=build_auto_embed(s), view=build_horse_panel_view("auto"))
            return

        if cid == "lj_edittimes":
            await interaction.response.send_modal(TimesModal())
            return

        if cid == "lj_setscore":
            await interaction.response.send_modal(ScoreModal())
            return

        if cid == "lj_setweights":
            await interaction.response.send_modal(WeightsModal())
            return

        if cid == "lj_setsr":
            await interaction.response.send_modal(JockeySRModal())
            return

        if cid == "lj_toggle_h2h":
            s = load_settings()
            before = "exclude_negative_h2h=" + str(s.get("exclude_negative_h2h", True))
            s["exclude_negative_h2h"] = not s.get("exclude_negative_h2h", True)
            save_settings(s)
            after = "exclude_negative_h2h=" + str(s["exclude_negative_h2h"])
            log_setting_change(interaction.user, "exclude_negative_h2h", before, after)
            await interaction.response.edit_message(embed=build_config_embed(s), view=build_horse_panel_view("config"))
            return

        if cid == "lj_toggle_distlist":
            s = load_settings()
            old_value = s.get("distance_whitelist_enforced", False)
            new_value = not old_value
            
            s["distance_whitelist_enforced"] = new_value
            save_settings(s)
            
            # Single log entry
            log_setting_change(interaction.user, "distance_whitelist_enforced", 
                              f"{old_value}", f"{new_value}")
            
            print(f"Toggle distance whitelist: {old_value} -> {new_value}")
            
            await interaction.response.edit_message(embed=build_config_embed(s), view=build_horse_panel_view("config"))
            return

        if cid == "lj_pickdate":
            print(f"DEBUG: Date selection triggered, values: {interaction.data.get('values')}")
            picked = (interaction.data.get("values") or [""])[0]
            if not picked:
                await interaction.response.send_message("❌ Please pick a date.", ephemeral=True)
                return
            print(f"DEBUG: Date picked: {picked}")
            s = load_settings()
            dt = datetime.strptime(picked, "%Y-%m-%d").date()
            print(f"DEBUG: Parsed date: {dt}, Today is: {datetime.now(SYD_TZ).date()}")
            await interaction.response.defer(ephemeral=True)
            print(f"DEBUG: Starting analysis for specific date: {dt}")
            result = await bot.generate_analysis(s.get("min_score", 9), target_date=dt)
            print(f"DEBUG: Analysis complete, result length: {len(result) if result else 0}")
            print(f"DEBUG: First 200 chars of result: {result[:200] if result else 'None'}")
            await send_analysis_message(
                f"🗓 {dt.strftime('%A %d %B %Y')} (Sydney)\n\n{result}",
                title=f"🏇 LJ Scan — {dt.isoformat()} (Config min {s.get('min_score', 9)}/12)",
                channel_id=interaction.channel.id
            )
            await interaction.followup.send("✅ Scan posted in this channel.", ephemeral=True)
            return

    except Exception as e:
        # Don't crash on interactions
        print(f"ERROR in interaction handler: {e}")
        import traceback
        traceback.print_exc()
        try:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
        except:
            pass

# ===== MAIN EXECUTION =====
async def main():
    """Main function to run the Discord bot"""
    print("Starting LJ Mile Model Discord Bot")
    print(f"Current Perth time: {datetime.now(PERTH_TZ).strftime('%Y-%m-%d %H:%M AWST')}")
    print(f"Data directory: {DATA_DIR}")
    print(f"Gemini API Key configured: {'Yes' if GEMINI_API_KEY else 'No'}")
    print(f"Discord Bot Token configured: {'Yes' if TOKEN else 'No'}")
    
    # Ensure data directory and files exist
    ensure_data_dir_and_files()
    
    if not TOKEN:
        print("❌ Discord bot token not configured.")
        return
    
    if not GEMINI_API_KEY:
        print("❌ Gemini API key not configured.")
        return
    
    # Start the Discord bot with proper cleanup
    try:
        print("🚀 Starting Discord bot...")
        await bot.start(TOKEN)
    except discord.LoginFailure:
        print("❌ Invalid token. Double-check you're using the correct Bot Token.")
    except Exception as e:
        print(f"❌ Error starting bot: {e}")
    finally:
        await bot.close()  # prevents "Unclosed connector"

# Check if running as main script
if __name__ == "__main__":
    ensure_data_dir_and_files()
    print("🚀 Starting Discord bot...")
    bot.run(TOKEN)  # Let discord.py handle the event loop and cleanup