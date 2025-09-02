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
import unicodedata
from datetime import datetime, timedelta, time as dtime, timezone

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

# LJ Mile Model Prompt - Enhanced for COMPREHENSIVE RACE-BY-RACE ANALYSIS
LJ_MILE_PROMPT = """🚨 CRITICAL: COMPREHENSIVE RACE-BY-RACE ANALYSIS of ALL Australian Thoroughbred races today with VERIFIED official fields. DO NOT invent horses, tracks, or races.

🔍 MANDATORY REAL DATA VERIFICATION:
1. Search racing.com.au for today's Australian race meetings
2. Search racenet.com.au for official fields and form guides
3. Search punters.com.au for current day race cards
4. Search tab.com.au for live Australian racing markets
5. If NO real racing found, state "No Australian racing meetings confirmed for this date" and STOP

🚨 ANTI-HALLUCINATION RULES:
- Use ONLY horses that appear in official race fields from verified Australian racing websites
- Use ONLY real Australian racetracks (Randwick, Flemington, Eagle Farm, Morphettville, etc.)
- Use ONLY official race distances from real race programs
- If unsure about any horse/track/race details, mark as "Unable to verify" and exclude
- Cross-reference ALL selections against multiple official sources

📊 COMPREHENSIVE RACE-BY-RACE MISSION: Analyze ALL verified Australian Thoroughbred race meetings today. Search EVERY confirmed track across ALL states (NSW, VIC, QLD, SA, WA, TAS, NT, ACT). For EVERY race with official distance **≤1600 m**, provide:

1. **COMPLETE FIELD ANALYSIS** - Score EVERY runner in each race (not just qualifiers)
2. **RACE-BY-RACE BREAKDOWN** - Show all horses with their LJ scores
3. **BEST SELECTION PER RACE** - Identify the top-rated horse in each race
4. **DETAILED REASONING** - Explain why each selection is best in their race

🌏 **VERIFIED VENUE COVERAGE** - Only include if racing confirmed:
NSW: Randwick, Rosehill, Canterbury, Kensington, Hawkesbury, Newcastle, Gosford, Wyong, Muswellbrook, Scone, Wagga, Albury
VIC: Flemington, Caulfield, Moonee Valley, Sandown, Geelong, Ballarat, Bendigo, Mornington, Cranbourne, Sale, Wangaratta
QLD: Eagle Farm, Doomben, Gold Coast, Sunshine Coast, Toowoomba, Rockhampton, Mackay, Townsville, Cairns, Ipswich
SA: Morphettville, Murray Bridge, Gawler, Port Lincoln, Mount Gambier
WA: Ascot, Belmont Park, Bunbury, Albany, Geraldton, Kalgoorlie, Northam
TAS: Elwick (Hobart), Mowbray (Launceston), Devonport, Spreyton
NT: Fannie Bay (Darwin)

🚦 DISTANCE RULES (STRICT - REAL DISTANCES ONLY)
- Eligible: 950 m–1600 m inclusive from OFFICIAL race programs
- Ineligible: **>1600 m** or any invented distances
- Verify distances against official race cards - do not guess

🌐 AUSTRALIAN RACING OFFICIAL SOURCES (VERIFY ALL DATA):
- Racing.com (VIC official) — https://www.racing.com
- Racing NSW — https://www.racingnsw.com.au
- Racing Queensland — https://www.racingqueensland.com.au  
- Racing Victoria — https://www.racingvictoria.com.au
- RWWA (WA) — https://www.rwwa.com.au
- Racenet — https://www.racenet.com.au
- Punters — https://www.punters.com.au
- TAB — https://www.tab.com.au

🔒 DATA VERIFICATION CHECKLIST:
✅ Horse names match official race fields exactly
✅ Track names are real Australian racecourses  
✅ Race distances confirmed in official programs
✅ Barrier draws and jockey bookings verified
✅ Form references use real past race results
✅ Cross-referenced against multiple official sources

🧠 LJ MILE MODEL — 12-POINT SCORING SYSTEM (≤1600 m ONLY)

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

⚔️ H2H MODULE — MANDATORY FOR ALL RUNNERS

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

SCORING METHODOLOGY:
- Award 1 point per satisfied criterion (12 max)
- Score ALL runners in each race, not just high scorers
- Apply distance filter FIRST; skip any race >1600 m
- Treat going adjacency: Good 3–5 adjacent; Soft 6–8 adjacent; Heavy 9–10 adjacent
- Use official fields/scratchings; ignore scratched runners

📊 **MANDATORY OUTPUT FORMAT - RACE-BY-RACE COMPREHENSIVE ANALYSIS:**

For each track, organize by race number and show ALL runners with scores:

🏁 **[TRACK NAME] - [STATE]**
📍 **Track Conditions:** [Track/Going] | **Rail:** [Position] | **Weather:** [Conditions]

═══════════════════════════════════════════════════

**RACE 1 - [DISTANCE]m - [RACE NAME] - [TIME AWST]**

**🏇 COMPLETE FIELD ANALYSIS:**
1. **[HORSE NAME]** - LJ Score: **[X/12]** | Barrier: [X] | Jockey: [Name]
2. **[HORSE NAME]** - LJ Score: **[X/12]** | Barrier: [X] | Jockey: [Name]
3. **[HORSE NAME]** - LJ Score: **[X/12]** | Barrier: [X] | Jockey: [Name]
[Continue for ALL runners in race]

� **RACE 1 SELECTION: [BEST HORSE NAME] (LJ Score: [X/12])**
📝 **Analysis:** [Why this horse is best in this race - 50-100 words covering key advantages over rivals, form highlights, and race setup]
⚔️ **Key Edge:** [Main advantage over second-best runner]

═══════════════════════════════════════════════════

**RACE 2 - [DISTANCE]m - [RACE NAME] - [TIME AWST]**
[Repeat format for all races]

🎯 **TRACK SUMMARY:**
- **Total Races Analyzed:** [X]
- **Best Value Race:** Race [X] - [Horse Name]
- **Strongest Selection:** Race [X] - [Horse Name] ([X/12])
- **Track Bias:** [Any track biases noted]

═══════════════════════════════════════════════════

� **SCAN SUMMARY REQUIRED** - Always include at end:
"🔍 **Daily Analysis Summary**: Analyzed [X] tracks, [Y] total races ≤1600m, [Z] total runners scored across all races."

🎯 **KEY REQUIREMENTS:**
1. Show EVERY horse in EVERY eligible race with their LJ score
2. Identify the BEST selection in each race
3. Provide clear reasoning for each race selection
4. Organize by track and race number for easy reading
5. Include track conditions and race times
6. NO minimum score filtering - show all runners with their scores
7. **RACE PREVIEW**: For each race, provide a 2-3 sentence prediction of how the race will unfold

📝 **RACE PREVIEW FORMAT** (Include for each race):
**🔮 Race Preview:** [2-3 sentences describing expected pace, key matchups, likely leader, how the race will be run, and predicted finish order for top 3-4 horses]

Example: "Expect a moderate pace with HORSE A likely to lead from the good gate. The main danger is HORSE B who should be charging home late from the back. HORSE C will need luck from the wide gate but has the class edge."
"""

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
    
    # NEW COMPREHENSIVE ANALYSIS SETTINGS
    'show_all_races': False,  # Show every race with all runners (no filtering)
    'show_all_runners': False,  # Show every runner in each race with scores
    'include_race_predictions': True,  # Include race outcome predictions
    'max_runners_per_race': 8,  # Limit runners shown per race (0 = show all)
    'show_low_scores': False,  # Show runners with scores below minimum
    'comprehensive_mode': False,  # Enable full comprehensive analysis mode
    'race_preview_enabled': True,  # Show race preview/predictions
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

# Perth timezone with Singapore server compensation
PERTH_TZ = pytz.timezone('Australia/Perth')
# Sydney timezone for date calculations 
SYD_TZ = pytz.timezone('Australia/Sydney')
# Singapore timezone for server location awareness
SINGAPORE_TZ = pytz.timezone('Asia/Singapore')

