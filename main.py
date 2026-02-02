import os
import json
import random
from datetime import datetime
from flask import Flask, request, jsonify, g
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3

app = Flask(__name__)
CORS(app)
app.config['DATABASE'] = 'golf_games.db'
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')

# ============= БАЗА ДАННЫХ =============
def get_db_connection():
    conn = sqlite3.connect(app.config['DATABASE'])
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with app.app_context():
        conn = get_db_connection()
        
        # Таблица устройств
        conn.execute('''
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица игр (ОБНОВЛЕНО: добавлены поля для новой логики)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS games (
                code TEXT PRIMARY KEY,
                device_id TEXT,
                current_hole INTEGER DEFAULT 1,
                total_shots INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed BOOLEAN DEFAULT FALSE,
                remaining_distance INTEGER DEFAULT 0,  -- Остаток до лунки
                total_distance INTEGER DEFAULT 0,      -- Общая дистанция лунки
                FOREIGN KEY (device_id) REFERENCES devices (id)
            )
        ''')
        
        # Таблица бросков
        conn.execute('''
            CREATE TABLE IF NOT EXISTS shots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_code TEXT,
                hole INTEGER,
                revolutions INTEGER,
                remaining_before INTEGER,  -- Остаток до броска
                remaining_after INTEGER,   -- Остаток после броска
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_code) REFERENCES games (code)
            )
        ''')
        
        # Таблица лунок (предопределенные 18 лунок)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS holes (
                number INTEGER PRIMARY KEY,
                name TEXT,
                par INTEGER,
                target_distance INTEGER,  -- Полная дистанция лунки
                tolerance INTEGER,        -- Допуск (радиус лунки)
                difficulty TEXT
            )
        ''')
        
        # Заполняем таблицу лунок, если она пустая
        cursor = conn.execute('SELECT COUNT(*) as count FROM holes')
        if cursor.fetchone()['count'] == 0:
            holes = [
                (1, 'Стартовая', 3, 100, 5, 'легкая'),
                (2, 'Прямая', 4, 150, 7, 'средняя'),
                (3, 'Поворот', 3, 120, 6, 'легкая'),
                (4, 'Длинная', 5, 200, 8, 'сложная'),
                (5, 'Короткая', 3, 90, 4, 'легкая'),
                (6, 'Ветреная', 4, 160, 7, 'средняя'),
                (7, 'Точная', 3, 110, 5, 'средняя'),
                (8, 'Холмистая', 4, 170, 8, 'сложная'),
                (9, 'Финишная', 4, 140, 6, 'средняя'),
                (10, 'Разворот', 3, 105, 5, 'легкая'),
                (11, 'Спираль', 4, 155, 7, 'средняя'),
                (12, 'Зигзаг', 3, 115, 6, 'средняя'),
                (13, 'Мост', 5, 190, 8, 'сложная'),
                (14, 'Водная', 3, 95, 4, 'легкая'),
                (15, 'Песчаная', 4, 165, 7, 'сложная'),
                (16, 'Лесная', 3, 125, 6, 'средняя'),
                (17, 'Скалистая', 4, 175, 8, 'сложная'),
                (18, 'Финальная', 4, 130, 5, 'средняя')
            ]
            conn.executemany('''
                INSERT INTO holes (number, name, par, target_distance, tolerance, difficulty)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', holes)
        
        conn.commit()
        conn.close()

# ============= API МАРШРУТЫ =============

@app.route('/')
def index():
    return jsonify({
        'status': 'online',
        'service': 'Spinner Golf API',
        'version': '3.1',
        'message': 'Новая логика: remaining = |remaining - revolutions|'
    })

@app.route('/api/status')
def api_status():
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now().isoformat(),
        'logic': 'remaining = |remaining - revolutions|'
    })

