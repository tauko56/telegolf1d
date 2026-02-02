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
    # exit(1)

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
SERVER_URL = os.environ.get('SERVER_URL', 'https://spinner-golf.onrender.com')

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
            remaining INTEGER DEFAULT 0,  -- Остаток оборотов до лунки
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
            remaining_before INTEGER NOT NULL,  -- Остаток до броска
            remaining_after INTEGER NOT NULL,  -- Остаток после броска
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
        conn = sqlite3.connect('golf_league.db')
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
    conn = sqlite3.connect('golf_league.db')
    cursor = conn.cursor()
    
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
            body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }
            .header { text-align: center; margin-bottom: 40px; }
            .card { background: #f5f5f5; padding: 20px; border-radius: 10px; margin: 20px 0; }
            code { background: #eee; padding: 2px 5px; border-radius: 3px; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🏌️ Spinner Golf - Сервер</h1>
            <div style="background: #4CAF50; color: white; padding: 10px; border-radius: 5px; display: inline-block;">
                🟢 Сервер работает | 📱 Telegram бот: ''' + ('Активен' if TELEGRAM_TOKEN else 'Не настроен') + '''
            </div>
        </div>
        
        <div class="card">
            <h3>🎮 Как играть:</h3>
            <ol>
                <li>Найдите в Telegram бота <code>@spinner_golf_bot</code></li>
                <li>Отправьте команду <code>/play</code></li>
                <li>Получите код игры</li>
                <li>Введите код в настройках ESP32</li>
                <li>Начните играть!</li>
            </ol>
        </div>
        
        <div class="card">
            <h3>📡 API для ESP32:</h3>
            <p><strong>Эндпоинты:</strong></p>
            <ul>
                <li><code>/api/get_game?code=КОД</code> - получить параметры игры</li>
                <li><code>/api/submit_shot</code> - отправить результат броска</li>
            </ul>
        </div>
    </body>
    </html>
    '''

@app.route('/api/status')
def api_status():
    """Проверка статуса сервера"""
    return jsonify({
        'status': 'ok',
        'server': 'Spinner Golf',
        'timestamp': datetime.now().isoformat()
    })

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
        SELECT game_code, player_name, difficulty, current_hole, 
               remaining, status, expires_at, total_shots
        FROM games 
        WHERE game_code = ?
    ''', (game_code,))
    
    game = cursor.fetchone()
    
    if not game:
        conn.close()
        return jsonify({'error': 'Игра не найдена'}), 404
    
    game_code, player_name, difficulty, current_hole, remaining, status, expires_at, total_shots = game
    
    # Проверяем, не истекла ли игра
    if expires_at:
        expires_datetime = parse_datetime(expires_at)
        if expires_datetime and expires_datetime < datetime.now():
            cursor.execute('UPDATE games SET status = "expired" WHERE game_code = ?', (game_code,))
            conn.commit()
            conn.close()
            return jsonify({'error': 'Игра истекла'}), 410
    
    # Получаем цель для текущей лунки
    target = GOLF_HOLES[current_hole - 1]
    
    # Если игра в статусе pending или remaining равно 0 (начало лунки), устанавливаем remaining = target
    if status == 'pending' or remaining == 0:
        cursor.execute('''
            UPDATE games 
            SET status = 'active', started_at = ?, remaining = ?
            WHERE game_code = ?
        ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), target, game_code))
        remaining = target
        status = 'active'
    
    conn.commit()
    
    # Если игра завершена
    if status == 'completed':
        conn.close()
        return jsonify({
            'game_completed': True,
            'message': 'Игра завершена! Начните новую игру.'
        })
    
    # Если текущая лунка превышает количество лунок
    if current_hole > len(GOLF_HOLES):
        cursor.execute('''
            UPDATE games 
            SET status = 'completed', completed_at = ?
            WHERE game_code = ?
        ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), game_code))
        conn.commit()
        conn.close()
        return jsonify({
            'game_completed': True,
            'message': 'Игра завершена! Поздравляем!'
        })
    
    # Получаем параметры сложности
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
        'target': target,  # Полная дистанция лунки
        'remaining': remaining,  # Остаток до лунки
        'total_shots': total_shots,
        'status': status
    })