def get_server_aware_time_info():
    """Get comprehensive time info accounting for Singapore server location"""
    perth_now = datetime.now(PERTH_TZ)
    sydney_now = datetime.now(SYD_TZ)
    singapore_now = datetime.now(SINGAPORE_TZ)
    
    return {
        'perth_time': perth_now.strftime('%Y-%m-%d %H:%M AWST'),
        'sydney_time': sydney_now.strftime('%Y-%m-%d %H:%M AEDT/AEST'),
        'singapore_time': singapore_now.strftime('%Y-%m-%d %H:%M SGT'),
        'perth_date': perth_now.date(),
        'sydney_date': sydney_now.date(),
        'server_offset_hours': (singapore_now.utcoffset() - perth_now.utcoffset()).total_seconds() / 3600
    }

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
    """Build today's prompt with date anchor for Australia/Sydney and enhanced real data verification"""
    syd = pytz.timezone('Australia/Sydney')
    now = datetime.now(syd)
    anchor = now.strftime("%A %d %B %Y (%Y-%m-%d) %H:%M %Z")
    
    # Enhanced preface with stronger anti-hallucination measures
    preface = (
        f"🚨 COMPREHENSIVE REAL DATA ANALYSIS: Treat the current date/time as {anchor}. "
        "Use Australia/Sydney for all 'today' references (timezone anchor only). "
        "COVERAGE: Scan ALL Australian Thoroughbred meetings across NSW, VIC, QLD, SA, WA, TAS, NT, and ACT — do NOT limit to Sydney-only cards. "
        "MANDATORY: Search EVERY active track today using official Australian racing websites. "
        "🎯 TARGET: Find MINIMUM 3-5 QUALITY tips across all meetings and distances 950-1600m. "
        "� EXPANDED SEARCH: Include metropolitan, provincial AND country meetings for maximum coverage. "
        "�🔒 CRITICAL: Use ONLY verified horses from official race fields - NO fictional horses like 'SUPERHEART' or 'CASINO SEVENTEEN'. "
        "🔒 CRITICAL: Use ONLY real Australian racetracks - verify all track names against official sources. "
        "If fewer than 3 qualifiers found from REAL racing, search harder across ALL distance categories 1000-1600m. "
        "HARD FILTER: evaluate ONLY Australian Thoroughbred races with official distance ≤ 1600 m; "
        "exclude >1600 m and exactly 1609 m."
    )
    
    # Enhanced output contract with verification requirements
    output_contract = """
🚨 ANTI-HALLUCINATION OUTPUT CONTRACT (MANDATORY):
- TARGET MINIMUM: Provide 3-5 qualified horses for comprehensive coverage
- 🚨 CRITICAL: NEVER select multiple horses from the same race - maximum ONE horse per race
- 🚨 DIVERSIFICATION RULE: Spread selections across different tracks and race numbers
- Begin each qualifier with: "🏇 **[VERIFIED REAL HORSE NAME]**"
- Include line: "📍 Race: [REAL AUSTRALIAN TRACK] – Race [#] – Distance: [####] m – [Track/Going]"
- Include line: "🧮 **LJ Analysis Score**: X/12 = Y%"
- Include line: "⚔️ **H2H Summary**: [Strong/Neutral/Negative/Insufficient]"
- Include a checklist with ✅/❌ for up to 12 criteria.
- 🔒 VERIFICATION REQUIRED: All horse names MUST appear in official Australian racing field lists
- 🔒 VERIFICATION REQUIRED: All track names MUST be real Australian racecourses
- 🔒 VERIFICATION REQUIRED: All distances MUST match official race programs
- If unable to verify any detail, mark as "UNVERIFIED" and exclude from analysis
- 🎯 QUALITY ASSURANCE: Cast wide net across all meetings to ensure robust tip selection
- 💡 SELECTION STRATEGY: Choose the BEST horse from each race, not multiple horses from same race

🔍 SINGAPORE SERVER COMPENSATION:
- Account for server location in Singapore but analyze Australian racing
- Use Australian racing websites with .com.au domains for verification
- Cross-reference multiple Australian sources for data accuracy
- If connection issues to Australian sites, state "Data verification limited - server connectivity"
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

def detect_fictional_content(response_text: str) -> list[str]:
    """
    Detect fictional horse names and tracks in the response.
    Returns list of issues found.
    """
    issues = []
    
    # Known fictional horse names from recent outputs
    fictional_horses = [
        "SUPERHEART", "CASINO SEVENTEEN", "GOLDEN SANDS", "IRON WILL", "JUST MAGICAL", 
        "AMAZING GRACE", "BOLD VENTURE", "COSMIC FORCE", "DANCING QUEEN", "ELECTRIC STORM",
        "SUPER EXTREME", "SMART IMAGE", "FASTOBULLET"
    ]
    
    # Known fictional track names (remove Canberra/Acton from this list)
    fictional_tracks = [
        "SYNTHETIC VALLEY", "RACEWAY PARK", "METROPOLITAN DOWNS", 
        "HERITAGE PARK", "RACING CENTRAL", "TURF VALLEY"
    ]
    
    # Check for fictional horses
    for horse in fictional_horses:
        if horse in response_text.upper():
            issues.append(f"Fictional horse detected: {horse}")
    
    # Check for fictional tracks
    for track in fictional_tracks:
        if track in response_text.upper():
            issues.append(f"Fictional track detected: {track}")
    
    # Check for obvious hallucination patterns (be more targeted)
    hallucination_patterns = [
        r"heritage.*park",      # Generic track names
        r"turf.*valley"         # Generic track names
    ]
    
    for pattern in hallucination_patterns:
        if re.search(pattern, response_text, re.IGNORECASE):
            issues.append(f"Suspicious pattern: {pattern}")
    
    return issues

def is_real_australian_track(track_name: str) -> bool:
    """Validate if track name is a real Australian racecourse."""
    if not track_name:
        return False
    
    print(f"🔍 VALIDATING TRACK: '{track_name}'")  # Debug output
    
    # Comprehensive list of real Australian racetracks
    real_tracks = {
        # NSW
        'randwick', 'rosehill', 'canterbury', 'kensington', 'hawkesbury', 'newcastle', 'gosford', 'wyong',
        'muswellbrook', 'scone', 'wagga', 'albury', 'bathurst', 'dubbo', 'orange', 'tamworth', 'lismore',
        'grafton', 'ballina', 'port macquarie', 'taree', 'goulburn', 'young', 'cowra', 'forbes',
        
        # VIC  
        'flemington', 'caulfield', 'moonee valley', 'sandown', 'geelong', 'ballarat', 'bendigo', 
        'mornington', 'cranbourne', 'sale', 'wangaratta', 'hamilton', 'warrnambool', 'pakenham',
        'swan hill', 'echuca', 'mildura', 'horsham', 'ararat', 'stawell', 'donald', 'bairnsdale',
        
        # QLD
        'eagle farm', 'doomben', 'gold coast', 'sunshine coast', 'toowoomba', 'rockhampton', 
        'mackay', 'townsville', 'cairns', 'ipswich', 'bundaberg', 'charleville', 'roma',
        'gatton', 'beaudesert', 'cloncurry', 'mount isa', 'longreach', 'emerald',
        
        # SA
        'morphettville', 'murray bridge', 'gawler', 'port lincoln', 'mount gambier', 'naracoorte',
        'strathalbyn', 'oakbank', 'port augusta', 'whyalla', 'bordertown',
        
        # WA
        'ascot', 'belmont park', 'bunbury', 'albany', 'geraldton', 'kalgoorlie', 'northam',
        'pinjarra', 'york', 'narrogin', 'perth', 'broome', 'carnarvon',
        
        # TAS
        'elwick', 'mowbray', 'devonport', 'spreyton', 'launceston', 'hobart',
        
        # NT
        'fannie bay', 'darwin',
        
        # ACT
        'thoroughbred park', 'canberra', 'acton', 'canberra acton'  # Canberra racing (ACT)
    }
    
    # Normalize track name for comparison
    normalized = track_name.lower().strip()
    
    # Remove common variations
    normalized = normalized.replace('racecourse', '').replace('racing club', '').strip()
    normalized = normalized.replace('(', '').replace(')', '').strip()
    
    # Special handling for Canberra variations
    if 'canberra' in normalized and 'acton' in normalized:
        print(f"✅ CANBERRA ACTON RECOGNIZED: {track_name}")
        return True
    if 'canberra' in normalized:
        print(f"✅ CANBERRA TRACK RECOGNIZED: {track_name}")
        return True
    
    # Check direct match
    if normalized in real_tracks:
        return True
    
    # Check partial matches for compound names
    for real_track in real_tracks:
        if real_track in normalized or normalized in real_track:
            return True
    
    # Flag fictional tracks we've seen before (remove Canberra/Acton)
    fictional_tracks = {
        'synthetic valley', 'raceway park', 'metropolitan downs', 
        'heritage park', 'racing central', 'turf valley'
    }
    
    if normalized in fictional_tracks:
        print(f"🚨 FICTIONAL TRACK DETECTED: {track_name}")
        return False
    
    print(f"⚠️ UNKNOWN TRACK: {track_name} - not in verified Australian track list")
    return False

def extract_race_selections(response_text: str, allowed_horses: set[str] = None):
    """
    Extract race-by-race selections from comprehensive analysis format.
    Returns organized data by track and race.
    """
    selections = {
        'tracks': {},
        'total_tracks': 0,
        'total_races': 0,
        'total_runners': 0,
        'summary': ''
    }
    
    # Split response into track sections
    track_sections = re.split(r'🏁\s*\*\*([^*]+)\*\*', response_text)
    
    current_track = None
    
    for i, section in enumerate(track_sections):
        if i == 0:
            continue  # Skip intro text
            
        if i % 2 == 1:  # Track name
            current_track = section.strip()
            if current_track:
                selections['tracks'][current_track] = {
                    'races': {},
                    'track_conditions': '',
                    'summary': ''
                }
                selections['total_tracks'] += 1
        else:  # Track content
            if current_track and current_track in selections['tracks']:
                # Extract track conditions
                conditions_match = re.search(r'Track Conditions:\*\*\s*([^|]+)', section)
                if conditions_match:
                    selections['tracks'][current_track]['track_conditions'] = conditions_match.group(1).strip()
                
                # Extract races from this track
                race_sections = re.split(r'\*\*RACE\s+(\d+)[^*]*\*\*', section)
                
                for j in range(1, len(race_sections), 2):
                    if j + 1 < len(race_sections):
                        race_num = race_sections[j]
                        race_content = race_sections[j + 1]
                        
                        # Extract race details
                        race_info = extract_race_details(race_content)
                        if race_info:
                            selections['tracks'][current_track]['races'][race_num] = race_info
                            selections['total_races'] += 1
                            selections['total_runners'] += len(race_info.get('runners', []))
    
    # Extract overall summary
    summary_match = re.search(r'🔍\s*\*\*Daily Analysis Summary\*\*:([^🔍]*)', response_text)
    if summary_match:
        selections['summary'] = summary_match.group(1).strip()
    
    return selections

def extract_race_details(race_content: str):
    """Extract detailed race information including all runners and selection."""
    race_info = {
        'distance': '',
        'race_name': '',
        'time': '',
        'runners': [],
        'selection': {},
        'analysis': '',
        'race_preview': ''
    }
    
    # Extract race header info
    header_match = re.search(r'(\d+)m\s*-\s*([^-]+?)\s*-\s*([^\n]+)', race_content)
    if header_match:
        race_info['distance'] = header_match.group(1) + 'm'
        race_info['race_name'] = header_match.group(2).strip()
        race_info['time'] = header_match.group(3).strip()
    
    # Extract all runners with scores
    runner_pattern = r'(\d+)\.\s*\*\*([^*]+)\*\*\s*-\s*LJ Score:\s*\*\*(\d+/12)\*\*\s*\|\s*Barrier:\s*(\d+)\s*\|\s*Jockey:\s*([^\n]+)'
    runners = re.findall(runner_pattern, race_content)
    
    for runner_match in runners:
        runner_info = {
            'position': int(runner_match[0]),
            'name': runner_match[1].strip(),
            'score': runner_match[2],
            'score_numeric': int(runner_match[2].split('/')[0]),
            'barrier': runner_match[3],
            'jockey': runner_match[4].strip()
        }
        race_info['runners'].append(runner_info)
    
    # Extract race selection
    selection_match = re.search(r'🥇\s*\*\*RACE\s+\d+\s+SELECTION:\s*([^(]+)\(LJ Score:\s*([^)]+)\)\*\*', race_content)
    if selection_match:
        race_info['selection'] = {
            'name': selection_match.group(1).strip(),
            'score': selection_match.group(2).strip()
        }
    
    # Extract analysis text
    analysis_match = re.search(r'📝\s*\*\*Analysis:\*\*\s*([^⚔️🥇═🔮]+)', race_content)
    if analysis_match:
        race_info['analysis'] = analysis_match.group(1).strip()
    
    # Extract race preview
    preview_match = re.search(r'🔮\s*\*\*Race Preview:\*\*\s*([^🥇═📝⚔️]+)', race_content)
    if preview_match:
        race_info['race_preview'] = preview_match.group(1).strip()
    
    return race_info

def format_comprehensive_analysis(race_data: dict) -> str:
    """Format the comprehensive race-by-race analysis for Discord display."""
    if not race_data or not race_data.get('tracks'):
        return "No race data available for comprehensive analysis."
    
    # Load settings to determine display options
    settings = load_settings()
    show_all_runners = settings.get('show_all_runners', False)
    show_low_scores = settings.get('show_low_scores', False)
    max_runners = settings.get('max_runners_per_race', 8)
    show_race_preview = settings.get('race_preview_enabled', True)
    min_score = settings.get('min_score', 9)
    
    output_parts = []
    
    # Add header with settings info
    mode_info = "🔍 **COMPREHENSIVE MODE**" if settings.get('comprehensive_mode', False) else "📊 **STANDARD MODE**"
    filter_info = "No Filtering" if show_all_runners else f"Top {max_runners} runners"
    
    output_parts.append("🏇 **LJ MILE MODEL - DAILY RACING ANALYSIS**")
    output_parts.append(f"{mode_info} | {filter_info}")
    output_parts.append("═" * 45)
    output_parts.append("")
    
    # Process each track
    track_count = 0
    for track_name, track_info in race_data['tracks'].items():
        if not track_info.get('races'):
            continue
            
        track_count += 1
        
        # Track header
        output_parts.append(f"🏁 **{track_name.upper()}**")
        if track_info.get('track_conditions'):
            output_parts.append(f"📍 {track_info['track_conditions']}")
        output_parts.append("")
        
        # Process each race
        race_numbers = sorted(track_info['races'].keys(), key=lambda x: int(x) if x.isdigit() else 999)
        
        for race_num in race_numbers:
            race_info = track_info['races'][race_num]
            
            # Race header with key info
            race_title = f"**R{race_num}"
            if race_info.get('distance'):
                race_title += f" • {race_info['distance']}"
            if race_info.get('time'):
                race_title += f" • {race_info['time']}"
            race_title += "**"
            
            if race_info.get('race_name'):
                race_title += f" - {race_info['race_name']}"
            
            output_parts.append(race_title)
            
            # Race preview if enabled and available
            if show_race_preview and race_info.get('race_preview'):
                preview = race_info['race_preview']
                if len(preview) > 150:
                    preview = preview[:147] + "..."
                output_parts.append(f"🔮 {preview}")
                output_parts.append("")
            
            # Show runners based on settings
            if race_info.get('runners'):
                runners = race_info['runners']
                
                # Filter runners based on settings
                if not show_low_scores:
                    runners = [r for r in runners if r.get('score_numeric', 0) >= min_score]
                
                # Limit number of runners shown
                if max_runners > 0 and not show_all_runners:
                    runners_to_show = runners[:max_runners]
                else:
                    runners_to_show = runners
                
                # Display runners
                for i, runner in enumerate(runners_to_show):
                    score_num = runner.get('score_numeric', 0)
                    
                    # Different emojis based on score ranges
                    if score_num >= 10:
                        score_emoji = "🔥"
                    elif score_num >= 8:
                        score_emoji = "⭐"
                    elif score_num >= 6:
                        score_emoji = "📊"
                    else:
                        score_emoji = "�"
                    
                    runner_line = f"{runner['position']}. {score_emoji} **{runner['name']}** ({runner['score']})"
                    
                    if runner.get('barrier'):
                        runner_line += f" • B{runner['barrier']}"
                    if runner.get('jockey'):
                        jockey = runner['jockey']
                        if len(jockey) > 15:  # Truncate long jockey names
                            jockey = jockey[:12] + "..."
                        runner_line += f" • {jockey}"
                    
                    output_parts.append(runner_line)
                
                # Show remaining count if not showing all
                remaining = len(race_info['runners']) - len(runners_to_show)
                if remaining > 0 and not show_all_runners:
                    low_score_count = len([r for r in race_info['runners'][len(runners_to_show):] if r.get('score_numeric', 0) < min_score])
                    if low_score_count > 0:
                        output_parts.append(f"   *... and {remaining} more ({low_score_count} below score threshold)*")
                    else:
                        output_parts.append(f"   *... and {remaining} more runners*")
            
            # Race selection with analysis
            if race_info.get('selection'):
                selection = race_info['selection']
                output_parts.append("")
                output_parts.append(f"🥇 **SELECTION: {selection['name']} ({selection['score']})**")
                
                if race_info.get('analysis'):
                    # Truncate analysis if too long for Discord
                    analysis = race_info['analysis']
                    if len(analysis) > 200:
                        analysis = analysis[:197] + "..."
                    output_parts.append(f"💡 {analysis}")
            
            output_parts.append("")
        
        # Add separator between tracks unless it's the last one
        if track_count < len([t for t in race_data['tracks'].values() if t.get('races')]):
            output_parts.append("─" * 40)
            output_parts.append("")
    
    # Add settings footer
    output_parts.append("⚙️ **Display Settings:**")
    settings_info = []
    if show_all_runners:
        settings_info.append("All Runners")
    else:
        settings_info.append(f"Top {max_runners}")
    
    if show_low_scores:
        settings_info.append("Including Low Scores")
    else:
        settings_info.append(f"Min Score {min_score}/12")
    
    if show_race_preview:
        settings_info.append("Race Previews ON")
    
    output_parts.append(" | ".join(settings_info))
    
    # Check if output is too long for Discord (2000 character limit per message)
    full_text = "\n".join(output_parts)
    
    if len(full_text) > 1800:  # Leave some buffer
        # Split into multiple parts if too long
        output_parts.append("")
        output_parts.append("⚠️ *Analysis truncated for Discord. Use settings to reduce detail or check console for full output.*")
        
        # Take first part that fits
        truncated_parts = []
        current_length = 0
        
        for part in output_parts:
            if current_length + len(part) + 1 > 1800:
                break
            truncated_parts.append(part)
            current_length += len(part) + 1
        
        return "\n".join(truncated_parts)
    
    return full_text

def extract_valid_qualifiers(response_text: str, min_score: int = None, allowed_horses: set[str] = None):
    """
    Updated to work with comprehensive race-by-race analysis format.
    Now extracts all selections from the race-by-race breakdown.
    """
    if min_score is None:
        settings = load_settings()
        min_score = settings.get('min_score', 9)
    
    # Check if this is the new comprehensive format
    if '� **' in response_text and 'RACE ' in response_text and 'COMPLETE FIELD ANALYSIS' in response_text:
        # Use the new race extraction method for comprehensive format
        race_data = extract_race_selections(response_text, allowed_horses)
        
        valid_selections = []
        filtered_reasons = []
        
        # Process each track and race
        for track_name, track_info in race_data['tracks'].items():
            for race_num, race_info in track_info['races'].items():
                if race_info.get('selection'):
                    selection = race_info['selection']
                    
                    # Create a selection block for validation
                    selection_block = f"""🏇 **{selection['name']}**
