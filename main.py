import os
import sqlite3
import random
import string
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)

# ==================== БЕЗОПАСНОЕ ПОЛУЧЕНИЕ ТОКЕНА ====================
# Токен берется ТОЛЬКО из переменных окружения Render
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
SECRET_KEY = os.environ.get('SECRET_KEY', 'public_key_2024')
SERVER_URL = os.environ.get('SERVER_URL', 'https://telegolf1d.onrender.com')

# Логируем (но не показываем токен)
print(f"🔧 Конфигурация загружена")
print(f"   Telegram Token: {'✅ Установлен' if TELEGRAM_TOKEN else '❌ Не установлен'}")
print(f"   Server URL: {SERVER_URL}")

# Проверяем наличие токена
if not TELEGRAM_TOKEN:
    print("⚠️ ВНИМАНИЕ: TELEGRAM_TOKEN не установлен в переменных окружения!")
    print("   Добавьте TELEGRAM_TOKEN в настройках Render")

# ==================== БАЗА ДАННЫХ ====================
def init_database():
    conn = sqlite3.connect('golf_league.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS games (
            game_id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_code TEXT UNIQUE,
            telegram_id INTEGER,
            player_name TEXT,
            status TEXT DEFAULT 'pending',
            difficulty INTEGER DEFAULT 1,
            current_hole INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            completed_at TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shots (
            shot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_code TEXT,
            device_id TEXT,
            hole_number INTEGER,
            revolutions INTEGER,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS devices (
            device_id TEXT PRIMARY KEY,
            device_name TEXT,
            last_seen TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()
    print("✅ База данных готова")

# ==================== ИГРОВЫЕ ПАРАМЕТРЫ (ФИКСИРОВАННЫЕ) ====================
GOLF_HOLES = [140, 180, 100, 200, 135, 100, 170, 210, 100, 
              150, 180, 120, 200, 135, 120, 170, 200, 110]

def get_tolerance(difficulty):
    return {1: 10, 2: 7, 3: 5, 4: 3, 5: 2}.get(difficulty, 5)

def generate_game_code():
    while True:
        code = ''.join(random.choices(string.ascii_uppercase, k=6))
        conn = sqlite3.connect('golf_league.db')
        cursor = conn.cursor()
        cursor.execute('SELECT game_code FROM games WHERE game_code = ?', (code,))
        if not cursor.fetchone():
            conn.close()
            return code
        conn.close()

# ==================== ОСНОВНЫЕ API ====================
@app.route('/')
def home():
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Spinner Golf - Публичный сервер</title>
        <meta charset="UTF-8">
        <style>
            body { font-family: Arial; max-width: 800px; margin: 0 auto; padding: 20px; }
            .header { text-align: center; margin-bottom: 40px; }
            .status { background: #4CAF50; color: white; padding: 10px; border-radius: 5px; }
            .card { background: #f5f5f5; padding: 20px; border-radius: 10px; margin: 20px 0; }
            code { background: #eee; padding: 2px 5px; border-radius: 3px; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🏌️ Spinner Golf - Публичный сервер</h1>
            <div class="status">
                🟢 Сервер работает | 📱 Бот: ''' + ('Активен' if TELEGRAM_TOKEN else 'Не настроен') + '''
            </div>
        </div>
        
        <div class="card">
            <h3>🎮 Как подключиться:</h3>
            <ol>
                <li>Соберите контроллер ESP32 по инструкции</li>
                <li>Загрузите код с GitHub</li>
                <li>Откройте Telegram бота: <code>@spinner_golf_public_bot</code></li>
                <li>Получите код игры командой <code>/newgame</code></li>
                <li>Введите код в настройках ESP32</li>
                <li>Играйте!</li>
            </ol>
        </div>
        
        <div class="card">
            <h3>📡 API для ESP32:</h3>
            <p>URL: <code>https://telegolf1d.onrender.com</code></p>
            <p>Эндпоинты:</p>
            <ul>
                <li><code>/api/get_game?code=КОД</code> - получить игру</li>
                <li><code>/api/submit_shot</code> - отправить результат</li>
                <li><code>/api/status</code> - проверить сервер</li>
            </ul>
        </div>
        
        <div style="text-align: center; margin-top: 40px;">
            <p>👨‍💼 Администратор системы: [Ваше имя]</p>
            <p>🚀 Публичная версия: 1.0</p>
        </div>
    </body>
    </html>
    '''

@app.route('/api/status')
def api_status():
    return jsonify({
        'status': 'ok',
        'server': 'Spinner Golf Public Server',
        'version': '1.0',
        'telegram_bot': 'active' if TELEGRAM_TOKEN else 'inactive',
        'timestamp': datetime.now().isoformat()
    })

@app.route('/api/get_game', methods=['GET'])
def api_get_game():
    game_code = request.args.get('code', '').upper()
    
    if not game_code:
        return jsonify({'error': 'Требуется код игры'}), 400
    
    conn = sqlite3.connect('golf_league.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT status, difficulty, current_hole FROM games WHERE game_code = ?', (game_code,))
    game = cursor.fetchone()
    
    if not game:
        return jsonify({'error': 'Игра не найдена'}), 404
    
    status, difficulty, current_hole = game
    
    if status not in ['pending', 'active']:
        return jsonify({'error': 'Игра завершена'}), 400
    
    if status == 'pending':
        cursor.execute('UPDATE games SET status = "active", started_at = ? WHERE game_code = ?',
                      (datetime.now(), game_code))
    
    if current_hole > len(GOLF_HOLES):
        return jsonify({
            'success': True,
            'game_completed': True,
            'message': 'Игра завершена!'
        })
    
    tolerance = get_tolerance(difficulty)
    
    response = {
        'success': True,
        'game_code': game_code,
        'difficulty': difficulty,
        'tolerance': tolerance,
        'current_hole': current_hole,
        'total_holes': len(GOLF_HOLES),
        'target': GOLF_HOLES[current_hole - 1]
    }
    
    conn.commit()
    conn.close()
    return jsonify(response)

@app.route('/api/submit_shot', methods=['POST'])
def api_submit_shot():
    try:
        data = request.json
        game_code = data.get('game_code', '').upper()
        revolutions = data.get('revolutions', 0)
        hole = data.get('hole', 1)
        device_id = data.get('device_id', 'unknown')
        
        if not game_code:
            return jsonify({'error': 'Требуется код игры'}), 400
        
        conn = sqlite3.connect('golf_league.db')
        cursor = conn.cursor()
        
        cursor.execute('SELECT difficulty, current_hole FROM games WHERE game_code = ?', (game_code,))
        game = cursor.fetchone()
        
        if not game:
            return jsonify({'error': 'Игра не найдена'}), 404
        
        difficulty, current_hole = game
        
        if hole != current_hole:
            return jsonify({'error': 'Неверный номер лунки'}), 400
        
        target = GOLF_HOLES[hole - 1]
        tolerance = get_tolerance(difficulty)
        difference = abs(revolutions - target)
        
        # Сохраняем бросок
        cursor.execute('''
            INSERT INTO shots (game_code, device_id, hole_number, revolutions)
            VALUES (?, ?, ?, ?)
        ''', (game_code, device_id, hole, revolutions))
        
        # Обновляем устройство
        cursor.execute('''
            INSERT OR REPLACE INTO devices (device_id, last_seen)
            VALUES (?, ?)
        ''', (device_id, datetime.now()))
        
        # Проверяем попадание
        if difference <= tolerance:
            next_hole = hole + 1
            
            if next_hole <= len(GOLF_HOLES):
                cursor.execute('UPDATE games SET current_hole = ? WHERE game_code = ?',
                              (next_hole, game_code))
                
                response = {
                    'status': 'hole_completed',
                    'message': f'Лунка {hole} завершена!',
                    'next_hole': next_hole,
                    'next_target': GOLF_HOLES[next_hole - 1]
                }
            else:
                cursor.execute('UPDATE games SET status = "completed", completed_at = ? WHERE game_code = ?',
                              (datetime.now(), game_code))
                
                response = {
                    'status': 'game_completed',
                    'message': 'Игра завершена! Отличная игра!'
                }
        else:
            response = {
                'status': 'continue',
                'message': f'Разница: {difference} оборотов',
                'needed': target - revolutions
            }
        
        conn.commit()
        conn.close()
        return jsonify(response)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==================== API ДЛЯ TELEGRAM БОТА ====================
def send_telegram_message(chat_id, text):
    """Безопасная отправка сообщения в Telegram"""
    if not TELEGRAM_TOKEN:
        print(f"⚠️ Не могу отправить сообщение: токен не установлен")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML'
    }
    
    try:
        response = requests.post(url, json=data, timeout=10)
        if response.status_code != 200:
            print(f"⚠️ Ошибка отправки в Telegram: {response.status_code}")
    except Exception as e:
        print(f"⚠️ Ошибка соединения с Telegram: {e}")

@app.route('/api/telegram/create_game', methods=['POST'])
def api_telegram_create_game():
    """ТОЛЬКО для внутреннего использования Telegram ботом"""
    if not TELEGRAM_TOKEN:
        return jsonify({'error': 'Telegram бот не настроен администратором'}), 503
    
    try:
        data = request.json
        telegram_id = data.get('telegram_id')
        player_name = data.get('player_name', 'Игрок')
        
        if not telegram_id:
            return jsonify({'error': 'Требуется telegram_id'}), 400
        
        game_code = generate_game_code()
        
        conn = sqlite3.connect('golf_league.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO games (game_code, telegram_id, player_name, status)
            VALUES (?, ?, ?, 'pending')
        ''', (game_code, telegram_id, player_name))
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'game_code': game_code,
            'message': 'Игра создана'
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/telegram/webhook', methods=['POST'])
def telegram_webhook():
    """Вебхук для Telegram бота"""
    if not TELEGRAM_TOKEN:
        return jsonify({'error': 'Bot not configured'}), 503
    
    try:
        data = request.json
        message = data.get('message', {})
        chat_id = message.get('chat', {}).get('id')
        text = message.get('text', '')
        
        if not chat_id:
            return jsonify({'ok': True})
        
        # Обработка команды /start
        if text == '/start' or text == '/start@spinner_golf_public_bot':
            user = message.get('from', {})
            telegram_id = user.get('id')
            username = user.get('username', '')
            first_name = user.get('first_name', '')
            
            player_name = username or first_name or f"Игрок_{telegram_id}"
            
            # Создаем игру через наш же API
            import requests
            response = requests.post(
                f"{SERVER_URL}/api/telegram/create_game",
                json={
                    'telegram_id': telegram_id,
                    'player_name': player_name
                },
                timeout=30
            )
            
            if response.status_code == 200:
                game_data = response.json()
                game_code = game_data['game_code']
                
                # Отправляем сообщение пользователю
                send_telegram_message(
                    chat_id,
                    f'🎮 <b>Spinner Golf</b>\n\n'
                    f'✅ Игра создана!\n\n'
                    f'<b>Код игры:</b> <code>{game_code}</code>\n\n'
                    f'📱 <b>Как использовать:</b>\n'
                    f'1. Переведите ESP32 в режим настройки\n'
                    f'2. Подключитесь к Wi-Fi "SpinnerGolf-Config"\n'
                    f'3. Откройте 192.168.4.1 в браузере\n'
                    f'4. Введите этот код в поле "Код игры"\n'
                    f'5. Сохраните и переключитесь в игровой режим\n\n'
                    f'🎯 Удачи в игре!'
                )
            else:
                send_telegram_message(
                    chat_id,
                    '❌ Сервер временно недоступен\n'
                    'Попробуйте через 30 секунд...'
                )
        
        # Обработка команды /help
        elif text == '/help' or text == '/help@spinner_golf_public_bot':
            help_text = (
                '🎮 <b>Spinner Golf Bot</b>\n\n'
                '<b>Команды:</b>\n'
                '/start - создать новую игру\n'
                '/help - показать это сообщение\n\n'
                '<b>Как играть:</b>\n'
                '1. Создайте игру командой /start\n'
                '2. Получите 6-значный код\n'
                '3. Введите код в настройках ESP32\n'
                '4. Крутите спиннер и попадайте в лунки!\n\n'
                '📡 Сервер: https://telegolf1d.onrender.com'
            )
            send_telegram_message(chat_id, help_text)
        
        # Обработка неизвестных команд
        elif text.startswith('/'):
            send_telegram_message(
                chat_id,
                '❓ Неизвестная команда\n'
                'Используйте /start для создания игры\n'
                'Или /help для помощи'
            )
        
        return jsonify({'ok': True})
        
    except Exception as e:
        print(f"Ошибка в вебхуке: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500

# ==================== ЗАПУСК СЕРВЕРА ====================
if __name__ == '__main__':
    init_database()
    port = int(os.environ.get('PORT', 10000))
    
    # Настраиваем вебхук для Telegram бота
    if TELEGRAM_TOKEN:
        try:
            webhook_url = f"{SERVER_URL}/api/telegram/webhook"
            set_webhook_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={webhook_url}"
            response = requests.get(set_webhook_url, timeout=10)
            print(f"🌐 Вебхук установлен: {response.json()}")
        except Exception as e:
            print(f"⚠️ Не удалось установить вебхук: {e}")
    
    print(f"🚀 Публичный сервер запущен на порту {port}")
    print(f"🔗 URL: {SERVER_URL}")
    
    app.run(host='0.0.0.0', port=port, debug=False)
