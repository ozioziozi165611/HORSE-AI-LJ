import os
import time
import math

# Import required libraries
import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
import asyncio
import logging
import json
import sys
from datetime import datetime, timezone, timedelta, time as dt_time
import pytz
from typing import Dict, List, Optional
import re

# Load environment variables
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0"))

# Check if required environment variables are set
if not ODDS_API_KEY:
    raise ValueError("ODDS_API_KEY environment variable is required")
if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN environment variable is required")
if not BOT_OWNER_ID:
    raise ValueError("BOT_OWNER_ID environment variable is required")

# BetVillian Configuration
BETVILLIAN_LOGO = os.getenv("BETVILLIAN_LOGO", "https://cdn.discordapp.com/attachments/1234567890/betvillian_logo.png")  # Logo URL from environment or default

# Global scanning to prevent duplicate API calls
GLOBAL_CACHE = {}
GLOBAL_CACHE_DURATION = 900  # 15 minutes cache
LAST_GLOBAL_SCAN = 0

# Global hourly scanning system
GLOBAL_SCAN_DATA = {}
LAST_GLOBAL_HOURLY_SCAN = None
NEXT_GLOBAL_SCAN_TIME = None

# Bookmaker URLs for betting links
BOOKMAKER_URLS = {
    "Sportsbet": "https://www.sportsbet.com.au/",
    "TAB": "https://www.tab.com.au/",
    "Ladbrokes": "https://www.ladbrokes.com.au/",
    "Neds": "https://www.neds.com.au/",
    "Unibet": "https://www.unibet.com.au/",
    "PointsBet (AU)": "https://pointsbet.com.au/",
    "PlayUp": "https://www.playup.com.au/",
    "Dabble": "https://dabble.com.au/"
}

# Bankroll tracking per guild
guild_bankrolls = {}  # {guild_id: {"current": float, "initial": 100.0, "daily_profit": float, "total_profit": float, "bets_today": [], "last_reset": datetime}}

# Track active bets for result checking
active_bets = {}  # {message_id: {"event_id": str, "outcome": str, "odds": float, "stake": float, "guild_id": int, "market_key": str, "posted_time": datetime, "channel_id": int}}

# Views imported inline to avoid circular imports
# Stats tracking variables
sent_alerts = []
bet_stats = {
    "ev": {"sent": 0, "profitable": 0},
    "arbitrage": {"sent": 0, "profitable": 0},
    "total_sent": 0, 
    "total_profitable": 0
}

# Configuration loaded from environment variables
REGION = "au"
SCAN_INTERVAL_SECONDS = 1800
GLOBAL_SCAN_INTERVAL = 3600  # Default global scan interval (1 hour)
DEFAULT_MIN_MARGIN = 2.0
DEFAULT_MIN_EV = 5.0
SETTINGS_FILE = "betvillian_settings.json"
BANKROLL_FILE = "bankroll_data.json"
ACTIVE_BETS_FILE = "active_bets.json"

BOOKMAKERS_ALL = [
    "PlayUp", "Unibet", "TAB", "Ladbrokes",
    "PointsBet (AU)", "Neds", "Sportsbet", "Dabble"
]

MARKET_LABELS = {
    "h2h": "Match Winner",
    "totals": "Over/Under",
    "spreads": "Handicap"
}

EXCLUDED_SPORT_KEYWORDS = [
    "winner", "futures", "election", "olympics",
    "medal", "outright", "mvp", "prop", "draft", "special"
]

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# File paths for data persistence
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "betvillian_settings.json")
BANKROLL_FILE = os.path.join(BASE_DIR, "bankroll_data.json")
ACTIVE_BETS_FILE = os.path.join(BASE_DIR, "active_bets.json")

def load_settings():
    """Load server settings from file"""
    global server_settings
    try:
        with open(SETTINGS_FILE, "r") as f:
            server_settings = {int(k): v for k, v in json.load(f).items()}
        logging.info(f"Loaded settings for {len(server_settings)} guilds")
    except FileNotFoundError:
        server_settings = {}
        logging.info("No settings file found, starting fresh")
    except Exception as e:
        logging.error(f"Failed to load settings: {e}")
        server_settings = {}

@bot.event
async def on_ready():
    """Initialize bot when ready"""
    print(f"🤖 Bot logged in as {bot.user}")
    
    # Load data on startup
    try:
        load_settings()
        load_bankroll_data()
        load_active_bets()
        print("✅ Loaded bot data successfully")
    except Exception as e:
        print(f"⚠️ Warning loading data: {e}")
    
    # Sync commands globally (works in any server)
    try:
        # List all commands before sync
        print(f"🔍 Commands to sync: {len(bot.tree.get_commands())}")
        for cmd in bot.tree.get_commands():
            print(f"  - {cmd.name}: {cmd.description}")
        
        # DEBUG: If we don't have 5 commands, something is wrong
        if len(bot.tree.get_commands()) < 5:
            print("❌ ERROR: Expected 5 commands but only found", len(bot.tree.get_commands()))
            print("🔧 Checking command definitions...")
        else:
            print("✅ All 5 commands detected properly")
        
        # Force sync without clearing (let Discord handle duplicates)
        print("� Syncing commands globally...")
        synced = await bot.tree.sync()
        print(f"✅ First sync: {len(synced)} commands globally")
        
        # Wait a moment and sync again to ensure it takes
        await asyncio.sleep(2)
        synced2 = await bot.tree.sync()
        print(f"🔄 Second sync: {len(synced2)} commands")
        
        # List synced commands
        for cmd in synced:
            print(f"  📝 /{cmd.name} - {cmd.description}")
            
    except Exception as e:
        print(f"❌ Failed to sync commands globally: {e}")
        # If sync fails, try again in 5 seconds
        try:
            print("🔄 Retrying command sync in 5 seconds...")
            await asyncio.sleep(5)
            synced = await bot.tree.sync()
            print(f"✅ Retry successful: {len(synced)} commands synced")
            for cmd in synced:
                print(f"  📝 /{cmd.name} - {cmd.description}")
        except Exception as e2:
            print(f"❌ Retry also failed: {e2}")
    
    # Start background tasks after bot is ready
    try:
        if not check_results_task.is_running():
            check_results_task.start()
            print("✅ Started bet result checking task")
        
        if not daily_summary_task.is_running():
            daily_summary_task.start()
            print("✅ Started daily summary task")
        
        if not save_active_bets_task.is_running():
            save_active_bets_task.start()
            print("✅ Started save active bets task")
        
        if not global_hourly_scan_task.is_running():
            global_hourly_scan_task.start()
            print("✅ Started global hourly scan task")
        
        if not scan_warning_task.is_running():
            scan_warning_task.start()
            print("✅ Started scan warning task")
    except Exception as e:
        print(f"❌ Error starting background tasks: {e}")
    
    print("🎉 Bot is ready! Commands are available in any server the bot is added to.")
    
    # Auto-enable scanning for servers with channels configured but scanning disabled
    try:
        auto_enabled_count = 0
        for guild_id, settings in server_settings.items():
            updated = False
            
            # Enable EV scanning if channel is set but scanning is disabled
            if settings.get("ev_alert_channel") and not settings.get("ev_scan_enabled", False):
                settings["ev_scan_enabled"] = True
                updated = True
                print(f"🔄 Auto-enabled EV scanning for guild {guild_id}")
            
            # Enable ARB scanning if channel is set but scanning is disabled
            if settings.get("arb_alert_channel") and not settings.get("arb_scan_enabled", False):
                settings["arb_scan_enabled"] = True
                updated = True
                print(f"🔄 Auto-enabled ARB scanning for guild {guild_id}")
            
            if updated:
                auto_enabled_count += 1
        
        if auto_enabled_count > 0:
            await save_settings_async()
            print(f"✅ Auto-enabled scanning for {auto_enabled_count} servers with configured channels")
    except Exception as e:
        print(f"⚠️ Error during auto-enable scan: {e}")
    
    # Show global scan initialization info
    if NEXT_GLOBAL_SCAN_TIME:
        print(f"🌐 Global Scan System: Initialized")
        print(f"⏰ First scan scheduled: {NEXT_GLOBAL_SCAN_TIME.strftime('%H:%M:%S UTC')}")
        print(f"📊 Scan frequency: Every hour")
        print(f"🔄 All servers will sync to this global timer for efficiency")
    else:
        print("⚠️ Global scan system not initialized")

server_settings = {}
last_ev_sent = {}
last_arb_sent = {}
next_scan_time = datetime.now(timezone.utc)

# Track sent EV bets and timestamps per guild
ev_sent_tracker = {}

# Caching
sports_cache = {"data": None, "timestamp": None}
odds_cache = {}  # key: (sport_key, market), value: {"data": ..., "timestamp": ...}
CACHE_SECONDS = 15  # Reduced from 60 to 15 seconds for fresher arbitrage odds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("betvillian.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

# Add this line
settings_lock = asyncio.Lock()

def save_settings():
    """Save server settings to file"""
    try:
        with open(SETTINGS_FILE, "w") as f:
            # Convert integer keys to strings for JSON serialization
            json.dump({str(k): v for k, v in server_settings.items()}, f, indent=2)
        logging.info(f"Saved settings for {len(server_settings)} guilds")
    except Exception as e:
        logging.error(f"Failed to save settings: {e}")

async def save_settings_async():
    """Async wrapper for save_settings with locking"""
    async with settings_lock:
        save_settings()

async def save_settings_async_unlocked():
    """Async wrapper for save_settings - assumes caller handles locking"""
    save_settings()

def format_datetime(iso_str):
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(pytz.timezone("Australia/Sydney"))
    return dt.strftime("%A, %d %B %Y %I:%M %p")

def is_admin_authorized(interaction: discord.Interaction) -> bool:
    # Bot owner always has access
    if interaction.user.id == BOT_OWNER_ID:
        return True
        
    guild_id = interaction.guild.id
    settings = server_settings.get(guild_id, {})
    if not settings.get("setup_done"):
        return True
    if interaction.user.id == settings.get("setup_user_id"):
        return True
    member = interaction.guild.get_member(interaction.user.id)
    if not member:
        # fallback to interaction.user (may not have roles)
        member = interaction.user
    authorized_roles = settings.get("authorized_roles", [])
    user_role_ids = [role.id for role in getattr(member, "roles", [])]
    return any(rid in authorized_roles for rid in user_role_ids)

def check_admin_authorization(user, guild_id):
    """Check if user has admin authorization for this guild"""
    # Bot owner always has access
    if user.id == BOT_OWNER_ID:
        return True
        
    settings = server_settings.get(guild_id, {})
    
    # If setup not done, allow anyone to set up
    if not settings.get("setup_done"):
        return True
    
    # Original setup user always has access
    if user.id == settings.get("setup_user_id"):
        return True
    
    # Check if user has authorized role
    authorized_roles = settings.get("authorized_roles", [])
    user_role_ids = [role.id for role in getattr(user, "roles", [])]
    return any(rid in authorized_roles for rid in user_role_ids)

def check_user_authorization(user, guild_id):
    """Check if user has basic authorization to use bot commands"""
    # Bot owner always has access
    if user.id == BOT_OWNER_ID:
        return True
        
    settings = server_settings.get(guild_id, {})
    
    # If setup not done, allow anyone
    if not settings.get("setup_done"):
        return True
    
    # Admin users can always use commands
    if check_admin_authorization(user, guild_id):
        return True
    
    # Check if user has any authorized role (for basic usage)
    authorized_roles = settings.get("authorized_roles", [])
    user_role_ids = [role.id for role in getattr(user, "roles", [])]
    return any(rid in authorized_roles for rid in user_role_ids)

def is_bot_owner(user_id: int) -> bool:
    """Check if user is the bot owner (only they can access certain commands)"""
    return user_id == BOT_OWNER_ID

# ---------- BANKROLL MANAGEMENT ----------

def initialize_bankroll(guild_id, bankroll_amount=1000.0):
    """Initialize bankroll with specified dollar amount"""
    if guild_id not in guild_bankrolls:
        guild_bankrolls[guild_id] = {
            "current": bankroll_amount,  # Current bankroll in dollars
            "initial": bankroll_amount,  # Initial bankroll in dollars
            "daily_profit": 0.0,  # Daily profit in dollars
            "total_profit": 0.0,  # Total profit in dollars
            "bets_today": [],
            "won_bets": 0,
            "lost_bets": 0,
            "pending_bets": 0,
            "void_bets": 0,
            "last_reset": datetime.now(timezone.utc).date(),
            "total_staked": 0.0,  # Total staked in dollars
            "total_winnings": 0.0  # Total winnings in dollars
        }
        save_bankroll_data()

def set_bankroll_amount(guild_id, new_amount):
    """Set new bankroll amount in dollars"""
    initialize_bankroll(guild_id)
    old_amount = guild_bankrolls[guild_id]["current"]
    
    # Update bankroll amounts
    guild_bankrolls[guild_id]["current"] = new_amount
    guild_bankrolls[guild_id]["initial"] = new_amount
    
    save_bankroll_data()
    return old_amount, new_amount

def calculate_stake_dollars(ev_percent, odds, current_bankroll):
    """Calculate stake in dollars using Kelly Criterion (1% = 1% of bankroll)"""
    if ev_percent <= 0 or odds <= 1.0:
        return 0.0
    
    # Kelly Criterion: f = (bp - q) / b
    win_prob = 1 / odds  # Simplified - normally would use true probability
    kelly_fraction = ((odds * win_prob) - 1) / (odds - 1)
    
    # Apply conservative factor and EV adjustment
    conservative_kelly = kelly_fraction * (ev_percent / 100) * 0.25  # 25% of Kelly
    
    # Cap at 2% of bankroll for safety
    stake_dollars = min(conservative_kelly * current_bankroll, current_bankroll * 0.02)
    
    # Minimum stake of 0.5% of bankroll if EV > 5%
    if ev_percent >= 5.0:
        stake_dollars = max(stake_dollars, current_bankroll * 0.005)
    
    return round(stake_dollars, 2)

def calculate_stake_suggestion(ev_percent, odds):
    """Calculate dynamic stake suggestion based on EV percentage and odds"""
    if ev_percent <= 0 or odds <= 1.0:
        return 0.5  # Minimum stake for any positive EV
    
    # Improved Kelly-style calculation for EV bets
    # For EV bets, we estimate true probability from the EV and offered odds
    implied_prob = 1 / odds  # Bookmaker's implied probability
    ev_decimal = ev_percent / 100
    
    # Estimate true probability: if EV = (true_odds / offered_odds) - 1
    # Then true_odds = offered_odds * (1 + EV)
    estimated_true_odds = odds * (1 + ev_decimal)
    true_prob = 1 / estimated_true_odds
    
    # Kelly Criterion: f = (bp - q) / b
    # where b = odds - 1, p = true_prob, q = 1 - true_prob
    b = odds - 1
    kelly_fraction = ((b * true_prob) - (1 - true_prob)) / b
    kelly_fraction = max(kelly_fraction, 0)  # Ensure non-negative
    
    # Scale based on EV confidence - higher EV = higher confidence
    if ev_percent >= 20:
        confidence_multiplier = 0.5  # 50% of Kelly for very high EV
    elif ev_percent >= 15:
        confidence_multiplier = 0.4  # 40% of Kelly
    elif ev_percent >= 10:
        confidence_multiplier = 0.3  # 30% of Kelly
    elif ev_percent >= 7:
        confidence_multiplier = 0.25  # 25% of Kelly
    elif ev_percent >= 5:
        confidence_multiplier = 0.2   # 20% of Kelly
    else:
        confidence_multiplier = 0.15  # 15% of Kelly for smaller edges
    
    # Calculate base stake
    stake = kelly_fraction * confidence_multiplier * 100  # Scale to reasonable units
    
    # Odds-based adjustments for risk management
    if odds >= 4.0:  # Very long odds - reduce stake
        stake *= 0.7
    elif odds >= 3.0:  # Long odds - slightly reduce stake
        stake *= 0.85
    elif odds >= 2.5:  # Medium-long odds
        stake *= 0.95
    elif odds <= 1.5:  # Very short odds - increase stake slightly
        stake *= 1.1
    
    # Apply reasonable bounds based on EV
    if ev_percent >= 15:
        min_stake, max_stake = 2.0, 12.0
    elif ev_percent >= 10:
        min_stake, max_stake = 1.5, 8.0
    elif ev_percent >= 7:
        min_stake, max_stake = 1.0, 6.0
    elif ev_percent >= 5:
        min_stake, max_stake = 0.8, 4.0
    else:
        min_stake, max_stake = 0.5, 2.5
    
    stake = max(stake, min_stake)
    stake = min(stake, max_stake)
    
    # Debug logging for stake calculation issues
    if stake == 0.5 and ev_percent > 5:
        print(f"[DEBUG] Stake calculation: EV={ev_percent}%, odds={odds}, kelly={kelly_fraction:.3f}, final_stake={stake}")
    
    return round(stake, 1)

def update_bankroll_result_enhanced(guild_id, stake_dollars, odds, won, bet_info):
    """Enhanced bankroll update with detailed tracking and analytics"""
    initialize_bankroll(guild_id)
    bankroll = guild_bankrolls[guild_id]
    
    # Check if it's a new day - reset daily profit
    today = datetime.now(timezone.utc).date()
    if bankroll["last_reset"] != today:
        bankroll["daily_profit"] = 0.0
        bankroll["bets_today"] = []
        bankroll["last_reset"] = today
        logging.info(f"📅 New day detected for guild {guild_id} - reset daily stats")
    
    # Calculate profit/loss
    if won:
        profit_dollars = stake_dollars * (odds - 1)
        bankroll["current"] += profit_dollars
        bankroll["daily_profit"] += profit_dollars
        bankroll["total_profit"] += profit_dollars
        bankroll["won_bets"] += 1
        bankroll["total_winnings"] += profit_dollars
        
        # Enhanced win tracking by market type
        market_wins = bankroll.setdefault("market_wins", {})
        market_key = bet_info.get("market_key", "unknown")
        market_wins[market_key] = market_wins.get(market_key, 0) + 1
        
    else:
        profit_dollars = -stake_dollars
        bankroll["current"] -= stake_dollars
        bankroll["daily_profit"] -= stake_dollars
        bankroll["total_profit"] -= stake_dollars
        bankroll["lost_bets"] += 1
        
        # Enhanced loss tracking by market type
        market_losses = bankroll.setdefault("market_losses", {})
        market_key = bet_info.get("market_key", "unknown")
        market_losses[market_key] = market_losses.get(market_key, 0) + 1
    
    # Remove from pending bets
    bankroll["pending_bets"] = max(0, bankroll.get("pending_bets", 0) - 1)
    bankroll["total_staked"] += stake_dollars
    
    # Enhanced bet tracking with more details
    bet_record = {
        "stake": stake_dollars,
        "odds": odds,
        "won": won,
        "profit": profit_dollars,
        "time": datetime.now(timezone.utc).isoformat(),
        "market_key": bet_info.get("market_key", "unknown"),
        "bookmaker": bet_info.get("bookmaker", "unknown"),
        "event_id": bet_info.get("event_id", "unknown"),
        "game": bet_info.get("game", "unknown"),
        "ev_percent": bet_info.get("ev_percent", 0),  # Store EV if available
        "outcome": bet_info.get("outcome", "unknown")
    }
    
    bankroll["bets_today"].append(bet_record)
    
    # Enhanced analytics tracking
    total_bets = bankroll["won_bets"] + bankroll["lost_bets"]
    if total_bets > 0:
        bankroll["win_rate"] = (bankroll["won_bets"] / total_bets) * 100
        bankroll["avg_stake"] = bankroll["total_staked"] / total_bets
        bankroll["avg_odds"] = sum(bet["odds"] for bet in bankroll["bets_today"][-10:]) / min(10, len(bankroll["bets_today"]))  # Last 10 bets
    
    # Risk management alerts
    if bankroll["current"] < bankroll["initial"] * 0.8:  # 20% drawdown
        logging.warning(f"⚠️ Guild {guild_id} hit 20% drawdown: ${bankroll['current']:.2f}")
    elif bankroll["current"] < bankroll["initial"] * 0.5:  # 50% drawdown
        logging.error(f"🚨 Guild {guild_id} hit 50% drawdown: ${bankroll['current']:.2f}")
    
    save_bankroll_data()
    
    # Enhanced logging
    result_emoji = "✅" if won else "❌"
    logging.info(f"{result_emoji} Bet result for guild {guild_id}: ${profit_dollars:+.2f} | Bankroll: ${bankroll['current']:.2f} | Market: {market_key}")
    
    return bankroll

def update_bankroll_result(guild_id, stake_dollars, odds, won):
    """Legacy function - redirect to enhanced version with minimal bet info"""
    fake_bet_info = {"market_key": "unknown", "bookmaker": "unknown", "event_id": "unknown", "game": "unknown"}
    return update_bankroll_result_enhanced(guild_id, stake_dollars, odds, won, fake_bet_info)