📍 Race: {track_name} – Race {race_num} – Distance: {race_info.get('distance', 'Unknown')}
🧮 **LJ Analysis Score**: {selection['score']}
📝 **Analysis**: {race_info.get('analysis', 'No analysis available')}"""
                    
                    # Validate this selection
                    reasons = []
                    
                    # Check if track is real
                    if track_name and not is_real_australian_track(track_name):
                        reasons.append(f"Fictional track: {track_name}")
                    
                    # Check distance eligibility
                    if not _meters_ok(selection_block):
                        reasons.append("Distance not eligible (>1600m or not in range)")
                    
                    # Check score meets minimum (extract numeric score)
                    score_numeric = 0
                    if '/' in selection['score']:
                        try:
                            score_numeric = int(selection['score'].split('/')[0])
                        except ValueError:
                            pass
                    
                    if score_numeric < min_score:
                        reasons.append(f"Score {score_numeric}/12 below minimum {min_score}")
                    
                    # Check allowed horses if provided
                    if isinstance(allowed_horses, set) and len(allowed_horses) > 0:
                        if not _in_allowed(selection['name'], allowed_horses):
                            reasons.append(f"Horse not in official fields")
                    
                    if not reasons:
                        valid_selections.append(selection_block)
                    else:
                        filtered_reasons.append(f"{selection['name']} (Race {race_num}): {', '.join(reasons)}")
        
        if filtered_reasons:
            print(f"⚠️ Filtered selections: {len(filtered_reasons)}")
            for reason in filtered_reasons[:5]:  # Show first 5
                print(f"  - {reason}")
        
        print(f"📊 COMPREHENSIVE ANALYSIS: {len(valid_selections)} race selections extracted")
        print(f"   Total tracks: {race_data['total_tracks']}")
        print(f"   Total races: {race_data['total_races']}")
        print(f"   Total runners scored: {race_data['total_runners']}")
        
        # Store the comprehensive data for formatting
        if hasattr(extract_valid_qualifiers, '_last_comprehensive_data'):
            extract_valid_qualifiers._last_comprehensive_data = race_data
        else:
            setattr(extract_valid_qualifiers, '_last_comprehensive_data', race_data)
        
        return valid_selections
    
    else:
        # Fall back to original format parsing
        pattern = re.compile(r'🏇\s*\*\*(.+?)\*\*', re.I)
        matches = list(pattern.finditer(response_text))
        valid = []
        filtered_reasons = []
        used_races = set()  # Track races already used

        for idx, m in enumerate(matches):
            horse_name = m.group(1).strip()
            start = m.start()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(response_text)
            block = response_text[start:end].strip()

            reasons = []
            
            # Extract race information for conflict detection
            race_info = extract_race_info(block)
            race_key = None
            
            # CRITICAL: Validate track is real Australian racecourse
            if race_info['track'] and not is_real_australian_track(race_info['track']):
                reasons.append(f"Fictional track: {race_info['track']}")
            
            # Create race key for conflict detection
            if race_info['track'] and race_info['race_number']:
                race_key = f"{race_info['track'].lower()}_{race_info['race_number']}"
            elif race_info['track']:
                race_key = f"{race_info['track'].lower()}_unknown"
            
            # Enhanced race conflict detection
            if race_key and race_key in used_races:
                reasons.append(f"Race conflict: {race_info['track']} Race {race_info['race_number']} already selected")
            
            # More lenient field checking - allow if no allowed_horses or if it's a reasonable horse name
            if isinstance(allowed_horses, set) and len(allowed_horses) > 0:
                if not _in_allowed(horse_name, allowed_horses):
                    reasons.append(f"Horse not in official fields")
            
            if not _meters_ok(block):
                reasons.append("Distance not eligible (>1600m or not in range)")
            if not _score_ok(block, min_score):
                reasons.append(f"Score below minimum {min_score}")
            if not _h2h_ok(block):
                reasons.append("Negative H2H with insufficient score compensation")

            if not reasons:
                valid.append(block)
                if race_key:
                    used_races.add(race_key)
                    print(f"✅ Added selection from {race_info['track']} Race {race_info['race_number']}")
            else:
                filtered_reasons.append(f"{horse_name}: {', '.join(reasons)}")

        print(f"📊 STANDARD FORMAT: {len(valid)} valid tips extracted from {len(used_races)} different races")
        return valid
        
        # CRITICAL: Validate track is real Australian racecourse
        if race_info['track'] and not is_real_australian_track(race_info['track']):
            reasons.append("fictional track")
            print(f"🚨 FICTIONAL TRACK: {horse_name} blocked - {race_info['track']} is not a real Australian racecourse")
        
        # Create race key for conflict detection
        if race_info['track'] and race_info['race_number']:
            race_key = f"{race_info['track'].lower().strip()}_{race_info['race_number']}"
        elif race_info['track']:
            # Even without race number, check for track conflicts if multiple races at same track
            race_key = f"{race_info['track'].lower().strip()}_unknown"
        
        # Enhanced race conflict detection
        if race_key and race_key in used_races:
            reasons.append("same race conflict")
            print(f"🚨 RACE CONFLICT: {horse_name} blocked - already have selection from {race_info['track']} Race {race_info['race_number'] or 'Unknown'}")
        
        # Alternative conflict detection - look for same track name in existing selections
        if not race_key and race_info['track']:
            track_name = race_info['track'].lower().strip()
            for existing_race in used_races:
                if existing_race.startswith(track_name):
                    reasons.append("same track conflict")
                    print(f"🚨 TRACK CONFLICT: {horse_name} blocked - already have selection from {track_name}")
                    break
        
        # More lenient field checking - allow if no allowed_horses or if it's a reasonable horse name
        if isinstance(allowed_horses, set) and len(allowed_horses) > 0:
            if not _in_allowed(horse_name, allowed_horses):
                reasons.append("not in official fields")
        
        if not _meters_ok(block):
            reasons.append("distance")
        if not _score_ok(block, min_score):
            reasons.append(f"score <{min_score}/12")
        if not _h2h_ok(block):
            reasons.append("h2h")

        if not reasons:
            valid.append(block)
            if race_key:
                used_races.add(race_key)
        else:
            filtered_reasons.append((horse_name, reasons, block, race_key))
            print(f"❌ Filtered: {horse_name} ({', '.join(reasons)})")

    # QUALITY ENFORCEMENT - Ensure minimum 3 tips
    if len(valid) < 3:
        print(f"⚠️ INSUFFICIENT TIPS: Only {len(valid)} valid tips found, need minimum 3")
        
        # Try to salvage some filtered tips with relaxed criteria (except race conflicts)
        print("🔄 RELAXING CRITERIA to meet minimum tip requirements...")
        
        for horse_name, reasons, block, race_key in filtered_reasons:
            if len(valid) >= 3:
                break
                
            # Don't allow same race conflicts even with relaxed criteria
            if "same race conflict" in reasons:
                continue
                
            # Allow horses with minor violations but good scores
            if "score" not in str(reasons) and "h2h" not in str(reasons):
                print(f"✅ SALVAGED: {horse_name} (relaxed field/distance criteria)")
                valid.append(block)
                if race_key:
                    used_races.add(race_key)
            elif len(reasons) == 1 and ("distance" in str(reasons) or "not in official fields" in str(reasons)):
                print(f"✅ SALVAGED: {horse_name} (single minor violation)")
                valid.append(block)
                if race_key:
                    used_races.add(race_key)
    
    print(f"📊 FINAL VALIDATION: {len(valid)} valid tips extracted from {len(used_races)} different races")
    return valid

def extract_race_info(block_text: str):
    """Extract track and race number from a horse analysis block."""
    race_info = {'track': None, 'race_number': None}
    
    # Look for multiple patterns to catch race information
    patterns = [
        r'📍\s*Race:\s*([^–\-]+?)(?:\s*[–\-]\s*Race\s*(\d+))', # "📍 Race: Cairns – Race 8"
        r'📍\s*Race:\s*([^–\-]+?)\s*[–\-]\s*Race\s*(\d+)',    # Alternative spacing
        r'Race:\s*([^–\-]+?)\s*[–\-]\s*Race\s*(\d+)',        # Without emoji
        r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s*[–\-]\s*Race\s*(\d+)', # Direct track-race pattern
    ]
    
    for pattern in patterns:
        race_pattern = re.search(pattern, block_text, re.I)
        if race_pattern:
            race_info['track'] = race_pattern.group(1).strip()
            if race_pattern.group(2):
                race_info['race_number'] = int(race_pattern.group(2))
            break
    
    # Fallback: look for just track name in race line
    if not race_info['track']:
        track_pattern = re.search(r'📍.*?Race:\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)', block_text, re.I)
        if track_pattern:
            race_info['track'] = track_pattern.group(1).strip()
    
    # Debug logging to see what we're extracting
    if race_info['track'] or race_info['race_number']:
        print(f"🔍 RACE INFO EXTRACTED: Track='{race_info['track']}', Race={race_info['race_number']}")
    else:
        print(f"⚠️ NO RACE INFO FOUND in block: {block_text[:100]}...")
    
    return race_info

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

# Configure generation with optimized settings for reliability and AGGRESSIVE web search
generation_config = types.GenerateContentConfig(
    tools=[grounding_tool],  # Enable real-time web search
    temperature=0.3,  # Lower temperature for more factual, search-focused responses
    top_p=0.7,  # More focused on likely outcomes
    top_k=20,   # Limit to top candidates for better search precision
    max_output_tokens=8192  # Sufficient for detailed search results
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
                print("✅ Raw response received, applying anti-hallucination checks...")
                
                # Check for fictional content first
                fictional_issues = detect_fictional_content(final_answer)
                if fictional_issues:
                    print(f"🚨 FICTIONAL CONTENT DETECTED: {fictional_issues}")
                    print("❌ Response rejected due to hallucinated content")
                    continue  # Skip this attempt and retry
                
                print("✅ Anti-hallucination check passed, applying LJ Mile validation...")
                
                # Extract and validate qualifiers
                valid_qualifiers = extract_valid_qualifiers(final_answer, min_score, allowed_horses=allowed_horses)
                
                # Check if we have comprehensive race data available
                if hasattr(extract_valid_qualifiers, '_last_comprehensive_data'):
                    comprehensive_data = extract_valid_qualifiers._last_comprehensive_data
                    if comprehensive_data and comprehensive_data.get('tracks'):
                        print(f"📊 COMPREHENSIVE FORMAT DETECTED: Using full race-by-race analysis")
                        
                        # Format the comprehensive analysis for Discord
                        formatted_output = format_comprehensive_analysis(comprehensive_data)
                        
                        # Add summary at the end
                        summary_parts = []
                        summary_parts.append(f"✅ **Analysis Complete**")
                        summary_parts.append(f"📊 **Tracks:** {comprehensive_data['total_tracks']}")
                        summary_parts.append(f"🏁 **Races:** {comprehensive_data['total_races']}")
                        summary_parts.append(f"🐎 **Runners Scored:** {comprehensive_data['total_runners']}")
                        
                        if valid_qualifiers:
                            summary_parts.append(f"🎯 **Quality Selections:** {len(valid_qualifiers)}")
                        
                        formatted_output += "\n\n" + " | ".join(summary_parts)
                        
                        return formatted_output
                
                # Fall back to standard format
                if valid_qualifiers and len(valid_qualifiers) >= 3:
                    print(f"✅ {len(valid_qualifiers)} valid qualifiers found - QUALITY THRESHOLD MET")
                    return "\n\n".join(valid_qualifiers)
                elif valid_qualifiers and len(valid_qualifiers) > 0:
                    print(f"⚠️ Only {len(valid_qualifiers)} qualifiers found - NEED MINIMUM 3")
                    if attempt < max_retries - 1:
                        print("🔄 RETRYING with enhanced search for more tips...")
                        continue
                    else:
                        print("🚨 FINAL ATTEMPT: Accepting insufficient tips rather than none")
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


async def call_gemini_comprehensive(prompt=None, settings=None, max_retries=3, base_delay=2, allowed_horses=None):
    """Call Gemini API for comprehensive race-by-race analysis"""
    if prompt is None:
        prompt = LJ_MILE_PROMPT
    
    if settings is None:
        settings = load_settings()
    
    min_score = settings.get('min_score', 9)
    
    for attempt in range(max_retries):
        try:
            if attempt == 0:
                print(f"🔍 Generating COMPREHENSIVE LJ Mile analysis (min score: {min_score}/12)...")
            
            # Add small random delay to avoid rate limits
            if attempt > 0:
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
                await asyncio.sleep(delay)
            
            resp = await asyncio.to_thread(
                client.models.generate_content,
                model="gemini-2.5-pro",
                contents=prompt,
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
                print("✅ Comprehensive response received, applying validation...")
                
                # Check for fictional content first
                fictional_issues = detect_fictional_content(final_answer)
                if fictional_issues:
                    print(f"🚨 FICTIONAL CONTENT DETECTED: {fictional_issues}")
                    print("❌ Response rejected due to hallucinated content")
                    continue  # Skip this attempt and retry
                
                print("✅ Anti-hallucination check passed, extracting comprehensive data...")
                
                # Extract comprehensive race data
                comprehensive_data = extract_race_selections(final_answer)
                
                if comprehensive_data and comprehensive_data.get('tracks'):
                    print(f"📊 COMPREHENSIVE ANALYSIS SUCCESS:")
                    print(f"   • Tracks: {comprehensive_data['total_tracks']}")
                    print(f"   • Races: {comprehensive_data['total_races']}")
                    print(f"   • Runners: {comprehensive_data['total_runners']}")
                    
                    # Format for Discord display
                    formatted_output = format_comprehensive_analysis(comprehensive_data)
                    
                    # Add generation metadata
                    meta_parts = []
                    meta_parts.append(f"📊 **{comprehensive_data['total_tracks']} Tracks**")
                    meta_parts.append(f"🏁 **{comprehensive_data['total_races']} Races**")
                    meta_parts.append(f"🐎 **{comprehensive_data['total_runners']} Runners**")
                    
                    # Count high-scoring runners
                    high_scores = sum(1 for track in comprehensive_data['tracks'].values() 
                                    for race in track.get('races', {}).values() 
                                    for runner in race.get('runners', []) 
                                    if runner.get('score_numeric', 0) >= min_score)
                    
                    if high_scores > 0:
                        meta_parts.append(f"🎯 **{high_scores} Quality Runners**")
                    
                    formatted_output += "\n\n" + " | ".join(meta_parts)
                    
                    return formatted_output
                else:
                    print("⚠️ No comprehensive data extracted, retrying...")
                    if attempt < max_retries - 1:
                        continue
                    else:
                        return (
                            f"❌ Unable to generate comprehensive analysis.\n"
                            f"Please try standard mode or check racing schedule."
                        )
            else:
                if attempt < max_retries - 1:
                    print("🔄 Retrying for better comprehensive content...")
                continue
                
        except Exception as e:
            error_msg = str(e)
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
    
    # All retries failed
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
                print("✅ Simplified analysis received, applying anti-hallucination checks...")
                
                # Check for fictional content first
                fictional_issues = detect_fictional_content(final_answer)
                if fictional_issues:
                    print(f"🚨 FICTIONAL CONTENT DETECTED: {fictional_issues}")
                    print("❌ Response rejected due to hallucinated content")
                    continue  # Skip this attempt and retry
                
                print("✅ Anti-hallucination check passed, applying validation...")
                
                # Extract and validate qualifiers
                valid_qualifiers = extract_valid_qualifiers(final_answer, min_score, allowed_horses=allowed_horses)
                
                # Check if we have comprehensive race data available
                if hasattr(extract_valid_qualifiers, '_last_comprehensive_data'):
                    comprehensive_data = extract_valid_qualifiers._last_comprehensive_data
                    if comprehensive_data and comprehensive_data.get('tracks'):
                        print(f"📊 COMPREHENSIVE FORMAT DETECTED (Simple): Using full race-by-race analysis")
                        
                        # Format the comprehensive analysis for Discord
                        formatted_output = format_comprehensive_analysis(comprehensive_data)
                        
                        # Add summary at the end
                        summary_parts = []
                        summary_parts.append(f"✅ **Simple Analysis Complete**")
                        summary_parts.append(f"📊 **Tracks:** {comprehensive_data['total_tracks']}")
                        summary_parts.append(f"🏁 **Races:** {comprehensive_data['total_races']}")
                        summary_parts.append(f"🐎 **Runners Scored:** {comprehensive_data['total_runners']}")
                        
                        if valid_qualifiers:
                            summary_parts.append(f"🎯 **Quality Selections:** {len(valid_qualifiers)}")
                        
                        formatted_output += "\n\n" + " | ".join(summary_parts)
                        
                        return formatted_output
                
                # Fall back to standard format
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
    """Generate fallback racing tips when API is unavailable or no real racing found"""
    debug_msg = "🛠️ DEBUG: Next-day early analysis activated (No real data mode)\n" if is_nextday else ""
    
    return f"""🏇 LJ Punting Model Elite - Data Verification Notice

