# pip install pyTelegramBotAPI SQLAlchemy psycopg2-binary APScheduler pytz requests
# В Railway.app укажи переменные окружения: BOT_TOKEN, DATABASE_URL, DEFAPI_KEY (опц.)

import os
import io
import time
import datetime
import requests
import telebot
from telebot import types
from sqlalchemy import create_engine, Column, Integer, String, Date, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

# ─── Настройки ───────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ['BOT_TOKEN']
DATABASE_URL = os.environ['DATABASE_URL']
# Railway иногда выдаёт postgres:// вместо postgresql://
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

CHAT_ID = int(os.environ.get('CHAT_ID', '0'))  # ID группы для авто-poll
ADMIN_IDS = [int(x) for x in os.environ.get('ADMIN_IDS', '123456789').split(',')]
DEFAPI_KEY = os.environ.get('DEFAPI_KEY', '')  # ключ DefAPI для генерации картинок
TZ = pytz.timezone('Europe/Moscow')

DEFAULT_TEAM_NAMES = ['Красные', 'Зелёные', 'Синие', 'Жёлтые', 'Белые', 'Чёрные']

bot = telebot.TeleBot(BOT_TOKEN)

# ─── База данных ─────────────────────────────────────────────────────────────

engine = create_engine(DATABASE_URL)
Base = declarative_base()
Session = sessionmaker(bind=engine)


class DailyVote(Base):
    __tablename__ = 'daily_votes'
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False)
    user_id = Column(Integer, nullable=False)
    username = Column(String, nullable=False)
    display_name = Column(String, nullable=True)  # имя + фамилия из Telegram
    choice = Column(Integer, nullable=False)  # 0,1,2 — индексы вариантов


class TeamToday(Base):
    __tablename__ = 'teams_today'
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False)
    team_number = Column(Integer, nullable=False)
    team_name = Column(String, nullable=False)
    player_username = Column(String, nullable=False)


class Match(Base):
    __tablename__ = 'matches'
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False)
    team_a_num = Column(Integer, nullable=False)
    score_a = Column(Integer, nullable=False, default=0)
    team_b_num = Column(Integer, nullable=False)
    score_b = Column(Integer, nullable=False, default=0)
    month = Column(String, nullable=False)  # "2026-02"


class Goal(Base):
    __tablename__ = 'goals'
    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(Integer, ForeignKey('matches.id'), nullable=False)
    player_username = Column(String, nullable=False)
    goals_count = Column(Integer, nullable=False, default=0)


class PlayerStat(Base):
    __tablename__ = 'player_stats'
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, nullable=False)
    month = Column(String, nullable=False)  # "2026-02"
    goals = Column(Integer, nullable=False, default=0)
    matches = Column(Integer, nullable=False, default=0)
    wins = Column(Integer, nullable=False, default=0)


class TeamStat(Base):
    __tablename__ = 'team_stats'
    id = Column(Integer, primary_key=True, autoincrement=True)
    team_num_or_name = Column(String, nullable=False)
    month = Column(String, nullable=False)
    points = Column(Integer, nullable=False, default=0)
    goals_scored = Column(Integer, nullable=False, default=0)
    goals_conceded = Column(Integer, nullable=False, default=0)


Base.metadata.create_all(engine)

# ─── Миграции (добавление новых колонок) ────────────────────────────────────

with engine.connect() as _conn:
    try:
        _conn.execute(
            __import__('sqlalchemy').text(
                "ALTER TABLE daily_votes ADD COLUMN display_name VARCHAR"
            )
        )
        _conn.commit()
    except Exception:
        _conn.rollback()  # колонка уже существует — ничего не делаем

# ─── Состояния для /record ───────────────────────────────────────────────────

user_states = {}  # {user_id: {'step': 1, 'team_a': ..., 'team_b': ..., ...}}

# ─── Вспомогательные функции ─────────────────────────────────────────────────


def today():
    return datetime.datetime.now(TZ).date()


def current_month():
    return today().strftime('%Y-%m')


def is_admin(user_id):
    return user_id in ADMIN_IDS


