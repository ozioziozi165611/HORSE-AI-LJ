import discord
from discord import Webhook
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
from datetime import datetime, timedelta, time as dtime

# API Configuration (Railway environment variables - NO DEFAULTS for security)
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')

# Validate required environment variables
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY environment variable is required")
if not WEBHOOK_URL:
    raise ValueError("WEBHOOK_URL environment variable is required")

# Data directory (Railway volume mount recommended: /data)
DATA_DIR = os.getenv('DATA_DIR', '/tmp/data')  # Default to /tmp/data for Railway

# Learning system files (within DATA_DIR)
LEARNING_DATA_FILE = os.path.join(DATA_DIR, 'racing_learning_data.json')
DAILY_PREDICTIONS_FILE = os.path.join(DATA_DIR, 'daily_predictions.json')

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
    """Ensure data directory and JSON files exist (Railway-friendly)."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
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

# Perth timezone
PERTH_TZ = pytz.timezone('Australia/Perth')

# Initialize Gemini client with proper SDK
client = genai.Client(api_key=GEMINI_API_KEY)

# Define grounding tool for REAL web search
grounding_tool = types.Tool(google_search=types.GoogleSearch())

# Configure generation with deep thinking AND real web search
generation_config = types.GenerateContentConfig(
    tools=[grounding_tool],  # Enable real-time web search
    thinking_config=types.ThinkingConfig(
        thinking_budget=-1,  # Dynamic thinking
        include_thoughts=True  # Include reasoning process
    ),
    temperature=0.2,
    top_p=0.8,
    top_k=30,
    max_output_tokens=16384
)

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

async def generate_horse_tips():
    """Generate tips for today's races only (Perth timezone)"""
    perth_now = datetime.now(PERTH_TZ)
    target_date_str = perth_now.strftime("%B %d, %Y")
    target_date_search = perth_now.strftime("%Y-%m-%d")
    current_time_perth = perth_now.strftime("%H:%M AWST")
    
    print(f"Generating tips for {target_date_str} at {current_time_perth}")
    
    # Get learning insights
    learning_insights = get_learning_enhanced_prompt()
    
    # Analyze today's races only
    tips_result = await analyze_racing_day(target_date_str, target_date_search, current_time_perth, learning_insights)
    
    # Save predictions for evening analysis
    predictions_data = {
        'date': target_date_search,
        'predictions': extract_predictions_for_learning(tips_result),
        'generated_at': current_time_perth
    }
    save_daily_predictions(predictions_data)
    
    return tips_result

def extract_predictions_for_learning(tips_content):
    """Extract predictions from tips content for later learning analysis"""
    predictions = []
    lines = tips_content.split('\n')
    
    current_horse = {}
    for line in lines:
        if line.startswith('🏇 **') and '**' in line:
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
        elif current_horse and any(keyword in line for keyword in ['LJ Score:', 'Race Time:', 'Track:', 'BET TYPE:']):
            current_horse['prediction_details'].append(line)
        elif current_horse and line.startswith('💡 **Analysis:**'):
            current_horse['analysis'] = line
    
    if current_horse:
        predictions.append(current_horse)
    
    return predictions