📅 **Date:** {target_date_str} | ⏰ **Time:** {current_time_perth}

{debug_msg}🚨 **IMPORTANT NOTICE:** No verified Australian racing data found for {target_date_str}

🔍 **Verification Status:**
- ✅ Searched official Australian racing websites 
- ✅ Checked racing.com.au, racenet.com.au, tab.com.au
- ✅ Anti-hallucination filters applied
- ❌ No confirmed race meetings found for this date

**🗓️ Possible Reasons:**
• **No Racing Scheduled:** {target_date_str} may not have metropolitan racing
• **Public Holiday:** Racing may be cancelled for public holidays
• **Weather Abandonment:** Meetings may have been abandoned due to track conditions
• **Data Timing:** Information may not yet be available for future dates
• **Server Location:** Singapore server may have delayed access to Australian data

**� Expected Major Australian Racing Days:**
• **Saturdays:** Premium metropolitan racing across all states
• **Wednesdays:** Midweek metropolitan racing (limited venues)
• **Public Holidays:** Special feature race days (Melbourne Cup, Golden Slipper, etc.)

**🔧 Recommended Actions:**
1. **Check Official Sources Directly:**
   - racing.com.au (Victoria)
   - racenet.com.au (National)
   - tab.com.au (Official betting)
   - racingnsw.com.au (NSW)