def get_today_players(session):
    """Игроки сегодня: кто выбрал 0 (абонемент) или 2 (разовая)."""
    votes = session.query(DailyVote).filter(
        DailyVote.date == today(),
        DailyVote.choice.in_([0, 2])  # индексы 0 и 2
    ).all()
    return votes


def get_today_teams(session):
    """Все команды на сегодня."""
    return session.query(TeamToday).filter(TeamToday.date == today()).all()


def get_team_names_today(session):
    """Словарь {номер_команды: название}."""
    teams = get_today_teams(session)
    names = {}
    for t in teams:
        names[t.team_number] = t.team_name
    return names


def get_display_names(session):
    """Словарь {username: display_name} для сегодняшних голосов."""
    votes = session.query(DailyVote).filter(DailyVote.date == today()).all()
    return {v.username: v.display_name for v in votes if v.display_name}


# ─── Авто-poll каждый четверг в 9:00 ────────────────────────────────────────

def send_thursday_poll():
    if CHAT_ID == 0:
        return
    bot.send_poll(
        chat_id=CHAT_ID,
        question='Сегодня вечером играем?',
        options=[
            'Играю по абонементу',
            'Не играю сегодня',
            'Хочу вписаться за разовую',
        ],
        is_anonymous=False,
        allows_multiple_answers=False,
    )


scheduler = BackgroundScheduler(timezone=TZ)
scheduler.add_job(send_thursday_poll, 'cron', day_of_week='thu', hour=9, minute=0)
scheduler.start()

# ─── Обработка ответов на poll ───────────────────────────────────────────────


@bot.poll_answer_handler()
def handle_poll_answer(poll_answer):
    """Сохраняем голос пользователя."""
    user = poll_answer.user
    uid = user.id
    uname = user.username or user.first_name or str(uid)
    # Собираем реальное имя из профиля Telegram
    name_parts = [user.first_name or '', user.last_name or '']
    display_name = ' '.join(p for p in name_parts if p).strip() or None
    option_ids = poll_answer.option_ids
    if not option_ids:
        return  # голос отозван
    choice = option_ids[0]

    session = Session()
    try:
        # Удаляем старый голос за сегодня, если есть
        session.query(DailyVote).filter(
            DailyVote.date == today(), DailyVote.user_id == uid
        ).delete()
        session.add(DailyVote(
            date=today(), user_id=uid, username=uname,
            display_name=display_name, choice=choice,
        ))
        session.commit()
    finally:
        session.close()


# ─── Команды бота ────────────────────────────────────────────────────────────

@bot.message_handler(commands=['start', 'help'])
def cmd_help(msg):
    text = (
        "Бот для организации игр\n\n"
        "Команды админа:\n"
        "/players — игроки на сегодня\n"
        "/clear_today — очистить данные дня\n"
        "/set_teams N — задать кол-во команд (2–6)\n"
        "/set_team_name N Имя — название команды\n"
        "/add_to_team N @user — добавить в команду\n"
        "/teams — показать составы\n"
        "/record — записать матч\n"
        "/reset_month — обнулить статистику месяца\n"
        "/poll — отправить poll вручную\n"
        "/mvp — poll MVP дня\n\n"
        "Для всех:\n"
        "/stats — вся статистика\n"
        "/stats_day — статистика дня\n"
        "/stats_month — статистика месяца\n"
        "/stats_day_img — день картинкой\n"
        "/stats_month_img — месяц картинкой"
    )
    bot.reply_to(msg, text)


@bot.message_handler(commands=['players'])
def cmd_players(msg):
    if not is_admin(msg.from_user.id):
        return
    session = Session()
    try:
        players = get_today_players(session)
        if not players:
            bot.reply_to(msg, 'Сегодня пока никто не записался.')
            return
        lines = []
        for p in players:
            label = 'абонемент' if p.choice == 0 else 'разовая'
            name = f'{p.display_name} ' if p.display_name else ''
            lines.append(f'{name}@{p.username} ({label})')
        bot.reply_to(msg, f'Игроки на сегодня ({len(lines)}):\n' + '\n'.join(lines))
    finally:
        session.close()


