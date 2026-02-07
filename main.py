import os
import sqlite3
import random
import string
import json
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
import telebot
from telebot import types
import threading
import time

app = Flask(__name__)
CORS(app)

# ==================== SETTINGS ====================
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    print("❌ ERROR: TELEGRAM_TOKEN not set!")

# Admin IDs
ADMIN_IDS = []
admin_ids_str = os.environ.get('ADMIN_IDS', '')
if admin_ids_str:
    try:
        ADMIN_IDS = [int(id.strip()) for id in admin_ids_str.split(',')]
    except:
        ADMIN_IDS = []
        print("⚠️ Could not parse ADMIN_IDS")

# Server URL
SERVER_URL = os.environ.get('SERVER_URL', 'https://telegolf1d.onrender.com')

# Initialize bot
bot = telebot.TeleBot(TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None

# ==================== GAME PARAMETERS ====================
# Hole distances
GOLF_HOLES = [100, 150, 200, 120, 180, 90, 160, 210, 130, 
              140, 170, 110, 190, 125, 140, 160, 195, 105]

# Difficulty levels (tolerance)
DIFFICULTY_LEVELS = {
    1: {"name": "Novice", "tolerance": 10},
    2: {"name": "Amateur", "tolerance": 7},
    3: {"name": "Pro", "tolerance": 5},
    4: {"name": "Master", "tolerance": 3}
}

# Game expiration time
GAME_EXPIRE_HOURS = 24

# ==================== DATABASE ====================
def init_database():
    conn = sqlite3.connect('golf_league.db', check_same_thread=False)
    cursor = conn.cursor()
    
    # Games table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS games (
            game_id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_code TEXT UNIQUE NOT NULL,
            telegram_id INTEGER NOT NULL,
            player_name TEXT,
            difficulty INTEGER DEFAULT 1,
            current_hole INTEGER DEFAULT 1,
            remaining INTEGER DEFAULT 0,
            accumulated_revolutions INTEGER DEFAULT 0,
            shots_on_current_hole INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            total_shots INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            expires_at TIMESTAMP
        )
    ''')
    
    # Shots table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shots (
            shot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_code TEXT NOT NULL,
            device_id TEXT,
            hole_number INTEGER NOT NULL,
            revolutions INTEGER NOT NULL,
            remaining_before INTEGER NOT NULL,
            remaining_after INTEGER NOT NULL,
            is_success BOOLEAN NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (game_code) REFERENCES games(game_code)
        )
    ''')
    
    # Player stats table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS player_stats (
            player_id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            total_games INTEGER DEFAULT 0,
            completed_games INTEGER DEFAULT 0,
            total_shots INTEGER DEFAULT 0,
            best_score INTEGER DEFAULT 999,
            last_played TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Leaderboard table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS leaderboard (
            record_id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            game_code TEXT NOT NULL,
            total_score INTEGER NOT NULL,
            difficulty INTEGER DEFAULT 1,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (telegram_id) REFERENCES player_stats(telegram_id),
            FOREIGN KEY (game_code) REFERENCES games(game_code)
        )
    ''')
    
    # Indexes
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_games_code ON games(game_code)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_games_telegram ON games(telegram_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_shots_code ON shots(game_code)')
    
    conn.commit()
    conn.close()
    print("✅ Database initialized")

# ==================== HELPER FUNCTIONS ====================
def generate_game_code():
    """Generate unique 6-character game code"""
    while True:
        code = ''.join(random.choices(string.ascii_uppercase, k=6))
        conn = sqlite3.connect('golf_league.db', check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('SELECT game_code FROM games WHERE game_code = ?', (code,))
        if not cursor.fetchone():
            conn.close()
            return code
        conn.close()

def parse_datetime(dt_str):
    """Universal datetime parser"""
    if not dt_str:
        return None
    try:
        return datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S.%f')
    except ValueError:
        try:
            return datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            try:
                return datetime.strptime(dt_str, '%Y-%m-%d')
            except ValueError:
                print(f"⚠️ Could not parse date: {dt_str}")
                return None

def update_player_stats(telegram_id, username, first_name, game_code):
    """Update player statistics"""
    conn = sqlite3.connect('golf_league.db', check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        # Get number of shots in game
        cursor.execute('SELECT COUNT(*) FROM shots WHERE game_code = ?', (game_code,))
        total_shots = cursor.fetchone()[0] or 0
        
        # Update player stats
        cursor.execute('''
            INSERT OR REPLACE INTO player_stats 
            (telegram_id, username, first_name, total_games, completed_games, 
             total_shots, best_score, last_played)
            VALUES (?, ?, ?, 
                COALESCE((SELECT total_games + 1 FROM player_stats WHERE telegram_id = ?), 1),
                COALESCE((SELECT completed_games + 1 FROM player_stats WHERE telegram_id = ?), 1),
                COALESCE((SELECT total_shots FROM player_stats WHERE telegram_id = ?), 0) + ?,
                CASE 
                    WHEN ? < COALESCE((SELECT best_score FROM player_stats WHERE telegram_id = ?), 999) 
                    THEN ? 
                    ELSE COALESCE((SELECT best_score FROM player_stats WHERE telegram_id = ?), 999)
                END,
                ?
            )
        ''', (telegram_id, username, first_name, 
              telegram_id, telegram_id, telegram_id, total_shots,
              total_shots, telegram_id, total_shots, telegram_id,
              datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        
        conn.commit()
    except Exception as e:
        print(f"❌ Error updating stats: {e}")
    finally:
        conn.close()

def send_telegram_update(game_code, message_type, data):
    """Send Telegram notifications for key events"""
    if not bot:
        return
    
    conn = sqlite3.connect('golf_league.db', check_same_thread=False)
    cursor = conn.cursor()
    
    # Get game info
    cursor.execute('''
        SELECT telegram_id, player_name, current_hole, total_shots, difficulty
        FROM games WHERE game_code = ?
    ''', (game_code,))
    
    game = cursor.fetchone()
    
    if not game:
        conn.close()
        return
    
    telegram_id, player_name, current_hole, total_shots, difficulty = game
    
    # Form message based on event type
    if message_type == 'hole_completed':
        tolerance = DIFFICULTY_LEVELS.get(difficulty, DIFFICULTY_LEVELS[1])["tolerance"]
        message = f"""
🎉 <b>Hole completed!</b>

🕳️ <b>Hole:</b> {data['hole_number']}
🎯 <b>Hole width:</b> ±{tolerance}
🏌️ <b>Shots on hole:</b> {data['shots_on_hole']}
🏆 <b>Total shots:</b> {total_shots}

📏 <b>Next hole:</b> {data['next_hole_distance']} revolutions

🌐 <b>Track online:</b>
{SERVER_URL}/game/{game_code}
"""
    elif message_type == 'game_completed':
        message = f"""
🏆 <b>GAME COMPLETED!</b>

🎮 <b>Final result:</b>
🕳️ Holes completed: 18
🏌️ Total shots: {total_shots}

🎯 <b>Congratulations, {player_name}!</b>

📊 <b>View statistics:</b> /stats
🎮 <b>Start new game:</b> /play

🌐 <b>Final page:</b>
{SERVER_URL}/game/{game_code}
"""
    else:
        conn.close()
        return
    
    try:
        bot.send_message(telegram_id, message, parse_mode='HTML')
    except Exception as e:
        print(f"⚠️ Telegram notification error: {e}")
    
    conn.close()

# ==================== ESP32 API ====================
@app.route('/')
def home():
    """Server home page"""
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Spinner Golf - Server</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { 
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
                max-width: 1000px; 
                margin: 0 auto; 
                padding: 20px; 
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                color: white;
            }
            .container {
                background: rgba(255, 255, 255, 0.95);
                padding: 40px;
                border-radius: 20px;
                color: #333;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            }
            .header { 
                text-align: center; 
                margin-bottom: 40px; 
            }
            h1 {
                color: #667eea;
                font-size: 2.5em;
                margin-bottom: 10px;
            }
            .status-badge {
                display: inline-block;
                background: #4CAF50;
                color: white;
                padding: 10px 25px;
                border-radius: 25px;
                font-weight: bold;
                margin: 10px 0;
            }
            .card { 
                background: white;
                padding: 25px; 
                border-radius: 15px; 
                margin: 25px 0; 
                box-shadow: 0 5px 20px rgba(0,0,0,0.1);
                border-left: 5px solid #667eea;
            }
            .card h3 {
                color: #667eea;
                margin-top: 0;
            }
            .feature-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
                gap: 20px;
                margin: 30px 0;
            }
            .feature {
                background: #f8f9fa;
                padding: 20px;
                border-radius: 10px;
                text-align: center;
            }
            .feature-icon {
                font-size: 2.5em;
                margin-bottom: 15px;
            }
            .api-link {
                background: #f0f0f0;
                padding: 12px;
                border-radius: 8px;
                font-family: monospace;
                margin: 10px 0;
                word-break: break-all;
            }
            .btn {
                display: inline-block;
                background: #667eea;
                color: white;
                padding: 12px 25px;
                border-radius: 8px;
                text-decoration: none;
                font-weight: bold;
                margin: 10px 5px;
                transition: transform 0.3s ease, background 0.3s ease;
            }
            .btn:hover {
                background: #764ba2;
                transform: translateY(-3px);
            }
            .telegram-status {
                background: #0088cc;
                color: white;
                padding: 8px 15px;
                border-radius: 20px;
                font-size: 0.9em;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🏌️ Spinner Golf - Server v3.4</h1>
                <div class="status-badge">
                    🟢 Server is running | 
                    <span class="telegram-status">🤖 Telegram bot: ''' + ('Active' if TELEGRAM_TOKEN else 'Not configured') + '''</span>
                </div>
            </div>
            
            <div class="card">
                <h3>🎮 How to play:</h3>
                <div class="feature-grid">
                    <div class="feature">
                        <div class="feature-icon">🤖</div>
                        <h4>1. Find bot</h4>
                        <p>Find bot in Telegram <code>@spinner_golf_bot</code></p>
                    </div>
                    <div class="feature">
                        <div class="feature-icon">🎯</div>
                        <h4>2. Start game</h4>
                        <p>Send command <code>/play</code></p>
                    </div>
                    <div class="feature">
                        <div class="feature-icon">📱</div>
                        <h4>3. Configure ESP32</h4>
                        <p>Enter code and select difficulty</p>
                    </div>
                    <div class="feature">
                        <div class="feature-icon">🌐</div>
                        <h4>4. Track online</h4>
                        <p>Open tracking link</p>
                    </div>
                </div>
            </div>
            
            <div class="card">
                <h3>🎯 Difficulty levels:</h3>
                <div class="feature-grid">
                    <div class="feature">
                        <h4>🥳 Novice</h4>
                        <p>Hole width: ±10 revolutions</p>
                    </div>
                    <div class="feature">
                        <h4>😊 Amateur</h4>
                        <p>Hole width: ±7 revolutions</p>
                    </div>
                    <div class="feature">
                        <h4>🤔 Pro</h4>
                        <p>Hole width: ±5 revolutions</p>
                    </div>
                    <div class="feature">
                        <h4>😎 Master</h4>
                        <p>Hole width: ±3 revolutions</p>
                    </div>
                </div>
            </div>
            
            <div class="card">
                <h3>📡 API for ESP32:</h3>
                <p><strong>Endpoints:</strong></p>
                <div class="api-link">POST /api/handle_request</div>
                <p>Main endpoint for all requests</p>
                
                <div class="api-link">GET /api/status</div>
                <p>Check server status</p>
            </div>
            
            <div class="card">
                <h3>🔗 Useful links:</h3>
                <a href="https://t.me/spinner_golf_bot" class="btn" target="_blank">🤖 Telegram bot</a>
                <a href="/api/status" class="btn" target="_blank">📡 Check API</a>
                
                <h4 style="margin-top: 20px;">📱 Game tracking:</h4>
                <p>After starting game you'll get link like:</p>
                <div class="api-link">''' + SERVER_URL + '''/game/ABC123</div>
                <p>Share this link with friends!</p>
            </div>
            
            <div style="text-align: center; margin-top: 40px; padding-top: 20px; border-top: 1px solid #eee;">
                <p>Spinner Golf v3.4 | Simplified one-dimensional golf with difficulty choice</p>
                <p style="color: #666; font-size: 0.9em;">Server automatically updates every 10 seconds</p>
            </div>
        </div>
    </body>
    </html>
    '''

