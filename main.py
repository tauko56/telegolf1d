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

# ==================== НАСТРОЙКИ ====================
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    print("❌ ОШИБКА: TELEGRAM_TOKEN не установлен!")

# ID администраторов
ADMIN_IDS = []
admin_ids_str = os.environ.get('ADMIN_IDS', '')
if admin_ids_str:
    try:
        ADMIN_IDS = [int(id.strip()) for id in admin_ids_str.split(',')]
    except:
        ADMIN_IDS = []
        print("⚠️ Не удалось распарсить ADMIN_IDS")

# URL сервера
SERVER_URL = os.environ.get('SERVER_URL', 'https://telegolf1d.onrender.com')

# Инициализация бота
bot = telebot.TeleBot(TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None

# ==================== ИГРОВЫЕ ПАРАМЕТРЫ ====================
# Дистанции до лунок
GOLF_HOLES = [100, 150, 200, 120, 180, 90, 160, 210, 130, 
              140, 170, 110, 190, 125, 140, 160, 195, 105]

# Уровни сложности (толеранс)
DIFFICULTY_LEVELS = {
    1: {"name": "Новичок", "tolerance": 10},
    2: {"name": "Любитель", "tolerance": 7},
    3: {"name": "Профи", "tolerance": 5},
    4: {"name": "Мастер", "tolerance": 3}
}

# Время жизни игры
GAME_EXPIRE_HOURS = 24

# ==================== БАЗА ДАННЫХ ====================
def init_database():
    conn = sqlite3.connect('golf_league.db', check_same_thread=False)
    cursor = conn.cursor()
    
    # Таблица игр
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
    
    # Таблица бросков
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
    
    # Таблица статистики
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
    
    # Таблица лидерборда
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
    
    # Индексы
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_games_code ON games(game_code)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_games_telegram ON games(telegram_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_shots_code ON shots(game_code)')
    
    conn.commit()
    conn.close()
    print("✅ База данных инициализирована")

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def generate_game_code():
    """Генерация уникального 6-значного кода игры"""
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
    """Универсальный парсер даты"""
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
                print(f"⚠️ Не удалось распарсить дату: {dt_str}")
                return None

def update_player_stats(telegram_id, username, first_name, game_code):
    """Обновление статистики игрока"""
    conn = sqlite3.connect('golf_league.db', check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        # Получаем количество бросков в игре
        cursor.execute('SELECT COUNT(*) FROM shots WHERE game_code = ?', (game_code,))
        total_shots = cursor.fetchone()[0] or 0
        
        # Обновляем статистику игрока
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
        print(f"❌ Ошибка обновления статистики: {e}")
    finally:
        conn.close()

def send_telegram_update(game_code, message_type, data):
    """Отправляет уведомления в Telegram о ключевых событиях"""
    if not bot:
        return
    
    conn = sqlite3.connect('golf_league.db', check_same_thread=False)
    cursor = conn.cursor()
    
    # Получаем информацию об игре
    cursor.execute('''
        SELECT telegram_id, player_name, current_hole, total_shots, difficulty
        FROM games WHERE game_code = ?
    ''', (game_code,))
    
    game = cursor.fetchone()
    
    if not game:
        conn.close()
        return
    
    telegram_id, player_name, current_hole, total_shots, difficulty = game
    
    # Формируем сообщение в зависимости от типа события
    if message_type == 'hole_completed':
        tolerance = DIFFICULTY_LEVELS.get(difficulty, DIFFICULTY_LEVELS[1])["tolerance"]
        message = f"""
🎉 <b>Лунка пройдена!</b>

🕳️ <b>Лунка:</b> {data['hole_number']}
🎯 <b>Ширина лунки:</b> ±{tolerance}
🏌️ <b>Ударов на лунке:</b> {data['shots_on_hole']}
🏆 <b>Всего ударов:</b> {total_shots}

📏 <b>Следующая лунка:</b> {data['next_hole_distance']} оборотов

🌐 <b>Следить онлайн:</b>
{SERVER_URL}/game/{game_code}
"""
    elif message_type == 'game_completed':
        message = f"""
🏆 <b>ИГРА ЗАВЕРШЕНА!</b>

🎮 <b>Финальный результат:</b>
🕳️ Пройдено лунок: 18
🏌️ Общее количество ударов: {total_shots}

🎯 <b>Поздравляем, {player_name}!</b>

📊 <b>Посмотреть статистику:</b> /stats
🎮 <b>Начать новую игру:</b> /play

🌐 <b>Итоговая страница:</b>
{SERVER_URL}/game/{game_code}
"""
    else:
        conn.close()
        return
    
    try:
        bot.send_message(telegram_id, message, parse_mode='HTML')
    except Exception as e:
        print(f"⚠️ Ошибка отправки уведомления в Telegram: {e}")
    
    conn.close()

# ==================== API ДЛЯ ESP32 ====================
@app.route('/')
def home():
    """Главная страница сервера"""
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Spinner Golf - Сервер</title>
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
                <h1>🏌️ Spinner Golf - Сервер v3.4</h1>
                <div class="status-badge">
                    🟢 Сервер работает | 
                    <span class="telegram-status">🤖 Telegram бот: ''' + ('Активен' if TELEGRAM_TOKEN else 'Не настроен') + '''</span>
                </div>
            </div>
            
            <div class="card">
                <h3>🎮 Как играть:</h3>
                <div class="feature-grid">
                    <div class="feature">
                        <div class="feature-icon">🤖</div>
                        <h4>1. Найти бота</h4>
                        <p>Найдите в Telegram бота <code>@spinner_golf_bot</code></p>
                    </div>
                    <div class="feature">
                        <div class="feature-icon">🎯</div>
                        <h4>2. Начать игру</h4>
                        <p>Отправьте команду <code>/play</code></p>
                    </div>
                    <div class="feature">
                        <div class="feature-icon">📱</div>
                        <h4>3. Настроить ESP32</h4>
                        <p>Введите код и выберите сложность</p>
                    </div>
                    <div class="feature">
                        <div class="feature-icon">🌐</div>
                        <h4>4. Следить онлайн</h4>
                        <p>Откройте ссылку для отслеживания</p>
                    </div>
                </div>
            </div>
            
            <div class="card">
                <h3>🎯 Уровни сложности:</h3>
                <div class="feature-grid">
                    <div class="feature">
                        <h4>🥳 Новичок</h4>
                        <p>Ширина лунки: ±10 оборотов</p>
                    </div>
                    <div class="feature">
                        <h4>😊 Любитель</h4>
                        <p>Ширина лунки: ±7 оборотов</p>
                    </div>
                    <div class="feature">
                        <h4>🤔 Профи</h4>
                        <p>Ширина лунки: ±5 оборотов</p>
                    </div>
                    <div class="feature">
                        <h4>😎 Мастер</h4>
                        <p>Ширина лунки: ±3 оборота</p>
                    </div>
                </div>
            </div>
            
            <div class="card">
                <h3>📡 API для ESP32:</h3>
                <p><strong>Эндпоинты:</strong></p>
                <div class="api-link">POST /api/handle_request</div>
                <p>Основной эндпоинт для всех запросов</p>
                
                <div class="api-link">GET /api/status</div>
                <p>Проверить статус сервера</p>
            </div>
            
            <div class="card">
                <h3>🔗 Полезные ссылки:</h3>
                <a href="https://t.me/spinner_golf_bot" class="btn" target="_blank">🤖 Telegram бот</a>
                <a href="/api/status" class="btn" target="_blank">📡 Проверить API</a>
                
                <h4 style="margin-top: 20px;">📱 Отслеживание игры:</h4>
                <p>После начала игры вы получите ссылку вида:</p>
                <div class="api-link">''' + SERVER_URL + '''/game/ABC123</div>
                <p>Поделитесь этой ссылкой с друзьями!</p>
            </div>
            
            <div style="text-align: center; margin-top: 40px; padding-top: 20px; border-top: 1px solid #eee;">
                <p>Spinner Golf v3.4 | Упрощенный одномерный гольф с выбором сложности</p>
                <p style="color: #666; font-size: 0.9em;">Сервер автоматически обновляется каждые 10 секунд</p>
            </div>
        </div>
    </body>
    </html>
    '''

@app.route('/api/status')
def api_status():
    """Проверка статуса сервера"""
    return jsonify({
        'status': 'ok',
        'server': 'Spinner Golf v3.4',
        'timestamp': datetime.now().isoformat()
    })

@app.route('/api/handle_request', methods=['POST'])
def handle_request():
    """Основной обработчик всех запросов от ESP32"""
    try:
        init_database()
        
        data = request.json
        if not data:
            return jsonify({'error': 'Нет данных'}), 400
        
        game_code = data.get('game_code', '').upper().strip()
        revolutions = data.get('revolutions', 0)
        device_id = data.get('device_id', 'unknown')
        difficulty = data.get('difficulty', 1)
        request_type = data.get('request_type', 'shot')
        
        if not game_code:
            return jsonify({'error': 'Требуется код игры'}), 400
        
        # Если запрос информационный (revolutions == 0)
        if request_type == 'info' or revolutions == 0:
            return handle_info_request(game_code, difficulty)
        else:
            # Запрос броска
            if not isinstance(revolutions, int) or revolutions < 0:
                return jsonify({'error': 'Некорректное количество оборотов'}), 400
            
            return handle_shot_request(game_code, revolutions, device_id, difficulty)
            
    except Exception as e:
        print(f"❌ Критическая ошибка в handle_request: {e}")
        return jsonify({'error': f'Ошибка сервера: {str(e)}'}), 500

def handle_info_request(game_code, difficulty):
    """Обработка информационного запроса"""
    conn = sqlite3.connect('golf_league.db', check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        # Проверяем существование игры
        cursor.execute('''
            SELECT game_code, player_name, difficulty, current_hole, 
                   remaining, status, total_shots, shots_on_current_hole
            FROM games 
            WHERE game_code = ?
        ''', (game_code,))
        
        game = cursor.fetchone()
        
        if not game:
            return jsonify({'error': 'Игра не найдена'}), 404
        
        (game_code_db, player_name, current_difficulty, current_hole, 
         remaining, status, total_shots, shots_on_hole) = game
        
        # Если игра в pending и передана новая сложность - обновляем
        if status == 'pending' and 1 <= difficulty <= 4:
            cursor.execute('''
                UPDATE games SET difficulty = ? WHERE game_code = ?
            ''', (difficulty, game_code))
            current_difficulty = difficulty
            conn.commit()
        
        # Проверяем, не истекла ли игра
        cursor.execute('SELECT expires_at FROM games WHERE game_code = ?', (game_code,))
        expires_at = cursor.fetchone()[0]
        if expires_at:
            expires_datetime = parse_datetime(expires_at)
            if expires_datetime and expires_datetime < datetime.now():
                cursor.execute('UPDATE games SET status = "expired" WHERE game_code = ?', (game_code,))
                conn.commit()
                return jsonify({'error': 'Игра истекла'}), 410
        
        # Получаем цель для текущей лунки
        if current_hole <= len(GOLF_HOLES):
            target = GOLF_HOLES[current_hole - 1]
        else:
            target = 0
        
        # Если игра в статусе pending или remaining равно 0, устанавливаем remaining = target
        if status == 'pending' or remaining == 0:
            cursor.execute('''
                UPDATE games 
                SET status = 'active', started_at = ?, remaining = ?
                WHERE game_code = ?
            ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), target, game_code))
            remaining = target
            status = 'active'
            conn.commit()
        
        # Получаем параметры сложности
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
            'message': f'Лунка {current_hole}: цель {target} оборотов. Ширина лунки: ±{tolerance}'
        })
        
    except Exception as e:
        print(f"❌ Ошибка в handle_info_request: {e}")
        return jsonify({'error': f'Ошибка сервера: {str(e)}'}), 500
    finally:
        conn.close()