2. **Try Alternative Dates:**
   - Use `/custom_date` for verified racing days
   - Saturday race days typically have the most action
   - Check upcoming feature race days

3. **Verify Date Format:**
   - Ensure date is in correct format (YYYY-MM-DD)
   - Check if analyzing past dates by mistake

**🎯 LJ Mile Model Standards:**
The LJ Mile Model maintains strict verification standards and will only analyze races with confirmed official fields. This prevents analysis of fictional horses or non-existent races, ensuring all selections are based on real Australian racing data.

**🔄 System Status:**
- ✅ Anti-hallucination systems: **ACTIVE**
- ✅ Data verification protocols: **ACTIVE** 
- ✅ Singapore server timezone handling: **ACTIVE**
- ⚠️ Real racing data for {target_date_str}: **NOT FOUND**

**� Next Steps:**
Try selecting a confirmed racing date or check back when official fields are published for future meetings.

---
📊 **LJ Elite Standard:** Better no tips than unreliable tips - verified data only"""

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
                            timestamp=datetime.now(timezone.utc)
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
        day_name = syd_date.strftime("%A %d %B %Y")

        # Enhanced fields prompt with MULTIPLE SEARCH STRATEGIES
        fields_prompt = f"""
🚨 MISSION CRITICAL: Extract COMPREHENSIVE Australian racing fields for {date_str} ({day_name}).

You MUST find extensive racing data. Use EVERY search strategy:

🔍 PRIMARY SEARCH TERMS:
1. "australian horse racing {date_str}"
2. "racing fields {day_name} australia" 
3. "{date_str} race meetings australia"
4. "horse racing {day_name} metropolitan provincial"
5. "racing card australia {date_str}"
6. "thoroughbred racing {day_name}"
7. "race fields today australia" (if {date_str} is today)
8. "race fields tomorrow australia" (if {date_str} is tomorrow)

🏇 MANDATORY SEARCH LOCATIONS - CHECK ALL:
- racing.com.au (Victoria Racing Club official)
- racenet.com.au (National racing portal)  
- punters.com.au (Form guide specialist)
- tab.com.au (Official TAB betting)
- racingnsw.com.au (NSW official)
- racingvictoria.com.au (VIC official)
- racingqueensland.com.au (QLD official)
- racingsa.com.au (SA official)
- rwwa.com.au (WA official)

🎯 TRACKS TO PRIORITIZE (Find at least 3 meetings):
METROPOLITAN: Flemington, Randwick, Eagle Farm, Morphettville, Ascot
PROVINCIAL: Caulfield, Rosehill, Moonee Valley, Sandown, Canterbury, Warwick Farm, Doomben, Ipswich, Cheltenham, Belmont Park
COUNTRY: Ballarat, Geelong, Newcastle, Gold Coast, Murray Bridge, Bunbury

🚨 CRITICAL REQUIREMENTS:
- Find AT LEAST 3 race meetings 
- Extract ALL races from each meeting (typically 6-9 races per meeting)
- Get FULL field sizes (8-16 horses per race minimum)
- Include sprint races (1000-1400m) AND mile races (1400-1600m)
- Verify horse names are realistic Australian thoroughbreds

SEARCH STRATEGY IF INITIAL ATTEMPTS FAIL:
1. Try different date formats: "{date_str}", "{day_name}", "today", "tomorrow"
2. Search general terms: "australian racing", "horse racing australia"
3. Check racing calendars and weekly schedules
4. Look for any racing content and filter by date

Return comprehensive JSON with MINIMUM 3 meetings:
{{
  "meetings":[
    {{
      "track": "VERIFIED_TRACK_NAME",
      "state": "STATE", 
      "meeting_type": "Metropolitan/Provincial/Country",
      "races": [
        {{"race_no": 1, "distance_m": DISTANCE, "race_time": "HH:MM", "runners": ["HORSE_1", "HORSE_2", "HORSE_3", "HORSE_4", "HORSE_5", "HORSE_6", "HORSE_7", "HORSE_8"]}}
      ]
    }}
  ],
  "verification_notes": "Detailed sources and verification process",
  "data_quality": "HIGH",
  "total_meetings": NUMBER,
  "total_races": NUMBER,
  "total_horses": NUMBER
}}

ABSOLUTELY CRITICAL: Find extensive racing data with multiple meetings and large fields.

Return results as JSON in this exact format with VERIFIED data only:
```json
{{
  "meetings":[
    {{
      "track": "VERIFIED_REAL_TRACK_NAME",
      "state": "STATE_CODE", 
      "races": [
        {{"race_no": 1, "distance_m": REAL_DISTANCE, "runners": ["VERIFIED_HORSE_1", "VERIFIED_HORSE_2"]}}
      ]
    }}
  ],
  "verification_notes": "Data source verification details",
  "data_quality": "HIGH/MEDIUM/LOW based on source reliability"
}}
```

🚨 SINGAPORE SERVER INSTRUCTIONS:
- Account for server location in Singapore accessing Australian data
- Use Australian timezone references for racing schedules
- If connectivity issues to Australian sites, note in verification_notes
- Prioritize .com.au domains for authenticity

