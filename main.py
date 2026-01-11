import os
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify

# Загружаем .env (если используется)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # На Render .env можно задать вручную

app = Flask(__name__)

def init_database():
    """Инициализация базы данных SQLite"""
    conn = sqlite3.connect('golf_league.db')
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS devices (
            device_id TEXT PRIMARY KEY,
            owner_id INTEGER,
            device_name TEXT,
            registration_date TIMESTAMP,
            last_seen TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS players (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            registration_date TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS games (
            game_id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_code TEXT UNIQUE,
            device_id TEXT,
            player_id INTEGER,
            difficulty INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            total_strokes INTEGER DEFAULT 0,
            FOREIGN KEY (device_id) REFERENCES devices(device_id),
            FOREIGN KEY (player_id) REFERENCES players(telegram_id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS game_results (
            result_id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER,
            hole_number INTEGER,
            strokes INTEGER,
            revolutions INTEGER,
            completed BOOLEAN,
            timestamp TIMESTAMP,
            FOREIGN KEY (game_id) REFERENCES games(game_id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS leaderboard (
            leaderboard_id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE,
            difficulty INTEGER,
            player_id INTEGER,
            total_strokes INTEGER,
            rank INTEGER,
            FOREIGN KEY (player_id) REFERENCES players(telegram_id)
        )
    ''')

    conn.commit()
    conn.close()
    print("✅ База данных инициализирована")

GOLF_COURSES = {
    "standard_18": {
        "holes": [140, 180, 100, 200, 135, 100, 170, 210, 100, 
                  150, 180, 120, 200, 135, 120, 170, 200, 110],
        "par": [3, 4, 3, 4, 3, 3, 4, 5, 3, 4, 4, 3, 4, 3, 3, 4, 4, 3]
    }
}

def calculate_tolerance(difficulty):
    tolerances = {1: 10, 2: 7, 3: 5, 4: 3, 5: 2}
    return tolerances.get(difficulty, 5)

# --- API маршруты (без изменений) ---
@app.route('/api/ping', methods=['POST'])
def api_ping():
    try:
        data = request.json
        device_id = data.get('device_id')
        if not device_id:
            return jsonify({'error': 'No device_id'}), 400
        conn = sqlite3.connect('golf_league.db')
        cursor = conn.cursor()
        cursor.execute('UPDATE devices SET last_seen = datetime("now") WHERE device_id = ?', (device_id,))
        conn.commit()
        conn.close()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/get_game', methods=['GET'])
def api_get_game():
    try:
        game_code = request.args.get('code')
        if not game_code:
            return jsonify({'error': 'No game code'}), 400
        conn = sqlite3.connect('golf_league.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT g.game_id, g.difficulty, d.device_id, p.telegram_id
            FROM games g
            JOIN devices d ON g.device_id = d.device_id
            JOIN players p ON g.player_id = p.telegram_id
            WHERE g.game_code = ? AND g.status = 'pending'
        ''', (game_code,))
        game = cursor.fetchone()
        if not game:
            return jsonify({'error': 'Game not found'}), 404
        game_id, difficulty, device_id, player_id = game
        cursor.execute('UPDATE games SET status = "started", started_at = datetime("now") WHERE game_id = ?', (game_id,))
        conn.commit()
        holes = GOLF_COURSES["standard_18"]["holes"]
        tolerance = calculate_tolerance(difficulty)
        response = {
            'success': True,
            'game_id': game_id,
            'device_id': device_id,
            'player_id': player_id,
            'difficulty': difficulty,
            'tolerance': tolerance,
            'holes': holes,
            'total_holes': len(holes),
            'current_hole': 1,
            'par': GOLF_COURSES["standard_18"]["par"][0],
            'target': holes[0]
        }
        conn.close()
        return jsonify(response)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/submit_shot', methods=['POST'])
def api_submit_shot():
    try:
        data = request.json
        game_id = data.get('game_id')
        hole = data.get('hole')
        revolutions = data.get('revolutions')
        if not all([game_id, hole, revolutions]):
            return jsonify({'error': 'Missing parameters'}), 400
        conn = sqlite3.connect('golf_league.db')
        cursor = conn.cursor()
        cursor.execute('SELECT difficulty, total_strokes, status FROM games WHERE game_id = ?', (game_id,))
        game_info = cursor.fetchone()
        if not game_info:
            return jsonify({'error': 'Game not found'}), 404
        difficulty, total_strokes, status = game_info
        if status != 'started':
            return jsonify({'error': 'Game not active'}), 400
        target = GOLF_COURSES["standard_18"]["holes"][hole - 1]
        tolerance = calculate_tolerance(difficulty)
        par = GOLF_COURSES["standard_18"]["par"][hole - 1]
        difference = revolutions - target
        abs_difference = abs(difference)
        new_total_strokes = (total_strokes or 0) + 1
        cursor.execute('''
            INSERT INTO game_results (game_id, hole_number, strokes, revolutions, completed, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (game_id, hole, new_total_strokes, revolutions, False, datetime.now()))
        if abs_difference <= tolerance:
            cursor.execute('UPDATE games SET total_strokes = ? WHERE game_id = ?', (new_total_strokes, game_id))
            if hole < len(GOLF_COURSES["standard_18"]["holes"]):
                next_hole = hole + 1
                next_target = GOLF_COURSES["standard_18"]["holes"][next_hole - 1]
                next_par = GOLF_COURSES["standard_18"]["par"][next_hole - 1]
                response = {
                    'status': 'hole_completed',
                    'message': f'Лунка {hole} завершена!',
                    'next_hole': next_hole,
                    'next_target': next_target,
                    'next_par': next_par,
                    'total_holes': len(GOLF_COURSES["standard_18"]["holes"]),
                    'strokes_on_hole': new_total_strokes - (total_strokes or 0)
                }
            else:
                cursor.execute('''
                    UPDATE games SET status = "completed", completed_at = datetime("now"), total_strokes = ?
                    WHERE game_id = ?
                ''', (new_total_strokes, game_id))
                cursor.execute('''
                    INSERT INTO leaderboard (date, difficulty, player_id, total_strokes)
                    SELECT date("now"), difficulty, player_id, total_strokes FROM games WHERE game_id = ?
                ''', (game_id,))
                response = {
                    'status': 'game_completed',
                    'message': f'Игра завершена! Всего ударов: {new_total_strokes}',
                    'total_strokes': new_total_strokes,
                    'total_par': sum(GOLF_COURSES["standard_18"]["par"]),
                    'score_diff': new_total_strokes - sum(GOLF_COURSES["standard_18"]["par"])
                }
            cursor.execute('UPDATE game_results SET completed = TRUE WHERE game_id = ? AND hole_number = ?', (game_id, hole))
        else:
            response = {
                'status': 'continue',
                'message': f'Не попали! Разница: {abs_difference:.1f} оборотов',
                'needed': -difference,
                'strokes_on_hole': new_total_strokes - (total_strokes or 0),
                'current_target': target,
                'tolerance': tolerance,
                'par': par
            }
        conn.commit()
        conn.close()
        return jsonify(response)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/get_leaderboard', methods=['GET'])
def api_get_leaderboard():
    try:
        difficulty = request.args.get('difficulty', 1, type=int)
        limit = request.args.get('limit', 10, type=int)
        conn = sqlite3.connect('golf_league.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT p.username, p.full_name, MIN(l.total_strokes) as best_score
            FROM leaderboard l
            JOIN players p ON l.player_id = p.telegram_id
            WHERE l.difficulty = ? AND l.date >= date("now", "-7 days")
            GROUP BY l.player_id
            ORDER BY best_score
            LIMIT ?
        ''', (difficulty, limit))
        results = cursor.fetchall()
        conn.close()
        leaderboard = []
        for i, (username, full_name, score) in enumerate(results, 1):
            leaderboard.append({
                'rank': i,
                'player': username or full_name or f'Игрок {i}',
                'score': score
            })
        return jsonify({'difficulty': difficulty, 'leaderboard': leaderboard})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/')
def index():
    return '''
    <!DOCTYPE html>
    <html>
    <head><title>Spinner Golf Server</title><meta charset="UTF-8">
    <style>body{font-family:Arial,sans-serif;margin:40px}.container{max-width:800px;margin:0 auto}.status{background:#f0f0f0;padding:20px;border-radius:10px}h1{color:#2c3e50}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;margin:20px 0}.stat-box{background:white;padding:15px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1)}</style>
    </head>
    <body>
        <div class="container">
            <h1>🏌️♂️ Spinner Golf Server</h1>
            <div class="status"><h2>Статус сервера: 🟢 Работает</h2></div>
            <div class="stats">
                <div class="stat-box"><h3>👥 Игроки</h3><p id="players_count">Загрузка...</p></div>
                <div class="stat-box"><h3>📱 Устройства</h3><p id="devices_count">Загрузка...</p></div>
                <div class="stat-box"><h3>🎮 Игры</h3><p id="games_count">Загрузка...</p></div>
            </div>
        </div>
        <script>
            async function loadStats() {
                try {
                    const r = await fetch('/admin/stats');
                    const d = await r.json();
                    document.getElementById('players_count').textContent = d.players;
                    document.getElementById('devices_count').textContent = d.devices;
                    document.getElementById('games_count').textContent = d.games;
                } catch (e) { console.error(e); }
            }
            loadStats(); setInterval(loadStats, 30000);
        </script>
    </body>
    </html>
    '''

@app.route('/admin/stats', methods=['GET'])
def admin_stats():
    conn = sqlite3.connect('golf_league.db')
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM players'); players = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM devices'); devices = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM games WHERE date(created_at) = date("now")'); games = c.fetchone()[0]
    conn.close()
    return jsonify({'players': players, 'devices': devices, 'games': games})

if __name__ == '__main__':
    init_database()
    port = int(os.environ.get('PORT', 10000))  # Render использует PORT
    print(f"🚀 Запуск сервера на порту {port}...")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
