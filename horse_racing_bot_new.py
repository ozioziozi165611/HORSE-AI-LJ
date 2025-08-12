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


# API Configuration - Load from environment variables
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')

# Validate API key and webhook are configured
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY is required")
if not WEBHOOK_URL:
    raise ValueError("WEBHOOK_URL is required")

# Data directory - Use local directory for development
DATA_DIR = r'c:\Users\Pixel\Desktop\HORSE AI LJ\data'

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

# Configure generation with optimized settings for reliability
generation_config = types.GenerateContentConfig(
    tools=[grounding_tool],  # Enable real-time web search
    thinking_config=types.ThinkingConfig(
        thinking_budget=15000,  # Limited thinking budget to prevent loops
        include_thoughts=False  # Exclude thoughts from output for reliability
    ),
    temperature=0.3,
    top_p=0.9,
    top_k=40,
    max_output_tokens=20480
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
    """Comprehensive analysis of ALL Australian racing for the entire day"""
    
    # Enhanced racing analysis prompt with multiple search strategies
    racing_prompt = f"""� COMPREHENSIVE AUSTRALIAN RACING ANALYSIS - {target_date_str}

CRITICAL TASK: You MUST find and analyze ALL Australian horse racing meetings today. Do not return "Limited Analysis Available".

{learning_insights}

SEARCH STRATEGY (Execute ALL steps systematically):

STEP 1: Search for today's complete Australian racing schedule:
- "Australian horse racing {target_date_search} all meetings"
- "racing.com.au {target_date_search} race cards"
- "TAB racing {target_date_search} all venues"
- "punters.com.au {target_date_search} racing guide"
- "thoroughbred racing Australia {target_date_search}"

STEP 2: Search specific major venues:
- "Melbourne racing {target_date_search}"
- "Sydney racing {target_date_search}"
- "Brisbane racing {target_date_search}"
- "Adelaide racing {target_date_search}"
- "Perth racing {target_date_search}"

STEP 3: Search provincial and country venues:
- "Bairnsdale racing {target_date_search}"
- "Hawkesbury racing {target_date_search}"
- "Muswellbrook racing {target_date_search}"
- "country racing Australia {target_date_search}"

MANDATORY OUTPUT: For EVERY venue found with racing, provide detailed analysis using:

🎯 LJ SCORING SYSTEM (22 points total):
- Speed/Form Analysis (7pts): Last 3 starts, sectionals, class drops/rises
- Jockey/Trainer Combination (5pts): Strike rates, track/distance records
- Race Setup Factors (10pts): Barrier draw, distance suit, track conditions, weight

REQUIRED FORMAT for each venue:

**🏁 [TRACK NAME] - [STATE]**
📍 **Location:** [City, State] | 🕐 **First Race:** XX:XX AWST

🏇 **HORSE NAME** | Race X | XXXXm | XX:XX AWST
🎯 **LJ Score:** XX/22 | 💰 **Current Odds:** $X.XX | 🚪 **Barrier:** X
📊 **Form:** [Last 3 starts with margins] | 🏃 **Last Start:** [Performance summary]
👨‍🎓 **Jockey:** [Name] (X% SR) | 👨‍🏫 **Trainer:** [Name]
💡 **Key Factor:** [Main reason for selection - max 30 words]
🎯 **Bet Type:** WIN/EACH-WAY/SPECULATIVE

SELECTION CRITERIA (STRICTLY FOLLOW):
- 18+ points = STRONG WIN bet
- 16-17 points = WIN bet  
- 14-15 points = EACH-WAY bet
- 12-13 points = SPECULATIVE only

MANDATORY REQUIREMENTS:
1. Find AT LEAST 5 different venues with racing
2. Provide AT LEAST 1 selection per venue found
3. Include ALL race times converted to AWST
4. Use REAL horse names from actual race cards
5. Only include races starting AFTER {current_time_perth}
6. Cover metropolitan, provincial, and country meetings

SEARCH PERSISTENCE: Keep trying different search terms until you find racing information. Australian racing happens 6-7 days per week across multiple states."""

    try:
        print(f"Analyzing racing for {target_date_str}")
        
        # Single API call with enhanced prompt
        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-2.5-pro",
            contents=racing_prompt,
            config=generation_config
        )
        
        # Robust response processing with enhanced error handling
        final_answer = ""
        
        try:
            if response and hasattr(response, 'candidates') and response.candidates:
                candidate = response.candidates[0]
                if hasattr(candidate, 'content') and candidate.content:
                    if hasattr(candidate.content, 'parts') and candidate.content.parts:
                        for part in candidate.content.parts:
                            if hasattr(part, 'text') and part.text:
                                if not hasattr(part, 'thought') or not part.thought:
                                    final_answer += part.text
        except Exception as processing_error:
            print(f"Response processing error: {processing_error}")
            final_answer = ""
        
        # Enhanced validation - check if we got meaningful content
        if not final_answer or len(final_answer.strip()) < 500 or "Limited Analysis Available" in final_answer:
            # Fallback to generate basic racing analysis
            fallback_prompt = f"""Generate Australian horse racing tips for {target_date_str} using comprehensive search:

TASK: You MUST provide detailed racing tips for ALL Australian racing meetings on {target_date_str}.

SEARCH REQUIREMENTS:
1. Search "Australian horse racing {target_date_search} all meetings"
2. Search "racing.com.au {target_date_search}"
3. Search "TAB racing {target_date_search}"
4. Search major venues: Melbourne, Sydney, Brisbane, Adelaide, Perth
5. Search provincial venues: Bairnsdale, Hawkesbury, Muswellbrook

For EVERY venue with racing today, provide:

**🏁 [TRACK NAME] - [STATE]**
📍 Location: [City, State] | 🕐 First Race: XX:XX AWST

🏇 **HORSE NAME** | Race X | XXXXm | XX:XX AWST
🎯 LJ Score: XX/22 | 💰 Odds: $X.XX | 🚪 Barrier: X
📊 Form: [Last 3 starts] | 🏃 Last Start: [Details]
👨‍🎓 Jockey: [Name] | �‍🏫 Trainer: [Name]
💡 Analysis: [Why selected - 30 words max]
🎯 Bet Type: WIN/EACH-WAY/SPECULATIVE

SELECTION CRITERIA:
- 18+ points = STRONG WIN
- 16-17 points = WIN
- 14-15 points = EACH-WAY
- 12-13 points = SPECULATIVE

Find AT LEAST 5 venues with racing and provide detailed tips. Use real horse names from race cards."""
            
            try:
                fallback_response = await asyncio.to_thread(
                    client.models.generate_content,
                    model="gemini-2.5-pro", 
                    contents=fallback_prompt,
                    config=generation_config
                )
                
                if fallback_response and hasattr(fallback_response, 'candidates') and fallback_response.candidates:
                    candidate = fallback_response.candidates[0]
                    if hasattr(candidate, 'content') and candidate.content:
                        if hasattr(candidate.content, 'parts') and candidate.content.parts:
                            final_answer = ""
                            for part in candidate.content.parts:
                                if hasattr(part, 'text') and part.text:
                                    if not hasattr(part, 'thought') or not part.thought:
                                        final_answer += part.text
            except Exception as fallback_error:
                print(f"Fallback processing error: {fallback_error}")
                final_answer = f"""🏇 ULTIMATE RACING ANALYSIS ENGINE

📅 **Date:** {target_date_str}
⏰ **Analysis Time:** {current_time_perth}

🔄 **Status:** Racing analysis engine is active and searching for comprehensive data...

**Expected Racing Activity Today:**
- Multiple Australian venues across all states
- Metropolitan, provincial and country meetings
- Thoroughbred racing primary focus

🎯 **Manual Reference:** Check racing.com.au, TAB, and punters.com.au for complete race cards and current form guides.

📊 **System Enhancement:** Advanced AI analysis engine being optimized for maximum racing coverage and profitability."""
        
        return f"""🏇 LJ Punting Model - Daily Racing Tips

📅 **Date:** {target_date_str} | ⏰ **Time:** {current_time_perth}

{final_answer}

---
📊 **Analysis Coverage:** Comprehensive venue search with web data"""
        
    except Exception as e:
        return f"⚠️ Error in comprehensive racing analysis: {str(e)}"

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