@bot.message_handler(commands=['clear_today'])
def cmd_clear_today(msg):
    if not is_admin(msg.from_user.id):
        return
    session = Session()
    try:
        session.query(DailyVote).filter(DailyVote.date == today()).delete()
        session.query(TeamToday).filter(TeamToday.date == today()).delete()
        # Удаляем голы перед матчами (FK goals.match_id → matches.id)
        today_match_ids = [
            m.id for m in session.query(Match.id).filter(Match.date == today()).all()
        ]
        if today_match_ids:
            session.query(Goal).filter(Goal.match_id.in_(today_match_ids)).delete()
        session.query(Match).filter(Match.date == today()).delete()
        session.commit()
        bot.reply_to(msg, 'Данные сегодняшнего дня очищены.')
    finally:
        session.close()


@bot.message_handler(commands=['set_teams'])
def cmd_set_teams(msg):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(msg, 'Формат: /set_teams N (2–6)')
        return
    n = int(parts[1])
    if n < 2 or n > 6:
        bot.reply_to(msg, 'Кол-во команд: от 2 до 6.')
        return
    session = Session()
    try:
        # Удаляем старые команды сегодня
        session.query(TeamToday).filter(TeamToday.date == today()).delete()
        # Создаём пустые команды с дефолтными названиями
        for i in range(1, n + 1):
            session.add(TeamToday(
                date=today(), team_number=i,
                team_name=DEFAULT_TEAM_NAMES[i - 1],
                player_username='__placeholder__'
            ))
        session.commit()
        names = [f'{i}. {DEFAULT_TEAM_NAMES[i - 1]}' for i in range(1, n + 1)]
        bot.reply_to(msg, f'Создано {n} команд:\n' + '\n'.join(names))
    finally:
        session.close()


@bot.message_handler(commands=['set_team_name'])
def cmd_set_team_name(msg):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        bot.reply_to(msg, 'Формат: /set_team_name N Название')
        return
    num = int(parts[1])
    name = parts[2].strip()
    session = Session()
    try:
        rows = session.query(TeamToday).filter(
            TeamToday.date == today(), TeamToday.team_number == num
        ).all()
        if not rows:
            bot.reply_to(msg, f'Команда #{num} не найдена. Сначала /set_teams.')
            return
        for row in rows:
            row.team_name = name
        session.commit()
        bot.reply_to(msg, f'Команда #{num} → «{name}»')
    finally:
        session.close()


@bot.message_handler(commands=['add_to_team'])
def cmd_add_to_team(msg):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) < 3 or not parts[1].isdigit():
        bot.reply_to(msg, 'Формат: /add_to_team N @username')
        return
    num = int(parts[1])
    username = parts[2].lstrip('@')
    session = Session()
    try:
        # Проверяем, что игрок в сегодняшнем списке
        players = get_today_players(session)
        player_names = [p.username for p in players]
        if username not in player_names:
            bot.reply_to(msg, f'@{username} нет в списке сегодняшних игроков.')
            return
        # Проверяем, что команда существует
        team_exists = session.query(TeamToday).filter(
            TeamToday.date == today(), TeamToday.team_number == num
        ).first()
        if not team_exists:
            bot.reply_to(msg, f'Команда #{num} не найдена.')
            return
        team_name = team_exists.team_name
        # Убираем игрока из других команд сегодня
        session.query(TeamToday).filter(
            TeamToday.date == today(),
            TeamToday.player_username == username
        ).delete()
        session.add(TeamToday(
            date=today(), team_number=num,
            team_name=team_name, player_username=username
        ))
        session.commit()
        bot.reply_to(msg, f'@{username} добавлен в команду #{num} «{team_name}»')
    finally:
        session.close()