def handle_shot_request(game_code, revolutions, device_id, difficulty):
    """Обработка запроса броска"""
    conn = sqlite3.connect('golf_league.db', check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        # Получаем данные игры с блокировкой строки
        cursor.execute('''
            SELECT difficulty, current_hole, remaining, status, 
                   telegram_id, total_shots, shots_on_current_hole
            FROM games 
            WHERE game_code = ?
        ''', (game_code,))
        
        game = cursor.fetchone()
        
        if not game:
            return jsonify({'error': 'Игра не найдена'}), 404
        
        (current_difficulty, current_hole, remaining, status, 
         telegram_id, total_shots, shots_on_hole) = game
        
        # Проверяем статус игры
        if status != 'active':
            return jsonify({'error': f'Игра не активна (статус: {status})'}), 400
        
        # Проверяем, что лунка существует
        if current_hole > len(GOLF_HOLES):
            return jsonify({'error': 'Игра уже завершена'}), 400
        
        # Получаем цель текущей лунки и толеранс
        target = GOLF_HOLES[current_hole - 1] if current_hole <= len(GOLF_HOLES) else 100
        tolerance = DIFFICULTY_LEVELS.get(current_difficulty, DIFFICULTY_LEVELS[1])["tolerance"]
        
        # Увеличиваем счетчик ударов
        total_shots += 1
        shots_on_hole += 1
        
        # Сохраняем остаток до броска
        remaining_before = remaining
        
        # Вычисляем новый остаток
        new_remaining = remaining_before - revolutions
        if new_remaining < 0:
            new_remaining = 0
        
        # Сохраняем бросок в базу
        cursor.execute('''
            INSERT INTO shots (game_code, device_id, hole_number, revolutions, 
                              remaining_before, remaining_after, is_success)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (game_code, device_id, current_hole, revolutions, 
              remaining_before, new_remaining, new_remaining <= tolerance))
        
        # Обновляем счетчики ударов
        cursor.execute('''
            UPDATE games 
            SET total_shots = ?, shots_on_current_hole = ?
            WHERE game_code = ?
        ''', (total_shots, shots_on_hole, game_code))
        
        # Проверяем успешность: если остаток ≤ tolerance, лунка завершена
        if new_remaining <= tolerance:
            # Лунка завершена!
            next_hole = current_hole + 1
            
            if next_hole <= len(GOLF_HOLES):
                # Переходим к следующей лунке
                next_target = GOLF_HOLES[next_hole - 1]
                
                cursor.execute('''
                    UPDATE games 
                    SET current_hole = ?, remaining = ?, 
                        shots_on_current_hole = 0, accumulated_revolutions = 0
                    WHERE game_code = ?
                ''', (next_hole, next_target, game_code))
                
                # Отправляем уведомление в Telegram
                send_telegram_update(game_code, 'hole_completed', {
                    'hole_number': current_hole,
                    'shots_on_hole': shots_on_hole,
                    'next_hole_distance': next_target
                })
                
                response = {
                    'status': 'hole_completed',
                    'message': f'🎉 Лунка {current_hole} пройдена за {shots_on_hole} ударов!',
                    'current_hole': current_hole,
                    'next_hole': next_hole,
                    'next_hole_distance': next_target,
                    'remaining': next_target,
                    'total_shots': total_shots,
                    'tolerance': tolerance,
                    'is_success': True
                }
            else:
                # Игра завершена
                cursor.execute('''
                    UPDATE games 
                    SET status = 'completed', completed_at = ?, remaining = 0
                    WHERE game_code = ?
                ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), game_code))
                
                # Обновляем статистику игрока
                cursor.execute('SELECT username, first_name FROM player_stats WHERE telegram_id = ?', (telegram_id,))
                player = cursor.fetchone()
                username = player[0] if player else None
                first_name = player[1] if player else "Игрок"
                
                update_player_stats(telegram_id, username, first_name, game_code)
                
                # Добавляем в лидерборд
                try:
                    cursor.execute('''
                        INSERT INTO leaderboard (telegram_id, game_code, total_score, difficulty)
                        VALUES (?, ?, ?, ?)
                    ''', (telegram_id, game_code, total_shots, current_difficulty))
                except Exception as e:
                    print(f"⚠️ Ошибка добавления в лидерборд: {e}")
                
                # Отправляем уведомление в Telegram
                send_telegram_update(game_code, 'game_completed', {
                    'final_score': total_shots
                })
                
                response = {
                    'status': 'game_completed',
                    'message': '🏆 Игра завершена! Отличная игра!',
                    'total_holes': len(GOLF_HOLES),
                    'total_shots': total_shots,
                    'final_score': total_shots,
                    'is_success': True
                }
        else:
            # Продолжаем текущую лунку
            cursor.execute('''
                UPDATE games 
                SET remaining = ?, accumulated_revolutions = accumulated_revolutions + ?
                WHERE game_code = ?
            ''', (new_remaining, revolutions, game_code))
            
            response = {
                'status': 'continue',
                'message': f'📊 Осталось: {new_remaining} из {target} (±{tolerance})',
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
            print("⚠️ База данных заблокирована, пробуем еще раз...")
            time.sleep(0.5)
            conn.close()
            time.sleep(0.5)
            return handle_shot_request(game_code, revolutions, device_id, difficulty)
        else:
            raise e
            
    except Exception as e:
        print(f"❌ Критическая ошибка в handle_shot_request: {e}")
        return jsonify({'error': f'Ошибка сервера: {str(e)}'}), 500
    finally:
        try:
            conn.close()
        except:
            pass

@app.route('/game/<game_code>')
def game_tracker(game_code):
    """Веб-страница отслеживания игры в реальном времени"""
    conn = sqlite3.connect('golf_league.db')
    cursor = conn.cursor()
    
    # Получаем информацию об игре
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
            <title>❌ Игра не найдена</title>
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
                <h1>❌ Игра не найдена</h1>
                <div class="error">
                    <p>Игра с кодом <strong>''' + game_code + '''</strong> не существует или была удалена.</p>
                    <p><a href="/">Вернуться на главную</a></p>
                </div>
            </div>
        </body>
        </html>
        '''
        return error_html, 404
    
    # Распаковываем данные
    game_code_db, player_name, current_hole, remaining, total_shots, status, created_at, difficulty = game
    
    # Получаем цель текущей лунки
    if current_hole <= len(GOLF_HOLES):
        target = GOLF_HOLES[current_hole - 1]
        progress_percent = min(100, int((target - remaining) / target * 100)) if target > 0 else 100
    else:
        target = 0
        progress_percent = 100
    
    # Получаем название сложности
    if difficulty in DIFFICULTY_LEVELS:
        difficulty_name = DIFFICULTY_LEVELS[difficulty]["name"]
        tolerance = DIFFICULTY_LEVELS[difficulty]["tolerance"]
    else:
        difficulty_name = "Неизвестно"
        tolerance = 5
    
    # Получаем последние 10 бросков
    cursor.execute('''
        SELECT hole_number, revolutions, timestamp
        FROM shots 
        WHERE game_code = ?
        ORDER BY timestamp DESC
        LIMIT 10
    ''', (game_code.upper(),))
    
    shots = cursor.fetchall()
    
    conn.close()
    
    # HTML шаблон с автообновлением
    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>🎮 Spinner Golf - {game_code_db}</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <!-- Автообновление каждые 10 секунд -->
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
                <div class="player-info">Игрок: {player_name}</div>
                <div class="player-info">Сложность: {difficulty_name} (±{tolerance})</div>
                <div class="player-info">Создана: {created_at}</div>
                
                <div class="status-badge status-{status}">
                    {{
                        '🎯 Активна' if status == 'active' else
                        '🏆 Завершена' if status == 'completed' else
                        '⏳ Ожидание' if status == 'pending' else status
                    }}
                </div>
            </div>
            
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-label">Текущая лунка</div>
                    <div class="stat-value">{current_hole}/18</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-label">Осталось оборотов</div>
                    <div class="stat-value">{remaining}</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-label">Всего ударов</div>
                    <div class="stat-value">{total_shots}</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-label">Цель лунки</div>
                    <div class="stat-value">{target}</div>
                </div>
            </div>
            
            {f'''
            <div class="progress-section">
                <div class="progress-title">
                    <span>Прогресс лунки {current_hole}</span>
                    <span>{progress_percent}%</span>
                </div>
                <div class="progress-bar">
                    <div class="progress-fill"></div>
                </div>
                <div class="hole-info">
                    🎯 Осталось: {remaining} из {target} оборотов
                </div>
            </div>
            ''' if status == 'active' and current_hole <= 18 else ''}
            
            <div class="shots-history">
                <div class="shots-title">📈 Последние броски</div>
                {f'''
                <div class="shots-list">
                    {' '.join([
                        f'''
                        <div class="shot-row">
                            <div><span class="shot-label">🕳️ Лунка:</span> <span class="shot-value">{hole}</span></div>
                            <div><span class="shot-label">🌀 Обороты:</span> <span class="shot-value">{rev}</span></div>
                            <div><span class="shot-label">🕐 Время:</span> <span class="shot-time">{time}</span></div>
                        </div>
                        ''' for hole, rev, time in shots
                    ]) if shots else '<p style="text-align: center; color: #888; padding: 20px;">Бросков пока нет</p>'}
                </div>
                '''}
            </div>
            
            <div class="footer">
                <div class="update-info">
                    🔄 Страница обновляется каждые 10 секунд<br>
                    Последнее обновление: {datetime.now().strftime("%H:%M:%S")}
                </div>
                <p style="margin-top: 15px;">
                    <a href="/" style="color: white; text-decoration: underline;">Главная страница</a> | 
                    <a href="https://t.me/spinner_golf_bot" style="color: white; text-decoration: underline;">Telegram бот</a>
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

# ==================== TELEGRAM БОТ ====================
def setup_telegram_bot():
    """Настройка и запуск Telegram бота"""
    if not bot:
        print("⚠️ Telegram бот не запущен: отсутствует токен")
        return
    
    @bot.message_handler(commands=['start', 'help'])
    def send_welcome(message):
        user = message.from_user
        welcome_text = f"""
🎮 Добро пожаловать в <b>Spinner Golf v3.4</b>, {user.first_name}!

<b>Упрощенная механика игры:</b>
• Каждая лунка имеет расстояние (например: 100 оборотов)
• Крутите спиннер, чтобы уменьшить расстояние до лунки
• Лунка считается завершенной, когда остаток ≤ tolerance
• Tolerance зависит от выбранного уровня сложности

🎯 <b>Уровни сложности:</b>
• 🥳 <b>Новичок</b>: ширина лунки ±10 оборотов
• 😊 <b>Любитель</b>: ширина лунки ±7 оборотов  
• 🤔 <b>Профи</b>: ширина лунки ±5 оборотов
• 😎 <b>Мастер</b>: ширина лунки ±3 оборота

📋 <b>Команды:</b>
/play - 🎯 Начать новую игру
/stats - 📊 Моя статистика
/leaderboard - 🏆 Топ игроков
/link - 🔗 Получить ссылку для отслеживания

Удачи! ⛳
        """
        bot.reply_to(message, welcome_text, parse_mode='HTML')
    
    @bot.message_handler(commands=['play'])
    def create_game(message):
        user = message.from_user
        
        # Генерируем код игры
        game_code = generate_game_code()
        
        # Устанавливаем время истечения
        expires_at = (datetime.now() + timedelta(hours=GAME_EXPIRE_HOURS)).strftime('%Y-%m-%d %H:%M:%S')
        
        # Устанавливаем начальный остаток для первой лунки
        first_hole_distance = GOLF_HOLES[0]
        
        conn = sqlite3.connect('golf_league.db', check_same_thread=False)
        cursor = conn.cursor()
        
        # Создаем новую игру
        cursor.execute('''
            INSERT INTO games (game_code, telegram_id, player_name, difficulty, expires_at, remaining)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (game_code, user.id, user.first_name, 1, expires_at, first_hole_distance))
        
        conn.commit()
        conn.close()
        
        # Формируем URL для отслеживания
        game_url = f"{SERVER_URL}/game/{game_code}"
        
        # Отправляем сообщение с инструкцией
        instructions = f"""
✅ <b>Игра создана!</b>

🎮 <b>Код игры:</b> <code>{game_code}</code>

📱 <b>Как подключить ESP32:</b>
1. Переведите ESP32 в режим настройки
2. Подключитесь к Wi-Fi сети <code>SpinnerGolf-Config</code>
3. Откройте в браузере <code>192.168.4.1</code>
4. Выберите уровень сложности
5. Введите этот код в поле "Код игры"
6. Сохраните настройки и переведите ESP32 в игровой режим
7. Нажмите кнопку для начала игры

🎯 <b>Уровни сложности в настройках ESP32:</b>
• 🥳 <b>Новичок</b>: ширина лунки ±10 оборотов
• 😊 <b>Любитель</b>: ширина лунки ±7 оборотов
• 🤔 <b>Профи</b>: ширина лунки ±5 оборотов
• 😎 <b>Мастер</b>: ширина лунки ±3 оборота

🌐 <b>Отслеживать игру онлайн:</b>
{game_url}

📱 <b>Поделитесь ссылкой</b> с друзьями, чтобы они следили за вашим прогрессом!

⏰ <b>Код действителен:</b> {GAME_EXPIRE_HOURS} часов
🏌️ <b>Количество лунок:</b> {len(GOLF_HOLES)}
⛳ <b>Первая лунка:</b> {first_hole_distance} оборотов

<b>Механика игры:</b>
• Лунка 1: {first_hole_distance} оборотов
• Крутите спиннер, чтобы уменьшить расстояние до лунки
• Лунка завершена, когда остаток ≤ выбранный tolerance
• Цель: пройти все 18 лунок за минимальное количество ударов!

Удачи! 🚀
        """
        
        bot.reply_to(message, instructions, parse_mode='HTML')
    
    @bot.message_handler(commands=['link'])
    def send_game_link(message):
        """Отправляет ссылку на отслеживание текущей игры"""
        user = message.from_user
        
        conn = sqlite3.connect('golf_league.db', check_same_thread=False)
        cursor = conn.cursor()
        
        # Ищем активную игру пользователя
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
                status_text = "активна"
            else:
                status_text = "ожидает начала"
            
            response = f"""
🔗 <b>Ссылка для отслеживания игры:</b>

{game_url}

🎮 <b>Код игры:</b> <code>{game_code}</code>
📊 <b>Статус:</b> {status_text}

📱 <b>Поделитесь этой ссылкой</b> с друзьями, чтобы они могли следить за вашим прогрессом в реальном времени!

<i>Страница обновляется автоматически каждые 10 секунд.</i>
"""
        else:
            response = "У вас нет активной игры. Начните новую игру командой /play"
        
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
            avg_score = f"{avg_score_result:.1f}" if avg_score_result else "Нет данных"
            
            if last_played:
                last_played_date = parse_datetime(last_played)
                if last_played_date:
                    last_played_str = last_played_date.strftime("%d.%m.%Y %H:%M")
                else:
                    last_played_str = "Неизвестно"
            else:
                last_played_str = "Еще не играли"
            
            stats_text = f"""
📊 <b>Статистика</b>

👤 <b>Игрок:</b> {user.first_name}
🎮 <b>Всего игр:</b> {total_games}
✅ <b>Завершено игр:</b> {completed_games}
🏌️ <b>Всего бросков:</b> {total_shots}
🎯 <b>Лучший результат:</b> {best_score if best_score != 999 else "Нет"}
📈 <b>Средний счет:</b> {avg_score}
🏆 <b>В топе:</b> {leaderboard_entries} раз
📅 <b>Последняя игра:</b> {last_played_str}
            """
        else:
            stats_text = """
📊 <b>Статистика</b>

У вас еще нет статистики! 🎮

Начните игру командой /play
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
            leaderboard_text = "<b>🏆 ТОП-10 ИГРОКОВ</b>\n\n"
            
            for i, (first_name, best_score, games_played) in enumerate(leaders, 1):
                medal = ""
                if i == 1: medal = "🥇 "
                elif i == 2: medal = "🥈 "
                elif i == 3: medal = "🥉 "
                
                leaderboard_text += f"{medal}{i}. {first_name}: {best_score} ({games_played} игр)\n"
            
            leaderboard_text += f"\nВсего игроков в рейтинге: {len(leaders)}"
        else:
            leaderboard_text = """
🏆 <b>ТОП ИГРОКОВ</b>

Рейтинг пока пуст! 🎮

Будьте первым, кто попадет в таблицу лидеров!
            """
        
        conn.close()
        bot.reply_to(message, leaderboard_text, parse_mode='HTML')
    
    # Запускаем бота
    print("🤖 Telegram бот запущен")
    bot.polling(none_stop=True)

# ==================== ЗАПУСК СЕРВЕРА ====================
def start_telegram_bot():
    """Запуск Telegram бота в отдельном потоке"""
    if TELEGRAM_TOKEN:
        try:
            setup_telegram_bot()
        except Exception as e:
            print(f"❌ Ошибка запуска Telegram бота: {e}")
    else:
        print("⚠️ Telegram бот не запущен: токен не указан")

if __name__ == '__main__':
    init_database()
    
    # Запускаем Telegram бота
    if TELEGRAM_TOKEN:
        bot_thread = threading.Thread(target=start_telegram_bot, daemon=True)
        bot_thread.start()
        print("🤖 Telegram бот запущен в отдельном потоке")
    
    # Запускаем Flask сервер
    port = int(os.environ.get('PORT', 10000))
    print(f"🚀 Flask сервер запущен на порту {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