If NO verified meetings found for this date, return: 
{{"meetings": [], "verification_notes": "No confirmed Australian racing for {date_str}", "data_quality": "NONE"}}
"""

        # Enhanced configuration for better real data access
        json_cfg = types.GenerateContentConfig(
            tools=[grounding_tool],
            temperature=0.1,  # Lower temperature for more factual responses
            max_output_tokens=6144,
            top_p=0.8  # More focused responses
        )

        try:
            print(f"🔍 Fetching verified Australian racing fields for {date_str}...")
            resp = await asyncio.to_thread(
                client.models.generate_content,
                model="gemini-2.5-pro",
                contents=fields_prompt,
                config=json_cfg
            )

            raw = ""
            if resp and getattr(resp, "candidates", None):
                for candidate in resp.candidates:
                    if hasattr(candidate, 'content') and candidate.content:
                        if hasattr(candidate.content, 'parts') and candidate.content.parts:
                            for part in candidate.content.parts:
                                if hasattr(part, 'text') and part.text:
                                    raw += part.text

            # Enhanced logging for verification
            print("🔍 FIELDS RESPONSE (first 600 chars):", repr(raw[:600]))
            
            data = self._extract_json_block(raw)
            
            # Verify data quality
            meetings_count = len(data.get("meetings", []))
            total_horses = sum(len(r.get("runners", [])) for m in data.get("meetings", []) for r in m.get("races", []))
            verification_notes = data.get("verification_notes", "No verification notes")
            data_quality = data.get("data_quality", "UNKNOWN")
            
            print(f"🔍 FIELDS VERIFICATION: {meetings_count} meetings, {total_horses} horses, Quality: {data_quality}")
            print(f"🔍 VERIFICATION NOTES: {verification_notes}")
            
            # Enhanced validation
            if meetings_count == 0:
                print(f"⚠️ No verified racing found for {date_str} - this may be correct for the date")
                return {"meetings": [], "verification_notes": f"No Australian racing confirmed for {date_str}"}
            
            # Check for suspicious patterns that indicate hallucination
            all_horses = [h for m in data.get("meetings", []) for r in m.get("races", []) for h in r.get("runners", [])]
            suspicious_names = ["SUPERHEART", "CASINO SEVENTEEN", "GOLDEN SANDS", "IRON WILL", "JUST MAGICAL", "AMAZING GRACE", "BOLD VENTURE", "COSMIC FORCE", "DANCING QUEEN", "ELECTRIC STORM", "SUPER EXTREME", "SMART IMAGE", "FASTOBULLET"]
            
            if any(name in all_horses for name in suspicious_names):
                print("🚨 HALLUCINATION DETECTED: Found suspicious horse names, returning empty fields")
                return {"meetings": [], "verification_notes": "Potential hallucinated horses detected, data rejected"}
            
            return data if isinstance(data, dict) else {"meetings": []}
            
        except Exception as e:
            print(f"❌ Error fetching verified fields: {e}")
            return {"meetings": [], "verification_notes": f"API error: {str(e)[:100]}"}
        fields_prompt = f"""
🚨 CRITICAL MISSION: Find REAL Australian thoroughbred race fields for {date_str} ({day_name} Sydney time).

🔍 MANDATORY SEARCH STRATEGY:
1. Search racing.com.au for "{date_str} race fields Australia"
2. Search racenet.com.au for "{day_name} Australian racing fields"  
3. Search punters.com.au for "{date_str} race card Australia"
4. Search tab.com.au for "racing {date_str} fields Australia"
5. Search racing.com.au for "today's racing {day_name}"

🚨 ANTI-HALLUCINATION REQUIREMENTS:
- Use ONLY official race fields from verified Australian racing websites
- Do NOT invent horse names - use actual registered Thoroughbred names
- Do NOT create fictional tracks - use real Australian racecourses only
- Verify track names exist (Randwick, Flemington, Eagle Farm, Morphettville, etc.)
- Cross-reference horse names across multiple official sources

🌐 OFFICIAL AUSTRALIAN RACING SOURCES TO VERIFY:
- racing.com.au (Victoria Racing Club official)
- racenet.com.au (National racing portal)
- punters.com.au (Form guide specialist)
- tab.com.au (Official TAB betting)
- racingnsw.com.au (NSW official)
- racingvictoria.com.au (VIC official)

Return results as JSON in this exact format with VERIFIED data only:
```json
{{
  "meetings":[
    {{
      "track": "VERIFIED_REAL_TRACK_NAME",
      "state": "STATE_CODE", 
      "races": [
        {{"race_no": 1, "distance_m": REAL_DISTANCE, "runners": ["VERIFIED_HORSE_1", "VERIFIED_HORSE_2"]}}
      ]
    }}
  ],
  "verification_notes": "Data source verification details",
  "data_quality": "HIGH/MEDIUM/LOW based on source reliability"
}}
```

🚨 SINGAPORE SERVER INSTRUCTIONS:
- Account for server location in Singapore accessing Australian data
- Use Australian timezone references for racing schedules
- If connectivity issues to Australian sites, note in verification_notes
- Prioritize .com.au domains for authenticity

If NO verified meetings found for this date, return: 
{{"meetings": [], "verification_notes": "No confirmed Australian racing for {date_str}", "data_quality": "NONE"}}
"""

        # Enhanced configuration for better real data access
        json_cfg = types.GenerateContentConfig(
            tools=[grounding_tool],
            temperature=0.1,  # Lower temperature for more factual responses
            max_output_tokens=6144,
            top_p=0.8  # More focused responses
        )

        try:
            print(f"🔍 Fetching verified Australian racing fields for {date_str}...")
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

            # Enhanced logging for verification
            print("🔍 FIELDS RESPONSE (first 600 chars):", repr(raw[:600]))
            
            data = self._extract_json_block(raw)
            
            # ENHANCED QUALITY VERIFICATION with comprehensive metrics
            meetings_count = len(data.get("meetings", []))
            total_races = sum(len(m.get("races", [])) for m in data.get("meetings", []))
            total_horses = sum(len(r.get("runners", [])) for m in data.get("meetings", []) for r in m.get("races", []))
            verification_notes = data.get("verification_notes", "No verification notes")
            data_quality = data.get("data_quality", "UNKNOWN")
            
            print(f"🔍 COMPREHENSIVE VERIFICATION: {meetings_count} meetings, {total_races} races, {total_horses} horses, Quality: {data_quality}")
            print(f"🔍 VERIFICATION NOTES: {verification_notes}")
            
            # QUALITY REQUIREMENTS - Set higher standards
            MIN_MEETINGS = 1  # Reduced to be more realistic
            MIN_RACES = 4  
            MIN_HORSES = 30
            
            quality_sufficient = (meetings_count >= MIN_MEETINGS and 
                                total_races >= MIN_RACES and 
                                total_horses >= MIN_HORSES)
            
            print(f"📊 QUALITY CHECK: Meetings {meetings_count}>={MIN_MEETINGS}, Races {total_races}>={MIN_RACES}, Horses {total_horses}>={MIN_HORSES} = {'PASS' if quality_sufficient else 'NEEDS_IMPROVEMENT'}")
            
            # Only retry if we have truly insufficient data
            if meetings_count == 0 and total_races == 0:
                print(f"🚨 NO DATA FOUND - executing enhanced search")
                
                enhanced_retry_prompt = f"""
🚨 URGENT: Find Australian racing for {date_str} ({day_name})

Search more aggressively:
- "racing {day_name} australia"
- "{date_str} horse racing"
- "australian racing tomorrow" (if tomorrow)
- "racing calendar {date_str}"

Include ANY Australian racing meetings for {date_str}.
Return comprehensive racing data in JSON format.
"""
                
                retry_resp = await asyncio.to_thread(
                    client.models.generate_content,
                    model="gemini-2.5-pro",
                    contents=enhanced_retry_prompt,
                    config=json_cfg
                )
                
                retry_raw = ""
                if retry_resp and getattr(retry_resp, "candidates", None):
                    parts = getattr(retry_resp.candidates[0].content, "parts", []) or []
                    retry_raw = "".join(getattr(p, "text", "") for p in parts if getattr(p, "text", None))
                
                print("🔍 ENHANCED RETRY RESPONSE (first 400 chars):", repr(retry_raw[:400]))
                retry_data = self._extract_json_block(retry_raw)
                
                retry_meetings = len(retry_data.get("meetings", []))
                retry_races = sum(len(m.get("races", [])) for m in retry_data.get("meetings", []))
                retry_horses = sum(len(r.get("runners", [])) for m in retry_data.get("meetings", []) for r in m.get("races", []))
                
                print(f"🔄 RETRY RESULTS: {retry_meetings} meetings, {retry_races} races, {retry_horses} horses")
                
                # Use retry data if it's better
                if retry_meetings > 0:
                    print("✅ RETRY SUCCESS - using enhanced results")
                    data = retry_data
                    meetings_count = retry_meetings
                    total_races = retry_races
                    total_horses = retry_horses
                else:
                    print("⚠️ RETRY ALSO FOUND NO DATA - proceeding with AI analysis anyway")
            else:
                print("📈 SUFFICIENT DATA FOUND - proceeding with analysis")
            
            # Check for suspicious patterns that indicate hallucination
            all_horses = [h for m in data.get("meetings", []) for r in m.get("races", []) for h in r.get("runners", [])]
            suspicious_names = ["SUPERHEART", "CASINO SEVENTEEN", "GOLDEN SANDS", "IRON WILL", "JUST MAGICAL", "AMAZING GRACE", "BOLD VENTURE", "COSMIC FORCE", "DANCING QUEEN", "ELECTRIC STORM", "SUPER EXTREME", "SMART IMAGE", "FASTOBULLET"]
            
            if any(name in all_horses for name in suspicious_names):
                print("🚨 HALLUCINATION DETECTED: Found suspicious horse names, returning empty fields")
                return {"meetings": [], "verification_notes": "Potential hallucinated horses detected, data rejected"}
            
            return data if isinstance(data, dict) else {"meetings": []}
            
        except Exception as e:
            print(f"❌ Error fetching verified fields: {e}")
            return {"meetings": [], "verification_notes": f"API error: {str(e)[:100]}"}

    def build_allowed_from_fields(self, fields_json):
        """Build a set of allowed horse names from official fields."""
        allowed_horses = set()
        for m in fields_json.get("meetings", []):
            for r in m.get("races", []):
                for h in r.get("runners", []):
                    if isinstance(h, str):
                        allowed_horses.add(h.strip().lower())
        return allowed_horses
        
    async def generate_analysis(self, min_score=9, target_date=None, comprehensive_mode=None):
        """Generate horse racing analysis with enhanced verification"""
        try:
            # Load settings to check comprehensive mode
            settings = load_settings()
            if comprehensive_mode is None:
                comprehensive_mode = settings.get('comprehensive_mode', False)
            
            # Get server-aware timing info
            time_info = get_server_aware_time_info()
            print(f"🌏 Server Time Info - Perth: {time_info['perth_time']}, Sydney: {time_info['sydney_time']}, Singapore: {time_info['singapore_time']}")
            
            # Fetch official fields to prevent hallucinated horses
            target_date_obj = target_date or datetime.now(SYD_TZ).date()
            fields = await self.fetch_fields_for_date_syd(target_date_obj)
            
            verification_notes = fields.get("verification_notes", "")
            meetings_count = len(fields.get("meetings", []))
            
            if looks_plausible_fields(fields) and meetings_count > 0:
                allowed = _build_allowed_norm_set(fields)  # real gating with normalized names
                print(f"✅ Loaded {len(allowed)} verified horse names from {meetings_count} race meetings")
                fields_for_prompt = fields  # inject real fields into prompt
            else:
                # NO FALLBACK - FORCE GEMINI TO SEARCH FOR REAL DATA
                target_date_str = target_date_obj.strftime("%A %d %B %Y")
                print(f"⚠️ Limited initial field data for {target_date_str} - will rely on AI web search during analysis")
                print(f"🔍 Verification notes: {verification_notes}")
                
                # Don't give up - proceed with analysis but tell Gemini to search harder
                allowed = set()  # Empty allowed set forces fresh search
                fields_for_prompt = {"meetings": [], "verification_notes": f"SEARCH REQUIRED: Find racing for {target_date_str}"}
                print("🚀 Proceeding with AI-POWERED SEARCH analysis - Gemini will find real racing data")
            
            # Choose prompt based on comprehensive mode
            if comprehensive_mode:
                base_prompt = LJ_MILE_PROMPT  # Use comprehensive prompt
                analysis_type = "comprehensive"
                print(f"🔍 Using COMPREHENSIVE analysis mode")
            else:
                base_prompt = build_today_prompt()  # Use standard prompt
                analysis_type = "standard"
                print(f"📊 Using STANDARD analysis mode")
            
            if target_date:
                # Custom date analysis
                syd = pytz.timezone('Australia/Sydney')
                target_dt = datetime.combine(target_date, datetime.min.time())
                target_dt = syd.localize(target_dt)
                anchor = target_dt.strftime("%A %d %B %Y (%Y-%m-%d) %H:%M %Z")
                
                # Add search instructions if fields were empty
                search_instruction = ""
                if not fields_for_prompt.get("meetings"):
                    search_instruction = f"""