@bot.message_handler(commands=['teams'])
def cmd_teams(msg):
    session = Session()
    try:
        teams = session.query(TeamToday).filter(
            TeamToday.date == today(),
            TeamToday.player_username != '__placeholder__'
        ).order_by(TeamToday.team_number).all()
        if not teams:
            bot.reply_to(msg, 'Команды на сегодня не сформированы.')
            return
        dn = get_display_names(session)
        # Группируем по номеру
        from collections import defaultdict
        grouped = defaultdict(list)
        team_names_map = {}
        for t in teams:
            grouped[t.team_number].append(t.player_username)
            team_names_map[t.team_number] = t.team_name
        lines = []
        for num in sorted(grouped.keys()):
            name = team_names_map.get(num, f'Команда {num}')
            parts = []
            for u in grouped[num]:
                real = dn.get(u)
                parts.append(f'{real} @{u}' if real else f'@{u}')
            lines.append(f'#{num} «{name}»: {", ".join(parts)}')
        bot.reply_to(msg, 'Составы на сегодня:\n' + '\n'.join(lines))
    finally:
        session.close()


# ─── /record — пошаговая запись матча ────────────────────────────────────────

@bot.message_handler(commands=['record'])
def cmd_record(msg):
    if not is_admin(msg.from_user.id):
        return
    session = Session()
    try:
        names = get_team_names_today(session)
    finally:
        session.close()
    if not names:
        bot.reply_to(msg, 'Сначала создай команды: /set_teams')
        return
    # Шаг 1: выбрать команду A
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for num in sorted(names.keys()):
        markup.add(f'{num}. {names[num]}')
    user_states[msg.from_user.id] = {'step': 1, 'teams': names}
    bot.reply_to(msg, 'Шаг 1: Выбери команду A:', reply_markup=markup)


@bot.message_handler(func=lambda m: m.from_user.id in user_states)
def handle_record_steps(msg):
    uid = msg.from_user.id
    state = user_states[uid]
    text = msg.text.strip()

    # Отмена
    if text.lower() in ('/cancel', 'отмена'):
        del user_states[uid]
        bot.reply_to(msg, 'Запись отменена.', reply_markup=types.ReplyKeyboardRemove())
        return

    step = state['step']
    teams = state['teams']

    # Шаг 1: выбор команды A
    if step == 1:
        num = _parse_team_choice(text, teams)
        if num is None:
            bot.reply_to(msg, 'Выбери команду из списка.')
            return
        state['team_a'] = num
        state['step'] = 2
        # Клавиатура без выбранной команды
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        for n in sorted(teams.keys()):
            if n != num:
                markup.add(f'{n}. {teams[n]}')
        bot.reply_to(msg, 'Шаг 2: Выбери команду B:', reply_markup=markup)

    # Шаг 2: выбор команды B
    elif step == 2:
        num = _parse_team_choice(text, teams)
        if num is None or num == state['team_a']:
            bot.reply_to(msg, 'Выбери другую команду.')
            return
        state['team_b'] = num
        state['step'] = 3
        bot.reply_to(msg, 'Шаг 3: Введи счёт (например, 5:3):',
                     reply_markup=types.ReplyKeyboardRemove())

    # Шаг 3: ввод счёта
    elif step == 3:
        if ':' not in text:
            bot.reply_to(msg, 'Формат счёта: 5:3')
            return
        parts = text.split(':')
        if len(parts) != 2 or not parts[0].strip().isdigit() or not parts[1].strip().isdigit():
            bot.reply_to(msg, 'Формат счёта: 5:3')
            return
        state['score_a'] = int(parts[0].strip())
        state['score_b'] = int(parts[1].strip())
        state['step'] = 4
        state['goals'] = []
        # Получаем игроков обеих команд для inline-кнопок
        session = Session()
        try:
            dn = get_display_names(session)
            team_players = session.query(TeamToday).filter(
                TeamToday.date == today(),
                TeamToday.team_number.in_([state['team_a'], state['team_b']]),
                TeamToday.player_username != '__placeholder__'
            ).all()
            state['match_players'] = [t.player_username for t in team_players]
            state['display_names'] = dn
        finally:
            session.close()
        _send_goals_keyboard(msg.chat.id, state, 'Шаг 4: Кто забил? Нажми на игрока:')

    # Шаг 4 (подшаг): ввод кол-ва голов текстом
    elif step == 4 and state.get('awaiting_count_for'):
        username = state['awaiting_count_for']
        if not text.isdigit() or int(text) < 1:
            bot.reply_to(msg, 'Введи число голов (1, 2, 3, ...).')
            return
        count = int(text)
        state['goals'].append({'username': username, 'count': count})
        del state['awaiting_count_for']
        dn = state.get('display_names', {})
        real = dn.get(username)
        label = f'{real} (@{username})' if real else f'@{username}'
        bot.reply_to(msg, f'{label}: {count} гол.')
        _send_goals_keyboard(msg.chat.id, state, 'Кто ещё забил?')