async def analyze_results_and_learn():
    """Analyze today's race results (Perth date) and learn from predictions"""
    perth_now = datetime.now(PERTH_TZ)
    today_str = perth_now.strftime('%Y-%m-%d')
    
    print(f"Analyzing results and learning for {today_str}")
    
    # Load today's predictions
    predictions_data = load_daily_predictions()
    if not predictions_data.get('predictions'):
        print("No predictions found for today")
        return "No predictions to analyze for today."
    
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
        # Get race results using web search
        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-2.5-pro",
            contents=results_prompt,
            config=generation_config
        )
        
        results_content = ""
        for part in response.candidates[0].content.parts:
            if not hasattr(part, 'thought') or not part.thought:
                results_content += part.text
        
        # Analyze our predictions against results
        learning_analysis = await analyze_prediction_accuracy(predictions_data, results_content)
        
        # Send results and learning update to Discord
        await send_webhook_message(
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
    """Check if the tips content contains any strong bets (16+ points)"""
    # Look for strong bet indicators in the response
    strong_bet_indicators = [
        "STRONG BETS (16+ Points)",
        "LJ Score: 16/22",
        "LJ Score: 17/22", 
        "LJ Score: 18/22",
        "LJ Score: 19/22",
        "LJ Score: 20/22",
        "LJ Score: 21/22",
        "LJ Score: 22/22",
        "BET TYPE:** WIN"
    ]
    
    # Check if any strong bet indicators are present
    for indicator in strong_bet_indicators:
        if indicator in tips_content:
            return True
    
    # Also check for phrases that indicate no strong bets found
    no_strong_bets_phrases = [
        "No horses met the 16+ criteria",
        "no horses meeting the 16+ criteria",
        "No horses with 16+ points found",
        "All selections are each-way"
    ]
    
    for phrase in no_strong_bets_phrases:
        if phrase in tips_content:
            return False
    
    return False

def extract_summary(tips_content):
    """Extract a brief summary from tips content for display when no strong bets found"""
    lines = tips_content.split('\n')
    summary_lines = []
    
    # Look for key information lines
    for line in lines:
        if any(keyword in line.lower() for keyword in ['each-way options', 'no horses met', 'analysis summary', 'track conditions']):
            summary_lines.append(line)
        elif 'LJ Score: 14/22' in line or 'LJ Score: 15/22' in line:
            summary_lines.append(line)
        elif line.startswith('🏇') and '|' in line:
            summary_lines.append(line)
    
    # If we found specific content, return it
    if summary_lines:
        return '\n'.join(summary_lines[:10])  # Limit to first 10 relevant lines
    
    # Otherwise return a basic summary
    if 'each-way' in tips_content.lower():
        return "⚖️ Some each-way options (14-15 points) were identified, but no strong bets (16+ points) found."
    else:
        return "❌ No qualifying selections found for this day."

async def analyze_racing_day(target_date_str, target_date_search, current_time_perth, learning_insights):
    """Analyze TODAY only (Perth date) with learning insights"""
    
    # Enhanced prompt with MANDATORY web search for REAL data, today only
    main_prompt = f"""🌐 LIVE WEB SEARCH REQUIRED - REAL AUSTRALIAN RACING DATA (TODAY ONLY)

Current Time: {current_time_perth} (Perth/AWST)
Target Analysis: today's races for {target_date_str} ONLY

{learning_insights}

You MUST use your web search capabilities to find REAL racing information for TODAY ({target_date_str}).

MANDATORY COMPREHENSIVE WEB SEARCHES TO PERFORM:

📊 RACE CARDS & BASIC DATA:
1. Search: "Australian horse racing {target_date_search} race cards TAB racing.com"
2. Search: "Randwick Moonee Valley Eagle Farm Belmont {target_date_str} race meetings"
3. Search: "NSW VIC QLD SA WA horse racing {target_date_str} full cards"
4. Search: "racing.com Australia {target_date_search} complete race cards"
5. Search: "TAB racing Australia {target_date_str} all meetings times barriers"

⚡ FORM & SECTIONALS DATA:
6. Search: "Australian racing form guide {target_date_search} sectional times"
7. Search: "horse racing sectionals Australia {target_date_str} 200m 400m 600m splits"
8. Search: "racing post Australia form guide {target_date_search}"
9. Search: "punters.com.au form guide {target_date_str}"
10. Search: "timeform Australia racing {target_date_search}"

🏇 JOCKEY & TRAINER STATS:
11. Search: "Australian jockey statistics {target_date_str} win rates last 30 days"
12. Search: "trainer statistics Australia racing {target_date_search} strike rates"
13. Search: "racing Australia jockey trainer form current season"

💬 COMMENTS & TRIALS:
14. Search: "horse racing trials Australia recent {target_date_search} results"
15. Search: "Australian racing media tips {target_date_str} trainer comments"
16. Search: "horse racing stable comments Australia {target_date_search}"
17. Search: "racing analysts tips Australia {target_date_str}"

🌦️ TRACK CONDITIONS & BREEDING:
18. Search: "Australian race track conditions {target_date_str} heavy soft good"
19. Search: "horse breeding sire dam track conditions Australia"
20. Search: "track bias Australia racing {target_date_str} patterns"

📈 ODDS & MARKET DATA:
21. Search: "TAB odds Australia {target_date_str} current betting markets"
22. Search: "sportsbet racing odds Australia {target_date_search}"
23. Search: "bet365 horse racing Australia {target_date_str}"

🔍 SCRATCHINGS & LATE MAIL:
24. Search: "horse racing scratchings Australia {target_date_search} late withdrawals"
25. Search: "Australian racing late mail {target_date_str} insider tips"

CURRENT TIME FILTERING:
- Use AWST times; ONLY include races that haven't started yet
- Check race start times and exclude completed races

🧠 ULTIMATE HORSE RACING AI TASK PROMPT
"LJ PUNTING MODEL – AUSTRALIAN WIN-ONLY HORSE RACING TIPS ENGINE"

🎯 OBJECTIVE:
Use COMPREHENSIVE web search across 25+ data sources to find REAL Australian races for {target_date_str}, then apply the enhanced LJ Punting Model (22-point evaluation) with intelligent data synthesis to identify both strong bets (16+) and each-way opportunities (14-15).

🚨 PRIORITY MANDATE: Focus heavily on finding horses that can reach 16+ points. Be generous with scoring when horses show strong form indicators, trainer/jockey combinations, or market confidence. The goal is to identify genuine betting opportunities, not just catalog horses.

CRITICAL ANALYSIS APPROACH:
- You MUST search extensively across multiple racing websites
- Cross-reference data from racing.com, TAB, punters.com.au, timeform, and racing media
- Use intelligent pattern recognition when exact sectionals unavailable
- Leverage trainer/jockey strike rates as strong predictive indicators
- Consider market movements and stable confidence signals
- Apply breeding analysis for track condition suitability
- Do NOT create fictional data - use real information and intelligent analysis

✅ LJ PUNTING MODEL – 22-POINT CHECKLIST:

🔥 Sectionals & Speed Figures (3)
- ✅ Top 3 splits in last race (200m, 400m, 600m)
- ✅ Last 600m among fastest of the day
- ✅ Strong recent public trial (top 3 or professional under light riding)

🗣️ Comments & Public Signals (2)
- ✅ Jockey/trainer comments suggest horse will improve over further
- ✅ General public/media/stable comments: "ready to win", "improving", "pleased with work"

🌦️ Track & Surface Factors (3)
- ✅ Proven on today's official track rating
- ✅ Has run on today's surface type (Turf or Synthetic)
- ✅ Bred to handle today's ground based on sire/dam lineage

🏇 Form & Competitive Strength (4)
- ✅ Has beaten a runner in this field during this prep
- ✅ Has beaten a mutual horse that beat a rival here
- ✅ Has won in a similar class/benchmark
- ✅ Has previously won at this stage of prep

⚖️ Weight & Jockey (3)
- ✅ Has won with today's jockey or same weight (±1.5kg)
- ✅ Jockey has ≥15% win rate last 30 days
- ✅ Horse has previously run well under this jockey

🧪 Trainer & Stable Confidence (2)
- ✅ Trainer has ≥15% win rate last 30 days
- ✅ Trainer has strong historical strike-rate at venue

🧭 Race Setup & Tactics (5)
- ✅ Track pattern suits horse's running style
- ✅ Main rivals are drawn poorly
- ✅ Field size advantages
- ✅ Proven in track direction
- ✅ Proven from similar barrier positions

📦 REQUIRED OUTPUT FORMAT:

**WEB SEARCH RESULTS:**
[List what REAL racing data you found from your web searches]
[Specify which races are still to run vs completed]

**REAL SELECTIONS:**
For each qualifying REAL horse (MUST be 16+ out of 22 criteria) from races yet to run:

🚨 ENHANCED ANALYSIS REQUIREMENTS:

SCORING FLEXIBILITY:
- Primary Target: Horses scoring 16+ out of 22 criteria for STRONG BETS
- Secondary Target: Horses scoring 14-15 out of 22 criteria for EACH-WAY considerations
- AGGRESSIVE SCORING: When assessing criteria, be generous with points for horses showing strong indicators
- Credit partial points (0.5) for criteria that are nearly met or strongly indicated by available data
- Use intelligent pattern recognition to award points when direct data unavailable but strong signals present
- Cross-reference multiple sources to build confidence in scoring decisions

DATA SOURCING STRATEGY:
- Cross-reference minimum 3 different websites per horse
- Use historical patterns when current sectionals unavailable
- Leverage trainer/jockey statistics as strong indicators
- Consider stable confidence signals and media commentary
- Factor in breeding lines for track condition suitability

MINIMUM SELECTION CRITERIA:
- ONLY use data found through comprehensive web search
- ONLY include REAL horses from races that haven't run yet
- Cross-verify information across multiple racing websites
- Maximum 15 selections total (mix of strong bets and each-way options)
- Clearly distinguish between 16+ (STRONG) and 14-15 (EACH-WAY) selections

📋 FINAL OUTPUT FORMAT FOR DISCORD (AWST times):

**🔥 STRONG BETS (16+ Points):**
Present strong selections in this format:

🏇 **HORSE NAME** | Track Name | Race X
⏰ **Race Time:** XX:XX AWST | 📏 **Distance:** XXXXm
🎯 **LJ Score:** XX/22 | 💰 **Odds:** $X.XX | 🚪 **Barrier:** X
🌦️ **Track:** Condition | 📊 **Analysis Score:** X.X/10
✅ **Status:** Still to run | 🔥 **BET TYPE:** WIN

💡 **Analysis:** [100-word analysis based on real data]
🔍 **Sources:** [Web sources verified]

---

**⚖️ EACH-WAY OPTIONS (14-15 Points):**
Present each-way selections in this format:

🏇 **HORSE NAME** | Track Name | Race X
⏰ **Race Time:** XX:XX AWST | 📏 **Distance:** XXXXm
🎯 **LJ Score:** XX/22 | 💰 **Odds:** $X.XX | 🚪 **Barrier:** X
🌦️ **Track:** Condition | 📊 **Analysis Score:** X.X/10
✅ **Status:** Still to run | ⚖️ **BET TYPE:** EACH-WAY

💡 **Analysis:** [100-word analysis with risk factors noted]
🔍 **Sources:** [Web sources verified]

---

BEGIN WEB SEARCH FOR REAL AUSTRALIAN RACING DATA FOR {target_date_str} (TODAY ONLY) NOW."""

    try:
        # Generate racing tips using REAL web search + deep thinking
        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-2.5-pro",
            contents=main_prompt,
            config=generation_config
        )
        
        # Process response parts to separate thoughts from final answer
        final_answer = ""
        thought_summary = ""
        
        for part in response.candidates[0].content.parts:
            if hasattr(part, 'thought') and part.thought:
                thought_summary += f"🤔 Deep Analysis: {part.text}\n\n"
            else:
                final_answer += part.text
        
        # Combine thought summary with final answer if available
        if thought_summary:
            return f"{thought_summary}📊 **FINAL RACING TIPS:**\n\n{final_answer}"
        else:
            return final_answer
        
    except Exception as e:
        return f"⚠️ Error generating tips: {str(e)}"

