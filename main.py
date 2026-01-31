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
# Токен бота берется из переменных окружения Render
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    print("❌ ОШИБКА: TELEGRAM_TOKEN не установлен в переменных окружения Render!")
    print("   Добавьте TELEGRAM_TOKEN в настройках Render")
    # В продакшене можно выйти с ошибкой, но для разработки продолжим
    # exit(1)

# ID администраторов (ваш Telegram ID и, возможно, других доверенных лиц)
ADMIN_IDS = []
admin_ids_str = os.environ.get('ADMIN_IDS', '')
if admin_ids_str:
    try:
        ADMIN_IDS = [int(id.strip()) for id in admin_ids_str.split(',')]
    except:
        ADMIN_IDS = []
        print("⚠️ Не удалось распарсить ADMIN_IDS")

# Секретный ключ для подписи запросов (опционально)
SECRET_KEY = os.environ.get('SECRET_KEY', 'public_spinner_golf_2024')

# URL сервера (для вебхука)
SERVER_URL = os.environ.get('SERVER_URL', 'https://spinner-golf.onrender.com')

# Инициализация бота
bot = telebot.TeleBot(TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None

# ==================== ИГРОВЫЕ ПАРАМЕТРЫ ====================
# Вы как администратор можете менять эти параметры
GOLF_HOLES = [100, 150, 200, 120, 180, 90, 160, 210, 130, 
              140, 170, 110, 190, 125, 140, 160, 195, 105]

# Уровни сложности
DIFFICULTY_LEVELS = {
    1: {"name": "Новичок", "tolerance": 10, "multiplier": 1.0},
    2: {"name": "Любитель", "tolerance": 7, "multiplier": 1.2},
    3: {"name": "Профи", "tolerance": 5, "multiplier": 1.5},
    4: {"name": "Мастер", "tolerance": 3, "multiplier": 2.0}
}

# Время жизни игры (в часах)
GAME_EXPIRE_HOURS = 24

# ==================== БАЗА ДАННЫХ ====================
def init_database():
    conn = sqlite3.connect('golf_league.db')
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
            status TEXT DEFAULT 'pending',  -- pending, active, completed, expired
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
            target INTEGER NOT NULL,
            difference INTEGER NOT NULL,
            is_success BOOLEAN NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (game_code) REFERENCES games(game_code)
        )
    ''')
    
    # Таблица статистики игроков
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS player_stats (
            player_id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            total_games INTEGER DEFAULT 0,
            completed_games INTEGER DEFAULT 0,
            total_shots INTEGER DEFAULT 0,
            total_holes INTEGER DEFAULT 0,
            best_score INTEGER DEFAULT 999,
            last_played TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Таблица лидерборда (лучшие результаты)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS leaderboard (
            record_id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            game_code TEXT NOT NULL,
            total_score INTEGER NOT NULL,
            total_time INTEGER,  -- в секундах
            difficulty INTEGER DEFAULT 1,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (telegram_id) REFERENCES player_stats(telegram_id),
            FOREIGN KEY (game_code) REFERENCES games(game_code)
        )
    ''')
    
    # Создаем индексы для ускорения запросов
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_games_code ON games(game_code)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_games_telegram ON games(telegram_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_shots_code ON shots(game_code)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_player_stats_telegram ON player_stats(telegram_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_leaderboard_score ON leaderboard(total_score)')
    
    conn.commit()
    conn.close()
    print("✅ База данных инициализирована")

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def generate_game_code():
    """Генерация уникального 6-значного кода игры"""
    while True:
        code = ''.join(random.choices(string.ascii_uppercase, k=6))
        conn = sqlite3.connect('golf_league.db')
        cursor = conn.cursor()
        cursor.execute('SELECT game_code FROM games WHERE game_code = ?', (code,))
        if not cursor.fetchone():
            conn.close()
            return code
        conn.close()

def get_game_info(game_code):
    """Получение информации об игре"""
    conn = sqlite3.connect('golf_league.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT g.game_code, g.telegram_id, g.player_name, g.difficulty, 
               g.current_hole, g.status, g.total_shots, g.created_at,
               p.username, p.first_name
        FROM games g
        LEFT JOIN player_stats p ON g.telegram_id = p.telegram_id
        WHERE g.game_code = ?
    ''', (game_code,))
    game = cursor.fetchone()
    conn.close()
    return game

def update_player_stats(telegram_id, username, first_name, game_code):
    """Обновление статистики игрока после завершения игры"""
    conn = sqlite3.connect('golf_league.db')
    cursor = conn.cursor()
    
    # Получаем общее количество бросков в игре
    cursor.execute('SELECT COUNT(*) FROM shots WHERE game_code = ?', (game_code,))
    total_shots = cursor.fetchone()[0] or 0
    
    # Получаем количество пройденных лунок
    cursor.execute('SELECT current_hole FROM games WHERE game_code = ?', (game_code,))
    current_hole = cursor.fetchone()[0] or 0
    holes_completed = current_hole - 1  # -1 потому что current_hole указывает на следующую лунку
    
    # Обновляем или создаем запись игрока
    cursor.execute('''
        INSERT OR REPLACE INTO player_stats 
        (telegram_id, username, first_name, total_games, completed_games, 
         total_shots, total_holes, best_score, last_played)
        VALUES (?, ?, ?, 
            COALESCE((SELECT total_games + 1 FROM player_stats WHERE telegram_id = ?), 1),
            COALESCE((SELECT completed_games + 1 FROM player_stats WHERE telegram_id = ?), 1),
            COALESCE((SELECT total_shots FROM player_stats WHERE telegram_id = ?), 0) + ?,
            COALESCE((SELECT total_holes FROM player_stats WHERE telegram_id = ?), 0) + ?,
            CASE 
                WHEN ? < COALESCE((SELECT best_score FROM player_stats WHERE telegram_id = ?), 999) 
                THEN ? 
                ELSE COALESCE((SELECT best_score FROM player_stats WHERE telegram_id = ?), 999)
            END,
            ?
        )
    ''', (telegram_id, username, first_name, 
          telegram_id, telegram_id, telegram_id, total_shots,
          telegram_id, holes_completed,
          total_shots, telegram_id, total_shots, telegram_id,
          datetime.now()))
    
    conn.commit()
    conn.close()

# ==================== API ДЛЯ ESP32 ====================
@app.route('/')
def home():
    """Главная страница сервера"""
    html = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Spinner Golf - Публичный сервер</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {
                font-family: Arial, sans-serif;
                max-width: 800px;
                margin: 0 auto;
                padding: 20px;
                line-height: 1.6;
            }
            .header {
                text-align: center;
                margin-bottom: 40px;
            }
            .status {
                background: #4CAF50;
                color: white;
                padding: 10px;
                border-radius: 5px;
                display: inline-block;
                margin-bottom: 20px;
            }
            .card {
                background: #f5f5f5;
                padding: 20px;
                border-radius: 10px;
                margin: 20px 0;
            }
            code {
                background: #eee;
                padding: 2px 5px;
                border-radius: 3px;
                font-family: monospace;
            }
            .button {
                display: inline-block;
                background: #4CAF50;
                color: white;
                padding: 10px 20px;
                text-decoration: none;
                border-radius: 5px;
                margin: 10px 5px;
            }
            .admin-panel {
                background: #ffebee;
                border-left: 4px solid #f44336;
                padding: 15px;
                margin: 20px 0;
            }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🏌️ Spinner Golf - Публичный сервер</h1>
            <div class="status">
                🟢 Сервер работает | 📱 Telegram бот: ''' + ('Активен' if TELEGRAM_TOKEN else 'Не настроен') + '''
            </div>
        </div>
        
        <p>Добро пожаловать на публичный сервер игры Spinner Golf! Здесь вы можете играть в виртуальный гольф, используя спиннер и контроллер на ESP32.</p>
        
        <div class="card">
            <h3>🎮 Как начать играть:</h3>
            <ol>
                <li>Найдите в Telegram бота <code>@spinner_golf_bot</code></li>
                <li>Отправьте команду <code>/start</code> или <code>/play</code></li>
                <li>Получите 6-значный код игры</li>
                <li>Настройте ESP32 контроллер с этим кодом</li>
                <li>Начните играть!</li>
            </ol>
        </div>
        
        <div class="card">
            <h3>📡 API для ESP32:</h3>
            <p>Все запросы отправляйте на: <code>''' + SERVER_URL + '''</code></p>
            <p><strong>Эндпоинты:</strong></p>
            <ul>
                <li><code>/api/get_game?code=КОД</code> - получить параметры игры (GET)</li>
                <li><code>/api/submit_shot</code> - отправить результат броска (POST)</li>
                <li><code>/api/status</code> - проверить статус сервера (GET)</li>
            </ul>
            <p><strong>Пример запроса:</strong></p>
            <pre>GET ''' + SERVER_URL + '''/api/get_game?code=ABC123</pre>
        </div>
        
        <div class="card">
            <h3>📊 Статистика:</h3>
            <p>Следите за своими результатами и соревнуйтесь с другими игроками!</p>
            <p>Используйте команды в боте:</p>
            <ul>
                <li><code>/stats</code> - ваша статистика</li>
                <li><code>/leaderboard</code> - таблица лидеров</li>
                <li><code>/help</code> - помощь</li>
            </ul>
        </div>
        
        <div style="text-align: center; margin-top: 40px;">
            <p>👨‍💼 Администратор системы: [Ваше имя]</p>
            <p>🚀 Версия сервера: 2.0 (публичная)</p>
            <p>📅 Дата запуска: ''' + datetime.now().strftime("%d.%m.%Y") + '''</p>
        </div>
    </body>
    </html>
    '''
    return html

@app.route('/api/status')
def api_status():
    """Проверка статуса сервера"""
    return jsonify({
        'status': 'ok',
        'server': 'Spinner Golf Public Server',
        'version': '2.0',
        'telegram_bot': 'active' if TELEGRAM_TOKEN else 'inactive',
        'timestamp': datetime.now().isoformat(),
        'online_players': get_online_players_count(),
        'total_games': get_total_games_count()
    })

def get_online_players_count():
    """Получение количества активных игроков (игравших за последние 24 часа)"""
    conn = sqlite3.connect('golf_league.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT COUNT(DISTINCT telegram_id) 
        FROM games 
        WHERE created_at > datetime('now', '-1 day')
    ''')
    count = cursor.fetchone()[0] or 0
    conn.close()
    return count

def get_total_games_count():
    """Получение общего количества игр"""
    conn = sqlite3.connect('golf_league.db')
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM games')
    count = cursor.fetchone()[0] or 0
    conn.close()
    return count

@app.route('/api/get_game', methods=['GET'])
def api_get_game():
    """Получение параметров игры для ESP32"""
    game_code = request.args.get('code', '').upper().strip()
    
    if not game_code:
        return jsonify({'error': 'Требуется код игры'}), 400
    
    conn = sqlite3.connect('golf_league.db')
    cursor = conn.cursor()
    
    # Проверяем существование игры
    cursor.execute('''
        SELECT game_code, telegram_id, player_name, difficulty, 
               current_hole, status, expires_at
        FROM games 
        WHERE game_code = ?
    ''', (game_code,))
    
    game = cursor.fetchone()
    
    if not game:
        return jsonify({'error': 'Игра не найдена'}), 404
    
    game_code, telegram_id, player_name, difficulty, current_hole, status, expires_at = game
    
    # Проверяем, не истекла ли игра
    if expires_at and datetime.strptime(expires_at, '%Y-%m-%d %H:%M:%S') < datetime.now():
        cursor.execute('UPDATE games SET status = "expired" WHERE game_code = ?', (game_code,))
        conn.commit()
        conn.close()
        return jsonify({'error': 'Игра истекла'}), 410
    
    # Если игра в статусе pending, переводим в active
    if status == 'pending':
        cursor.execute('''
            UPDATE games 
            SET status = 'active', started_at = ?
            WHERE game_code = ?
        ''', (datetime.now(), game_code))
        status = 'active'
    
    conn.commit()
    
    # Если игра завершена
    if status == 'completed':
        conn.close()
        return jsonify({
            'game_completed': True,
            'message': 'Игра завершена! Начните новую игру в Telegram боте.'
        })
    
    # Если текущая лунка превышает количество лунок
    if current_hole > len(GOLF_HOLES):
        cursor.execute('''
            UPDATE games 
            SET status = 'completed', completed_at = ?
            WHERE game_code = ?
        ''', (datetime.now(), game_code))
        conn.commit()
        conn.close()
        return jsonify({
            'game_completed': True,
            'message': 'Игра завершена! Поздравляем!'
        })
    
    # Получаем параметры текущей лунки
    target = GOLF_HOLES[current_hole - 1]
    tolerance = DIFFICULTY_LEVELS.get(difficulty, DIFFICULTY_LEVELS[1])["tolerance"]
    
    conn.close()
    
    return jsonify({
        'success': True,
        'game_code': game_code,
        'player_name': player_name,
        'difficulty': difficulty,
        'tolerance': tolerance,
        'current_hole': current_hole,
        'total_holes': len(GOLF_HOLES),
        'target': target,
        'status': status
    })

@app.route('/api/submit_shot', methods=['POST'])
def api_submit_shot():
    """Прием результатов броска от ESP32"""
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'Нет данных'}), 400
        
        game_code = data.get('game_code', '').upper().strip()
        revolutions = data.get('revolutions', 0)
        hole = data.get('hole', 1)
        device_id = data.get('device_id', 'unknown')
        
        if not game_code:
            return jsonify({'error': 'Требуется код игры'}), 400
        
        if not isinstance(revolutions, int) or revolutions <= 0:
            return jsonify({'error': 'Некорректное количество оборотов'}), 400
        
        conn = sqlite3.connect('golf_league.db')
        cursor = conn.cursor()
        
        # Получаем данные игры
        cursor.execute('''
            SELECT difficulty, current_hole, status, telegram_id
            FROM games 
            WHERE game_code = ?
        ''', (game_code,))
        
        game = cursor.fetchone()
        
        if not game:
            conn.close()
            return jsonify({'error': 'Игра не найдена'}), 404
        
        difficulty, current_hole, status, telegram_id = game
        
        # Проверяем статус игры
        if status != 'active':
            conn.close()
            return jsonify({'error': f'Игра не активна (статус: {status})'}), 400
        
        # Проверяем номер лунки
        if hole != current_hole:
            conn.close()
            return jsonify({'error': f'Неверный номер лунки. Ожидается лунка {current_hole}'}), 400
        
        # Получаем цель для текущей лунки
        target = GOLF_HOLES[hole - 1]
        tolerance = DIFFICULTY_LEVELS.get(difficulty, DIFFICULTY_LEVELS[1])["tolerance"]
        difference = abs(revolutions - target)
        is_success = difference <= tolerance
        
        # Сохраняем бросок в базу
        cursor.execute('''
            INSERT INTO shots (game_code, device_id, hole_number, revolutions, target, difference, is_success)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (game_code, device_id, hole, revolutions, target, difference, is_success))
        
        # Увеличиваем счетчик бросков в игре
        cursor.execute('''
            UPDATE games 
            SET total_shots = total_shots + 1 
            WHERE game_code = ?
        ''', (game_code,))
        
        # Если бросок успешный, переходим к следующей лунке
        if is_success:
            next_hole = hole + 1
            
            if next_hole <= len(GOLF_HOLES):
                cursor.execute('UPDATE games SET current_hole = ? WHERE game_code = ?',
                              (next_hole, game_code))
                
                response = {
                    'status': 'hole_completed',
                    'message': f'🎉 Лунка {hole} завершена!',
                    'next_hole': next_hole,
                    'next_target': GOLF_HOLES[next_hole - 1],
                    'difference': difference,
                    'is_success': True
                }
            else:
                # Игра завершена
                cursor.execute('''
                    UPDATE games 
                    SET status = 'completed', current_hole = ?, completed_at = ?
                    WHERE game_code = ?
                ''', (next_hole, datetime.now(), game_code))
                
                # Получаем статистику игрока для обновления
                cursor.execute('SELECT username, first_name FROM player_stats WHERE telegram_id = ?', (telegram_id,))
                player = cursor.fetchone()
                username = player[0] if player else None
                first_name = player[1] if player else "Игрок"
                
                # Обновляем статистику игрока
                update_player_stats(telegram_id, username, first_name, game_code)
                
                # Добавляем запись в лидерборд
                cursor.execute('''
                    SELECT COUNT(*) as total_shots, 
                           (julianday(completed_at) - julianday(started_at)) * 24 * 60 * 60 as total_time
                    FROM games 
                    WHERE game_code = ?
                ''', (game_code,))
                
                game_stats = cursor.fetchone()
                total_shots = game_stats[0] if game_stats else 0
                total_time = int(game_stats[1]) if game_stats and game_stats[1] else 0
                
                cursor.execute('''
                    INSERT INTO leaderboard (telegram_id, game_code, total_score, total_time, difficulty)
                    VALUES (?, ?, ?, ?, ?)
                ''', (telegram_id, game_code, total_shots, total_time, difficulty))
                
                response = {
                    'status': 'game_completed',
                    'message': '🏆 Игра завершена! Отличная игра!',
                    'total_holes': len(GOLF_HOLES),
                    'total_shots': total_shots,
                    'is_success': True
                }
        else:
            response = {
                'status': 'continue',
                'message': f'Разница: {difference} оборотов (цель: {target})',
                'needed': target - revolutions,
                'is_success': False
            }
        
        conn.commit()
        conn.close()
        return jsonify(response)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==================== TELEGRAM БОТ ====================
def setup_telegram_bot():
    """Настройка и запуск Telegram бота в отдельном потоке"""
    if not bot:
        print("⚠️ Telegram бот не запущен: отсутствует токен")
        return
    
    @bot.message_handler(commands=['start', 'help'])
    def send_welcome(message):
        user = message.from_user
        welcome_text = f"""
🎮 Добро пожаловать в <b>Spinner Golf</b>, {user.first_name}!

Я помогу тебе сыграть в виртуальный гольф с использованием спиннера и контроллера на ESP32.

📋 <b>Доступные команды:</b>
/play - 🎯 Начать новую игру
/stats - 📊 Моя статистика
/leaderboard - 🏆 Топ игроков
/help - ℹ️  Помощь

🎯 <b>Как начать играть:</b>
1. Собери контроллер ESP32 по инструкции
2. Настрой его через Wi-Fi "SpinnerGolf-Config"
3. Получи код игры командой /play
4. Введи код в настройках ESP32
5. Начни играть!

Удачи на поле! ⛳
        """
        bot.reply_to(message, welcome_text, parse_mode='HTML')
    
    @bot.message_handler(commands=['play'])
    def create_game(message):
        user = message.from_user
        
        # Генерируем код игры
        game_code = generate_game_code()
        
        # Устанавливаем время истечения игры
        expires_at = datetime.now() + timedelta(hours=GAME_EXPIRE_HOURS)
        
        conn = sqlite3.connect('golf_league.db')
        cursor = conn.cursor()
        
        # Создаем новую игру
        cursor.execute('''
            INSERT INTO games (game_code, telegram_id, player_name, difficulty, expires_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (game_code, user.id, user.first_name, 1, expires_at))
        
        conn.commit()
        conn.close()
        
        # Отправляем сообщение с инструкцией
        instructions = f"""
✅ <b>Игра создана!</b>

🎮 <b>Код игры:</b> <code>{game_code}</code>

📱 <b>Как подключить ESP32:</b>
1. Переведите ESP32 в режим настройки (переключатель вниз)
2. Подключитесь к Wi-Fi сети <code>SpinnerGolf-Config</code>
3. Откройте в браузере <code>192.168.4.1</code>
4. Введите этот код в поле "Код игры"
5. Сохраните настройки и переведите ESP32 в игровой режим
6. Нажмите кнопку на контроллере для начала игры

⏰ <b>Код действителен:</b> {GAME_EXPIRE_HOURS} часов
🎯 <b>Сложность:</b> Новичок
🏌️ <b>Количество лунок:</b> {len(GOLF_HOLES)}

⚠️ <b>Важно:</b> Бесплатный сервер может "засыпать" после 15 минут бездействия. 
Первый запрос после простоя может занимать до 60 секунд.

Удачи! 🚀
        """
        
        bot.reply_to(message, instructions, parse_mode='HTML')
    
    @bot.message_handler(commands=['stats'])
    def show_stats(message):
        user = message.from_user
        
        conn = sqlite3.connect('golf_league.db')
        cursor = conn.cursor()
        
        # Получаем статистику игрока
        cursor.execute('''
            SELECT total_games, completed_games, total_shots, total_holes, best_score, last_played
            FROM player_stats 
            WHERE telegram_id = ?
        ''', (user.id,))
        
        stats = cursor.fetchone()
        
        if stats:
            total_games, completed_games, total_shots, total_holes, best_score, last_played = stats
            
            # Получаем дополнительные данные
            cursor.execute('''
                SELECT COUNT(*) FROM leaderboard WHERE telegram_id = ?
            ''', (user.id,))
            leaderboard_entries = cursor.fetchone()[0] or 0
            
            cursor.execute('''
                SELECT AVG(total_score) FROM leaderboard WHERE telegram_id = ?
            ''', (user.id,))
            avg_score_result = cursor.fetchone()[0]
            avg_score = f"{avg_score_result:.1f}" if avg_score_result else "Нет данных"
            
            # Форматируем дату последней игры
            if last_played:
                last_played_date = datetime.strptime(last_played, '%Y-%m-%d %H:%M:%S')
                last_played_str = last_played_date.strftime("%d.%m.%Y %H:%M")
            else:
                last_played_str = "Еще не играли"
            
            stats_text = f"""
📊 <b>Ваша статистика</b>

👤 <b>Игрок:</b> {user.first_name}
🎮 <b>Всего игр:</b> {total_games}
✅ <b>Завершено игр:</b> {completed_games}
🏌️ <b>Всего бросков:</b> {total_shots}
⛳ <b>Пройдено лунок:</b> {total_holes}
🎯 <b>Лучший результат:</b> {best_score if best_score != 999 else "Нет"}
📈 <b>Средний счет:</b> {avg_score}
🏆 <b>В топе:</b> {leaderboard_entries} раз
📅 <b>Последняя игра:</b> {last_played_str}
            """
        else:
            stats_text = """
📊 <b>Ваша статистика</b>

У вас еще нет статистики! 🎮

Начните свою первую игру командой /play и покажите всем, на что вы способны! 💪
            """
        
        conn.close()
        bot.reply_to(message, stats_text, parse_mode='HTML')
    
    @bot.message_handler(commands=['leaderboard'])
    def show_leaderboard(message):
        conn = sqlite3.connect('golf_league.db')
        cursor = conn.cursor()
        
        # Получаем топ-10 игроков по лучшему результату
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
Начните игру командой /play и покажите всем, на что вы способны! 💪
            """
        
        conn.close()
        bot.reply_to(message, leaderboard_text, parse_mode='HTML')
    
    # ==================== АДМИН-КОМАНДЫ ====================
    @bot.message_handler(commands=['admin'])
    def admin_panel(message):
        user = message.from_user
        
        if user.id not in ADMIN_IDS:
            bot.reply_to(message, "⛔ У вас нет прав администратора")
            return
        
        admin_text = """
🔧 <b>Админ-панель Spinner Golf</b>

📊 <b>Статистика:</b>
/admin_stats - Статистика сервера
/admin_users - Список пользователей
/admin_games - Список игр

⚙️ <b>Управление:</b>
/admin_broadcast - Рассылка сообщений
/admin_reset - Сброс игры (только для разработки)

🛠️ <b>Система:</b>
/admin_restart - Перезапуск бота
/admin_export - Экспорт данных
        """
        
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"),
            types.InlineKeyboardButton("👥 Пользователи", callback_data="admin_users")
        )
        markup.row(
            types.InlineKeyboardButton("🎮 Игры", callback_data="admin_games"),
            types.InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast")
        )
        
        bot.reply_to(message, admin_text, parse_mode='HTML', reply_markup=markup)
    
    @bot.callback_query_handler(func=lambda call: True)
    def handle_callback_query(call):
        user = call.from_user
        
        if user.id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "⛔ У вас нет прав администратора")
            return
        
        if call.data == "admin_stats":
            conn = sqlite3.connect('golf_league.db')
            cursor = conn.cursor()
            
            cursor.execute('SELECT COUNT(*) FROM games')
            total_games = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM player_stats')
            total_players = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM games WHERE status = "active"')
            active_games = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM games WHERE created_at > datetime("now", "-1 day")')
            today_games = cursor.fetchone()[0]
            
            conn.close()
            
            stats_text = f"""
📊 <b>Статистика сервера</b>

🎮 <b>Всего игр:</b> {total_games}
👥 <b>Зарегистрировано игроков:</b> {total_players}
🟢 <b>Активных игр:</b> {active_games}
📅 <b>Игр за сегодня:</b> {today_games}
🕒 <b>Время работы:</b> {get_uptime()}
🌐 <b>URL сервера:</b> {SERVER_URL}
            """
            
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=stats_text,
                parse_mode='HTML'
            )
        
        elif call.data == "admin_users":
            conn = sqlite3.connect('golf_league.db')
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT first_name, total_games, best_score, last_played
                FROM player_stats 
                ORDER BY last_played DESC 
                LIMIT 10
            ''')
            
            users = cursor.fetchall()
            conn.close()
            
            if users:
                users_text = "<b>👥 Последние 10 игроков</b>\n\n"
                
                for i, (first_name, total_games, best_score, last_played) in enumerate(users, 1):
                    last_played_date = datetime.strptime(last_played, '%Y-%m-%d %H:%M:%S') if last_played else None
                    last_played_str = last_played_date.strftime("%d.%m.%Y") if last_played_date else "Никогда"
                    
                    users_text += f"{i}. {first_name}: {total_games} игр, лучший: {best_score if best_score != 999 else 'Нет'}, последняя: {last_played_str}\n"
            else:
                users_text = "Нет данных об игроках"
            
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=users_text,
                parse_mode='HTML'
            )
        
        bot.answer_callback_query(call.id)
    
    # Запускаем бота
    print("🤖 Telegram бот запущен")
    bot.polling(none_stop=True)

def get_uptime():
    """Получение времени работы сервера (для отображения в админке)"""
    # В реальной реализации нужно сохранять время старта сервера
    return "Несколько часов"  # Заглушка

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
    # Инициализируем базу данных
    init_database()
    
    # Запускаем Telegram бота в отдельном потоке
    if TELEGRAM_TOKEN:
        import threading
        bot_thread = threading.Thread(target=start_telegram_bot, daemon=True)
        bot_thread.start()
        print("🤖 Telegram бот запущен в отдельном потоке")
    else:
        print("⚠️ Telegram бот не запущен (отсутствует токен)")
    
    # Запускаем Flask сервер
    port = int(os.environ.get('PORT', 10000))
    print(f"🚀 Flask сервер запущен на порту {port}")
    print(f"🌐 Доступ по адресу: http://0.0.0.0:{port}")
    
    app.run(host='0.0.0.0', port=port, debug=False)