def _parse_team_choice(text, teams):
    """Парсит выбор команды из текста вида '1. Красные' или просто '1'."""
    num_str = text.split('.')[0].strip()
    if num_str.isdigit():
        num = int(num_str)
        if num in teams:
            return num
    return None


def _send_goals_keyboard(chat_id, state, text):
    """Отправляет inline-клавиатуру с игроками и кнопкой Готово."""
    dn = state.get('display_names', {})
    players = state.get('match_players', [])
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    for u in players:
        real = dn.get(u)
        label = f'{real} (@{u})' if real else f'@{u}'
        buttons.append(types.InlineKeyboardButton(label, callback_data=f'goal_player:{u}'))
    # Добавляем кнопки по 2 в ряд
    for i in range(0, len(buttons), 2):
        markup.row(*buttons[i:i+2])
    markup.row(types.InlineKeyboardButton('Готово', callback_data='goal_done'))

    # Текущие голы
    if state.get('goals'):
        tally = []
        for g in state['goals']:
            real = dn.get(g['username'])
            name = f'{real} (@{g["username"]})' if real else f'@{g["username"]}'
            tally.append(f'  {name}: {g["count"]} гол.')
        text += '\n\nЗаписаны голы:\n' + '\n'.join(tally)

    bot.send_message(chat_id, text, reply_markup=markup)



@bot.callback_query_handler(func=lambda call: call.data.startswith('goal_'))
def handle_goal_callback(call):
    uid = call.from_user.id
    if uid not in user_states or user_states[uid].get('step') != 4:
        bot.answer_callback_query(call.id, 'Сессия записи не активна.')
        return
    state = user_states[uid]
    data = call.data

    if data == 'goal_done':
        bot.answer_callback_query(call.id)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        _finish_match(call.message, state)
        del user_states[uid]

    elif data.startswith('goal_player:'):
        username = data.split(':', 1)[1]
        state['awaiting_count_for'] = username
        bot.answer_callback_query(call.id)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        dn = state.get('display_names', {})
        real = dn.get(username)
        label = f'{real} (@{username})' if real else f'@{username}'
        bot.send_message(
            call.message.chat.id,
            f'Сколько голов у {label}? Введи число:',
        )


def _finish_match(msg, state):
    """Сохраняем матч и обновляем статистику."""
    session = Session()
    try:
        month = current_month()
        match = Match(
            date=today(),
            team_a_num=state['team_a'],
            score_a=state['score_a'],
            team_b_num=state['team_b'],
            score_b=state['score_b'],
            month=month,
        )
        session.add(match)
        session.flush()  # получаем match.id

        # Сохраняем голы
        for g in state.get('goals', []):
            session.add(Goal(
                match_id=match.id,
                player_username=g['username'],
                goals_count=g['count'],
            ))

        # Обновляем статистику игроков
        _update_player_stats(session, state, month)
        # Обновляем статистику команд
        _update_team_stats(session, state, month)

        session.commit()

        ta = state['teams'].get(state['team_a'], f"#{state['team_a']}")
        tb = state['teams'].get(state['team_b'], f"#{state['team_b']}")
        bot.reply_to(
            msg,
            f'Матч записан: «{ta}» {state["score_a"]}:{state["score_b"]} «{tb}»',
            reply_markup=types.ReplyKeyboardRemove(),
        )
    finally:
        session.close()