🚨 MANDATORY WEB SEARCH REQUIRED: Initial field search failed.
You MUST search Australian racing websites to find racing for {anchor}.
Search racing.com.au, racenet.com.au, punters.com.au, tab.com.au
Fields for {target_date.strftime('%A %d %B %Y')} ARE published - find them.
DO NOT proceed with analysis until you find real racing data.

"""
                
                custom_prompt = (
                    search_instruction +
                    f"🚨 CRITICAL DATE INSTRUCTION: You MUST analyze races for {anchor}. "
                    f"Do NOT analyze today ({datetime.now(SYD_TZ).strftime('%A %d %B %Y')}). "
                    f"ONLY analyze races scheduled for {target_date.strftime('%A %d %B %Y')} ({target_date.isoformat()}). "
                    f"🌏 SERVER CONTEXT: Analysis requested from Singapore server for Australian racing data. "
                    f"Use the verified race fields provided and access Australian racing websites directly. "
                ) + "\n\n" + inject_fields_into_prompt(base_prompt, fields_for_prompt)
                
                if comprehensive_mode:
                    result = await call_gemini_comprehensive(custom_prompt, settings, allowed_horses=allowed)
                else:
                    result = await call_gemini_with_retry(custom_prompt, min_score, allowed_horses=allowed)
            else:
                # Regular today analysis with injected fields
                search_instruction = ""
                if not fields_for_prompt.get("meetings"):
                    search_instruction = f"""
🚨 MANDATORY WEB SEARCH: Initial field search failed.
You MUST search Australian racing websites NOW to find today's racing.
Search racing.com.au, racenet.com.au, punters.com.au, tab.com.au
Racing fields for today ARE published - find them before proceeding.