# Получение информации об игре
@app.route('/api/get_game', methods=['GET'])
def get_game():
    game_code = request.args.get('code')
    
    if not game_code:
        return jsonify({'success': False, 'error': 'No code provided'}), 400

    conn = get_db_connection()
    
    # Ищем игру
    game = conn.execute('''
        SELECT g.*, h.target_distance, h.tolerance
        FROM games g
        LEFT JOIN holes h ON g.current_hole = h.number
        WHERE g.code = ?
    ''', (game_code,)).fetchone()
    
    if not game:
        conn.close()
        return jsonify({'success': False, 'error': 'Game not found'}), 404
    
    # Если игра новая, устанавливаем начальные значения
    if game['remaining_distance'] == 0 and game['current_hole'] == 1:
        remaining_distance = game['target_distance']
        total_distance = game['target_distance']
        
        # Обновляем игру с начальными значениями
        conn.execute('''
            UPDATE games 
            SET remaining_distance = ?, total_distance = ?
            WHERE code = ?
        ''', (remaining_distance, total_distance, game_code))
        conn.commit()
    else:
        remaining_distance = game['remaining_distance']
        total_distance = game['total_distance']
    
    conn.close()
    
    return jsonify({
        'success': True,
        'current_hole': game['current_hole'],
        'target': game['target_distance'],
        'tolerance': game['tolerance'],
        'remaining': remaining_distance,  # ОБНОВЛЕНО: возвращаем remaining
        'total': total_distance,
        'completed': bool(game['completed'])
    })

# Создание новой игры
@app.route('/api/create_game', methods=['POST'])
def create_game():
    data = request.get_json()
    device_id = data.get('device_id')
    
    if not device_id:
        return jsonify({'success': False, 'error': 'No device ID provided'}), 400
    
    # Генерируем уникальный код игры
    game_code = ''.join(random.choices('ABCDEFGHJKLMNPQRSTUVWXYZ23456789', k=6))
    
    conn = get_db_connection()
    
    # Регистрируем устройство, если его нет
    device = conn.execute('SELECT * FROM devices WHERE id = ?', (device_id,)).fetchone()
    if not device:
        conn.execute('INSERT INTO devices (id, name) VALUES (?, ?)', (device_id, 'New Device'))
    
    # Получаем параметры первой лунки
    first_hole = conn.execute('SELECT * FROM holes WHERE number = 1').fetchone()
    
    # Создаем новую игру (ОБНОВЛЕНО: инициализируем remaining и total)
    conn.execute('''
        INSERT INTO games (code, device_id, current_hole, remaining_distance, total_distance)
        VALUES (?, ?, ?, ?, ?)
    ''', (game_code, device_id, 1, first_hole['target_distance'], first_hole['target_distance']))
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True,
        'game_code': game_code,
        'message': 'Game created successfully',
        'hole': 1,
        'target': first_hole['target_distance'],
        'tolerance': first_hole['tolerance'],
        'remaining': first_hole['target_distance']  # ОБНОВЛЕНО: возвращаем remaining
    })

# Отправка результата броска (ОБНОВЛЕНО: новая логика расчета)
@app.route('/api/submit_shot', methods=['POST'])
def submit_shot():
    data = request.get_json()
    
    game_code = data.get('game_code')
    device_id = data.get('device_id')
    revolutions = data.get('revolutions')
    hole = data.get('hole')
    
    if not all([game_code, device_id, revolutions is not None, hole]):
        return jsonify({'success': False, 'error': 'Missing data'}), 400
    
    try:
        revolutions = int(revolutions)
        hole = int(hole)
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid data format'}), 400
    
    conn = get_db_connection()
    
    # Проверяем игру
    game = conn.execute('''
        SELECT g.*, h.target_distance, h.tolerance
        FROM games g
        LEFT JOIN holes h ON g.current_hole = h.number
        WHERE g.code = ? AND g.device_id = ?
    ''', (game_code, device_id)).fetchone()
    
    if not game:
        conn.close()
        return jsonify({'success': False, 'error': 'Game not found'}), 404
    
    if game['completed']:
        conn.close()
        return jsonify({'success': False, 'error': 'Game already completed'}), 400
    
    if game['current_hole'] != hole:
        conn.close()
        return jsonify({'success': False, 'error': 'Wrong hole number'}), 400
    
    # ОБНОВЛЕНО: Новая логика расчета remaining
    old_remaining = game['remaining_distance']
    new_remaining = abs(old_remaining - revolutions)
    tolerance = game['tolerance']
    
    # Сохраняем бросок
    conn.execute('''
        INSERT INTO shots (game_code, hole, revolutions, remaining_before, remaining_after)
        VALUES (?, ?, ?, ?, ?)
    ''', (game_code, hole, revolutions, old_remaining, new_remaining))
    
    # Обновляем игру
    conn.execute('''
        UPDATE games 
        SET remaining_distance = ?, total_shots = total_shots + 1
        WHERE code = ?
    ''', (new_remaining, game_code))
    
    # Проверяем, завершена ли лунка
    hole_completed = new_remaining <= tolerance
    
    response = {
        'success': True,
        'hole_completed': hole_completed,
        'remaining': new_remaining,
        'old_remaining': old_remaining,
        'revolutions': revolutions,
        'tolerance': tolerance
    }
    
    if hole_completed:
        # Лунка завершена
        if hole == 18:
            # Это последняя лунка - игра завершена
            conn.execute('UPDATE games SET completed = TRUE WHERE code = ?', (game_code,))
            response['status'] = 'game_completed'
            response['message'] = 'Игра завершена! Поздравляем!'
        else:
            # Переходим к следующей лунке
            next_hole = hole + 1
            next_hole_info = conn.execute('SELECT * FROM holes WHERE number = ?', (next_hole,)).fetchone()
            
            # ОБНОВЛЕНО: Сбрасываем remaining для новой лунки
            conn.execute('''
                UPDATE games 
                SET current_hole = ?, 
                    remaining_distance = ?,
                    total_distance = ?
                WHERE code = ?
            ''', (next_hole, next_hole_info['target_distance'], 
                  next_hole_info['target_distance'], game_code))
            
            response['status'] = 'hole_completed'
            response['next_hole'] = next_hole
            response['next_target'] = next_hole_info['target_distance']
            response['next_tolerance'] = next_hole_info['tolerance']
            response['new_remaining'] = next_hole_info['target_distance']  # ОБНОВЛЕНО
            response['message'] = f'Лунка {hole} завершена! Переходим к лунке {next_hole}'
    else:
        response['status'] = 'continue'
        response['message'] = f'Продолжайте! Осталось {new_remaining} до лунки'
    
    conn.commit()
    
    # Получаем статистику
    shots = conn.execute('''
        SELECT COUNT(*) as count, SUM(revolutions) as total_revolutions
        FROM shots 
        WHERE game_code = ?
    ''', (game_code,)).fetchone()
    
    conn.close()
    
    response['total_shots'] = shots['count']
    response['total_revolutions'] = shots['total_revolutions'] or 0
    
    return jsonify(response)