def _update_player_stats(session, state, month):
    """Обновляем player_stats для всех игроков команд A и B."""
    team_a_num = state['team_a']
    team_b_num = state['team_b']
    sa, sb = state['score_a'], state['score_b']

    # Собираем игроков каждой команды
    all_team_players = session.query(TeamToday).filter(
        TeamToday.date == today(),
        TeamToday.team_number.in_([team_a_num, team_b_num]),
        TeamToday.player_username != '__placeholder__'
    ).all()

    team_a_players = [t.player_username for t in all_team_players if t.team_number == team_a_num]
    team_b_players = [t.player_username for t in all_team_players if t.team_number == team_b_num]

    # Голы по username
    goals_map = {g['username']: g['count'] for g in state.get('goals', [])}

    for username in team_a_players + team_b_players:
        is_team_a = username in team_a_players
        win = (sa > sb and is_team_a) or (sb > sa and not is_team_a)

        stat = session.query(PlayerStat).filter(
            PlayerStat.username == username, PlayerStat.month == month
        ).first()
        if not stat:
            stat = PlayerStat(username=username, month=month, goals=0, matches=0, wins=0)
            session.add(stat)
        stat.matches += 1
        stat.goals += goals_map.get(username, 0)
        if win:
            stat.wins += 1


def _update_team_stats(session, state, month):
    """Обновляем team_stats: очки, голы забитые/пропущенные."""
    sa, sb = state['score_a'], state['score_b']
    teams_map = state.get('teams', {})
    for num, score_for, score_against in [
        (state['team_a'], sa, sb),
        (state['team_b'], sb, sa),
    ]:
        name = teams_map.get(num, str(num))
        key = f'{num}_{name}'
        stat = session.query(TeamStat).filter(
            TeamStat.team_num_or_name == key, TeamStat.month == month
        ).first()
        if not stat:
            stat = TeamStat(team_num_or_name=key, month=month,
                            points=0, goals_scored=0, goals_conceded=0)
            session.add(stat)
        stat.goals_scored += score_for
        stat.goals_conceded += score_against
        if score_for > score_against:
            stat.points += 3
        elif score_for == score_against:
            stat.points += 1


# ─── /stats ──────────────────────────────────────────────────────────────────

def _build_day_stats_text(session):
    """Статистика за сегодняшний игровой день."""
    dn = get_display_names(session)
    team_names = get_team_names_today(session)
    lines = [f'Игровой день {today()}']
    lines.append('━' * 24)

    # Составы
    from collections import defaultdict
    team_rows = session.query(TeamToday).filter(
        TeamToday.date == today(),
        TeamToday.player_username != '__placeholder__'
    ).order_by(TeamToday.team_number).all()
    if team_rows:
        grouped = defaultdict(list)
        tnames = {}
        for t in team_rows:
            grouped[t.team_number].append(t.player_username)
            tnames[t.team_number] = t.team_name
        lines.append('')
        lines.append('Составы:')
        for num in sorted(grouped.keys()):
            name = tnames.get(num, f'Команда {num}')
            parts = []
            for u in grouped[num]:
                real = dn.get(u)
                parts.append(f'{real} (@{u})' if real else f'@{u}')
            lines.append(f'  {name}: {", ".join(parts)}')

    # Матчи
    today_matches = session.query(Match).filter(Match.date == today()).all()
    if today_matches:
        lines.append('')
        lines.append('Матчи:')
        for m in today_matches:
            na = team_names.get(m.team_a_num, f'Команда {m.team_a_num}')
            nb = team_names.get(m.team_b_num, f'Команда {m.team_b_num}')
            lines.append(f'  {na}  {m.score_a} : {m.score_b}  {nb}')

    # Голы дня
    if today_matches:
        match_ids = [m.id for m in today_matches]
        goals = session.query(Goal).filter(Goal.match_id.in_(match_ids)).all()
        if goals:
            from collections import Counter
            totals = Counter()
            for g in goals:
                totals[g.player_username] += g.goals_count
            lines.append('')
            lines.append('Бомбардиры дня:')
            for uname, cnt in totals.most_common():
                real = dn.get(uname)
                label = f'{real} (@{uname})' if real else f'@{uname}'
                lines.append(f'  {label}: {cnt} гол.')

    return '\n'.join(lines) if len(lines) > 2 else None