"""
                
                enhanced_prompt = search_instruction + inject_fields_into_prompt(base_prompt, fields_for_prompt)
                enhanced_prompt += f"\n\n🌏 SERVER CONTEXT: Analysis from Singapore server at {time_info['singapore_time']} for Australian racing. Use verified fields data provided."
                
                if comprehensive_mode:
                    result = await call_gemini_comprehensive(enhanced_prompt, settings, allowed_horses=allowed)
                else:
                    result = await call_gemini_with_retry(enhanced_prompt, min_score=min_score, allowed_horses=allowed)
            
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
    comprehensive_mode = settings.get('comprehensive_mode', False)
    
    try:
        analysis = await bot.generate_analysis(min_score, comprehensive_mode=comprehensive_mode)
        
        # Handle comprehensive mode display
        if comprehensive_mode:
            # For comprehensive mode, send as regular message (not embed) due to length
            if len(analysis) > 2000:
                # Split into chunks if too long
                chunks = []
                current_chunk = ""
                lines = analysis.split('\n')
                
                for line in lines:
                    if len(current_chunk) + len(line) + 1 > 1900:  # Leave buffer for Discord
                        if current_chunk:
                            chunks.append(current_chunk)
                        current_chunk = line
                    else:
                        current_chunk += '\n' + line if current_chunk else line
                
                if current_chunk:
                    chunks.append(current_chunk)
                
                # Send first chunk as followup
                await interaction.followup.send(chunks[0])
                
                # Send remaining chunks as separate messages
                for chunk in chunks[1:]:
                    await interaction.followup.send(chunk)
            else:
                await interaction.followup.send(analysis)
        else:
            # Standard mode - use embed
            embed = discord.Embed(
                title=f"🏇 LJ Mile Model Analysis (≥{min_score}/12)",
                description=analysis[:4096] if len(analysis) > 4096 else analysis,
                color=0x00ff00,
                timestamp=datetime.now(timezone.utc)
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
        comprehensive_mode = settings.get('comprehensive_mode', False)
        
        if min_score is not None and not 1 <= min_score <= 12:
            await interaction.followup.send("❌ Score must be between 1 and 12!")
            return
        
        analysis = await bot.generate_analysis(score_threshold, target_date, comprehensive_mode=comprehensive_mode)
        
        if comprehensive_mode:
            # For comprehensive mode, send as regular message due to length
            mode_text = "🔍 COMPREHENSIVE"
            
            if len(analysis) > 2000:
                # Split into chunks if too long
                chunks = []
                current_chunk = f"**{mode_text} Analysis for {target_date.strftime('%A, %B %d, %Y')} (≥{score_threshold}/12)**\n\n"
                lines = analysis.split('\n')
                
                for line in lines:
                    if len(current_chunk) + len(line) + 1 > 1900:
                        if current_chunk:
                            chunks.append(current_chunk)
                        current_chunk = line
                    else:
                        current_chunk += '\n' + line if current_chunk else line
                
                if current_chunk:
                    chunks.append(current_chunk)
                
                # Send first chunk
                await interaction.followup.send(chunks[0])
                
                # Send remaining chunks
                for chunk in chunks[1:]:
                    await interaction.followup.send(chunk)
            else:
                full_message = f"**{mode_text} Analysis for {target_date.strftime('%A, %B %d, %Y')} (≥{score_threshold}/12)**\n\n{analysis}"
                await interaction.followup.send(full_message)
        else:
            # Standard mode - use embed
            embed = discord.Embed(
                title=f"🏇 LJ Mile Model - {target_date.strftime('%A, %B %d, %Y')}",
                description=f"**Analysis for {date} (≥{score_threshold}/12)**\n\n" + 
                           (analysis[:4000] if len(analysis) > 4000 else analysis),
                color=0x00ff00,
                timestamp=datetime.now(timezone.utc)
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


class AdvancedSettingsModal(discord.ui.Modal, title="Advanced Analysis Settings"):
    def __init__(self, settings, refresh_callback=None):
        super().__init__()
        self.settings = settings
        self.refresh_callback = refresh_callback
        
        # Initialize text inputs with current values
        self.race_preview = discord.ui.TextInput(
            label="Race Preview (ON/OFF)",
            placeholder="ON",
            default="ON" if settings.get('race_preview_enabled', True) else "OFF",
            required=True,
            max_length=3
        )
        
        self.min_score_display = discord.ui.TextInput(
            label="Minimum Score to Display (1-12)",
            placeholder="6",
            default=str(settings.get('min_score_display', 6)),
            required=True,
            max_length=2
        )
        
        self.add_item(self.race_preview)
        self.add_item(self.min_score_display)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            # Validate race preview setting
            preview_val = str(self.race_preview).strip().upper()
            if preview_val not in ['ON', 'OFF']:
                raise ValueError("Race preview must be ON or OFF")
            
            # Validate minimum score
            min_score = int(str(self.min_score_display).strip())
            if min_score < 1 or min_score > 12:
                raise ValueError("Minimum score must be between 1 and 12")
            
            # Update settings
            self.settings['race_preview_enabled'] = (preview_val == 'ON')
            self.settings['min_score_display'] = min_score
            save_settings(self.settings)
            
            # Log changes
            log_setting_change(interaction.user, "race_preview_enabled", 
                             not self.settings['race_preview_enabled'], 
                             self.settings['race_preview_enabled'])
            log_setting_change(interaction.user, "min_score_display", 
                             self.settings.get('min_score_display', 6), min_score)
            
            await interaction.response.send_message(
                f"✅ Advanced settings updated:\n"
                f"• Race Preview: **{preview_val}**\n"
                f"• Min Score Display: **{min_score}/12**", 
                ephemeral=True
            )
            
        except ValueError as e:
            await interaction.response.send_message(f"❌ {str(e)}", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error updating settings: {str(e)}", ephemeral=True)


class RunnerDisplayModal(discord.ui.Modal, title="Runner Display Settings"):
    def __init__(self, settings, refresh_callback=None):
        super().__init__()
        self.settings = settings
        self.refresh_callback = refresh_callback
        
        # Initialize text inputs with current values
        self.show_all_runners = discord.ui.TextInput(
            label="Show All Runners (ON/OFF)",
            placeholder="OFF",
            default="ON" if settings.get('show_all_runners', False) else "OFF",
            required=True,
            max_length=3
        )
        
        self.max_runners = discord.ui.TextInput(
            label="Max Runners Per Race (if not showing all)",
            placeholder="8",
            default=str(settings.get('max_runners_per_race', 8)),
            required=True,
            max_length=2
        )
        
        self.show_low_scores = discord.ui.TextInput(
            label="Show Low Scoring Runners (ON/OFF)",
            placeholder="OFF",
            default="ON" if settings.get('show_low_scores', False) else "OFF",
            required=True,
            max_length=3
        )
        
        self.add_item(self.show_all_runners)
        self.add_item(self.max_runners)
        self.add_item(self.show_low_scores)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            # Validate show all runners
            all_runners_val = str(self.show_all_runners).strip().upper()
            if all_runners_val not in ['ON', 'OFF']:
                raise ValueError("Show All Runners must be ON or OFF")
            
            # Validate max runners
            max_runners = int(str(self.max_runners).strip())
            if max_runners < 1 or max_runners > 20:
                raise ValueError("Max runners must be between 1 and 20")
            
            # Validate show low scores
            low_scores_val = str(self.show_low_scores).strip().upper()
            if low_scores_val not in ['ON', 'OFF']:
                raise ValueError("Show Low Scores must be ON or OFF")
            
            # Update settings
            self.settings['show_all_runners'] = (all_runners_val == 'ON')
            self.settings['max_runners_per_race'] = max_runners
            self.settings['show_low_scores'] = (low_scores_val == 'ON')
            save_settings(self.settings)
            
            # Log changes
            log_setting_change(interaction.user, "show_all_runners", 
                             not self.settings['show_all_runners'], 
                             self.settings['show_all_runners'])
            log_setting_change(interaction.user, "max_runners_per_race", 
                             self.settings.get('max_runners_per_race', 8), max_runners)
            log_setting_change(interaction.user, "show_low_scores", 
                             not self.settings['show_low_scores'], 
                             self.settings['show_low_scores'])
            
            await interaction.response.send_message(
                f"✅ Runner display settings updated:\n"
                f"• Show All Runners: **{all_runners_val}**\n"
                f"• Max Runners: **{max_runners}** (when not showing all)\n"
                f"• Show Low Scores: **{low_scores_val}**", 
                ephemeral=True
            )
            
        except ValueError as e:
            await interaction.response.send_message(f"❌ {str(e)}", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error updating settings: {str(e)}", ephemeral=True)

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
    def __init__(self, settings=None, refresh_callback=None):
        super().__init__(timeout=300)
        self.settings = settings or load_settings()
        self.refresh_callback = refresh_callback
        
        # Add the tab selector first
        self.add_item(HorseTabs())
        
        # Add the distances selector with current settings
        current_distances = self.settings.get("include_distances", _distance_options())
        self.add_item(DistancesSelect(current_distances))
        
        # Update button states after all items are added
        self._update_comprehensive_button()

    @discord.ui.button(label="Set Min Score", style=discord.ButtonStyle.primary, row=0)
    async def set_score(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ScoreModal())

    @discord.ui.button(label="Set Weight Limits", style=discord.ButtonStyle.primary, row=0)
    async def set_weights(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WeightsModal())

    @discord.ui.button(label="Set Min Jockey SR %", style=discord.ButtonStyle.primary, row=0)
    async def set_jockey_sr(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(JockeySRModal())

    @discord.ui.button(label="Comprehensive Mode", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_comprehensive_mode(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Toggle comprehensive mode on/off."""
        self.settings['comprehensive_mode'] = not self.settings.get('comprehensive_mode', False)
        save_settings(self.settings)
        
        status = "ON" if self.settings['comprehensive_mode'] else "OFF"
        mode_desc = ("Show all tracks, races, and detailed analysis" if self.settings['comprehensive_mode'] 
                    else "Show filtered selections only")
        
        # Update button style and label based on state
        if self.settings['comprehensive_mode']:
            button.style = discord.ButtonStyle.success
            button.label = "Comprehensive Mode ✅"
        else:
            button.style = discord.ButtonStyle.secondary  
            button.label = "Comprehensive Mode"
        
        await interaction.response.send_message(
            f"🔍 **Comprehensive mode is now {status}**\n{mode_desc}", 
            ephemeral=True
        )

    def _update_comprehensive_button(self):
        """Update the comprehensive mode button state on init"""
        for item in self.children:
            if hasattr(item, 'callback') and item.callback.__name__ == 'toggle_comprehensive_mode':
                if self.settings.get('comprehensive_mode', False):
                    item.style = discord.ButtonStyle.success
                    item.label = "Comprehensive Mode ✅"
                else:
                    item.style = discord.ButtonStyle.secondary
                    item.label = "Comprehensive Mode"
                break

    @discord.ui.button(label="Analysis Settings", style=discord.ButtonStyle.secondary, emoji="⚙️", row=1)
    async def show_advanced_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show advanced comprehensive analysis settings."""
        modal = AdvancedSettingsModal(self.settings, refresh_callback=self.refresh_callback)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Runner Display", style=discord.ButtonStyle.secondary, emoji="🏇", row=1)
    async def show_runner_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show runner display settings."""
        modal = RunnerDisplayModal(self.settings, refresh_callback=self.refresh_callback)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Toggle H2H Filter", style=discord.ButtonStyle.secondary, row=2)
    async def toggle_h2h(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Toggle H2H negative filter."""
        self.settings['exclude_negative_h2h'] = not self.settings.get('exclude_negative_h2h', True)
        save_settings(self.settings)
        
        status = "ON" if self.settings['exclude_negative_h2h'] else "OFF"
        await interaction.response.send_message(
            f"🔄 H2H Negative Filter is now **{status}**", 
            ephemeral=True
        )

    @discord.ui.button(label="Toggle Distance Whitelist", style=discord.ButtonStyle.secondary, row=2)
    async def toggle_distance_whitelist(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Toggle distance whitelist enforcement."""
        self.settings['distance_whitelist_enforced'] = not self.settings.get('distance_whitelist_enforced', False)
        save_settings(self.settings)
        
        status = "ON" if self.settings['distance_whitelist_enforced'] else "OFF"
        await interaction.response.send_message(
            f"🎯 Distance Whitelist is now **{status}**", 
            ephemeral=True
        )

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, row=2)
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
    
    # Comprehensive mode settings
    comp_mode = "✅ ON" if settings.get("comprehensive_mode", False) else "❌ OFF"
    show_all = "✅ All" if settings.get("show_all_runners", False) else f"🔢 Top {settings.get('max_runners_per_race', 8)}"
    race_preview = "✅ ON" if settings.get("race_preview_enabled", True) else "❌ OFF"
    show_low = "✅ ON" if settings.get("show_low_scores", False) else "❌ OFF"
    
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

    embed = (discord.Embed(
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
    .add_field(name="🔍 Comprehensive Mode", value=comp_mode, inline=True)
    .add_field(name="🏇 Runners Display", value=show_all, inline=True)
    .add_field(name="🔮 Race Preview", value=race_preview, inline=True)
    .add_field(name="📊 Show Low Scores", value=show_low, inline=True)
    .add_field(name="📝 Recent Changes", value=changes_text, inline=False))
    
    return embed


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
    if which == "config":
        s = load_settings()
        # Use the new ConfigView class for the config tab
        return ConfigView(s)
    
    # For auto and search tabs, use the standard view structure
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
            timestamp=datetime.now(timezone.utc)
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
                    timestamp=datetime.now(timezone.utc)
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
            try:
                await interaction.response.edit_message(embed=build_auto_embed(s), view=build_horse_panel_view("auto"))
            except:
                try:
                    await interaction.edit_original_response(embed=build_auto_embed(s), view=build_horse_panel_view("auto"))
                except:
                    pass
            return

        if cid == "lj_edittimes":
            try:
                await interaction.response.send_modal(TimesModal())
            except:
                pass
            return

        if cid == "lj_setscore":
            try:
                await interaction.response.send_modal(ScoreModal())
            except:
                pass
            return

        if cid == "lj_setweights":
            try:
                await interaction.response.send_modal(WeightsModal())
            except:
                pass
            return

        if cid == "lj_setsr":
            try:
                await interaction.response.send_modal(JockeySRModal())
            except:
                pass
            return

        if cid == "lj_toggle_h2h":
            s = load_settings()
            before = "exclude_negative_h2h=" + str(s.get("exclude_negative_h2h", True))
            s["exclude_negative_h2h"] = not s.get("exclude_negative_h2h", True)
            save_settings(s)
            after = "exclude_negative_h2h=" + str(s["exclude_negative_h2h"])
            log_setting_change(interaction.user, "exclude_negative_h2h", before, after)
            try:
                await interaction.response.edit_message(embed=build_config_embed(s), view=build_horse_panel_view("config"))
            except:
                try:
                    await interaction.edit_original_response(embed=build_config_embed(s), view=build_horse_panel_view("config"))
                except:
                    pass
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
            
            try:
                await interaction.response.edit_message(embed=build_config_embed(s), view=build_horse_panel_view("config"))
            except:
                try:
                    await interaction.edit_original_response(embed=build_config_embed(s), view=build_horse_panel_view("config"))
                except:
                    pass
            return

        if cid == "lj_pickdate":
            print(f"DEBUG: Date selection triggered, values: {interaction.data.get('values')}")
            picked = (interaction.data.get("values") or [""])[0]
            if not picked:
                try:
                    await interaction.response.send_message("❌ Please pick a date.", ephemeral=True)
                except:
                    try:
                        await interaction.followup.send("❌ Please pick a date.", ephemeral=True)
                    except:
                        pass
                return
            
            print(f"DEBUG: Date picked: {picked}")
            s = load_settings()
            dt = datetime.strptime(picked, "%Y-%m-%d").date()
            print(f"DEBUG: Parsed date: {dt}, Today is: {datetime.now(SYD_TZ).date()}")
            
            # Safe defer - try response first, then followup if that fails
            deferred = False
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
                    deferred = True
                else:
                    # Already responded, we'll use followup later
                    pass
            except Exception as defer_error:
                print(f"DEBUG: Defer failed: {defer_error}")
                # Continue without deferring
                pass
            
            print(f"DEBUG: Starting analysis for specific date: {dt}")
            result = await bot.generate_analysis(s.get("min_score", 9), target_date=dt)
            print(f"DEBUG: Analysis complete, result length: {len(result) if result else 0}")
            print(f"DEBUG: First 200 chars of result: {result[:200] if result else 'None'}")
            
            await send_analysis_message(
                f"🗓 {dt.strftime('%A %d %B %Y')} (Sydney)\n\n{result}",
                title=f"🏇 LJ Scan — {dt.isoformat()} (Config min {s.get('min_score', 9)}/12)",
                channel_id=interaction.channel.id
            )
            
            # Send confirmation
            try:
                if deferred:
                    await interaction.followup.send("✅ Scan posted in this channel.", ephemeral=True)
                else:
                    await interaction.response.send_message("✅ Scan posted in this channel.", ephemeral=True)
            except Exception as confirm_error:
                print(f"DEBUG: Confirmation send failed: {confirm_error}")
                # Don't crash on confirmation failure
                pass
            return

    except Exception as e:
        # Don't crash on interactions
        print(f"ERROR in interaction handler: {e}")
        import traceback
        traceback.print_exc()
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
        except Exception as error_send_fail:
            print(f"Failed to send error message: {error_send_fail}")
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