@app.route('/api/status')
def api_status():
    """Check server status"""
    return jsonify({
        'status': 'ok',
        'server': 'Spinner Golf v3.4',
        'timestamp': datetime.now().isoformat()
    })

@app.route('/api/handle_request', methods=['POST'])
def handle_request():
    """Main handler for all ESP32 requests"""
    try:
        init_database()
        
        data = request.json
        if not data:
            return jsonify({'error': 'No data'}), 400
        
        game_code = data.get('game_code', '').upper().strip()
        revolutions = data.get('revolutions', 0)
        device_id = data.get('device_id', 'unknown')
        difficulty = data.get('difficulty', 1)
        request_type = data.get('request_type', 'shot')
        
        if not game_code:
            return jsonify({'error': 'Game code required'}), 400
        
        # If informational request (revolutions == 0)
        if request_type == 'info' or revolutions == 0:
            return handle_info_request(game_code, difficulty)
        else:
            # Shot request
            if not isinstance(revolutions, int) or revolutions < 0:
                return jsonify({'error': 'Invalid revolutions count'}), 400
            
            return handle_shot_request(game_code, revolutions, device_id, difficulty)
            
    except Exception as e:
        print(f"❌ Critical error in handle_request: {e}")
        return jsonify({'error': f'Server error: {str(e)}'}), 500