def _build_month_stats_text(session):
    """Статистика за текущий месяц."""
    month = current_month()
    dn = get_display_names(session)
    lines = [f'Статистика за {month}']
    lines.append('━' * 24)

    # Команды
    teams = session.query(TeamStat).filter(
        TeamStat.month == month
    ).order_by(TeamStat.points.desc()).all()
    if teams:
        lines.append('')
        lines.append('Команды:')
        for t in teams:
            tname = t.team_num_or_name.split('_', 1)[1] if '_' in t.team_num_or_name else t.team_num_or_name
            lines.append(
                f'  {tname}: {t.points} очк. | '
                f'{t.goals_scored} заб. | {t.goals_conceded} проп.'
            )

    # Игроки
    players = session.query(PlayerStat).filter(
        PlayerStat.month == month
    ).order_by(PlayerStat.goals.desc()).all()
    if players:
        lines.append('')
        lines.append('Игроки:')
        for i, p in enumerate(players, 1):
            real = dn.get(p.username)
            name = f'{real} (@{p.username})' if real else f'@{p.username}'
            lines.append(
                f'  {i}. {name}\n'
                f'      {p.goals} гол. | {p.matches} матч. | {p.wins} побед'
            )

    return '\n'.join(lines) if len(lines) > 2 else None


@bot.message_handler(commands=['stats'])
def cmd_stats(msg):
    """Показывает и дневную, и месячную статистику."""
    session = Session()
    try:
        day = _build_day_stats_text(session)
        month = _build_month_stats_text(session)
        parts = [p for p in [day, month] if p]
        bot.reply_to(msg, '\n\n'.join(parts) if parts else 'Статистика пока пуста.')
    finally:
        session.close()


@bot.message_handler(commands=['stats_day'])
def cmd_stats_day(msg):
    session = Session()
    try:
        text = _build_day_stats_text(session)
        bot.reply_to(msg, text or 'Сегодня матчей ещё не было.')
    finally:
        session.close()


@bot.message_handler(commands=['stats_month'])
def cmd_stats_month(msg):
    session = Session()
    try:
        text = _build_month_stats_text(session)
        bot.reply_to(msg, text or 'Статистика за месяц пока пуста.')
    finally:
        session.close()


# ─── Генерация картинок (Nano Banana Pro через DefAPI) ───────────────────────

def _defapi_generate_image(prompt):
    """Отправляет запрос в DefAPI и возвращает URL картинки или None."""
    if not DEFAPI_KEY:
        print('[DefAPI] DEFAPI_KEY не задан')
        return None
    headers = {
        'Authorization': f'Bearer {DEFAPI_KEY}',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }
    try:
        resp = requests.post(
            'https://api.defapi.org/api/image/gen',
            headers=headers,
            json={'model': 'google/nano-banana-pro', 'prompt': prompt},
            timeout=30,
        )
        print(f'[DefAPI] POST /api/image/gen → {resp.status_code}')
        data = resp.json()
        print(f'[DefAPI] Response: {data}')
        if data.get('code') != 0:
            print(f'[DefAPI] Error: {data.get("message")} | {data.get("detail")}')
            return None
        task_id = data['data']['task_id']
        print(f'[DefAPI] Task created: {task_id}')
    except Exception as e:
        print(f'[DefAPI] POST failed: {e}')
        return None

    for attempt in range(20):
        time.sleep(3)
        try:
            r = requests.get(
                f'https://api.defapi.org/api/task/query?task_id={task_id}',
                headers=headers,
                timeout=15,
            )
            task_resp = r.json()
            task_data = task_resp.get('data', {})
            status = task_data.get('status')
            print(f'[DefAPI] Poll #{attempt+1}: status={status}')
            if status in ('success', 'completed'):
                result = task_data.get('result')
                print(f'[DefAPI] Result type={type(result).__name__}, value={str(result)[:200]}')
                if not result:
                    return None
                if isinstance(result, str):
                    return result
                if isinstance(result, list) and result:
                    item = result[0]
                    return item if isinstance(item, str) else item.get('url')
                if isinstance(result, dict):
                    return result.get('url') or result.get('image')
                return None
            if status == 'failed':
                reason = task_data.get('status_reason', {})
                print(f'[DefAPI] Task failed: {reason}')
                return None
        except Exception as e:
            print(f'[DefAPI] Poll #{attempt+1} error: {e}')
    print('[DefAPI] Timeout: task did not complete in 60s')
    return None