@app.route('/api/submit_shot', methods=['POST'])
def api_submit_shot():
    """Прием результатов броска от ESP32 - УПРОЩЕННАЯ МЕХАНИКА"""
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'Нет данных'}), 400
        
        game_code = data.get('game_code', '').upper().strip()
        revolutions = data.get('revolutions', 0)
        device_id = data.get('device_id', 'unknown')
        
        if not game_code:
            return jsonify({'error': 'Требуется код игры'}), 400
        
        if not isinstance(revolutions, int) or revolutions <= 0:
            return jsonify({'error': 'Некорректное количество оборотов'}), 400
        
        conn = sqlite3.connect('golf_league.db')
        cursor = conn.cursor()
        
        # Получаем данные игры
        cursor.execute('''
            SELECT difficulty, current_hole, remaining, status, telegram_id, total_shots
            FROM games 
            WHERE game_code = ?
        ''', (game_code,))
        
        game = cursor.fetchone()
        
        if not game:
            conn.close()
            return jsonify({'error': 'Игра не найдена'}), 404
        
        difficulty, current_hole, remaining, status, telegram_id, total_shots = game
        
        # Проверяем статус игры
        if status != 'active':
            conn.close()
            return jsonify({'error': f'Игра не активна (статус: {status})'}), 400
        
        # Проверяем, что лунка существует
        if current_hole > len(GOLF_HOLES):
            conn.close()
            return jsonify({'error': 'Игра уже завершена'}), 400
        
        # Получаем цель текущей лунки и толеранс
        target = GOLF_HOLES[current_hole - 1]
        tolerance = DIFFICULTY_LEVELS.get(difficulty, DIFFICULTY_LEVELS[1])["tolerance"]
        
        # Сохраняем остаток до броска
        remaining_before = remaining
        
        # УПРОЩЕННАЯ МЕХАНИКА: вычитаем брошенные обороты из оставшегося расстояния
        if revolutions > remaining_before:
            # Если крутили больше чем оставалось - это перекрут
            remaining_after = 0
        else:
            remaining_after = remaining_before - revolutions
        
        # Проверяем успешность: если остаток ≤ tolerance, лунка завершена
        is_success = remaining_after <= tolerance
        
        # Сохраняем бросок в базу
        cursor.execute('''
            INSERT INTO shots (game_code, device_id, hole_number, revolutions, 
                              remaining_before, remaining_after, is_success)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (game_code, device_id, current_hole, revolutions, 
              remaining_before, remaining_after, is_success))
        
        # Увеличиваем счетчик бросков
        total_shots += 1
        
        if is_success:
            # Лунка завершена!
            next_hole = current_hole + 1
            
            if next_hole <= len(GOLF_HOLES):
                # Переходим к следующей лунке
                next_target = GOLF_HOLES[next_hole - 1]
                cursor.execute('''
                    UPDATE games 
                    SET current_hole = ?, remaining = ?, total_shots = ?
                    WHERE game_code = ?
                ''', (next_hole, next_target, total_shots, game_code))
                
                response = {
                    'status': 'hole_completed',
                    'message': f'🎉 Лунка {current_hole} завершена!',
                    'next_hole': next_hole,
                    'next_target': next_target,
                    'remaining': 0,
                    'is_success': True
                }
            else:
                # Игра завершена
                cursor.execute('''
                    UPDATE games 
                    SET status = 'completed', completed_at = ?, total_shots = ?
                    WHERE game_code = ?
                ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), total_shots, game_code))
                
                # Обновляем статистику игрока
                cursor.execute('SELECT username, first_name FROM player_stats WHERE telegram_id = ?', (telegram_id,))
                player = cursor.fetchone()
                username = player[0] if player else None
                first_name = player[1] if player else "Игрок"
                
                update_player_stats(telegram_id, username, first_name, game_code)
                
                # Добавляем в лидерборд
                cursor.execute('''
                    INSERT INTO leaderboard (telegram_id, game_code, total_score, difficulty)
                    VALUES (?, ?, ?, ?)
                ''', (telegram_id, game_code, total_shots, difficulty))
                
                response = {
                    'status': 'game_completed',
                    'message': '🏆 Игра завершена! Отличная игра!',
                    'total_holes': len(GOLF_HOLES),
                    'total_shots': total_shots,
                    'is_success': True
                }
        else:
            # Продолжаем текущую лунку
            cursor.execute('''
                UPDATE games 
                SET remaining = ?, total_shots = ?
                WHERE game_code = ?
            ''', (remaining_after, total_shots, game_code))
            
            response = {
                'status': 'continue',
                'message': f'📊 Осталось: {remaining_after} из {target} (±{tolerance})',
                'remaining': remaining_after,
                'is_success': False
            }
        
        conn.commit()
        conn.close()
        return jsonify(response)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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
🎮 Добро пожаловать в <b>Spinner Golf</b>, {user.first_name}!

<b>Упрощенная механика игры:</b>
• Каждая лунка имеет расстояние (например: 100 оборотов)
• Крутите спиннер, чтобы уменьшить расстояние до лунки
• Лунка считается завершенной, когда остаток ≤ tolerance
• Tolerance зависит от уровня сложности

📋 <b>Команды:</b>
/play - 🎯 Начать новую игру
/stats - 📊 Моя статистика
/leaderboard - 🏆 Топ игроков

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
        
        conn = sqlite3.connect('golf_league.db')
        cursor = conn.cursor()
        
        # Создаем новую игру с remaining = дистанция первой лунки
        cursor.execute('''
            INSERT INTO games (game_code, telegram_id, player_name, difficulty, expires_at, remaining)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (game_code, user.id, user.first_name, 1, expires_at, first_hole_distance))
        
        conn.commit()
        conn.close()
        
        # Отправляем сообщение с инструкцией
        instructions = f"""
✅ <b>Игра создана!</b>

🎮 <b>Код игры:</b> <code>{game_code}</code>

📱 <b>Как подключить ESP32:</b>
1. Переведите ESP32 в режим настройки
2. Подключитесь к Wi-Fi сети <code>SpinnerGolf-Config</code>
3. Откройте в браузере <code>192.168.4.1</code>
4. Введите этот код в поле "Код игры"
5. Сохраните настройки и переведите ESP32 в игровой режим
6. Нажмите кнопку для начала игры

⏰ <b>Код действителен:</b> {GAME_EXPIRE_HOURS} часов
🎯 <b>Сложность:</b> Новичок (tolerance = 10)
🏌️ <b>Количество лунок:</b> {len(GOLF_HOLES)}
⛳ <b>Первая лунка:</b> {first_hole_distance} оборотов

<b>Механика игры:</b>
• Лунка 1: {first_hole_distance} оборотов
• Крутите спиннер, чтобы уменьшить расстояние до лунки
• Лунка завершена, когда остаток ≤ 10 оборотов
• Чем меньше бросков, тем лучше результат!

Удачи! 🚀
        """
        
        bot.reply_to(message, instructions, parse_mode='HTML')
    
    @bot.message_handler(commands=['stats'])
    def show_stats(message):
        user = message.from_user
        
        conn = sqlite3.connect('golf_league.db')
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
        conn = sqlite3.connect('golf_league.db')
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
    