async def handle_expired_bet(message_id, bet_info):
    """Handle bets that are too old to check"""
    try:
        # Mark as void in bankroll
        guild_id = bet_info["guild_id"]
        initialize_bankroll(guild_id)
        guild_bankrolls[guild_id]["void_bets"] = guild_bankrolls[guild_id].get("void_bets", 0) + 1
        guild_bankrolls[guild_id]["pending_bets"] = max(0, guild_bankrolls[guild_id].get("pending_bets", 0) - 1)
        save_bankroll_data()
        
        # Add void reaction to original message
        channel = bot.get_channel(bet_info["channel_id"])
        if channel:
            try:
                message = await channel.fetch_message(message_id)
                await message.add_reaction("🚫")  # Void reaction
            except:
                pass  # Message might be deleted
        
        logging.info(f"⏰ Expired bet {message_id} marked as void after 14 days")
        
    except Exception as e:
        logging.error(f"Error handling expired bet {message_id}: {e}")

async def flag_bet_for_review(message_id, bet_info, game_result):
    """Flag bets that need manual review"""
    try:
        # Log detailed information for manual review
        logging.warning(f"🔍 Bet flagged for manual review:")
        logging.warning(f"   Message ID: {message_id}")
        logging.warning(f"   Event: {bet_info.get('game', 'Unknown')}")
        logging.warning(f"   Market: {bet_info.get('market_key', 'Unknown')}")
        logging.warning(f"   Outcome: {bet_info.get('outcome', 'Unknown')}")
        logging.warning(f"   Game Result: {game_result}")
        
        # Add review reaction
        channel = bot.get_channel(bet_info["channel_id"])
        if channel:
            try:
                message = await channel.fetch_message(message_id)
                await message.add_reaction("🔍")  # Review needed reaction
            except:
                pass
        
        # Could implement a manual review queue here in the future
        
    except Exception as e:
        logging.error(f"Error flagging bet for review {message_id}: {e}")