def handle_info_request(game_code, difficulty):
    """Handle informational request"""
    conn = sqlite3.connect('golf_league.db', check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        # Check if game exists
        cursor.execute('''
            SELECT game_code, player_name, difficulty, current_hole, 
                   remaining, status, total_shots, shots_on_current_hole
            FROM games 
            WHERE game_code = ?
        ''', (game_code,))
        
        game = cursor.fetchone()
        
        if not game:
            return jsonify({'error': 'Game not found'}), 404
        
        (game_code_db, player_name, current_difficulty, current_hole, 
         remaining, status, total_shots, shots_on_hole) = game
        
        # If game in pending and new difficulty provided - update
        if status == 'pending' and 1 <= difficulty <= 4:
            cursor.execute('''
                UPDATE games SET difficulty = ? WHERE game_code = ?
            ''', (difficulty, game_code))
            current_difficulty = difficulty
            conn.commit()
        
        # Check if game expired
        cursor.execute('SELECT expires_at FROM games WHERE game_code = ?', (game_code,))
        expires_at = cursor.fetchone()[0]
        if expires_at:
            expires_datetime = parse_datetime(expires_at)
            if expires_datetime and expires_datetime < datetime.now():
                cursor.execute('UPDATE games SET status = "expired" WHERE game_code = ?', (game_code,))
                conn.commit()
                return jsonify({'error': 'Game expired'}), 410
        
        # Get target for current hole
        if current_hole <= len(GOLF_HOLES):
            target = GOLF_HOLES[current_hole - 1]
        else:
            target = 0
        
        # If game in pending status or remaining is 0, set remaining = target
        if status == 'pending' or remaining == 0:
            cursor.execute('''
                UPDATE games 
                SET status = 'active', started_at = ?, remaining = ?
                WHERE game_code = ?
            ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), target, game_code))
            remaining = target
            status = 'active'
            conn.commit()
        
        # Get difficulty parameters
        tolerance = DIFFICULTY_LEVELS.get(current_difficulty, DIFFICULTY_LEVELS[1])["tolerance"]
        difficulty_name = DIFFICULTY_LEVELS.get(current_difficulty, DIFFICULTY_LEVELS[1])["name"]
        
        return jsonify({
            'success': True,
            'is_informational': True,
            'status': 'info',
            'game_code': game_code_db,
            'player_name': player_name,
            'difficulty': current_difficulty,
            'difficulty_name': difficulty_name,
            'tolerance': tolerance,
            'current_hole': current_hole,
            'hole_distance': target,
            'remaining': remaining,
            'total_shots': total_shots,
            'shots_on_hole': shots_on_hole,
            'status': status,
            'message': f'Hole {current_hole}: target {target} revolutions. Hole width: ±{tolerance}'
        })
        
    except Exception as e:
        print(f"❌ Error in handle_info_request: {e}")
        return jsonify({'error': f'Server error: {str(e)}'}), 500
    finally:
        conn.close()

def handle_shot_request(game_code, revolutions, device_id, difficulty):
    """Handle shot request"""
    conn = sqlite3.connect('golf_league.db', check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        # Get game data with row lock
        cursor.execute('''
            SELECT difficulty, current_hole, remaining, status, 
                   telegram_id, total_shots, shots_on_current_hole
            FROM games 
            WHERE game_code = ?
        ''', (game_code,))
        
        game = cursor.fetchone()
        
        if not game:
            return jsonify({'error': 'Game not found'}), 404
        
        (current_difficulty, current_hole, remaining, status, 
         telegram_id, total_shots, shots_on_hole) = game
        
        # Check game status
        if status != 'active':
            return jsonify({'error': f'Game not active (status: {status})'}), 400
        
        # Check if hole exists
        if current_hole > len(GOLF_HOLES):
            return jsonify({'error': 'Game already completed'}), 400
        
        # Get current hole target and tolerance
        target = GOLF_HOLES[current_hole - 1] if current_hole <= len(GOLF_HOLES) else 100
        tolerance = DIFFICULTY_LEVELS.get(current_difficulty, DIFFICULTY_LEVELS[1])["tolerance"]
        
        # Increase shot counters
        total_shots += 1
        shots_on_hole += 1
        
        # Save remaining before shot
        remaining_before = remaining
        
        # Calculate new remaining
        new_remaining = remaining_before - revolutions
        if new_remaining < 0:
            new_remaining = 0
        
        # Save shot to database
        cursor.execute('''
            INSERT INTO shots (game_code, device_id, hole_number, revolutions, 
                              remaining_before, remaining_after, is_success)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (game_code, device_id, current_hole, revolutions, 
              remaining_before, new_remaining, new_remaining <= tolerance))
        
        # Update shot counters
        cursor.execute('''
            UPDATE games 
            SET total_shots = ?, shots_on_current_hole = ?
            WHERE game_code = ?
        ''', (total_shots, shots_on_hole, game_code))
        
        # Check success: if remaining ≤ tolerance, hole completed
        if new_remaining <= tolerance:
            # Hole completed!
            next_hole = current_hole + 1
            
            if next_hole <= len(GOLF_HOLES):
                # Move to next hole
                next_target = GOLF_HOLES[next_hole - 1]
                
                cursor.execute('''
                    UPDATE games 
                    SET current_hole = ?, remaining = ?, 
                        shots_on_current_hole = 0, accumulated_revolutions = 0
                    WHERE game_code = ?
                ''', (next_hole, next_target, game_code))
                
                # Send Telegram notification
                send_telegram_update(game_code, 'hole_completed', {
                    'hole_number': current_hole,
                    'shots_on_hole': shots_on_hole,
                    'next_hole_distance': next_target
                })
                
                response = {
                    'status': 'hole_completed',
                    'message': f'🎉 Hole {current_hole} completed in {shots_on_hole} shots!',
                    'current_hole': current_hole,
                    'next_hole': next_hole,
                    'next_hole_distance': next_target,
                    'remaining': next_target,
                    'total_shots': total_shots,
                    'tolerance': tolerance,
                    'is_success': True
                }
            else:
                # Game completed
                cursor.execute('''
                    UPDATE games 
                    SET status = 'completed', completed_at = ?, remaining = 0
                    WHERE game_code = ?
                ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), game_code))
                
                # Update player stats
                cursor.execute('SELECT username, first_name FROM player_stats WHERE telegram_id = ?', (telegram_id,))
                player = cursor.fetchone()
                username = player[0] if player else None
                first_name = player[1] if player else "Player"
                
                update_player_stats(telegram_id, username, first_name, game_code)
                
                # Add to leaderboard
                try:
                    cursor.execute('''
                        INSERT INTO leaderboard (telegram_id, game_code, total_score, difficulty)
                        VALUES (?, ?, ?, ?)
                    ''', (telegram_id, game_code, total_shots, current_difficulty))
                except Exception as e:
                    print(f"⚠️ Leaderboard addition error: {e}")
                
                # Send Telegram notification
                send_telegram_update(game_code, 'game_completed', {
                    'final_score': total_shots
                })
                
                response = {
                    'status': 'game_completed',
                    'message': '🏆 Game completed! Great game!',
                    'total_holes': len(GOLF_HOLES),
                    'total_shots': total_shots,
                    'final_score': total_shots,
                    'is_success': True
                }
        else:
            # Continue current hole
            cursor.execute('''
                UPDATE games 
                SET remaining = ?, accumulated_revolutions = accumulated_revolutions + ?
                WHERE game_code = ?
            ''', (new_remaining, revolutions, game_code))
            
            response = {
                'status': 'continue',
                'message': f'📊 Remaining: {new_remaining} of {target} (±{tolerance})',
                'current_hole': current_hole,
                'remaining': new_remaining,
                'total_shots': total_shots,
                'tolerance': tolerance,
                'is_success': False
            }
        
        conn.commit()
        return jsonify(response)
        
    except sqlite3.OperationalError as e:
        if "database is locked" in str(e):
            print("⚠️ Database locked, retrying...")
            time.sleep(0.5)
            conn.close()
            time.sleep(0.5)
            return handle_shot_request(game_code, revolutions, device_id, difficulty)
        else:
            raise e
            
    except Exception as e:
        print(f"❌ Critical error in handle_shot_request: {e}")
        return jsonify({'error': f'Server error: {str(e)}'}), 500
    finally:
        try:
            conn.close()
        except:
            pass

@app.route('/game/<game_code>')
def game_tracker(game_code):
    """Web page for real-time game tracking"""
    conn = sqlite3.connect('golf_league.db')
    cursor = conn.cursor()
    
    # Get game information
    cursor.execute('''
        SELECT game_code, player_name, current_hole, remaining, 
               total_shots, status, created_at, difficulty
        FROM games 
        WHERE game_code = ? AND status != 'expired'
    ''', (game_code.upper(),))
    
    game = cursor.fetchone()
    
    if not game:
        error_html = '''
        <!DOCTYPE html>
        <html>
        <head>
            <title>❌ Game not found</title>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                body { font-family: Arial, sans-serif; text-align: center; padding: 50px; }
                .container { max-width: 500px; margin: 0 auto; }
                .error { background: #ffebee; padding: 20px; border-radius: 10px; }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>❌ Game not found</h1>
                <div class="error">
                    <p>Game with code <strong>''' + game_code + '''</strong> doesn't exist or was deleted.</p>
                    <p><a href="/">Return to home</a></p>
                </div>
            </div>
        </body>
        </html>
        '''
        return error_html, 404
    
    # Unpack data
    game_code_db, player_name, current_hole, remaining, total_shots, status, created_at, difficulty = game
    
    # Get current hole target
    if current_hole <= len(GOLF_HOLES):
        target = GOLF_HOLES[current_hole - 1]
        progress_percent = min(100, int((target - remaining) / target * 100)) if target > 0 else 100
    else:
        target = 0
        progress_percent = 100
    
    # Get difficulty name
    if difficulty in DIFFICULTY_LEVELS:
        difficulty_name = DIFFICULTY_LEVELS[difficulty]["name"]
        tolerance = DIFFICULTY_LEVELS[difficulty]["tolerance"]
    else:
        difficulty_name = "Unknown"
        tolerance = 5
    
    # Get last 10 shots
    cursor.execute('''
        SELECT hole_number, revolutions, timestamp
        FROM shots 
        WHERE game_code = ?
        ORDER BY timestamp DESC
        LIMIT 10
    ''', (game_code.upper(),))
    
    shots = cursor.fetchall()
    
    conn.close()
    
    # HTML template with auto-refresh
    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>🎮 Spinner Golf - {game_code_db}</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <!-- Auto-refresh every 10 seconds -->
        <meta http-equiv="refresh" content="10">
        <style>
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}
            
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
                color: #333;
            }}
            
            .container {{
                max-width: 800px;
                margin: 0 auto;
            }}
            
            .header {{
                background: white;
                padding: 30px;
                border-radius: 15px;
                margin-bottom: 20px;
                box-shadow: 0 10px 30px rgba(0,0,0,0.1);
                text-align: center;
            }}
            
            .game-code {{
                font-size: 2.5em;
                font-weight: bold;
                color: #667eea;
                margin-bottom: 10px;
                font-family: monospace;
                letter-spacing: 3px;
            }}
            
            .player-info {{
                color: #666;
                font-size: 1.1em;
                margin-bottom: 5px;
            }}
            
            .stats-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
                gap: 15px;
                margin: 30px 0;
            }}
            
            .stat-card {{
                background: white;
                padding: 20px;
                border-radius: 10px;
                text-align: center;
                box-shadow: 0 5px 15px rgba(0,0,0,0.05);
                transition: transform 0.3s ease;
            }}
            
            .stat-card:hover {{
                transform: translateY(-5px);
            }}
            
            .stat-label {{
                font-size: 0.9em;
                color: #888;
                margin-bottom: 8px;
                text-transform: uppercase;
                letter-spacing: 1px;
            }}
            
            .stat-value {{
                font-size: 2.2em;
                font-weight: bold;
                color: #667eea;
            }}
            
            .progress-section {{
                background: white;
                padding: 25px;
                border-radius: 15px;
                margin: 20px 0;
                box-shadow: 0 5px 15px rgba(0,0,0,0.05);
            }}
            
            .progress-title {{
                display: flex;
                justify-content: space-between;
                margin-bottom: 15px;
                font-size: 1.1em;
                color: #555;
            }}
            
            .progress-bar {{
                height: 25px;
                background: #f0f0f0;
                border-radius: 12px;
                overflow: hidden;
            }}
            
            .progress-fill {{
                height: 100%;
                background: linear-gradient(90deg, #4cd964, #5ac8fa);
                width: {progress_percent}%;
                transition: width 1s ease;
                border-radius: 12px;
            }}
            
            .shots-history {{
                background: white;
                padding: 25px;
                border-radius: 15px;
                margin-top: 20px;
                box-shadow: 0 5px 15px rgba(0,0,0,0.05);
            }}
            
            .shots-title {{
                font-size: 1.2em;
                color: #555;
                margin-bottom: 20px;
                padding-bottom: 10px;
                border-bottom: 2px solid #f0f0f0;
            }}
            
            .shot-row {{
                display: grid;
                grid-template-columns: 1fr 1fr 2fr;
                gap: 15px;
                padding: 15px;
                border-bottom: 1px solid #f0f0f0;
            }}
            
            .shot-row:nth-child(even) {{
                background: #f9f9f9;
            }}
            
            .shot-row:last-child {{
                border-bottom: none;
            }}
            
            .shot-value {{
                font-weight: bold;
                color: #667eea;
            }}
            
            .shot-time {{
                color: #888;
                font-size: 0.9em;
            }}
            
            .status-badge {{
                display: inline-block;
                padding: 5px 15px;
                border-radius: 20px;
                font-size: 0.9em;
                font-weight: bold;
                margin-top: 10px;
            }}
            
            .status-active {{
                background: #e8f5e8;
                color: #4CAF50;
            }}
            
            .status-completed {{
                background: #e3f2fd;
                color: #2196F3;
            }}
            
            .footer {{
                text-align: center;
                margin-top: 30px;
                color: white;
                font-size: 0.9em;
            }}
            
            .update-info {{
                background: rgba(255,255,255,0.1);
                padding: 10px;
                border-radius: 10px;
                display: inline-block;
                margin-top: 10px;
            }}
            
            .hole-info {{
                font-size: 1.5em;
                color: #333;
                margin: 10px 0;
            }}
            
            @media (max-width: 600px) {{
                .stats-grid {{
                    grid-template-columns: 1fr;
                }}
                
                .shot-row {{
                    grid-template-columns: 1fr;
                    gap: 5px;
                }}
                
                .header {{
                    padding: 20px;
                }}
                
                .game-code {{
                    font-size: 2em;
                }}
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🎮 Spinner Golf</h1>
                <div class="game-code">{game_code_db}</div>
                <div class="player-info">Player: {player_name}</div>
                <div class="player-info">Difficulty: {difficulty_name} (±{tolerance})</div>
                <div class="player-info">Created: {created_at}</div>
                
                <div class="status-badge status-{status}">
                    {{
                        '🎯 Active' if status == 'active' else
                        '🏆 Completed' if status == 'completed' else
                        '⏳ Pending' if status == 'pending' else status
                    }}
                </div>
            </div>
            
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-label">Current Hole</div>
                    <div class="stat-value">{current_hole}/18</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-label">Remaining Revolutions</div>
                    <div class="stat-value">{remaining}</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-label">Total Shots</div>
                    <div class="stat-value">{total_shots}</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-label">Hole Target</div>
                    <div class="stat-value">{target}</div>
                </div>
            </div>
            
            {f'''
            <div class="progress-section">
                <div class="progress-title">
                    <span>Hole {current_hole} Progress</span>
                    <span>{progress_percent}%</span>
                </div>
                <div class="progress-bar">
                    <div class="progress-fill"></div>
                </div>
                <div class="hole-info">
                    🎯 Remaining: {remaining} of {target} revolutions
                </div>
            </div>
            ''' if status == 'active' and current_hole <= 18 else ''}
            
            <div class="shots-history">
                <div class="shots-title">📈 Last Shots</div>
                {f'''
                <div class="shots-list">
                    {' '.join([
                        f'''
                        <div class="shot-row">
                            <div><span class="shot-label">🕳️ Hole:</span> <span class="shot-value">{hole}</span></div>
                            <div><span class="shot-label">🌀 Revolutions:</span> <span class="shot-value">{rev}</span></div>
                            <div><span class="shot-label">🕐 Time:</span> <span class="shot-time">{time}</span></div>
                        </div>
                        ''' for hole, rev, time in shots
                    ]) if shots else '<p style="text-align: center; color: #888; padding: 20px;">No shots yet</p>'}
                </div>
                '''}
            </div>
            
            <div class="footer">
                <div class="update-info">
                    🔄 Page refreshes every 10 seconds<br>
                    Last update: {datetime.now().strftime("%H:%M:%S")}
                </div>
                <p style="margin-top: 15px;">
                    <a href="/" style="color: white; text-decoration: underline;">Home page</a> | 
                    <a href="https://t.me/spinner_golf_bot" style="color: white; text-decoration: underline;">Telegram bot</a>
                </p>
            </div>
        </div>
        
        <script>
            document.addEventListener('DOMContentLoaded', function() {{
                const elements = document.querySelectorAll('.stat-card, .progress-section, .shots-history');
                elements.forEach(el => {{
                    el.style.opacity = '0';
                    el.style.transform = 'translateY(20px)';
                }});
                
                setTimeout(() => {{
                    elements.forEach((el, i) => {{
                        setTimeout(() => {{
                            el.style.transition = 'opacity 0.5s ease, transform 0.5s ease';
                            el.style.opacity = '1';
                            el.style.transform = 'translateY(0)';
                        }}, i * 100);
                    }});
                }}, 300);
            }});
        </script>
    </body>
    </html>
    '''
    
    return html

# ==================== TELEGRAM BOT ====================
def setup_telegram_bot():
    """Setup and run Telegram bot"""
    if not bot:
        print("⚠️ Telegram bot not started: token missing")
        return
    
    @bot.message_handler(commands=['start', 'help'])
    def send_welcome(message):
        user = message.from_user
        welcome_text = f"""
🎮 Welcome to <b>Spinner Golf v3.4</b>, {user.first_name}!

<b>Simplified game mechanics:</b>
• Each hole has distance (e.g.: 100 revolutions)
• Spin the spinner to reduce distance to hole
• Hole is considered completed when remaining ≤ tolerance
• Tolerance depends on selected difficulty level

🎯 <b>Difficulty levels:</b>
• 🥳 <b>Novice</b>: hole width ±10 revolutions
• 😊 <b>Amateur</b>: hole width ±7 revolutions  
• 🤔 <b>Pro</b>: hole width ±5 revolutions
• 😎 <b>Master</b>: hole width ±3 revolutions

📋 <b>Commands:</b>
/play - 🎯 Start new game
/stats - 📊 My statistics
/leaderboard - 🏆 Top players
/link - 🔗 Get tracking link

Good luck! ⛳
        """
        bot.reply_to(message, welcome_text, parse_mode='HTML')
    
    @bot.message_handler(commands=['play'])
    def create_game(message):
        user = message.from_user
        
        # Generate game code
        game_code = generate_game_code()
        
        # Set expiration time
        expires_at = (datetime.now() + timedelta(hours=GAME_EXPIRE_HOURS)).strftime('%Y-%m-%d %H:%M:%S')
        
        # Set initial remaining for first hole
        first_hole_distance = GOLF_HOLES[0]
        
        conn = sqlite3.connect('golf_league.db', check_same_thread=False)
        cursor = conn.cursor()
        
        # Create new game
        cursor.execute('''
            INSERT INTO games (game_code, telegram_id, player_name, difficulty, expires_at, remaining)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (game_code, user.id, user.first_name, 1, expires_at, first_hole_distance))
        
        conn.commit()
        conn.close()
        
        # Form tracking URL
        game_url = f"{SERVER_URL}/game/{game_code}"
        
        # Send instruction message
        instructions = f"""
✅ <b>Game created!</b>

🎮 <b>Game code:</b> <code>{game_code}</code>

📱 <b>How to connect ESP32:</b>
1. Switch ESP32 to configuration mode
2. Connect to Wi-Fi network <code>SpinnerGolf-Config</code>
3. Open in browser <code>192.168.4.1</code>
4. Select difficulty level
5. Enter this code in "Game Code" field
6. Save settings and switch ESP32 to game mode
7. Press button to start game

🎯 <b>Difficulty levels in ESP32 settings:</b>
• 🥳 <b>Novice</b>: hole width ±10 revolutions
• 😊 <b>Amateur</b>: hole width ±7 revolutions
• 🤔 <b>Pro</b>: hole width ±5 revolutions
• 😎 <b>Master</b>: hole width ±3 revolutions

🌐 <b>Track game online:</b>
{game_url}

📱 <b>Share this link</b> with friends so they can follow your progress!

⏰ <b>Code valid for:</b> {GAME_EXPIRE_HOURS} hours
🏌️ <b>Number of holes:</b> {len(GOLF_HOLES)}
⛳ <b>First hole:</b> {first_hole_distance} revolutions

<b>Game mechanics:</b>
• Hole 1: {first_hole_distance} revolutions
• Spin the spinner to reduce distance to hole
• Hole completed when remaining ≤ selected tolerance
• Goal: complete all 18 holes in minimum number of shots!

Good luck! 🚀
        """
        
        bot.reply_to(message, instructions, parse_mode='HTML')
    
    @bot.message_handler(commands=['link'])
    def send_game_link(message):
        """Send tracking link for current game"""
        user = message.from_user
        
        conn = sqlite3.connect('golf_league.db', check_same_thread=False)
        cursor = conn.cursor()
        
        # Find user's active game
        cursor.execute('''
            SELECT game_code, status 
            FROM games 
            WHERE telegram_id = ? AND status IN ('active', 'pending')
            ORDER BY created_at DESC 
            LIMIT 1
        ''', (user.id,))
        
        game = cursor.fetchone()
        
        if game:
            game_code, status = game
            game_url = f"{SERVER_URL}/game/{game_code}"
            
            if status == 'active':
                status_text = "active"
            else:
                status_text = "pending start"
            
            response = f"""
🔗 <b>Game tracking link:</b>

{game_url}

🎮 <b>Game code:</b> <code>{game_code}</code>
📊 <b>Status:</b> {status_text}

📱 <b>Share this link</b> with friends so they can track your progress in real time!

<i>Page updates automatically every 10 seconds.</i>
"""
        else:
            response = "You don't have an active game. Start new game with /play"
        
        conn.close()
        bot.reply_to(message, response, parse_mode='HTML')
    
    @bot.message_handler(commands=['stats'])
    def show_stats(message):
        user = message.from_user
        
        conn = sqlite3.connect('golf_league.db', check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT total_games, completed_games, total_shots, best_score, last_played
            FROM player_stats 
            WHERE telegram_id = ?
        ''', (user.id,))
        
        stats = cursor.fetchone()
        
        if stats:
            total_games, completed_games, total_shots, best_score, last_played = stats
            
            cursor.execute('SELECT COUNT(*) FROM leaderboard WHERE telegram_id = ?', (user.id,))
            leaderboard_entries = cursor.fetchone()[0] or 0
            
            cursor.execute('SELECT AVG(total_score) FROM leaderboard WHERE telegram_id = ?', (user.id,))
            avg_score_result = cursor.fetchone()[0]
            avg_score = f"{avg_score_result:.1f}" if avg_score_result else "No data"
            
            if last_played:
                last_played_date = parse_datetime(last_played)
                if last_played_date:
                    last_played_str = last_played_date.strftime("%d.%m.%Y %H:%M")
                else:
                    last_played_str = "Unknown"
            else:
                last_played_str = "Haven't played yet"
            
            stats_text = f"""
📊 <b>Statistics</b>

👤 <b>Player:</b> {user.first_name}
🎮 <b>Total games:</b> {total_games}
✅ <b>Completed games:</b> {completed_games}
🏌️ <b>Total shots:</b> {total_shots}
🎯 <b>Best score:</b> {best_score if best_score != 999 else "None"}
📈 <b>Average score:</b> {avg_score}
🏆 <b>In top:</b> {leaderboard_entries} times
📅 <b>Last game:</b> {last_played_str}
            """
        else:
            stats_text = """
📊 <b>Statistics</b>

You don't have statistics yet! 🎮

Start game with /play
            """
        
        conn.close()
        bot.reply_to(message, stats_text, parse_mode='HTML')
    
    @bot.message_handler(commands=['leaderboard'])
    def show_leaderboard(message):
        conn = sqlite3.connect('golf_league.db', check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT p.first_name, MIN(l.total_score) as best_score, COUNT(l.record_id) as games_played
            FROM leaderboard l
            JOIN player_stats p ON l.telegram_id = p.telegram_id
            GROUP BY l.telegram_id
            ORDER BY best_score ASC
            LIMIT 10
        ''')
        
        leaders = cursor.fetchall()
        
        if leaders:
            leaderboard_text = "<b>🏆 TOP-10 PLAYERS</b>\n\n"
            
            for i, (first_name, best_score, games_played) in enumerate(leaders, 1):
                medal = ""
                if i == 1: medal = "🥇 "
                elif i == 2: medal = "🥈 "
                elif i == 3: medal = "🥉 "
                
                leaderboard_text += f"{medal}{i}. {first_name}: {best_score} ({games_played} games)\n"
            
            leaderboard_text += f"\nTotal players in ranking: {len(leaders)}"
        else:
            leaderboard_text = """
🏆 <b>TOP PLAYERS</b>

Ranking is empty! 🎮

Be first to get on the leaderboard!
            """
        
        conn.close()
        bot.reply_to(message, leaderboard_text, parse_mode='HTML')
    
    # Start bot
    print("🤖 Telegram bot started")
    bot.polling(none_stop=True)

# ==================== SERVER START ====================
def start_telegram_bot():
    """Start Telegram bot in separate thread"""
    if TELEGRAM_TOKEN:
        try:
            setup_telegram_bot()
        except Exception as e:
            print(f"❌ Telegram bot startup error: {e}")
    else:
        print("⚠️ Telegram bot not started: token not specified")

if __name__ == '__main__':
    init_database()
    
    # Start Telegram bot
    if TELEGRAM_TOKEN:
        bot_thread = threading.Thread(target=start_telegram_bot, daemon=True)
        bot_thread.start()
        print("🤖 Telegram bot started in separate thread")
    
    # Start Flask server
    port = int(os.environ.get('PORT', 10000))
    print(f"🚀 Flask server started on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
   