# Получение статистики игры
@app.route('/api/game_stats', methods=['GET'])
def game_stats():
    game_code = request.args.get('code')
    
    if not game_code:
        return jsonify({'success': False, 'error': 'No code provided'}), 400
    
    conn = get_db_connection()
    
    game = conn.execute('SELECT * FROM games WHERE code = ?', (game_code,)).fetchone()
    if not game:
        conn.close()
        return jsonify({'success': False, 'error': 'Game not found'}), 404
    
    shots = conn.execute('''
        SELECT * FROM shots 
        WHERE game_code = ? 
        ORDER BY created_at DESC
    ''', (game_code,)).fetchall()
    
    hole_stats = conn.execute('''
        SELECT hole, COUNT(*) as shots, 
               SUM(revolutions) as total_revolutions,
               MIN(remaining_before) as start_distance,
               MIN(remaining_after) as min_remaining
        FROM shots 
        WHERE game_code = ?
        GROUP BY hole
        ORDER BY hole
    ''', (game_code,)).fetchall()
    
    conn.close()
    
    return jsonify({
        'success': True,
        'game': dict(game),
        'shots': [dict(shot) for shot in shots],
        'hole_stats': [dict(stat) for stat in hole_stats],
        'current_hole_info': {
            'hole': game['current_hole'],
            'remaining': game['remaining_distance'],
            'total': game['total_distance']
        }
    })

# Получение всех лунок
@app.route('/api/holes', methods=['GET'])
def get_holes():
    conn = get_db_connection()
    holes = conn.execute('SELECT * FROM holes ORDER BY number').fetchall()
    conn.close()
    
    return jsonify({
        'success': True,
        'holes': [dict(hole) for hole in holes]
    })

# ============= ЗАПУСК СЕРВЕРА =============
if __name__ == '__main__':
    init_db()
    print("=" * 60)
    print("    SPINNER GOLF SERVER v3.1")
    print("    НОВАЯ ЛОГИКА: remaining = |remaining - revolutions|")
    print("=" * 60)
    print("📊 База данных инициализирована")
    print("🌐 Сервер запущен на порту 10000")
    print("🔗 Доступные эндпоинты:")
    print("   GET  /api/status          - Проверка статуса")
    print("   GET  /api/get_game?code=  - Получить игру")
    print("   POST /api/create_game     - Создать игру")
    print("   POST /api/submit_shot     - Отправить бросок")
    print("   GET  /api/game_stats?code - Статистика игры")
    print("   GET  /api/holes           - Все лунки")
    print("=" * 60)
    
    app.run(host='0.0.0.0', port=10000, debug=True)