async def send_webhook_message(content, title="🏇 LJ Punting Model - Daily Racing Tips"):
    try:
        async with aiohttp.ClientSession() as session:
            webhook = Webhook.from_url(WEBHOOK_URL, session=session)
            
            # Create and send the embed
            embed = discord.Embed(
                title=title,
                description=content[:4096] if len(content) > 4096 else content,
                color=0x00ff00
            )
            embed.set_footer(text=f"Generated on {datetime.now(PERTH_TZ).strftime('%B %d, %Y at %H:%M AWST')}")
            
            # Split content into multiple embeds if too long
            if len(content) > 4096:
                remaining_content = content[4096:]
                await webhook.send(embed=embed)
                
                while remaining_content:
                    chunk = remaining_content[:4096]
                    remaining_content = remaining_content[4096:]
                    
                    embed = discord.Embed(description=chunk, color=0x00ff00)
                    await webhook.send(embed=embed)
                    await asyncio.sleep(1)  # Small delay between messages
            else:
                await webhook.send(embed=embed)
                
    except Exception as e:
        print(f"Error sending webhook: {str(e)}")

async def main():
    print("Starting Horse Racing Tips Bot (Perth schedule)")
    print(f"Current Perth time: {datetime.now(PERTH_TZ).strftime('%Y-%m-%d %H:%M AWST')}")
    print(f"Data directory: {DATA_DIR}")
    print(f"API Key configured: {'Yes' if GEMINI_API_KEY else 'No'}")
    print(f"Webhook configured: {'Yes' if WEBHOOK_URL else 'No'}")
    
    # Ensure data directory and files exist for Railway
    ensure_data_dir_and_files()
    
    mode = os.environ.get('RUN_MODE', 'once').lower()
    if mode == 'schedule':
        # Simple scheduler loop checking Perth time every minute
        print("Running in scheduler mode: will post at 07:00 and 19:00 AWST daily")
        last_run_date_morning = None
        last_run_date_evening = None
        try:
            while True:
                now_perth = datetime.now(PERTH_TZ)
                today = now_perth.date()
                current_time = now_perth.time()
                
                # 7:00 AM run
                if current_time >= dtime(7, 0) and (last_run_date_morning != today):
                    print("Triggering 7AM tips run...")
                    tips = await generate_horse_tips()
                    await send_webhook_message(tips)
                    last_run_date_morning = today
                
                # 7:00 PM run
                if current_time >= dtime(19, 0) and (last_run_date_evening != today):
                    print("Triggering 7PM results analysis run...")
                    await analyze_results_and_learn()
                    last_run_date_evening = today
                
                await asyncio.sleep(60)
        except KeyboardInterrupt:
            print("Scheduler stopped")
    else:
        # One-off immediate run (morning tips)
        try:
            print("Generating tips (one-off)...")
            tips = await generate_horse_tips()
            print("Sending tips to Discord...")
            await send_webhook_message(tips)
            print("Done.")
        except Exception as e:
            print(f"Error: {str(e)}")

# Run the script
if __name__ == "__main__":
    asyncio.run(main())