def _send_stats_image(msg, stats_text, caption):
    """Генерирует картинку из текста статистики и отправляет в чат."""
    if not DEFAPI_KEY:
        bot.reply_to(msg, 'DEFAPI_KEY не настроен.')
        return
    if not stats_text:
        bot.reply_to(msg, 'Нет данных для генерации.')
        return

    bot.reply_to(msg, 'Генерирую картинку...')

    prompt = (
        "Create a beautiful dark-themed sports statistics infographic card for a football (soccer) group. "
        "Use a sleek modern design with a dark gradient background (dark blue to black). "
        "Include a football icon at the top. "
        "Render ALL the following text EXACTLY as shown, with clear readable white font:\n\n"
        f"{stats_text}\n\n"
        "Use clean typography, subtle neon accent lines as separators, and a professional sports layout. "
        "The text must be perfectly legible and rendered exactly as provided above."
    )

    image_url = _defapi_generate_image(prompt)
    if not image_url:
        bot.reply_to(msg, 'Не удалось сгенерировать картинку.')
        return
    try:
        img_resp = requests.get(image_url, timeout=30)
        img_resp.raise_for_status()
        bot.send_photo(msg.chat.id, io.BytesIO(img_resp.content), caption=caption)
    except Exception:
        bot.reply_to(msg, 'Не удалось загрузить картинку.')


@bot.message_handler(commands=['stats_day_img'])
def cmd_stats_day_img(msg):
    session = Session()
    try:
        text = _build_day_stats_text(session)
    finally:
        session.close()
    _send_stats_image(msg, text, f'Игровой день {today()}')


@bot.message_handler(commands=['stats_month_img'])
def cmd_stats_month_img(msg):
    session = Session()
    try:
        text = _build_month_stats_text(session)
    finally:
        session.close()
    _send_stats_image(msg, text, f'Статистика за {current_month()}')


# ─── /reset_month ────────────────────────────────────────────────────────────

@bot.message_handler(commands=['reset_month'])
def cmd_reset_month(msg):
    if not is_admin(msg.from_user.id):
        return
    session = Session()
    try:
        month = current_month()
        session.query(PlayerStat).filter(PlayerStat.month == month).delete()
        session.query(TeamStat).filter(TeamStat.month == month).delete()
        session.commit()
        bot.reply_to(msg, f'Статистика за {month} обнулена.')
    finally:
        session.close()


# ─── /poll — ручная отправка poll-а в чат ─────────────────────────────────────

@bot.message_handler(commands=['poll'])
def cmd_poll(msg):
    if not is_admin(msg.from_user.id):
        return
    bot.send_poll(
        chat_id=msg.chat.id,
        question='Сегодня вечером играем?',
        options=[
            'Играю по абонементу',
            'Не играю сегодня',
            'Хочу вписаться за разовую',
        ],
        is_anonymous=False,
        allows_multiple_answers=False,
    )


# ─── /mvp ────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=['mvp'])
def cmd_mvp(msg):
    if not is_admin(msg.from_user.id):
        return
    session = Session()
    try:
        teams = session.query(TeamToday).filter(
            TeamToday.date == today(),
            TeamToday.player_username != '__placeholder__'
        ).all()
        if not teams:
            bot.reply_to(msg, 'Нет игроков в командах на сегодня.')
            return
        dn = get_display_names(session)
        usernames = list(set(t.player_username for t in teams))
        options = []
        for u in usernames:
            real = dn.get(u)
            options.append(f'{real} @{u}' if real else f'@{u}')
        options.append('Никто')
        bot.send_poll(
            chat_id=msg.chat.id,
            question='MVP дня?',
            options=options,
            is_anonymous=False,
            allows_multiple_answers=False,
        )
    finally:
        session.close()


# ─── Запуск ──────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('Бот запущен...')
    bot.polling(none_stop=True)