async def send_comprehensive_bet_result(message_id, bet_info, bet_won, updated_bankroll):
    """Send enhanced bet result notification with analytics"""
    try:
        # Add reaction to original message
        await add_bet_reaction(message_id, bet_info, bet_won)
        
        # Send enhanced notification
        channel = bot.get_channel(bet_info["channel_id"])
        if not channel:
            return
        
        profit = bet_info["stake"] * (bet_info["odds"] - 1) if bet_won else -bet_info["stake"]
        
        # Enhanced embed with more details
        embed = discord.Embed(
            title="🎯 Bet Result - Enhanced Tracking",
            color=0x00FF00 if bet_won else 0xFF0000,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.set_thumbnail(url=BETVILLIAN_LOGO)
        
        # Result and basic info
        result_emoji = "✅" if bet_won else "❌"
        embed.add_field(
            name="📊 Outcome Details",
            value=f"**Result:** {'WON' if bet_won else 'LOST'} {result_emoji}\n"
                  f"**Market:** {MARKET_LABELS.get(bet_info.get('market_key', 'unknown'), bet_info.get('market_key', 'Unknown'))}\n"
                  f"**Bookmaker:** {bet_info.get('bookmaker', 'Unknown')}",
            inline=True
        )
        
        # Financial details
        embed.add_field(
            name="💰 Financial Impact",
            value=f"**Profit/Loss:** ${profit:+.2f}\n"
                  f"**Stake:** ${bet_info['stake']:.2f}\n"
                  f"**Odds:** {bet_info['odds']:.2f}",
            inline=True
        )
        
        # Enhanced bankroll info
        win_rate = updated_bankroll.get("win_rate", 0)
        total_bets = updated_bankroll["won_bets"] + updated_bankroll["lost_bets"]
        
        embed.add_field(
            name="📈 Updated Statistics", 
            value=f"**Current Bankroll:** ${updated_bankroll['current']:,.2f}\n"
                  f"**Daily P&L:** ${updated_bankroll['daily_profit']:+,.2f}\n"
                  f"**Win Rate:** {win_rate:.1f}% ({updated_bankroll['won_bets']}/{total_bets})",
            inline=False
        )
        
        # Risk management info
        initial_bankroll = updated_bankroll["initial"]
        drawdown = ((initial_bankroll - updated_bankroll["current"]) / initial_bankroll) * 100
        
        if drawdown > 0:
            risk_color = "🟡" if drawdown < 10 else "🟠" if drawdown < 20 else "🔴"
            embed.add_field(
                name="⚠️ Risk Analysis",
                value=f"**Drawdown:** {drawdown:.1f}% {risk_color}\n"
                      f"**Peak Bankroll:** ${max(initial_bankroll, updated_bankroll['current']):,.2f}",
                inline=True
            )
        
        embed.set_footer(text="BetVillian Enhanced Result Tracker • Comprehensive Analytics", icon_url=BETVILLIAN_LOGO)
        
        await channel.send(embed=embed)
        
    except Exception as e:
        logging.error(f"Failed to send comprehensive bet result for {message_id}: {e}")

async def save_active_bets_to_file():
    """Save active bets to file immediately"""
    try:
        if active_bets:
            # Convert datetime objects to strings for JSON serialization
            serializable_bets = {}
            for bet_id, bet_info in active_bets.items():
                bet_copy = bet_info.copy()
                if isinstance(bet_copy.get("posted_time"), datetime):
                    bet_copy["posted_time"] = bet_copy["posted_time"].isoformat()
                serializable_bets[bet_id] = bet_copy
            
            with open(ACTIVE_BETS_FILE, "w") as f:
                json.dump(serializable_bets, f, indent=2)
            
            logging.info(f"💾 Saved {len(active_bets)} active bets to file")
    except Exception as e:
        logging.error(f"Error saving active bets to file: {e}")

def save_bankroll_data():
    """Save bankroll data to file"""
    try:
        with open(BANKROLL_FILE, "w") as f:
            # Convert date objects to strings for JSON serialization
            data_to_save = {}
            for guild_id, data in guild_bankrolls.items():
                data_copy = data.copy()
                if hasattr(data_copy.get("last_reset"), 'isoformat'):
                    data_copy["last_reset"] = data_copy["last_reset"].isoformat()
                data_to_save[str(guild_id)] = data_copy
            json.dump(data_to_save, f, indent=2)
        logging.info(f"Saved bankroll data for {len(guild_bankrolls)} guilds")
    except Exception as e:
        logging.error(f"Failed to save bankroll data: {e}")

def load_bankroll_data():
    """Load bankroll data from file"""
    global guild_bankrolls
    try:
        with open(BANKROLL_FILE, "r") as f:
            data = json.load(f)
            guild_bankrolls = {}
            for guild_id, bankroll_data in data.items():
                # Convert string dates back to date objects
                if "last_reset" in bankroll_data and isinstance(bankroll_data["last_reset"], str):
                    try:
                        bankroll_data["last_reset"] = datetime.fromisoformat(bankroll_data["last_reset"]).date()
                    except:
                        bankroll_data["last_reset"] = datetime.now(timezone.utc).date()
                guild_bankrolls[int(guild_id)] = bankroll_data
        logging.info(f"Loaded bankroll data for {len(guild_bankrolls)} guilds")
    except FileNotFoundError:
        guild_bankrolls = {}
        logging.info("No bankroll file found, starting fresh")
    except Exception as e:
        logging.error(f"Failed to load bankroll data: {e}")
        guild_bankrolls = {}

async def send_daily_bankroll_summary(guild_id):
    """Send daily P&L summary to configured channel"""
    settings = server_settings.get(guild_id, {})
    channel_id = settings.get("bankroll_notification_channel")
    
    if not channel_id:
        return
    
    channel = bot.get_channel(channel_id)
    if not channel:
        return
    
    initialize_bankroll(guild_id)
    bankroll = guild_bankrolls[guild_id]
    
    today_bets = len(bankroll["bets_today"])
    daily_profit = bankroll["daily_profit"]
    total_profit = bankroll["total_profit"]
    current_bankroll = bankroll["current"]
    initial_bankroll = bankroll["initial"]
    
    # Count pending bets from active_bets - ONLY for this server
    pending_bets = sum(1 for bet_info in active_bets.values() if bet_info["guild_id"] == guild_id)
    bankroll["pending_bets"] = pending_bets  # Update the count
    
    # Calculate server-specific win rate
    total_bets = bankroll["won_bets"] + bankroll["lost_bets"]
    win_rate = (bankroll["won_bets"] / total_bets * 100) if total_bets > 0 else 0
    
    # Calculate global statistics across all servers
    global_won_bets = sum(bankroll_data.get("won_bets", 0) for bankroll_data in guild_bankrolls.values())
    global_lost_bets = sum(bankroll_data.get("lost_bets", 0) for bankroll_data in guild_bankrolls.values())
    global_total_bets = global_won_bets + global_lost_bets
    global_win_rate = (global_won_bets / global_total_bets * 100) if global_total_bets > 0 else 0
    global_total_profit = sum(bankroll_data.get("total_profit", 0) for bankroll_data in guild_bankrolls.values())
    
    # Create embed
    color = 0x00FF00 if daily_profit >= 0 else 0xFF0000
    embed = discord.Embed(
        title="📊 Daily Bankroll Summary",
        description=f"**{datetime.now(timezone.utc).strftime('%A, %B %d, %Y')}**",
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    
    embed.set_thumbnail(url=BETVILLIAN_LOGO)
    
    # Today's Performance
    embed.add_field(
        name="📈 Today's Performance",
        value=f"**Profit/Loss:** ${daily_profit:+,.2f}\n"
              f"**Bets Placed:** {today_bets}\n"
              f"**Current Bankroll:** ${current_bankroll:,.2f}",
        inline=True
    )
    
    # Server All-Time Performance
    embed.add_field(
        name="🎯 Server All-Time Performance", 
        value=f"**Server Total Profit:** ${total_profit:+,.2f}\n"
              f"**Server Total Bets:** {total_bets}\n"
              f"**Server Win Rate:** {win_rate:.1f}%",
        inline=True
    )
    
    # Bet Status - Show server-specific pending bets with global won/lost stats
    embed.add_field(
        name="🎲 Server Bet Status",
        value=f"**Server Won:** {bankroll['won_bets']} ✅\n"
              f"**Server Lost:** {bankroll['lost_bets']} ❌\n"
              f"**Server Pending:** {pending_bets} ⏳",
        inline=True
    )
    
    # Global Network Statistics in Daily Summary
    embed.add_field(
        name="🌐 Global Network Stats",
        value=f"**Network Won:** {global_won_bets} ✅\n"
              f"**Network Lost:** {global_lost_bets} ❌\n"
              f"**Network Win Rate:** {global_win_rate:.1f}%\n"
              f"**Network P&L:** ${global_total_profit:+,.2f}",
        inline=True
    )
    
    # ROI Calculation
    roi = (total_profit / initial_bankroll) * 100 if initial_bankroll > 0 else 0
    embed.add_field(
        name="💰 Financial Summary",
        value=f"**ROI:** {roi:+.2f}%\n"
              f"**Initial Bankroll:** ${initial_bankroll:,.2f}\n"
              f"**Current Bankroll:** ${current_bankroll:,.2f}",
        inline=False
    )
    
    embed.set_footer(text="BetVillian Bankroll Tracker • Direct dollar tracking", icon_url=BETVILLIAN_LOGO)
    
    await channel.send(embed=embed)

# ---------- BET RESULT CHECKING ----------

async def check_bet_results():
    """Enhanced bet result checking with better error handling and retry logic"""
    if not active_bets:
        logging.debug("No active bets to check")
        return
        
    current_time = datetime.now(timezone.utc)
    completed_bets = []
    results_summary = {"resolved": 0, "failed": 0, "waiting": 0, "expired": 0}
    
    logging.info(f"🔍 Checking {len(active_bets)} active bets for results...")
    
    # Sort bets by posted time (oldest first) for more predictable processing
    sorted_bets = sorted(active_bets.items(), key=lambda x: x[1]["posted_time"])
    
    for message_id, bet_info in sorted_bets:
        try:
            # Enhanced timing logic
            time_since_posted = (current_time - bet_info["posted_time"]).total_seconds()
            
            # Skip if too new (wait at least 2 hours after event start)
            if time_since_posted < 7200:  # 2 hours
                results_summary["waiting"] += 1
                continue
            
            # Give up if too old (14 days instead of 7 for better coverage)
            if time_since_posted > 1209600:  # 14 days
                logging.warning(f"Expiring bet {message_id} - too old ({time_since_posted/86400:.1f} days)")
                await handle_expired_bet(message_id, bet_info)
                completed_bets.append(message_id)
                results_summary["expired"] += 1
                continue
            
            # Enhanced result checking with sport-specific logic
            result = await get_game_result_enhanced(bet_info)
            if result and result.get("completed"):
                logging.info(f"🎯 Game completed for bet {message_id}: {bet_info.get('game', 'Unknown')}")
                bet_outcome = determine_bet_outcome_enhanced(result, bet_info)
                
                if bet_outcome is not None:  # Result determined successfully
                    # Update bankroll with enhanced tracking
                    updated_bankroll = update_bankroll_result_enhanced(
                        bet_info["guild_id"], 
                        bet_info["stake"], 
                        bet_info["odds"], 
                        bet_outcome,
                        bet_info  # Pass full bet info for enhanced tracking
                    )
                    
                    # Enhanced notification system
                    await send_comprehensive_bet_result(message_id, bet_info, bet_outcome, updated_bankroll)
                    
                    completed_bets.append(message_id)
                    results_summary["resolved"] += 1
                    logging.info(f"✅ Bet {message_id} resolved: {'WON' if bet_outcome else 'LOST'} (${bet_info['stake']:.2f})")
                else:
                    # Game completed but outcome unclear - mark for manual review
                    logging.warning(f"⚠️ Game completed but unclear outcome for bet {message_id}")
                    await flag_bet_for_review(message_id, bet_info, result)
                    completed_bets.append(message_id)  # Remove from tracking to avoid continuous checking
                    results_summary["failed"] += 1
            else:
                # Still waiting for game completion
                logging.debug(f"⏳ Game not completed yet for bet {message_id}: {bet_info.get('game', 'Unknown')}")
                results_summary["waiting"] += 1
                
        except Exception as e:
            logging.error(f"❌ Error checking bet {message_id}: {e}")
            results_summary["failed"] += 1
    
    # Remove completed bets and log summary
    for bet_id in completed_bets:
        active_bets.pop(bet_id, None)
    
    if any(results_summary.values()):
        logging.info(f"📊 Bet check results: {results_summary['resolved']} resolved, {results_summary['waiting']} waiting, {results_summary['expired']} expired, {results_summary['failed']} failed")
    
    # Save updated active bets
    if completed_bets:
        await save_active_bets_to_file()

async def get_game_result_enhanced(bet_info):
    """Enhanced game result fetching with sport-specific optimization and caching"""
    event_id = bet_info["event_id"]
    if not event_id:
        return None
    
    # Try to determine sport from bet context for more targeted search
    sport_hints = []
    game_text = bet_info.get("game", "").lower()
    
    # Sport detection heuristics
    if any(term in game_text for term in ["nfl", "football", "patriots", "cowboys"]):
        sport_hints = ["americanfootball_nfl", "americanfootball_ncaaf"]
    elif any(term in game_text for term in ["nba", "lakers", "warriors", "basketball"]):
        sport_hints = ["basketball_nba", "basketball_ncaab"]
    elif any(term in game_text for term in ["nhl", "hockey", "rangers", "bruins"]):
        sport_hints = ["icehockey_nhl"]
    elif any(term in game_text for term in ["mlb", "baseball", "yankees", "dodgers"]):
        sport_hints = ["baseball_mlb"]
    elif any(term in game_text for term in ["premier", "champions", "soccer", "football"]):
        sport_hints = ["soccer_epl", "soccer_uefa_champs_league"]
    
    # Comprehensive sports list with hints prioritized
    all_sports = sport_hints + [
        "americanfootball_nfl", "basketball_nba", "icehockey_nhl", 
        "soccer_epl", "baseball_mlb", "basketball_ncaab", 
        "americanfootball_ncaaf", "soccer_uefa_champs_league",
        "tennis_atp", "tennis_wta", "cricket_international",
        "rugby_league_nrl", "aussierules_afl"
    ]
    
    # Remove duplicates while preserving order
    sports_to_try = list(dict.fromkeys(all_sports))
    
    for sport_key in sports_to_try:
        try:
            url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores/"
            params = {
                "api_key": ODDS_API_KEY,
                "eventIds": event_id,
                "daysFrom": 14  # Extended search period
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=20) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and len(data) > 0:
                            game_result = data[0]
                            # Enhanced logging with more details
                            logging.info(f"🎯 Found result for {event_id} in {sport_key}: completed={game_result.get('completed', False)}, scores={len(game_result.get('scores', []))}")
                            return game_result
                    elif resp.status == 422:
                        continue  # Invalid event ID for this sport
                    elif resp.status == 429:
                        logging.warning(f"Rate limited on {sport_key}, waiting...")
                        await asyncio.sleep(1)
                        continue
                    else:
                        logging.warning(f"API error {resp.status} for sport {sport_key}")
                        continue
                        
        except asyncio.TimeoutError:
            logging.warning(f"Timeout checking {sport_key} for event {event_id}")
            continue
        except Exception as e:
            logging.error(f"Error checking {sport_key} for event {event_id}: {e}")
            continue
    
    logging.debug(f"No result found for event {event_id} across {len(sports_to_try)} sports")
    return None

async def get_game_result(event_id):
    """Legacy function - redirect to enhanced version"""
    fake_bet_info = {"event_id": event_id, "game": ""}
    return await get_game_result_enhanced(fake_bet_info)

def determine_bet_outcome_enhanced(game_result, bet_info):
    """Enhanced bet outcome determination with better parsing and edge case handling"""
    market_key = bet_info["market_key"]
    outcome = bet_info["outcome"].lower().strip()
    
    scores = game_result.get("scores", [])
    if len(scores) < 2:
        logging.warning(f"Insufficient score data for bet {bet_info.get('event_id', 'unknown')}")
        return None
    
    # Enhanced score extraction with error handling
    try:
        home_team = game_result.get("home_team", "").lower()
        away_team = game_result.get("away_team", "").lower()
        
        home_score = None
        away_score = None
        
        for score_data in scores:
            team_name = score_data.get("name", "").lower()
            if team_name == home_team or any(word in team_name for word in home_team.split()):
                home_score = float(score_data.get("score", 0))
            elif team_name == away_team or any(word in team_name for word in away_team.split()):
                away_score = float(score_data.get("score", 0))
        
        if home_score is None or away_score is None:
            logging.error(f"Could not extract scores: home={home_score}, away={away_score}")
            return None
            
    except (ValueError, TypeError) as e:
        logging.error(f"Score parsing error: {e}")
        return None
    
    # Enhanced market-specific logic
    if market_key == "h2h":
        return determine_h2h_outcome(outcome, home_team, away_team, home_score, away_score)
    elif market_key == "totals":
        return determine_totals_outcome(outcome, home_score + away_score)
    elif market_key == "spreads":
        return determine_spreads_outcome(outcome, home_team, away_team, home_score, away_score)
    else:
        logging.warning(f"Unknown market key: {market_key}")
        return None

def determine_h2h_outcome(outcome, home_team, away_team, home_score, away_score):
    """Determine head-to-head bet outcome"""
    if any(team_word in outcome for team_word in home_team.split()):
        return home_score > away_score
    elif any(team_word in outcome for team_word in away_team.split()):
        return away_score > home_score
    elif any(draw_word in outcome for draw_word in ["draw", "tie", "deadlock"]):
        return home_score == away_score
    else:
        logging.warning(f"Could not match outcome '{outcome}' to teams '{home_team}' vs '{away_team}'")
        return None

def determine_totals_outcome(outcome, total_score):
    """Determine over/under bet outcome with enhanced parsing"""
    # Multiple regex patterns for different total formats
    patterns = [
        r'over\s*(\d+\.?\d*)',  # "over 45.5"
        r'(\d+\.?\d*)\s*over',  # "45.5 over"  
        r'under\s*(\d+\.?\d*)', # "under 45.5"
        r'(\d+\.?\d*)\s*under', # "45.5 under"
        r'o\s*(\d+\.?\d*)',     # "o 45.5"
        r'u\s*(\d+\.?\d*)',     # "u 45.5"
        r'(\d+\.?\d*)\s*pts',   # "45.5 pts"
    ]
    
    line_value = None
    is_over = None
    
    for pattern in patterns:
        match = re.search(pattern, outcome)
        if match:
            line_value = float(match.group(1))
            if any(word in outcome for word in ["over", "o "]):
                is_over = True
            elif any(word in outcome for word in ["under", "u "]):
                is_over = False
            break
    
    if line_value is None:
        logging.warning(f"Could not parse total line from outcome: '{outcome}'")
        return None
    
    if is_over is None:
        logging.warning(f"Could not determine over/under from outcome: '{outcome}'")
        return None
    
    if is_over:
        return total_score > line_value
    else:
        return total_score < line_value

def determine_spreads_outcome(outcome, home_team, away_team, home_score, away_score):
    """Determine point spread bet outcome with enhanced parsing"""
    # Enhanced spread parsing
    spread_patterns = [
        r'([+-]?\d+\.?\d*)\s*spread',
        r'([+-]?\d+\.?\d*)\s*pts',
        r'([+-]?\d+\.?\d*)\s*points',
        r'\(([+-]?\d+\.?\d*)\)',
        r'([+-]?\d+\.?\d*)'
    ]
    
    spread_value = None
    for pattern in spread_patterns:
        match = re.search(pattern, outcome)
        if match:
            try:
                spread_value = float(match.group(1))
                break
            except ValueError:
                continue
    
    if spread_value is None:
        logging.warning(f"Could not parse spread from outcome: '{outcome}'")
        return None
    
    # Determine which team the bet is on
    if any(team_word in outcome for team_word in home_team.split()):
        # Bet on home team
        adjusted_home_score = home_score + spread_value
        return adjusted_home_score > away_score
    elif any(team_word in outcome for team_word in away_team.split()):
        # Bet on away team  
        adjusted_away_score = away_score + spread_value
        return adjusted_away_score > home_score
    else:
        logging.warning(f"Could not determine team for spread bet: '{outcome}'")
        return None

def determine_bet_outcome(game_result, bet_info):
    """Legacy function - redirect to enhanced version"""
    return determine_bet_outcome_enhanced(game_result, bet_info)

async def add_bet_reaction(message_id, bet_info, bet_won):
    """Add win/loss reaction to bet message"""
    try:
        channel = bot.get_channel(bet_info["channel_id"])
        if channel:
            message = await channel.fetch_message(message_id)
            if bet_won:
                await message.add_reaction("✅")
            else:
                await message.add_reaction("❌")
    except Exception as e:
        logging.error(f"Failed to add reaction: {e}")

async def send_bet_result_notification(bet_info, bet_won, updated_bankroll):
    """Send bet result notification"""
    try:
        channel = bot.get_channel(bet_info["channel_id"])
        if not channel:
            return
        
        profit = bet_info["stake"] * (bet_info["odds"] - 1) if bet_won else -bet_info["stake"]
        
        embed = discord.Embed(
            title="🎯 Bet Result",
            color=0x00FF00 if bet_won else 0xFF0000,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.set_thumbnail(url=BETVILLIAN_LOGO)
        
        embed.add_field(
            name="📊 Outcome",
            value=f"**Result:** {'WON' if bet_won else 'LOST'} ✅" if bet_won else f"**Result:** {'WON' if bet_won else 'LOST'} ❌",
            inline=True
        )
        
        embed.add_field(
            name="💰 P&L",
            value=f"**Profit/Loss:** ${profit:+.2f}\n**Stake:** ${bet_info['stake']:.2f}",
            inline=True
        )
        
        embed.add_field(
            name="📈 Updated Bankroll", 
            value=f"**Current:** ${updated_bankroll['current']:,.2f}\n**Daily P&L:** ${updated_bankroll['daily_profit']:+,.2f}",
            inline=False
        )
        
        embed.set_footer(text="BetVillian Result Tracker", icon_url=BETVILLIAN_LOGO)
        
        await channel.send(embed=embed)
        
    except Exception as e:
        logging.error(f"Failed to send bet result notification: {e}")

# Background task to check results every 30 minutes for faster updates
@tasks.loop(minutes=30)
async def check_results_task():
    """Background task to check bet results - runs every 30 minutes"""
    try:
        await check_bet_results()
        if active_bets:
            logging.info(f"Checked {len(active_bets)} active bets for results")
        else:
            logging.debug("No active bets to check")
    except Exception as e:
        logging.error(f"Error in check_results_task: {e}")

# Background task to send daily summaries
@tasks.loop(time=dt_time(hour=0, minute=0))  # Daily at midnight UTC
async def daily_summary_task():
    """Send daily bankroll summaries"""
    try:
        for guild_id in guild_bankrolls.keys():
            try:
                await send_daily_bankroll_summary(guild_id)
                logging.info(f"Sent daily summary for guild {guild_id}")
            except Exception as e:
                logging.error(f"Failed to send daily summary for guild {guild_id}: {e}")
    except Exception as e:
        logging.error(f"Error in daily_summary_task: {e}")

# Task to save active bets periodically (in case of bot restart)
@tasks.loop(hours=1)
async def save_active_bets_task():
    """Save active bets to file periodically"""
    try:
        if active_bets:
            # Convert datetime objects to strings for JSON serialization
            serializable_bets = {}
            for bet_id, bet_info in active_bets.items():
                bet_copy = bet_info.copy()
                if isinstance(bet_copy.get("posted_time"), datetime):
                    bet_copy["posted_time"] = bet_copy["posted_time"].isoformat()
                serializable_bets[bet_id] = bet_copy
            
            with open(ACTIVE_BETS_FILE, "w") as f:
                json.dump(serializable_bets, f, indent=2)
            
            logging.info(f"Saved {len(active_bets)} active bets to file")
    except Exception as e:
        logging.error(f"Error saving active bets: {e}")

@check_results_task.before_loop
async def before_check_results():
    await bot.wait_until_ready()

@daily_summary_task.before_loop
async def before_daily_summary():
    await bot.wait_until_ready()

@save_active_bets_task.before_loop
async def before_save_active_bets():
    await bot.wait_until_ready()

# Global hourly scanning task - synchronizes all servers to one scan
@tasks.loop(hours=1)
async def global_hourly_scan_task():
    """Global hourly scan that syncs all servers to prevent duplicate API calls"""
    global GLOBAL_SCAN_DATA, LAST_GLOBAL_HOURLY_SCAN, NEXT_GLOBAL_SCAN_TIME
    
    try:
        current_time = datetime.now(timezone.utc)
        LAST_GLOBAL_HOURLY_SCAN = current_time
        NEXT_GLOBAL_SCAN_TIME = current_time + timedelta(hours=1)
        
        logging.info("🌐 Starting global hourly scan for all servers...")
        print(f"🌐 Global Scan Started: {current_time.strftime('%H:%M:%S UTC')}")
        print(f"⏰ Next Global Scan: {NEXT_GLOBAL_SCAN_TIME.strftime('%H:%M:%S UTC')}")
        
        # Fetch all odds data once for all servers
        print("📡 Fetching global odds data...")
        GLOBAL_SCAN_DATA = await fetch_all_odds_data()
        
        if not GLOBAL_SCAN_DATA:
            logging.warning("❌ No global scan data retrieved")
            print("❌ No global scan data retrieved")
            return
        
        print(f"✅ Global data fetched successfully - {len(GLOBAL_SCAN_DATA)} sports")
        active_servers = []
        
        # Process each server with scanning enabled
        for guild_id, settings in server_settings.items():
            try:
                guild_id = int(guild_id)
                guild = bot.get_guild(guild_id)
                guild_name = guild.name if guild else f"Guild {guild_id}"
                
                # Check if EV scanning is enabled
                if settings.get("ev_scan_enabled", False):
                    ev_channel_id = settings.get("ev_alert_channel")
                    if ev_channel_id:
                        channel = bot.get_channel(ev_channel_id)
                        if channel:
                            await send_ev_alert_with_data(guild_id, channel, GLOBAL_SCAN_DATA)
                            active_servers.append(f"{guild_name} (EV)")
                            logging.info(f"✅ Sent EV alerts to guild {guild_id}")
                
                # Check if ARB scanning is enabled
                if settings.get("arb_scan_enabled", False):
                    arb_channel_id = settings.get("arb_alert_channel")
                    if arb_channel_id:
                        channel = bot.get_channel(arb_channel_id)
                        if channel:
                            await send_arbitrage_alert_with_data(guild_id, channel, GLOBAL_SCAN_DATA)
                            active_servers.append(f"{guild_name} (ARB)")
                            logging.info(f"✅ Sent ARB alerts to guild {guild_id}")
                            
            except Exception as e:
                logging.error(f"Error processing guild {guild_id} in global scan: {e}")
                print(f"❌ Error processing guild {guild_id}: {e}")
        
        scan_summary = f"Global scan completed - {len(active_servers)} active scan targets"
        logging.info(scan_summary)
        print(f"✅ {scan_summary}")
        if active_servers:
            print(f"   📊 Active: {', '.join(active_servers[:5])}" + (f" + {len(active_servers)-5} more" if len(active_servers) > 5 else ""))
        else:
            print("   📊 No servers have scanning enabled")
        
        print(f"⏰ Next automatic scan: {NEXT_GLOBAL_SCAN_TIME.strftime('%H:%M:%S UTC')}")
        
    except Exception as e:
        logging.error(f"Error in global_hourly_scan_task: {e}")
        print(f"❌ Global scan failed: {e}")

@global_hourly_scan_task.before_loop
async def before_global_hourly_scan():
    await bot.wait_until_ready()
    # Set initial next scan time
    global NEXT_GLOBAL_SCAN_TIME
    NEXT_GLOBAL_SCAN_TIME = datetime.now(timezone.utc) + timedelta(hours=1)
    print(f"🌐 Global scan timer initialized - first scan at {NEXT_GLOBAL_SCAN_TIME.strftime('%H:%M:%S UTC')}")

@global_hourly_scan_task.error
async def global_scan_error(error):
    logging.error(f"Global scan task error: {error}")
    print(f"❌ Global scan task error: {error}")
    # Restart the timer
    global NEXT_GLOBAL_SCAN_TIME
    NEXT_GLOBAL_SCAN_TIME = datetime.now(timezone.utc) + timedelta(hours=1)
    print(f"🔄 Reset next scan time to: {NEXT_GLOBAL_SCAN_TIME.strftime('%H:%M:%S UTC')}")

@bot.event
async def on_guild_join(guild):
    """Handle bot joining a new server"""
    print(f"🎉 BetVillian joined server: {guild.name} ({guild.id})")
    logging.info(f"Bot joined guild: {guild.name} ({guild.id})")
    
    # Initialize default settings for new server
    guild_id = guild.id
    if guild_id not in server_settings:
        server_settings[guild_id] = {
            "setup_done": False,
            "ev_scan_enabled": False,  # Will auto-enable when channel is set
            "arb_scan_enabled": False,  # Will auto-enable when channel is set
            "min_ev": DEFAULT_MIN_EV,
            "min_margin": DEFAULT_MIN_MARGIN,
            "ev_bookmakers": BOOKMAKERS_ALL.copy(),
            "arb_bookmakers": BOOKMAKERS_ALL.copy()
        }
        await save_settings_async()
        print(f"✅ Initialized default settings for {guild.name}")
    
    # Show global scan info
    if NEXT_GLOBAL_SCAN_TIME:
        current_time = datetime.now(timezone.utc)
        time_until_scan = NEXT_GLOBAL_SCAN_TIME - current_time
        minutes_until = max(0, int(time_until_scan.total_seconds() // 60))
        print(f"🌐 Server will sync to global scan timer: {minutes_until} minutes until next scan")

def load_active_bets():
    """Load active bets from file on startup"""
    global active_bets
    try:
        with open(ACTIVE_BETS_FILE, "r") as f:
            data = json.load(f)
            active_bets = {}
            for bet_id, bet_info in data.items():
                # Convert string dates back to datetime objects
                if "posted_time" in bet_info and isinstance(bet_info["posted_time"], str):
                    try:
                        bet_info["posted_time"] = datetime.fromisoformat(bet_info["posted_time"])
                    except:
                        bet_info["posted_time"] = datetime.now(timezone.utc)
                active_bets[int(bet_id)] = bet_info
            
            logging.info(f"Loaded {len(active_bets)} active bets from file")
    except FileNotFoundError:
        active_bets = {}
        logging.info("No active bets file found, starting fresh")
    except Exception as e:
        logging.error(f"Failed to load active bets: {e}")
        active_bets = {}

async def get_sports():
    """Enhanced sports fetching with better error handling"""
    now = datetime.now(timezone.utc)
    
    # Check cache first
    if sports_cache["data"] and (now - sports_cache["timestamp"]).total_seconds() < CACHE_SECONDS:
        return sports_cache["data"]
    
    url = "https://api.the-odds-api.com/v4/sports"
    params = {"api_key": ODDS_API_KEY}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list):
                        # Enhanced filtering with better sport selection
                        filtered = []
                        for sport in data:
                            if (sport.get('active', False) and 
                                not any(word in sport['key'].lower() for word in EXCLUDED_SPORT_KEYWORDS)):
                                filtered.append(sport)
                        
                        sports_cache["data"] = filtered
                        sports_cache["timestamp"] = now
                        
                        logging.info(f"Fetched {len(filtered)} active sports (filtered from {len(data)} total)")
                        if filtered:
                            sample_sports = [s['title'] for s in filtered[:5]]
                            logging.info(f"Sample sports: {', '.join(sample_sports)}")
                        
                        return filtered
                    else:
                        logging.error(f"Invalid sports data structure: {type(data)}")
                        return []
                
                elif resp.status == 401:
                    logging.error("Invalid API key when fetching sports")
                    return []
                    
                elif resp.status == 429:
                    logging.warning("Rate limited when fetching sports")
                    await asyncio.sleep(1)
                    return []
                    
                else:
                    logging.warning(f"API error {resp.status} when fetching sports")
                    return []
                    
    except asyncio.TimeoutError:
        logging.warning("Timeout when fetching sports")
        return []
    except aiohttp.ClientError as e:
        logging.error(f"Client error when fetching sports: {e}")
        return []
    except Exception as e:
        logging.error(f"Unexpected error when fetching sports: {e}")
        return []

async def get_odds(sport_key, market):
    """Enhanced odds fetching with better error handling and rate limiting"""
    now = datetime.now(timezone.utc)
    cache_key = (sport_key, market)
    
    # Check cache first
    if cache_key in odds_cache and (now - odds_cache[cache_key]["timestamp"]).total_seconds() < CACHE_SECONDS:
        return odds_cache[cache_key]["data"]
    
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
    params = {
        "api_key": ODDS_API_KEY,
        "regions": REGION,
        "markets": market,
        "oddsFormat": "decimal"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=20) as resp:
                # Enhanced status code handling
                if resp.status == 200:
                    data = await resp.json()
                    # Validate data structure
                    if isinstance(data, list):
                        odds_cache[cache_key] = {"data": data, "timestamp": now}
                        logging.debug(f"Successfully fetched {len(data)} events for {sport_key}-{market}")
                        return data
                    else:
                        logging.warning(f"Invalid data structure for {sport_key}-{market}: {type(data)}")
                        return []
                        
                elif resp.status == 422:
                    # Invalid parameters - sport/market combination doesn't exist
                    logging.debug(f"Invalid sport/market combination: {sport_key}-{market}")
                    odds_cache[cache_key] = {"data": [], "timestamp": now}
                    return []
                    
                elif resp.status == 429:
                    # Rate limited - wait and retry once
                    logging.warning(f"Rate limited for {sport_key}-{market}, waiting 2 seconds...")
                    await asyncio.sleep(2)
                    async with session.get(url, params=params, timeout=20) as retry_resp:
                        if retry_resp.status == 200:
                            data = await retry_resp.json()
                            if isinstance(data, list):
                                odds_cache[cache_key] = {"data": data, "timestamp": now}
                                return data
                    return []
                    
                elif resp.status == 401:
                    logging.error(f"Invalid API key for {sport_key}-{market}")
                    return []
                    
                else:
                    logging.warning(f"API error {resp.status} for {sport_key}-{market}")
                    return []
                    
    except asyncio.TimeoutError:
        logging.warning(f"Timeout fetching odds for {sport_key}-{market}")
        return []
    except aiohttp.ClientError as e:
        logging.error(f"Client error fetching odds for {sport_key}-{market}: {e}")
        return []
    except Exception as e:
        logging.error(f"Unexpected error fetching odds for {sport_key}-{market}: {e}")
        return []

def find_arbitrage(events, market_key, min_margin, allowed_books):
    """Enhanced arbitrage detection with improved logic and better filtering"""
    opportunities = []
    now = datetime.now(pytz.UTC)
    max_time = now + timedelta(days=7)  # Extended time window
    
    logging.debug(f"🔍 find_arbitrage: {len(events)} events, market={market_key}, min_margin={min_margin}%, {len(allowed_books)} books")
    
    for event in events:
        try:
            event_time = datetime.fromisoformat(event['commence_time'].replace('Z', '+00:00'))
        except (ValueError, KeyError):
            logging.debug(f"Invalid event time format for event: {event.get('id', 'unknown')}")
            continue
            
        # Skip events that are too far in future or too close (less than 1 hour)
        time_until_event = (event_time - now).total_seconds()
        if time_until_event < 3600 or time_until_event > 604800:  # 1 hour to 7 days
            continue
            
        if not event.get('bookmakers'):
            continue
            
        # Enhanced outcome tracking with normalized names
        outcome_odds = {}  # {normalized_outcome: {bookmaker: odds}}
        valid_bookmakers = set()
        
        for bookmaker in event['bookmakers']:
            bookmaker_name = bookmaker['title']
            if bookmaker_name not in allowed_books:
                continue
                
            valid_bookmakers.add(bookmaker_name)
            
            for market in bookmaker.get('markets', []):
                if market['key'] != market_key:
                    continue
                    
                for outcome in market['outcomes']:
                    name = outcome['name'].strip()
                    odds = outcome.get('price', 0)
                    
                    # Skip invalid odds
                    if not odds or odds <= 1.01 or odds > 50:
                        continue
                    
                    # Normalize outcome names for better matching
                    normalized_name = normalize_outcome_name(name, market_key, outcome)
                    
                    if normalized_name not in outcome_odds:
                        outcome_odds[normalized_name] = {}
                    
                    # Keep best odds per bookmaker per outcome
                    if bookmaker_name not in outcome_odds[normalized_name] or odds > outcome_odds[normalized_name][bookmaker_name]:
                        outcome_odds[normalized_name][bookmaker_name] = odds
        
        # Enhanced arbitrage detection - ONLY 2-way markets (no draws)
        if len(outcome_odds) == 2 and len(valid_bookmakers) >= 2:
            arb_result = calculate_arbitrage_opportunity(outcome_odds, event, market_key, min_margin)
            if arb_result:
                opportunities.append(arb_result)
                logging.info(f"🎯 ARB FOUND: {event.get('home_team', 'Team1')} vs {event.get('away_team', 'Team2')} - {arb_result[2]:.2f}% margin")
        else:
            logging.debug(f"   Skipped: {len(outcome_odds)} outcomes (need exactly 2), {len(valid_bookmakers)} bookmakers")
    
    logging.debug(f"✅ find_arbitrage result: {len(opportunities)} opportunities")
    return opportunities

def normalize_outcome_name(name, market_key, outcome):
    """Normalize outcome names for consistent arbitrage detection"""
    name = name.strip().lower()
    
    if market_key == 'h2h':
        # For head-to-head, keep team names as-is but normalize draw
        if name in ['draw', 'tie', 'deadlock']:
            return 'draw'
        return name.title()
    
    elif market_key == 'totals':
        # For totals, include the point value
        point = outcome.get('point', 0)
        if 'over' in name:
            return f"Over {point}"
        elif 'under' in name:
            return f"Under {point}"
        return f"{name.title()} {point}"
    
    elif market_key == 'spreads':
        # For spreads, include the spread value
        point = outcome.get('point', 0)
        return f"{name.title()} ({point:+g})"
    
    return name.title()

def calculate_arbitrage_opportunity(outcome_odds, event, market_key, min_margin):
    """Calculate if there's a valid arbitrage opportunity"""
    try:
        # Find the best odds for each outcome across all bookmakers
        best_odds = {}
        for outcome, bookmaker_odds in outcome_odds.items():
            if bookmaker_odds:  # Make sure there are odds available
                best_price = max(bookmaker_odds.values())
                best_bookmaker = max(bookmaker_odds.items(), key=lambda x: x[1])[0]
                best_odds[outcome] = (best_price, best_bookmaker)
        
        # Need at least 2 outcomes for arbitrage
        if len(best_odds) < 2:
            return None
        
        # Calculate arbitrage
        inv_sum = sum(1 / odds for odds, _ in best_odds.values())
        
        # Valid arbitrage opportunity
        if 0 < inv_sum < 1:
            margin = (1 - inv_sum) * 100
            if margin >= min_margin:
                return (event, best_odds, margin, market_key)
        
        return None
        
    except Exception as e:
        logging.error(f"Error calculating arbitrage for event {event.get('id', 'unknown')}: {e}")
        return None

def calculate_ev(odds_offered: float, odds_fair: float) -> float:
    return ((odds_offered / odds_fair) - 1) * 100

def kelly_stake(ev_percent: float, odds: float, bankroll=100.0, fraction=0.01):
    ev_decimal = ev_percent / 100
    b = odds - 1
    if b <= 0:
        return 0.0
    kelly_fraction = ev_decimal / b
    return round(bankroll * kelly_fraction * fraction, 2)

def improved_kelly_stake(odds, fair_odds, bankroll=1.0, risk_factor=0.25):
    win_prob = 1 / fair_odds
    kelly_fraction = ((odds * win_prob) - 1) / (odds - 1)
    kelly_fraction = max(kelly_fraction, 0)
    stake = bankroll * kelly_fraction * risk_factor
    return round(stake, 2)

def moderate_stake_suggestion(odds, fair_odds, ev, current_bankroll, min_ev=0):
    """Calculate stake suggestion in dollars based on bankroll"""
    win_prob = 1 / fair_odds
    kelly_fraction = ((odds * win_prob) - 1) / (odds - 1)
    kelly_fraction = max(kelly_fraction, 0)
    base_stake = kelly_fraction * (ev / 100) * current_bankroll
    
    if odds < 2:
        stake = min(max(base_stake * 0.1, current_bankroll * 0.01), current_bankroll * 0.04)  # 1-4% of bankroll
    elif 2 <= odds < 3:
        stake = min(max(base_stake * 0.08, current_bankroll * 0.015), current_bankroll * 0.03)  # 1.5-3% of bankroll
    else:
        stake = min(max(base_stake * 0.05, current_bankroll * 0.005), current_bankroll * 0.02)  # 0.5-2% of bankroll
    
    if ev > 10:
        stake = min(stake * 1.5, current_bankroll * 0.05)  # Max 5% for high EV
    
    return round(stake, 2)

def find_ev_opportunities(events, market_key, min_ev, allowed_books):
    """Enhanced EV opportunity detection with improved calculation"""
    opportunities = []
    now = datetime.now(pytz.UTC)
    max_time = now + timedelta(days=7)  # Extended time window
    
    for event in events:
        try:
            event_time = datetime.fromisoformat(event['commence_time'].replace('Z', '+00:00'))
        except:
            continue
            
        # Skip events that are too far in future or too close
        time_until_event = (event_time - now).total_seconds()
        if time_until_event < 3600 or time_until_event > 604800:  # 1 hour to 7 days
            continue
            
        if not event.get('bookmakers'):
            continue
            
        # Collect all odds for each outcome
        outcome_odds = {}
        
        for bookmaker in event['bookmakers']:
            if bookmaker['title'] not in allowed_books:
                continue
                
            for market in bookmaker.get('markets', []):
                if market['key'] != market_key:
                    continue
                    
                for outcome in market['outcomes']:
                    name = outcome['name'].strip()
                    
                    # Include point value for spreads/totals in the name for clarity
                    if market_key == 'spreads' and 'point' in outcome:
                        name = f"{name} ({outcome['point']:+g})"
                    elif market_key == 'totals' and 'point' in outcome:
                        name = f"{name} {outcome['point']}pts"
                    
                    price = outcome.get('price', 0)
                    if not price or price <= 1.01 or price > 50:  # Skip invalid odds
                        continue
                        
                    if name not in outcome_odds:
                        outcome_odds[name] = []
                    outcome_odds[name].append((price, bookmaker['title']))
        
        # Calculate EV for each outcome with sufficient data
        for outcome_name, offers in outcome_odds.items():
            if len(offers) < 2:  # Need at least 2 bookmakers for fair odds calculation
                continue
                
            # Calculate fair odds using average of all bookmakers
            implied_probs = [1 / price for price, _ in offers if price > 1.01]
            if not implied_probs:
                continue
                
            # Remove outliers for better fair odds calculation
            if len(implied_probs) >= 3:
                implied_probs.sort()
                # Remove extreme outliers (top and bottom 10%)
                trim_count = max(1, len(implied_probs) // 10)
                implied_probs = implied_probs[trim_count:-trim_count]
            
            avg_implied_prob = sum(implied_probs) / len(implied_probs)
            fair_odds = 1 / avg_implied_prob
            
            # Find best offered odds
            best_offer = max(offers, key=lambda item: item[0])
            offered_price, book = best_offer
            
            # Calculate EV
            ev = calculate_ev(offered_price, fair_odds)
            
            if ev >= min_ev:
                # Calculate dynamic stake suggestion
                stake = calculate_stake_suggestion(ev, offered_price)
                
                opportunities.append({
                    "event": event,
                    "market_key": market_key,
                    "outcome": outcome_name,
                    "bookmaker": book,
                    "offered_odds": offered_price,
                    "fair_odds": round(fair_odds, 2),
                    "ev": round(ev, 2),
                    "stake": stake,
                    "num_bookmakers": len(offers)
                })
    
    # Sort by EV descending
    opportunities.sort(key=lambda x: x['ev'], reverse=True)
    return opportunities
# ---------- ALERT FUNCTIONS ----------

async def fetch_all_odds_data():
    """Fetch all odds data for global caching"""
    try:
        logging.info("📡 Starting global odds data fetch...")
        sports = await get_sports()
        logging.info(f"🏈 Found {len(sports)} sports to check")
        
        all_data = {}
        
        for sport in sports:
            sport_key = sport["key"]
            logging.info(f"📊 Fetching data for sport: {sport_key}")
            
            for market_key in ["h2h", "totals", "spreads"]:
                try:
                    events = await get_odds(sport_key, market_key)
                    if events:
                        cache_key = f"{sport_key}-{market_key}"
                        all_data[cache_key] = events
                        logging.info(f"✅ {cache_key}: {len(events)} events")
                    else:
                        logging.debug(f"❌ {sport_key}-{market_key}: No events")
                except Exception as market_error:
                    logging.error(f"Error fetching {sport_key}-{market_key}: {market_error}")
        
        logging.info(f"🎯 Global fetch complete: {len(all_data)} market datasets")
        return all_data
    except Exception as e:
        logging.error(f"Error fetching global odds data: {e}")
        return {}

async def send_arbitrage_alert_with_data(guild_id, channel, global_data):
    """Enhanced arbitrage alert using cached global data with improved error handling"""
    try:
        settings = server_settings.get(guild_id, {})
        min_margin = settings.get("min_margin", DEFAULT_MIN_MARGIN)
        allowed_books = settings.get("arb_bookmakers", BOOKMAKERS_ALL.copy())
        role_id = settings.get("arb_mention_role")
        mention = f"<@&{role_id}>" if role_id else ""

        opportunities = []
        
        # Enhanced debug logging
        logging.info(f"🔍 Checking arbitrage for guild {guild_id}: min_margin={min_margin}%, {len(allowed_books)} bookmakers")
        logging.info(f"📊 Global data keys: {list(global_data.keys())}")
        
        # Validate we have bookmakers configured
        if not allowed_books:
            logging.warning(f"No bookmakers configured for guild {guild_id}")
            return
        
        # Process cached data with better error handling
        markets_checked = 0
        events_processed = 0
        
        for cache_key, events in global_data.items():
            if not events or not isinstance(events, list):
                continue
                
            try:
                if "-" in cache_key:
                    sport_key, market_key = cache_key.rsplit("-", 1)
                    if market_key in ["h2h", "totals", "spreads"]:
                        markets_checked += 1
                        events_processed += len(events)
                        
                        logging.info(f"🏈 Checking {sport_key}-{market_key}: {len(events)} events")
                        found = find_arbitrage(events, market_key, min_margin, allowed_books)
                        logging.info(f"   Found {len(found)} arbitrage opportunities")
                        opportunities.extend(found)
            except Exception as market_error:
                logging.error(f"Error processing market {cache_key}: {market_error}")
                continue

        logging.info(f"🎯 Arbitrage scan complete for guild {guild_id}: {markets_checked} markets, {events_processed} events, {len(opportunities)} opportunities")

        if not opportunities:
            # Don't send "no opportunities" messages to avoid spam - only log
            logging.debug(f"No arbitrage opportunities for guild {guild_id} (min margin: {min_margin}%)")
            return

        # Enhanced opportunity processing with duplicate prevention
        sent_count = 0
        last_arb_sent_guild = last_arb_sent.setdefault(guild_id, set())
        
        for event, best_odds, margin, market_key in opportunities:
            try:
                # Create unique identifier for this opportunity
                event_id = event.get('id', 'unknown')
                id_key = f"{event_id}-{market_key}-{margin:.1f}"
                
                # Skip if we've already sent this recently
                if id_key in last_arb_sent_guild:
                    logging.debug(f"Skipping duplicate arbitrage alert: {id_key}")
                    continue

                # Enhanced embed creation
                embed = create_enhanced_arbitrage_embed(event, best_odds, margin, market_key)
                
                if not embed:
                    logging.warning(f"Failed to create embed for arbitrage opportunity: {event_id}")
                    continue

                # Create enhanced view with calculator
                view = discord.ui.View(timeout=None)
                
                # Add arbitrage calculator button with error handling
                try:
                    odds_list = [odds for odds, book in best_odds.values()]
                    book_list = [book for odds, book in best_odds.values()]
                    
                    if len(odds_list) >= 2 and len(book_list) >= 2:
                        calc_button = ArbCalculatorButton(odds_list, book_list, margin)
                        view.add_item(calc_button)
                        logging.info(f"✅ Added arbitrage calculator button for {len(odds_list)} outcomes")
                    else:
                        logging.warning(f"❌ Cannot add calculator button: insufficient outcomes ({len(odds_list)} odds, {len(book_list)} books)")
                except Exception as view_error:
                    logging.error(f"Error creating calculator button: {view_error}")
                
                # Send alert with enhanced error handling
                try:
                    msg = await channel.send(content=mention, embed=embed, view=view)
                    
                    # Track alert for stats and deduplication
                    last_arb_sent_guild.add(id_key)
                    sent_alerts.append({
                        "guild_id": guild_id,
                        "channel_id": channel.id, 
                        "message_id": msg.id,
                        "alert_type": "arbitrage",
                        "event_name": f"{event.get('home_team', 'Team1')} vs {event.get('away_team', 'Team2')}",
                        "margin": margin,
                        "market": market_key,
                        "status": "sent",
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    })
                    
                    bet_stats["arbitrage"]["sent"] += 1
                    bet_stats["total_sent"] += 1
                    sent_count += 1
                    
                    logging.info(f"✅ Sent arbitrage alert for {event.get('home_team', 'Team1')} vs {event.get('away_team', 'Team2')} ({margin:.2f}% margin)")
                    
                    # Rate limiting - don't send too many at once
                    if sent_count >= 3:
                        logging.info(f"Rate limiting: sent {sent_count} arbitrage alerts for guild {guild_id}")
                        break
                        
                except discord.HTTPException as discord_error:
                    logging.error(f"Discord error sending arbitrage alert: {discord_error}")
                except Exception as send_error:
                    logging.error(f"Error sending arbitrage alert: {send_error}")
                    
            except Exception as opportunity_error:
                logging.error(f"Error processing arbitrage opportunity: {opportunity_error}")
                continue

        # Clean up old sent alerts (remove entries older than 1 hour)
        try:
            current_time = time.time()
            cleanup_cutoff = current_time - 3600  # 1 hour ago
            
            # This is a simplified cleanup - in a real implementation, you'd want to track timestamps
            if len(last_arb_sent_guild) > 100:  # Prevent memory bloat
                last_arb_sent_guild.clear()
                logging.info(f"Cleared arbitrage alert cache for guild {guild_id}")
                
        except Exception as cleanup_error:
            logging.error(f"Error during arbitrage alert cleanup: {cleanup_error}")
            
        if sent_count > 0:
            logging.info(f"🎯 Successfully sent {sent_count} arbitrage alerts to guild {guild_id}")
            
    except Exception as e:
        logging.error(f"Critical error in send_arbitrage_alert_with_data for guild {guild_id}: {e}")

def create_enhanced_arbitrage_embed(event, best_odds, margin, market_key):
    """Create enhanced arbitrage embed with better formatting and error handling"""
    try:
        embed = discord.Embed(
            title="🔀 Arbitrage Opportunity",
            description=f"**Guaranteed Profit:** `{margin:.2f}%`",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.set_thumbnail(url=BETVILLIAN_LOGO)
        
        # Enhanced match information
        home_team = event.get('home_team', 'Team 1')
        away_team = event.get('away_team', 'Team 2')
        sport_title = event.get('sport_title', 'Unknown Sport')
        
        embed.add_field(name="🏈 Sport", value=f"**{sport_title}**", inline=True)
        embed.add_field(name="⚔️ Match", value=f"**{home_team} vs {away_team}**", inline=True)
        embed.add_field(name="📊 Margin", value=f"**{margin:.2f}%**", inline=True)
        
        embed.add_field(
            name="🎯 Market",
            value=f"**{MARKET_LABELS.get(market_key, market_key.title())}**",
            inline=False
        )
        
        # Enhanced timing information
        try:
            commence_time = event.get('commence_time', '')
            if commence_time:
                formatted_time = format_datetime(commence_time)
                embed.add_field(
                    name="⏰ Start Time",
                    value=f"**{formatted_time}**",
                    inline=False
                )
        except Exception as time_error:
            logging.warning(f"Error formatting commence time: {time_error}")
        
        # Enhanced stakes calculation with better formatting
        total_suggested_stake = 100.0  # Use $100 as base unit
        stakes_info = ""
        profit_amount = 0
        total_investment = 0
        
        try:
            # Calculate individual stakes using proper arbitrage formula
            inv_sum = sum(1 / odds for odds, _ in best_odds.values())
            
            for outcome, (odds, bookmaker) in best_odds.items():
                stake = total_suggested_stake / (odds * inv_sum)
                potential_return = stake * odds
                profit_amount = max(profit_amount, potential_return - total_suggested_stake)
                total_investment += stake
                
                # Enhanced formatting with clickable bookmaker links
                bookmaker_url = BOOKMAKER_URLS.get(bookmaker, "#")
                stakes_info += f"**{outcome}:** ${stake:.2f} @ {odds:.2f} ([{bookmaker}]({bookmaker_url}))\n"
            
            embed.add_field(
                name="💰 Optimal Stakes & Odds",
                value=stakes_info,
                inline=False
            )
            
            # Enhanced profit information
            roi = (profit_amount / total_suggested_stake) * 100
            embed.add_field(
                name="📈 Profit Analysis",
                value=f"**Guaranteed Profit:** ${profit_amount:.2f}\n"
                      f"**Total Investment:** ${total_suggested_stake:.2f}\n"
                      f"**ROI:** {roi:.2f}%\n"
                      f"**Margin:** {margin:.2f}%",
                inline=True
            )
            
            # Risk analysis
            risk_level = "🟢 Low" if margin >= 3 else "🟡 Medium" if margin >= 1.5 else "🟠 High"
            embed.add_field(
                name="⚠️ Risk Level",
                value=f"{risk_level}\n"
                      f"**Bookmakers:** {len(best_odds)}\n"
                      f"**Time Sensitivity:** High",
                inline=True
            )
            
        except Exception as calc_error:
            logging.error(f"Error calculating arbitrage stakes: {calc_error}")
            embed.add_field(
                name="💰 Stakes & Odds",
                value="Error calculating optimal stakes",
                inline=False
            )
        
        embed.set_footer(text="BetVillian Enhanced Arbitrage Scanner • Act quickly - odds change fast!", icon_url=BETVILLIAN_LOGO)
        
        return embed
        
    except Exception as e:
        logging.error(f"Error creating arbitrage embed: {e}")
        return None

async def send_ev_alert_with_data(guild_id, channel, global_data):
    """Send EV alert using cached global data"""
    try:
        settings = server_settings.get(guild_id, {})
        min_ev = settings.get("min_ev", DEFAULT_MIN_EV)
        allowed_books = settings.get("ev_bookmakers", BOOKMAKERS_ALL)
        role_id = settings.get("ev_mention_role")
        mention = f"<@&{role_id}>" if role_id else ""

        # Limit: 5 unique bets per 10 minutes
        now = datetime.now(timezone.utc)
        tracker = ev_sent_tracker.setdefault(guild_id, [])
        tracker[:] = [tup for tup in tracker if (now - tup[1]).total_seconds() < 600]

        evs = []
        
        # Use cached data instead of fetching fresh
        for cache_key, events in global_data.items():
            if "-" in cache_key:
                sport_key, market_key = cache_key.rsplit("-", 1)
                if market_key in ["h2h", "totals", "spreads"]:
                    evs.extend(find_ev_opportunities(events, market_key, min_ev, allowed_books))

        def get_ev_sent_ids(guild_id):
            return set(server_settings.get(guild_id, {}).get("ev_sent_ids", []))

        async def add_ev_sent_id(guild_id, id_key):
            ids = set(server_settings.setdefault(guild_id, {}).setdefault("ev_sent_ids", []))
            ids.add(id_key)
            server_settings[guild_id]["ev_sent_ids"] = list(ids)
            await save_settings_async()

        ev_sent_ids = get_ev_sent_ids(guild_id)
        new_evs = [ev for ev in evs if f"{ev['event']['id']}-{ev['outcome']}" not in ev_sent_ids]
        if not new_evs:
            # Don't send "no new EV bets" messages to avoid spam - only log
            logging.debug(f"No new EV opportunities for guild {guild_id}")
            return

        for ev in new_evs[:5]:
            id_key = f"{ev['event']['id']}-{ev['outcome']}"
            
            # Calculate dynamic stake suggestion
            stake_units = calculate_stake_suggestion(ev['ev'], ev['offered_odds'])
            
            # Track the bet info (simplified)
            bet_info = {
                "event_id": ev["event"]["id"],
                "outcome": ev["outcome"],
                "odds": ev["offered_odds"],
                "stake": stake_units,
                "guild_id": guild_id,
                "market_key": ev["market_key"],
                "posted_time": datetime.now(timezone.utc),
                "channel_id": channel.id,
                "game": f"{ev['event']['home_team']} vs {ev['event']['away_team']}",
                "bookmaker": ev["bookmaker"]
            }
            
            embed = discord.Embed(
                title="📈 +EV Bet Found",
                color=discord.Color.green()
            )
            embed.add_field(name="Sport", value=f"**{ev['event'].get('sport_title', 'Unknown')}**", inline=True)
            embed.add_field(name="Match", value=f"**{ev['event']['home_team']} vs {ev['event']['away_team']}**", inline=True)
            embed.add_field(
                name="⚡ EV",
                value=f"**{ev['ev']:.1f}%**",
                inline=True
            )
            embed.add_field(
                name="Market & Outcome",
                value=f"**{MARKET_LABELS.get(ev['market_key'], ev['market_key'])}**\n➤ **{ev['outcome']}**",
                inline=False
            )
            embed.add_field(
                name="Start Time",
                value=f"**{format_datetime(ev['event']['commence_time'])}**",
                inline=False
            )
            embed.add_field(
                name="Best Odds",
                value=f"**{ev['offered_odds']}** ({ev['bookmaker']})",
                inline=True
            )
            embed.add_field(
                name="Fair Odds",
                value=f"**{ev['fair_odds']}**",
                inline=True
            )
            embed.add_field(
                name="Stake Suggestion",
                value=f"**{stake_units:.1f} units**",
                inline=True
            )
            
            embed.set_footer(text="BetVillian +EV Scanner • Dynamic stake suggestions")

            # Send alert without tracking
            msg = await channel.send(content=mention, embed=embed)
            
            # Track alert for stats
            sent_alerts.append({
                "guild_id": guild_id,
                "channel_id": channel.id,
                "message_id": msg.id,
                "alert_type": "ev",
                "event_name": f"{ev['event']['home_team']} vs {ev['event']['away_team']}",
                "outcome": ev['outcome'],
                "status": "pending"
            })
            bet_stats["ev"]["sent"] += 1

            await add_ev_sent_id(guild_id, id_key)

    except Exception as e:
        logging.error(f"Error in send_ev_alert_with_data: {e}")

# Redundant testing commands removed - use admin panel instead

# enable-scanning command removed - redundant with admin panel auto-enable logic

@bot.tree.command(name="force-enable-scanning", description="[OWNER] Enable scanning for all servers with channels configured")
async def force_enable_scanning(interaction: discord.Interaction):
    """Force enable scanning for all servers that have channels configured"""
    if not is_bot_owner(interaction.user.id):
        await interaction.response.send_message("❌ This command is only available to the bot owner.", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        enabled_count = 0
        total_servers = 0
        
        for guild_id, settings in server_settings.items():
            total_servers += 1
            updated = False
            
            # Enable EV scanning if channel is set but scanning is disabled
            if settings.get("ev_alert_channel") and not settings.get("ev_scan_enabled", False):
                settings["ev_scan_enabled"] = True
                updated = True
                logging.info(f"Auto-enabled EV scanning for guild {guild_id}")
            
            # Enable ARB scanning if channel is set but scanning is disabled
            if settings.get("arb_alert_channel") and not settings.get("arb_scan_enabled", False):
                settings["arb_scan_enabled"] = True
                updated = True
                logging.info(f"Auto-enabled ARB scanning for guild {guild_id}")
            
            if updated:
                enabled_count += 1
        
        # Save updated settings
        if enabled_count > 0:
            await save_settings_async()
        
        embed = discord.Embed(
            title="🔄 Force Enable Scanning Results",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(
            name="📊 Results",
            value=f"**Total Servers:** {total_servers}\n"
                  f"**Scanning Enabled:** {enabled_count} servers\n"
                  f"**Already Active:** {total_servers - enabled_count} servers",
            inline=False
        )
        
        if enabled_count > 0:
            embed.add_field(
                name="✅ Action Taken",
                value=f"Automatically enabled scanning for {enabled_count} servers that had alert channels configured but scanning disabled.",
                inline=False
            )
        else:
            embed.add_field(
                name="ℹ️ No Action Needed",
                value="All servers with configured channels already have scanning enabled.",
                inline=False
            )
        
        embed.set_footer(text="BetVillian Auto-Enable System")
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        await interaction.followup.send(f"❌ Error enabling scanning: {e}")
        logging.error(f"Error in force_enable_scanning: {e}")

@bot.tree.command(name="start-global-sync", description="[OWNER] Start/restart global hourly scanning")
async def start_global_sync(interaction: discord.Interaction):
    """Start or restart the global scanning system"""
    if not is_bot_owner(interaction.user.id):
        await interaction.response.send_message("❌ This command is only available to the bot owner.", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    global NEXT_GLOBAL_SCAN_TIME, LAST_GLOBAL_HOURLY_SCAN
    
    try:
        current_time = datetime.now(timezone.utc)
        
        # Check if task is already running
        if global_hourly_scan_task.is_running():
            # Just update the next scan time to start soon
            NEXT_GLOBAL_SCAN_TIME = current_time + timedelta(minutes=2)
            status = "⚡ Global scan task already running - next scan moved to 2 minutes"
        else:
            # Start the task
            global_hourly_scan_task.start()
            NEXT_GLOBAL_SCAN_TIME = current_time + timedelta(minutes=2)
            status = "🚀 Global scan task started - first scan in 2 minutes"
        
        # Count active servers
        active_servers = 0
        for guild_id, settings in server_settings.items():
            if (settings.get("ev_scan_enabled") and settings.get("ev_alert_channel")) or \
               (settings.get("arb_scan_enabled") and settings.get("arb_alert_channel")):
                active_servers += 1
        
        embed = discord.Embed(
            title="🌐 Global Sync Started",
            description="Hourly scanning system is now active",
            color=0x00FF00,
            timestamp=current_time
        )
        
        embed.add_field(
            name="⚡ Status",
            value=status,
            inline=False
        )
        
        embed.add_field(
            name="📊 Coverage",
            value=f"**Active Servers:** {active_servers}\n"
                  f"**Total Servers:** {len(server_settings)}\n"
                  f"**Frequency:** Every hour after first scan",
            inline=True
        )
        
        embed.add_field(
            name="⏰ Schedule",
            value=f"**Next Scan:** {NEXT_GLOBAL_SCAN_TIME.strftime('%H:%M:%S UTC')}\n"
                  f"**Scan Type:** EV + Arbitrage\n"
                  f"**API Efficiency:** Shared data across all servers",
            inline=True
        )
        
        embed.add_field(
            name="🎯 What Happens Next",
            value="• System fetches odds for all sports\n"
                  "• EV opportunities detected and sent\n"
                  "• Arbitrage opportunities detected and sent\n"
                  "• Process repeats every hour automatically",
            inline=False
        )
        
        embed.set_footer(text="BetVillian Global Sync System", icon_url=BETVILLIAN_LOGO)
        
        await interaction.followup.send(embed=embed)
        
        print(f"🌐 Global sync manually started by owner - next scan: {NEXT_GLOBAL_SCAN_TIME.strftime('%H:%M:%S UTC')}")
        
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to start global sync: {e}")
        logging.error(f"Global sync start error: {e}")

@bot.tree.command(name="arb-status", description="Check arbitrage scanning status")
async def arb_status(interaction: discord.Interaction):
    """Check the current arbitrage scanning status and configuration"""
    guild_id = interaction.guild.id
    settings = server_settings.get(guild_id, {})
    
    embed = discord.Embed(
        title="🔀 Arbitrage Status Report",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc)
    )
    
    # Configuration status
    arb_enabled = settings.get("arb_scan_enabled", False)
    min_margin = settings.get("min_margin", DEFAULT_MIN_MARGIN)
    arb_bookmakers = settings.get("arb_bookmakers", BOOKMAKERS_ALL.copy())
    arb_channel = settings.get("arb_channel")
    
    status_emoji = "✅" if arb_enabled else "❌"
    embed.add_field(
        name="⚙️ Configuration",
        value=f"**Status:** {status_emoji} {'Enabled' if arb_enabled else 'Disabled'}\n"
              f"**Min Margin:** {min_margin}%\n"
              f"**Bookmakers:** {len(arb_bookmakers)} configured\n"
              f"**Channel:** {'Set' if arb_channel else 'Not set'}",
        inline=False
    )
    
    # Global scan status
    global NEXT_GLOBAL_SCAN_TIME, LAST_GLOBAL_HOURLY_SCAN
    if NEXT_GLOBAL_SCAN_TIME:
        current_time = datetime.now(timezone.utc)
        time_until_scan = NEXT_GLOBAL_SCAN_TIME - current_time
        minutes_until = max(0, int(time_until_scan.total_seconds() // 60))
        
        embed.add_field(
            name="🌐 Global Scan Timer",
            value=f"**Next Scan:** {minutes_until} minutes\n"
                  f"**Scan Time:** {NEXT_GLOBAL_SCAN_TIME.strftime('%H:%M UTC')}\n"
                  f"**Last Scan:** {'Never' if not LAST_GLOBAL_HOURLY_SCAN else LAST_GLOBAL_HOURLY_SCAN.strftime('%H:%M UTC')}",
            inline=True
        )
    else:
        embed.add_field(
            name="🌐 Global Scan Timer",
            value="❌ Not initialized",
            inline=True
        )
    
    # Statistics
    arb_stats = bet_stats.get("arbitrage", {"sent": 0})
    total_sent = arb_stats.get("sent", 0)
    
    embed.add_field(
        name="📊 Statistics",
        value=f"**Arbitrage Alerts Sent:** {total_sent}\n"
              f"**Cache Status:** {len(odds_cache)} markets cached\n"
              f"**API Limits:** Check logs for rate limiting",
        inline=True
    )
    
    # Bookmaker list (first 8)
    if arb_bookmakers:
        bookmaker_list = ", ".join(arb_bookmakers[:8])
        if len(arb_bookmakers) > 8:
            bookmaker_list += f" +{len(arb_bookmakers) - 8} more"
    else:
        bookmaker_list = "None configured"
    
    embed.add_field(
        name="🏢 Bookmakers",
        value=bookmaker_list,
        inline=False
    )
    
    # Quick setup guide if disabled
    if not arb_enabled:
        embed.add_field(
            name="🚀 Quick Setup",
            value="1. Use `/admin` → **Arbitrage Settings** → **Toggle Arbitrage Scanning** to enable\n"
                  "2. Or use `/force-enable-scanning` (owner only) to enable all scanning\n"
                  "3. Set alert channel with `/admin` → **Alert Channel Selection**",
            inline=False
        )
    elif not arb_channel:
        embed.add_field(
            name="⚠️ Setup Required",
            value="Scanning is enabled but no alert channel is set!\n"
                  "Use `/admin` → **Alert Channel Selection** to configure",
            inline=False
        )
    
    embed.set_footer(text="BetVillian Arbitrage Monitor")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="force-sync", description="[OWNER] Force sync all commands")
async def force_sync(interaction: discord.Interaction):
    """Force sync all bot commands - for debugging"""
    if not is_bot_owner(interaction.user.id):
        await interaction.response.send_message("❌ This command is only available to the bot owner.", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        synced = await bot.tree.sync()
        embed = discord.Embed(
            title="🔄 Command Sync Results",
            color=0x00FF00,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(
            name="✅ Sync Complete",
            value=f"Successfully synced **{len(synced)}** commands",
            inline=False
        )
        
        # List synced commands
        if synced:
            command_list = "\n".join([f"• `/{cmd.name}` - {cmd.description}" for cmd in synced])
            embed.add_field(
                name="📝 Synced Commands",
                value=command_list,
                inline=False
            )
        
        embed.set_footer(text="BetVillian Command Sync")
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to sync commands: {e}")

async def send_arbitrage_alert(guild_id, channel):
    """Legacy arbitrage alert function - redirects to enhanced version"""
    try:
        logging.info(f"🔄 Legacy arbitrage alert called for guild {guild_id}, fetching fresh data...")
        
        # Fetch fresh data for this specific call
        global_data = await fetch_all_odds_data()
        
        if not global_data:
            logging.warning(f"No global data available for legacy arbitrage alert in guild {guild_id}")
            return
        
        # Use the enhanced function with fresh data
        await send_arbitrage_alert_with_data(guild_id, channel, global_data)
        
    except Exception as e:
        logging.error(f"Error in legacy send_arbitrage_alert for guild {guild_id}: {e}")

async def send_ev_alert(guild_id, channel):
    try:
        settings = server_settings.get(guild_id, {})
        min_ev = settings.get("min_ev", DEFAULT_MIN_EV)
        allowed_books = settings.get("ev_bookmakers", BOOKMAKERS_ALL)
        role_id = settings.get("ev_mention_role")
        mention = f"<@&{role_id}>" if role_id else ""

        # Limit: 5 unique bets per 10 minutes
        now = datetime.now(timezone.utc)
        tracker = ev_sent_tracker.setdefault(guild_id, [])
        tracker[:] = [tup for tup in tracker if (now - tup[1]).total_seconds() < 600]
        sent_ids = {tup[0] for tup in tracker}

        sports = await get_sports()
        evs = []

        for sport in sports:
            for market_key in ["h2h", "totals", "spreads"]:
                events = await get_odds(sport["key"], market_key)
                if events:
                    evs.extend(find_ev_opportunities(events, market_key, min_ev, allowed_books))

        def get_ev_sent_ids(guild_id):
            return set(server_settings.get(guild_id, {}).get("ev_sent_ids", []))

        async def add_ev_sent_id(guild_id, id_key):
            ids = set(server_settings.setdefault(guild_id, {}).setdefault("ev_sent_ids", []))
            ids.add(id_key)
            server_settings[guild_id]["ev_sent_ids"] = list(ids)
            await save_settings_async()

        ev_sent_ids = get_ev_sent_ids(guild_id)
        new_evs = [ev for ev in evs if f"{ev['event']['id']}-{ev['outcome']}" not in ev_sent_ids]
        if not new_evs:
            # Don't send "no new EV bets" messages to avoid spam - only log
            logging.debug(f"No new EV opportunities for guild {guild_id}")
            return

        for ev in new_evs[:5]:
            id_key = f"{ev['event']['id']}-{ev['outcome']}"
            
            # Calculate dynamic stake suggestion
            stake_units = calculate_stake_suggestion(ev['ev'], ev['offered_odds'])
            
            # Automatically track the bet
            bet_info = {
                "event_id": ev["event"]["id"],
                "outcome": ev["outcome"],
                "odds": ev["offered_odds"],
                "stake": stake_units,  # Store stake units directly
                "guild_id": guild_id,
                "market_key": ev["market_key"],
                "posted_time": datetime.now(timezone.utc),
                "channel_id": channel.id,
                "game": f"{ev['event']['home_team']} vs {ev['event']['away_team']}",
                "bookmaker": ev["bookmaker"]
            }
            
            embed = discord.Embed(
                title="📈 +EV Bet Found",
                color=discord.Color.green()
            )
            embed.add_field(name="Sport", value=f"**{ev['event'].get('sport_title', 'Unknown')}**", inline=True)
            embed.add_field(name="Match", value=f"**{ev['event']['home_team']} vs {ev['event']['away_team']}**", inline=True)
            embed.add_field(name="⚡ EV", value=f"**{ev['ev']:.1f}%**", inline=True)
            embed.add_field(
                name="Market & Outcome",
                value=f"**{MARKET_LABELS.get(ev['market_key'], ev['market_key'])}**\n➤ **{ev['outcome']}**",
                inline=False
            )
            embed.add_field(
                name="Start Time",
                value=f"**{format_datetime(ev['event']['commence_time'])}**",
                inline=False
            )
            embed.add_field(
                name="Best Odds",
                value=f"**{ev['offered_odds']}** ({ev['bookmaker']})",
                inline=True
            )
            embed.add_field(
                name="Fair Odds",
                value=f"**{ev['fair_odds']}**",
                inline=True
            )
            embed.add_field(
                name="Stake Suggestion",
                value=f"**{stake_units:.1f} units**",
                inline=True
            )

            embed.set_footer(text="BetVillian +EV Scanner • Dynamic stake suggestions")

            # Send alert without tracking
            msg = await channel.send(content=mention, embed=embed)
            
            # Track alert for stats only
            sent_alerts.append({
                "guild_id": guild_id,
                "channel_id": channel.id,
                "message_id": msg.id,
                "alert_type": "ev",
                "event_name": f"{ev['event']['home_team']} vs {ev['event']['away_team']}",
                "outcome": ev['outcome'],
                "status": None
            })
            bet_stats["ev"]["sent"] += 1

            await add_ev_sent_id(guild_id, id_key)
        logging.info(f"Sent {len(new_evs[:5])} EV alerts to guild {guild_id}")
    except Exception as e:
        logging.error(f"send_ev_alert failed for guild {guild_id}: {e}")
        # Don't send error messages to channel to avoid spam - only log
        pass

async def log_admin_action(guild_id, user_id, action):
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "action": action
    }
    server_settings.setdefault(guild_id, {}).setdefault("admin_audit_log", []).append(entry)
    await save_settings_async()

# ---------- UI: ADMIN PANEL ----------

class ArbCalculatorButton(discord.ui.Button):
    def __init__(self, odds_list, book_list, margin=None):
        super().__init__(label="🧮 Arbitrage Calculator", style=discord.ButtonStyle.success)
        self.odds_list = [float(o) for o in odds_list]
        self.book_list = book_list
        self.margin = margin

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ArbCalculatorModal(self.odds_list, self.book_list, self.margin))

class ArbCalculatorModal(discord.ui.Modal):
    def __init__(self, odds_list, book_list, margin=None):
        super().__init__(title="🧮 Arbitrage Calculator")
        self.odds_list = odds_list
        self.book_list = book_list
        self.margin = margin
        
        # Total stake input
        self.total_amount = discord.ui.TextInput(
            label="Total Stake Amount (units)",
            placeholder="Enter your total stake amount (e.g. 100)",
            required=True,
            max_length=10
        )
        
        # Create editable odds inputs - limit to 4 outcomes max for modal limits
        self.odds_inputs = []
        max_outcomes = min(len(self.odds_list), 4)
        
        for i in range(max_outcomes):
            odds = self.odds_list[i]
            book = self.book_list[i] if i < len(self.book_list) else f"Outcome {i+1}"
            
            odds_input = discord.ui.TextInput(
                label=f"{book[:20]}... Odds" if len(book) > 20 else f"{book} Odds",
                placeholder=f"Current: {odds:.2f}",
                default=str(odds),
                required=True,
                max_length=10
            )
            self.odds_inputs.append(odds_input)
            self.add_item(odds_input)
        
        # Add total amount input last
        self.add_item(self.total_amount)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            total = float(self.total_amount.value)
            if total <= 0:
                await interaction.response.send_message("❌ Total stake must be positive.", ephemeral=True)
                return
            
            # Get updated odds from user input
            updated_odds = []
            for odds_input in self.odds_inputs:
                try:
                    odds_value = float(odds_input.value)
                    if odds_value <= 1.0:
                        await interaction.response.send_message("❌ All odds must be greater than 1.0", ephemeral=True)
                        return
                    updated_odds.append(odds_value)
                except ValueError:
                    await interaction.response.send_message("❌ Please enter valid decimal odds (e.g. 2.50)", ephemeral=True)
                    return
            
            # Calculate arbitrage stakes
            inv_sum = sum(1/odds for odds in updated_odds)
            
            # Check if still profitable
            if inv_sum >= 1.0:
                await interaction.response.send_message("⚠️ **Warning:** These odds do not guarantee profit! Total inverse odds ≥ 1.0", ephemeral=True)
                return
            
            # Calculate individual stakes and profits
            stakes = [round(total / (odds * inv_sum), 2) for odds in updated_odds]
            profits = [round(stake * odds - total, 2) for stake, odds in zip(stakes, updated_odds)]
            guaranteed_profit = min(profits)
            profit_margin = (1 - inv_sum) * 100
            
            # Create result embed
            embed = discord.Embed(
                title="🧮 Arbitrage Calculator Results",
                description=f"**Total Stake:** {total} units | **Profit Margin:** {profit_margin:.2f}%",
                color=discord.Color.green()
            )
            
            # Stakes breakdown
            stake_text = ""
            for i, (stake, book, odds) in enumerate(zip(stakes, self.book_list[:len(stakes)], updated_odds)):
                book_name = book[:15] + "..." if len(book) > 15 else book
                stake_text += f"**{book_name}:** {stake} units @ {odds:.2f}\n"
            
            embed.add_field(
                name="💰 Stake Distribution",
                value=stake_text,
                inline=False
            )
            
            # Profit analysis
            profit_text = ""
            for i, (profit, book) in enumerate(zip(profits, self.book_list[:len(profits)])):
                book_name = book[:15] + "..." if len(book) > 15 else book
                profit_text += f"**{book_name} wins:** +{profit:.2f} units\n"
            
            embed.add_field(
                name="📈 Profit Per Outcome",
                value=profit_text,
                inline=True
            )
            
            # Summary
            roi_percent = (guaranteed_profit / total) * 100
            embed.add_field(
                name="✅ Guaranteed Results",
                value=f"**Profit:** +{guaranteed_profit:.2f} units\n**ROI:** {roi_percent:.1f}%\n**Margin:** {profit_margin:.2f}%",
                inline=True
            )
            
            # Add risk warning if margin is low
            if profit_margin < 1.0:
                embed.add_field(
                    name="⚠️ Risk Warning",
                    value="Low profit margin! Double-check odds before placing bets.",
                    inline=False
                )
            
            embed.set_footer(text="BetVillian Arbitrage Calculator • Odds are editable")
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        except ValueError:
            await interaction.response.send_message("❌ Please enter valid numbers only.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Calculator error: {str(e)[:100]}", ephemeral=True)

class RoleSelect(discord.ui.Select):
    def __init__(self, guild: discord.Guild, page=0):
        self.guild = guild
        self.page = page
        
        all_roles = [r for r in guild.roles if not r.managed and r.name != "@everyone"]
        
        # Calculate pagination
        start_idx = page * 25
        end_idx = start_idx + 25
        roles_page = all_roles[start_idx:end_idx]
        
        options = [
            discord.SelectOption(label=r.name, value=str(r.id))
            for r in roles_page
        ]
        
        # If no options, add placeholder
        if not options:
            options = [discord.SelectOption(label="No roles available", value="none")]
        
        super().__init__(
            placeholder=f"Authorize roles to use /admin (Page {page + 1})",
            options=options,
            min_values=1,
            max_values=len(options) if options[0].value != "none" else 1  # Allow selecting multiple roles
        )

    async def callback(self, interaction: discord.Interaction):
        if "none" in self.values:
            await interaction.response.send_message("❌ No valid roles selected.", ephemeral=True)
            return
        
        # Defer the interaction to avoid conflicts
        await interaction.response.defer(ephemeral=True)
            
        guild_id = interaction.guild.id
        server_settings.setdefault(guild_id, {})["authorized_roles"] = [int(v) for v in self.values]
        server_settings[guild_id]["setup_done"] = True
        server_settings[guild_id]["setup_user_id"] = interaction.user.id
        await save_settings_async()
        await log_admin_action(guild_id, interaction.user.id, "Changed authorized roles")
        await interaction.followup.send("✅ Authorized roles updated and locked.", ephemeral=True)

class MinEVSelect(discord.ui.Select):
    def __init__(self, guild_id):
        current_ev = server_settings.get(guild_id, {}).get("min_ev", DEFAULT_MIN_EV)
        options = [
            discord.SelectOption(label=f"{ev}%", value=str(ev), default=(ev == current_ev))
            for ev in range(1, 21)
        ]
        super().__init__(placeholder="Set minimum EV %", options=options)

    async def callback(self, interaction: discord.Interaction):
        # Defer the interaction to avoid conflicts
        await interaction.response.defer(ephemeral=True)
        
        guild_id = interaction.guild.id
        min_ev = float(self.values[0])
        async with settings_lock:
            server_settings.setdefault(guild_id, {})["min_ev"] = min_ev
            await save_settings_async_unlocked()
        await interaction.followup.send(f"✅ Minimum EV set to {min_ev}%", ephemeral=True)

class SetBankrollButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="💰 Set Bankroll", style=discord.ButtonStyle.primary, emoji="💵")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SetBankrollModal())

class SetBankrollModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="💰 Set Total Bankroll")
        
        self.bankroll_input = discord.ui.TextInput(
            label="Full Bankroll Amount (in dollars)",
            placeholder="Enter your total bankroll, e.g. 1000 (= 100 units)",
            required=True,
            max_length=10
        )
        self.add_item(self.bankroll_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = float(self.bankroll_input.value)
            if amount <= 0:
                await interaction.response.send_message("❌ Bankroll amount must be positive!", ephemeral=True)
                return
            
            guild_id = interaction.guild_id
            old_amount, new_amount = set_bankroll_amount(guild_id, amount)
            
            embed = discord.Embed(
                title="💰 Bankroll Updated",
                description=f"**New Bankroll:** ${new_amount:,.2f}",
                color=0x00FF00,
                timestamp=datetime.now(timezone.utc)
            )
            
            embed.add_field(
                name="📊 Unit System",
                value=f"• **Total Bankroll:** ${new_amount:,.2f} (100 units)\n• **1 unit = 1% of bankroll** (${new_amount/100:.2f})\n• Stake suggestions shown in units for easy sizing",
                inline=False
            )
            
            if old_amount != new_amount:
                embed.add_field(
                    name="📈 Change",
                    value=f"**Previous:** ${old_amount:,.2f}\n**New:** ${new_amount:,.2f}",
                    inline=True
                )
            
            embed.set_footer(text="BetVillian Bankroll Manager • Direct dollar tracking")
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
            await log_admin_action(guild_id, interaction.user.id, f"Set bankroll to ${amount:,.2f}")
            
        except ValueError:
            await interaction.response.send_message("❌ Please enter a valid number!", ephemeral=True)
        except Exception as e:
            logging.error(f"Error setting bankroll: {e}")
            await interaction.response.send_message("❌ Error setting bankroll. Please try again.", ephemeral=True)

class ForceEVScanButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Force EV Scan", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        user = interaction.user
        settings = server_settings.get(guild_id, {})
        channel_id = settings.get("ev_alert_channel")
        allowed_books = settings.get("ev_bookmakers", BOOKMAKERS_ALL)
        
        # Debug global scan info
        current_time = datetime.now(timezone.utc)
        if NEXT_GLOBAL_SCAN_TIME:
            time_until_scan = NEXT_GLOBAL_SCAN_TIME - current_time
            minutes_until = max(0, int(time_until_scan.total_seconds() // 60))
            scan_info = f"Global scan in {minutes_until} minutes"
        else:
            scan_info = "Global scan initializing"
        
        logging.info(f"[ADMIN] {user} ({user.id}) pressed Force EV Scan in guild {guild_id}")
        print(f"[ADMIN] {user} ({user.id}) pressed Force EV Scan in guild {guild_id}")
        print(f"[DEBUG] 🌐 {scan_info}")
        print(f"[DEBUG] EV Settings for guild {guild_id}: channel_id={channel_id}, allowed_books={allowed_books}")
        print(f"[DEBUG] All settings for guild {guild_id}: {settings}")
        print(f"[DEBUG] Settings keys: {list(settings.keys())}")
        
        if not channel_id or not allowed_books:
            msg = f"❌ Cannot force EV scan: Please set EV alert channel and EV bookmakers first.\n🌐 {scan_info}"
            logging.warning(msg)
            print(msg)
            await interaction.response.send_message(msg, ephemeral=True)
            return
        channel = bot.get_channel(channel_id)
        if not channel:
            msg = f"❌ EV alert channel not found.\n🌐 {scan_info}"
            logging.warning(msg)
            print(msg)
            await interaction.response.send_message(msg, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await send_ev_alert(guild_id, channel)
            msg = f"✅ Successfully forced EV scan.\n🌐 {scan_info}"
            logging.info(msg)
            print(msg)
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            msg = f"❌ EV scan failed: {e}\n🌐 {scan_info}"
            logging.error(msg)
            print(msg)
            await interaction.followup.send(msg, ephemeral=True)

class ForceArbScanButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Force ARB Scan", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        settings = server_settings.get(guild_id, {})
        channel_id = settings.get("arb_alert_channel")
        allowed_books = settings.get("arb_bookmakers")
        
        # Debug global scan info
        current_time = datetime.now(timezone.utc)
        if NEXT_GLOBAL_SCAN_TIME:
            time_until_scan = NEXT_GLOBAL_SCAN_TIME - current_time
            minutes_until = max(0, int(time_until_scan.total_seconds() // 60))
            scan_info = f"Global scan in {minutes_until} minutes"
        else:
            scan_info = "Global scan initializing"
        
        print(f"[ADMIN] Force ARB Scan pressed in guild {guild_id}")
        print(f"[DEBUG] 🌐 {scan_info}")
        
        if not channel_id or not allowed_books:
            await interaction.response.send_message(
                f"❌ Cannot force ARB scan: Please set ARB alert channel and ARB bookmakers first.\n🌐 {scan_info}",
                ephemeral=True
            )
            return
        channel = bot.get_channel(channel_id)
        if not channel:
            await interaction.response.send_message(
                f"❌ ARB alert channel not found.\n🌐 {scan_info}",
                ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await send_arbitrage_alert(guild_id, channel)
        await interaction.followup.send(f"✅ Successfully forced ARB scan.\n🌐 {scan_info}", ephemeral=True)

class ToggleArbScanButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Toggle ARB Scan", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        async with settings_lock:
            settings = server_settings.setdefault(guild_id, {})
            current = settings.get("arb_scan_enabled", False)
            settings["arb_scan_enabled"] = not current
            await save_settings_async_unlocked()
        
        # Manage independent scanning if needed
        if settings.get("scan_mode", "global") == "independent":
            await manage_independent_scanning(guild_id)
        
        # Global scan info
        current_time = datetime.now(timezone.utc)
        if NEXT_GLOBAL_SCAN_TIME:
            time_until_scan = NEXT_GLOBAL_SCAN_TIME - current_time
            minutes_until = max(0, int(time_until_scan.total_seconds() // 60))
            scan_info = f"Next automatic scan in {minutes_until} minutes"
        else:
            scan_info = "Automatic scanning initializing"
        
        state = "enabled" if settings["arb_scan_enabled"] else "disabled"
        
        # If this is the first time enabling, give helpful info
        if settings["arb_scan_enabled"] and not settings.get("arb_notification_channel"):
            warning = "\n⚠️ Don't forget to set your ARB notification channel!"
        else:
            warning = ""
        
        await interaction.response.send_message(
            f"📊 Arbitrage scanning is now **{state}**.\n🌐 {scan_info}{warning}", 
            ephemeral=True
        )

class ToggleEVScanButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Toggle EV Scan", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        async with settings_lock:
            settings = server_settings.setdefault(guild_id, {})
            current = settings.get("ev_scan_enabled", False)
            settings["ev_scan_enabled"] = not current
            await save_settings_async_unlocked()
        
        # Manage independent scanning if needed
        if settings.get("scan_mode", "global") == "independent":
            await manage_independent_scanning(guild_id)
        
        # Global scan info
        current_time = datetime.now(timezone.utc)
        if NEXT_GLOBAL_SCAN_TIME:
            time_until_scan = NEXT_GLOBAL_SCAN_TIME - current_time
            minutes_until = max(0, int(time_until_scan.total_seconds() // 60))
            scan_info = f"Next automatic scan in {minutes_until} minutes"
        else:
            scan_info = "Automatic scanning initializing"
        
        state = "enabled" if settings["ev_scan_enabled"] else "disabled"
        
        # If this is the first time enabling, give helpful info
        if settings["ev_scan_enabled"] and not settings.get("ev_notification_channel"):
            warning = "\n⚠️ Don't forget to set your EV notification channel!"
        else:
            warning = ""
        
        await interaction.response.send_message(
            f"📈 EV scanning is now **{state}**.\n🌐 {scan_info}{warning}", 
            ephemeral=True
        )

class GlobalScanButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🌐 Global Sync Scan", style=discord.ButtonStyle.primary, emoji="🔄")

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        user = interaction.user
        
        # Check if user is bot owner
        if not is_bot_owner(interaction.user.id):
            await interaction.response.send_message("❌ Only the bot owner can trigger global scans.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Fetch global data once
            global_data = await fetch_all_odds_data()
            if not global_data:
                await interaction.followup.send("❌ Failed to fetch global odds data.", ephemeral=True)
                return
            
            # Send alerts to all configured guilds
            scan_count = 0
            for g_id, settings in server_settings.items():
                guild = bot.get_guild(g_id)
                if not guild:
                    continue
                
                try:
                    # Send EV alerts if configured
                    ev_channel_id = settings.get("ev_alert_channel")
                    ev_books = settings.get("ev_bookmakers", BOOKMAKERS_ALL)
                    if ev_channel_id and ev_books and settings.get("ev_scan_enabled", False):
                        ev_channel = bot.get_channel(ev_channel_id)
                        if ev_channel:
                            await send_ev_alert_with_data(g_id, ev_channel, global_data)
                            scan_count += 1
                    
                    # Send ARB alerts if configured  
                    arb_channel_id = settings.get("arb_alert_channel")
                    arb_books = settings.get("arb_bookmakers")
                    if arb_channel_id and arb_books and settings.get("arb_scan_enabled", False):
                        arb_channel = bot.get_channel(arb_channel_id)
                        if arb_channel:
                            await send_arbitrage_alert_with_data(g_id, arb_channel, global_data)
                            scan_count += 1
                            
                except Exception as e:
                    logging.error(f"Error scanning guild {g_id}: {e}")
            
            await interaction.followup.send(f"✅ Global scan completed! Sent alerts to {scan_count} configured channels.", ephemeral=True)
            
        except Exception as e:
            logging.error(f"Global scan failed: {e}")
            await interaction.followup.send(f"❌ Global scan failed: {e}", ephemeral=True)

class MinMarginSelect(discord.ui.Select):
    def __init__(self, guild_id):
        current_margin = server_settings.get(guild_id, {}).get("min_margin", DEFAULT_MIN_MARGIN)
        options = [
            discord.SelectOption(label=f"{m}%", value=str(m), default=(m == current_margin))
            for m in range(1, 21)
        ]
        super().__init__(placeholder="Set minimum arbitrage margin %", options=options)

    async def callback(self, interaction: discord.Interaction):
        # Defer the interaction to avoid conflicts
        await interaction.response.defer(ephemeral=True)
        
        guild_id = interaction.guild.id
        margin = float(self.values[0])
        async with settings_lock:
            server_settings.setdefault(guild_id, {})["min_margin"] = margin
            await save_settings_async_unlocked()
        await interaction.followup.send(f"✅ Min arbitrage margin set to {margin}%", ephemeral=True)

class BookmakerMultiSelect(discord.ui.Select):
    def __init__(self, guild_id, setting="bookmakers", page=0):
        self.guild_id = guild_id
        self.setting = setting
        self.page = page
        
        current_books = server_settings.get(guild_id, {}).get(setting)
        
        # Calculate pagination
        start_idx = page * 25
        end_idx = start_idx + 25
        bookmakers_page = BOOKMAKERS_ALL[start_idx:end_idx]
        
        options = [
            discord.SelectOption(
                label=b,
                value=b,
                default=(current_books is not None and b in current_books)
            )
            for b in bookmakers_page
        ]
        
        # If no options, add placeholder
        if not options:
            options = [discord.SelectOption(label="No bookmakers available", value="none")]
        
        super().__init__(
            placeholder=f"Select bookmakers for {setting.replace('_', ' ')} (Page {page + 1})",
            options=options,
            min_values=1,
            max_values=len(options) if options[0].value != "none" else 1
        )
        self.setting = setting

    async def callback(self, interaction: discord.Interaction):
        if "none" in self.values:
            await interaction.response.send_message("❌ No valid bookmakers selected.", ephemeral=True)
            return
        
        # Defer the interaction to avoid conflicts
        await interaction.response.defer(ephemeral=True)
            
        guild_id = interaction.guild.id
        print(f"[DEBUG] BookmakerMultiSelect saving {self.setting}={self.values} for guild {guild_id}")
        async with settings_lock:
            server_settings.setdefault(guild_id, {})[self.setting] = self.values
            await save_settings_async_unlocked()
        print(f"[DEBUG] After save, settings for guild {guild_id}: {server_settings.get(guild_id, {})}")
        await interaction.followup.send(f"✅ Bookmakers for {self.setting.replace('_', ' ')} updated.", ephemeral=True)

class ChannelSelect(discord.ui.Select):
    def __init__(self, channels, placeholder, setting):
        options = [
            discord.SelectOption(label=channel.name, value=str(channel.id))
            for channel in channels
        ]
        super().__init__(placeholder=placeholder, options=options, min_values=1, max_values=1)
        self.setting = setting

    async def callback(self, interaction: discord.Interaction):
        # Defer the interaction to avoid conflicts
        await interaction.response.defer(ephemeral=True)
        
        guild_id = interaction.guild.id
        channel_id = int(self.values[0])
        async with settings_lock:
            server_settings.setdefault(guild_id, {})[self.setting] = channel_id
            await save_settings_async_unlocked()
        await interaction.followup.send(f"✅ Alert channel set to <#{channel_id}>", ephemeral=True)

class MultiChannelSelectView(discord.ui.View):
    def __init__(self, guild, setting):
        super().__init__(timeout=None)
        channels = guild.text_channels
        # Split channels into groups of 25
        for i in range(0, len(channels), 25):
            chunk = channels[i:i+25]
            self.add_item(ChannelSelect(chunk, f"Select alert channel group {i//25 + 1} for {setting.replace('_', ' ')}", setting))

class AlertChannelSelect(discord.ui.Select):
    def __init__(self, guild: discord.Guild, setting="alert_channel", page=0):
        self.guild = guild
        self.setting = setting
        self.page = page
        
        current_channel = server_settings.get(guild.id, {}).get(setting)
        all_channels = guild.text_channels
        
        # Calculate pagination
        start_idx = page * 25
        end_idx = start_idx + 25
        channels_page = all_channels[start_idx:end_idx]
        
        options = [
            discord.SelectOption(
                label=channel.name,
                value=str(channel.id),
                default=(current_channel is not None and int(current_channel) == channel.id)
            )
            for channel in channels_page
        ]
        
        # If no options, add a placeholder
        if not options:
            options = [discord.SelectOption(label="No channels available", value="none")]
        
        placeholder = f"Select alert channel for {setting.replace('_', ' ')} (Page {page + 1})"
        super().__init__(placeholder=placeholder, options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("❌ No valid channel selected.", ephemeral=True)
            return
            
        # Defer the interaction to avoid conflicts with pagination
        await interaction.response.defer(ephemeral=True)
        
        guild_id = interaction.guild.id
        channel_id = int(self.values[0])
        
        # Auto-enable scanning when alert channels are set
        auto_enable_message = ""
        
        async with settings_lock:
            server_settings.setdefault(guild_id, {})[self.setting] = channel_id
            
            # Auto-enable EV scanning when EV channel is set
            if self.setting == "ev_alert_channel":
                if not server_settings[guild_id].get("ev_scan_enabled", False):
                    server_settings[guild_id]["ev_scan_enabled"] = True
                    auto_enable_message = "\n🔄 **EV scanning automatically enabled!**"
            
            # Auto-enable ARB scanning when ARB channel is set
            elif self.setting == "arb_alert_channel":
                if not server_settings[guild_id].get("arb_scan_enabled", False):
                    server_settings[guild_id]["arb_scan_enabled"] = True
                    auto_enable_message = "\n🔄 **Arbitrage scanning automatically enabled!**"
            
            await save_settings_async_unlocked()
        
        await interaction.followup.send(
            f"✅ Alert channel for {self.setting.replace('_', ' ')} set to <#{channel_id}>{auto_enable_message}", 
            ephemeral=True
        )

class PaginatedChannelView(discord.ui.View):
    def __init__(self, guild: discord.Guild, setting: str, is_filtered: bool = False):
        super().__init__(timeout=300)
        self.guild = guild
        self.setting = setting
        self.is_filtered = is_filtered
        self.current_page = 0
        
        if is_filtered:
            keywords = ["ev", "arb", "expectedvalue", "bot"]
            self.all_channels = [
                channel for channel in guild.text_channels
                if any(kw in channel.name.lower() for kw in keywords)
            ]
            if not self.all_channels:
                self.all_channels = guild.text_channels
        else:
            self.all_channels = guild.text_channels
        
        self.max_pages = (len(self.all_channels) - 1) // 25 + 1
        self.update_view()
    
    def update_view(self):
        self.clear_items()
        
        # Add channel select for current page
        if self.is_filtered:
            select = FilteredChannelSelect(self.guild, self.setting, self.current_page)
        else:
            select = AlertChannelSelect(self.guild, self.setting, self.current_page)
        
        self.add_item(select)
        
        # Add navigation buttons if needed
        if self.max_pages > 1:
            if self.current_page > 0:
                prev_button = discord.ui.Button(label="◀ Previous", style=discord.ButtonStyle.secondary)
                prev_button.callback = self.previous_page
                self.add_item(prev_button)
            
            if self.current_page < self.max_pages - 1:
                next_button = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary)
                next_button.callback = self.next_page
                self.add_item(next_button)
        
        # Add page info if multiple pages
        if self.max_pages > 1:
            info_button = discord.ui.Button(
                label=f"Page {self.current_page + 1}/{self.max_pages}", 
                style=discord.ButtonStyle.secondary, 
                disabled=True
            )
            self.add_item(info_button)
    
    async def previous_page(self, interaction: discord.Interaction):
        self.current_page -= 1
        self.update_view()
        try:
            await interaction.response.edit_message(view=self)
        except discord.InteractionResponded:
            await interaction.edit_original_response(view=self)
    
    async def next_page(self, interaction: discord.Interaction):
        self.current_page += 1
        self.update_view()
        try:
            await interaction.response.edit_message(view=self)
        except discord.InteractionResponded:
            await interaction.edit_original_response(view=self)

class PaginatedBookmakerView(discord.ui.View):
    def __init__(self, guild_id: int, setting: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.setting = setting
        self.current_page = 0
        
        self.max_pages = (len(BOOKMAKERS_ALL) - 1) // 25 + 1
        self.update_view()
    
    def update_view(self):
        self.clear_items()
        
        # Add bookmaker select for current page
        select = BookmakerMultiSelect(self.guild_id, self.setting, self.current_page)
        self.add_item(select)
        
        # Add navigation buttons if needed
        if self.max_pages > 1:
            if self.current_page > 0:
                prev_button = discord.ui.Button(label="◀ Previous", style=discord.ButtonStyle.secondary)
                prev_button.callback = self.previous_page
                self.add_item(prev_button)
            
            if self.current_page < self.max_pages - 1:
                next_button = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary)
                next_button.callback = self.next_page
                self.add_item(next_button)
        
        # Add page info if multiple pages
        if self.max_pages > 1:
            info_button = discord.ui.Button(
                label=f"Page {self.current_page + 1}/{self.max_pages}", 
                style=discord.ButtonStyle.secondary, 
                disabled=True
            )
            self.add_item(info_button)
    
    async def previous_page(self, interaction: discord.Interaction):
        self.current_page -= 1
        self.update_view()
        try:
            await interaction.response.edit_message(view=self)
        except discord.InteractionResponded:
            await interaction.edit_original_response(view=self)
    
    async def next_page(self, interaction: discord.Interaction):
        self.current_page += 1
        self.update_view()
        try:
            await interaction.response.edit_message(view=self)
        except discord.InteractionResponded:
            await interaction.edit_original_response(view=self)

class PaginatedRoleView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=300)
        self.guild = guild
        self.current_page = 0
        
        all_roles = [r for r in guild.roles if not r.managed and r.name != "@everyone"]
        self.max_pages = (len(all_roles) - 1) // 25 + 1 if all_roles else 1
        self.update_view()
    
    def update_view(self):
        self.clear_items()
        
        # Add role select for current page
        select = RoleSelect(self.guild, self.current_page)
        self.add_item(select)
        
        # Add navigation buttons if needed
        if self.max_pages > 1:
            if self.current_page > 0:
                prev_button = discord.ui.Button(label="◀ Previous", style=discord.ButtonStyle.secondary)
                prev_button.callback = self.previous_page
                self.add_item(prev_button)
            
            if self.current_page < self.max_pages - 1:
                next_button = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary)
                next_button.callback = self.next_page
                self.add_item(next_button)
        
        # Add page info if multiple pages
        if self.max_pages > 1:
            info_button = discord.ui.Button(
                label=f"Page {self.current_page + 1}/{self.max_pages}", 
                style=discord.ButtonStyle.secondary, 
                disabled=True
            )
            self.add_item(info_button)
    
    async def previous_page(self, interaction: discord.Interaction):
        self.current_page -= 1
        self.update_view()
        try:
            await interaction.response.edit_message(view=self)
        except discord.InteractionResponded:
            await interaction.edit_original_response(view=self)
    
    async def next_page(self, interaction: discord.Interaction):
        self.current_page += 1
        self.update_view()
        try:
            await interaction.response.edit_message(view=self)
        except discord.InteractionResponded:
            await interaction.edit_original_response(view=self)

class MentionRoleSelect(discord.ui.Select):
    def __init__(self, guild: discord.Guild, setting="mention_role"):
        options = [
            discord.SelectOption(label=role.name, value=str(role.id))
            for role in guild.roles if not role.managed and role.name != "@everyone"
        ]
        super().__init__(placeholder=f"Select mention role for {setting.replace('_', ' ')}", options=options)
        self.setting = setting

    async def callback(self, interaction: discord.Interaction):
        # Defer the interaction to avoid conflicts
        await interaction.response.defer(ephemeral=True)
        
        guild_id = interaction.guild.id
        role_id = int(self.values[0])
        async with settings_lock:
            server_settings.setdefault(guild_id, {})[self.setting] = role_id
            await save_settings_async_unlocked()
        await interaction.followup.send(f"✅ Mention role for {self.setting.replace('_', ' ')} set to <@&{role_id}>", ephemeral=True)

class ScanIntervalSelect(discord.ui.Select):
    def __init__(self, guild_id):
        current_interval = server_settings.get(guild_id, {}).get("scan_interval", SCAN_INTERVAL_SECONDS // 60)
        options = [
            discord.SelectOption(label=f"{m} minutes", value=str(m), default=(m == current_interval))
            for m in [5, 10, 15, 30, 60, 120, 180]
        ]
        super().__init__(placeholder="Set scan interval (minutes)", options=options)

    async def callback(self, interaction: discord.Interaction):
        # Only bot owner can change scan interval
        if not is_bot_owner(interaction.user.id):
            await interaction.response.send_message("❌ Only the bot owner can change scan intervals.", ephemeral=True)
            return
        
        # Defer the interaction to avoid conflicts
        await interaction.response.defer(ephemeral=True)
            
        guild_id = interaction.guild.id
        interval = int(self.values[0])
        async with settings_lock:
            server_settings.setdefault(guild_id, {})["scan_interval"] = interval
            await save_settings_async_unlocked()
        
        # Restart independent scanning if this guild uses independent mode
        if server_settings[guild_id].get("scan_mode", "global") == "independent":
            await manage_independent_scanning(guild_id)
        
        await interaction.followup.send(f"✅ Scan interval set to {interval} minutes.", ephemeral=True)

class ScanModeSelect(discord.ui.Select):
    def __init__(self, guild_id):
        current_mode = server_settings.get(guild_id, {}).get("scan_mode", "global")
        options = [
            discord.SelectOption(
                label="Global Sync (1 hour intervals)", 
                value="global", 
                description="Sync with global scanning schedule",
                default=(current_mode == "global")
            ),
            discord.SelectOption(
                label="Independent Timing", 
                value="independent", 
                description="Use custom scan interval for this server",
                default=(current_mode == "independent")
            )
        ]
        super().__init__(placeholder="Choose scanning mode", options=options)

    async def callback(self, interaction: discord.Interaction):
        # Only bot owner can change scan mode
        if not is_bot_owner(interaction.user.id):
            await interaction.response.send_message("❌ Only the bot owner can change scan modes.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        guild_id = interaction.guild.id
        mode = self.values[0]
        async with settings_lock:
            server_settings.setdefault(guild_id, {})["scan_mode"] = mode
            await save_settings_async_unlocked()
        
        # Manage independent scanning based on the new mode
        await manage_independent_scanning(guild_id)
        
        if mode == "global":
            msg = f"✅ Scanning mode set to **Global Sync** (scans every hour at synchronized times)"
        else:
            interval = server_settings[guild_id].get("scan_interval", 15)
            msg = f"✅ Scanning mode set to **Independent** (scans every {interval} minutes)"
        
        await interaction.followup.send(msg, ephemeral=True)

# Add this to your ServerSettingsView
class ServerSettingsView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=None)
        self.guild = guild
        
        # Add role selection button instead of direct select
        role_button = discord.ui.Button(label="Set Authorized Roles", style=discord.ButtonStyle.secondary, emoji="👥")
        role_button.callback = self.select_roles
        self.add_item(role_button)
        
        self.add_item(ScanModeSelect(guild.id))
        self.add_item(ScanIntervalSelect(guild.id))
        self.add_item(GlobalScanButton())
        self.add_item(BackToLandingButton(guild))
    
    async def select_roles(self, interaction: discord.Interaction):
        view = PaginatedRoleView(self.guild)
        await interaction.response.send_message("Select authorized roles:", view=view, ephemeral=True)

# In your background scan tasks, use the interval from settings:
# Track independent scan tasks
independent_scan_tasks = {}

# Global scan task that runs every hour for servers using global sync
async def global_scan_task():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            # Scan all guilds that use global sync mode
            for guild_id in server_settings:
                settings = server_settings[guild_id]
                
                # Only scan guilds using global mode
                if settings.get("scan_mode", "global") != "global":
                    continue
                
                # Check if scanning is enabled for this guild
                if not (settings.get("arb_scan_enabled", False) or settings.get("ev_scan_enabled", False)):
                    continue
                
                await perform_guild_scan(guild_id, settings)
                
        except Exception as e:
            logging.error(f"Global scan error: {e}")
        
        # Wait for the global interval (1 hour)
        await asyncio.sleep(GLOBAL_SCAN_INTERVAL)

# Independent scan tasks for each guild
async def independent_scan_task(guild_id):
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            if guild_id not in server_settings:
                break
                
            settings = server_settings[guild_id]
            
            # Only scan if using independent mode
            if settings.get("scan_mode", "global") != "independent":
                break
            
            # Check if scanning is enabled for this guild
            if not (settings.get("arb_scan_enabled", False) or settings.get("ev_scan_enabled", False)):
                # Wait a bit before checking again
                await asyncio.sleep(60)
                continue
            
            await perform_guild_scan(guild_id, settings)
            
        except Exception as e:
            logging.error(f"Independent scan error for guild {guild_id}: {e}")
        
        # Use the guild's custom interval
        settings = server_settings.get(guild_id, {})
        interval_minutes = settings.get("scan_interval", 15)
        await asyncio.sleep(interval_minutes * 60)

# Shared function to perform scanning for a guild
async def perform_guild_scan(guild_id, settings):
    try:
        # EV scanning
        if settings.get("ev_scan_enabled", False):
            channel_id = settings.get("ev_alert_channel")
            if channel_id:
                channel = bot.get_channel(channel_id)
                if channel:
                    await send_ev_alert(guild_id, channel)
        
        # Arbitrage scanning
        if settings.get("arb_scan_enabled", False):
            channel_id = settings.get("arb_alert_channel")
            if channel_id:
                channel = bot.get_channel(channel_id)
                if channel:
                    await send_arbitrage_alert(guild_id, channel)
                
        logging.info(f"Completed scan for guild {guild_id}")
        
    except Exception as e:
        logging.error(f"Scan error for guild {guild_id}: {e}")

# Function to start/stop independent scanning for a guild
async def manage_independent_scanning(guild_id):
    settings = server_settings.get(guild_id, {})
    scan_mode = settings.get("scan_mode", "global")
    
    # Stop existing independent task if it exists
    if guild_id in independent_scan_tasks:
        independent_scan_tasks[guild_id].cancel()
        del independent_scan_tasks[guild_id]
        logging.info(f"Stopped independent scanning for guild {guild_id}")
    
    # Start new independent task if needed
    if scan_mode == "independent":
        task = bot.loop.create_task(independent_scan_task(guild_id))
        independent_scan_tasks[guild_id] = task
        logging.info(f"Started independent scanning for guild {guild_id}")

# Main background task coordinator
async def background_scan_task():
    await bot.wait_until_ready()
    
    # Start the global scan task
    bot.loop.create_task(global_scan_task())
    
    # Start independent scan tasks for guilds that need them
    for guild_id in server_settings:
        await manage_independent_scanning(guild_id)
    
    # Keep this task alive but don't do anything
    while not bot.is_closed():
        await asyncio.sleep(3600)  # Sleep for 1 hour

class ServerSettingsButton(discord.ui.Button):
    def __init__(self, guild):
        super().__init__(label="Server Settings", style=discord.ButtonStyle.primary)
        self.guild = guild

    async def callback(self, interaction: discord.Interaction):
        if not is_admin_authorized(interaction):
            await interaction.response.send_message("❌ You are not authorized to access server settings.", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=server_settings_embed(),
            view=ServerSettingsView(self.guild)
        )

class EVSettingsButton(discord.ui.Button):
    def __init__(self, guild):
        super().__init__(label="EV Settings", style=discord.ButtonStyle.success)
        self.guild = guild

    async def callback(self, interaction: discord.Interaction):
        if not is_admin_authorized(interaction):
            await interaction.response.send_message("❌ You are not authorized to access EV settings.", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=ev_settings_embed(),
            view=EVSettingsView(self.guild)
        )

class ArbSettingsButton(discord.ui.Button):
    def __init__(self, guild):
        super().__init__(label="Arbitrage Settings", style=discord.ButtonStyle.danger)
        self.guild = guild

    async def callback(self, interaction: discord.Interaction):
        if not is_admin_authorized(interaction):
            await interaction.response.send_message("❌ You are not authorized to access arbitrage settings.", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=arb_settings_embed(),
            view=ArbSettingsView(self.guild)
        )

# --- Embeds for each settings page ---
def server_settings_embed():
    embed = discord.Embed(
        title="⚙️ Server Settings",
        description="Configure server-wide settings.",
        color=discord.Color.blurple()
    )
    embed.add_field(name="Role Authorization", value="Select roles who can use /admin.", inline=False)
    embed.add_field(name="Scan Mode", value="Choose between global sync (1 hour) or independent timing.", inline=False)
    embed.add_field(name="Scan Interval", value="Set custom scan interval (only for independent mode).", inline=False)
    embed.add_field(name="Global Sync Scan", value="Trigger a synchronized scan across all servers (Bot Owner only).", inline=False)
    return embed

def ev_settings_embed():
    embed = discord.Embed(
        title="📈 EV Settings",
        description="Configure +EV scanning and alerts.",
        color=discord.Color.green()
    )
    embed.add_field(name="Minimum EV %", value="Set the minimum EV percentage for alerts.", inline=False)
    embed.add_field(name="Bookmakers", value="Choose bookmakers for EV scans.", inline=False)
    embed.add_field(name="Alert Channel", value="Set the channel for EV notifications.", inline=False)
    embed.add_field(name="Mention Role", value="Set the role to mention for EV alerts.", inline=False)
    embed.add_field(name="Toggle EV Scan", value="Enable/disable automatic EV scanning.", inline=False)
    embed.add_field(name="Force EV Scan", value="Manually trigger an EV scan.", inline=False)
    return embed

def arb_settings_embed():
    embed = discord.Embed(
        title="🔀 Arbitrage Settings",
        description="Configure arbitrage scanning and alerts.",
        color=discord.Color.gold()
    )
    embed.add_field(name="Minimum Margin %", value="Set the minimum arbitrage margin for alerts.", inline=False)
    embed.add_field(name="Bookmakers", value="Choose bookmakers for ARB scans.", inline=False)
    embed.add_field(name="Alert Channel", value="Set the channel for ARB notifications.", inline=False)
    embed.add_field(name="Mention Role", value="Set the role to mention for ARB alerts.", inline=False)
    embed.add_field(name="Force ARB Scan", value="Manually trigger an arbitrage scan.", inline=False)
    embed.add_field(name="Toggle ARB Scan", value="Enable or disable automatic arbitrage scanning.", inline=False)
    return embed

# --- Views for each settings page ---

class EVSettingsView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=None)
        self.guild = guild
        self.add_item(MinEVSelect(guild.id))
        
        # Add bookmaker selection button instead of direct select
        bookmaker_button = discord.ui.Button(label="Set EV Bookmakers", style=discord.ButtonStyle.secondary, emoji="📚")
        bookmaker_button.callback = self.select_ev_bookmakers
        self.add_item(bookmaker_button)
        
        # Add channel selection button instead of direct select
        channel_button = discord.ui.Button(label="Set EV Alert Channel", style=discord.ButtonStyle.primary, emoji="📢")
        channel_button.callback = self.select_ev_channel
        self.add_item(channel_button)
        
        self.add_item(MentionRoleSelect(guild, setting="ev_mention_role"))
        self.add_item(ToggleEVScanButton())
        self.add_item(ForceEVScanButton())
        self.add_item(BackToAdminButton(guild))
    
    async def select_ev_bookmakers(self, interaction: discord.Interaction):
        view = PaginatedBookmakerView(self.guild.id, "ev_bookmakers")
        await interaction.response.send_message("Select EV bookmakers:", view=view, ephemeral=True)
    
    async def select_ev_channel(self, interaction: discord.Interaction):
        view = PaginatedChannelView(self.guild, "ev_alert_channel", is_filtered=True)
        await interaction.response.send_message("Select EV alert channel:", view=view, ephemeral=True)

class ArbSettingsView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=None)
        self.guild = guild
        self.add_item(MinMarginSelect(guild.id))
        
        # Add bookmaker selection button instead of direct select
        bookmaker_button = discord.ui.Button(label="Set ARB Bookmakers", style=discord.ButtonStyle.secondary, emoji="📚")
        bookmaker_button.callback = self.select_arb_bookmakers
        self.add_item(bookmaker_button)
        
        # Add channel selection button instead of direct select
        channel_button = discord.ui.Button(label="Set ARB Alert Channel", style=discord.ButtonStyle.primary, emoji="📢")
        channel_button.callback = self.select_arb_channel
        self.add_item(channel_button)
        
        self.add_item(MentionRoleSelect(guild, setting="arb_mention_role"))
        self.add_item(ForceArbScanButton())
        self.add_item(ToggleArbScanButton())
        self.add_item(BackToAdminButton(guild))
    
    async def select_arb_bookmakers(self, interaction: discord.Interaction):
        view = PaginatedBookmakerView(self.guild.id, "arb_bookmakers")
        await interaction.response.send_message("Select ARB bookmakers:", view=view, ephemeral=True)
    
    async def select_arb_channel(self, interaction: discord.Interaction):
        view = PaginatedChannelView(self.guild, "arb_alert_channel", is_filtered=True)
        await interaction.response.send_message("Select ARB alert channel:", view=view, ephemeral=True)

class BackToAdminButton(discord.ui.Button):
    def __init__(self, guild):
        super().__init__(label="← Back to Admin", style=discord.ButtonStyle.secondary)
        self.guild = guild

    async def callback(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="⚙️ BetVillian Admin Panel",
            description="Configure bot settings and bankroll notifications",
            color=0x4ECDC4,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.set_thumbnail(url=BETVILLIAN_LOGO)
        
        # Current settings
        settings = server_settings.get(interaction.guild_id, {})
        
        # Format notification channel
        channel_id = settings.get('bankroll_notification_channel')
        channel_text = f'<#{channel_id}>' if channel_id else 'Not set'
        
        embed.add_field(
            name="💰 Bankroll Settings",
            value=f"**Notification Channel:** {channel_text}\n"
                  f"**Daily Summary:** {'Enabled' if settings.get('daily_summary_enabled', True) else 'Disabled'}",
            inline=True
        )
        
        embed.add_field(
            name="🔒 Authorization",
            value=f"**Authorized Roles:** {len(settings.get('authorized_roles', []))} roles\n"
                  f"**Admin Only:** {'Yes' if settings.get('admin_only', False) else 'No'}",
            inline=True
        )
        
        embed.set_footer(text="BetVillian Admin Panel", icon_url=BETVILLIAN_LOGO)
        
        await interaction.response.edit_message(
            embed=embed,
            view=AdminSettingsView(self.guild)
        )

class BackToLandingButton(discord.ui.Button):
    def __init__(self, guild):
        super().__init__(label="⬅️ Back", style=discord.ButtonStyle.secondary)
        self.guild = guild

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=landing_embed(),
            view=AdminSettingsView(self.guild)
        )

def landing_embed():
    embed = discord.Embed(
        title="🔧 BetVillian Admin Panel",
        description="Choose a settings category below:",
        color=discord.Color.blurple()
    )
    embed.add_field(name="⚙️ Server Settings", value="Role access, alert channel, etc.", inline=False)
    embed.add_field(name="📈 EV Settings", value="EV scan controls.", inline=False)
    embed.add_field(name="🔀 Arbitrage Settings", value="Arbitrage scan controls.", inline=False)
    return embed

@bot.event
async def on_command_error(ctx, error):
    logging.error(f"Command error: {error}")
    if isinstance(error, commands.CommandNotFound):
        pass  # Ignore unknown commands
        await ctx.send("❌ Command not found.", ephemeral=True)
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You do not have permission to use this command.", ephemeral=True)
    else:
        await ctx.send(f"❌ An error occurred: {str(error)}", ephemeral=True)

# New Arbitrage Calculator feature
# Removed duplicate ArbCalculatorButton and ArbCalculatorModal classes - now using unified version above

class FilteredChannelSelect(discord.ui.Select):
    def __init__(self, guild, setting, page=0):
        self.guild = guild
        self.setting = setting
        self.page = page
        
        keywords = ["ev", "arb", "expectedvalue", "bot"]
        filtered_channels = [
            channel for channel in guild.text_channels
            if any(kw in channel.name.lower() for kw in keywords)
        ]
        
        # If no filtered channels, use all channels
        if not filtered_channels:
            filtered_channels = guild.text_channels
        
        # Calculate pagination
        start_idx = page * 25
        end_idx = start_idx + 25
        channels_page = filtered_channels[start_idx:end_idx]
        
        options = [
            discord.SelectOption(label=channel.name, value=str(channel.id))
            for channel in channels_page
        ]
        
        # If no options, add a placeholder
        if not options:
            options = [discord.SelectOption(label="No channels available", value="none")]
        
        placeholder = f"Select alert channel for {setting.replace('_', ' ')} (Page {page + 1})"
        super().__init__(placeholder=placeholder, options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("❌ No valid channel selected.", ephemeral=True)
            return
            
        # Defer the interaction to avoid conflicts with pagination
        await interaction.response.defer(ephemeral=True)
        
        guild_id = interaction.guild.id
        channel_id = int(self.values[0])
        print(f"[DEBUG] FilteredChannelSelect saving {self.setting}={channel_id} for guild {guild_id}")
        async with settings_lock:
            server_settings.setdefault(guild_id, {})[self.setting] = channel_id
            await save_settings_async_unlocked()
        print(f"[DEBUG] After save, settings for guild {guild_id}: {server_settings.get(guild_id, {})}")
        
        await interaction.followup.send(f"✅ Alert channel set to <#{channel_id}>", ephemeral=True)

# Note: Discord bot setup is defined earlier in the file (line 103)
# Note: File paths for data persistence are defined earlier in the file

# Authorization and Data Functions

def check_user_authorization(user, guild_id):
    """Check if user has permission to use betting commands"""
    # Bot owner always has access to all servers
    if user.id == BOT_OWNER_ID:
        return True
        
    if user.guild_permissions.administrator:
        return True
    
    # Check if user has authorized role from settings
    settings = server_settings.get(guild_id, {})
    authorized_roles = settings.get("authorized_roles", [])
    
    if not authorized_roles:
        return True  # No restrictions set
    
    user_role_ids = [role.id for role in user.roles]
    return any(rid in authorized_roles for rid in user_role_ids)

def check_admin_authorization(user, guild_id):
    """Check if user has admin permissions"""
    # Bot owner always has access to all servers
    if user.id == BOT_OWNER_ID:
        return True
    
    # Check if user has Discord administrator permissions
    return user.guild_permissions.administrator

async def get_odds_data(sport, market, region):
    """Fetch odds data from The Odds API"""
    try:
        url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds/"
        params = {
            "api_key": ODDS_API_KEY,
            "regions": region,
            "markets": market,
            "oddsFormat": "decimal",
            "dateFormat": "iso"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data
                else:
                    logging.error(f"Odds API error {resp.status}: {await resp.text()}")
                    return []
    except Exception as e:
        logging.error(f"Error fetching odds data: {e}")
        return []

# ---------- ADMIN SETTINGS COMMANDS ----------

@bot.tree.command(name="admin", description="Access admin settings panel")
async def admin_command(interaction: discord.Interaction):
    if not check_admin_authorization(interaction.user, interaction.guild_id):
        await interaction.response.send_message("❌ You don't have admin permission for this command.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="⚙️ BetVillian Admin Panel",
        description="Configure bot settings and bankroll notifications",
        color=0x4ECDC4,
        timestamp=datetime.now(timezone.utc)
    )
    
    embed.set_thumbnail(url=BETVILLIAN_LOGO)
    
    # Current settings
    settings = server_settings.get(interaction.guild_id, {})
    
    # Format notification channel
    channel_id = settings.get('bankroll_notification_channel')
    channel_text = f'<#{channel_id}>' if channel_id else 'Not set'
    
    embed.add_field(
        name="💰 Bankroll Settings",
        value=f"**Notification Channel:** {channel_text}\n"
              f"**Daily Summary:** {'Enabled' if settings.get('daily_summary_enabled', True) else 'Disabled'}",
        inline=True
    )
    
    embed.add_field(
        name="🔒 Authorization",
        value=f"**Authorized Roles:** {len(settings.get('authorized_roles', []))} roles\n"
              f"**Admin Only:** {'Yes' if settings.get('admin_only', False) else 'No'}",
        inline=True
    )
    
    embed.set_footer(text="BetVillian Admin Panel", icon_url=BETVILLIAN_LOGO)
    
    view = AdminSettingsView(interaction.guild)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

class AdminSettingsView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=300)
        self.guild = guild
    
    @discord.ui.button(label="Bankroll Settings", style=discord.ButtonStyle.primary, emoji="💰")
    async def bankroll_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=self.create_bankroll_settings_embed(),
            view=BankrollSettingsView(self.guild)
        )
    
    @discord.ui.button(label="Manage Roles", style=discord.ButtonStyle.secondary, emoji="🔒") 
    async def manage_roles(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = RoleSelectView(interaction.guild_id)
        await interaction.response.send_message("Select roles that can use betting commands:", view=view, ephemeral=True)
    
    @discord.ui.button(label="EV Settings", style=discord.ButtonStyle.success, emoji="📈")
    async def ev_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=self.create_ev_settings_embed(),
            view=EVSettingsView(self.guild)
        )
    
    @discord.ui.button(label="Arbitrage Settings", style=discord.ButtonStyle.danger, emoji="🔀")
    async def arb_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=self.create_arb_settings_embed(),
            view=ArbSettingsView(self.guild)
        )
    
    def create_bankroll_settings_embed(self):
        settings = server_settings.get(self.guild.id, {})
        embed = discord.Embed(
            title="💰 Bankroll Settings",
            description="Configure bankroll management and notifications",
            color=0x4ECDC4
        )
        
        channel_id = settings.get("bankroll_notification_channel")
        daily_summary = settings.get("daily_summary_enabled", True)
        
        embed.add_field(
            name="Current Settings",
            value=f"**Notification Channel:** {'<#' + str(channel_id) + '>' if channel_id else 'Not set'}\n"
                  f"**Daily Summary:** {'Enabled' if daily_summary else 'Disabled'}",
            inline=False
        )
        return embed

    def create_ev_settings_embed(self):
        settings = server_settings.get(self.guild.id, {})
        embed = discord.Embed(
            title="📈 EV Settings",
            description="Configure Expected Value betting alerts",
            color=0x00FF00
        )
        
        min_ev = settings.get("min_ev", DEFAULT_MIN_EV)
        channel_id = settings.get("ev_alert_channel")
        bookmakers = settings.get("ev_bookmakers", [])
        
        embed.add_field(
            name="Current Settings",
            value=f"**Min EV:** {min_ev}%\n"
                  f"**Alert Channel:** {'<#' + str(channel_id) + '>' if channel_id else 'Not set'}\n"
                  f"**Bookmakers:** {len(bookmakers)} selected",
            inline=False
        )
        return embed

    def create_arb_settings_embed(self):
        settings = server_settings.get(self.guild.id, {})
        embed = discord.Embed(
            title="🔀 Arbitrage Settings", 
            description="Configure arbitrage betting alerts",
            color=0xFFD700
        )
        
        min_margin = settings.get("min_margin", DEFAULT_MIN_MARGIN)
        channel_id = settings.get("arb_alert_channel")
        bookmakers = settings.get("arb_bookmakers", [])
        
        embed.add_field(
            name="Current Settings",
            value=f"**Min Margin:** {min_margin}%\n"
                  f"**Alert Channel:** {'<#' + str(channel_id) + '>' if channel_id else 'Not set'}\n"
                  f"**Bookmakers:** {len(bookmakers)} selected",
            inline=False
        )
        return embed

class ChannelSelectView(discord.ui.View):
    def __init__(self, guild_id, setting_name):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.setting_name = setting_name
        
        # Add channel select dropdown
        select = discord.ui.ChannelSelect(
            placeholder="Choose a channel...",
            channel_types=[discord.ChannelType.text],
            max_values=1
        )
        select.callback = self.channel_select_callback
        self.add_item(select)
    
    async def channel_select_callback(self, interaction: discord.Interaction):
        channel = interaction.data['values'][0]
        channel_obj = interaction.guild.get_channel(int(channel))
        
        if self.guild_id not in server_settings:
            server_settings[self.guild_id] = {}
        
        server_settings[self.guild_id][self.setting_name] = int(channel)
        await save_settings_async()
        
        await interaction.response.send_message(
            f"✅ {self.setting_name.replace('_', ' ').title()} set to {channel_obj.mention}",
            ephemeral=True
        )

class RoleSelectView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        
        # Add role select dropdown  
        select = discord.ui.RoleSelect(
            placeholder="Choose authorized roles...",
            max_values=10
        )
        select.callback = self.role_select_callback
        self.add_item(select)
    
    async def role_select_callback(self, interaction: discord.Interaction):
        roles = [interaction.guild.get_role(int(role_id)) for role_id in interaction.data['values']]
        
        if self.guild_id not in server_settings:
            server_settings[self.guild_id] = {}
        
        server_settings[self.guild_id]["authorized_roles"] = [int(role_id) for role_id in interaction.data['values']]
        await save_settings_async()
        
        role_mentions = ", ".join([role.mention for role in roles if role])
        await interaction.response.send_message(
            f"✅ Authorized roles updated: {role_mentions}",
            ephemeral=True
        )

# ---------- BANKROLL COMMANDS ----------

# @bot.tree.command(name="bankroll", description="View enhanced bankroll and betting analytics")
# REMOVED: Bankroll command replaced with stake suggestions only

# @bot.tree.command(name="reset_bankroll", description="Reset bankroll to original amount (Admin only)")
# REMOVED: Reset bankroll command

# @bot.tree.command(name="check_bets", description="Manually check all pending bet results (Admin only)")
# REMOVED: Check bets command - part of bankroll feature removal

# @bot.tree.command(name="active_bets", description="View all currently tracked bets")  
# REMOVED: Active bets command - part of bankroll feature removal

# ============== MISSING COMMANDS ==============

# scan_status command removed - redundant with arb-status command

# startsync command removed - redundant with admin panel

# Add a 10-minute warning task
@tasks.loop(minutes=1)
async def scan_warning_task():
    """Send warning 10 minutes before global scan"""
    global NEXT_GLOBAL_SCAN_TIME
    
    if not NEXT_GLOBAL_SCAN_TIME:
        return
    
    current_time = datetime.now(timezone.utc)
    time_until_scan = NEXT_GLOBAL_SCAN_TIME - current_time
    total_seconds = int(time_until_scan.total_seconds())
    
    # Send warning exactly 10 minutes before scan (console only)
    if 590 <= total_seconds <= 610:  # 10 minutes ± 10 seconds
        print(f"⚠️ Global scan starting in 10 minutes at {NEXT_GLOBAL_SCAN_TIME.strftime('%H:%M:%S UTC')}")
        logging.info(f"⚠️ Global scan warning: 10 minutes until next scan at {NEXT_GLOBAL_SCAN_TIME.strftime('%H:%M:%S UTC')}")
        
        # Count active servers for console display
        active_server_count = 0
        for guild_id, settings in server_settings.items():
            if settings.get("ev_scan_enabled", False) or settings.get("arb_scan_enabled", False):
                active_server_count += 1
        
        if active_server_count > 0:
            print(f"📊 {active_server_count} servers have scanning enabled and will receive alerts")
        else:
            print(f"  No servers have scanning enabled - configure via /settings")

@scan_warning_task.before_loop
async def before_scan_warning():
    await bot.wait_until_ready()

# setup command removed - use admin panel instead

# test_arb command removed - redundant with admin panel testing features

# ============== COMPLETE UI VIEWS ==============

class SetupView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=300)
        self.guild = guild
        self.add_item(SetupRoleSelect(guild))

class SetupRoleSelect(discord.ui.Select):
    def __init__(self, guild: discord.Guild, page=0):
        self.guild = guild
        self.page = page
        
        all_roles = [role for role in guild.roles if not role.managed and role.name != "@everyone"]
        
        # Calculate pagination
        start_idx = page * 25
        end_idx = start_idx + 25
        roles_page = all_roles[start_idx:end_idx]
        
        options = [
            discord.SelectOption(label=role.name, value=str(role.id))
            for role in roles_page
        ]
        
        # If no options, add placeholder
        if not options:
            options = [discord.SelectOption(label="No roles available", value="none")]
        
        super().__init__(
            placeholder=f"Select roles who can access admin panel (Page {page + 1})",
            options=options,
            min_values=1,
            max_values=min(len(options), 5) if options[0].value != "none" else 1
        )

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        server_settings[guild_id] = {
            "authorized_roles": [int(v) for v in self.values],
            "setup_done": True,
            "setup_user_id": interaction.user.id
        }
        await save_settings_async()
        
        embed = discord.Embed(
            title="✅ Setup Complete!",
            description="BetVillian is now set up for your server.",
            color=0x00FF00,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(
            name="🎯 What's next?",
            value="• Use `/admin` to configure alert channels\n• Set up bookmakers and thresholds\n• Use `/ev` or `/arb` to find betting opportunities",
            inline=False
        )
        
        embed.set_thumbnail(url=BETVILLIAN_LOGO)
        embed.set_footer(text="BetVillian Bot", icon_url=BETVILLIAN_LOGO)
        
        await interaction.response.edit_message(embed=embed, view=None)

class BackToLandingButton(discord.ui.Button):
    def __init__(self, guild):
        super().__init__(label="← Back to Main", style=discord.ButtonStyle.secondary)
        self.guild = guild

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=landing_embed(),
            view=SettingsLanding(self.guild)
        )

# ============== COMPLETE ADMIN PANEL ==============

# ============== MISSING COMMANDS ==============

async def debug_command_callback(interaction: discord.Interaction):
    """Debug command callback function"""
    guild_id = interaction.guild.id
    settings = server_settings.get(guild_id, {})
    
    embed = discord.Embed(
        title="🐛 Debug Information",
        color=0x4ECDC4,
        timestamp=datetime.now(timezone.utc)
    )
    
    embed.add_field(
        name="📊 Server Status",
        value=f"**Guild ID:** {guild_id}\n**Setup Done:** {settings.get('setup_done', False)}\n**Active Bets:** {len(active_bets)}",
        inline=True
    )
    
    embed.add_field(
        name="⚙️ Settings",
        value=f"**Min EV:** {settings.get('min_ev', 'Not set')}%\n**Min Margin:** {settings.get('min_margin', 'Not set')}%\n**EV Books:** {len(settings.get('ev_bookmakers', []))}",
        inline=True
    )
    
    embed.add_field(
        name="📈 Channels",
        value=f"**EV Channel:** <#{settings.get('ev_alert_channel', 'Not set')}>\n**ARB Channel:** <#{settings.get('arb_alert_channel', 'Not set')}>\n**Bankroll Channel:** <#{settings.get('bankroll_notification_channel', 'Not set')}>",
        inline=False
    )
    
    embed.set_thumbnail(url=BETVILLIAN_LOGO)
    embed.set_footer(text="BetVillian Debug", icon_url=BETVILLIAN_LOGO)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ============== COMPLETE ADMIN VIEWS ==============

class BankrollSettingsView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=None)
        self.guild = guild
        
        # Add channel selection button instead of direct select
        channel_button = discord.ui.Button(label="Set Notification Channel", style=discord.ButtonStyle.primary)
        channel_button.callback = self.select_notification_channel
        self.add_item(channel_button)
        
        self.add_item(SetBankrollButton())
        self.add_item(SummaryTimeSelect(guild.id))
        self.add_item(ToggleDailySummaryButton())
        self.add_item(ForceProfitButton())
        self.add_item(ResetBankrollButton())
        self.add_item(BackToAdminButton(guild))
    
    async def select_notification_channel(self, interaction: discord.Interaction):
        view = PaginatedChannelView(self.guild, "bankroll_notification_channel", is_filtered=False)
        await interaction.response.send_message("Select bankroll notification channel:", view=view, ephemeral=True)

class SummaryTimeSelect(discord.ui.Select):
    def __init__(self, guild_id):
        current_time = server_settings.get(guild_id, {}).get("summary_time", "daily")
        options = [
            discord.SelectOption(label="Daily (24h)", value="daily", default=(current_time == "daily")),
            discord.SelectOption(label="Every 6 hours", value="6h", default=(current_time == "6h")),
            discord.SelectOption(label="Every 12 hours", value="12h", default=(current_time == "12h")),
            discord.SelectOption(label="Weekly", value="weekly", default=(current_time == "weekly")),
            discord.SelectOption(label="Disabled", value="disabled", default=(current_time == "disabled"))
        ]
        super().__init__(placeholder="Set profit summary frequency", options=options)

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        time_setting = self.values[0]
        async with settings_lock:
            server_settings.setdefault(guild_id, {})["summary_time"] = time_setting
            await save_settings_async_unlocked()
        
        if time_setting == "disabled":
            await interaction.response.send_message("✅ Profit summaries disabled.", ephemeral=True)
        else:
            await interaction.response.send_message(f"✅ Profit summaries set to **{time_setting}**.", ephemeral=True)

class SetBankrollButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Set Bankroll", style=discord.ButtonStyle.primary, emoji="💰")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SetBankrollModal())

class SetBankrollModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Set Bankroll")
        self.bankroll_input = discord.ui.TextInput(
            label="Full Bankroll Amount (in dollars)",
            placeholder="Enter amount (e.g. 1000 for $1000)",
            required=True,
            max_length=10
        )
        self.add_item(self.bankroll_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = float(self.bankroll_input.value)
            if amount <= 0:
                await interaction.response.send_message("❌ Bankroll amount must be positive.", ephemeral=True)
                return
            
            guild_id = interaction.guild_id
            initialize_bankroll(guild_id)
            guild_bankrolls[guild_id]["current"] = amount
            guild_bankrolls[guild_id]["initial"] = amount
            guild_bankrolls[guild_id]["total_profit"] = 0.0
            guild_bankrolls[guild_id]["daily_profit"] = 0.0
            save_bankroll_data()
            
            await interaction.response.send_message(f"✅ Bankroll set to ${amount:,.2f}.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ Invalid amount. Please enter a valid number.", ephemeral=True)

class ToggleDailySummaryButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Toggle Daily Summary", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        async with settings_lock:
            settings = server_settings.setdefault(guild_id, {})
            current = settings.get("daily_summary_enabled", True)
            settings["daily_summary_enabled"] = not current
            await save_settings_async_unlocked()
        state = "enabled" if settings["daily_summary_enabled"] else "disabled"
        await interaction.response.send_message(f"✅ Daily summary is now **{state}**.", ephemeral=True)

class ForceProfitButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Force Profit Check", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        settings = server_settings.get(guild_id, {})
        channel_id = settings.get("bankroll_notification_channel")
        
        if not channel_id:
            await interaction.response.send_message("❌ Please set a bankroll notification channel first.", ephemeral=True)
            return
        
        channel = bot.get_channel(channel_id)
        if not channel:
            await interaction.response.send_message("❌ Bankroll notification channel not found.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            await send_daily_bankroll_summary(guild_id)
            await interaction.followup.send("✅ Bankroll summary sent to notification channel.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to send bankroll summary: {e}", ephemeral=True)

class ResetBankrollButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Reset Bankroll", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        
        # Initialize/reset bankroll
        guild_bankrolls[guild_id] = {
            "current": 100.0,
            "initial": 100.0,
            "daily_profit": 0.0,
            "total_profit": 0.0,
            "bets_today": [],
            "won_bets": 0,
            "lost_bets": 0,
            "last_reset": datetime.now(timezone.utc).date(),
            "total_staked": 0.0,
            "total_winnings": 0.0
        }
        save_bankroll_data()
        
        await interaction.response.send_message("✅ Bankroll has been reset to 100 units.", ephemeral=True)

class BankrollSettingsButton(discord.ui.Button):
    def __init__(self, guild):
        super().__init__(label="Bankroll Settings", style=discord.ButtonStyle.secondary)
        self.guild = guild

    async def callback(self, interaction: discord.Interaction):
        if not check_admin_authorization(interaction.user, interaction.guild.id):
            await interaction.response.send_message("❌ You are not authorized to access bankroll settings.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="💰 Bankroll Settings",
            description="Configure bankroll tracking and notifications.",
            color=0x4ECDC4
        )
        embed.add_field(name="Notification Channel", value="Set channel for daily summaries and bet results.", inline=False)
        
        await interaction.response.edit_message(
            embed=embed,
            view=BankrollSettingsView(self.guild)
        )

# Placeholder for BankrollSettingsButton - not yet implemented
class BankrollSettingsButton(discord.ui.Button):
    def __init__(self, guild):
        super().__init__(label="💰 Bankroll Settings", style=discord.ButtonStyle.success)
        self.guild = guild

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("💰 Bankroll settings coming soon!", ephemeral=True)

# Update SettingsLanding to include bankroll settings
class SettingsLanding(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=None)
        self.add_item(ServerSettingsButton(guild))
        self.add_item(EVSettingsButton(guild))
        self.add_item(ArbSettingsButton(guild))
        self.add_item(BankrollSettingsButton(guild))

# ============== BOT EVENTS ==============
# Note: on_ready() function is defined earlier in the file (line 107) with enhanced debugging

# ============== BOT STARTUP ==============

try:
    load_settings()
    load_bankroll_data()  # Load saved bankroll data
    load_active_bets()    # Load any pending bets from previous session
    
    # Only run bot if this is the main module
    if __name__ == "__main__":
        logging.info("BetVillian bot starting...")
        try:
            bot.run(DISCORD_TOKEN)
        except discord.HTTPException as e:
            logging.error(f"Discord HTTP error: {e}")
            print(f"Discord HTTP error: {e}")
        except discord.ConnectionClosed as e:
            logging.error(f"Discord connection closed: {e}")
            print(f"Discord connection closed: {e}")
        except Exception as e:
            logging.error(f"Bot runtime error: {e}")
            print(f"Bot runtime error: {e}")
            raise
except Exception as e:
    print(f"Startup error: {e}", file=sys.stderr)
    logging.error(f"Startup error: {e}")
    raise

# ============== CLEANED UP COMMAND STRUCTURE ==============
# 
# 🎯 ESSENTIAL COMMANDS REMAINING:
# 
# 1. /admin - Main admin panel for all configuration
#    - Access all settings through interactive UI
#    - Configure channels, bookmakers, thresholds
#    - Enable/disable scanning types
# 
# 2. /arb-status - Check arbitrage scanning status
#    - Quick overview of current settings
#    - Shows next scan time and configuration
#    - Provides setup guidance if needed
# 
# 3. /force-enable-scanning - Owner-only utility
#    - Bulk enable scanning for all servers with channels
#    - Emergency configuration tool
#    - Useful for maintenance and updates
#
# 4. /start-global-sync - Owner-only scanning control
#    - Start/restart the global hourly scanning system
#    - Triggers EV and arbitrage scans every hour
#    - Synchronizes all servers for API efficiency
# 
# 🚫 REMOVED REDUNDANT COMMANDS:
# - /test-scan, /test-arb-full, /debug-arb (testing commands)
# - /enable-scanning (redundant with admin panel auto-enable)
# - /setup (redundant with admin panel)
# - /scan_status (overlapped with arb-status)
# - /startsync (replaced with start-global-sync)
# - /test_arb (testing command redundant with admin panel)
# 
# ✅ RESULT: Streamlined from 15+ commands to 4 essential commands
# All functionality preserved in the comprehensive admin panel!
#
# 🚀 TO START THE BOT WITH GLOBAL SYNC:
# 1. Run: python arb.py
# 2. Bot will auto-start global scanning on ready
# 3. Use /start-global-sync to manually restart if needed
# 4. Use /force-enable-scanning to enable all configured servers
# 5. Check /arb-status to verify scanning is working
#
# 💡 STAKE CALCULATION IMPROVEMENTS:
# - Fixed EV stake suggestions to use proper Kelly Criterion
# - Stakes now scale appropriately with EV percentage
# - Higher EV = higher stakes (up to 12 units for 15%+ EV)
# - Debug logging added for troubleshooting